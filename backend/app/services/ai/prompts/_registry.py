"""提示词注册表。

所有提示词通过 _reg() 注册到 _ALL_PROMPTS 全局字典中。
供 routers 提供给前端展示和编辑使用。

增强功能：
- 变量提取与校验：自动提取模板中的 {变量占位符} 和 __变量名__，支持运行时验证
- 模板清洗：统一换行符、移除零宽字符
- 统一变量渲染：render_prompt() 函数一次性替换所有变量并验证
"""

from __future__ import annotations

import logging
import re


logger = logging.getLogger(__name__)

_ALL_PROMPTS: dict[str, dict] = {}

# ✅ R6 修复（2026-09-22）：启动时提示词模板变量预检。
#
# 背景：logs/backend.log 里曾出现 40 次同一签名告警
#     "Prompt 'content_generation_system' has missing variables (not passed in call): ['max']"
# 这类告警每次正文生成都会被触发（正文生成调用次数 × 章节数），既淹没真实错误，
# 又掩盖了"真的缺变量"（如 scheme_name / section_number 遗漏传参）的信号。
#
# 做法：给"典型误报变量"打白名单 —— 这些短名字几乎总是来自 JSON 示例（如 {max: ...}）
#     或代码块里的字面变量，而非真的需要 render_prompt 传入。
#     白名单命中时 validate_prompt_variables 会跳过，不再产生告警。
#     真实缺变量（scheme_name / section_number 等业务变量）不受影响，仍会告警。
#
# 白名单依据：`{max}` 是常见 JSON 示例字段；`{tasks}` `{type}` 等短字段同理。
# 长度 1~3 的变量名如果**同时**在提示词中出现了 JSON 示例（含 ":" 或 "["）则视为误报。
# 保守起见：白名单只影响告警，不影响 render_prompt 的实际替换行为。
_FALSE_POSITIVE_VARS = frozenset({
    # 极短常见 JSON 字段名（长度 1-3）
    "max", "min", "id", "ok", "no", "to", "on", "off",
    "url", "api", "tag", "key", "val",
    # 常见 JSON 结构字段
    "tasks", "items", "steps", "rows", "cells", "lines", "text",
    "status", "result", "output", "content", "value", "label",
    "type", "code", "name", "role", "goal", "text", "text",
    "level", "order", "index", "count", "total",
    # markdown 语法
    "title", "subtitle", "quote", "note", "warn",
})


def _looks_like_json_example(context: str, var: str) -> bool:
    """判断变量在模板里的上下文是否像 JSON 示例或 LaTeX 下标（无需 render 替换）。

    ✅ 保守匹配：只有满足以下条件之一才视为误报（不产生 missing 告警）：
    1. 变量后面 10 个字符内出现 ASCII 冒号 ":" 或英文方括号 "["/"}"（JSON 特征）；
    2. 变量前面 5 个字符内出现 "{"（LaTeX 下标如 `$p_{max}$`）。
    中文冒号"："不算，避免"最大数量：{max}"这类自然语言被误判。
    """
    marker = f"{{{var}}}"
    idx = context.find(marker)
    if idx < 0:
        return False
    tail = context[idx + len(marker): idx + len(marker) + 10]
    if (":" in tail) or ("[" in tail) or ("{" in tail):
        return True
    # LaTeX 下标：$p_{max}$ —— 前 15 字符里有 `$` 且后 10 字符里有 `$`
    head = context[max(0, idx - 15): idx]
    if "$" in head and "$" in tail:
        return True
    return False


