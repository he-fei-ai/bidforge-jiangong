# -*- coding: utf-8 -*-
"""HTML 解析器：基于标准库 HTMLParser 的状态机。

为保留原始格式，解析时为每个块记录其在原始 HTML 字符串中的
字符区间 [start, end)，导出时按区间回插，不重排原文。
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from ..models import Block, BlockKind, Document, figure_caption_of

_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "table",
               "figure", "ul", "ol", "pre", "blockquote", "img"}


class _DocHTMLParser(HTMLParser):
    def __init__(self, raw: str):
        super().__init__(convert_charrefs=True)
        self.raw = raw
        # 行首偏移表，用于 getpos -> 绝对偏移
        starts, acc = [0], 0
        for ln in raw.splitlines(keepends=True):
            acc += len(ln)
            starts.append(acc)
        self._line_starts = starts
        self.blocks: list[Block] = []
        self._stack: list[tuple[str, int]] = []   # (tag, abs_offset)
        self._buf: list[str] = []                 # 当前块文本缓冲
        self._table_rows: list[list[str]] = []
        self._cur_row: list[str] | None = None
        self._in_cell = False
        self._section: list[str] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _block_end(self) -> int:
        """当前解析位置若处于结束标签开头，后延到标签尾，保证区间完整。"""
        pos = self._offset()
        if self.raw.startswith("</", pos):
            gt = self.raw.find(">", pos)
            if gt != -1:
                return gt + 1
        return pos

    def _flush(self, tag: str, start: int) -> None:
        """块结束：生成 Block。"""
        text = " ".join("".join(self._buf).split())
        kind = BlockKind.PARAGRAPH
        level = 0
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            kind = BlockKind.HEADING
            level = int(tag[1])
        elif tag == "table":
            kind = BlockKind.TABLE
            text = " | ".join(self._table_rows[0]) if self._table_rows else text
        elif tag in ("ul", "ol"):
            kind = BlockKind.LIST
        elif tag == "pre" or tag == "blockquote":
            kind = BlockKind.CODE

        # 图注判定：文本形如"图 x-y"且块内不含图片标签
        end = self._block_end()
        if kind in (BlockKind.PARAGRAPH, BlockKind.CODE) and figure_caption_of(text) \
                and "<img" not in self.raw[start:end].lower():
            kind = BlockKind.CAPTION

        if kind != BlockKind.HEADING and not text and kind not in (BlockKind.IMAGE,):
            return  # 空块忽略
        if kind == BlockKind.HEADING:
            self._section = self._section[: level - 1] + [text]

        self.blocks.append(Block(
            id=len(self.blocks), kind=kind, text=text, level=level,
            rows=[r[:] for r in self._table_rows],
            section=" > ".join(self._section),
            src={"html_start": start, "html_end": end, "tag": tag},
        ))
        self._table_rows = []
        self._cur_row = None

    # ---- HTMLParser 回调 ----

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "img":
            start = self._offset()
            end = self.raw.find(">", start) + 1
            src = dict(attrs).get("src", "")
            alt = dict(attrs).get("alt", "")
            self.blocks.append(Block(
                id=len(self.blocks), kind=BlockKind.IMAGE,
                text=f"[图片] {alt or src}", image_ref=src,
                section=" > ".join(self._section),
                src={"html_start": start, "html_end": end, "tag": "img"},
            ))
            return
        if tag in _BLOCK_TAGS:
            self._stack.append((tag, self._offset()))
            self._buf = []
            self._table_rows = []
        if tag == "tr":
            self._cur_row = []
        if tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            cell = " ".join("".join(getattr(self, "_cell_buf", [])).split())
            if self._cur_row is not None:
                self._cur_row.append(cell)
            return
        if tag == "tr" and self._cur_row is not None:
            if any(c for c in self._cur_row):
                self._table_rows.append(self._cur_row)
            self._cur_row = None
            return
        if tag in _BLOCK_TAGS and self._stack:
            # 弹到匹配的开始标签
            while self._stack and self._stack[-1][0] != tag:
                inner_tag, inner_start = self._stack.pop()
                self._flush(inner_tag, inner_start)
            if self._stack:
                _, start = self._stack.pop()
                # 图注优先：figure/figcaption 文本
                self._flush(tag, start)

    def handle_data(self, data):
        if self._in_cell:
            self._cell_buf.append(data)
        self._buf.append(data)


def parse_html(path: str | Path) -> Document:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    parser = _DocHTMLParser(raw)
    parser.feed(raw)
    # 收尾未闭合的块
    while parser._stack:
        tag, start = parser._stack.pop()
        parser._flush(tag, start)
    return Document(source_path=str(p), fmt="html", blocks=parser.blocks, raw=raw)
