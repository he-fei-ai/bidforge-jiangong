# -*- coding: utf-8 -*-
"""R129（2026-10-22）：DOCX 导出增强收口（引入易标式导出机制）。

覆盖：
  A 组 Word 2010+ 专有扩展清除（services/docx_namespace）：
      python-docx 默认模板根元素确含 w14/wp14 等声明（实测取证）→
      sanitize 后根元素纯净、python-docx 可重开、幂等、被引用前缀保留；
  B 组 导出后读回校验（services/export_validate）：
      Markdown / HTML / HTML 实体 / 占位符残留计数 + 大纲映射提取；
  C 组 L1~L9 标题层级：9 级样式默认表、outlineLvl 显式写入、TOC 域深度 1-9；
  D 组 公式三级降级：OMML → PNG 图片（matplotlib mathtext）→ 原文；
  E 组 接线与版本锚：_build_docx_sync 落盘后清除+校验（E2E）、v30 锚、
      正文 SystemPrompt Markdown 列表语法约束锚。
"""
import io
import re
import zipfile

import pytest
from app.routers import export as E
from app.routers.export import (
    _add_omml_formula,
    _add_toc_field,
    _build_docx_sync,
    _load_heading_styles,
    _parse_content_blocks,
)
from app.services import docx_math
from app.services.docx_namespace import check_docx_file, sanitize_docx_file
from app.services.export_validate import post_export_check, validate_document
from docx import Document
from docx.oxml.ns import qn


def _instr_texts(doc):
    return [el.text or "" for el in doc.element.body.iter(qn("w:instrText"))]


@pytest.fixture(autouse=True)
def _fresh_fix_stats():
    """隔离线程局部修复统计（每用例从零开始，断言计数不被历史污染）。"""
    E._FIX_STATS_LOCAL.stats = None
    yield
    E._FIX_STATS_LOCAL.stats = None


