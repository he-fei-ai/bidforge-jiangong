# -*- coding: utf-8 -*-
"""R128（2026-10-22）：交叉引用 REF 域收口回归（遗留 3）。

覆盖：
  A 组 行为：正文「图 X-Y / 表 X-Y」引用 → 5-run REF 域（instrText 含书签、
      \\h 超链接、域结果 = 原匹配文本，可见文本逐字节不变）；
  B 组 反例：未登记编号 / 书签被回滚 → 保持纯文本（绝不生成坏域）；
  C 组 题注：图题/表题编号前缀加书签、题注自身段不被包域、书签名唯一；
  D 组 接线与兼容：_build_docx_sync 内回填先于严格序列归一（静态锁）+
      E2E（表格引用包域、可见文本不变、python-docx 重开）；
  E 组 版本/统计：v29 锚 + cross_ref_fields 统计键。
"""
import io
import zipfile

import pytest
from app.routers import export as E
from app.routers.export import (
    _add_caption_text_with_ref_bookmark,
    _add_runs_with_inline_format,
    _add_table_caption,
    _backfill_cross_reference_fields,
    _build_docx_sync,
    _insert_image_with_caption,
    _load_heading_styles,
    _normalize_docx_strict_sequence,
    _parse_content_blocks,
    _reset_refx,
)
from docx import Document
from docx.oxml.ns import qn


def _mk_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, format="PNG", dpi=(300, 300))
    buf.seek(0)
    return buf


def _instr_texts(doc):
    return [el.text or "" for el in doc.element.body.iter(qn("w:instrText"))]


def _visible_text(doc):
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


def _ref_flds(doc):
    return [t for t in _instr_texts(doc) if " REF " in t]


@pytest.fixture(autouse=True)
def _fresh_refx():
    _reset_refx()
    yield
    _reset_refx()


# ============================================================
# A 组：正文引用 → REF 域（行为实证）
# ============================================================
class TestBodyRefField:
    def test_figure_reference_becomes_ref_field(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "支架搭设如图 3-1 所示，详见下文。")
        assert _insert_image_with_caption(
            doc, _mk_png(), "图 3-1 支架搭设流程", log_label="t") is True
        # 回填前：无 REF 域
        assert _ref_flds(doc) == []
        assert _backfill_cross_reference_fields(doc) == 1
        flds = _ref_flds(doc)
        assert len(flds) == 1
        assert "_Ref_fig_3_1" in flds[0] and "\\h" in flds[0]
        # 可见文本逐字节不变（域结果 = 原匹配文本）
        assert "支架搭设如图 3-1 所示，详见下文。" in _visible_text(doc)

    def test_field_is_five_run_structure(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "如图 3-1 所示")
        _insert_image_with_caption(doc, _mk_png(), "图 3-1 流程", log_label="t")
        _backfill_cross_reference_fields(doc)
        target = None
        for para in doc.paragraphs:
            if any(" REF " in (it.text or "") for it in para._p.iter(qn("w:instrText"))):
                target = para
                break
        assert target is not None
        kinds = [fc.get(qn("w:fldCharType"))
                 for fc in target._p.iter(qn("w:fldChar"))]
        assert kinds == ["begin", "separate", "end"]

    def test_table_reference_becomes_ref_field(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "人力配置详见表 2-1。")
        _add_table_caption(doc, "表 2-1 劳动力配置计划", font_size=10.5)
        assert _backfill_cross_reference_fields(doc) == 1
        flds = _ref_flds(doc)
        assert len(flds) == 1 and "_Ref_tbl_2_1" in flds[0]
        assert "人力配置详见表 2-1。" in _visible_text(doc)

    def test_table_reference_compact_form_also_matched(self):
        """紧排「表1-1」同样命中（\\s* 零空格形态）。"""
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "见表1-1备注栏。")
        _add_table_caption(doc, "表 1-1 材料", font_size=10.5)
        assert _backfill_cross_reference_fields(doc) == 1
        assert "见表1-1备注栏。" in _visible_text(doc)

    def test_two_references_in_one_run_both_wrapped(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "先见表 1-1，再看表 1-2 附表。")
        _add_table_caption(doc, "表 1-1 主表", font_size=10.5)
        _add_table_caption(doc, "表 1-2 附表", font_size=10.5)
        assert _backfill_cross_reference_fields(doc) == 2
        assert len(_ref_flds(doc)) == 2
        assert "先见表 1-1，再看表 1-2 附表。" in _visible_text(doc)

    def test_stats_counter_incremented(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "如图 3-1 所示")
        _insert_image_with_caption(doc, _mk_png(), "图 3-1 流程", log_label="t")
        _backfill_cross_reference_fields(doc)
        assert E._fix_stats()["cross_ref_fields"] >= 1


