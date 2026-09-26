# -*- coding: utf-8 -*-
"""Markdown 解析器：逐行扫描，产出带行号的结构块（行号用于导出回插）。"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, BlockKind, Document, figure_caption_of

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、])\s+")
_IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_CODE_FENCE = re.compile(r"^\s*(```|~~~)")


def _split_table_row(line: str) -> list[str]:
    """拆分 Markdown 表格行为单元格列表。"""
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_table_sep(line: str) -> bool:
    """表格分隔行：| --- | :--: |。"""
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "---") for c in cells)


def parse_markdown(path: str | Path) -> Document:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    lines = raw.splitlines()
    blocks: list[Block] = []
    section_stack: list[tuple[int, str]] = []  # (level, text)

    def _section() -> str:
        return " > ".join(t for _, t in section_stack)

    def _push(block: Block) -> None:
        block.section = _section()
        blocks.append(block)
        block_id_seq[0] += 1

    block_id_seq = [-1]
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 代码块：整体跳过并登记
        m = _CODE_FENCE.match(line)
        if m:
            j = i + 1
            while j < n and not _CODE_FENCE.match(lines[j]):
                j += 1
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.CODE,
                        text="\n".join(lines[i + 1:j]),
                        src={"line_start": i, "line_end": min(j, n - 1)}))
            i = j + 1
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, text))
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.HEADING,
                        text=text, level=level,
                        src={"line_start": i, "line_end": i}))
            i += 1
            continue

        # 表格块（连续 | 行，第二行为分隔行）
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            rows = [_split_table_row(lines[i])]
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(_split_table_row(lines[j]))
                j += 1
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.TABLE,
                        text=" | ".join(rows[0]), rows=rows,
                        src={"line_start": i, "line_end": j - 1}))
            i = j
            continue

        # 独立图片行
        m = _IMAGE_ONLY_RE.match(line)
        if m:
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.IMAGE,
                        text=f"[图片] {m.group(1)}", image_ref=m.group(1),
                        src={"line_start": i, "line_end": i}))
            i += 1
            continue

        # 图注行（"图 x-y 标题"且非标题）
        cap = figure_caption_of(stripped)
        if cap and _IMAGE_RE.search(line) is None:
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.CAPTION,
                        text=stripped, src={"line_start": i, "line_end": i}))
            i += 1
            continue

        # 列表
        if _LIST_RE.match(line):
            j = i
            items = []
            while j < n and _LIST_RE.match(lines[j]):
                items.append(lines[j].strip())
                j += 1
            _push(Block(id=block_id_seq[0] + 1, kind=BlockKind.LIST,
                        text="\n".join(items),
                        src={"line_start": i, "line_end": j - 1}))
            i = j
            continue

        # 普通段落（合并连续非空、非其他结构的行）
        j = i
        parts = []
        while j < n and lines[j].strip() and not _HEADING_RE.match(lines[j]) \
                and not _CODE_FENCE.match(lines[j]) \
                and not lines[j].strip().startswith("|") \
                and not _LIST_RE.match(lines[j]):
            parts.append(lines[j].strip())
            j += 1
        text = "".join(parts)
        # 段落内嵌图片也视为图片存在
        if _IMAGE_ONLY_RE.match(text):
            kind = BlockKind.IMAGE
            ref = _IMAGE_RE.search(text).group(2)
        else:
            kind = BlockKind.PARAGRAPH
            ref = ""
        _push(Block(id=block_id_seq[0] + 1, kind=kind, text=text,
                    image_ref=ref, src={"line_start": i, "line_end": j - 1}))
        i = j

    return Document(source_path=str(p), fmt="markdown", blocks=blocks, raw=raw)
