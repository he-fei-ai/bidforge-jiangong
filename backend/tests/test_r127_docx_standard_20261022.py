# -*- coding: utf-8 -*-
"""R127（2026-10-22）：DOCX 格式标准符合性收口回归。

覆盖本轮四项修复：
  1. 列表改走 numbering.xml + w:numPr（不再是手写前缀文本）；
  2. 附录 / 封面表格补 tblHeader、cantSplit、fixed layout；
  3. PAGE / NUMPAGES / TOC 域按 Word 写法拆 run；
  4. 正文改走 Body Text CN 段落样式，不再逐段写直接缩进。

同时校验 DOCX 包结构、numbering 定义顺序、标题不倾斜、无西里尔字符。
"""
import logging
import re
import zipfile

from app.routers.export import _build_docx_sync, _load_heading_styles, _parse_content_blocks
from docx import Document
from docx.oxml.ns import qn

_CONTENT = (
    "本章说明施工组织与安全措施。\n"
    "\n"
    "## 1.1 施工准备\n"
    "\n"
    "1、成立项目组织机构\n"
    "2、编制专项施工方案\n"
    "3、完成技术交底\n"
    "（一）设计标准\n"
    "（二）材料要求\n"
    "（1）土方开挖\n"
    "（1）基坑支护\n"
    "- 注意事项一\n"
    "- 注意事项二\n"
    "\n"
    "| 项目 | 要求 | 备注 |\n"
    "| --- | --- | --- |\n"
    "| 混凝土 | C30 | 抗渗 P6 |\n"
    "| 钢筋 | HRB400 | 复试合格 |\n"
)


def _build(tmp_path, margins=None):
    sections = [{
        "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
        "title": "第一章 总体概述", "content": _CONTENT,
    }]
    blocks = {"c1": _parse_content_blocks(_CONTENT)}
    out = str(tmp_path / "r127.docx")
    _build_docx_sync(
        out,
        {"id": "s1", "project_id": "p1", "name": "R127 格式标准验证方案"},
        sections, {}, {}, {}, blocks,
        "宋体", 12, "R127 页眉", "", True,
        True, True,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        page_break_before_chapter=True,
        line_spacing=1.5,
        page_number_style="page_of_total",
        toc_depth=3,
        cover_info={"项目名称": "R127 格式标准验证项目", "编制单位": "测试单位"},
        global_facts=[
            {"gt": "项目概况", "title": "项目名称", "content": "R127 验证项目"},
            {"gt": "项目概况", "title": "建设地点", "content": "示例市示例区"},
            {"gt": "项目概况", "title": "建设规模", "content": "示例规模" * 30},
        ],
        appendix_sources=[{
            "title": "知识库要点",
            "items": [{"name": "安全要点", "value": "临边防护、洞口防护" * 20}],
        }],
        margins=margins,
    )
    return out


def _part(path, name):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def test_package_structure_complete(tmp_path):
    """OOXML 必需部件齐全，且能被 python-docx 重新打开。"""
    path = _build(tmp_path)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
    for required in (
        "[Content_Types].xml", "_rels/.rels", "word/document.xml",
        "word/styles.xml", "word/settings.xml", "word/fontTable.xml",
        "word/numbering.xml", "word/_rels/document.xml.rels",
    ):
        assert required in names, required
    doc = Document(path)
    assert doc.paragraphs


def test_lists_use_real_numbering_not_text(tmp_path):
    """列表：每项都有 w:numPr，且正文不再出现手写编号前缀。"""
    path = _build(tmp_path)
    doc = Document(path)
    list_paras = [p for p in doc.paragraphs if p.style.name == "List Paragraph"]
    assert list_paras, "未生成列表段落"
    for p in list_paras:
        num_pr = p._p.pPr.numPr
        assert num_pr is not None, p.text
        assert num_pr.ilvl.val >= 0
        assert num_pr.numId.val
        # numPr 子元素顺序必须是 ilvl -> numId（CT_NumPr）
        assert [c.tag for c in num_pr] == [qn("w:ilvl"), qn("w:numId")]
        assert not re.match(r"^(\d+[、.）)]|[（(][一二三四五六七八九十\d]+[）)]|•)", p.text), p.text

    numbering = doc.part.numbering_part.element
    texts = {lvl.find(qn("w:lvlText")).get(qn("w:val"))
             for lvl in numbering.findall(".//" + qn("w:lvl"))}
    assert any(t and "、" in t for t in texts), texts
    assert any(t and "（%" in t for t in texts), texts
    fmts = {f.get(qn("w:val")) for f in numbering.findall(".//" + qn("w:numFmt"))}
    assert "chineseCounting" in fmts, fmts

    # abstractNum 必须全部排在 num 之前（CT_Numbering 序列）
    children = [c.tag for c in numbering]
    first_num = children.index(qn("w:num"))
    assert all(t == qn("w:abstractNum") for t in children[:first_num])
    abs_ids = [a.get(qn("w:abstractNumId")) for a in numbering.findall(qn("w:abstractNum"))]
    assert len(abs_ids) == len(set(abs_ids)), "abstractNumId 重复"


