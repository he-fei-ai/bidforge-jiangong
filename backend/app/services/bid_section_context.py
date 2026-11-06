"""标段（施工范围）上下文提示的构建与读取。

本模块只做两件事：

1. **提示构建**：把用户选定的施工范围渲染成一段注入 AI 调用的 system 提示；
2. **选择读取**：从 ``bid_sections`` 表读取已落库的选择，并还原成标段对象。

为什么需要第二段 system 消息：同一份多标段招标文件里，各标段的工程规模、
技术参数、工期完全不同。若不明示「本次只处理 X 标段」，模型会把其它标段的
参数混入本次结果（例如把一标段的基坑深度写进二标段），下游的目录、正文与
图表会全部跟着偏。

两条设计约束：

- 提示构建是纯函数，不依赖数据库，便于单测与复用；
- 没有任何有效明细、且未声明「已选定范围」时返回空串 —— 调用方据此保持
  既有提示词逐字不变，避免升级后提取结果悄悄变化。
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: 基础提示（语义：限定在当前范围，不扩散到其它标段）。
#: 措辞采用中性的「项目资料 / 施工范围」，避免把招投标语境带进每一轮提取。
_BASE_HINT = (
    "本项目包含多个标段，当前资料已按用户选择的施工范围处理。"
    "请仅关注当前选择标段和当前输入内容，不要主动扩展到其他标段。"
)

#: 「已选定范围但无明细」时的兜底提示：只告知限定意图，不编造标段属性。
_NO_DETAIL_HINT = (
    "本项目包含多个标段，当前资料已按用户选择的施工范围处理。" "请以当前输入内容为准，不要主动扩展到其他标段。"
)

#: 依据条目上限。
#: 推导依据：提示会在**每一次**提取调用（逐段 + 合并）的 system 消息里重复出现，
#: 属于固定上下文成本。单条依据按 40~60 字估算，6 条约 300~360 字；加上标题、
#: 描述与引导语，整段提示约 500 字以内，占单次调用预算（数万字符）的 1% 左右。
#: 取 6 而非更大值，是为了在「保留足够佐证」与「不稀释主提示」之间取平衡。
_MAX_EVIDENCE = 6

#: 依据条目之间的分隔符（中文分号：与正文的并列关系一致，且比换行更紧凑）。
_EVIDENCE_SEPARATOR = "；"


def _flatten_whitespace(value) -> str:
    """把任意值压成单行文本：折叠连续空白并去掉首尾空白。"""
    return " ".join(str(value or "").split()).strip()


def _join_evidence(value) -> str:
    """把依据列表渲染为「条目；条目」形式，自动丢弃空条目并截断到上限。"""
    raw_items = value if isinstance(value, (list, tuple)) else ()
    cleaned = (_flatten_whitespace(item) for item in raw_items)
    kept = [item for item in cleaned if item][:_MAX_EVIDENCE]
    return _EVIDENCE_SEPARATOR.join(kept)


# 字段渲染表：按展示顺序声明「输出标签 → 取值来源 → 渲染方式」。
# 用表驱动而非 if/elif 链，新增字段只需追加一行，渲染顺序与标签不会分叉。
# 同一字段允许配置多个来源键：历史数据可能用 camelCase（headLine），
# 前端可能传 snake_case（head_line），按声明顺序取第一个非空值。
_FIELD_SPECS: tuple[tuple[str, tuple[str, ...], Callable[[object], str]], ...] = (
    ("当前选择标段", ("title",), _flatten_whitespace),
    ("AI 识别标题行", ("headLine", "head_line"), _flatten_whitespace),
    ("AI 识别描述", ("description",), _flatten_whitespace),
    ("AI 识别依据", ("evidence",), _join_evidence),
)


def _first_present(section: dict, keys: tuple[str, ...]) -> object:
    """按声明顺序返回第一个非空来源值（全空时返回 None）。"""
    for key in keys:
        value = section.get(key)
        if value:
            return value
    return None


def _render_detail_lines(section: dict) -> list[str]:
    """把标段明细渲染成「标签：内容」行；没有任何有效内容时返回空列表。"""
    lines: list[str] = []
    for label, source_keys, render_fn in _FIELD_SPECS:
        raw = _first_present(section, source_keys)
        if raw is None:
            continue
        text = render_fn(raw)
        if text:
            lines.append(f"{label}：{text}")
    return lines


def build_bid_section_context_hint(selected_section: Optional[dict] = None, has_selected_section: bool = False) -> str:
    """构建标段上下文提示（无任何有效信息时返回空串）。

    Args:
        selected_section: 选中的标段对象，允许字段：
            id / title / headLine（或 head_line）/ description / evidence
        has_selected_section: 是否「已选定施工范围」但标段明细缺失。
            为 True 时至少给出「请以当前输入内容为准」的兜底提示。

    Returns:
        多行提示文本；未选定范围且无明细时返回 ""（调用方据此保持旧行为）。
    """
    section = selected_section if isinstance(selected_section, dict) else {}
    detail_lines = _render_detail_lines(section)
    if not detail_lines:
        return _NO_DETAIL_HINT if has_selected_section else ""
    return "\n".join([_BASE_HINT, *detail_lines])


def parse_selected_section_json(raw) -> dict:
    """解析 ``bid_sections.selected_section_json`` 列（任何异常都容错为 {}）。

    容错边界：空值、非 JSON、JSON 数组/标量都返回 {}，只有 JSON 对象才透传。
    """
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

    作用域口径：优先精确匹配 scheme_id，其次项目级（scheme_id=''）；同一层内
    取最近更新的一条。旧库可能缺表或缺列，一切读取异常都降级为「无选择」，
    不让读取失败阻塞解析主流程。
    """
    if not project_id:
        return {}
    sql = "SELECT * FROM bid_sections WHERE project_id=?"
    params: list = [project_id]
    if scheme_id:
        sql += (
            " AND (scheme_id=? OR scheme_id='')"
            " ORDER BY CASE WHEN scheme_id=? THEN 0 ELSE 1 END,"
            " updated_at DESC LIMIT 1"
        )
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
    """从 ``bid_sections`` 行还原「选中标段对象」。

    还原顺序：优先完整明细 JSON（selected_section_json），否则退回仅有
    title/id 的历史数据（兼容前端只传标题、或旧版本只落库标题的情形）。
    """
    if not row:
        return {}
    detail = parse_selected_section_json(row.get("selected_section_json"))
    if detail:
        return detail
    title = _flatten_whitespace(row.get("selected_section_title"))
    section_id = _flatten_whitespace(row.get("selected_section_id"))
    if title or section_id:
        return {"id": section_id, "title": title}
    return {}


async def resolve_section_hint(db, project_id: str, scheme_id: str = "") -> tuple[str, dict]:
    """读取当前选中标段并生成提示（唯一的注入入口）。

    向后兼容约定：**只有用户确实选定过施工范围时**才返回非空提示。
    多标段项目但未选择时返回空串 —— 旧行为的 system prompt 逐字不变。
    「未选择」这一事实由路由层通过 ``multi_section_unselected`` 字段回传前端，
    由前端提示用户去选择，而不是靠这里注入一段暗示性文案。

    Returns:
        (hint, selected_section)
    """
    row = await load_bid_section_row(db, project_id, scheme_id)
    section = selected_section_from_row(row)
    hint = build_bid_section_context_hint(section, has_selected_section=bool(section))
    return hint, section