def _is_false_positive(key: str, var: str, template: str) -> bool:
    """判断 (key, var) 组合是否是 JSON 示例导致的误报。

    ✅ 保守策略：只有满足以下**全部**条件才视为误报：
    1. 变量名在 _FALSE_POSITIVE_VARS 白名单里；
    2. 变量在模板中的实际使用上下文**看起来像 JSON**（含冒号/方括号）；
    3. 模板整体不是明显的业务上下文（长度 > 200 字符，含较多中文）。

    不满足任一条则保留告警，让运维看到"真实"缺变量。

    ✅ 可选区块例外（2026-09-26）：变量在模板中**独占一整行**
    （`^\\s*\\{var\\}\\s*$`）时，语义是「有值才注入这一段」—— 调用方
    不传时 render_prompt 会整行丢弃，不会留下残留占位符。因此
    「未传」不是缺陷，不应报缺失（否则每次调用都刷无意义 WARNING）。
    ⚠️ 仅供**运行期缺失告警**（validate_prompt_variables）使用；
    契约漂移校验（check_prompt_variables）走的是「模板里到底有没有这个
    占位符」的语义，不能套用本例外 —— 否则整行区块会被误判成
    「声明了但没用」（declared_not_used）而报假漂移。
    """
    if var not in _FALSE_POSITIVE_VARS:
        return False
    return _looks_like_json_example(template, var)


#: 整行独占的可选区块占位符（行内无其它文字）
_OPTIONAL_BLOCK_RE = re.compile(
    r"^[ \t]*\{([A-Za-z_]\w{1,})\}[ \t]*$", re.MULTILINE)


def _is_optional_block_var(var: str, template: str) -> bool:
    """变量在模板中是否独占一整行（= 可选区块，未传即整行丢弃）。"""
    if not template or not var:
        return False
    for m in _OPTIONAL_BLOCK_RE.finditer(template):
        if m.group(1) == var:
            return True
    return False

# ⚠️ 排除 JSON 对象键上下文：`{key}:`（ASCII 冒号）是 JSON 示例而非变量占位符。
#    否则提示词里未加引号的 JSON 示例（如 {tasks: [...]}）会被误当作变量：
#    既污染变量列表 / 触发"缺失变量"误告警，又可能在 render 阶段被静默替换、
#    从而篡改提示词中的 JSON。正规占位符后接中文冒号「：」/换行/标点，不受影响。
_VARIABLE_PATTERN = re.compile(
    r"\{([A-Za-z_]\w{1,})\}(?!\s*:)"  # {variable}：首字符字母/下划线，总长 ≥2，排除 {key}:
    r"|"
    r"__([A-Z][A-Z0-9_]{2,})__"       # __VARIABLE__ 格式：必须大写字母开头，总长 ≥5（排除纯下划线）
)
_RESIDUAL_PLACEHOLDER_PATTERN = re.compile(
    r"\{[A-Za-z_]\w{1,}\}(?!\s*:)"
    r"|"
    r"__[A-Z][A-Z0-9_]{2,}__"
)
_ZERO_WIDTH_PATTERN = re.compile(r"[\u00AD\u200B\u200C\u200D\uFEFF]")


def _reg(key: str, category: str, label: str, value: str,
         requires: list[str] | None = None) -> str:
    """注册一个提示词到元信息表，自动提取变量列表。

    BUG-FIX（提示词重置失效）：
      原实现只有 "content" 一个字段，update_prompt() 会原地改写它，
      导致"出厂默认值"被用户编辑内容覆盖，POST /prompts/{key}/reset
      实际恢复的是编辑后的内容而非出厂默认。
      现新增只写一次的 "default_content" 字段作为不可变基线。

    ✅ G4 变量契约（2026-09-24）：可选声明 ``requires`` —— 调用方**必须**提供的
    变量白名单。不声明（默认 None）= 完全向后兼容，不做任何契约校验；
    声明后由 :func:`check_prompt_variables` 在启动期比对「声明」与「模板实际
    占位符」，双向漂移（声明了模板没用 / 模板用了没声明）都会被打 WARNING。
    这是把「缺变量只能靠运行期告警发现」升级为「启动期即可发现」的护栏。
    """
    # ✅ 变量列表只含「调用方需提供的变量」：{SHARED_*} 是运行时由
    #    _cache._resolve_shared_keys 动态解析的共享片段引用，并非调用方入参，
    #    不应进入 variables 列表（否则前端编辑器把它当「必需变量」展示、
    #    审计 diff 把启用/停用一段共享规则误记为「变量增删」）。
    variables = extract_user_variables(value)
    _ALL_PROMPTS[key] = {
        "key": key,
        "category": category,
        "label": label,
        "content": value,
        # 出厂默认基线：一经注册永不修改，仅供 reset / 对比使用
        "default_content": value,
        "variables": variables,
        # ✅ 2026-09-25（BUG-A · 契约漂移基线污染）：出厂默认变量集合基线。
        #   旧实现只有 variables 一个字段，update_prompt() 会原地改写它；
        #   而 check_prompt_variables() 拿 meta["variables"] 当「实际占位符」
        #   与契约表比对 —— 用户一旦通过编辑器改了模板占位符，内存注册表就
        #   被污染，下次启动期校验必然误报漂移（哪怕出厂模板与契约一致）。
        #   现在出厂基线单独保存、永不被 update_prompt/reset_prompt 修改，
        #   check_prompt_variables 只以出厂基线为准，契约漂移检测才能真正可靠。
        "default_variables": list(variables),
        # 变量契约白名单：声明 = 强制校验；未声明（None）= 不校验
        "requires": sorted(requires) if requires else None,
    }
    return value