def test_appendix_tables_repeat_header_and_do_not_split(tmp_path):
    """附录事实表 / 参考资料表：表头跨页重复、行不跨页、固定布局。"""
    path = _build(tmp_path)
    doc = Document(path)
    key_value_tables = []
    for tbl in doc.tables:
        first = "".join(c.text for c in tbl.rows[0].cells)
        if "名称" in first and "内容" in first:
            key_value_tables.append(tbl)
    assert len(key_value_tables) >= 2, "附录表未生成"

    for tbl in key_value_tables:
        tr_pr = tbl.rows[0]._tr.trPr
        assert tr_pr is not None and tr_pr.find(qn("w:tblHeader")) is not None
        for row in tbl.rows:
            row_pr = row._tr.trPr
            assert row_pr is not None and row_pr.find(qn("w:cantSplit")) is not None
        tbl_pr = tbl._tbl.tblPr
        layout = tbl_pr.find(qn("w:tblLayout"))
        assert layout is not None and layout.get(qn("w:type")) == "fixed"
        tbl_w = tbl_pr.find(qn("w:tblW"))
        assert tbl_w is not None and tbl_w.get(qn("w:type")) == "dxa"


def test_cover_table_rows_do_not_split(tmp_path):
    path = _build(tmp_path)
    doc = Document(path)
    cover = doc.tables[0]
    for row in cover.rows:
        assert row._tr.trPr.find(qn("w:cantSplit")) is not None


def test_fields_are_split_into_word_style_runs(tmp_path):
    """PAGE / NUMPAGES / TOC 域：begin/instr/separate/result/end 分 run 存放。"""
    path = _build(tmp_path)
    doc_xml = _part(path, "word/document.xml")
    footer = next(n for n in zipfile.ZipFile(path).namelist()
                  if n.startswith("word/footer"))
    footer_xml = _part(path, footer)

    assert "TOC \\o" in doc_xml
    assert "PAGE" in footer_xml and "NUMPAGES" in footer_xml
    combined = doc_xml + footer_xml
    # 不存在「同一 run 内同时含 fldChar 与 instrText」的旧写法
    for run in re.findall(r"<w:r\b.*?</w:r>", combined, flags=re.S):
        if "<w:fldChar" in run:
            assert "<w:instrText" not in run, run[:200]
            assert "<w:t" not in run, run[:200]
    assert combined.count('w:fldCharType="begin"') == combined.count('w:fldCharType="end"')


def test_body_uses_style_not_direct_indent(tmp_path):
    path = _build(tmp_path)
    doc = Document(path)
    body = next(p for p in doc.paragraphs if p.text.startswith("本章说明"))
    assert body.style.name == "Body Text CN"
    assert body.style.paragraph_format.first_line_indent is not None
    # 直接 pPr 不应再写 ind/firstLine（由样式承担）
    ind = body._p.pPr.find(qn("w:ind"))
    assert ind is None or ind.get(qn("w:firstLine")) is None


def test_heading_styles_not_italic(tmp_path):
    path = _build(tmp_path)
    doc = Document(path)
    for level in range(1, 8):
        st = doc.styles[f"Heading {level}"]
        assert st.font.italic is not True, f"Heading {level} 被设为斜体"


def test_no_cyrillic_homoglyphs(tmp_path):
    path = _build(tmp_path)
    text = _part(path, "word/document.xml")
    assert not re.search(r"[\u0400-\u04FF]", text)


def test_tables_fit_content_width_with_word_margins(tmp_path):
    """Word 标准页边距（左右 3.17cm）下，所有表宽不得超过版心（14.65cm）。"""
    path = _build(tmp_path, margins={
        "top": 2.54, "bottom": 2.54, "left": 3.17, "right": 3.17})
    doc = Document(path)
    sec = doc.sections[-1]
    available_cm = (sec.page_width - sec.left_margin - sec.right_margin) / 360000
    assert abs(available_cm - 14.66) < 0.1
    for tbl in doc.tables:
        tbl_w = tbl._tbl.tblPr.find(qn("w:tblW"))
        width_cm = int(tbl_w.get(qn("w:w"))) / 567
        assert width_cm <= available_cm + 0.05, (width_cm, available_cm)


def test_handwritten_xml_sequence_is_valid_without_normalizer(tmp_path, caplog):
    """手写 XML 本身顺序正确，不依赖落盘前兜底重排（否则 WPS 兼容性只是运气）。"""
    with caplog.at_level(logging.WARNING, logger="app.routers.export"):
        _build(tmp_path)
    assert "已修正" not in caplog.text, caplog.text


def test_image_width_clamped_to_content_width():
    """图片宽度按当前节版心封顶：左右 3.18cm 时不得再固定 16cm（会溢出页面）。"""
    from io import BytesIO

    from app.routers.export import _content_width_cm, _insert_image_with_caption
    from docx import Document
    from docx.shared import Cm
    from PIL import Image

    doc = Document()
    sec = doc.sections[-1]
    sec.page_width = Cm(21)
    sec.page_height = Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(3.18)
    sec.top_margin = sec.bottom_margin = Cm(2.54)

    buf = BytesIO()
    Image.new("RGB", (2400, 800), "white").save(buf, format="PNG", dpi=(300, 300))
    assert _insert_image_with_caption(doc, buf, "图 1-1 版心宽度验证") is True

    available_cm = _content_width_cm(doc)
    assert abs(available_cm - 14.66) < 0.1, available_cm
    width_cm = doc.inline_shapes[0].width / 360000
    assert width_cm <= available_cm + 0.01, (width_cm, available_cm)

