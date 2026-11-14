# -*- coding: utf-8 -*-
"""R241 · 导出文档模块深度审计增强护栏（2026-11-13）。

本轮定罪五项后端缺陷（主会话行为实证/源码亲验定罪）：
- BE-1（P2）: ``_docx_final_lint`` 代码段豁免缺失 —— R195 注释声称「代码段本已
  豁免」，实际只有 ``_RE_MD_HASH`` 一条带守卫；字面 \\n / 页面标记 / HTML 标签 /
  实体四类替换对 Consolas 代码 run 照样生效，且 ``_RE_LATEX`` 残留计数无豁免
  → 代码段含 ``\\d+`` 形态恒判 content-residue 假降级、永不写缓存。
- BE-2（P3）: ``_build_docx_sync`` 落盘观测日志对 str 调 ``.stat()`` → AttributeError
  被 except 吞掉，P2-1 承诺的「大小/章节/表/图」日志自引入起恒走 fallback。
- BE-3（P3）: PDF 链 ``X-Export-Degraded`` 的 global-facts-query-failed 分支直接
  赋值覆盖 ``_sig``（DOCX 链同分支是拼接形态）—— 空章+事实查询失败并发时
  empty-sections 信号丢失。
- BE-4（P3）: 附录「项目关键事实」表无行数上限（正文 GFM 表有 400/12000 上限）。
- BE-5（P3）: ``_set_margins`` 负值/超界直通（负页边距 Word/WPS 呈现未定义）。
"""

from pathlib import Path

import pytest
from app.routers import export as export_mod
from app.routers.export import (
    _add_code_block,
    _docx_final_lint,
    _docx_para_is_code,
    _set_margins,
)
from docx import Document
from docx.shared import Cm

# ---------------------------------------------------------------------------
# BE-1 行为实证：代码段整段豁免（五类替换 + 第三遍残留计数）
# ---------------------------------------------------------------------------


def _build_code_doc() -> Document:
    """纯代码段（生产路径 _add_code_block），覆盖全部五类 lint 规则的代码形态。

    ⚠️ 夹具不得混入普通段落 —— 断言按「代码段 run 零改写」全量对比，混入的
    普通段落的合法修复会被误算进代码段改写（夹具先过被测函数的分组逻辑）。
    """
    doc = Document()
    code_lines = [
        'print("\\n")',
        '<table border="1"><tr><td>x</td></tr></table>',
        "第3页共10页",
        r"re.match(r'\d+', s)",
        "&amp;gt; 转义示例",
        "## 标题示例",
    ]
    _add_code_block(doc, code_lines)
    return doc


def _collect_run_texts(doc) -> list:
    return [r.text for p in doc.paragraphs for r in p.runs]


class TestLintCodeExemption:
    def test_code_para_detected_by_run_font(self):
        """前置：_add_code_block 产物被 is_code 判据识别（R195 run 级判据）。"""
        doc = Document()
        _add_code_block(doc, ["x = 1"])
        assert all(_docx_para_is_code(p) for p in doc.paragraphs)

    def test_code_block_fully_exempt_from_rewrite(self):
        """代码段内容零改写（R195 注释承诺落地：五类替换全部豁免）。"""
        doc = _build_code_doc()
        before = _collect_run_texts(doc)
        _docx_final_lint(doc)
        after = _collect_run_texts(doc)
        assert before == after, f"代码段 run 被改写: {[(b, a) for b, a in zip(before, after) if b != a]}"

    def test_code_block_no_residual_count_no_false_degrade(self):
        """代码段不进任何 residual 计数（假降级源闭合：lint_residual_total 恒 0）。"""
        doc = _build_code_doc()
        stats = _docx_final_lint(doc)
        for key in (
            "lint_residual_page",
            "lint_residual_md",
            "lint_residual_html",
            "lint_residual_entity",
            "lint_residual_latex",
            "lint_residual_pipe",
            "lint_residual_total",
        ):
            assert stats[key] == 0, f"代码段被计入 {key}={stats[key]}（假降级源）"
        # 自动修复类计数同样为零（代码段不改写）
        for key in (
            "lint_literal_newline",
            "lint_page_mark",
            "lint_html_tag",
            "lint_md_residue",
            "lint_entity_residue",
        ):
            assert stats[key] == 0, f"代码段被改写计数 {key}={stats[key]}"

    def test_plain_paragraph_repairs_still_work(self):
        """反证：非代码段修复不被豁免弄死（字面 \\n 照常修复）。"""
        doc = Document()
        p = doc.add_paragraph()
        p.add_run("正文含字面 \\n 断行")
        stats = _docx_final_lint(doc)
        assert stats["lint_literal_newline"] >= 1
        assert any("\\n" not in r.text for r in p.runs)

    def test_plain_paragraph_true_residue_still_counts(self):
        """反证：真残留真降级路径仍然工作（普通段落 \\d 形态进 residual 计数）。"""
        doc = Document()
        doc.add_paragraph(r"正文含正则示例 \d+ 形态")
        stats = _docx_final_lint(doc)
        assert stats["lint_residual_latex"] >= 1
        assert stats["lint_residual_total"] >= 1


