# -*- coding: utf-8 -*-
"""R183（2026-11-07）：导出 DOCX 的 OOXML 结构级校验。

用户硬要求：导出的 DOCX 必须能被 WPS 与 Windows Office 正常打开。
本用例不复用「能生成」这一弱判据，而是直接按 OPC / ECMA-376 检查**产物本身**：

  1. ZIP 容器 CRC 全通过、必需部件齐全；
  2. 包内全部 XML / RELS 部件可解析（截断的 document.xml 在此暴露）；
  3. [Content_Types].xml 覆盖每个部件，且主文档 MIME 正确；
  4. 每个关系文件的 Target 都指向真实存在的部件；
  5. word/document.xml 根元素命名空间正确；
  6. 落盘前严格序列归一后的 CT_* 序列违例为 0；
  7. settings.xml 的 w:updateFields 位于 w:compat / w:rsids 之前；
  8. 图题 / 表题编号唯一（不出现重复占号）。

同时覆盖 R183 修复面：缓存可用判据 `_zip_integrity_ok` 必须拒绝
「CRC 正常但 OOXML 结构损坏」的产物 —— 这类文件作为缓存命中交付，
用户看到的就是「导出文档无法打开」。
"""

import io
import os
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.routers.export as E  # noqa: E402
from app.routers._export_cache import (  # noqa: E402
    _CT_NS,
    _REL_NS,
    _WML_DOCUMENT_CT,
    _WML_NS,
    _cache_row_viable,
    _docx_package_problems,
    _zip_integrity_ok,
)

_PNG_BYTES = None


def _chart_png() -> bytes:
    """生成 >100 字节的真实 PNG（`_chart_ok` 会拒绝过小/不可解码的字节）。"""
    global _PNG_BYTES
    if _PNG_BYTES is None:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (320, 200), (40, 120, 200)).save(buf, format="PNG")
        _PNG_BYTES = buf.getvalue()
    return _PNG_BYTES

_DOCX_OPTIONS = {
    "font_name": "宋体",
    "font_size": 12.0,
    "page_header": "页眉",
    "page_footer": "页脚",
    "show_page_number": True,
    "show_title_page": True,
    "show_toc": True,
    "bidder_name": "测试投标人",
    "page_break_before_chapter": True,
    "line_spacing": 1.15,
    "page_number_style": "page_of_total",
    "toc_depth": 3,
    "margins": {"top": 2.5, "bottom": 2.5, "left": 2.5, "right": 2.5},
    "cover_info": {"工程名称": "结构校验工程", "编制单位": "测试单位"},
    "chart_fail_placeholder": False,
    "heading_border": True,
    "scheme_forms": {"compilation_note": True},
}

_S1_CONTENT = "\n".join(
    [
        "## 子标题",
        "正文含特殊字符 \x00\x01 与长串：" + "甲" * 800,
        "",
        "| 序号 | 名称 | 规格 |",
        "|---|---|---|",
        "| 1 | 螺栓 | M24 |",
        "| 2 | 电缆 | YJV-4x25 |",
        "",
        "```python",
        "x = 1",
        "```",
        "",
        "公式 $E=mc^2$ 与块级 $$\\frac{a}{b}$$ 混排。",
        "",
        "[CHART_TYPE: flowchart]",
        "",
        "1. 顶层一",
        "   - 子项",
        "2. 顶层二",
    ]
)


def _build_full_docx(out_path) -> str:
    """走生产构建入口（_build_docx_task → _build_docx_sync）生成一份全功能 DOCX。"""
    code = "graph TD\n A[开始]-->B[结束]"
    roots = [
        {"id": "s1", "parent_id": "", "level": 1, "sort_order": 0, "title": "第一章 总体概述", "content": _S1_CONTENT},
        {"id": "s2", "parent_id": "", "level": 1, "sort_order": 1, "title": "第二章 施工工艺", "content": "|a|b|\n|-|-|\n|1|2|\n"},
    ]
    prep = {
        "scheme": {"id": "s1", "name": "结构校验方案", "project_id": "p1"},
        "roots": roots,
        "children_map": {},
        "chart_lookup": {("s1", "flowchart"): code},
        "rendered_bytes": {("flowchart", code): io.BytesIO(_chart_png())},
        "blocks_cache": {r["id"]: E._parse_content_blocks(r.get("content") or "") for r in roots},
        "heading_styles": E._load_heading_styles({}),
        "image_bytes": {},
        "global_facts": [],
        "appendix_sources": [],
        "config": {},
        "docx_options": dict(_DOCX_OPTIONS),
    }
    stats = E._build_docx_sync(*E._build_docx_task(str(out_path), prep))
    assert isinstance(stats, dict)
    return str(out_path)