# ============================================================
# A 组：Word 2010+ 专有扩展清除
# ============================================================
class TestNamespaceSanitize:
    def _save_empty(self, tmp_path, name="t.docx"):
        p = tmp_path / name
        Document().save(str(p))
        return p

    def test_default_template_has_banned_ns(self, tmp_path):
        """取证锚：python-docx 默认模板根元素确含 2010+ 命名空间（R129 的靶子）。"""
        p = self._save_empty(tmp_path)
        report = check_docx_file(p)
        assert report["clean"] is False
        assert any("w14" in "/".join(v) for v in report["issues"].values())

    def test_sanitize_removes_banned_ns_and_reopens(self, tmp_path):
        p = self._save_empty(tmp_path)
        rep = sanitize_docx_file(p)
        assert "word/document.xml" in rep["parts_changed"]
        assert set(rep["parts_changed"]["word/document.xml"]) >= {"w14", "wp14"}
        # 文件级校验转绿
        assert check_docx_file(p)["clean"] is True
        # python-docx 重开无损
        doc = Document(str(p))
        assert doc.paragraphs is not None
        # 根元素不再含被删前缀的声明
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        root = xml[xml.find("<w:document"):xml.find(">", xml.find("<w:document"))]
        for banned in ("w14", "wp14", "w15", "wpg", "wps", "wpc", "wpi"):
            assert f"xmlns:{banned}=" not in root
        assert 'mc:Ignorable="w14 wp14"' not in root

    def test_sanitize_idempotent_bytes_stable(self, tmp_path):
        p = self._save_empty(tmp_path)
        sanitize_docx_file(p)
        before = p.read_bytes()
        rep2 = sanitize_docx_file(p)
        assert rep2["parts_changed"] == {} and rep2["attrs_stripped"] == 0
        assert p.read_bytes() == before

    def test_used_prefix_keeps_declaration(self, tmp_path):
        """被引用的专有前缀绝不删声明（防 XML 损坏的第一护栏）。"""
        p = tmp_path / "used.docx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types/>')
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0"?>\n<w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
                'mc:Ignorable="w14">'
                "<w:body><w:p><w14:textFill><w14:fill/></w14:textFill>"
                "<w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>")
        rep = sanitize_docx_file(p)
        # 前缀仍被元素引用 ⇒ 声明保留、mc:Ignorable 保留
        assert "word/document.xml" not in rep["parts_changed"]
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "xmlns:w14=" in xml
        assert 'mc:Ignorable="w14"' in xml
        # 校验器仍如实报告（clean=False）
        assert check_docx_file(p)["clean"] is False

    def test_strips_w14_comment_attrs(self, tmp_path):
        """防御性删除 w14:paraId 等残存注释属性（模板残存）。"""
        p = tmp_path / "attrs.docx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types/>')
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0"?>\n<w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p w14:paraId="1A2B3C4D" w14:textId="77777777">'
                "<w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>")
        rep = sanitize_docx_file(p)
        assert rep["attrs_stripped"] == 2
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        assert "w14:paraId" not in xml and "w14:textId" not in xml

    def test_non_target_parts_untouched(self, tmp_path):
        """theme 等非目标部件一字节不动（范围契约）。"""
        p = tmp_path / "with_theme.docx"
        theme = '<?xml version="1.0"?><a:theme xmlns:w14="urn:x"/>'
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
            z.writestr("word/document.xml",
                       '<?xml version="1.0"?><w:document '
                       'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
            z.writestr("word/theme/theme1.xml", theme)
        sanitize_docx_file(p)
        with zipfile.ZipFile(p) as z:
            assert z.read("word/theme/theme1.xml").decode("utf-8") == theme


# ============================================================
# B 组：导出后读回校验
# ============================================================
class TestExportValidate:
    def test_residues_detected(self):
        doc = Document()
        doc.add_paragraph("# 残留标题行")
        doc.add_paragraph("这里有 ** 加粗标记残留")
        doc.add_paragraph("实体 &nbsp; 残留")
        doc.add_paragraph("数据 ×× kPa 占位")
        doc.add_paragraph("<td>HTML 标签残留</td>")
        doc.add_paragraph("`代码反引号` 残留")
        doc.add_paragraph("![图片](http://x/y.png) 语法残留")
        tbl = doc.add_table(rows=1, cols=1)
        tbl.rows[0].cells[0].paragraphs[0].text = "表内 ** 残留"
        rep = validate_document(doc)
        assert rep["markdown_residue"] >= 5
        assert rep["html_residue"] >= 1
        assert rep["entity_residue"] >= 1
        assert rep["placeholder_residue"] >= 1
        # 表格单元格文本也被扫到
        assert any("表内" in s for v in rep["samples"].values() for s in v)
        # 样例体积受控
        assert all(len(v) <= 5 for v in rep["samples"].values())

    def test_clean_doc_zero_counts(self):
        doc = Document()
        doc.add_paragraph("1.1.1 一般规定：基础埋深按地质条件与设计允许值控制。")
        doc.add_paragraph("2.1.2 立杆间距不大于 1.5m，符合规范要求。")
        rep = validate_document(doc)
        assert rep["markdown_residue"] == 0
        assert rep["html_residue"] == 0
        assert rep["entity_residue"] == 0
        assert rep["placeholder_residue"] == 0

    def test_outline_map_extraction(self):
        doc = Document()
        doc.add_paragraph("第一章 工程概况", style="Heading 1")
        doc.add_paragraph("1.1 编制依据", style="Heading 2")
        rep = validate_document(doc)
        assert rep["outline_map"] == [
            {"level": 1, "text": "第一章 工程概况"},
            {"level": 2, "text": "1.1 编制依据"},
        ]

    def test_post_export_check_ns_flow(self, tmp_path):
        """未清理 → ns_clean=False；清理后 → True（校验器与清除器闭环）。"""
        p = tmp_path / "ns.docx"
        doc = Document()
        doc.add_paragraph("干净正文。")
        doc.save(str(p))
        before = post_export_check(doc, p)
        assert before["ns_clean"] is False
        sanitize_docx_file(p)
        after = post_export_check(doc, p)
        assert after["ns_clean"] is True


# ============================================================
# C 组：L1~L9 标题层级
# ============================================================
class TestHeadingLevels:
    def test_default_styles_cover_1_to_9(self):
        assert sorted(E._DEFAULT_HEADING_STYLES) == list(range(1, 10))

    def test_load_heading_styles_9_levels_with_override(self):
        styles = _load_heading_styles({"heading_styles": {"9": {"font_size": 10}}})
        assert sorted(styles) == list(range(1, 10))
        assert styles[9]["font_size"] == 10
        assert styles[1]["font_size"] == 16

    def test_toc_field_depth_9(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_toc_field(p, 9)
        assert any('\\o "1-9"' in t for t in _instr_texts(doc))

    def test_toc_field_depth_clamped(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_toc_field(p, 99)
        assert any('\\o "1-9"' in t for t in _instr_texts(doc))

    def test_toc_depth_config_clamp_static(self):
        """配置层 toc_depth 上限同步放宽到 9（静态锁，防两处漂移）。"""
        import inspect
        src = inspect.getsource(E)
        assert src.count('min(_int("toc_depth", 3), 9)') == 1

    def test_heading_7_9_outline_lvl_written(self):
        """L7~L9 样式显式写入 outlineLvl（6/7/8）——用户硬性要求。"""
        doc = Document()
        for lvl in range(1, 10):
            st = doc.styles[f"Heading {lvl}"]
            if lvl >= 7:
                ol = st.element.get_or_add_pPr().get_or_add_outlineLvl()
                ol.set(qn("w:val"), str(lvl - 1))
        for lvl in (7, 8, 9):
            ol = doc.styles[f"Heading {lvl}"].element.get_or_add_pPr().get_or_add_outlineLvl()
            assert ol.get(qn("w:val")) == str(lvl - 1)


# ============================================================
# D 组：公式三级降级（OMML → 图片 → 原文）
# ============================================================
def _mk_png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (80, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


class TestFormulaImageFallback:
    def test_latex_to_png_ok(self):
        data = docx_math.latex_to_png_bytes(r"\frac{N}{A} \leq f")
        assert data is not None
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_latex_to_png_unsupported_returns_none(self):
        # mathtext 不支持的命令 → best-effort None，绝不抛异常
        assert docx_math.latex_to_png_bytes(r"\absolutelynotacommand{x}") is None

    def test_omml_failure_degrades_to_image(self, monkeypatch):
        def _raise(latex):
            raise ValueError("OMML 含空参数元素，拒绝产出")

        monkeypatch.setattr(docx_math, "latex_to_omml", _raise)
        monkeypatch.setattr(docx_math, "latex_to_png_bytes",
                            lambda *a, **k: _mk_png_bytes())
        doc = Document()
        p = doc.add_paragraph()
        _add_omml_formula(p, "N = 1.2g_k", display=False, whole_para=False)
        # 图片已行内嵌入（w:drawing），且无原文文本回退
        assert p._p.find(qn("w:r") + "/" + qn("w:drawing")) is not None or \
            any(True for _ in p._p.iter(qn("w:drawing")))
        assert "$" not in (p.text or "")
        assert E._fix_stats()["formula_image_fallback"] == 1

    def test_image_failure_falls_back_to_text(self, monkeypatch):
        def _raise(latex):
            raise ValueError("拒绝产出")

        monkeypatch.setattr(docx_math, "latex_to_omml", _raise)
        monkeypatch.setattr(docx_math, "latex_to_png_bytes", lambda *a, **k: None)
        doc = Document()
        p = doc.add_paragraph()
        _add_omml_formula(p, "N = 1.2g_k", display=False, whole_para=False)
        assert p.text == "$N = 1.2g_k$"
        assert E._fix_stats()["formula_image_fallback"] == 0

    def test_omml_success_no_fallback(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_omml_formula(p, r"\frac{M}{W} \leq f", display=False, whole_para=False)
        assert any(True for _ in p._p.iter(qn("m:oMath"))) or \
            p._p.find(qn("m:oMath")) is not None
        assert E._fix_stats()["formula_image_fallback"] == 0


# ============================================================
# E 组：接线与版本锚
# ============================================================
class TestWiringAndVersion:
    def test_exporter_version_30(self):
        # ✅ R131-A（2026-10-23）随标准同步：v30 → v31，图题/配图 alt 剥离生成期
        # 「图 X-Y」前缀改变**可见题注文本**（双图号 → 单图号），旧缓存必须失效；
        # 依据 export.py `_EXPORTER_VERSION` 头注释「每次修改导出排版逻辑后 +1」。
        assert E._EXPORTER_VERSION == "31"

    def test_fix_stats_keys_include_new(self):
        st = E._fix_stats()
        for key in ("ns_parts_sanitized", "postcheck_markdown_residue",
                    "postcheck_html_residue", "postcheck_entity_residue",
                    "postcheck_placeholder_residue", "formula_image_fallback"):
            assert key in st and st[key] == 0

    def test_wiring_static_lock_postcheck_before_stats(self):
        """段落级校验必须位于 _log_fix_stats 之前（计数才能进日志与响应头）。"""
        import inspect
        src = inspect.getsource(_build_docx_sync)
        i_check = src.index("validate_document(doc)")
        i_stats = src.index("fix_stats = _log_fix_stats()")
        assert i_check < i_stats
        i_sanitize = src.index("sanitize_docx_file(out_path)")
        i_save = src.index("doc.save(out_path)")
        assert i_save < i_sanitize

    def test_prompt_markdown_list_constraint(self):
        from app.services.ai.prompts._registry import get_prompt
        text = get_prompt("content_generation_system")
        assert "标准 Markdown 列表语法" in text
        assert "手工序号段落冒充列表" in text

    # ---- E2E：完整走一遍 _build_docx_sync ----
    _CONTENT = (
        "支架搭设应满足下列要求：\n\n"
        "1. 立杆间距不大于 1.5m；\n"
        "2. 扫地杆距底座上皮不大于 200mm。\n\n"
        "基础处理详见表 1-1。\n\n"
        "| 部位 | 要求 |\n| --- | --- |\n| 基础 | 平整夯实 |\n"
    )

    def _build_e2e(self, tmp_path):
        sections = [
            {"id": "s1", "parent_id": "", "level": 1,
             "title": "第一章 施工工艺", "content": self._CONTENT,
             "sort_order": 1},
        ]
        blocks = {"s1": _parse_content_blocks(self._CONTENT)}
        out = tmp_path / "r129_e2e.docx"
        stats = _build_docx_sync(
            str(out),
            {"id": "sch1", "project_id": "p1", "name": "R129 导出增强验证方案"},
            sections, {}, {}, {}, blocks,
            "宋体", 12, "R129 页眉", "", True,
            True, True,
            bidder_name="",
            heading_styles=_load_heading_styles({}),
            page_break_before_chapter=True,
            line_spacing=1.5,
            page_number_style="page_of_total",
            toc_depth=3,
            cover_info={"项目名称": "R129 验证项目", "编制单位": "测试单位"},
        )
        return out, stats

    def test_e2e_namespace_sanitized_and_reported(self, tmp_path):
        out, stats = self._build_e2e(tmp_path)
        # 落盘产物根元素纯净
        assert check_docx_file(out)["clean"] is True
        # 返回统计带清理与校验键（X-Fix-Stats 消费面）
        assert stats.get("ns_parts_sanitized", 0) >= 1
        assert stats.get("ns_clean") is True
        assert stats.get("outline_entries", 0) >= 1
        # ⚠️ outline_map（中文标题）刻意不进响应头（HTTP 头 latin-1 限制）
        assert "outline_map" not in stats

    def test_e2e_doc_reopens_no_residue(self, tmp_path):
        out, _ = self._build_e2e(tmp_path)
        doc = Document(str(out))
        rep = validate_document(doc)
        # 渲染链路应把 Markdown 全部消费掉：正文不得有残留
        assert rep["markdown_residue"] == 0
        assert rep["html_residue"] == 0
        assert rep["entity_residue"] == 0
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None

    def test_e2e_l9_styles_and_toc_reachable(self, tmp_path):
        out, _ = self._build_e2e(tmp_path)
        with zipfile.ZipFile(out) as z:
            styles_xml = z.read("word/styles.xml").decode("utf-8")
        # Heading 8/9 样式在产物中存在且带 outlineLvl（7/8）
        assert 'w:styleId="Heading8"' in styles_xml
        assert 'w:styleId="Heading9"' in styles_xml
        m = re.search(
            r'w:styleId="Heading9".*?<w:outlineLvl w:val="(\d)"', styles_xml, re.S)
        assert m and m.group(1) == "8"
