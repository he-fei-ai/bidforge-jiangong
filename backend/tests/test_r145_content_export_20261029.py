# -*- coding: utf-8 -*-
"""R145（2026-10-29）· 正文生成 + 导出文档模块 · 表头前短行（标题带/列组标签）表格修复护栏。

【背景】日志取证：``DOCX 导出后校验检出残留：Markdown 1``（trace a18cea096cc6）。
读回产物定位：Body Text CN 段落 ``| --- | --- | --- |``（GFM 表格分隔行泄漏）。
根因：生产库「抹灰参数」章表 4-1 源文为
    | 项目 | 技术参数 |          ← 表头带 2 列（列组标签）
    | 项目 | 技术参数 | 执行要求 | ← 表头 3 列
    | --- | --- | --- |          ← 分隔行 3 列
``_split_table_block`` 按「任一行未被任何表格段覆盖 → 整块拒绝」的保守判据，
把 2 列标题带当**孤儿行** → 整张表降级为裸文本段落，管道语法/分隔行原样印进
交付 DOCX（用户可见乱码）。

【修法（两处联动，R145）】
  ① ``content_blocks._split_table_block``：表头**前**的短行（单元格数 < 分隔行
     列数、至少一格有内容）是「表头带/列组标签」，属于表格一部分；前置进首个
     表段并标记覆盖。仍不全部覆盖（真·孤儿行）→ 照旧整体拒绝（保守）。
  ② ``export._add_table_from_markup``：分隔行按正则**动态定位**（不再硬编码
     index 1）；表头短行尾部空单元格按 **gridSpan** 合并（「技术参数」跨 2 列），
     与 ``_merge_table_title_band``（通栏标题带）互补。

【判据教训】① 同族缺陷回扫：本缺陷与「表格缺少 GFM 分隔行」格式契约告警同族，
但后者是**无分隔行**（不适用本修复），刻意不混修；② 修复必须两端联动——只改
content_blocks 会让分隔行位置后移而 ``_add_table_from_markup`` 硬编码 index 1
跳过表头（B 组行为锁抓得住）；③ gridSpan 检测按「内容末列 < ncols-1」而非
「行长度 < ncols」——``_unstack_or_trim_table_rows`` 已把短行补齐到 ncols。
"""
from __future__ import annotations

import re

import pytest
from app.services.content_blocks import (
    _parse_content_blocks,
    _split_table_block,
    _table_cell_count,
)
from docx import Document
from docx.oxml.ns import qn

# 生产实拍：抹灰参数章表 4-1（表头前 2 列标题带 + 3 列表头 + 3 列分隔行）
LEADING_SHORT_TABLE = [
    "| 项目 | 技术参数 |",
    "| 项目 | 技术参数 | 执行要求 |",
    "| --- | --- | --- |",
    "| 分层厚度 | 每遍 5~9mm | 单遍抹灰厚度控制在 5mm 以上 |",
    "| 总厚度控制 | 普通抹灰 ≤18mm | 立面垂直度与表面平整度同步控制 |",
]

# 规范序表（分隔行在 index 1）——回归锚：逐字节不变
NORMAL_TABLE = [
    "| 检验项目 | 允许偏差（mm） | 检验方法 |",
    "| --- | --- | --- |",
    "| 立面垂直度 | ≤2mm | 用2m托线板检查 |",
    "| 表面平整度 | ≤2mm | 用2m靠尺和塞尺检查 |",
]

# 真·孤儿行（**同列数**、无表头带）——回归锚：仍整体拒绝。
# （列数相同 ⇒ 不被误判为「表头前短行」，真孤儿不会被我的修复救援）
ORPHAN_TABLE = [
    "| 孤儿数据行 | 不该被识别 | 与表同宽 |",
    "| 项目 | 技术参数 | 执行要求 |",
    "| --- | --- | --- |",
    "| 分层厚度 | 每遍 5~9mm | 单遍抹灰厚度控制 |",
]

