"""标段（投标范围）上下文提示 —— 移植自 OpenBidKit。

参考实现：`client/electron/utils/bidSectionContext.cjs::buildBidSectionContextHint`
配套实现：`client/electron/services/bidAnalysisTask.cjs` 把该提示作为**独立
system 消息**注入每一次招标文件解析调用（单项提取与分段合并都注入）。

为什么需要：
同一份多标段招标文件里，各标段的工程规模、技术参数、工期完全不同。若不显式
告诉模型「本次只处理 X 标段」，模型会把其它标段的参数一起抽出来（例如把一标段
的基坑深度写进二标段），下游目录/正文/图表全部跟着错。本软件此前只在
`upload_outline` 做规则检测并回传 `multi_section_hint`，**从未把选中标段
注入任何 AI 调用** —— `build_system_prompt(section_hint)` 的入参形同虚设。

设计约束：
- 纯函数 `build_bid_section_context_hint` 不依赖 DB，便于单测与复用；
- 无选中标段时返回空串 → 调用方 system prompt 与旧版逐字一致（向后兼容）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: 基础提示（与参考实现逐字对齐）
_BASE_HINT = (
    "本项目为多标段，当前招标文件已按用户选择的投标范围处理。"
    "请仅关注当前选择标段和当前输入内容，不要主动扩展到其他标段。"
)
#: 没有标段明细时的提示（仅「已选定范围」这一事实可告知）
_NO_DETAIL_HINT = (
    "本项目为多标段，当前招标文件已按用户选择的投标范围处理。"
    "请以当前输入内容为准，不要主动扩展到其他标段。"
)

#: evidence 最多保留条数（与参考实现一致：避免把大段原文塞进 system 消息）
_MAX_EVIDENCE = 6


def _normalize_text(value) -> str:
    """压平空白并去首尾空格（参考实现 normalizeText 的等价语义）。"""
    return " ".join(str(value or "").split()).strip()


def _normalize_evidence(value) -> list[str]:
    """把 evidence 归一化为非空字符串列表（最多 6 条）。"""
    items = value if isinstance(value, (list, tuple)) else []
    out = [_normalize_text(x) for x in items]
    return [x for x in out if x][:_MAX_EVIDENCE]


def build_bid_section_context_hint(selected_section: Optional[dict] = None,
                                   has_selected_section: bool = False) -> str:
    """构建标段上下文提示（无任何有效信息时返回空串）。

    Args:
        selected_section: 选中的标段对象，允许字段：
            id / title / headLine(或 head_line) / description / evidence
        has_selected_section: 是否「已选定投标范围」但标段明细缺失。
            为 True 时至少给出「请以当前输入内容为准」的兜底提示。

    Returns:
        多行提示文本；未选定范围且无明细时返回 ""（调用方据此保持旧行为）。
    """
    section = selected_section if isinstance(selected_section, dict) else {}
    title = _normalize_text(section.get("title"))
    head_line = _normalize_text(section.get("headLine") or section.get("head_line"))
    description = _normalize_text(section.get("description"))
    evidence = _normalize_evidence(section.get("evidence"))

    if not (title or head_line or description or evidence):
        return _NO_DETAIL_HINT if has_selected_section else ""

    lines = [_BASE_HINT]
    if title:
        lines.append(f"当前选择标段：{title}")
    if head_line:
        lines.append(f"AI 识别标题行：{head_line}")
    if description:
        lines.append(f"AI 识别描述：{description}")
    if evidence:
        lines.append(f"AI 识别依据：{'；'.join(evidence)}")
    return "\n".join(lines)


def parse_selected_section_json(raw) -> dict:
    """解析 bid_sections.selected_section_json 列（容错为 {}）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def load_bid_section_row(db, project_id: str, scheme_id: str = "") -> dict:
    """读取该项目的多标段检测/选择行（不存在时返回 {}）。

    作用域口径与 `bid_analysis.check_bid_sections` 的写入口径一致：
    优先精确匹配 scheme_id，取不到再退回该项目最近一次检测结果。
    """
    if not project_id:
        return {}
    sql = "SELECT * FROM bid_sections WHERE project_id=?"
    params: list = [project_id]
    if scheme_id:
        # 精确匹配当前方案优先（scheme_id 相同），其次项目级（scheme_id=''）
        sql += (" AND (scheme_id=? OR scheme_id='')"
                " ORDER BY CASE WHEN scheme_id=? THEN 0 ELSE 1 END,"
                " updated_at DESC LIMIT 1")
        params.extend([scheme_id, scheme_id])
    else:
        sql += " ORDER BY updated_at DESC LIMIT 1"
    try:
        cur = await db.execute(sql, tuple(params))
        row = await cur.fetchone()
    except Exception as e:  # 旧库可能缺列/缺表 —— 一律降级为「无选择」
        logger.warning("读取标段选择失败（按未选择处理）: %s", e)
        return {}
    return dict(row) if row else {}


def selected_section_from_row(row: dict) -> dict:
    """从 bid_sections 行还原「选中标段对象」。"""
    if not row:
        return {}
    detail = parse_selected_section_json(row.get("selected_section_json"))
    if detail:
        return detail
    # 兼容：仅有标题的历史数据（或前端只传了 title）
    title = _normalize_text(row.get("selected_section_title"))
    sid = _normalize_text(row.get("selected_section_id"))
    if title or sid:
        return {"id": sid, "title": title}
    return {}


async def resolve_section_hint(db, project_id: str,
                               scheme_id: str = "") -> tuple[str, dict]:
    """读取当前选中标段并生成提示（唯一注入入口）。

    ✅ 向后兼容约定：**只有用户确实选定过投标范围时**才返回非空提示。
    多标段项目但未选择时返回空串 —— 旧行为的 system prompt 逐字不变，
    避免「升级后提取结果悄悄变化」。未选择这一事实由路由层通过
    `multi_section_unselected` 字段回传给前端提示用户去选择。

    Returns:
        (hint, selected_section)
    """
    row = await load_bid_section_row(db, project_id, scheme_id)
    section = selected_section_from_row(row)
    hint = build_bid_section_context_hint(
        section, has_selected_section=bool(section))
    return hint, section