@pytest.fixture(scope="module")
def built_docx(tmp_path_factory):
    out = tmp_path_factory.mktemp("r183") / "structure.docx"
    return _build_full_docx(out)


def _package_names(path):
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def _local_tag(tag):
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_parts(path):
    with zipfile.ZipFile(path) as zf:
        return {
            n: ET.fromstring(zf.read(n))
            for n in zf.namelist()
            if n.endswith(".xml") or n.endswith(".rels")
        }


def _strict_violations(path):
    """按生产同一份 `_STRICT_CT_SEQUENCE` 检查全部 w: 部件。"""
    from docx.oxml.ns import qn

    hits = []
    xml_parts = _xml_parts(path)
    for part, root in xml_parts.items():
        if not part.startswith("word/") or not part.endswith(".xml"):
            continue
        for tag, seq in E._STRICT_CT_SEQUENCE.items():
            rank = {qn(f"w:{name}"): i for i, name in enumerate(seq)}
            for el in root.iter(qn(f"w:{tag}")):
                children = list(el)
                if len(children) < 2:
                    continue
                ranks = [rank.get(c.tag) for c in children]
                if any(r is None for r in ranks):
                    continue
                if ranks != sorted(ranks):
                    hits.append((part, tag, [_local_tag(c.tag) for c in children]))
    return hits


class TestBuiltPackageStructure:
    def test_required_parts_and_zip_crc(self, built_docx):
        with zipfile.ZipFile(built_docx) as zf:
            assert zf.testzip() is None
            names = set(zf.namelist())
        for req in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels"):
            assert req in names, f"缺少必需部件: {req}"

    def test_all_xml_parts_are_well_formed(self, built_docx):
        xml_parts = _xml_parts(built_docx)
        assert xml_parts, "包内没有任何 XML 部件"
        with zipfile.ZipFile(built_docx) as zf:
            xml_like = [n for n in zf.namelist() if n.endswith((".xml", ".rels"))]
        assert set(xml_like) == set(xml_parts)

    def test_content_types_cover_every_part(self, built_docx):
        with zipfile.ZipFile(built_docx) as zf:
            ct = ET.fromstring(zf.read("[Content_Types].xml"))
            names = set(zf.namelist())
        defaults = {(el.get("Extension") or "").lower() for el in ct.findall(f"{{{_CT_NS}}}Default")}
        overrides = {(el.get("PartName") or "").lstrip("/"): el.get("ContentType") for el in ct.findall(f"{{{_CT_NS}}}Override")}
        uncovered = []
        for name in names:
            if name == "[Content_Types].xml" or name.endswith(".rels"):
                continue
            ext = posixpath.basename(name).rsplit(".", 1)[-1].lower()
            if name not in overrides and ext not in defaults:
                uncovered.append(name)
        assert not uncovered, f"部件未被 [Content_Types].xml 覆盖: {uncovered}"
        assert overrides["word/document.xml"] == _WML_DOCUMENT_CT

    def test_all_relationship_targets_exist(self, built_docx):
        with zipfile.ZipFile(built_docx) as zf:
            names = set(zf.namelist())
            broken = []
            for rel_name in [n for n in names if n.endswith(".rels")]:
                rel_root = ET.fromstring(zf.read(rel_name))
                base_dir = posixpath.dirname(posixpath.dirname(rel_name))
                for rel in rel_root.findall(f"{{{_REL_NS}}}Relationship"):
                    if (rel.get("TargetMode") or "").lower() == "external":
                        continue
                    target = rel.get("Target") or ""
                    full = (
                        posixpath.normpath(target.lstrip("/"))
                        if target.startswith("/")
                        else posixpath.normpath(posixpath.join(base_dir, target))
                    )
                    if full not in names:
                        broken.append((rel_name, target, full))
        assert not broken, f"关系目标不存在: {broken}"

    def test_document_root_namespace(self, built_docx):
        with zipfile.ZipFile(built_docx) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
        assert root.tag == f"{{{_WML_NS}}}document"

    def test_no_strict_sequence_violations(self, built_docx):
        assert _strict_violations(built_docx) == []

    def test_update_fields_precedes_compat_and_rsids(self, built_docx):
        with zipfile.ZipFile(built_docx) as zf:
            root = ET.fromstring(zf.read("word/settings.xml"))
        tags = [_local_tag(c.tag) for c in root if isinstance(c.tag, str)]
        assert "updateFields" in tags
        for later in ("compat", "rsids"):
            if later in tags:
                assert tags.index("updateFields") < tags.index(later)

    def test_validator_accepts_built_package(self, built_docx):
        st = os.stat(built_docx)
        assert _docx_package_problems(built_docx) == []
        assert _zip_integrity_ok(built_docx, st.st_size, st.st_mtime_ns) is True
        assert _cache_row_viable(built_docx) is True

    def test_python_docx_can_reopen(self, built_docx):
        from docx import Document

        Document(built_docx)

    def test_caption_numbers_are_unique(self, built_docx):
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document(built_docx)
        fig = [
            p.text.strip()
            for p in doc.paragraphs
            if p.alignment == WD_ALIGN_PARAGRAPH.CENTER and re.match(r"^图\s*\d+-\d+", p.text.strip())
        ]
        tbl = [
            p.text.strip()
            for p in doc.paragraphs
            if p.alignment == WD_ALIGN_PARAGRAPH.CENTER and re.match(r"^表\s*\d+-\d+", p.text.strip())
        ]
        assert fig and tbl, "构建样本应包含图题与表题"
        fig_nums = [re.match(r"^图\s*(\d+-\d+)", t).group(1) for t in fig]
        tbl_nums = [re.match(r"^表\s*(\d+-\d+)", t).group(1) for t in tbl]
        assert [n for n, c in Counter(fig_nums).items() if c > 1] == []
        assert [n for n, c in Counter(tbl_nums).items() if c > 1] == []