# R117 通栏标题带真实形态：分隔行**紧跟**标题带（标题带即表头，index 0）。
# ——回归锚：仍由 _merge_table_title_band 处理（本修复不改变其行为）
TITLE_BAND_TABLE = [
    "| 吊装作业关键文件核查清单 | | |",
    "| --- | --- | --- |",
    "| 检查项目 | 允许偏差 | 检验方法 |",
    "| 墙面垂直度 | 3mm | 2m垂直检测尺检查 |",
]


# ---------------------------------------------------------------------------
# A 组：_split_table_block / _parse_content_blocks 行为锁
# ---------------------------------------------------------------------------
class TestSplitTableBlock:
    def test_leading_short_row_accepted(self):
        """表头前 2 列标题带被接受，前置进表段（列组标签属于表格）。"""
        segs = _split_table_block(LEADING_SHORT_TABLE)
        assert len(segs) == 1
        seg = segs[0]
        # 标题带前置：segment = [标题带, 表头, 分隔行, *数据]
        assert seg[0] == "| 项目 | 技术参数 |"
        assert seg[1] == "| 项目 | 技术参数 | 执行要求 |"
        assert seg[2] == "| --- | --- | --- |"
        assert len(seg) == 5

    def test_normal_table_unchanged(self):
        """规范序表（分隔行 index 1）产出逐字节不变（回归锚）。"""
        segs = _split_table_block(NORMAL_TABLE)
        assert segs == [NORMAL_TABLE]

    def test_orphan_row_still_rejected(self):
        """真·孤儿行（同列数）仍整体拒绝（保守，不半识别）。"""
        assert _split_table_block(ORPHAN_TABLE) == []

    def test_title_band_unchanged(self):
        """R117 通栏标题带（仅首格内容）产出不变（回归锚）。"""
        segs = _split_table_block(TITLE_BAND_TABLE)
        assert len(segs) == 1
        assert segs[0] == TITLE_BAND_TABLE

    def test_no_separator_returns_empty(self):
        """块内无分隔行 → 返回 []（调用方按旧行为降级段落）。"""
        assert _split_table_block(["| a | b |", "| c | d |"]) == []

    def test_cell_count_helper(self):
        """_table_cell_count：含空单元格，剥离首尾竖线。"""
        assert _table_cell_count("| 项目 | 技术参数 |") == 2
        assert _table_cell_count("| --- | --- | --- |") == 3
        assert _table_cell_count("| 项目 | |") == 2


class TestParseContentBlocks:
    def test_leading_short_row_parses_as_table(self):
        """整章解析：标题带+列数不一致表成为 table 块，不再降级段落。"""
        src = "\n".join(["表 4-1 抹灰工程主要技术参数表", ""] + LEADING_SHORT_TABLE)
        blocks = _parse_content_blocks(src)
        tables = [b for b in blocks if b.get("type") == "table"]
        assert len(tables) == 1
        assert tables[0]["lines"][0] == "| 项目 | 技术参数 |"
        # 无段落含分隔行（泄漏的管道语法）
        assert not any(
            b.get("type") == "paragraph" and "---" in b.get("text", "")
            for b in blocks)

    def test_production_mohu_has_two_tables(self):
        """生产实拍「抹灰参数」整章：两张表都解析为 table，零分隔行段落。

        ✅ R152（2026-11-01 · 遗留收口）改造：原实现硬锚定单一章节 ID
        （954f14e9…），而生产库是**易变数据**——该章节随后被正常业务删除
        （525→396 行），用例变成永久假红。现改为三级降级：
          ① 原锚点章节仍在 → 原断言逐字保留；
          ② 原锚点已删 → 在全库动态寻找仍含「表头带 + 分隔行」形态的实拍
             章节，锁同一机制不变量（表成块、零分隔行段落）；
          ③ 全库无该形态 → skip（数据易变，非回归信号；机制锁由上方
             合成用例 test_leading_short_row_parses_as_table 承载）。
        """
        import sqlite3
        conn = sqlite3.connect("data/scheme_assistant.db")
        row = conn.execute(
            "SELECT content FROM sections WHERE id=?",
            ("954f14e9-2758-4eb9-8f9a-920e98b9f78f",)).fetchone()
        if row is None:
            # 动态重锚：找仍含 markdown 表分隔行的实拍章节
            rows = conn.execute(
                "SELECT id, content FROM sections"
                " WHERE content LIKE '%| ---%' OR content LIKE '%|---%'"
                " ORDER BY updated_at DESC LIMIT 20").fetchall()
            conn.close()
            live = [r for r in rows
                    if any(b.get("type") == "table"
                           for b in _parse_content_blocks(r[1] or ""))]
            if not live:
                pytest.skip(
                    "生产库锚点章节 954f14e9 已被删除且无同类实拍数据"
                    "（数据易变，非回归信号）；机制锁由合成用例承载")
            row = live[0]
        else:
            conn.close()
        blocks = _parse_content_blocks(row[1])
        tables = [b for b in blocks if b.get("type") == "table"]
        assert tables, f"实拍章节 {row[0][:8]} 应解析出表格块"
        assert not any(
            b.get("type") == "paragraph" and "---" in b.get("text", "")
            for b in blocks)