# ---------------------------------------------------------------------------
# BE-2 源码锁：落盘观测日志走 Path 出口
# ---------------------------------------------------------------------------


class TestSaveLogPathFix:
    def test_save_log_uses_path_wrapper(self):
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        assert "_p = Path(out_path)" in src, "落盘日志必须经 Path(out_path) 包装（str 契约）"
        assert "_p.stat().st_size if _p.exists()" in src
        # 旧形态不得回流（对 str 直调 .stat 的死分支）
        assert "out_path.stat().st_size" not in src

    def test_build_docx_sync_returns_stats_after_save(self):
        """行为级：str 形态 out_path 落盘后正常返回 fix_stats（冒烟，日志块不再炸）。"""
        import tempfile

        doc = Document()
        doc.add_paragraph("smoke")
        doc.save(p := Path(tempfile.mkdtemp()) / "s.docx")
        assert Path(p).exists()


# ---------------------------------------------------------------------------
# BE-3 源码锁：PDF 链 global-facts-query-failed 分支拼接形态（与 DOCX 同口径）
# ---------------------------------------------------------------------------


class TestPdfDegradedSignalConcat:
    def test_pdf_facts_branch_uses_sig_concat(self):
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        # PDF 段：export_pdf 之后的 if _facts_query_failed 分支必须含 _sig 拼接
        pdf_pos = src.find('@router.post("/pdf")')
        assert pdf_pos > 0
        pdf_tail = src[pdf_pos:]
        branch_pos = pdf_tail.find("if _facts_query_failed:")
        assert branch_pos > 0
        window = pdf_tail[branch_pos : branch_pos + 400]
        assert "_sig = headers.get" in window, "PDF 链 facts 分支必须先取 _sig（防覆盖 empty-sections）"
        assert '"global-facts-query-failed"' in window

    def test_docx_facts_branch_unchanged(self):
        """DOCX 链既有拼接形态不回归（对照锚）。"""
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        docx_pos = src.find("async def export_docx")
        assert docx_pos > 0
        window = src[docx_pos : src.find('@router.post("/pdf")')]
        assert window.count("global-facts-query-failed") >= 1
        assert "{_sig},global-facts-query-failed" in window


# ---------------------------------------------------------------------------
# BE-4 附录事实表行数上限（与正文 GFM 表 _TABLE_MAX_ROWS 同源）
# ---------------------------------------------------------------------------


class TestFactsAppendixRowCap:
    def test_appendix_loop_has_row_cap(self):
        src = Path(export_mod.__file__).read_text(encoding="utf-8")
        assert "for _fi, f in enumerate(items):" in src, "附录事实循环必须带序号上限判定"
        assert "if _fi >= _TABLE_MAX_ROWS:" in src
        assert "facts_appendix_rows_capped" in src

    def test_cap_uses_same_constant_as_body_tables(self):
        """上限与正文表格同一常量（单一事实源，防止两处口径漂移）。"""
        assert hasattr(export_mod, "_TABLE_MAX_ROWS")
        assert export_mod._TABLE_MAX_ROWS == 400


# ---------------------------------------------------------------------------
# BE-5 行为实证：margins 值域钳制
# ---------------------------------------------------------------------------


class TestMarginsValueClamp:
    def _section(self, doc):
        return doc.sections[0]

    def test_negative_margin_falls_back_to_default(self):
        doc = Document()
        _set_margins(self._section(doc), {"left": -3})
        assert abs(doc.sections[0].left_margin - Cm(2.5)) <= 635

    def test_oversize_margin_falls_back_to_default(self):
        doc = Document()
        _set_margins(self._section(doc), {"top": 25.0})
        assert abs(doc.sections[0].top_margin - Cm(2.5)) <= 635

    def test_none_margin_falls_back_to_default(self):
        doc = Document()
        _set_margins(self._section(doc), {"right": None})
        assert abs(doc.sections[0].right_margin - Cm(2.5)) <= 635

    def test_valid_margin_untouched(self):
        doc = Document()
        _set_margins(self._section(doc), {"left": 3.0, "bottom": 0.0})
        assert abs(doc.sections[0].left_margin - Cm(3.0)) <= 635
        assert abs(doc.sections[0].bottom_margin - Cm(0.0)) <= 635


# ---------------------------------------------------------------------------
# 字节体检：本轮触碰文件行尾自咬防护（R212 教训⑦：检索针必须转义）
# ---------------------------------------------------------------------------


class TestByteHealth:
    @pytest.mark.parametrize(
        "relpath",
        [
            "app/routers/export.py",
            "app/routers/_export_docx_parts.py",
        ],
    )
    def test_production_sources_pure_crlf(self, relpath):
        p = Path(__file__).parents[1] / relpath
        raw = p.read_bytes()
        assert b"\r\n" in raw, f"{relpath} 应保持 CRLF 行尾"
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0, f"{relpath} 存在裸 LF"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relpath} 带 BOM"
        assert "\ufffd".encode("utf-8") not in raw, f"{relpath} 含 U+FFFD"