def _write_broken(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return str(path)


_CT_SHELL = (
    "<?xml version='1.0'?>"
    "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
    "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
    "<Override PartName='/word/document.xml' ContentType='%s'/>"
    "</Types>" % _WML_DOCUMENT_CT
)
_RELS_MAIN = (
    "<?xml version='1.0'?>"
    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
    "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'"
    " Target='word/document.xml'/>"
    "</Relationships>"
)
_RELS_DOC = (
    "<?xml version='1.0'?>"
    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>"
)
_DOC_MIN = (
    "<?xml version='1.0'?>"
    "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body/></w:document>"
)


class TestCacheGuardRejectsBrokenPackages:
    """R183 前的真实误判：这四类文件 CRC 全部正常，旧判据一律返回 True。"""

    def _verdict(self, path):
        st = os.stat(path)
        return _zip_integrity_ok(path, st.st_size, st.st_mtime_ns)

    def test_empty_content_types_is_rejected(self, tmp_path):
        path = _write_broken(
            tmp_path / "empty_ct.docx",
            {
                "[Content_Types].xml": "<Types/>",
                "word/document.xml": _DOC_MIN,
            },
        )
        assert self._verdict(path) is False
        assert any("main-part-mime-invalid" in p or "ct-root-invalid" in p for p in _docx_package_problems(path))

    def test_unparsable_content_types_is_rejected(self, tmp_path):
        path = _write_broken(
            tmp_path / "bad_ct.docx",
            {
                "[Content_Types].xml": "<Types><Default",
                "word/document.xml": _DOC_MIN,
            },
        )
        assert self._verdict(path) is False

    def test_truncated_document_xml_is_rejected(self, tmp_path):
        path = _write_broken(
            tmp_path / "truncated.docx",
            {
                "[Content_Types].xml": _CT_SHELL,
                "_rels/.rels": _RELS_MAIN,
                "word/document.xml": _DOC_MIN[:40],
                "word/_rels/document.xml.rels": _RELS_DOC,
            },
        )
        assert self._verdict(path) is False
        assert any(p.startswith("xml-invalid:word/document.xml") for p in _docx_package_problems(path))

    def test_document_without_override_is_rejected(self, tmp_path):
        path = _write_broken(
            tmp_path / "no_override.docx",
            {
                "[Content_Types].xml": (
                    "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
                    "<Default Extension='xml' ContentType='application/xml'/></Types>"
                ),
                "_rels/.rels": _RELS_MAIN,
                "word/document.xml": _DOC_MIN,
                "word/_rels/document.xml.rels": _RELS_DOC,
            },
        )
        assert self._verdict(path) is False

    def test_complete_minimal_package_is_accepted(self, tmp_path):
        path = _write_broken(
            tmp_path / "valid_min.docx",
            {
                "[Content_Types].xml": _CT_SHELL,
                "_rels/.rels": _RELS_MAIN,
                "word/document.xml": _DOC_MIN,
                "word/_rels/document.xml.rels": _RELS_DOC,
            },
        )
        assert self._verdict(path) is True
        assert _docx_package_problems(path) == []
