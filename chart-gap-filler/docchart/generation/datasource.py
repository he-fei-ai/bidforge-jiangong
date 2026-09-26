# -*- coding: utf-8 -*-
"""数据源定位与抽取：表格 -> ChartData；正文数量关系 -> ChartData。

对应增强链路：图表意图识别 -> 数据源定位 -> 数据提取/清洗/推断。
"""

from __future__ import annotations

import re

from ..models import TODO_RE, Block, BlockKind, ChartData, Document, Series
from ..detection.rules import numeric_cell

# 正文中的"标签+数值+单位"对，如：地上建筑面积159173平方米
_LABEL_NUM_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,14}?)\s*(?:为|达到|约|计)?\s*"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(平方米|㎡|m2|m²|万m²|万元|元|米|m|mm|cm|天|层|人|台|个|根|吨|%|kN|MPa|N·m)"
)
# 百分比构成："住宅占62%、配套占18%"
_PCT_ITEM_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{2,10}?)[占计达]?\s*(\d{1,3}(?:\.\d+)?)\s*%")


def extract_from_table(table: Block) -> ChartData | None:
    """把表格转成 ChartData：首列为分类轴，数值占比高的列成系列。"""
    rows = table.rows
    if len(rows) < 2:
        return None
    header = rows[0]
    data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    if not data_rows:
        return None
    n_cols = max(len(r) for r in rows)
    categories = [r[0] if r else "" for r in data_rows]
    series_list: list[Series] = []
    for c in range(1, n_cols):
        col_name = header[c] if c < len(header) and header[c].strip() else f"列{c}"
        vals: list[float] = []
        ok = 0
        for r in data_rows:
            cell = r[c] if c < len(r) else ""
            v = numeric_cell(cell)
            if v is None:
                vals.append(0.0)
            else:
                vals.append(v)
                ok += 1
        # 列至少 6 成可解析才纳入系列
        if ok >= max(2, int(len(data_rows) * 0.6)):
            series_list.append(Series(name=col_name.strip(), values=vals))
    if not series_list:
        return None
    has_todo = any(TODO_RE.search(c) for r in data_rows for c in r if c.strip())
    return ChartData(
        title=(categories and series_list and
               (header[0] if header and header[0].strip() else "")) or "",
        categories=categories, series=series_list,
        x_label=header[0].strip() if header and header[0].strip() else "",
        source_block_id=table.id, complete=not has_todo,
    )


def extract_from_paragraph(para: Block) -> ChartData | None:
    """从正文抽取"标签-数值"对（如面积构成、配比清单）。"""
    text = para.text or ""
    items: list[tuple[str, float]] = []
    # 优先百分比构成
    pcts = _PCT_ITEM_RE.findall(text)
    if len(pcts) >= 2:
        items = [(name.strip(), float(v)) for name, v in pcts]
    else:
        for name, num, unit in _LABEL_NUM_RE.findall(text):
            try:
                v = float(num.replace(",", ""))
            except ValueError:
                continue
            # 过滤明显无意义匹配（纯编号类：层号、日期）
            if re.search(r"[\u4e00-\u9fff]", name) and unit not in ("层", "天"):
                items.append((name.strip()[-10:], v))
    # 去重保序
    seen: set[str] = set()
    uniq = []
    for k, v in items:
        if k not in seen:
            seen.add(k)
            uniq.append((k, v))
    if len(uniq) < 2:
        return None
    return ChartData(
        categories=[k for k, _ in uniq],
        series=[Series(name="数值", values=[v for _, v in uniq])],
        source_block_id=para.id, complete=True,
    )


def locate_data(doc: Document, gap_anchor: int) -> ChartData | None:
    """数据源定位：优先锚点处表格，其次向前 3 块找表格/段落数值。"""
    block = doc.blocks[gap_anchor] if 0 <= gap_anchor < len(doc.blocks) else None
    if block is not None and block.kind == BlockKind.TABLE:
        data = extract_from_table(block)
        if data:
            return data
    lo = max(0, gap_anchor - 2)
    for b in doc.blocks[lo: gap_anchor + 3]:
        if b.kind == BlockKind.TABLE:
            data = extract_from_table(b)
            if data:
                return data
        elif b.kind == BlockKind.PARAGRAPH:
            data = extract_from_paragraph(b)
            if data:
                return data
    return None