# ============================================================
# B 组：反例 —— 未登记 / 已回滚 → 纯文本（绝不生成坏域）
# ============================================================
class TestPlainFallback:
    def test_unregistered_number_stays_plain(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "如图 9-9 所示（本章无此图）。")
        assert _backfill_cross_reference_fields(doc) == 0
        assert _ref_flds(doc) == []
        assert "如图 9-9 所示" in _visible_text(doc)

    def test_no_bookmark_at_all_returns_zero(self):
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "如图 3-1 所示")
        assert _backfill_cross_reference_fields(doc) == 0

    def test_mixed_registered_and_unregistered_in_same_paragraph(self):
        """同段混排：已登记编号包域、未登记编号保持纯文本。

        ⚠️ 这是存在性校验（①判据）的**唯一真判据** —— B 组其余用例的文档
        没有任何书签（existing 为空走早退），摘掉存在性校验的变异对它们
        结构失明（A/B M2 首跑 NOT-CAUGHT 实证）。
        """
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "见表 1-1 主表；历史编号见表 9-9 已废弃。")
        _add_table_caption(doc, "表 1-1 主表", font_size=10.5)
        assert _backfill_cross_reference_fields(doc) == 1
        flds = _ref_flds(doc)
        assert len(flds) == 1 and "_Ref_tbl_1_1" in flds[0]
        # 未登记编号保持纯文本（坏域会让 Word 报「引用源不存在」）
        assert "历史编号见表 9-9 已废弃" in _visible_text(doc)

    def test_removed_bookmark_leaves_plain_text(self):
        """书签被删（模拟题注被 rollback）→ 存在性校验兜住，不生成坏域。"""
        doc = Document()
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "如图 3-1 所示")
        assert _insert_image_with_caption(
            doc, _mk_png(), "图 3-1 流程", log_label="t") is True
        assert _ref_flds(doc) == []
        # 模拟题注段整体被回滚：删除全部 _Ref_ 书签
        for tag in ("w:bookmarkStart", "w:bookmarkEnd"):
            for el in list(doc.element.body.iter(qn(tag))):
                if tag == "w:bookmarkStart" and not (el.get(qn("w:name")) or "").startswith("_Ref_"):
                    continue
                el.getparent().remove(el)
        assert _backfill_cross_reference_fields(doc) == 0
        assert _ref_flds(doc) == []
        assert "如图 3-1 所示" in _visible_text(doc)

    def test_no_prefix_caption_keeps_no_bookmark(self):
        """无编号前缀的题注（历史形态）走旧路径：整串单 run、无书签。"""
        doc = Document()
        _add_table_caption(doc, "示意表格", font_size=10.5)
        bms = [el for el in doc.element.body.iter(qn("w:bookmarkStart"))
               if (el.get(qn("w:name")) or "").startswith("_Ref_")]
        assert bms == []


# ============================================================
# C 组：题注书签（不可见元素、自身不被包域、签名唯一）
# ============================================================
class TestCaptionBookmarks:
    def test_figure_caption_has_bookmark_around_number(self):
        doc = Document()
        assert _insert_image_with_caption(
            doc, _mk_png(), "图 3-1 支架搭设流程", log_label="t") is True
        caps = [p for p in doc.paragraphs
                if any((bs.get(qn("w:name")) or "").startswith("_Ref_")
                       for bs in p._p.findall(qn("w:bookmarkStart")))]
        assert len(caps) == 1
        names = [bs.get(qn("w:name")) for bs in caps[0]._p.findall(qn("w:bookmarkStart"))]
        assert names == ["_Ref_fig_3_1"]
        # 书签包住的是编号 run 而非整条题注
        run_texts = [t.text or "" for t in caps[0]._p.iter(qn("w:t"))]
        assert run_texts[0] == "图 3-1"
        assert "支架搭设流程" in "".join(run_texts[1:])
        # 题注自身段不被包域
        assert caps[0]._p.findall(".//" + qn("w:fldChar")) == []

    def test_table_caption_has_bookmark(self):
        doc = Document()
        _add_table_caption(doc, "表 2-1 劳动力配置计划", font_size=10.5)
        names = [bs.get(qn("w:name")) for bs in doc.element.body.iter(qn("w:bookmarkStart"))
                 if (bs.get(qn("w:name")) or "").startswith("_Ref_")]
        assert names == ["_Ref_tbl_2_1"]

    def test_bookmark_ids_unique(self):
        doc = Document()
        _insert_image_with_caption(doc, _mk_png(), "图 3-1 A", log_label="t")
        _insert_image_with_caption(doc, _mk_png(), "图 3-2 B", log_label="t")
        _add_table_caption(doc, "表 2-1 C", font_size=10.5)
        ids = [el.get(qn("w:id")) for el in doc.element.body.iter(qn("w:bookmarkStart"))
               if (el.get(qn("w:name")) or "").startswith("_Ref_")]
        assert len(ids) == 3 and len(set(ids)) == 3

    def test_backfill_skips_caption_paragraph_even_if_number_repeats(self):
        """题注段里再次出现同编号（病态题注）也不嵌套包域。"""
        doc = Document()
        _add_table_caption(doc, "表 2-1 表 2-1 说明", font_size=10.5)
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, "见表 2-1。")
        assert _backfill_cross_reference_fields(doc) == 1  # 只有正文 1 处
        assert len(_ref_flds(doc)) == 1



