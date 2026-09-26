"""招标文件多标段 AI 识别（移植自 OpenBidKit）。

参考实现：`client/electron/services/bidSectionExtractionTask.cjs`
  - `numberMarkdownLines()`       ：给正文逐行加 `L000001 | ` 前缀，让模型能回填行号
  - `splitUserTextByContextLimit()`：按上下文上限分段（本软件复用
    `bid_analysis_service.split_for_analysis`，避免第二套切分实现）
  - 分段提取 → 候选合并 → 去重/归一 → **必须 ≥2 个有效标段**才视为成功
  - `dedupeSections()`            ：同标段跨段合并 includeRanges/evidence，
    按首个行号排序并重编号 section-1..N

与本软件既有 `bid_section_detector` 的关系：
  - detector 是**纯规则**快速判断（秒级、零成本、只回答「是否疑似多标段」）；
  - 本模块是**AI 结构化识别**（有成本、给出可选择的标段清单 + 行号区间 + 依据），
    两者互补：先规则提示，用户在需要精确选择投标范围时再跑 AI 识别。

⚠️ 只在本端点被显式调用时才产生 AI 调用（默认零成本、零行为变化）。
"""
from __future__ import annotations

import logging
import re

from app.services.bid_analysis_service import split_for_analysis

logger = logging.getLogger(__name__)

#: 行号前缀宽度（与参考实现一致：L000001 | 原文）
_LINE_PREFIX_WIDTH = 6

#: 「第X标段」归一化正则（与参考实现 normalizeSectionTitle 等价）
_SECTION_TITLE_PREFIX_RE = re.compile(
    r"^第([一二三四五六七八九十壹贰叁肆伍\d]+)(标段|标包|分包|包)$")


def number_markdown_lines(markdown: str) -> str:
    """给正文逐行加行号前缀（`L000001 | 原文`），供模型回填 include_ranges。"""
    lines = str(markdown or "").split("\n")
    return "\n".join(
        f"L{str(i + 1).zfill(_LINE_PREFIX_WIDTH)} | {line}"
        for i, line in enumerate(lines))


def normalize_section_title(value: str) -> str:
    """标段标题归一化（用于合并同一标段在不同分段出现的候选）。"""
    text = re.sub(r"\s+", "", str(value or "")).strip()
    return _SECTION_TITLE_PREFIX_RE.sub(r"\1\2", text).lower()


def get_section_merge_key(section: dict) -> str:
    """合并键：优先标题，无标题时退回序号（与参考实现 getSectionMergeKey 一致）。"""
    title_key = normalize_section_title(section.get("title"))
    unit = str(section.get("unit") or "标段")
    if title_key:
        return f"{unit}:{title_key}"
    return f"{unit}:{section.get('index')}"


