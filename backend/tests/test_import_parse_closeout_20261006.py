"""解析提取模块第二轮收口护栏（2026-10-06）。

第一轮（2026-10-05，test_import_parse_closeout_20261005.py /
test_import_parse_r13_closeout_20261005.py）收口了上传配额文案漂移、文件数预检、
R13 读写判空等缺陷。本轮在解析层结构化抽取中确认并修复 3 项净新增问题，
每个用例均为「修复前必失败」的反向用例：

  D5 单列 GFM 表格整体漏抽——``_SEP_ROW_RE`` 旧正则的列分组量词为 ``+``、
     每列短横线要求 ``-{2,}``，合法单列分隔行 ``| --- |`` 及单横线 ``|:-:|``
     不匹配，表格不进 tables、【表格】锚点同样失效；页文本仍保留
     （内容未丢，但结构化/溯源断链）。
  D6 表格标题被标记行污染——``<!-- page:2 -->``、``[IMAGE: x, page:1]``
     及「图片标记+文字」混排行原样写入 title 字段。
  E1 块级公式只能抽单行——跨行 ``$$...$$`` 全部漏抽，一行多公式只取首个。
  E2 /system/activity 缺文档解析成败监控——补 documents 统计段
     （加法式字段，旧消费方不受影响）。
"""
from __future__ import annotations

import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from app.services.doc_pipeline.md_structured import (  # noqa: E402
    _SEP_ROW_RE,
    parse_markdown_structured,
)


# =========================================================================
# D5. 单列 GFM 表格抽取
# =========================================================================
class TestSingleColumnTable:
    def test_separator_single_column_matches(self):
        """修复前：单列分隔行全部不匹配。"""
        assert _SEP_ROW_RE.match("| --- |")
        assert _SEP_ROW_RE.match("|:---:|")
        assert _SEP_ROW_RE.match("| :-- |")
        # GFM 允许单元格内只有一个短横线（旧 -{2,} 漏判）
        assert _SEP_ROW_RE.match("|:-:|")
        assert _SEP_ROW_RE.match("|-|")

    def test_bare_thematic_break_not_matched(self):
        """强制首管道后，裸 ``---`` 主题线/setext 下划线不得被误判为分隔行。"""
        assert not _SEP_ROW_RE.match("---")
        assert not _SEP_ROW_RE.match(" --- ")

    def test_plain_single_column_table_extracted(self):
        """修复前：tables=0，单列表结构化产物丢失。"""
        md = "| 单列 |\n| --- |\n| 值1 |\n| 值2 |"
        out = parse_markdown_structured(md, doc_id="D1")
        assert out["table_count"] == 1
        tbl = out["tables"][0]
        assert tbl["headers"] == ["单列"]
        assert tbl["rows"] == [["值1"], ["值2"]]
        assert tbl["data"] == [["单列"], ["值1"], ["值2"]]
        assert tbl["source_ref"] == "D1#page:1#table:t1"
        # 页归属与页内表索引一致
        assert out["pages"][0]["tables"] == ["t1"]

    def test_anchor_single_column_table_extracted(self):
        """【表格】锚点也救不了单列表（锚点后仍走同一正则）——修复前 table_count=0。"""
        md = "【表格】\n| 单列 |\n|:---:|\n| 值 |"
        out = parse_markdown_structured(md)
        assert out["table_count"] == 1
        assert out["tables"][0]["rows"] == [["值"]]

    def test_multi_column_tables_still_extracted(self):
        """两列/三列常规场景零回归。"""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        out = parse_markdown_structured(md)
        assert out["table_count"] == 1
        assert out["tables"][0]["headers"] == ["A", "B"]

        md3 = "| A | B | C |\n|:--|:-:|--:|\n| 1 | 2 | 3 |"
        assert parse_markdown_structured(md3)["table_count"] == 1


# =========================================================================
# D6. 表格标题标记污染
# =========================================================================
class TestTableTitleCleanup:
    def test_page_marker_not_title(self):
        """修复前 title == '<!-- page:2 -->'。"""
        md = "<!-- page:2 -->\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert parse_markdown_structured(md)["tables"][0]["title"] == ""

    def test_bracket_image_marker_not_title(self):
        """修复前 title == '[IMAGE: x, page:1]'。"""
        md = "[IMAGE: x, page:1]\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert parse_markdown_structured(md)["tables"][0]["title"] == ""

    def test_mixed_image_and_text_keeps_visible_text(self):
        """「图片标记+文字」混排行只保留可见文本（修复前带 ![] 标记）。"""
        md = "![](x.png)工程量汇总表\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert parse_markdown_structured(md)["tables"][0]["title"] == "工程量汇总表"

    def test_earlier_real_candidate_is_kept(self):
        """纯标记行不覆盖此前真实文本候选。"""
        md = ("真实标题\n<!-- page:9 -->\n| A | B |\n"
              "| --- | --- |\n| 1 | 2 |")
        assert parse_markdown_structured(md)["tables"][0]["title"] == "真实标题"

    def test_normal_title_unchanged(self):
        md = "工程规模表\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert parse_markdown_structured(md)["tables"][0]["title"] == "工程规模表"