# ============================================================
# D 组：接线与兼容（E2E + 静态锁 + fail-soft）
# ============================================================
_E2E_CONTENT = (
    "施工安排详见表 1-1，材料要求见表 1-1 备注。\n"
    "\n"
    "| 项目 | 要求 | 备注 |\n"
    "| --- | --- | --- |\n"
    "| 混凝土 | C30 | 抗渗 P6 |\n"
)


def _build_e2e(tmp_path, name="r128.docx"):
    sections = [{
        "id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
        "title": "第一章 总体概述", "content": _E2E_CONTENT,
    }]
    blocks = {"c1": _parse_content_blocks(_E2E_CONTENT)}
    out = str(tmp_path / name)
    _build_docx_sync(
        out,
        {"id": "s1", "project_id": "p1", "name": "R128 交叉引用验证方案"},
        sections, {}, {}, {}, blocks,
        "宋体", 12, "R128 页眉", "", True,
        True, True,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        page_break_before_chapter=True,
        line_spacing=1.5,
        page_number_style="page_of_total",
        toc_depth=3,
        cover_info={"项目名称": "R128 验证项目", "编制单位": "测试单位"},
    )
    return out


class TestWiring:
    def test_e2e_table_refs_become_fields(self, tmp_path):
        out = _build_e2e(tmp_path)
        doc = Document(out)
        flds = _ref_flds(doc)
        assert len(flds) == 2
        assert all("_Ref_tbl_1_1" in f for f in flds)
        assert "施工安排详见表 1-1，材料要求见表 1-1 备注。" in _visible_text(doc)

    def test_e2e_doc_reopens_and_zip_intact(self, tmp_path):
        out = _build_e2e(tmp_path)
        Document(out)  # python-docx 重开
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None

    def test_wiring_static_lock_backfill_before_normalize(self):
        """静态锁：回填必须位于 _build_docx_sync 内、严格序列归一之前。"""
        import inspect
        src = inspect.getsource(_build_docx_sync)
        i_fill = src.index("_backfill_cross_reference_fields(doc)")
        i_norm = src.index("_normalize_docx_strict_sequence(doc)")
        assert i_fill < i_norm

    def test_backfill_is_fail_soft_on_garbage(self):
        """入参垃圾 → fail-soft 返回 0，绝不抛异常阻断导出。"""
        class _Boom:
            def iter(self, tag):
                raise RuntimeError("boom")
        assert _backfill_cross_reference_fields(_Boom()) == 0


# ============================================================
# E 组：版本 / 统计键
# ============================================================
class TestVersionAndStats:
    def test_version_bumped_to_29(self):
        # ✅ R129（2026-10-22）：v29 → v30（Word 2010+ 命名空间清除 + L1~L9 +
        # 公式图片降级 —— 均改变产物字节，REF 域契约不变，本组其余断言不受影响）。
        # ✅ R131-A（2026-10-23）：v30 → v31（图题/配图 alt 剥离生成期「图 X-Y」
        # 前缀，可见题注文本由双图号变单图号 —— 旧缓存内含错号必须整体失效；
        # REF 域登记/回填契约不变，本组其余断言不受影响）。
        assert E._EXPORTER_VERSION == "31"

    def test_stats_key_registered(self):
        assert "cross_ref_fields" in E._FIX_STATS_KEYS
        # 初始化覆盖全键（顺序无关，线程局部残留免疫 —— 单测线程内先前的
        # A 组用例可能已累计过计数，这里只验证「键在初始化字典里」）。
        st = E._fix_stats()
        assert set(st) == set(E._FIX_STATS_KEYS)

    def test_legacy_stats_keys_untouched(self):
        for k in ("formulas", "block_formulas", "replacement_chars",
                  "control_chars", "gbk_mojibake", "latin1_mojibake",
                  "cyrillic", "block_render_failed", "table_truncated",
                  "fences_fixed", "repeated_blocks", "near_duplicate_blocks"):
            assert k in E._FIX_STATS_KEYS, k

