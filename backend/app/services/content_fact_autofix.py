"""正文生成后 · 本章事实冲突自动定点修复（R77 · 2026-10-14 引入易标机制）

引入背景（对齐参考软件 OpenBidKit 易标 ``contentGenerationTask.cjs``）
--------------------------------------------------------------
参考软件的正文一致性保障是「生成后审计 + 定点替换」闭环：

  ① 把正文与全局事实一起交给 AI，**只检查已经明确写出**的事实冲突；
  ② AI 返回冲突章节 / 冲突原文 / 违反了哪条事实；
  ③ 修复时返回精确修改 ``old_text`` / ``new_text``；
  ④ 程序只有在 ``old_text`` 能够**唯一命中**当前小节时才替换；找不到、
     或多处相同内容都拒绝修改，并把失败原因回灌给 AI 重试（上限 2 次）；
  ⑤ 仍失败 → 转人工核对，**不改写已经正确的内容**。

本软件既有链路与该机制的差距（引入前实测）：
  * ``content_standard.standard_report`` 每章落库前都会检出
    ``value_mismatch`` / ``value_conflict`` / ``model_conflict`` 三类
    **error 级事实冲突**（正文数值/型号与本章事实矛盾），但此前只写进
    ``last_generation_report`` 报告，**不进入任何修复链路** —— 用户要到
    审核/预检或全文一致性 Agent 才能处理，而后者需先扫描 + 仲裁 + 人工确认；
  * ``consistency_edits`` 已具备「唯一命中才替换」的定点编辑能力
    （``find_unique_span`` / ``apply_unique_edits``），但只挂在全文一致性
    Agent 链路，未接入正文生成自检的自动修复闭环。

本模块把两侧接起来：standard_report 检出的事实冲突 → AI 产出
old_text/new_text 定点编辑 → 唯一命中才替换 → 失败回灌重试（上限 2 次）
→ 只回写发生变化的小节。纯逻辑（冲突筛选 / 结果结构）与 IO（AI 调用）
分离，便于单测与 A/B 反向验证。

安全边界（刻意与参考软件对齐）：
  * 只修 standard_report 已明确检出的 error 级事实冲突，不扩大审计面；
  * AI 可返回空 ``edits``（判定并非真实矛盾时宁可不改，见提示词纪律 7）；
  * ``old_text`` 不唯一 / 找不到一律拒绝，绝不猜（复用
    ``consistency_edits.apply_unique_edits`` 同一判据，不复制第二份）；
  * 任何异常 fail-soft，绝不阻断正文落库；
  * 无冲突章节零 AI 调用（成本中性）。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("content_fact_autofix")

#: standard_report 产出的「正文取值与本章事实矛盾」issue 类型（均为 error 级）。
#: 口径唯一事实源 = content_standard.standard_report 的 issue type 值域，
#: 此处只做**筛选**（白名单），不新造判据。
FACT_CONFLICT_ISSUE_TYPES: tuple[str, ...] = (
    "value_mismatch",   # 精准模式：正文数值与事实不一致
    "value_conflict",   # 模糊模式：正文数值与事实偏差超 ±10% 容差
    "model_conflict",   # 两模式：同系列不同型号且与事实标题语境同现
)

#: 定点修复尝试上限（对齐参考软件 ``CONSISTENCY_REPAIR_MAX_ATTEMPTS = 2``）：
#: 首次 + 一次「带具体失败原因的定向修复」。JSON 层面的格式修复由
#: ``collect_json_response_with_chat`` 自带的有界重试负责，两个层次不混算。
FACT_AUTOFIX_MAX_ATTEMPTS = 2

#: 汇总键序（空汇总工厂与前端消费面共用，防止两端口径漂移）
FACT_AUTOFIX_SUMMARY_KEYS = (
    "sections", "fixed_sections", "conflicts", "applied", "rejected")


def empty_fact_autofix_summary() -> dict:
    """空汇总（单一事实源：sse_handlers 初始化与前端默认值都经此产出）。"""
    return {k: 0 for k in FACT_AUTOFIX_SUMMARY_KEYS}


def accumulate_fact_autofix(summary: dict | None, report: dict | None) -> dict:
    """把单章校验报告里的 ``fact_autofix`` 结果累加进终态汇总（纯函数）。

    Args:
        summary: 既有汇总（None / 坏形态 → 按空汇总起步）。
        report: 该章落库时的校验报告（``standard_report`` 产物，可含
            ``fact_autofix`` 键；无该键 / 非 dict → 原样返回汇总）。

    Returns:
        新汇总 dict（不改入参）。

    计数口径（与 :func:`select_fact_conflicts` 的触发条件严格一致）：
        - 只在 ``attempted`` 为真时计数 —— 无冲突章节零成本、不进汇总，
          终态载荷与功能引入前同构（空汇总全 0）；
        - ``attempted`` 但未修复也计数（``sections``+1、``fixed_sections``+0）——
          「转人工核对」的信号不静默（R71「失败不静默」纪律）；
        - ``conflicts`` = 检出并送修的冲突条数；``applied`` = 实际替换处数；
          ``rejected`` = 被拒 edit 条数（多处命中/找不到/过短）。
    """
    out = dict(summary) if isinstance(summary, dict) else empty_fact_autofix_summary()
    for k in FACT_AUTOFIX_SUMMARY_KEYS:
        out[k] = int(out.get(k) or 0)
    fa = report.get("fact_autofix") if isinstance(report, dict) else None
    if not isinstance(fa, dict) or not fa.get("attempted"):
        return out
    rejected = fa.get("rejected")
    out["sections"] += 1
    out["fixed_sections"] += 1 if fa.get("applied") else 0
    out["conflicts"] += int(fa.get("conflicts") or 0)
    out["applied"] += int(fa.get("applied") or 0)
    out["rejected"] += len(rejected) if isinstance(rejected, list) else 0
    return out


def select_fact_conflicts(report) -> list[dict]:
    """从 ``standard_report`` 报告中筛出 **error 级事实冲突** issue（纯函数）。

    只认 ``FACT_CONFLICT_ISSUE_TYPES`` 三类（正文数值/型号与本章事实矛盾），
    且 severity 必须为 ``error`` —— 与易标「只找已经明确写出、且与事实相违背
    的内容」同口径；``fact_value_missing``（事实未被引用）是 warning，不在此列
    （缺项≠冲突，报缺失会诱导 AI 补写，违背易标审计纪律第 3 条）。

    Args:
        report: ``standard_report`` 返回的 dict（或任何值；非 dict 返回空表）。

    Returns:
        冲突 issue 列表（元素为原 report 中的 issue dict，原样引用不改写）。
    """
    if not isinstance(report, dict):
        return []
    issues = report.get("issues")
    if not isinstance(issues, list):
        return []
    out: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        if it.get("type") in FACT_CONFLICT_ISSUE_TYPES \
                and it.get("severity") == "error":
            out.append(it)
    return out


def _validate_edits(obj) -> list:
    """定点编辑 JSON 的结构校验（与 consistency_edits 同口径的薄校验）。

    只验「顶层是对象且 edits 是数组」；逐条编辑的可用性由
    ``apply_unique_edits`` 的「唯一命中」判据把关（拒绝即记原因）。
    """
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if obj.get("edits") is None:
        return ["缺少 edits 字段"]
    if not isinstance(obj.get("edits"), list):
        return ["edits 必须是数组"]
    return []


def _failure_lines(rejected: list) -> list[str]:
    """把 apply_unique_edits 的拒绝明细渲染为回灌给 AI 的失败原因行。"""
    lines: list[str] = []
    for item in rejected or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            old, reason = item
        elif isinstance(item, dict):
            old = item.get("old_text") or ""
            reason = item.get("reason") or ""
        else:  # pragma: no cover - 防御未知形态
            continue
        lines.append(f"{reason}: {str(old)[:60]}")
    return lines


async def autofix_section_fact_conflicts(
    *, content: str, standard: str, fact_rows: list,
    conflicts: list, section_id: str = "", section_title: str = "",
    chat_fn=None,
) -> tuple[str, dict]:
    """对一章已生成正文执行「事实冲突 → 定点替换」自动修复。

    完整链路（对齐易标普通修复）：

    1. 渲染本章事实文本（复用 ``facts_builder._render_facts_text`` 同一出口，
       不复制渲染逻辑）；
    2. AI 只产出 ``edits``（old_text/new_text/reason），**不重写整章**；
    3. ``apply_unique_edits`` 逐条应用：old_text 唯一命中才替换，多处命中 /
       找不到 / 过短一律拒绝；
    4. 有拒绝 → 把失败原因回灌给 AI 定向重试（上限
       ``FACT_AUTOFIX_MAX_ATTEMPTS`` 次）；仍失败 → 保留已应用的部分，
       结果里记 ``rejected`` 供人工核对（易标同款「转人工」兜底）；
    5. AI 返回空 edits（判定并非真实矛盾）→ 原文返回，一个字节都不改。

    Args:
        content: 章节正文（Markdown）。
        standard: 生效生成标准（precise / fuzzy；驱动事实引导语口径）。
        fact_rows: 本章**实际注入**的事实行（5 元组，与 standard_report 同源）。
        conflicts: ``select_fact_conflicts`` 筛出的 error 级事实冲突。
        section_id / section_title: 仅用于提示词定位与日志。
        chat_fn: 可选的 ``chat_with_fallback`` 同签名可调用对象（测试注入）。

    Returns:
        ``(新正文, 结果 dict)``。结果 dict 字段：
        ``attempted``（是否真的发起了 AI 调用）、``applied``（成功替换条数）、
        ``rejected``（被拒明细）、``conflicts``（输入冲突条数）、
        ``attempts``（实际 AI 轮数）。任何失败路径都不抛异常。
    """
    result: dict = {
        "attempted": False, "applied": 0, "rejected": [],
        "conflicts": len(conflicts or []), "attempts": 0,
    }
    body = content or ""
    if not body or not conflicts:
        return body, result
    # 无事实文本无从修复（调用方已门控 fact_rows_for_section，此处双保险）
    from app.services.facts_builder import _render_facts_text
    facts_text = _render_facts_text(list(fact_rows or []))
    if not facts_text:
        return body, result
    if chat_fn is None:
        from app.services.ai.provider_factory import chat_with_fallback as chat_fn

    from app.services.ai.json_response import (  # 局部导入：避免模块级环
        GENERIC_REPAIR_KEY,
        collect_json_response_with_chat,
    )
    from app.services.ai.prompts._registry import render
    from app.services.consistency_edits import apply_unique_edits

    result["attempted"] = True
    messages = [
        {"role": "system", "content": render("content_fact_autofix_system")},
        {"role": "user", "content": render(
            "content_fact_autofix_user",
            global_facts=facts_text,
            section_id=section_id or "-",
            section_title=section_title or "-",
            generation_standard=standard or "",
            conflicts=json.dumps(conflicts, ensure_ascii=False, indent=2),
        ) + f"\n\n【待修复章节原文】\n{body}"},
    ]

    failures: list[str] = []
    for attempt in range(1, FACT_AUTOFIX_MAX_ATTEMPTS + 1):
        result["attempts"] = attempt
        req = list(messages)
        if failures:
            # 失败原因回灌（对齐易标 buildConsistencyRepairMessages 的
            # 「上次修复应用失败原因」块）：让下一轮带着具体问题定向重试。
            req.append({"role": "user", "content":
                        "上次修复应用失败原因：\n"
                        + "\n".join(f"- {f}" for f in failures)
                        + "\n请重新返回能够在当前正文中唯一定位的 old_text。"})
        try:
            obj, _raw = await collect_json_response_with_chat(
                chat_fn, req,
                validate_fn=_validate_edits,
                max_retries=1,
                temperature=0.1, timeout=120,
                scene="content_fact_autofix",
                repair_key=GENERIC_REPAIR_KEY,
            )
        except Exception as e:  # noqa: BLE001 - fail-soft：进下一轮或放弃
            failures = [f"第 {attempt} 次修复调用失败：{e}"]
            logger.warning(
                "[事实冲突定点修复] 第 %s 章节第 %d 次调用异常: %s",
                section_id[:8], attempt, e)
            continue

        if not isinstance(obj, dict):
            failures = ["模型未返回可应用的 edits"]
            continue
        edits = obj.get("edits") or []
        if not edits:
            # AI 判定并非真实矛盾 → 宁可不改（安全阀，易标提示词同款纪律）
            logger.info(
                "[事实冲突定点修复] 第 %s 章节 AI 判定无需修改"
                "（冲突 %d 条）", section_id[:8], len(conflicts))
            return body, result

        res = apply_unique_edits(body, edits)
        body = res.content
        result["applied"] += res.applied
        result["rejected"] = [
            {"old_text": o[:60], "reason": r} for o, r in res.rejected]
        if res.applied > 0 and not res.rejected:
            return body, result
        # 部分成功 / 全部被拒：把拒绝原因带回下一轮；已应用的结果保留在
        # body 里（下一轮的 old_text 相对**最新**正文定位，语义正确）。
        failures = _failure_lines(res.rejected) or ["模型未返回可应用的 edits"]
        logger.info(
            "[事实冲突定点修复] 第 %s 章节第 %d 轮：应用 %d、拒绝 %d",
            section_id[:8], attempt, res.applied, len(res.rejected))
    return body, result