# =========================================================================
# E1. 跨行 / 多条块级公式
# =========================================================================
class TestBlockFormulaExtraction:
    def test_multiline_formula_extracted(self):
        """修复前：跨行公式 formula_count=0。"""
        md = ("<!-- page:1 -->\n正文\n$$\n\\sigma = \\frac{N}{A}\n$$\n后文")
        out = parse_markdown_structured(md, doc_id="D1")
        assert out["formula_count"] == 1
        f = out["formulas"][0]
        assert f["latex"] == "\\sigma = \\frac{N}{A}"
        assert f["page_num"] == 1
        assert f["source_ref"] == "D1#page:1#formula:f1"
        assert out["pages"][0]["formulas"] == ["f1"]

    def test_multiline_formula_dedup_from_page_text(self):
        """公式原文不再重复进页文本，整块只在起始行留占位。"""
        md = "$$\na+b\n$$"
        text = parse_markdown_structured(md)["pages"][0]["text"]
        assert "$$" not in text
        assert "formulas" in text

    def test_multiple_formulas_on_one_line(self):
        """修复前：一行两公式只抽首个（实际旧实现能抽单行，但此处固化双条行为）。"""
        out = parse_markdown_structured("$$a$$ x $$b$$")
        assert out["formula_count"] == 2
        assert [f["formula_id"] for f in out["formulas"]] == ["f1", "f2"]

    def test_multiline_formula_page_assignment(self):
        """跨页边界的公式按起始行归页。"""
        md = "<!-- page:1 -->\n$$\nx\n$$\n<!-- page:2 -->\n乙"
        out = parse_markdown_structured(md)
        assert out["formulas"][0]["page_num"] == 1

    def test_math_lines_not_table_title(self):
        """联动缺陷：``$$`` 分隔行与公式内部行不得成为后续表格的标题候选。

        预扫公式 span 前，旧扫描会把闭合 ``$$`` 记为 last_plain，表格 title
        被写成 ``$$``。修复后标题保留公式之前的真实正文行。
        """
        md = (
            "材料用量按下列控制。\n"
            "$$\nM = qL^2/8\n$$\n"
            "| 材料 |\n| - |\n| 钢筋 |\n"
        )
        out = parse_markdown_structured(md)
        title = out["tables"][0]["title"]
        assert title != "$$"
        assert "材料用量" in title


# =========================================================================
# E2. /system/activity 文档解析监控
# =========================================================================
async def _patch_read_conn(db_conn, monkeypatch):
    from app.routers import system as sys_mod

    async def fake_read_conn():
        return db_conn

    async def fake_release(_conn):
        return None

    monkeypatch.setattr(sys_mod, "get_read_conn", fake_read_conn)
    monkeypatch.setattr(sys_mod, "release_read_conn", fake_release)


@pytest.mark.asyncio
async def test_activity_documents_empty_state(db_conn, monkeypatch):
    """空库：documents 段零值且 failure_rate=None（不误报 0%）。"""
    await _patch_read_conn(db_conn, monkeypatch)
    from app.routers.system import activity

    data = await activity(limit=5)
    docs = data["documents"]
    assert set(docs.keys()) == {
        "total", "parsed", "failed", "pending", "last_upload_at", "failure_rate"}
    assert docs["total"] == 0
    assert docs["failure_rate"] is None


@pytest.mark.asyncio
async def test_activity_documents_counts(db_conn, monkeypatch):
    """有文档：按 parse_status 计数并派生失败率。"""
    await _patch_read_conn(db_conn, monkeypatch)
    await db_conn.execute(
        "INSERT INTO projects (id, name) VALUES ('p1', '项目1')")
    for doc_id, status in [("d1", "success"), ("d2", "success"),
                           ("d3", "failed"), ("d4", "pending")]:
        await db_conn.execute(
            "INSERT INTO project_documents (id, project_id, file_name, parse_status)"
            " VALUES (?, 'p1', ?, ?)",
            (doc_id, f"{doc_id}.pdf", status))
    await db_conn.commit()

    from app.routers.system import activity
    docs = (await activity(limit=5))["documents"]
    assert docs["total"] == 4
    assert docs["parsed"] == 2
    assert docs["failed"] == 1
    assert docs["pending"] == 1
    assert docs["failure_rate"] == 25.0
    assert docs["last_upload_at"]


@pytest.mark.asyncio
async def test_activity_documents_failsoft(monkeypatch):
    """统计查询抛错时不阻断状态栏，documents 回落零值（fail-soft）。"""
    from app.routers import system as sys_mod

    async def _boom():
        raise RuntimeError("db down")

    async def _noop(_conn):
        return None

    monkeypatch.setattr(sys_mod, "get_read_conn", _boom)
    monkeypatch.setattr(sys_mod, "release_read_conn", _noop)

    data = await sys_mod.activity(limit=5)
    docs = data["documents"]
    assert docs["total"] == 0
    assert docs["failure_rate"] is None
