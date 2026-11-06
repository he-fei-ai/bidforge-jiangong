# -*- coding: utf-8 -*-
"""W-01 / W-02 升级项回归（2026-11-05，对照公众号标书方法论文章）。

W-01：补遗/澄清/答疑文件语义
  - 新分类「补遗文件」提取优先级最高（0），自动分类规则置顶；
  - `_combine_doc_texts` 为补遗段追加「同名条款以本文档为准」标记；
  - bid 提取 STABLE_SYSTEM_PROMPT 含同名条款覆盖规则。

W-02：表格结构保真
  - DOCX：gridSpan / vMerge 按网格展开（重复值填充，列不错位）；
  - PDF：跨页续表按「列数相同 + 首行相似/无表头」拼接（纯函数 _stitch_pdf_tables）。
"""

import io
import zipfile

from app.services import doc_categories as dc
from app.services.bid_analysis_service import STABLE_SYSTEM_PROMPT
from app.services.file_parser import _parse_docx, _stitch_pdf_tables

# ---------------------------------------------------------------------------
# W-01 补遗文件分类
# ---------------------------------------------------------------------------


def test_addendum_priority_is_highest():
    assert dc.extract_priority("补遗文件") == 0
    assert dc.extract_priority("补遗文件") < dc.extract_priority("招标文件")
    # 既有分类相对次序不变
    assert dc.extract_priority("招标文件") < dc.extract_priority("合同文件")
    assert dc.extract_priority("报价清单") < dc.extract_priority("其他")


def test_addendum_autoclassify_rules_first():
    """补遗语义必须压过「招标/投标」宽关键词（规则置顶）。"""
    assert dc.auto_classify_document("XX项目招标补遗文件（一）.pdf") == "补遗文件"
    assert dc.auto_classify_document("投标答疑纪要.docx") == "补遗文件"
    assert dc.auto_classify_document("澄清函.pdf") == "补遗文件"
    assert dc.auto_classify_document("补充通知2026-03.pdf") == "补遗文件"


def test_addendum_keywords_do_not_swallow_normal_files():
    """关键词刻意收窄：普通「补充说明」「更正后图纸」不得误判为补遗。"""
    assert dc.auto_classify_document("施工方案补充说明.docx") == "其他"
    assert dc.auto_classify_document("招标公告.pdf") == "招标文件"


def test_combine_doc_texts_marks_addendum_segment():
    from app.routers.bid_analysis import _combine_doc_texts

    docs = [
        {"file_name": "招标文件.pdf", "doc_category": "招标文件", "parsed_markdown": "工期 300 天。"},
        {"file_name": "补遗（一）.pdf", "doc_category": "补遗文件", "parsed_markdown": "工期调整为 280 天。"},
    ]
    joined = _combine_doc_texts(docs)
    assert dc.ADDENDUM_OVERRIDE_MARK in joined
    # 补遗优先级最高 → 排在招标文件之前
    assert joined.index("补遗（一）.pdf") < joined.index("招标文件.pdf")
    # 非补遗段不得带标记
    tender_seg = joined.split("---")[1]
    assert dc.ADDENDUM_OVERRIDE_MARK not in tender_seg


def test_system_prompt_has_addendum_override_rule():
    assert "补遗" in STABLE_SYSTEM_PROMPT
    assert "同名条款" in STABLE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# W-02 DOCX 合并单元格网格展开
# ---------------------------------------------------------------------------

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _make_docx(tbl_inner: str) -> bytes:
    document_xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}><w:body><w:tbl>{tbl_inner}</w:tbl></w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _tc(text: str, props: str = "") -> str:
    tcpr = f"<w:tcPr>{props}</w:tcPr>" if props else ""
    return f'<w:tc>{tcpr}<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc>'