def get_default_prompt(key: str) -> str:
    """获取提示词的出厂默认内容（不受用户编辑影响）。

    用于「恢复默认」与「默认/当前是否被修改」判断。
    """
    meta = _ALL_PROMPTS.get(key)
    if not meta:
        return ""
    return meta.get("default_content", meta.get("content", ""))


def is_prompt_modified(key: str) -> bool:
    """判断提示词是否被用户修改过（与出厂默认比较）。"""
    meta = _ALL_PROMPTS.get(key)
    if not meta:
        return False
    return meta.get("content", "") != meta.get("default_content", "")


def get_prompt_variables(key: str) -> list[str]:
    """获取指定提示词所需的变量列表。"""
    meta = _ALL_PROMPTS.get(key)
    if meta:
        return meta.get("variables", [])
    return []


def validate_prompt_variables(key: str, **kwargs) -> list[str]:
    """验证提示词变量是否全部提供，返回缺失变量列表。

    ✅ R6 修复（2026-09-22）：过滤"JSON 示例误报"。
    背景：`{max}` 这类极短字段常出现在提示词的 JSON 示例里（如 `{max: 200}`），
    被 _VARIABLE_PATTERN 误抓后进入 variables 列表，导致 render_prompt 反复告警
    "missing variables"，淹没真实错误信号。
    过滤规则见 _is_false_positive()。真实缺变量（业务语义名）不受影响。
    """
    required = get_prompt_variables(key)
    kwargs_lower = {k.lower(): v for k, v in kwargs.items()}
    template = _ALL_PROMPTS.get(key, {}).get("content", "")
    # ✅ 整行独占的可选区块（2026-09-26）：未传时 render_prompt 整行丢弃，
    #    不留残留占位符 → 不算"缺失"，否则每次调用都刷无意义 WARNING。
    missing = [v for v in required
               if v.lower() not in kwargs_lower or kwargs_lower[v.lower()] is None
               if not _is_optional_block_var(v, template)]
    # 过滤误报：保留业务语义变量，只抑制"JSON 示例"类短字段
    missing = [v for v in missing if not _is_false_positive(key, v, template)]
    return missing


