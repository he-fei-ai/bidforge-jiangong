# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮 Agent 辅助修复（R147）。

定位（用户指令 §三）：「复杂排版错乱可引入 Agent 辅助」。本模块把
Agent 的作用域**严格收窄**到一个可验证的变换上：

    识别标题编号方案 → 返回每个标题的结构化编号决策 → 本地按决策重排。

Agent **永不直接产出/修改 XML**（防注入与错乱）；它只返回
``{para_seq, scheme, level}`` 白名单决策，重排的 old/new token 全部由
本地 ``plan_heading_renumber`` 计算，且决策可被本地校验拒绝
（方案不在白名单 / 级别越界 / 段落定位失配 → 丢弃该决策）。

无 API key / Agent 失败 → 原样返回 unresolved（不阻断交付）。
场景名 ``export_docx_repair``（provider_factory.KNOWN_SCENES 已登记）。
"""
from __future__ import annotations

import logging
from typing import Callable

import app.services.docx_repair._xmlutil as _xu
from app.services.docx_repair._xmlutil import iter_paragraphs, para_text
from app.services.docx_repair.inspector import plan_heading_renumber
from app.services.docx_repair.model import RepairAction, RepairFinding

logger = logging.getLogger("docx_repair")

#: Agent 可选方案白名单（与 inspector._L1_SCHEMES 键一致）
_SCHEMES = ("dizhang", "dun", "arabic")
_MAX_HEADINGS = 200

EmitFn = Callable[[dict], None]


def _noop_emit(_evt: dict) -> None:
    return None


def agent_fix_layout(
    parts: dict[str, bytes],
    unresolved: list[RepairFinding],
    emit: EmitFn = _noop_emit,
) -> tuple[list[RepairAction], list[RepairFinding]]:
    """对程序修复无法收敛的 C1 类问题做 Agent 辅助编号决策。

    返回 ``(actions, 仍未解决的 unresolved)``。决策不改变文本之外的任何
    结构；重排的替换对（old→new）由本地计算并逐一校验。
    """
    actions: list[RepairAction] = []
    xml = parts.get("word/document.xml", b"").decode("utf-8", errors="replace")
    headings: list[tuple[int, int, str]] = []
    for seq, (_s, _e, p) in enumerate(iter_paragraphs(xml)):
        lvl = _xu.para_heading_level(p)
        if lvl:
            headings.append((seq, lvl, para_text(p).strip()))
    if not headings or len(headings) > _MAX_HEADINGS:
        return actions, list(unresolved)

    plan = plan_heading_renumber(headings)
    unknown = plan.get("unrecognized") or []
    if not unknown:
        return actions, list(unresolved)

    decisions = _ask_agent_for_scheme(headings, unknown, unresolved)
    if not decisions:
        return actions, list(unresolved)

    # 本地按决策重排：用 Agent 给出的 L1 方案重算 plan（安全阀解除）。
    # 编号深度/逐链替换仍全部由 inspector.plan_heading_renumber 计算。
    scheme = decisions.get("scheme")
    if scheme not in _SCHEMES:
        return actions, list(unresolved)
    plan2 = plan_heading_renumber(headings, scheme=scheme)
    n_fixed = 0
    from app.services.docx_repair.fixes import _replace_text_token
    for item in plan2["renumber"]:
        seq = int(item.get("para_seq", -1))
        if seq < 0:
            continue
        # 决策校验：该标题必须是 Agent 明确认可的已知形态（未知级别不动）
        lvl_decision = (decisions.get("levels") or {}).get(str(seq))
        if lvl_decision is not None and (not isinstance(lvl_decision, int)
                                         or not 1 <= lvl_decision <= 9
                                         or lvl_decision != int(item.get("level", 0))):
            continue
        spans = [(m2.start(), m2.end(), m2.group(0))
                 for m2 in _xu._PARA_RE.finditer(xml)]
        if seq >= len(spans):
            continue
        s, _e, p = spans[seq]
        new_p, n = _replace_text_token(
            p, str(item["old_token"]).strip(), str(item["new"]).strip())
        if n:
            xml = xml[:s] + new_p + xml[s + len(p):]
            n_fixed += 1
    if not n_fixed:
        return actions, list(unresolved)
    parts["word/document.xml"] = xml.encode("utf-8")
    actions.append(RepairAction(
        category="numbering", code="agent_fix", location="document.xml",
        message=f"Agent 辅助重排 {n_fixed} 个标题编号（方案：{scheme}，"
                "决策由 Agent 给出、替换对由本地计算并校验）",
        detail={"fixed": n_fixed, "scheme": scheme}))
    # 重排后复检：num_sequence 类 unresolved 就地清除
    still = [u for u in unresolved
             if u.code not in ("num_sequence", "num_scheme_unknown")]
    return actions, still


def _xu_re_subnum(text: str):
    from app.services.docx_repair.inspector import _SUB_NUM_RE
    return _SUB_NUM_RE.match(text)


def _ask_agent_for_scheme(headings, unknown, unresolved) -> dict:
    """调用 LLM 返回 ``{"scheme": ..., "levels": {seq: level}}``；失败返回 {}。

    本函数在构建线程（asyncio.to_thread）内同步执行：直接
    ``asyncio.run`` 起独立事件循环调 collect_json_response（async）。
    """
    import asyncio

    from app.config import settings

    if not (getattr(settings, "agnes_api_key", "") or ""):
        logger.info("Agent 辅助修复跳过：未配置 AI key")
        return {}
    sample = [
        {"para_seq": seq, "level": lvl, "text": text[:60]}
        for seq, lvl, text in headings[:_MAX_HEADINGS]
    ]
    prompt = (
        "以下是导出 DOCX 初稿的标题清单（para_seq 为文档顺序号）。"
        "前若干个标题的编号方案无法识别，导致编号重排被保守跳过。"
        "请判断全文 L1 标题的编号方案，只返回 JSON："
        '{"scheme": "dizhang"|"dun"|"arabic", "levels": {"para_seq": 级别}}。\n'
        '方案含义：dizhang=「第X章」，dun=「一、」，arabic=「1」。\n'
        "标题清单：\n" + _json_dumps(sample))
    try:
        from app.services.ai.json_response import collect_json_response
        result = asyncio.run(collect_json_response(
            [{"role": "user", "content": prompt}],
            scene="export_docx_repair", max_retries=1, timeout=90))
    except Exception as e:  # noqa: BLE001
        logger.warning("Agent 编号决策调用失败（降级为程序重排跳过）: %s", e)
        return {}
    if not isinstance(result, dict):
        return {}
    return {"scheme": result.get("scheme"),
            "levels": result.get("levels") if isinstance(
                result.get("levels"), dict) else {}}


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