def normalize_line_range(raw, total_lines: int) -> dict | None:
    """校验并归一化单个行号区间；非法（越界/倒序/非数字）返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        start = int(float(raw.get("startLine", raw.get("start_line", 0)) or 0))
        end = int(float(raw.get("endLine", raw.get("end_line", 0)) or 0))
    except (TypeError, ValueError):
        return None
    if start < 1 or end < start or start > total_lines or end > total_lines:
        return None
    out = {"start_line": start, "end_line": end}
    reason = str(raw.get("reason") or "").strip()
    if reason:
        out["reason"] = reason
    return out


def merge_ranges(ranges: list) -> list[dict]:
    """区间去重（按 start-end-reason 去重）并按 startLine 排序。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in ranges or []:
        if not isinstance(r, dict):
            continue
        key = (r.get("start_line"), r.get("end_line"), r.get("reason", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    out.sort(key=lambda x: (x.get("start_line", 0), x.get("end_line", 0)))
    return out


def _first_range_start(section: dict) -> int:
    ranges = merge_ranges(section.get("include_ranges") or [])
    return ranges[0]["start_line"] if ranges else 2 ** 31


def normalize_section(raw, index: int, total_lines: int) -> dict | None:
    """归一化单个标段候选（无标题 → 丢弃，与参考实现一致）。"""
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
    include_ranges = [r for r in (
        normalize_line_range(x, total_lines) for x in raw_ranges) if r]
    evidence = [str(e).strip() for e in (raw.get("evidence") or [])
                if str(e).strip()]
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


def dedupe_sections(sections: list[dict]) -> list[dict]:
    """合并同一标段的多次出现，丢弃无有效行号区间的候选，重编号 section-1..N。"""
    merged: dict[str, dict] = {}
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        key = get_section_merge_key(sec)
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(sec)
            continue
        cur["include_ranges"] = merge_ranges(
            [*(cur.get("include_ranges") or []), *(sec.get("include_ranges") or [])])
        cur["evidence"] = list(dict.fromkeys(
            [*(cur.get("evidence") or []), *(sec.get("evidence") or [])]))
        if not cur.get("head_line") and sec.get("head_line"):
            cur["head_line"] = sec["head_line"]
        if not cur.get("description") and sec.get("description"):
            cur["description"] = sec["description"]

    out: list[dict] = []
    for sec in merged.values():
        sec["include_ranges"] = merge_ranges(sec.get("include_ranges") or [])
        if not sec["include_ranges"]:
            # 与参考实现一致：拿不出真实行号范围的候选不算有效标段
            continue
        out.append(sec)
    out.sort(key=lambda s: (_first_range_start(s), s.get("index") or 0))
    for i, sec in enumerate(out):
        sec["id"] = f"section-{i + 1}"
        sec["index"] = i + 1
    return out


def normalize_sections_response(value, total_lines: int) -> dict:
    """把模型返回的 `{"sections": [...]}` 归一化为去重后的候选集合。"""
    source = value.get("sections") if isinstance(value, dict) else None
    normalized = []
    for i, raw in enumerate(source if isinstance(source, list) else []):
        sec = normalize_section(raw, i, total_lines)
        if sec:
            normalized.append(sec)
    return {"sections": dedupe_sections(normalized)}


def validate_sections_response(value: dict) -> None:
    """有效性校验：至少 2 个标段，否则抛 ValueError（由路由转成 422）。"""
    sections = value.get("sections") if isinstance(value, dict) else None
    if not isinstance(sections, list) or len(sections) < 2:
        raise ValueError("未识别到至少两个有效标段")


# ---------------------------------------------------------------------------
# 提示词（与参考实现同语义，中文口径一致）
# ---------------------------------------------------------------------------

_SYSTEM_EXTRACT = (
    "你是严谨的招标文件多标段识别专家。"
    "你只能基于用户提供的带行号文本识别标段、标包、分包、采购包、包件或标的。"
)


def build_extract_messages(segment: str, segment_index: int,
                           total_segments: int) -> list[dict]:
    """分段提取提示词（带行号文本 → 结构化标段候选）。"""
    return [
        {"role": "system", "content": _SYSTEM_EXTRACT},
        {"role": "user", "content": f"""当前是招标文件第 {segment_index}/{total_segments} 段。每行格式为“L000001 | 原文”。

任务：识别本段中明确属于某个标段/标包/分包/采购包/包件/标的的内容，并返回结构化 JSON。

要求：
1. 只识别明确属于某个标段的内容范围。
2. 通用条款不要归入某个标段；不确定归属的内容不要输出范围。
3. include_ranges 必须使用输入中的真实行号，start_line 和 end_line 都是不带 L 前缀的数字。
4. 不要编造标段，不要补写原文没有的范围。
5. 无法提供有效 include_ranges 的候选不要输出到 sections。
6. 如果本段没有明确标段内容，返回 {{"sections":[]}}。
7. 只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。

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
{segment}"""},
    ]


_SYSTEM_MERGE = ("你是严谨的招标文件多标段识别结果合并专家。"
                 "你只能合并用户提供的分段识别结果，不得编造新标段或新行号。")


def build_merge_messages(segment_results: list[dict]) -> list[dict]:
    """候选合并提示词（多分段结果 → 单一标段清单）。"""
    import json as _json
    return [
        {"role": "system", "content": _SYSTEM_MERGE},
        {"role": "user", "content": f"""以下是同一份招标文件各分段识别出的标段候选。请合并重复标段，保留所有明确属于各标段的 include_ranges 和 evidence。

要求：
1. 同一标段跨多个分段出现时合并为一个 sections 项。
2. 不要把通用条款合并到任何标段。
3. 不要新增分段结果中没有的行号范围。
4. 如果最终少于两个标段，原样返回已有结果。
5. 只返回 JSON，不要输出 Markdown、代码块、解释或额外文字。

分段结果：
{_json.dumps(segment_results, ensure_ascii=False, indent=2)}"""},
    ]


def _validate_has_sections(obj) -> list[str]:
    """collect_json_response 的校验函数：必须含 sections 且为数组。"""
    if not isinstance(obj, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(obj.get("sections"), (list, tuple)):
        return ["缺少 sections 数组"]
    return []


async def extract_bid_sections(ai_collect=None, markdown: str = "",
                               chunk_size: int | None = None,
                               chunk_overlap: int | None = None) -> dict:
    """AI 识别招标文件标段清单（分段提取 → 合并 → 去重）。

    Args:
        ai_collect: 异步 JSON 收集器，签名兼容
            `app.services.ai.json_response.collect_json_response`
            （`await ai_collect(messages, validate_fn, json_mode=..., scene=...)`
            返回 `(obj, raw)`）。注入以便单测。
        markdown: 招标文件全文（或项目资料的合并文本）。
        chunk_size: 分段字符数（None → 复用 `split_for_analysis` 默认值）。
        chunk_overlap: 分段重叠字符数（None → 复用默认值）。

    Returns:
        {"sections": [...], "segment_count": int, "estimated_calls": int}

    Raises:
        ValueError: 未识别到至少两个有效标段（由调用方转 422）。
    """
    from app.services.ai.json_response import collect_json_response

    collector = ai_collect or collect_json_response
    clean = str(markdown or "").strip()
    if not clean:
        raise ValueError("没有可识别的招标文件正文，请先上传并解析文件")

    total_lines = clean.count("\n") + 1
    numbered = number_markdown_lines(clean)
    split_kwargs: dict = {}
    if chunk_size:
        split_kwargs["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        split_kwargs["overlap"] = chunk_overlap
    segments = split_for_analysis(numbered, **split_kwargs)
    segments = segments or [numbered]

    logger.info("多标段 AI 识别：原文 %d 字 / %d 行，切分为 %d 段",
                len(clean), total_lines, len(segments))

    segment_results: list[dict] = []
    for index, segment in enumerate(segments):
        obj, _raw = await collector(
            build_extract_messages(segment, index + 1, len(segments)),
            _validate_has_sections,
            json_mode=True,
            temperature=0.2,
            scene="bid_section_extract",
        )
        segment_results.append(normalize_sections_response(obj, total_lines))

    estimated_calls = len(segments)
    if len(segments) > 1:
        merged_obj, _raw = await collector(
            build_merge_messages(segment_results),
            _validate_has_sections,
            json_mode=True,
            temperature=0.2,
            scene="bid_section_merge",
        )
        estimated_calls += 1
    else:
        merged_obj = segment_results[0]

    merged = normalize_sections_response(merged_obj, total_lines)
    validate_sections_response(merged)
    return {
        "sections": merged["sections"],
        "segment_count": len(segments),
        "estimated_calls": estimated_calls,
    }