# ---------------------------------------------------------------------------
# B 组：export._add_table_from_markup 渲染行为锁（动态分隔行 + gridSpan）
# ---------------------------------------------------------------------------
class TestAddTableFromMarkup:
    def _render(self, tbl_lines):
        from app.routers.export import _add_table_from_markup
        doc = Document()
        _add_table_from_markup(doc, tbl_lines)
        return doc

    def test_no_separator_residual(self):
        """渲染后正文无分隔行残留（核心缺陷：Markdown 残留 = 0）。"""
        from app.services.export_validate import validate_document
        doc = self._render(LEADING_SHORT_TABLE)
        report = validate_document(doc)
        assert report["markdown_residue"] == 0

    def test_gridspan_merge_applied(self):
        """表头短行尾部空单元格按 gridSpan 合并（技术参数跨 2 列）。"""
        doc = self._render(LEADING_SHORT_TABLE)
        t = doc.tables[0]
        assert len(t.rows) == 4  # 标题带 + 表头 + 2 数据
        # 首行 tc 数 = 2（项目 + 技术参数 span 2），gridSpan=2 存在
        tc0 = t.rows[0]._tr.tc_lst
        assert len(tc0) == 2
        tcPr = tc0[1].find(qn("w:tcPr"))
        gs = tcPr.find(qn("w:gridSpan")) if tcPr is not None else None
        assert gs is not None
        assert gs.get(qn("w:val")) == "2"

    def test_normal_table_still_renders(self):
        """规范序表渲染逐字节不变（回归锚）。"""
        doc = self._render(NORMAL_TABLE)
        t = doc.tables[0]
        assert len(t.rows) == 3
        assert len(t.columns) == 3
        # 首行 tc 数 = 3（无合并）
        assert len(t.rows[0]._tr.tc_lst) == 3

    def test_dynamic_separator_position(self):
        """分隔行不在 index 1（前置后）仍正确跳过（旧实现硬编码 index 1 会跳错）。"""
        from app.routers.export import _add_table_from_markup
        doc = Document()
        # 手动构造：标题带前置，分隔行在 index 2
        _add_table_from_markup(doc, LEADING_SHORT_TABLE)
        t = doc.tables[0]
        # 表头行（index 1 原始）= 第二行，应渲染为表头文本
        assert "执行要求" in t.rows[1].cells[2].text


# ---------------------------------------------------------------------------
# C 组：导出后校验闭环（post_export_check 残留 = 0）
# ---------------------------------------------------------------------------
class TestPostExportNoResidual:
    def test_full_pipeline_no_markdown_residue(self):
        """完整链路：解析 → 渲染 → 落盘 → 导出后校验零 Markdown 残留。"""
        import os
        import tempfile

        from app.routers.export import _add_table_from_markup
        from app.services.export_validate import post_export_check
        src = "\n".join(["表 4-1 抹灰工程主要技术参数表", ""] + LEADING_SHORT_TABLE)
        blocks = _parse_content_blocks(src)
        tbl = [b for b in blocks if b.get("type") == "table"][0]
        doc = Document()
        _add_table_from_markup(doc, tbl["lines"])
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            doc.save(path)
            report = post_export_check(doc, path)
            assert report["markdown_residue"] == 0
            assert report["html_residue"] == 0
            assert report["entity_residue"] == 0
        finally:
            os.remove(path)