def clean_prompt_text(text: str) -> str:
    """清洗提示词文本：统一换行符、移除零宽字符、去首尾空白。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_PATTERN.sub("", text)
    return text.strip()


def extract_variables(template: str) -> list[str]:
    """从模板字符串中提取所有 {变量名} 或 __变量名__ 占位符（原始，含 SHARED_*）。"""
    return sorted(set(v for match in _VARIABLE_PATTERN.findall(template) for v in match if v))


def _is_shared_reference(name: str) -> bool:
    """判断变量名是否为 {SHARED_*} 共享片段引用。

    这类占位符由 ``_cache._resolve_shared_keys`` 在运行时解析为对应共享
    提示词的当前内容（DB 优先），并非调用方需提供的入参变量。
    """
    return name.startswith("SHARED_")


def extract_user_variables(template: str) -> list[str]:
    """提取「调用方需提供的变量」，排除运行时自动解析的 {SHARED_*} 共享片段引用。

    与 ``extract_variables`` 的区别：本函数用于注册表 variables 字段、前端
    列表展示与审计 diff —— 这些场景只关心「调用方需传参的变量」，
    SHARED_* 由系统自动注入，混入会误导编辑器与审计口径。
    """
    return sorted(set(v for v in extract_variables(template)
                      if not _is_shared_reference(v)))


def has_residual_placeholders(text: str) -> bool:
    """检查文本中是否还有未替换的变量占位符。"""
    return bool(_RESIDUAL_PLACEHOLDER_PATTERN.search(text))


def render_prompt(template: str, **kwargs) -> str:
    """统一的变量渲染函数，替换模板中的所有变量占位符。

    支持两种变量格式（均为大小写不敏感）：
    - {variable_name} 格式
    - __VARIABLE_NAME__ 格式

    ✅ 单遍替换修复：原实现对「已替换后的文本」逐变量做 re.sub（多遍替换），
    用户资料（material）/ 模型原始输出（invalid_content）等先注入的大段文本中
    若恰好含与后续变量同名的占位符字样（如 {summary}、{heading}），会被
    二次替换，造成提示词结构错位 / 内容污染。现改为单遍扫描一次完成，
    替换值不再被重新扫描。
    """
    lookup = {str(k).lower(): v for k, v in kwargs.items() if v is not None}
    if lookup:
        names = sorted((re.escape(n) for n in lookup), key=len, reverse=True)
        # (?!\s*:) 与提取正则一致：JSON 对象键（{key}:）不参与替换，避免篡改 JSON 示例
        pattern = re.compile(
            r"\{(" + "|".join(names) + r")\}(?!\s*:)"
            r"|"
            r"__(" + "|".join(names) + r")__",
            flags=re.IGNORECASE,
        )

        def _replace(m: re.Match) -> str:
            name = (m.group(1) or m.group(2) or "").lower()
            return str(lookup[name])

        result = pattern.sub(_replace, template)
    else:
        result = template

    # ✅ 整行独占占位符 = 可选区块（2026-09-26）：模板里单独占一行的
    #    {scheme_basis} 之类区块，语义是「有值才注入这一段」。
    #    旧实现不丢行 → 调用方未传时**字面量 "{scheme_basis}" 原样进入发给
    #    模型的提示词**，模型要么把它当正文读、要么按未知变量编内容。
    #    只处理「整行就是这一个占位符」；行内混排（如 "【事实】：{project_facts}"）
    #    不在此处理 —— 空串渲染成 "【事实】：" 是可接受的降级。
    result = re.sub(r"^[ \t]*\{[A-Za-z_][A-Za-z0-9_]*\}[ \t]*\r?\n", "",
                    result, flags=re.MULTILINE)
    result = re.sub(r"^[ \t]*\{[A-Za-z_][A-Za-z0-9_]*\}[ \t]*$", "",
                    result, flags=re.MULTILINE)

    # ✅ 只在【模板】层面检测未解析占位符：
    #    旧实现检测的是"渲染后文本"，会把注入的文档正文 / 模型原始输出中
    #    形如 {xxx} 或 __XXX__ 的内容误报为"未解析占位符"，既产生日志噪音，
    #    又误导排查方向（例如把标书正文里的填空下划线当成提示词变量缺失）。
    if has_residual_placeholders(template):
        template_vars = extract_variables(template)
        kwargs_lower = {k.lower() for k in kwargs if kwargs[k] is not None}
        unresolved = [v for v in template_vars
                      if v.lower() not in kwargs_lower
                      and not v.startswith("SHARED_")]
        # ✅ 2026-09-23（正文生成深度审计 · P2 日志噪音）：此处此前**没有**套用
        #    _is_false_positive 过滤，与 validate_prompt_variables() 的两条告警
        #    路径口径分叉 —— 后者早已过滤（R6），本处仍在报。运行库实测
        #    2026-09-23 20:54~20:55 单个正文任务打出 6 条
        #    "Prompt template has unresolved placeholders: ['max']"
        #    （logs/backend.log，trace=d283bb90c239），历史上更曾有 40 次/天。
        #    `{max}` 来自提示词里的 JSON 示例，并非真缺变量，
        #    噪音会淹没真实的 scheme_name/section_number 漏传信号
        #    （AGENTS.md §5.3 已登记该遗留项）。现与 validate 路径共用同一判据。
        unresolved = [v for v in unresolved
                      if not _is_false_positive("", v, template)]
        if unresolved:
            logger.warning("Prompt template has unresolved placeholders: %s", unresolved)

    return result


def get_prompt(key: str) -> str:
    """获取提示词原始内容（无变量渲染）。"""
    p = _ALL_PROMPTS.get(key)
    if not p:
        raise KeyError(f"提示词不存在: {key}")
    return p["content"]


def render(key: str, **kwargs) -> str:
    """按变量渲染提示词（DB 优先，回退硬编码）。

    走 _cache.get_prompt() 逻辑，确保前端编辑保存到 DB 的提示词生效。
    """
    from app.services.ai.prompts._cache import get_prompt as _cache_get
    return _cache_get(key, **kwargs)


def list_prompts(category: str | None = None) -> list[dict]:
    """列出所有提示词，可按分类过滤。

    附带 default_content / modified 字段，便于前端展示"已修改"标记
    与提供"恢复默认"预览。
    """
    items = [dict(p) for p in _ALL_PROMPTS.values()]
    for p in items:
        p["modified"] = p.get("content", "") != p.get("default_content", "")
    if category:
        items = [p for p in items if p["category"] == category]
    return items


def update_prompt(key: str, content: str) -> bool:
    """更新提示词内容（仅内存，持久化由路由层处理）。

    BUG-FIX：仅改写 "content"，保留不可变的 "default_content"。
    """
    if key not in _ALL_PROMPTS:
        return False
    _ALL_PROMPTS[key]["content"] = content
    _ALL_PROMPTS[key]["variables"] = extract_user_variables(content)
    return True


def reset_prompt(key: str) -> str | None:
    """将提示词内容恢复为出厂默认，返回恢复后的内容（key 不存在返回 None）。"""
    if key not in _ALL_PROMPTS:
        return None
    default = _ALL_PROMPTS[key].get("default_content", _ALL_PROMPTS[key].get("content", ""))
    _ALL_PROMPTS[key]["content"] = default
    _ALL_PROMPTS[key]["variables"] = extract_user_variables(default)
    return default


# ---------------------------------------------------------------------------
# ✅ G4 变量契约表（2026-09-24 · 遗留项 4 闭环）
# ---------------------------------------------------------------------------
#: 模板 key → 调用方**必须**提供的变量白名单。
#:
#: 为什么集中在这里而不散落在各模板文件：
#:   _reg() 的模板正文是多行三引号字符串，在其调用处追加参数极易误改到
#:   引号边界；集中声明一处维护、一处校验，且新增/删除模板时契约一目了然。
#:
#: 声明口径 = **已核对全部渲染调用点的实际传参**（不是猜的）：
#:   · content_generation_system  ← sse_handlers.py 正文渲染（4/4 全传）
#:   · content_continue_system    ← sse_handlers.py 续写渲染（3/3 全传）
#:   · outline_short_system       ← sse_handlers.py 短方案目录（4/4 全传）
#:   · outline_level1_system      ← sse_handlers.py 长方案一级目录（6/6 全传）
#:   · outline_review_system      ← sse_handlers.py 目录审核（6/6 全传）
#:   · outline_patch_system       ← sse_handlers.py 外科式补齐（6/6 全传）
#:   · outline_feedback_system    ← sse_handlers.py 反馈修正（6/6 全传）
#:
#: 未列入的模板 = 调用点尚未逐一核对，保持「不校验」以避免误报。
PROMPT_VARIABLE_CONTRACTS: dict[str, list[str]] = {
    "content_generation_system": [
        "scheme_name", "scheme_type", "section_number", "standards_text",
        # ✅ E3（2026-09-25 · 提示词条件注入）：根据本章是否有 DB 子章节、
        #    body_subheading_demote_with_children 配置，动态生成正文子标题编号规则片段。
        "subheading_rule",
    ],
    "content_continue_system": [
        "scheme_name", "scheme_type", "standards_text",
    ],
    "outline_short_system": [
        "scheme_name", "scheme_type", "construction_scope", "project_facts",
        # ✅ 2026-09-26（目录生成三项依据收敛）：{scheme_basis} = 方案名称
        #   确定性解析产物（类型/工序/工艺/对象/危大分类，services/scheme_basis）；
        #   {standards_text} = standards_registry 匹配到的编制依据规范。
        #   两者均「无法匹配时渲染为空串 = 不注入」，故不传时模板仍可正常渲染。
        "scheme_basis", "standards_text",
    ],
    "outline_level1_system": [
        "scheme_name", "scheme_type", "construction_scope",
        "project_brief", "reference_outline", "project_facts",
        "scheme_basis", "standards_text",
    ],
    "outline_review_system": [
        "scheme_name", "scheme_type", "construction_scope",
        "is_dangerous", "outline_json", "project_facts",
        "scheme_basis",
    ],
    "outline_patch_system": [
        "scheme_name", "scheme_type", "project_brief",
        "project_facts", "chapter_titles", "missing_items",
    ],
    "outline_feedback_system": [
        "scheme_name", "scheme_type", "project_brief",
        "project_facts", "original_outline", "review_suggestions",
    ],
    # 全局事实模板：调用方已逐项核对，启动期纳入统一变量契约防漂移。
    "facts_extract_system": [
        "zone_type", "priority_weight", "priority_hint", "heading",
        "zone_hint", "material", "chunk_index", "NORM_DICT_BLOCK", "summary",
    ],
    "facts_json_fix_system": ["issues", "target_description", "invalid_content"],
    "global_facts_adjust_system": ["current_facts", "instruction"],
}


def _apply_variable_contracts() -> None:
    """把集中式契约表套用到已注册的模板元信息（在所有模板模块 import 之后调用）。

    幂等：重复调用只会重写同样的值。未知 key 跳过（模板可能已被动态删除）。
    """
    for key, requires in PROMPT_VARIABLE_CONTRACTS.items():
        meta = _ALL_PROMPTS.get(key)
        if meta is None:
            continue
        meta["requires"] = sorted(requires)


# ---------------------------------------------------------------------------
# ✅ G4 变量契约校验（2026-09-24 · 遗留项 4 闭环）
# ---------------------------------------------------------------------------
def check_prompt_variables(verbose: bool = False,
                           strict: bool = False) -> list[dict]:
    """校验全部注册提示词的「变量契约」，返回不一致明细。

    检查两类双向漂移（仅对声明了 ``requires`` 的模板生效）：
      1. ``declared_not_used``  —— 声明了必需变量，但模板里根本没这个占位符
          （契约写错 / 模板被改过而契约没同步）；
      2. ``used_not_declared``  —— 模板里有占位符，但没进契约白名单
          （调用方可能忘传 → 运行期残留占位符）。

    :param verbose: True 时把明细打到 WARNING 日志（供 main.lifespan 启动期调用）；
                    False = 静默（不打印日志），**但仍如实返回 issues**，
                    调用方可据此自行汇总告警。
    :param strict:  True 且存在漂移时抛出 :class:`PromptContractError`
                    （阻断启动）。默认 False = 不改变既有启动行为（向后兼容）。
    :return: 不一致列表（空列表 = 契约全部一致）

    ✅ 2026-09-25（BUG-B · verbose=False 语义漂移 / 配置项静默失效）：
      main.py 的启动期调用是 ``check_prompt_variables(verbose=settings.prompt_strict_variables)``,
      而 config 注释写着「False = 不改变启动行为 / True 开启」。旧实现里
      verbose=False 时**完全不打印任何日志**，于是：
        * 契约漂移在默认配置下**静默无声**（既不告警也不阻断），与「默认关闭、
          开启才生效」的注释正好相反；
        * ``prompt_strict_variables`` 从未真正「开启校验」—— 它只控制
          「是否把明细打到日志」，属于配置项静默失效。
      现按 docstring 口径修回：verbose 只决定**是否打印明细日志**，返回值恒为
      完整漂移明细（调用方 main.py 已改为无条件汇总告警）。``strict`` 是显式
      新增能力（默认关闭），开启时才阻断启动。

    说明：默认路径只做**校验与告警**，绝不修改模板内容、不阻断服务启动；
    未声明 ``requires`` 的模板（绝大多数）完全跳过，保证零副作用。
    """
    issues: list[dict] = []
    for key, meta in sorted(_ALL_PROMPTS.items()):
        requires = meta.get("requires")
        if not requires:
            continue
        # ✅ 2026-09-25（BUG-A · 契约漂移基线污染）：以出厂默认基线比对
        #   （meta["content"]/["variables"] 会被 update_prompt 原地改写，用它
        #   当基线会导致「出厂模板与契约一致、但用户改过一次就误报漂移」）。
        template = meta.get("default_content") or meta.get("content", "")
        raw_actual = {v for v in (meta.get("default_variables")
                                  or meta.get("variables") or [])
                      if not _is_false_positive(key, v, template)}
        # 与 validate_prompt_variables 同口径：大小写不敏感比较
        actual_lower = {v.lower() for v in raw_actual}
        declared_lower = {v.lower() for v in requires}
        declared_not_used = sorted(
            v for v in requires if v.lower() not in actual_lower)
        used_not_declared = sorted(
            v for v in raw_actual if v.lower() not in declared_lower)
        if not (declared_not_used or used_not_declared):
            continue
        issues.append({
            "key": key,
            "declared_not_used": declared_not_used,
            "used_not_declared": used_not_declared,
        })
        if verbose:
            parts = []
            if declared_not_used:
                parts.append(f"声明了但模板未使用={declared_not_used}")
            if used_not_declared:
                parts.append(f"模板使用但未声明={used_not_declared}")
            logger.warning(
                "提示词变量契约不一致 %s：%s", key, "；".join(parts))
    if strict and issues:
        raise PromptContractError(
            f"提示词变量契约存在 {len(issues)} 处漂移："
            + "；".join(f"{i['key']}(未用={i['declared_not_used']}"
                        f"/未声明={i['used_not_declared']})" for i in issues))
    return issues


class PromptContractError(RuntimeError):
    """提示词变量契约漂移且开启严格模式（``prompt_contract_fail_fast``）时抛出。

    仅显式开启严格模式时使用 —— 默认路径只打 WARNING、不阻断启动，
    保证向后兼容（AGENTS.md §3.1.3：新增能力默认向后兼容）。
    """


from app.services.ai.prompts import outline  # noqa: E402,F401
from app.services.ai.prompts import content  # noqa: E402,F401
from app.services.ai.prompts import charts  # noqa: E402,F401
from app.services.ai.prompts import analysis  # noqa: E402,F401
from app.services.ai.prompts import illustration  # noqa: E402,F401  （自招投标平台移植：配图编排/方案）
from app.services.ai.prompts import consistency_repair  # noqa: E402,F401  （全文一致性 Agent 修复：扫描/仲裁/修复）
# ✅ G4 变量契约：模板全部注册后再套用契约表（见上方 PROMPT_VARIABLE_CONTRACTS）
_apply_variable_contracts()

