"""招标文件多标段 AI 识别。

职责链路：带行号文本 → 分段 → 逐段结构化提取 → 候选合并 → 去重归一 →
「至少 2 个有效标段」校验。

与规则检测器的关系：规则检测器只回答「是否疑似多标段」（零 AI 成本）；
本模块在用户需要精确选择投标范围时才运行，给出可选标段清单（含行号区间与
依据），两者互补。只在本端点被显式调用时才产生 AI 调用，默认零成本。
"""

from __future__ import annotations

import logging
import re

from app.services.bid_analysis_service import split_for_analysis

logger = logging.getLogger(__name__)

#: 行号前缀的数字位宽。
#: 推导：一份 40 万字符的招标文件按每行 40 字估算约 1 万行；6 位可表示
#: 999999 行，留出约 100 倍余量，同时前缀总长只有 7 个字符（``L000001``），
#: 不至于让行号本身显著抬高分段体积。
_LINE_PREFIX_WIDTH = 6

#: 标段单位后缀（长词在前：先匹配「标段/标包/分包」，最后才匹配单字「包」）。
_TITLE_UNITS = ("标段", "标包", "分包", "包")

#: 可用于序数位的中文数字与阿拉伯数字。
_CN_NUMERALS = frozenset("一二三四五六七八九十壹贰叁肆伍")

#: 排序哨兵：没有有效行号区间的候选排到最后（不会与真实行号冲突）。
_NO_RANGE_ORDER = 2**31

#: 分段提取任务的规则清单（编号由位置生成，保证提示词与规则表永远同源）。
_EXTRACT_RULES = (
    "只识别明确属于某个标段的内容范围。",
    "通用条款不要归入某个标段；不确定归属的内容不要输出范围。",
    "include_ranges 必须使用输入中的真实行号，start_line 和 end_line 都是不带 L 前缀的数字。",
    "不要编造标段，不要补写原文没有的范围。",
    "无法提供有效 include_ranges 的候选不要输出到 sections。",
    '如果本段没有明确标段内容，返回 {"sections":[]}。',
    "只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。",
)

#: 合并任务的规则清单（同上，编号由位置生成）。
_MERGE_RULES = (
    "同一标段跨多个分段出现时合并为一个 sections 项。",
    "不要把通用条款合并到任何标段。",
    "不要新增分段结果中没有的行号范围。",
    "如果最终少于两个标段，原样返回已有结果。",
    "只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。",
)


def _numbered_rules(rules: tuple[str, ...]) -> str:
    """把规则表渲染成「1. …」形式的编号清单。"""
    return "\n".join(f"{position}. {rule}" for position, rule in enumerate(rules, start=1))


def number_markdown_lines(markdown: str) -> str:
    """给正文逐行加行号前缀（``L000001 | 原文``），供模型回填 include_ranges。"""
    lines = str(markdown or "").split("\n")
    return "\n".join(f"L{str(index + 1).zfill(_LINE_PREFIX_WIDTH)} | {line}" for index, line in enumerate(lines))


def _strip_ordinal_prefix(text: str) -> str:
    """去掉「第」字序数前缀（仅当整体形如「第<数字><单位>」时）。

    与正则等价但改为显式解析：先确认尾部单位，再确认前缀全部由数字组成。
    任一步不满足就原样返回，避免误伤「第一标段：设备采购」这类带后缀的标题。
    """
    if not text.startswith("第"):
        return text
    body = text[1:]
    for unit in _TITLE_UNITS:
        if not body.endswith(unit):
            continue
        numeral = body[: -len(unit)]
        if numeral and all(char in _CN_NUMERALS or char.isdigit() for char in numeral):
            return numeral + unit
        return text
    return text


def normalize_section_title(value: str) -> str:
    """标段标题归一化（用于合并同一标段在不同分段出现的候选）。"""
    text = re.sub(r"\s+", "", str(value or ""))
    return _strip_ordinal_prefix(text).strip().lower()


def get_section_merge_key(section: dict) -> str:
    """合并键：优先标题，无标题时退回序号。"""
    title_key = normalize_section_title(section.get("title"))
    unit = str(section.get("unit") or "标段")
    fallback = section.get("index")
    return f"{unit}:{title_key}" if title_key else f"{unit}:{fallback}"


def _coerce_line_number(raw: dict, primary: str, fallback: str) -> int | None:
    """取行号：优先 primary 键；该键完全缺失时才退回 fallback 键。

    返回 None 表示无法解析为数字（含空值、字符串脏值）。
    """
    value = raw[primary] if primary in raw else raw.get(fallback, 0)
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return None


def normalize_line_range(raw, total_lines: int) -> dict | None:
    """校验并归一化单个行号区间；非法（越界/倒序/非数字）返回 None。"""
    if not isinstance(raw, dict):
        return None
    start = _coerce_line_number(raw, "startLine", "start_line")
    end = _coerce_line_number(raw, "endLine", "end_line")
    if start is None or end is None:
        return None
    if not (1 <= start <= total_lines and start <= end <= total_lines):
        return None
    out = {"start_line": start, "end_line": end}
    reason = str(raw.get("reason") or "").strip()
    if reason:
        out["reason"] = reason
    return out


