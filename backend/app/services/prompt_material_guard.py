"""外部资料治理统一入口（R175 · 2026-11-03）。

把「凭据脱敏 → 注入围栏 → 上下文预算」收敛为**单一公共函数**，供所有向
外部 AI 发送用户资料原文的链路复用，消除历史上「每个链路各写一份开关判断 +
各写一份围栏逻辑」的分叉（正文生成一处接线、其余五处裸奔的问题）。

三个开关全部**默认关闭**，因此本模块接入任何链路时，未开启配置的行为与
修复前**逐字节一致**：

* ``prompt_redact_sensitive``（默认 False）：对外部资料原文做凭据脱敏；
* ``prompt_injection_defense``（默认 False）：给外部资料加「只读数据」围栏；
* ``prompt_context_budget``（默认 0）：按优先级削减超长上下文。

⚠️ 行为变更说明（详见 docs/提示词模块审计_R175_20261103.md）：
本模块**不改变任何默认值**。用户显式开启 ``prompt_redact_sensitive`` 后，
送往 AI 的资料原文中的疑似凭据会被替换为 ``[已脱敏凭据]``（仅出站，不回写库），
把该开关置回 False 即完全回退。

设计约束：
* 纯函数，不触 DB、不发网络请求（配置读取走 ``app.config.settings``）；
* 任何异常都必须 fail-soft（治理失败绝不能阻断生成）；
* 围栏幂等：已含围栏标记的文本不会被二次包裹。
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger("prompt_material_guard")

#: 启动期提示只打一次（避免每个请求刷屏）
_POSTURE_LOGGED = False


# --------------------------------------------------------------------------
# 开关读取（唯一事实源，禁止各链路自行 getattr 判断）
# --------------------------------------------------------------------------


def redaction_enabled() -> bool:
    """凭据脱敏开关。

    生效条件 = ``prompt_redact_sensitive`` **或** ``prompt_injection_defense``。

    ✅ 为什么「或」：修复前（历史行为）注入防护开启时会顺带做脱敏，已有用户
    若只开了 ``prompt_injection_defense``，改成纯独立开关会让他们**静默失去**
    既有脱敏（属回归）。因此保留旧的耦合语义，同时允许新开关单独开启脱敏。
    """
    return bool(
        getattr(settings, "prompt_redact_sensitive", False) or getattr(settings, "prompt_injection_defense", False)
    )


def injection_defense_enabled() -> bool:
    """注入防护开关（``prompt_injection_defense``，默认 False）。"""
    return bool(getattr(settings, "prompt_injection_defense", False))


def context_budget() -> int:
    """上下文预算（``prompt_context_budget``，≤0 表示关闭）。"""
    return int(getattr(settings, "prompt_context_budget", 0) or 0)


def governance_posture() -> dict:
    """当前治理姿态（供诊断接口 / 启动日志使用）。"""
    return {
        "redaction_enabled": redaction_enabled(),
        "injection_defense_enabled": injection_defense_enabled(),
        "context_budget": context_budget(),
        # 未开启脱敏时给出显式风险提示，避免「以为已防护」的静默假安全感
        "redaction_notice": (
            ""
            if redaction_enabled()
            else "敏感信息脱敏未开启（PROMPT_REDACT_SENSITIVE=False）：上传资料原文"
            "中的疑似密钥/凭据将原样发送给外部 AI 服务商。生产环境建议开启。"
        ),
    }


def log_governance_posture_once() -> None:
    """启动期一次性打印治理姿态；脱敏未开启时以 WARNING 明确提示风险。

    由 ``app.main`` 的 lifespan 调用一次（测试可直接调用，幂等）。
    """
    global _POSTURE_LOGGED
    if _POSTURE_LOGGED:
        return
    _POSTURE_LOGGED = True
    posture = governance_posture()
    if not posture["redaction_enabled"]:
        logger.warning(
            "提示词敏感信息脱敏当前**未开启**（PROMPT_REDACT_SENSITIVE=False）："
            "用户上传资料原文会原样发往外部 AI。如需防护请在 .env 或 "
            "PUT /system/governance 中开启（重启后仍生效）。"
        )
    if posture["injection_defense_enabled"]:
        logger.info("提示词注入防护已开启（外部资料将加只读围栏并扫描注入手法）")
    if posture["context_budget"] > 0:
        logger.info("提示词上下文预算已开启：%d 字符", posture["context_budget"])


# --------------------------------------------------------------------------
# 核心治理函数
# --------------------------------------------------------------------------


def redact_text(text: str) -> tuple[str, int]:
    """对外部资料原文做凭据脱敏，返回 (脱敏后文本, 命中数)。

    开关关闭时**原样返回**（命中数 0），保证既有行为逐字节不变。
    只替换高置信度凭据形态（sk-/gsk_/api_key=…/URL 内嵌账号口令），
    刻意避开 GB/JGJ 标准编号、工程编号、人员姓名等正常文本。
    """
    if not text or not redaction_enabled():
        return text, 0
    try:
        from app.services.prompt_governance import redact_sensitive

        return redact_sensitive(text)
    except Exception:  # noqa: BLE001 - 脱敏失败绝不阻断生成
        logger.warning("凭据脱敏失败（使用原文继续）", exc_info=True)
        return text, 0


def redact_only(text: str) -> str:
    """只脱敏、不加围栏、不做预算（供出站消息统一收口使用）。"""
    out, n = redact_text(text)
    if n:
        logger.warning("出站消息中检出 %d 处疑似凭据，已脱敏", n)
    return out


def redact_outbound_messages(messages: list) -> list:
    """出站前对**所有角色**的消息统一脱敏（覆盖全部 AI 链路的兜底收口）。

    ✅ 为什么放在出站侧：一致性审计 / 合规预检 / 导出等链路目前由其他会话
    占用或直接组装 f-string 提示词，逐点接线会漏；出站单点收口可保证
    「任何走向外部 AI 的用户资料原文」都被同一判据覆盖，且**不修改**库内
    原文（用户上传的文档内容保持完整）。

    ✅ R204（2026-11-09 · system 消息此前完全绕过脱敏）：旧实现只处理
    ``role == "user"``，其余角色原样放行。但本仓**大量外部资料被放进
    system 消息**：``compliance_check_system``（章节正文/全局事实/清单）、
    ``expert_review_system``（附件/全局事实）、``consistency_audit_system``、
    ``facts_extract_system``（上传的招标文件全文）、``outline_short_system``
    （项目资料摘要/关键事实）、图表修复提示词的 ``{code}``（上一轮 AI 输出）
    …… 于是开启 ``PROMPT_REDACT_SENSITIVE=True`` 时，这些链路的凭据
    **原样外发** —— 启动日志却宣告「已开启脱敏」，属假绿。
    现改为对所有角色脱敏（``content`` 非字符串时跳过；不改结构、不改字节序）。

    开关关闭时返回**原列表对象**（调用方无需感知，零开销零行为变化）。
    """
    if not redaction_enabled() or not isinstance(messages, list):
        return messages
    out: list = []
    changed = False
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            out.append(msg)
            continue
        redacted, n = redact_text(content)
        total += n
        if n:
            changed = True
            out.append({**msg, "content": redacted})
        else:
            out.append(msg)
    if total:
        logger.warning("出站消息共脱敏 %d 处疑似凭据", total)
    return out if changed else messages


def scan_outbound_messages(messages: list) -> list:
    """出站消息**注入手法检测**（只读 · 默认常开 · 绝不改动任何字节）。

    ✅ 为什么与 :func:`redact_outbound_messages` 分开（G1 · 2026-11-09）：
    注入防护此前整体挂在 ``prompt_injection_defense``（**默认 False**）之下，
    于是默认部署下既没有围栏、也**没有任何检测** —— 运维完全看不到
    「用户上传的招标文件里带了越狱指令」。两种行为的代价不同：

    * **围栏/脱敏**会**改写**送模型的字节 → 属于行为变更，必须由用户显式
      开启（沿用既有开关，默认值逐字不变）；
    * **检测**只读不写（只产出 WARNING + 指标）→ 没有任何行为风险，故
      **默认常开**，让风险面在默认部署下也可见。

    命中不阻断生成（误报成本高：招标文件/技术规格书里出现「忽略以上要求」
    并不罕见），只告警并计数，由运维按需处置。

    :param messages: 出站消息列表。
    :return: **原列表对象**（逐字节不变；本函数不返回改写结果）。
    """
    if not isinstance(messages, list) or not messages:
        return messages
    try:
        from app.services.prompt_governance import scan_prompt_injection
    except Exception:  # noqa: BLE001 - 检测不可用时绝不阻断调用
        logger.debug("注入手法检测不可用（跳过）", exc_info=True)
        return messages
    total_hits = 0
    samples: list[str] = []
    try:
        from app.services.ai.prompts import _metrics as _pm

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            try:
                _pm.record_injection_scan(role)
            except Exception:  # noqa: BLE001 - 指标失败不得影响检测
                pass
            hits = scan_prompt_injection(content)
            if not hits:
                continue
            total_hits += len(hits)
            for h in hits:
                try:
                    _pm.record_injection_hit(str(h.get("pattern") or "unknown"))
                except Exception:  # noqa: BLE001 - 同上
                    pass
            for h in hits[:3]:
                sample = str(h.get("text") or "")[:60]
                if sample and sample not in samples:
                    samples.append(sample)
    except Exception:  # noqa: BLE001 - 检测整体失败绝不阻断调用
        logger.debug("注入手法检测失败（跳过）", exc_info=True)
        return messages
    if total_hits:
        logger.warning(
            "出站消息检出疑似提示词注入手法 %d 处（仅告警，未改写内容、未阻断生成）：%s。"
            "如需自动隔离，请在治理配置中开启 prompt_injection_defense（将外部资料包入只读围栏）。",
            total_hits,
            " / ".join(samples[:5]),
        )
    return messages


def guard_text(text: str, labels=None) -> str:
    """按标签给外部资料段加「只读数据」边界围栏（开关关闭时原样返回）。

    幂等：文本中已含围栏标记时不再二次包裹（防止链路接线 + 出站兜底重复加栏）。
    """
    if not text or not injection_defense_enabled():
        return text
    try:
        from app.services.prompt_governance import (
            MATERIAL_OPEN,
            guard_external_segments,
        )

        if MATERIAL_OPEN in text:
            return text
        if labels is None:
            return guard_external_segments(text)
        return guard_external_segments(text, labels=labels)
    except Exception:  # noqa: BLE001
        logger.warning("注入防护围栏失败（使用原文继续）", exc_info=True)
        return text


def apply_budget(text: str) -> str:
    """按 ``prompt_context_budget`` 削减超长上下文（≤0 时原样返回）。"""
    if not text:
        return text
    budget = context_budget()
    if budget <= 0:
        return text
    try:
        from app.services.prompt_governance import apply_context_budget

        return apply_context_budget(text, budget)
    except Exception:  # noqa: BLE001
        logger.warning("上下文预算削减失败（使用原文继续）", exc_info=True)
        return text


def govern_text(text: str, *, labels=None, fence: bool = True, budget: bool = True) -> str:
    """统一治理入口：围栏 → 脱敏 → 预算（顺序固定，任何一步失败均降级）。

    顺序理由：
    1. 先围栏：把资料显式标注为「只读数据」，降低被当作指令执行的概率；
    2. 再脱敏：只替换凭据形态片段，不影响围栏结构；
    3. 最后预算：截断会切掉尾部（含 ``MATERIAL_CLOSE``），因此放在最后；
       截断后若围栏闭合标记丢失，由 ``_repair_closing_fence`` 补回。

    :param labels: 外部资料段标签；None = 使用 ``MATERIAL_LABELS`` 默认集
    :param fence: 是否加围栏（某些链路已自带围栏语义时可关）
    :param budget: 是否做预算削减
    """
    if not text:
        return text
    out = text
    if fence:
        out = guard_text(out, labels=labels)
    before = out
    out, n = redact_text(out)
    if n:
        logger.warning("外部资料中检出 %d 处疑似凭据，已脱敏", n)
    if budget:
        out = apply_budget(out)
        out = _repair_closing_fence(before, out)
    return out


def govern_blob(text: str, label: str = "外部资料原文", *, fence: bool = True, budget: bool = True) -> str:
    """单块外部资料（无「【标签】」分段结构）的统一治理入口。

    与 :func:`govern_text` 的区别：这里对**整块**资料加围栏
    （``guard_material`` 语义），用于事实提取素材 / 招标文件分段 /
    上传文档正文等「整段都是不可信外部输入」的场景。

    顺序：围栏 → 脱敏 → 预算（与 govern_text 一致）。
    """
    if not text:
        return text
    out = text
    if fence and injection_defense_enabled():
        try:
            from app.services.prompt_governance import MATERIAL_OPEN, guard_material

            if MATERIAL_OPEN not in out:
                out = guard_material(label, out)
        except Exception:  # noqa: BLE001
            logger.warning("单块资料围栏失败（使用原文继续）", exc_info=True)
    out, n = redact_text(out)
    if n:
        logger.warning("外部资料（%s）中检出 %d 处疑似凭据，已脱敏", label, n)
    if budget:
        before = out
        out = apply_budget(out)
        out = _repair_closing_fence(before, out)
    return out


def clip_text(text: str, limit: int, label: str = "") -> str:
    """按字符上限截断文本，并在**真正发生截断**时写日志 + 计入指标（可见化）。

    ✅ R179（2026-11-05 · 截断静默丢信息）：目录链路多处对 ``project_brief`` /
    ``project_facts`` 做硬切片（1500 / ``PROJECT_FACTS_LIMIT_OUTLINE``），旧实现
    是**静默丢字** —— 日志与指标都无痕迹，运维无法区分「没截断」与「截断丢了
    高优信息」，也无法在方案变长后判断该不该调大上限。本函数只做「截断 + 留痕」：
    未超限时原样返回（逐字节一致），``limit <= 0`` 表示不截断；
    留痕失败一律 fail-soft，绝不阻断生成。
    """
    if not text or limit is None or limit <= 0 or len(text) <= limit:
        return text
    try:
        logger.warning(
            "上下文截断（%s）：%d → %d 字符，超出部分已丢弃",
            label or "未命名片段",
            len(text),
            limit,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断生成
        # ✅ R204（2026-11-09 · 留痕失败不再静默）：三个裁剪原语此前都用
        #   ``except Exception: pass`` 吞掉落痕失败 —— 于是「零截断」与
        #   「没发生截断」在指标上不可区分（与 R186 第 5 条口径不一致）。
        _record_ledger_failure("truncation_log")
    try:
        from app.services.ai.prompts._metrics import record_token_budget_truncated

        record_token_budget_truncated(label or "unknown")
    except Exception:  # noqa: BLE001 - 指标失败不得阻断生成
        _record_ledger_failure("truncation_counter")
    return text[:limit]


# --------------------------------------------------------------------------
# 进模型请求的「留痕裁剪」（R180 · 2026-11-07）
# --------------------------------------------------------------------------
#: 优先在这些边界处收刀（从强到弱）：段落 → 行 → 句 → 分句 → 词。
_CLIP_BOUNDARIES = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " ")
#: 边界离上限太远（不足 60%）时退化为硬切，避免为了「不切断」丢掉过多有效内容。
_CLIP_BOUNDARY_MIN_RATIO = 0.6


def _clip_cut_point(text: str, limit: int) -> int:
    """返回 ``<= limit`` 的最优切点：尽量落在段落/句子/词边界之后。

    ⚠️ 必须**按 _CLIP_BOUNDARIES 的优先级**逐个尝试，不能取全局最靠后的分隔符：
    数据字典条目里既有换行又有空格，``max`` 会让条目内的空格（如「置信度 」
    之后，295 字处）盖过条目换行（252 字处），结果把一条 ``- 关键指标…``
    切成半截 —— 正是「不切断」要禁止的情形。
    """
    head = text[:limit]
    min_cut = int(limit * _CLIP_BOUNDARY_MIN_RATIO)
    for sep in _CLIP_BOUNDARIES:
        idx = head.rfind(sep)
        if idx >= 0:
            cand = idx + len(sep)
            if cand >= min_cut:
                return cand
    return limit


def clip_text_logged(text: str, limit: int, label: str = "") -> str:
    """内部判定用裁剪：只记日志/指标，**绝不向文本插标记**。

    用于关键词匹配、启发式兜底等「改文本就会改判定结果」的场景 —— 往这类
    字符串里加截断说明会改变匹配/正则结果，等于用可观测性换另一种静默错误。
    返回值与旧 ``text[:limit]`` 逐字节一致，仅新增日志账目。
    """
    if not text or limit is None or limit <= 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    name = label or "文本"
    # ✅ 2026-11-07（第 5 条）：此前本行外面还套了一层 `except Exception: pass`，
    #    是死代码 —— logging 的 ``Handler.handle`` 会自己吞掉处理器/格式化异常
    #    （只走 ``handleError``，不向上冒泡），所以这里永远不会抛。真被吞掉时
    #    丢的是本函数**唯一**的账目（这条「仅日志」截断记录），而裁剪照旧发生，
    #    结果就是「截断在静默发生」—— 正是本函数要防的那种假绿。故直接去掉兜底。
    logger.warning(
        "上下文截断（仅日志，%s）：%d → %d 字，已省略 %d 字（文本保持原样，未插标记）",
        name,
        len(text),
        limit,
        omitted,
    )
    # ✅ 2026-11-07（第 5 条）：指标留痕**保留** fail-soft —— 本模块约定
    #    「治理失败绝不能阻断生成」，且 ``_metrics`` 可能在导入期处于半初始化
    #    （本模块被更早的链路抢先导入），此时抛错会改变裁剪结果本身。
    #    但留痕失败必须可观测：落到 ``_metrics.clip_ledger_failures`` 降级计数器
    #    （吞掉就会丢「截断计数到底记上没有」这一事实 —— 快照里的「零截断」与
    #    「没发生截断」将无法区分）。纯 pass 不算留痕，故这里不再 pass。
    try:
        from app.services.ai.prompts import _metrics as _pm

        _pm.record_token_budget_truncated(name)
    except Exception:  # noqa: BLE001 - 指标埋点失败不得阻断裁剪；失败已计入 clip_ledger_failures
        _record_ledger_failure("truncation_counter")
    return text[:limit]


def _record_ledger_failure(channel: str) -> None:
    """把「留痕机制自身故障」落到降级计数器（尽力而为，二次失败也不得冒泡）。

    单独抽函数是为了让 except 分支体是可复核的单一动作；内层再失败时只写
    debug 日志 —— 观测面自己都不可用时只能放弃，绝不能因此改变裁剪结果。
    """
    try:
        from app.services.ai.prompts import _metrics as _pm

        _pm.record_clip_ledger_failure(channel)
    except Exception:  # noqa: BLE001 - 二次兜底：降级计数也不可用时只能放弃，绝不阻断裁剪
        logger.debug("留痕机制故障降级计数失败（忽略）", exc_info=True)


#: 调整指令（用户下达的本次任务本体）的物理上限（字），**与预算开关无关**。
#: 2026-11-07：R186 删除 ``instruction[:3000]`` 时漏掉了「开关默认关闭时
#: ``prompt_context_budget=0``，预算器直通」这一事实 —— 用户粘贴整篇文档会
#: 直接超模型窗口（502）。6000 的推导：编辑对象 JSON 上限 30000 字 +
#: system 模板约 3000 字，按仓库既有口径 1.5 字/token 保守折算约 22k tokens；
#: 32k token 窗口扣掉输出预留 4k token 后指令最多约 9k 字，再留 3k 字余量
#: （截断说明本身、tokenizer 偏差、模板增长）→ 6000。
INSTRUCTION_PROMPT_MAX_CHARS = 6000


#: 调整指令占「用户上下文安全上限」的份额（G6 · 2026-11-09 · 窗口联动）。
#: 指令是任务本体，不能被压到没有表达空间；但它也只是上下文的一部分，
#: 不应挤占资料段的额度 —— 取 1/4 是「够说清意图，又不喧宾夺主」的折中。
INSTRUCTION_SHARE_OF_CONTEXT = 0.25

#: 调整指令下限（字）。低于此值，用户几乎无法表达「要改什么」，
#: 指令段落会退化成噪声（模型据此胡乱生成）。
INSTRUCTION_MIN_CHARS = 600


def effective_instruction_cap() -> int:
    """解析**本次生效**的调整指令物理上限（字）。

    ✅ G6（2026-11-09 · 6000 字上限与模型窗口脱钩）：
    :data:`INSTRUCTION_PROMPT_MAX_CHARS` = 6000 的推导锚定「32k token 窗口」，
    但该锚点是**写死的常量** —— 用户把模型换成 8k/16k 小窗口后，6000 字
    （≈4k token）指令 + system 模板 + 资料段仍可能直接撑爆窗口（502）。
    本函数把上限与「用户自述的上下文安全上限」联动：

      1. ``settings.prompt_instruction_max_chars`` > 0 → 显式指定（夹到硬天花板）；
      2. ``settings.context_length_limit`` > 0 → 按份额推导
         （指令 ≤ 上下文的 1/4，夹到 [600, 6000]）；
      3. 两者都未配置 → :data:`INSTRUCTION_PROMPT_MAX_CHARS`（6000，**既有行为
         逐字节不变**）。

    ⚠️ 口径诚实说明：本仓 ``ai_config`` 表**不持久化**每个模型的上下文窗口
    （``context_tokens`` 只在 ``GET /models`` 拉取瞬间存在，见
    ``routers/ai_config/models.py``），故此处只能用**全局上下文安全上限**
    作窗口代理 —— 它是用户自己配置的「正文上下文能有多大」，
    与真实模型窗口同量级，但不是同一个量。

    :return: 生效上限（字）；调用方据此裁剪指令段。
    """
    try:
        explicit = int(getattr(settings, "prompt_instruction_max_chars", 0) or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return max(INSTRUCTION_MIN_CHARS, min(explicit, INSTRUCTION_PROMPT_MAX_CHARS))
    try:
        ctx_limit = int(getattr(settings, "context_length_limit", 0) or 0)
    except (TypeError, ValueError):
        ctx_limit = 0
    if ctx_limit <= 0:
        return INSTRUCTION_PROMPT_MAX_CHARS
    derived = int(ctx_limit * INSTRUCTION_SHARE_OF_CONTEXT)
    return max(INSTRUCTION_MIN_CHARS, min(derived, INSTRUCTION_PROMPT_MAX_CHARS))


def clip_text_notice_meta(text: str, limit: int) -> dict | None:
    """只算「倘若截断会怎么记账」，**不截断、不写日志、不计指标**。

    账面字段与 :func:`clip_text_notice_report` 完全一致（同一切点算法）：
    ``from`` / ``to`` / ``omitted`` / ``limit``；未超限返回 ``None``。

    用途：本次请求**不渲染提示词**、但仍需向用户回传「这条文本本身超限」的
    路径（如 ``global_facts.adjust_facts(do_apply=True)`` 复用已确认操作计划）。
    这类路径若改调 ``clip_text_notice_report`` 取账面，会为同一份文本再写一次
    截断日志、再记一次截断指标 —— 账目算重。纯账面函数把两件事拆开：
    记账口径一致，副作用只发生一次。
    """
    if not text or limit is None or limit <= 0 or len(text) <= limit:
        return None
    cut = _clip_cut_point(text, limit)
    return {"from": len(text), "to": cut, "omitted": len(text) - cut, "limit": int(limit)}


def clip_text_notice_report(
    text: str, limit: int, label: str = ""
) -> tuple[str, dict | None]:
    """:func:`clip_text_notice` 的带账面版本：``(新文本, 账面 | None)``。

    账面字段：``from``（原文长度）、``to``（保留长度）、``omitted``（省略字数）、
    ``limit``（本次上限）。未超限时返回 ``(原文, None)``，供调用方据此判断
    「是否真的截断过」并在响应里加法式回传。

    约定与 :func:`clip_text_notice` 完全一致（同一实现，字节输出不变）。
    """
    meta = clip_text_notice_meta(text, limit)
    if meta is None:
        return text, None
    cut = meta["to"]
    omitted = meta["omitted"]
    name = label or "文本"
    try:
        logger.warning(
            "上下文截断（%s）：%d → %d 字，已省略 %d 字（模型可见说明已附加）",
            name,
            len(text),
            cut,
            omitted,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断生成
        # ✅ R204：留痕失败落降级计数（不再静默 pass）。
        _record_ledger_failure("truncation_log")
    try:
        from app.services.ai.prompts._metrics import record_token_budget_truncated

        record_token_budget_truncated(name)
    except Exception:  # noqa: BLE001 - 指标失败不得阻断生成
        _record_ledger_failure("truncation_counter")
    out = text[:cut] + f"\n\n[……{name}已截断：原文 {len(text)} 字，此处保留前 {cut} 字，已省略 {omitted} 字]"
    return out, {"from": len(text), "to": cut, "omitted": omitted, "limit": int(limit)}


def clip_text_notice_at(text: str, cut: int, label: str = "") -> str:
    """在**给定切点**收刀，附加模型可见说明并留痕（日志 + 指标）。

    ✅ R202（2026-11-09 · G2）：供「自定义切点算法」复用同一套说明与账目
    格式 —— 原先只有「按 limit 自动切点」这一条路径带模型可见说明；
    另有调用方（如 ``routers/_sse_outline_review._truncate_context``）按自己的
    规则（句末边界）切点后**直接返回**，模型看不到任何截断痕迹，只能把
    半份上下文当成完整上下文理解（并据此断言「资料里没有提到 X」）。

    与 :func:`clip_text_notice_report` 的关系：后者 = 「算切点 + 记账」，
    本函数 = 「按给定切点记账」，两者产出的说明文本格式**逐字相同**。

    :param cut: 保留长度（``0 <= cut <= len(text)``；越界会被夹到合法区间）。
    :return: 收刀后的文本（附模型可见说明）。
    """
    if not text:
        return text
    total = len(text)
    cut = max(0, min(int(cut), total))
    name = label or "文本"
    omitted = total - cut
    try:
        logger.warning(
            "上下文截断（%s）：%d → %d 字，已省略 %d 字（模型可见说明已附加）",
            name,
            total,
            cut,
            omitted,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断生成
        # ✅ R204：留痕失败落降级计数（不再静默 pass）。
        _record_ledger_failure("truncation_log")
    try:
        from app.services.ai.prompts._metrics import record_token_budget_truncated

        record_token_budget_truncated(name)
    except Exception:  # noqa: BLE001 - 指标失败不得阻断生成
        _record_ledger_failure("truncation_counter")
    return text[:cut] + f"\n\n[……{name}已截断：原文 {total} 字，此处保留前 {cut} 字，已省略 {omitted} 字]"


def clip_text_notice(text: str, limit: int, label: str = "") -> str:
    """进模型请求的截断：**按边界收刀 + 模型可见说明 + 日志/指标留痕**。

    与 :func:`clip_text` 的区别只在「模型可见」：``clip_text`` 仅写日志，
    模型收到的仍是半截文本、无从得知后面还有内容（会据此断言「未提及」）。
    本函数在截断后追加一段说明，显式告知原文长度与省略字数：

        [……{label}已截断：原文 N 字，此处保留前 K 字，已省略 M 字]

    约定：
    * 未超限时**原样返回**（逐字节一致），``limit <= 0`` 表示不截断；
    * 切点优先落在段落/换行/句末/分句/词边界，边界离上限太远才硬切；
    * 留痕（日志/指标）失败一律 fail-soft，绝不阻断生成。

    2026-11-07 起改为 :func:`clip_text_notice_report` 的薄包装（取新文本），
    返回值逐字节不变；需要账面（from/to/omitted）的调用方改用 report 版本。
    """
    return clip_text_notice_report(text, limit, label)[0]


def _repair_closing_fence(before: str, after: str) -> str:
    """预算截断可能切掉围栏的闭合标记 —— 补回，避免模型把后续内容当作资料。

    仅在「截断前含闭合标记、截断后丢失」时补一个（不重复、不改变正常文本）。
    """
    try:
        from app.services.prompt_governance import MATERIAL_CLOSE

        if MATERIAL_CLOSE in before and MATERIAL_CLOSE not in after:
            return after.rstrip("\n") + "\n" + MATERIAL_CLOSE
    except Exception:  # noqa: BLE001
        pass
    return after
