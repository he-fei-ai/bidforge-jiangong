# -*- coding: utf-8 -*-
"""R65（2026-10-11）护栏 —— 引入《文档生成规则分析与软件标准建立》的三项未落地增强。

背景：对照该标准文档的 15 项增强清单逐条盘点，12 项本仓已在位
（泛化/占位标题禁用与检测、Markdown 清理、表格表头加粗+底纹+跨页重复、
图号「章-序」按章重编、规范引用清单校验、空话套话检测、术语归一字典、
文档模板库、TOC 对齐、页眉页脚、封面、章节标题去重）。本轮补齐三项**缺口**：

A. 目录结构规范（文档 1.1 CHAPTER_COUNT / SECTION_PER_CHAPTER / MAX_DEPTH）
   —— `outline_quality.check_structure_standards` 建议性核对，
   **只报不阻断**（微型专项方案 4 章属合法形态）。
B. 正文段落结构校验（文档 2.1 PARAGRAPH_LENGTH / DATA_FORMATS）
   —— `content_standard.paragraph_structure_report` 咨询性校验，
   写进生成标准报告新键 ``structure``，不动正文、不改 error/warning 计数。
C. 导出版式预设（文档 4.2 ExportStandard：行距 1.5、上下 2.54cm、左右 3.18cm）
   —— `export.LAYOUT_PRESETS` + `_resolve_layout_preset`，默认**不启用**，
   既有 2.5cm/1.15 默认逐字不变；`layout_preset` 纳入导出指纹白名单。

纪律：三处全部加法式、纯函数、fail-soft；本文件同时锁住
「建议绝不翻转既有 ok 结论」与「常量不得同名遮蔽」（本轮真实踩过的雷：
首版 `_FENCE_RE` 覆盖模块级围栏正则，打挂 strip_code_blocks 三条既有护栏）。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import tempfile

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app.services.content_standard import (  # noqa: E402
    PARA_MAX_CHARS,
    paragraph_structure_report,
)
from app.services.outline_quality import (  # noqa: E402
    OUTLINE_CHAPTER_MAX,
    OUTLINE_CHAPTER_MIN,
    OUTLINE_MAX_DEPTH,
    OUTLINE_SECTION_MIN,
    check_outline_continuity,
    check_structure_standards,
    render_structure_notice,
)


def _tree(path: str) -> ast.Module:
    with io.open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _fn_src(path: str, name: str, cls: str = ast.FunctionDef) -> str:
    for node in _tree(path).body:
        if isinstance(node, cls) and node.name == name:
            with io.open(path, encoding="utf-8") as f:
                return ast.get_source_segment(f.read(), node) or ""
    raise AssertionError(f"未找到函数 {name}")


# ===========================================================================
# A) 目录结构规范：check_structure_standards
# ===========================================================================
class TestOutlineStructureStandards:

    def _mk(self, n_ch: int, n_sec: int = 3, depth: int = 3) -> list:
        # 标题全文唯一：重名会被连续性校验判 duplicate_titles，
        # 干扰「结构建议不影响连续性结论」这一核心断言。
        def _level(d: int, prefix: str) -> list:
            if d >= depth:
                return []
            rows = []
            for i in range(n_sec):
                name = f"{prefix}{d}-{i}"
                rows.append({"title": name, "level": d + 1,
                             "children": _level(d + 1, name + ".")})
            return rows
        return [{"title": f"第{i}章 专项工程", "level": 1,
                 "children": _level(1, f"ch{i}.")}
                for i in range(1, n_ch + 1)]

    def test_compliant_outline_produces_no_warnings(self):
        rep = check_structure_standards(self._mk(7, 3, 3))
        assert rep["evaluated"] is True
        assert rep["chapters"] == 7 and rep["max_depth"] == 3
        assert rep["warnings"] == []
        assert render_structure_notice(rep) == ""

    def test_constants_match_standard_doc(self):
        # 文档 1.1：5-10 章 / 最多 4 级；下限 2 取本仓提示词更严契约
        assert (OUTLINE_CHAPTER_MIN, OUTLINE_CHAPTER_MAX) == (5, 10)
        assert OUTLINE_MAX_DEPTH == 4
        assert OUTLINE_SECTION_MIN == 2

    def test_too_few_chapters_flagged_advisory_only(self):
        outline = self._mk(4, 3, 3)
        rep = check_structure_standards(outline)
        kinds = {w["kind"] for w in rep["warnings"]}
        assert kinds == {"chapter_count"}
        assert rep["warnings"][0]["count"] == 4
        # 关键契约：结构建议不得翻转连续性 ok（该目录本身连续性完好）
        assert check_outline_continuity(outline)["ok"] is True

    @pytest.mark.parametrize("n_ch", [1, 4, 5, 10, 11, 24])
    def test_chapter_count_boundary_table(self, n_ch):
        rep = check_structure_standards(self._mk(n_ch, 2, 2))
        flagged = any(w["kind"] == "chapter_count" for w in rep["warnings"])
        assert flagged is not (OUTLINE_CHAPTER_MIN <= n_ch <= OUTLINE_CHAPTER_MAX)

    def test_depth_over_four_flagged(self):
        rep = check_structure_standards(self._mk(6, 2, 5))
        assert rep["max_depth"] == 5
        assert "max_depth" in {w["kind"] for w in rep["warnings"]}

    def test_section_count_flagged_per_chapter(self):
        # 第 1 章仅 1 个二级节（<2），其余章 2 个：只报 1 条
        outline = self._mk(5, 2, 2)
        outline[0]["children"] = outline[0]["children"][:1]
        rep = check_structure_standards(outline)
        sec_warns = [w for w in rep["warnings"] if w["kind"] == "section_count"]
        assert len(sec_warns) == 1 and sec_warns[0]["path"] == "1"

    def test_chapter_without_children_not_flagged(self):
        # 直属单章无子节：空父壳由连续性校验的 empty_parents 负责，这里不重复判
        outline = self._mk(5, 2, 2)
        outline[2]["children"] = []
        rep = check_structure_standards(outline)
        assert "section_count" not in {w["kind"] for w in rep["warnings"]
                                      if w["path"] == "3"}

    def test_empty_and_none_never_raise(self):
        for bad in (None, [], ["x", None], [{"title": ""}], [{}]):
            rep = check_structure_standards(bad)
            assert set(rep) >= {"evaluated", "chapters", "max_depth", "warnings"}

    def test_advisory_never_touches_continuity_source(self):
        # AST 级：check_outline_continuity 函数体内不得引用结构规范
        # （一旦被引用，结构建议就会渗进 ok 判定，微型方案被误判不合格）
        src = _fn_src(os.path.join(BACKEND, "app", "services", "outline_quality.py"),
                      "check_outline_continuity")
        assert "check_structure_standards" not in src

    def test_notice_lists_messages_truncated(self):
        rep = check_structure_standards(self._mk(12, 1, 2))
        notice = render_structure_notice(rep)
        assert notice.startswith("目录结构规范建议")
        assert notice.count("；") <= 5  # 最多 6 条


# ===========================================================================
# B) 正文段落结构校验：paragraph_structure_report
# ===========================================================================
class TestParagraphStructure:

    def test_clean_engineering_text_gets_no_warnings(self):
        rep = paragraph_structure_report(
            "基坑支护采用 φ800 钻孔灌注桩，桩长 18m，混凝土 C30。\n\n"
            "计划于2026年1月5日开工，总工期 450 日历天，围护桩养护 7 天。")
        assert rep["warnings"] == []
        assert rep["long_paragraphs"] == 0
        assert rep["paragraphs"] == 2

    def test_long_paragraph_flagged_and_capped(self):
        rep = paragraph_structure_report("措施要求。" * 200)  # 1000 字整段
        assert rep["long_paragraphs"] == 200 // PARA_MAX_CHARS + 1
        lp = [w for w in rep["warnings"] if w["type"] == "long_paragraph"]
        assert len(lp) == 1  # 报告封顶 3 条，防长文刷屏
        assert lp[0]["severity"] == "advisory"

    def test_many_long_paragraphs_capped_at_three(self):
        body = ("甲" * 600 + "\n\n" + "乙" * 600 + "\n\n"
                + "丙" * 600 + "\n\n" + "丁" * 600)
        rep = paragraph_structure_report(body)
        assert rep["long_paragraphs"] == 4
        assert len([w for w in rep["warnings"]
                    if w["type"] == "long_paragraph"]) == 3

    def test_headings_lists_and_tables_excluded(self):
        md = ("# 第一章 工程概况\n\n"
              "- 支护形式：排桩加内支撑\n- 降水方式：管井降水\n\n"
              "| 工序 | 工期 |\n| --- | --- |\n| 开挖 | 120天 |\n")
        rep = paragraph_structure_report(md)
        # 表格行里的「120天」不进入散文文本
        assert [w for w in rep["warnings"] if w["type"] == "duration_unit"] == []

    def test_dates_norm_compliant_forms_pass(self):
        for ok in ("2026年1月5日", "2027-03-15"):
            assert [w for w in paragraph_structure_report(
                f"{ok}开工。")["warnings"] if w["type"] == "date_format"] == []
        for bad in ("2026/1/5", "2026.1.5", "2026-1-5"):
            rep = paragraph_structure_report(f"计划{bad}进场施工。")
            hits = [w for w in rep["warnings"] if w["type"] == "date_format"]
            assert len(hits) == 1 and bad in hits[0]["message"]

    def test_duplicate_bad_dates_deduped(self):
        rep = paragraph_structure_report("2026/1/5 与 2026/1/5 同日两提。")
        assert len([w for w in rep["warnings"] if w["type"] == "date_format"]) == 1

    def test_duration_unit_only_when_context_and_no_calendar_day(self):
        rep = paragraph_structure_report("总工期 120 天，逾期违约。")
        hits = [w for w in rep["warnings"] if w["type"] == "duration_unit"]
        assert len(hits) == 1 and "120 日历天" in hits[0]["message"]
        # 窗口内已有「日历天」口径 ⇒ 不判（宁可少报）
        rep2 = paragraph_structure_report("总工期 120 天（即 120 日历天）完成。")
        assert [w for w in rep2["warnings"] if w["type"] == "duration_unit"] == []
        # 无工期语义词的裸「N天」不判
        assert [w for w in paragraph_structure_report(
            "闲置 3 天后复用。")["warnings"] if w["type"] == "duration_unit"] == []

    def test_unclosed_fence_fallback_drops_inner_dates(self):
        # strip_code_blocks 对未闭合围栏无能为力，这里的逐行兜底必须生效
        rep = paragraph_structure_report("正文正常。\n\n```mermaid\n2099/9/9 x")
        assert [w for w in rep["warnings"] if w["type"] == "date_format"] == []

    def test_garbage_input_never_raises(self):
        for bad in (None, "", 12345, "```\n\n\n```"):
            rep = paragraph_structure_report(bad if isinstance(bad, str) else "")
            assert set(rep) == {"paragraphs", "recommended_range",
                               "long_paragraphs", "warnings"}

    def test_advisory_types_stay_in_declared_value_set(self):
        rep = paragraph_structure_report(
            "于2026/1/5进场，总工期120天。\n\n" + "长" * 600)
        assert {w["type"] for w in rep["warnings"]} <= {
            "long_paragraph", "date_format", "duration_unit"}
        assert all(w["severity"] == "advisory" for w in rep["warnings"])


# ===========================================================================
# C) 导出版式预设：layout_preset
# ===========================================================================
class TestLayoutPreset:

    def _base(self) -> dict:
        return {"line_spacing": 1.15, "margins": None, "font_name": "宋体",
                "font_size": 12}

    def test_absent_preset_returns_equal_dict(self):
        from app.routers.export import _resolve_layout_preset
        base = self._base()
        out = _resolve_layout_preset({}, base)
        assert out == base
        assert out is not base, "必须返回新 dict，不得就地改写调用方选项"

    def test_engineering_preset_matches_standard_doc(self):
        from app.routers.export import LAYOUT_PRESETS, _resolve_layout_preset
        out = _resolve_layout_preset({"layout_preset": "engineering"},
                                     self._base())
        assert out["line_spacing"] == LAYOUT_PRESETS["engineering"]["line_spacing"] == 1.5
        mg = out["margins"]
        assert (mg["top"], mg["bottom"]) == (2.54, 2.54)
        assert (mg["left"], mg["right"]) == (3.18, 3.18)

    def test_preset_name_is_case_and_space_tolerant(self):
        from app.routers.export import _resolve_layout_preset
        out = _resolve_layout_preset({"layout_preset": " Engineering "},
                                     self._base())
        assert out["line_spacing"] == 1.5

    def test_unknown_preset_noop(self):
        from app.routers.export import _resolve_layout_preset
        base = self._base()
        assert _resolve_layout_preset({"layout_preset": "公文"}, base) == base

    def test_user_margin_keys_win_over_preset(self):
        from app.routers.export import _resolve_layout_preset
        out = _resolve_layout_preset(
            {"layout_preset": "engineering",
             "margins": {"top": 2.0, "bogus": "x", "bottom": None}},
            self._base())
        assert out["margins"]["top"] == 2.0          # 显式细调保留
        assert out["margins"]["left"] == 3.18        # 未细调的键由预设补
        assert "bogus" not in out["margins"]         # 非数值脏键丢弃
        assert out["margins"]["bottom"] == 2.54      # None 回落预设

    def test_layout_preset_in_fingerprint_whitelist(self):
        from app.routers.export import _EXPORT_CONFIG_KEYS
        assert "layout_preset" in _EXPORT_CONFIG_KEYS, \
            "不入指纹白名单 ⇒ 切预设命中旧缓存，预设形同虚设"

    def test_preset_applied_at_docx_and_pdf_convergence(self):
        # _build_docx_task 是两条链路唯一收敛点，必须在这里套预设
        src = _fn_src(os.path.join(BACKEND, "app", "routers", "export.py"),
                      "_build_docx_task")
        assert "_resolve_layout_preset(prep.get(\"config\") or {}, prep[\"docx_options\"])" in src

    def test_default_docx_bytes_ignore_preset_code_path(self, tmp_path):
        """未配置预设时构建参数与改造前同值：行距 1.15、边距走 2.5 缺省。"""
        from docx import Document
        from docx.shared import Cm
        from app.routers.export import (
            _build_docx_sync, _load_heading_styles, _parse_content_blocks,
            _resolve_layout_preset,
        )
        content = "本章为施工工艺技术内容，参数按专项计算书核定。"
        sections = [{"id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
                     "title": "第一章 施工工艺技术", "content": content}]
        blocks = {"c1": _parse_content_blocks(content)}

        def _build(opts: dict) -> str:
            out = str(tmp_path / f"preset_{opts['line_spacing']}.docx")
            _build_docx_sync(
                out, {"id": "s1", "project_id": "p1",
                      "name": "深基坑开挖专项施工方案"},
                sections, {}, {}, {}, blocks,
                opts["font_name"], opts["font_size"], "", "",
                False, False, False,
                bidder_name="",
                heading_styles=_load_heading_styles({}),
                page_break_before_chapter=True,
                line_spacing=opts["line_spacing"],
                page_number_style="simple", toc_depth=3,
                margins=opts["margins"],
            )
            return out

        base = _resolve_layout_preset({}, self._base())
        eng = _resolve_layout_preset({"layout_preset": "engineering"},
                                     self._base())
        # 未配置预设且 margins=None 时，_build_docx_sync 不动页面设置
        # （`if margins:` 短路），产物即 python-docx 空白文档的默认版式 ——
        # 与改造前逐字一致。配置预设后左右 3.18cm / 上下 2.54cm / 行距 1.5。
        from docx import Document as _Blank
        d_blank = _Blank()
        d0 = Document(_build(base))
        assert d0.sections[0].left_margin == d_blank.sections[0].left_margin
        assert d0.styles["Normal"].paragraph_format.line_spacing == 1.15
        d1 = Document(_build(eng))
        # Cm(3.18) 非整数 EMU（1144800），python-docx 写入按 1cm=360000 取整
        # 读回 1144905 ⇒ 用 ±150 EMU 容差判等，不做假精确断言。
        assert abs(d1.sections[0].left_margin - Cm(3.18)) <= 150
        assert abs(d1.sections[0].top_margin - Cm(2.54)) <= 150
        assert d1.styles["Normal"].paragraph_format.line_spacing == 1.5


# ===========================================================================
# D) 生成链路接线：只报不阻断
# ===========================================================================
class TestGenerationWiring:

    SSE = os.path.join(BACKEND, "app", "routers", "sse_handlers.py")

    def test_outline_advisory_wired_into_quality_report(self):
        src = _fn_src(self.SSE, "_attach_quality_report")
        assert "struct = check_structure_standards(outline)" in src
        assert '"structure": struct' in src
        # 建议只进 info 级日志；连续性未通过仍是唯一 warning
        assert 'logger.warning("%s（仅建议，不阻断）"' not in src

    def test_paragraph_advisory_wired_post_generation(self):
        src = ""
        with io.open(self.SSE, encoding="utf-8") as f:
            src = f.read()
        assert "report[\"structure\"] = _struct_rep" in src
        i_std = src.find("report = standard_report(")
        i_par = src.find("_struct_rep = paragraph_structure_report(content)")
        assert 0 < i_std < i_par, "段落校验必须排在 standard_report 之后，不得被其重建覆盖"
        assert "段落结构建议" in src and "仅咨询，不阻断" in src

    def test_continuity_ok_semantics_unchanged(self):
        # 同一份「4 章合规目录」（level 齐备、无跳级、标题唯一）：
        # structure 有建议、continuity 仍 ok —— 章数偏少不得把目录判成不合格
        outline = [{"title": f"第{i}章 脚手架工程", "level": 1, "children": [
            {"title": f"{i}a 搭设工艺", "level": 2, "children": [{"title": f"{i}a1 基座检查"}]},
            {"title": f"{i}b 拆除工艺", "level": 2, "children": [{"title": f"{i}b1 清运顺序"}]}]}
            for i in range(1, 5)]
        assert check_outline_continuity(outline)["ok"] is True
        assert check_structure_standards(outline)["warnings"]


# ===========================================================================
# E) 字节体检 + 常量遮蔽回归（本轮真实踩过的雷）
# ===========================================================================
TOUCHED = [
    os.path.join(BACKEND, "app", "services", "outline_quality.py"),
    os.path.join(BACKEND, "app", "services", "content_standard.py"),
    os.path.join(BACKEND, "app", "routers", "sse_handlers.py"),
    os.path.join(BACKEND, "app", "routers", "export.py"),
    os.path.join(BACKEND, "tests", "test_outline_name_line_20260927.py"),
]


class TestByteHealthAndShadowing:

    @pytest.mark.parametrize("path", TOUCHED)
    def test_file_byte_health(self, path):
        with io.open(path, "rb") as f:
            data = f.read()
        assert data.count(b"\n") - data.count(b"\r\n") == 0, f"{path} 存在裸 LF"
        assert data.count(b"\r") - data.count(b"\r\n") == 0, f"{path} 存在孤立 CR"
        assert not data.startswith(b"\xef\xbb\xbf"), f"{path} 不应有 BOM"
        text = data.decode("utf-8")
        assert "\ufffd" not in text and "\x00" not in text

    @pytest.mark.parametrize("path", TOUCHED)
    def test_source_compiles(self, path):
        with io.open(path, encoding="utf-8") as f:
            compile(f.read(), path, "exec")

    def test_no_duplicate_module_level_names(self):
        """同名遮蔽回归锁：本轮追加的补丁不得与文件既有模块级符号重名
        （首版 _FENCE_RE 覆盖 181 行围栏正则、打挂 strip_code_blocks 的教训）。
        范围限定本轮真正追加代码的 services 层 + export.py；sse_handlers.py
        存在**历史遗留**的条件定义同名符号（如 CONTENT_REQUEST_TIMEOUT），
        不属于本轮契约，纳管需单独立项。"""
        for path in [os.path.join(BACKEND, "app", "services", "outline_quality.py"),
                     os.path.join(BACKEND, "app", "services", "content_standard.py"),
                     os.path.join(BACKEND, "app", "routers", "export.py")]:
            tree = _tree(path)
            seen: set[str] = set()
            for node in tree.body:
                targets: list[str] = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    targets = [node.name]
                elif isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(
                        node.target, ast.Name):
                    targets = [node.target.id]
                for name in targets:
                    assert name not in seen, \
                        f"{os.path.basename(path)}: 模块级符号 {name} 重复定义"
                    seen.add(name)

    def test_content_standard_fence_regex_single_definition(self):
        """strip_code_blocks 依赖的围栏正则全文件只许有一处定义。"""
        path = os.path.join(BACKEND, "app", "services", "content_standard.py")
        with io.open(path, encoding="utf-8") as f:
            src = f.read()
        assert len(re.findall(r"^_FENCE_RE = ", src, re.M)) == 1
        # ✅ R76b · G3 锚点订正（护栏失锚同族，本轮实踩）：原断言是
        #    `assert "_PS_FENCE_LINE_RE = " in src` —— 意图为「段落校验用独立命名、
        #    不与模块级 _FENCE_RE 遮蔽」，但它锁的是**本地副本存在**。R76b 把那份
        #    行级翻转副本删掉、改消费 fence_common.iter_all_blocks 后，该断言只靠
        #    「处置注释里逐字引用了旧代码」侥幸为真（注释一改即恒红，副本回流也测不出）。
        #    现按 AST 锁反向不变式：不得重新定义本地围栏正则 + 必须真的消费唯一出口。
        assigned = {t.id for node in _tree(path).body if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)}
        assert "_PS_FENCE_LINE_RE" not in assigned, (
            "段落校验不得重新定义本地围栏正则（闭合/块行判据唯一事实源 = services/fence_common）"
        )
        assert "iter_all_blocks" in src, (
            "paragraph_structure_report 必须消费 fence_common 的块行口径"
        )
