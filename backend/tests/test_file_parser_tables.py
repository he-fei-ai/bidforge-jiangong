"""解析提取模块增强（2026-09-17）回归测试。

覆盖：
- 文本归一化（normalize_pdf_text：换页符/中文空格/重复绘制压缩）
- 文字层缺失判定（has_informative_text）
- 双引擎表格正文去重（remove_lines_covered_by_tables）
- 表头智能识别（find_table_header / render_table_with_header）+ CSV 端到端
- MinerU 客户端纯函数（make_data_id / extract_markdown_from_zip）与分派守卫
"""
import io
import zipfile

import pytest

from app.services.file_parser import (
    ParseError,
    find_table_header,
    has_informative_text,
    normalize_pdf_text,
    parse_file_content_ex,
    remove_lines_covered_by_tables,
    render_table_with_header,
    _line_covered_by_tables,
    _compact_dedup_probe,
)
import app.services.mineru_client as mineru_client


# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------

def test_normalize_pdf_text_formfeed_and_blank_lines():
    text = "第一段\n\f\n\n\n第二段"
    out = normalize_pdf_text(text)
    assert "\f" not in out
    assert "第一段" in out and "第二段" in out
    assert "\n\n\n" not in out


def test_normalize_pdf_text_collapses_cjk_spaces():
    assert normalize_pdf_text("工 程 概 况") == "工程概况"
    assert normalize_pdf_text("施工 总平面布置 。") .startswith("施工总平面布置")


def test_normalize_pdf_text_collapses_repeated_cjk_chunks():
    # PDF 重复绘制："工程概况工程概况" → "工程概况"
    assert normalize_pdf_text("工程概况工程概况") == "工程概况"
    # 中文短语 + 空白重复
    out = normalize_pdf_text("安全文明施工 安全文明施工 措施")
    assert out.count("安全文明施工") == 1


def test_normalize_pdf_text_keeps_number_sequences():
    # 合法的点分编号/网址/纯数字不得被重复压缩误伤
    assert normalize_pdf_text("1.1.1 一般规定") == "1.1.1 一般规定"
    assert normalize_pdf_text("GB50300-2013") == "GB50300-2013"


# ---------------------------------------------------------------------------
# 文字层缺失判定
# ---------------------------------------------------------------------------

def test_has_informative_text_positive_and_negative():
    assert has_informative_text("隐蔽工程验收记录表格")
    assert has_informative_text("1 2 3 第4页 第5页 建筑工程施工质量验收统一标准GB50300")
    # 只有页码/零散符号 → 无文字层
    assert not has_informative_text("第 1 页\n- 2 -\n3 / 24")
    assert not has_informative_text("")


# ---------------------------------------------------------------------------
# 双引擎表格合并去重
# ---------------------------------------------------------------------------

def test_remove_lines_covered_by_tables_exact_and_chunk():
    table = ["【表格】", "| 设备名称 | 数量 |", "| --- | --- |",
             "| 塔吊 | 2 |", "| 挖掘机 | 3 |"]
    text = ("施工部署说明\n"
            "设备名称 数量\n"
            "塔吊 2\n"
            "挖掘机型号为三一SY215C履带式挖掘机数量三台\n"
            "未被表格覆盖的独立行")
    out = remove_lines_covered_by_tables(text, ["\n".join(table)])
    assert "施工部署说明" in out
    assert "设备名称 数量" not in out
    assert "塔吊 2" not in out
    assert "未被表格覆盖的独立行" in out


def test_line_covered_by_tables_short_lines():
    probe = _compact_dedup_probe("| 塔吊 | 2 |")
    assert _line_covered_by_tables(_compact_dedup_probe("塔吊"), probe)
    # 3~7 字符且非完全包含 → 不判覆盖（防误删）
    assert not _line_covered_by_tables(_compact_dedup_probe("塔式起重机"), probe)


def test_remove_lines_no_tables_noop():
    assert remove_lines_covered_by_tables("原文", []) == "原文"
    assert remove_lines_covered_by_tables("", ["| a | b |"]) == ""


# ---------------------------------------------------------------------------
# 表头智能识别
# ---------------------------------------------------------------------------