def test_docx_gridspan_expands_and_pads():
    """横向合并：gridSpan=2 的单元格在 2 个网格列上重复填充，列不错位。"""
    tbl = (
        f"<w:tr>{_tc('评分因素', '<w:gridSpan w:val=\"2\"/>')}{_tc('分值')}</w:tr>"
        f"<w:tr>{_tc('技术方案')}{_tc('工期')}{_tc('30')}</w:tr>"
    )
    out = _parse_docx(_make_docx(tbl))
    assert "| 评分因素 | 评分因素 | 分值 |" in out
    assert "| 技术方案 | 工期 | 30 |" in out


def test_docx_vmerge_continue_fills_from_restart():
    """纵向合并：vMerge 续行（空 tc）取最近 restart 文本填充。"""
    tbl = (
        f"<w:tr>{_tc('机械名称')}{_tc('数量')}</w:tr>"
        f"<w:tr>{_tc('塔式起重机', '<w:vMerge w:val=\"restart\"/>')}{_tc('2 台')}</w:tr>"
        f"<w:tr>{_tc('', '<w:vMerge/>')}{_tc('3 台')}</w:tr>"
    )
    out = _parse_docx(_make_docx(tbl))
    assert "| 塔式起重机 | 2 台 |" in out
    assert "| 塔式起重机 | 3 台 |" in out


def test_docx_plain_table_unchanged_shape():
    """无合并的表格输出与旧口径一致（首行表头 + 分隔行）。"""
    tbl = f"<w:tr>{_tc('a')}{_tc('b')}</w:tr><w:tr>{_tc('1')}{_tc('2')}</w:tr>"
    out = _parse_docx(_make_docx(tbl))
    assert "【表格】" in out
    assert "| a | b |" in out
    assert "| --- | --- |" in out
    assert "| 1 | 2 |" in out


# ---------------------------------------------------------------------------
# W-02 PDF 跨页续表拼接（纯函数）
# ---------------------------------------------------------------------------

HEADER = ["评分因素", "分值", "备注"]


def test_stitch_merges_cross_page_with_repeated_header():
    """续页重复表头 → 并入上表且表头只保留一份；同页其余表不受影响。"""
    per_page = [
        (0, [[HEADER, ["技术方案", "30", ""], ["工期", "20", ""]]]),
        (1, [[HEADER, ["安全", "15", ""]], [["其他表", "x"], ["a", "b"]]]),
    ]
    stitched = _stitch_pdf_tables(per_page)
    assert len(stitched) == 2
    pidx, rows = stitched[0]
    assert pidx == 0
    assert rows == [HEADER, ["技术方案", "30", ""], ["工期", "20", ""], ["安全", "15", ""]]
    assert stitched[1] == (1, [["其他表", "x"], ["a", "b"]])


def test_stitch_merges_headerless_continuation():
    """续页首行不像表头（数值主导的数据行）→ 整表追加。"""
    per_page = [
        (0, [[HEADER, ["技术方案", "30", ""]]]),
        (1, [[["塔吊", "2", "80"]]]),
    ]
    stitched = _stitch_pdf_tables(per_page)
    assert len(stitched) == 1
    assert stitched[0][1][-1] == ["塔吊", "2", "80"]


def test_stitch_rejects_different_column_count():
    """列数不同 → 不拼（保守，防误并）。"""
    per_page = [
        (0, [[HEADER, ["技术方案", "30", ""]]]),
        (1, [[["列1", "列2"], ["a", "b"]]]),
    ]
    stitched = _stitch_pdf_tables(per_page)
    assert len(stitched) == 2


def test_stitch_rejects_new_table_with_own_header():
    """续页首行像表头且与上表表头不相似 → 视为新表，不拼。"""
    per_page = [
        (0, [[HEADER, ["技术方案", "30", ""]]]),
        (1, [[["机械名称", "型号", "数量"], ["塔吊", "QTZ80", "2"]]]),
    ]
    stitched = _stitch_pdf_tables(per_page)
    assert len(stitched) == 2
