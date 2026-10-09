"""提示词运行时缓存。

实现 DB 优先、回退硬编码的读取策略。
- 启动时从数据库 prompt_templates 表加载所有提示词到 _prompt_cache
- get_prompt() 优先返回 DB 内容，缺失时回退到 _ALL_PROMPTS
- reload_prompt_cache() 用于前端编辑保存后强制刷新
- 增强：自动清洗模板文本、缺失变量警告
- 增强：线程安全的缓存操作
- 增强：render_prompt() 统一变量渲染接口
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

from ._registry import (
    _ALL_PROMPTS,
    clean_prompt_text,
    extract_variables,
    has_residual_placeholders,
    render_prompt,
    validate_prompt_variables,
)
# ✅ R48（2026-10-06 · prompts 运行时指标）：热路径渲染计数。
#    _metrics 顶层只依赖 stdlib（collections.Counter），不反向 import _cache/_registry，
#    故此处顶层 import 无循环依赖风险；每个埋点内部已 try/except 兜底，计数失败
#    绝不影响 prompt 渲染主流程。
from ._metrics import record_render, record_render_error

logger = logging.getLogger(__name__)

_prompt_cache: dict[str, str] | None = None
# ✅ BUG 修复（2026-09-23 · 缓存路径漂移）：记录缓存加载时的 DB 路径。
#    旧实现固定读 DATA_DIR/"scheme_assistant.db"，而真实连接路径以 app.db.DB_PATH
#    为准（测试/多实例会切换该值）—— 路径切换后缓存仍指向旧库，前端编辑
#    提示词"不生效"且无任何告警。现记录并在路径变化时自动重载。
_loaded_db_path: str | None = None
# ✅ 2026-09-24（遗留项 5 闭环）：同时记录缓存加载时库文件的 mtime。
#    光有路径不足以发现「外部进程 / 脚本 / 迁移工具直接写 prompt_templates」——
#    那种情况下路径没变、缓存也没被主动失效，读取方永远拿不到新值。
#    用 mtime 比较即可闭环，且正常路径下只多一次 os.stat。
_loaded_db_mtime: float | None = None
# 使用 RLock（可重入锁）防止 get_prompt 内调用 _load_prompt_cache_sync 时死锁
_cache_lock = threading.RLock()

# {SHARED_*} 占位符：运行时动态解析为对应共享提示词的当前内容（DB 优先）
_SHARED_KEY_RE = re.compile(r"\{(SHARED_[A-Z0-9_]+)\}")

#: 共享片段解析的最大嵌套层数（防止模板互相引用导致无限递归）。
#: ✅ 2026-09-27（BUG-P0-2 · SHARED 递归打爆调用栈）：
#:   旧实现按注释假设「SHARED_* 内容本身不含 SHARED 占位符，无递归风险」——
#:   但内容**由用户在提示词编辑器里自由编辑**，该假设不成立。实测把
#:   ``SHARED_OUTPUT_SPEC`` 的内容改成 ``see {SHARED_OUTPUT_SPEC}`` 后，
#:   ``get_prompt('outline_short_system')`` 直接抛 ``RecursionError``
#:   （实测耗时 0.45s，且异常在 finally 之外逃逸）。互引（A→B→A）同理。
#:   这是**可由用户配置触发**的可用性缺陷：一次误编辑就让所有依赖该共享
#:   片段的生成任务全部失败。改为「深度上限 + 环检测 + 保留原字面量」。
_SHARED_MAX_DEPTH = 4


def _resolve_shared_keys(prompt: str, _depth: int = 0,
                         _chain: tuple[str, ...] = ()) -> str:
    """将模板中的 {SHARED_*} 占位符替换为对应共享提示词的当前内容（DB 优先）。

    ✅ 修复：旧实现 SHARED_* 在 outline.py import 期字符串拼接（值拷贝），
    前端编辑"共享规则"下的 SHARED_* key 后对目录生成永不生效。
    现改为运行时动态解析。

    ✅ 2026-09-27（BUG-P0-2）：新增**递归防护**。命中即保留原字面量
    ``{SHARED_XXX}`` 并打 WARNING（既不崩，也不静默丢内容）：

    1. ``key in _chain`` —— 该 key 已在本条解析链上出现，即成环（A→B→A）；
    2. ``_depth`` 超过 :data:`_SHARED_MAX_DEPTH` —— 兜底深度上限；
    3. 解析结果为空（key 未注册 / 拼写错误）—— 保留字面量并明确告警。

    环检测用**解析链**而非「展开结果里是否还含 SHARED_」：后者会把
    「A 引用了一个拼错的 B」误判成环，从而把 A 已成功展开的部分**整段
    丢弃**；前者精确且不做无谓的内容损失。

    :param _depth: 当前递归深度（内部参数，勿由调用方传）。
    :param _chain: 本条解析链上已展开的 key，用于环检测（内部参数）。
    """
    if "{SHARED_" not in prompt:
        return prompt

    if _depth >= _SHARED_MAX_DEPTH:
        logger.warning(
            "共享提示词嵌套超过 %d 层，停止展开（剩余：%s）。"
            "请检查是否存在互相引用。", _SHARED_MAX_DEPTH,
            sorted(set(_SHARED_KEY_RE.findall(prompt))))
        return prompt

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in _chain:
            logger.warning(
                "共享提示词存在循环引用：%s（链：%s），已保留字面量 { %s }",
                key, " → ".join((*_chain, key)), key)
            return m.group(0)
        try:
            resolved = _resolve_shared_keys(
                get_prompt(key), _depth + 1, (*_chain, key))
        except Exception as e:
            logger.warning("解析共享提示词 %s 失败: %s", key, e)
            return m.group(0)
        if not resolved:
            # key 未注册（拼写错误或已下架）——保留字面量并明确告警
            logger.warning(
                "共享提示词 %s 不存在（未注册），{ %s } 已按字面量保留；"
                "请检查拼写。可用共享片段：%s",
                key, key, sorted(k for k in _ALL_PROMPTS if k.startswith("SHARED_")))
            return m.group(0)
        return resolved

    return _SHARED_KEY_RE.sub(_repl, prompt)



def _current_db_path() -> str | None:
    """取当前真实数据库路径（以 app.db.DB_PATH 为唯一事实源）。"""
    try:
        from app.db import DB_PATH
        return str(DB_PATH)
    except Exception as e:
        logger.debug("读取 app.db.DB_PATH 失败（回退 DATA_DIR 默认路径）: %s", e)
        try:
            from app.config import DATA_DIR
            return str(DATA_DIR / "scheme_assistant.db")
        except Exception:
            return None


def _db_newer_than_cache(db_path: str) -> bool:
    """判断库文件是否在缓存加载之后被写过（用于外部写入的自动重载）。

    失败时一律返回 False（保留现有缓存、不影响业务），并在 debug 级别留痕。
    注意不做「同秒写入检测」：mtime 精度内两次写入无法区分，宁可漏一次重载
    （下次写入必然可见），也不要引入每次 os.stat 之外的额外开销。
    """
    if _loaded_db_mtime is None:
        return False
    try:
        return os.stat(db_path).st_mtime > _loaded_db_mtime
    except Exception as e:
        logger.debug("读取 DB 文件 mtime 失败（沿用现有缓存）: %s", e)
        return False


def _load_prompt_cache_sync():
    """同步方式从数据库加载所有提示词到缓存。"""
    global _prompt_cache, _loaded_db_path, _loaded_db_mtime
    with _cache_lock:
        try:
            import sqlite3

            db_path = _current_db_path()
            if not db_path or db_path == ":memory:":
                _prompt_cache = {}
                _loaded_db_path = db_path
                return
            if not Path(db_path).exists():
                _prompt_cache = {}
                _loaded_db_path = db_path
                return
            _loaded_db_mtime = os.stat(db_path).st_mtime
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT key, content FROM prompt_templates")
                rows = cur.fetchall()
                local_cache: dict[str, str] = {}
                for row in rows:
                    cleaned = clean_prompt_text(row["content"])
                    local_cache[row["key"]] = cleaned
                _prompt_cache = local_cache
                _loaded_db_path = db_path
                logger.info("Prompt cache loaded: %d templates from DB", len(local_cache))
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to load prompt cache from DB: %s", e)
            _prompt_cache = None


def _get_prompt_cache() -> dict[str, str]:
    """获取提示词缓存；未加载或 **DB 路径已切换 / 库文件已变新** 时自动（重）加载。

    ✅ 配套 _loaded_db_path：测试/多实例切换 app.db.DB_PATH 后，缓存不再
    静默指向旧库（旧行为无重载逻辑，前端编辑提示词"不生效"）。
    ✅ 2026-09-24（遗留项 5 闭环）：旧实现只看「缓存是否为空」与「路径是否变化」，
    因此**同一进程内的保存**必须依赖路由显式调 reload_prompt_cache()，而
    **外部进程 / 脚本 / 迁移工具直接写 prompt_templates** 时读取方永远拿不到新值
    （表现为「提示词已保存却不生效」且无任何告警）。现增加库文件 mtime 比较：
    只要库比缓存加载时更新，就重新加载。正常路径下仅多一次 os.stat（微秒级），
    不影响热路径性能。
    """
    global _prompt_cache
    with _cache_lock:
        current = _current_db_path()
        if _prompt_cache is None or (
                current is not None and _loaded_db_path != current):
            _prompt_cache = {}
            try:
                _load_prompt_cache_sync()
            except Exception as e:
                logger.warning("Failed to load prompt cache from DB: %s", e)
                _prompt_cache = None
        elif current is not None and _loaded_db_path == current:
            # 路径未变但库已变新 → 外部写入（多进程 / 脚本 / 迁移），自动重载
            if _db_newer_than_cache(current):
                _prompt_cache = {}
                try:
                    _load_prompt_cache_sync()
                    logger.info("Prompt cache reloaded: DB 文件已更新")
                except Exception as e:
                    logger.warning("Failed to reload prompt cache from DB: %s", e)
                    _prompt_cache = None
        return _prompt_cache or {}


def get_prompt(key: str, **kwargs) -> str:
    """获取提示词，优先读取数据库，回退到硬编码。

    当提供 kwargs 时，自动使用 render_prompt 替换变量。

    BUG-FIX（空值污染）：
      原实现 `if _prompt_cache and key in _prompt_cache` 只判断 key 是否存在。
      一旦用户清空某个提示词后保存（DB 中 content='' / 纯空白），
      该空串会被当作有效内容下发，AI 实际收到空的 system prompt，
      生成质量静默劣化且无任何报错。现在空/纯空白一律视为未配置，
      回退到注册表的出厂默认。
    """
    # ✅ R48（2026-10-06 · 运行时指标）：成功返回记 render_total、异常路径记
    #    render_errors。整段包 try/except —— 计数本身在 _metrics 内 fail-soft，
    #    这里的 except 只兜「渲染本身抛错」这一真实事件，记完原样 re-raise，
    #    绝不吞异常。未知 key 的 ``return ""`` 在 try 内直接退出（不计入渲染，
    #    因为它不是任何已注册模板的渲染）。
    try:
        with _cache_lock:
            cached = _get_prompt_cache().get(key)
        # 关键：仅当缓存内容包含有效字符时才采用 DB 版本
        if cached is not None and cached.strip():
            prompt = cached
        else:
            meta = _ALL_PROMPTS.get(key)
            if not meta:
                # ✅ R39 T1：先惰性补注册「定义在包外」的提示词（投标分析域 19 条），
                #   再判定是否真的未知键。否则提示词管理页会列不出
                #   这批模板，而提取侧也拿不到 DB 覆盖。
                try:
                    from app.services.ai.prompts._registry import register_lazy_prompts
                    register_lazy_prompts()
                except Exception as e:  # noqa: BLE001 - 补注册失败不得影响读取
                    logger.warning("惰性补注册失败: %s", e)
                meta = _ALL_PROMPTS.get(key)
            if not meta:
                logger.warning("Prompt key '%s' not found in registry or cache", key)
                return ""
            if cached is not None and not cached.strip():
                logger.warning(
                    "Prompt '%s' 在数据库中内容为空，已回退到出厂默认（避免下发空提示词）", key
                )
            prompt = meta.get("default_content") or meta.get("content", "")

        # ✅ 先动态解析 {SHARED_*} 占位符（避免其被误报为 missing 变量）
        prompt = _resolve_shared_keys(prompt)

        # BUG-FIX（2026-09-23 · 正文生成深度审计 · P0 日志噪音）：
        #   旧实现把缺失变量校验放在 `if kwargs:` 之外，导致任何"取原始模板"的合法
        #   调用 get_prompt("key")（无 kwargs，例如提示词编辑器预览、/prompts 列表
        #   接口、只读模板的内部用法）都会把**全部**变量当成缺失上报。真实日志里
        #   因此每次正文生成都刷出：
        #       2026-09-23 20:33:57,539 _cache: Prompt 'content_generation_system'
        #           has missing variables: ['scheme_name', 'scheme_type',
        #                                   'section_number', 'standards_text']
        #   而 scheme_name / scheme_type 实际是传了的。"没传参数"根本无从判断
        #   "缺了什么"，所以校验只在 kwargs 非空时有意义 —— 这也让告警语义与
        #   validate_prompt_variables() 自身一致（后者同样只在有 kwargs 时才有意义）。
        if kwargs:
            missing = [
                v for v in validate_prompt_variables(key, **kwargs)
                if not v.startswith("SHARED_")
            ]
            if missing:
                logger.warning(
                    "Prompt '%s' has missing variables (not passed in call): %s",
                    key, missing,
                )
            prompt = render_prompt(prompt, **kwargs)
    except Exception:
        record_render_error(key)
        raise

    record_render(key)
    return prompt


def get_prompt_with_validation(key: str, **kwargs) -> tuple[str, list[str]]:
    """获取提示词并校验变量，返回 (提示词文本, 缺失变量列表)。

    注意：与 get_prompt() 不同，本函数的职责就是"校验变量完整性"，
    因此 kwargs 为空时仍会如实返回全部缺失变量（这是调用方明确索取的
    校验结果，不是日志噪音）。日志噪音只来自 get_prompt() 的告警路径，
    该路径已在 get_prompt() 内用 `if kwargs:` 收紧（2026-09-23 修复）。
    """
    prompt = get_prompt(key, **kwargs)
    missing = [
        v for v in validate_prompt_variables(key, **kwargs)
        if not v.startswith("SHARED_")
    ]

    if not missing and has_residual_placeholders(prompt):
        residual = extract_variables(prompt)
        logger.warning("Prompt '%s' has residual placeholders after rendering: %s", key, residual)

    return prompt, missing


def get_db_overrides() -> dict[str, str]:
    """返回 DB 中全部已定制提示词的快照（key -> 当前生效内容）。

    供管理接口（GET /prompts）合并展示：编辑器必须看到与运行时一致的内容，
    否则服务重启后内存注册表回退出厂默认，会显示"未修改"并在下次保存时
    静默覆盖 DB 中的用户定制。
    """
    with _cache_lock:
        return dict(_get_prompt_cache())


def reload_prompt_cache():
    """强制重新加载提示词缓存（保存后调用）。

    ✅ 2026-09-24（遗留项 5 闭环）：旧实现只把 _prompt_cache 置空，
    未同步重置「已加载路径」与「已加载 mtime」两个派生标记。
    后果：保存后紧接着外部又改了库，_get_prompt_cache 的 mtime 分支会
    与过期标记比对，产生一次不必要的重载或恰好漏掉一次重载 ——
    属于「现在没炸、但语义自相矛盾」的技术债。现三者一并失效，
    使「显式失效」与「按需加载」的状态机口径完全一致。
    """
    global _prompt_cache, _loaded_db_path, _loaded_db_mtime
    with _cache_lock:
        _prompt_cache = None
        _loaded_db_path = None
        _loaded_db_mtime = None
    logger.info("Prompt cache invalidated, will reload on next access")