def test_find_table_header_simple():
    rows = [["设备", "数量"], ["塔吊", "2"], ["挖掘机", "3"]]
    start, end, header = find_table_header(rows)
    assert (start, end) == (0, 0)
    assert header == ["设备", "数量"]


def test_find_table_header_with_pre_title_rows():
    rows = [
        ["某工程机械设备配置表"],
        ["编制日期：2026-09-17"],
        ["设备名称", "数量", "备注"],
        ["塔吊", "2", ""],
        ["挖掘机", "3", "履带式"],
    ]
    start, end, header = find_table_header(rows)
    assert (start, end) == (2, 2)
    assert header == ["设备名称", "数量", "备注"]


def test_find_table_header_multi_row_merge():
    rows = [
        ["主要机械设备", "", "检测设备"],
        ["名称", "数量", "名称"],
        ["塔吊", "2", "全站仪"],
    ]
    start, end, header = find_table_header(rows)
    assert (start, end) == (0, 1)
    assert header[0] == "主要机械设备 / 名称"
    assert header[2] == "检测设备 / 名称"


def test_find_table_header_none_generates_generic():
    # 纯数字数据行不具备表头语义
    rows = [["10", "20"], ["30", "40"]]
    start, end, header = find_table_header(rows)
    assert start == -1 and end == -1 and header == []


def test_render_table_with_header_pre_rows_kept():
    rows = [
        ["某工程机械设备配置表"],
        ["设备名称", "数量"],
        ["塔吊", "2"],
    ]
    lines = render_table_with_header(rows)
    assert lines[0] == "【表格】"
    assert "某工程机械设备配置表" in lines[1]
    assert "| 设备名称 | 数量 |" in lines
    assert "| --- | --- |" in lines
    assert "| 塔吊 | 2 |" in lines


def test_render_table_with_header_generic_when_missing():
    lines = render_table_with_header([["10", "20"], ["30", "40"]])
    assert "| 列1 | 列2 |" in lines
    assert "| 10 | 20 |" in lines


def test_parse_csv_uses_smart_header():
    # 首行是单列表题（非表头），第二行才是表头
    content = "某项目机械设备表\n设备,数量\n塔吊,2\n".encode("utf-8")
    text, diag = parse_file_content_ex(content, "t.csv")
    assert "某项目机械设备表" in text
    assert "| 设备 | 数量 |" in text
    assert "| --- | --- |" in text
    assert "| 塔吊 | 2 |" in text


# ---------------------------------------------------------------------------
# MinerU 客户端纯函数与分派守卫
# ---------------------------------------------------------------------------

def test_make_data_id_sanitizes_and_truncates():
    assert mineru_client.make_data_id("招标文件 2026.docx") == "招标文件_2026.docx"
    did = mineru_client.make_data_id("x" * 200)
    assert len(did) <= 96
    assert mineru_client.make_data_id("///***") == "document"


def test_extract_markdown_from_zip_prefers_full_md():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other/summary.md", "次要内容")
        zf.writestr("full.md", "# 完整正文")
    assert mineru_client.extract_markdown_from_zip(buf.getvalue()) == "# 完整正文"


def test_extract_markdown_from_zip_fallback_any_md_and_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("out/result.md", "内容")
    assert mineru_client.extract_markdown_from_zip(buf.getvalue()) == "内容"
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("a.txt", "x")
    with pytest.raises(ParseError):
        mineru_client.extract_markdown_from_zip(empty.getvalue())


def test_parse_with_mineru_disabled_and_unconfigured(monkeypatch):
    monkeypatch.setattr(mineru_client.settings, "mineru_enabled", True)
    monkeypatch.setattr(mineru_client.settings, "mineru_provider", "")
    with pytest.raises(ParseError):
        mineru_client.parse_with_mineru(b"x", "a.pdf")
    monkeypatch.setattr(mineru_client.settings, "mineru_enabled", False)
    monkeypatch.setattr(mineru_client.settings, "mineru_provider", "agent")
    with pytest.raises(ParseError):
        mineru_client.parse_with_mineru(b"x", "a.pdf")
