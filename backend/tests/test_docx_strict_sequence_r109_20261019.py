"""R109 护栏 · 导出 DOCX「WPS 打不开」收口（OOXML 严格序列）

取证（桌面《文档损坏诊断与修复报告》对应成稿，逐 ZIP 条目 + lxml 体检）：
  落地脚手架第1轮 162 处 / 第2轮 166 处 / 模板工程第4轮 256 处
  ``<w:tcPr>`` 子元素顺序违例（实测 tcW → vAlign → shd，ECMA-376 CT_TcPr
  要求 shd 在 vAlign 之前）；同一份文档「能打开」的修复版违例数为 0。
  其余部件与结构（tbl 归属 / sectPr 位置 / 非法 XML 字符）两侧零差异 ——
  这是唯一差异点：Word 对 CT_* 序列违例宽容，WPS 严格校验直接拒绝打开。
根因：``get_or_add_tcPr().append(shd)`` 把 w:shd 追加到末尾，而该格此前已被
  ``cell.vertical_alignment = CENTER`` 写入 w:vAlign。

覆盖：
  A 组 · _set_cell_shading 插入位置（vAlign 之后写入仍合法 / 幂等 / fill 正确）
  B 组 · _add_table_from_markup 端到端产物零违例（表头底纹格）
  C 组 · _add_cover_info_table / _add_form_table 端到端产物零违例
  D 组 · _normalize_ct_sequence（乱序修正 / 有序零改动 / 未知元素不动 / 注释不动）
  E 组 · _normalize_docx_strict_sequence 整包归一与 fail-soft
  F 组 · 源码锁（零 tcPr.append(shd)；save 前必先归一）
  G 组 · 字节体检

判据纪律（AGENTS §5.14/§5.15）：功能实证为主，AST/源码锁只锚调用形态与结构。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import app.routers.export as E
import pytest
from docx import Document
from docx.oxml.ns import qn
from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TCPR = [
    "cnfStyle",
    "tcW",
    "gridSpan",
    "hMerge",
    "vMerge",
    "tcBorders",
    "shd",
    "noWrap",
    "tcMar",
    "textDirection",
    "tcFitText",
    "vAlign",
    "hideMark",
]
PPR = [
    "pStyle",
    "keepNext",
    "keepLines",
    "pageBreakBefore",
    "framePr",
    "widowControl",
    "numPr",
    "suppressLineNumbers",
    "pBdr",
    "shd",
    "tabs",
    "suppressAutoHyphens",
    "kinsoku",
    "wordWrap",
    "overflowPunct",
    "topLinePunct",
    "autoSpaceDE",
    "autoSpaceDN",
    "bidi",
    "adjustRightInd",
    "snapToGrid",
    "spacing",
    "ind",
    "contextualSpacing",
    "mirrorIndents",
    "suppressOverlap",
    "jc",
    "textDirection",
    "textAlignment",
    "textboxTightWrap",
    "outlineLvl",
    "divId",
    "cnfStyle",
    "rPr",
    "sectPr",
    "pPrChange",
]
RPR = [
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
]
TBLPR = [
    "tblStyle",
    "tblpPr",
    "tblOverlap",
    "bidiVisual",
    "tblStyleRowBandSize",
    "tblStyleColBandSize",
    "tblW",
    "jc",
    "tblCellSpacing",
    "tblInd",
    "tblBorders",
    "shd",
    "tblLayout",
    "tblCellMar",
    "tblLook",
    "tblCaption",
    "tblDescription",
]
TRPR = [
    "cnfStyle",
    "divId",
    "gridBefore",
    "gridAfter",
    "wBefore",
    "wAfter",
    "cantSplit",
    "trHeight",
    "tblHeader",
    "tblCellSpacing",
    "jc",
    "hidden",
]
ORDERS = {"pPr": PPR, "rPr": RPR, "tblPr": TBLPR, "tcPr": TCPR, "trPr": TRPR}


def _violations(root) -> list[str]:
    """返回所有严格序列容器的违例描述（空列表 = 合法）。"""
    bad: list[str] = []
    for tag, order in ORDERS.items():
        idx = {t: i for i, t in enumerate(order)}
        for el in root.iter(f"{{{W}}}{tag}"):
            known = [etree.QName(c).localname for c in el if isinstance(c.tag, str) and etree.QName(c).localname in idx]
            ranks = [idx[k] for k in known]
            if ranks != sorted(ranks):
                bad.append(f"{tag}:{known}")
    return bad


def _violations_in_docx(path) -> list[str]:
    bad: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".xml"):
                bad += [f"{name}|{v}" for v in _violations(etree.fromstring(z.read(name)))]
    return bad


def _docx(tmp_path, name="t.docx"):
    return str(Path(tmp_path) / name)


# ---------------------------------------------------------------------------
# A 组 · _set_cell_shading 插入位置
# ---------------------------------------------------------------------------
class TestSetCellShading:
    def test_shd_precedes_existing_valign(self):
        """先写 vAlign 再上底纹 —— 正是旧实现产出非法序列的场景。"""
        from docx.enum.table import WD_ALIGN_VERTICAL

        doc = Document()
        t = doc.add_table(rows=1, cols=1)
        cell = t.rows[0].cells[0]
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER  # 先落 w:vAlign
        E._set_cell_shading(cell, "B6B1D1")
        seq = [etree.QName(c).localname for c in cell._tc.get_or_add_tcPr()]
        assert "vAlign" in seq and "shd" in seq
        # vAlign 必然仍在（垂直居中语义不能丢）
        assert cell.vertical_alignment == WD_ALIGN_VERTICAL.CENTER
        # shd 必须排在 vAlign 之前（CT_TcPr 序列）
        assert seq.index("shd") < seq.index("vAlign")
        assert _violations(doc.element.body) == []

    def test_shd_attrs_and_fill(self):
        doc = Document()
        cell = doc.add_table(rows=1, cols=1).rows[0].cells[0]
        E._set_cell_shading(cell, "F2F2F2")
        shd = cell._tc.get_or_add_tcPr().find(qn("w:shd"))
        assert shd is not None
        assert shd.get(qn("w:fill")) == "F2F2F2"
        assert shd.get(qn("w:val")) == "clear"
        assert shd.get(qn("w:color")) == "auto"

    def test_idempotent_no_duplicate_shd(self):
        """旧实现重复调用会堆出多个 w:shd（CT_TcPr 只允许 0~1 个）。"""
        doc = Document()
        cell = doc.add_table(rows=2, cols=1).rows[0].cells[0]
        E._set_cell_shading(cell, "F2F2F2")
        E._set_cell_shading(cell, "B6B1D1")
        shds = cell._tc.get_or_add_tcPr().findall(qn("w:shd"))
        assert len(shds) == 1
        assert shds[0].get(qn("w:fill")) == "F2F2F2"  # 首次生效，后续幂等


# ---------------------------------------------------------------------------
# B/C 组 · 端到端产物零违例（落盘后回读体检）
# ---------------------------------------------------------------------------
class TestEndToEndNoViolation:
    def test_table_from_markup(self, tmp_path):
        doc = Document()
        E._add_table_from_markup(
            doc,
            [
                "| 名称 | 规格 |",
                "| --- | --- |",
                "| 钢管 | φ48.3×3.6 |",
                "| 扣件 | 直角 |",
            ],
            "宋体",
        )
        p = _docx(tmp_path, "markup.docx")
        doc.save(p)
        assert _violations_in_docx(p) == []
        d2 = Document(p)
        assert len(d2.tables) == 1
        assert d2.tables[0].rows[0].cells[0].text.strip() == "名称"

    def test_cover_info_table(self, tmp_path):
        doc = Document()
        E._add_cover_info_table(doc, {"工程名称": "某项目", "方案编号": "FA-001"}, "宋体")
        p = _docx(tmp_path, "cover.docx")
        doc.save(p)
        assert _violations_in_docx(p) == []

    def test_form_table(self, tmp_path):
        doc = Document()
        E._add_form_table(doc, [("编制人", ""), ("审核人", "")], "宋体", keep_empty=True)
        p = _docx(tmp_path, "form.docx")
        doc.save(p)
        assert _violations_in_docx(p) == []

    def test_mixed_document(self, tmp_path):
        """一张文档里同时含三类表格 + 段落底纹，整体零违例。"""
        doc = Document()
        E._add_cover_info_table(doc, {"工程名称": "某项目"}, "宋体")
        E._add_table_from_markup(doc, ["| a | b |", "| --- | --- |", "| 1 | 2 |"], "宋体")
        E._add_form_table(doc, [("签字", "")], "宋体", keep_empty=True)
        para = doc.add_paragraph("正文")
        E._set_paragraph_shading(para, "F2F2F2")
        p = _docx(tmp_path, "mixed.docx")
        E._normalize_docx_strict_sequence(doc)
        doc.save(p)
        assert _violations_in_docx(p) == []


# ---------------------------------------------------------------------------
# D 组 · _normalize_ct_sequence 语义
# ---------------------------------------------------------------------------
def _tc_pr_xml(inner: str) -> etree._Element:
    return etree.fromstring(
        '<w:tcPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' f"{inner}</w:tcPr>"
    )


class TestNormalizeCtSequence:
    def test_reorders_bad_tcpr(self):
        el = _tc_pr_xml(
            '<w:tcW w:w="100" w:type="dxa"/><w:vAlign w:val="center"/>' '<w:shd w:val="clear" w:fill="B6B1D1"/>'
        )
        n = E._normalize_ct_sequence(el.getroottree().getroot())
        assert n == 1
        seq = [etree.QName(c).localname for c in el]
        assert seq == ["tcW", "shd", "vAlign"]
        assert _violations(el) == []

    def test_ordered_container_untouched(self):
        """已有序 → 一个字节都不动（稳定排序，既有产物零漂移）。"""
        el = _tc_pr_xml('<w:tcW w:w="1" w:type="dxa"/><w:shd w:fill="FFF"/>' '<w:vAlign w:val="center"/>')
        before = etree.tostring(el)
        assert E._normalize_ct_sequence(el) == 0
        assert etree.tostring(el) == before

    def test_unknown_child_skips_container(self):
        """容器内出现序列表以外的子元素 → 整容器不动（fail-safe）。"""
        el = _tc_pr_xml('<w:vAlign w:val="center"/><w:shd w:fill="FFF"/>' "<w:customXmlInsRangeStart/>")
        before = etree.tostring(el)
        assert E._normalize_ct_sequence(el) == 0
        assert etree.tostring(el) == before

    def test_comment_skips_container(self):
        el = _tc_pr_xml('<w:vAlign w:val="center"/><!-- 注 --><w:shd w:fill="FFF"/>')
        before = etree.tostring(el)
        assert E._normalize_ct_sequence(el) == 0
        assert etree.tostring(el) == before

    def test_content_preserved_after_reorder(self):
        el = _tc_pr_xml('<w:vAlign w:val="center"/><w:shd w:fill="B6B1D1"/>' '<w:tcW w:w="2268" w:type="dxa"/>')
        E._normalize_ct_sequence(el)
        assert el.find(qn("w:vAlign")).get(qn("w:val")) == "center"
        assert el.find(qn("w:shd")).get(qn("w:fill")) == "B6B1D1"
        assert el.find(qn("w:tcW")).get(qn("w:w")) == "2268"


# ---------------------------------------------------------------------------
# E 组 · _normalize_docx_strict_sequence 整包归一
# ---------------------------------------------------------------------------
class TestNormalizeDocxPackage:
    def test_fixes_manually_broken_document(self, tmp_path):
        """人工制造「旧实现」形态的非法 tcPr，归一后必须归零。"""
        doc = Document()
        t = doc.add_table(rows=1, cols=2)
        for cell in t.rows[0].cells:
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_pr.append(
                etree.fromstring(
                    '<w:vAlign xmlns:w="http://schemas.openxmlformats.org/'
                    'wordprocessingml/2006/main" w:val="center"/>'
                )
            )
            tc_pr.append(
                etree.fromstring(
                    '<w:shd xmlns:w="http://schemas.openxmlformats.org/' 'wordprocessingml/2006/main" w:fill="B6B1D1"/>'
                )
            )
        assert len(_violations(doc.element.body)) == 2
        n = E._normalize_docx_strict_sequence(doc)
        assert n == 2
        assert _violations(doc.element.body) == []

    def test_clean_document_returns_zero(self, caplog):
        doc = Document()
        doc.add_paragraph("正文一段")
        with caplog.at_level("WARNING"):
            assert E._normalize_docx_strict_sequence(doc) == 0
        assert "OOXML 严格序列违例" not in caplog.text

    def test_part_without_element_skipped(self, monkeypatch):
        doc = Document()

        class _Bare:
            element = None

        monkeypatch.setattr(doc.part.package, "iter_parts", lambda: [("p1", _Bare()), ("p2", doc.part)])
        assert E._normalize_docx_strict_sequence(doc) == 0

    def test_part_raising_does_not_block(self, monkeypatch, caplog):
        doc = Document()

        class _Boom:
            @property
            def element(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(doc.part.package, "iter_parts", lambda: [("p1", _Boom()), ("p2", doc.part)])
        with caplog.at_level("WARNING"):
            assert E._normalize_docx_strict_sequence(doc) == 0
        assert "序列归一跳过一个部件" in caplog.text

    def test_iter_parts_raising_falls_back_to_main(self, monkeypatch, caplog):
        doc = Document()

        def _boom():
            raise RuntimeError("no package")

        monkeypatch.setattr(doc.part.package, "iter_parts", _boom)
        with caplog.at_level("WARNING"):
            assert E._normalize_docx_strict_sequence(doc) == 0
        assert "无法枚举包内部件" in caplog.text


# ---------------------------------------------------------------------------
# F 组 · 源码锁
# ---------------------------------------------------------------------------
class TestSourceLocks:
    @pytest.fixture()
    def src(self) -> str:
        # 拆分兼容：聚合读取 export.py 与其子模块（_export_*.py）
        base = Path(E.__file__).parent
        segs = [Path(E.__file__).read_text(encoding="utf-8")]
        segs += [p.read_text(encoding="utf-8") for p in sorted(base.glob("_export_*.py"))]
        return "\n".join(segs)

    def test_no_tcpr_append_shd(self, src):
        """旧的非法追加方式不得复活（AST 判据，注释里的字面量不算）。"""
        import ast

        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append":
                seg = ast.get_source_segment(src, node) or ""
                if "tcPr" in seg:
                    hits.append(seg)
        assert hits == []

    def test_shading_sites_use_helper(self, src):
        assert src.count("_set_cell_shading(") >= 5  # 3 处调用点 + 定义 + 文档引用

    def test_normalize_called_before_save(self, src):
        i_norm = src.index("    _normalize_docx_strict_sequence(doc)")
        i_save = src.index("    doc.save(out_path)")
        assert 0 < i_norm < i_save

    def test_exporter_version_invalidates_broken_artifacts(self):
        """旧（坏）成稿必须整体失效：否则内容与配置未变 → 每次都命中打不开的缓存。"""
        assert int(E._EXPORTER_VERSION) >= 23


# ---------------------------------------------------------------------------
# G 组 · 字节体检
# ---------------------------------------------------------------------------
class TestByteHealth:
    def test_export_py_bytes(self):
        raw = Path(E.__file__).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw.replace(b"\r\n", b"")
        assert "�".encode("utf-8") not in raw