def merge_ranges(ranges: list) -> list[dict]:
    """区间去重（按 start-end-reason 去重）并按 startLine 排序。"""
    unique: dict[tuple, dict] = {}
    for item in ranges or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("start_line"), item.get("end_line"), item.get("reason", ""))
        # setdefault 保留首次出现的副本，等价于「先到先得」的集合去重
        unique.setdefault(key, dict(item))
    return sorted(unique.values(), key=lambda item: (item.get("start_line", 0), item.get("end_line", 0)))


def _first_range_start(section: dict) -> int:
    ranges = merge_ranges(section.get("include_ranges") or [])
    return ranges[0]["start_line"] if ranges else _NO_RANGE_ORDER


def normalize_section(raw, index: int, total_lines: int) -> dict | None:
    """归一化单个标段候选（无标题 → 丢弃）。"""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    try:
        sec_index = int(raw.get("index") or index + 1)
    except (TypeError, ValueError):
        sec_index = index + 1
    if sec_index <= 0:
        sec_index = index + 1

    raw_ranges = raw.get("include_ranges") or raw.get("includeRanges") or []
    include_ranges = [r for r in (normalize_line_range(x, total_lines) for x in raw_ranges) if r]
    evidence = [str(e).strip() for e in (raw.get("evidence") or []) if str(e).strip()]
    return {
        "id": str(raw.get("id") or f"section-{sec_index}").strip(),
        "index": sec_index,
        "unit": str(raw.get("unit") or "标段").strip() or "标段",
        "title": title,
        "head_line": str(raw.get("head_line") or raw.get("headLine") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "include_ranges": include_ranges,
        "evidence": evidence,
    }


def _absorb_section(target: dict, incoming: dict) -> None:
    """把同一标段的后一次出现并入已有候选（区间并集、依据去重、补空字段）。"""
    target["include_ranges"] = merge_ranges(
        [*(target.get("include_ranges") or []), *(incoming.get("include_ranges") or [])]
    )
    target["evidence"] = list(dict.fromkeys([*(target.get("evidence") or []), *(incoming.get("evidence") or [])]))
    for field in ("head_line", "description"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]


def dedupe_sections(sections: list[dict]) -> list[dict]:
    """合并同一标段的多次出现，丢弃无有效行号区间的候选，重编号 section-1..N。"""
    grouped: dict[str, dict] = {}
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        key = get_section_merge_key(section)
        if key not in grouped:
            grouped[key] = dict(section)
        else:
            _absorb_section(grouped[key], section)

    usable: list[dict] = []
    for section in grouped.values():
        section["include_ranges"] = merge_ranges(section.get("include_ranges") or [])
        # 拿不出真实行号范围的候选不算有效标段
        if section["include_ranges"]:
            usable.append(section)

    usable.sort(key=lambda section: (_first_range_start(section), section.get("index") or 0))
    for position, section in enumerate(usable, start=1):
        section["id"] = f"section-{position}"
        section["index"] = position
    return usable


def normalize_sections_response(value, total_lines: int) -> dict:
    """把模型返回的 ``{"sections": [...]}`` 归一化为去重后的候选集合。"""
    source = value.get("sections") if isinstance(value, dict) else None
    normalized = []
    for position, raw in enumerate(source if isinstance(source, list) else []):
        section = normalize_section(raw, position, total_lines)
        if section:
            normalized.append(section)
    return {"sections": dedupe_sections(normalized)}


def validate_sections_response(value: dict) -> None:
    """有效性校验：至少 2 个标段，否则抛 ValueError（由路由转成 422）。"""
    sections = value.get("sections") if isinstance(value, dict) else None
    if not isinstance(sections, list) or len(sections) < 2:
        raise ValueError("未识别到至少两个有效标段")


# ---------------------------------------------------------------------------
# 提示词
# 系统提示词存放在提示词注册表（``bid_section_extract_system`` /
# ``bid_section_merge_system``），本模块不保留字面量副本；每次调用时现取，
# 保证编辑器修改模板后本进程即时生效。
# ---------------------------------------------------------------------------


def _system_extract() -> str:
    from app.services.ai.prompts import get_prompt

    return get_prompt("bid_section_extract_system")


def _system_merge() -> str:
    from app.services.ai.prompts import get_prompt

    return get_prompt("bid_section_merge_system")


def build_extract_messages(segment: str, segment_index: int, total_segments: int) -> list[dict]:
    """分段提取提示词（带行号文本 → 结构化标段候选）。

    分段来自外部不可信原文，统一走 ``prompt_material_guard.govern_blob``
    （围栏 → 凭据脱敏 → 预算）；三个开关默认关闭，未开启时与引入前逐字节一致。
    """
    from app.services.prompt_material_guard import govern_blob

    segment = govern_blob(segment, "招标文件分段")
    user_content = f"""当前是招标文件第 {segment_index}/{total_segments} 段。每行格式为“L000001 | 原文”。

任务：识别本段中明确属于某个标段/标包/分包/采购包/包件/标的的内容，并返回结构化 JSON。

要求：
{_numbered_rules(_EXTRACT_RULES)}

返回格式：
{{
  "sections": [
    {{
      "index": 1,
      "unit": "标段",
      "title": "一标段",
      "head_line": "一标段：设备采购及安装",
      "description": "设备采购、安装、调试及售后服务。",
      "include_ranges": [
        {{"start_line": 120, "end_line": 180, "reason": "一标段采购清单"}}
      ],
      "evidence": ["一标段：设备采购及安装"]
    }}
  ]
}}

带行号文本：
{segment}"""
    return [
        {"role": "system", "content": _system_extract()},
        {"role": "user", "content": user_content},
    ]


def build_merge_messages(segment_results: list[dict]) -> list[dict]:
    """候选合并提示词（多分段结果 → 单一标段清单）。

    分段结果同样可能携带原文片段（evidence/description），合并前统一治理
    （围栏 → 凭据脱敏 → 预算）；默认关闭时逐字不变。
    """
    import json as _json

    from app.services.prompt_material_guard import govern_blob

    payload = govern_blob(_json.dumps(segment_results, ensure_ascii=False, indent=2), "招标文件分段识别结果")
    user_content = f"""以下是同一份招标文件各分段识别出的标段候选。请合并重复标段，保留所有明确属于各标段的 include_ranges 和 evidence。

要求：
{_numbered_rules(_MERGE_RULES)}

分段结果：
{payload}"""
    return [
        {"role": "system", "content": _system_merge()},
        {"role": "user", "content": user_content},
    ]


def _validate_has_sections(obj) -> list[str]:
    """collect_json_response 的校验函数：必须含 sections 且为数组。"""
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(obj.get("sections"), (list, tuple)):
        return ["缺少 sections 数组"]
    return []


def _split_numbered_markdown(numbered: str, chunk_size: int | None, chunk_overlap: int | None) -> list[str]:
    """按调用方给定的分段参数切分；未给参数时沿用统一分段器的默认值。"""
    split_kwargs: dict = {}
    if chunk_size:
        split_kwargs["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        split_kwargs["overlap"] = chunk_overlap
    return split_for_analysis(numbered, **split_kwargs) or [numbered]


async def extract_bid_sections(
    ai_collect=None, markdown: str = "", chunk_size: int | None = None, chunk_overlap: int | None = None
) -> dict:
    """AI 识别招标文件标段清单（分段提取 → 合并 → 去重）。

    Args:
        ai_collect: 异步 JSON 收集器，签名兼容
            ``app.services.ai.json_response.collect_json_response``
            （``await ai_collect(messages, validate_fn, json_mode=..., scene=...)``
            返回 ``(obj, raw)``）。注入以便单测。
        markdown: 招标文件全文（或项目资料的合并文本）。
        chunk_size: 分段字符数（None → 沿用 ``split_for_analysis`` 默认值）。
        chunk_overlap: 分段重叠字符数（None → 沿用默认值）。

    Returns:
        {"sections": [...], "segment_count": int, "estimated_calls": int}

    Raises:
        ValueError: 未识别到至少两个有效标段（由调用方转 422）。
    """
    from app.services.ai.json_response import collect_json_response

    collector = ai_collect or collect_json_response
    clean = str(markdown or "").strip()
    if not clean:
        raise ValueError("没有可识别的项目资料正文，请先上传并解析文件")

    total_lines = clean.count("\n") + 1
    numbered = number_markdown_lines(clean)
    segments = _split_numbered_markdown(numbered, chunk_size, chunk_overlap)

    logger.info("多标段 AI 识别：原文 %d 字 / %d 行，切分为 %d 段", len(clean), total_lines, len(segments))

    segment_results: list[dict] = []
    for position, segment in enumerate(segments, start=1):
        obj, _raw = await collector(
            build_extract_messages(segment, position, len(segments)),
            _validate_has_sections,
            json_mode=True,
            temperature=0.2,
            scene="bid_section_extract",
        )
        segment_results.append(normalize_sections_response(obj, total_lines))

    estimated_calls = len(segments)
    if len(segments) == 1:
        merged_obj = segment_results[0]
    else:
        merged_obj, _raw = await collector(
            build_merge_messages(segment_results),
            _validate_has_sections,
            json_mode=True,
            temperature=0.2,
            scene="bid_section_merge",
        )
        estimated_calls += 1

    merged = normalize_sections_response(merged_obj, total_lines)
    validate_sections_response(merged)
    return {
        "sections": merged["sections"],
        "segment_count": len(segments),
        "estimated_calls": estimated_calls,
    }
