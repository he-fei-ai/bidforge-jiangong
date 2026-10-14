# -*- coding: utf-8 -*-
"""文档质量分析报告（2026-10-10）修复回归测试。

对应报告第二部分已实施的修复项：
- P0-A 修复5：Markdown 字面转义残留还原（docx_math.unescape_markdown_literals）
- P0-B 修复1/17：页眉/封面模板残留清洗 + 项目名兜底（export.py）
- P0-C 修复7：重复标题尾部（N）计数后缀整组剥除（outline_quality._norm）
- P1-D 修复14：规范编号-名称配对校验（export_audit._detect_standard_pairing）
- P1-E 修复13：术语混用检测（export_audit._detect_terminology_mix）
- P2-F 增强12：空话套话句式检测（export_audit._detect_filler_phrases）

全部为行为断言：正样例零误报、坏样例必命中（含报告实证形态）。
"""
from app.services.docx_math import (
    clean_text,
    scan_issues,
    unescape_markdown_literals,
)
from app.services.outline_quality import _norm
from app.routers.export import (
    _display_scheme_name,
    _project_name_from_facts,
    _sanitize_template_residue_title,
)
from app.routers.export_audit import audit_content


# ---------------------------------------------------------------------------
# P0-A：转义残留还原（修复5）
# ---------------------------------------------------------------------------
class TestEscapeResidue:
    def test_literal_newline(self):
        # 字面 \n → 真换行（黏行修复）
        assert unescape_markdown_literals("第1层\\n第2层") == "第1层\n第2层"

    def test_markdown_punct_escapes(self):
        assert unescape_markdown_literals("\\*重点\\*") == "*重点*"
        assert unescape_markdown_literals("\\_标题\\_") == "_标题_"

    def test_latex_commands_protected(self):
        # LaTeX 命令绝不触碰（后随字母 → lookbehind/lookahead 排除）
        assert unescape_markdown_literals("$\\nabla$") == "$\\nabla$"
        assert unescape_markdown_literals("\\top") == "\\top"

    def test_windows_path_protected(self):
        assert unescape_markdown_literals("C:\\new") == "C:\\new"
        assert unescape_markdown_literals("C:\\nodes") == "C:\\nodes"

    def test_formula_span_protected(self):
        # 公式跨度内逐字保留（交给公式转换器）
        assert unescape_markdown_literals("$a\\nb$") == "$a\\nb$"
        assert unescape_markdown_literals("\\[a\\nb\\]") == "\\[a\\nb\\]"

    def test_escaped_backslash_not_touched(self):
        # 已转义反斜杠 \\n 不动（lookbehind 排除）
        assert unescape_markdown_literals("\\\\n") == "\\\\n"

    def test_clean_text_wires_unescape(self):
        out = clean_text("第1层\\n第2层 与 \\*号\\*")
        assert "\\n" not in out and "\\*" not in out

    def test_scan_issues_counts_residue(self):
        stats = scan_issues("第1层\\n第2层 与 \\*号\\*")
        assert stats["escape_residue"] == 3  # \n ×1 + \* ×2
        assert scan_issues("正常文本无残留")["escape_residue"] == 0


# ---------------------------------------------------------------------------
# P0-B：页眉/封面模板残留清洗 + 项目名兜底（修复1/17）
# ---------------------------------------------------------------------------
class TestTemplateResidue:
    def test_sanitize_strips_reported_residue(self):
        # 报告实证形态：「招投标项目 2026/10/7 19:56:43」整串残留
        assert _sanitize_template_residue_title(
            "招投标项目 2026/10/7 19:56:43") == ""
        assert _sanitize_template_residue_title("招投标项目") == ""
        assert _sanitize_template_residue_title("") == ""

    def test_sanitize_keeps_real_title(self):
        assert _sanitize_template_residue_title(
            "XX大桥加固工程专项施工方案") == "XX大桥加固工程专项施工方案"

    def test_sanitize_does_not_blank_generic_word_names(self):
        # 「示例」/「工程」等泛词被刻意排除在残留词表外：只由泛词拼成的名字
        # 可能是真实落库工程名（前置表单测试夹具就叫「示例工程」），整串置空
        # 会把用户数据静默吃成空白标题。
        assert _sanitize_template_residue_title("示例工程") == "示例工程"
        assert _sanitize_template_residue_title("基坑支护工程") == "基坑支护工程"
        assert _sanitize_template_residue_title("临时用电工程") == "临时用电工程"

    def test_project_name_from_facts(self):
        facts = [{"title": "项目名称", "content": "市政道路提升工程"},
                 {"title": "工期", "content": "617天"}]
        assert _project_name_from_facts(facts) == "市政道路提升工程"
        assert _project_name_from_facts([]) == ""


# ---------------------------------------------------------------------------
# P0-C：重复标题（N）后缀整组剥除（修复7）
# ---------------------------------------------------------------------------
class TestNormCounterSuffix:
    def test_counter_suffix_stripped_whole_group(self):
        # （N）整组剥除 → 与本体碰撞，重复被捕获
        assert _norm("专项6（3）") == "专项6"
        assert _norm("总体实施策划（7）") == "总体实施策划"

    def test_halfwidth_paren_suffix(self):
        assert _norm("专项6(3)") == "专项6"

    def test_normal_title_untouched(self):
        assert _norm("施工准备") == "施工准备"
        assert _norm("总体实施策划") == "总体实施策划"

    def test_non_numeric_paren_not_counter(self):
        # 括号内非纯数字（正文性内容）不按计数剥（仅剥字符，语义不变）
        assert _norm("脚手架（落地式）") == "脚手架落地式"


# ---------------------------------------------------------------------------
# P1-D/E/F：audit_content 三类专项体检（检测型集成）
# ---------------------------------------------------------------------------
class TestAuditContentNewRules:
    def test_bad_content_hits_all_three(self):
        content = ("依据 JGJ 39《施工现场临时用电安全技术规范》执行。"
                   "外架搭设完成后，外脚手架应组织验收。"
                   "我们要高度重视安全工作，进一步提高管理水平，"
                   "切实做好防护措施，确保万无一失。")
        r = audit_content([{"id": "s1", "title": "安全措施",
                            "level": 2, "content": content}])
        keys = {it["key"] for it in r["items"]}
        assert {"standard_pairing", "terminology_mix",
                "filler_phrase"} <= keys

    def test_good_content_no_false_positive(self):
        content = ("依据 JGJ 46《施工现场临时用电安全技术规范》执行，"
                   "外脚手架搭设完成后应组织验收。")
        r = audit_content([{"id": "s2", "title": "施工准备",
                            "level": 2, "content": content}])
        keys = {it["key"] for it in r["items"]}
        assert not ({"standard_pairing", "terminology_mix",
                     "filler_phrase"} & keys)

    def test_empty_sections_zero_items(self):
        assert audit_content([])["total"] == 0


# ---------------------------------------------------------------------------
# P1-G 修复10：工期声明自相矛盾检测（内容问题5）
# P1-H 修复18：里程碑排期合理性检测（内容问题6）
# ---------------------------------------------------------------------------
class TestScheduleAndMilestone:
    def test_schedule_conflict_detected(self):
        # 报告实证：正文 617 日历天、另一处又写 718 天 → 自相矛盾
        content = ("本工程总工期 617日历天，计划于 2028-05-30 竣工。"
                   "详见图9-2甘特图，总工期约 718天。")
        r = audit_content([{"id": "s1", "title": "工期",
                            "level": 2, "content": content}])
        keys = {it["key"] for it in r["items"]}
        assert "schedule_conflict" in keys
        item = next(it for it in r["items"] if it["key"] == "schedule_conflict")
        assert item["level"] == "error"
        assert item["count"] == 2

    def test_schedule_single_value_no_false_positive(self):
        # 全文只有一种工期声明 → 不误报
        content = "本工程总工期 617日历天，确保 2028-05-30 竣工。"
        r = audit_content([{"id": "s2", "title": "工期",
                            "level": 2, "content": content}])
        assert "schedule_conflict" not in {it["key"] for it in r["items"]}

    def test_schedule_ignores_unrelated_days(self):
        # 「养护 28 天」「龄期 7 天」不应被当成工期声明
        content = ("混凝土养护 28 天即可达到设计强度，"
                   "本工程总工期 617日历天。")
        r = audit_content([{"id": "s3", "title": "施工",
                            "level": 2, "content": content}])
        assert "schedule_conflict" not in {it["key"] for it in r["items"]}

    def test_milestone_secondary_structure_after_scaffold_down(self):
        # 报告工序逻辑：二次结构及抹灰须在外部脚手架拆除「之前」完成；
        # 此处反置为 二次结构(10/30) 晚于 外架拆除(9/30) → 不合常理，应被检出。
        content = ("出地面（±0）2026-12-15；主体封顶 2027-07-15；"
                   "外架及塔吊拆除 2027-09-30；二次结构及抹灰 2027-10-30；"
                   "竣工 2028-05-30。")
        r = audit_content([{"id": "s4", "title": "里程碑",
                            "level": 2, "content": content}])
        keys = {it["key"] for it in r["items"]}
        assert "milestone_issue" in keys
        item = next(it for it in r["items"] if it["key"] == "milestone_issue")
        assert any("二次结构" in s or "外架" in s for s in item["sections"])

    def test_milestone_normal_order_no_false_positive(self):
        # 正常排期（二次结构早于外架拆除、间隔合理、顺序递增）→ 不误报
        content = ("出地面（±0）2026-12-15；主体封顶 2027-07-15；"
                   "外架及塔吊拆除 2027-10-30；二次结构及抹灰 2027-09-30；"
                   "竣工 2028-05-30。")
        r = audit_content([{"id": "s5", "title": "里程碑",
                            "level": 2, "content": content}])
        assert "milestone_issue" not in {it["key"] for it in r["items"]}

    def test_milestone_too_short_gap(self):
        # 相邻里程碑间隔 <7 天 → 漏排/错排告警
        content = ("基础验收 2027-03-01；主体开工 2027-03-05；竣工 2028-05-30。")
        r = audit_content([{"id": "s6", "title": "里程碑",
                            "level": 2, "content": content}])
        keys = {it["key"] for it in r["items"]}
        assert "milestone_issue" in keys

    def test_single_milestone_no_false_positive(self):
        # 只有一个日期 → 不足以排期，不误报
        content = "本工程计划于 2028-05-30 竣工。"
        r = audit_content([{"id": "s7", "title": "工期",
                            "level": 2, "content": content}])
        assert "milestone_issue" not in {it["key"] for it in r["items"]}


# ---------------------------------------------------------------------------
# 接线护栏：清洗/兜底必须真正进入封面 / 页眉 / 表单抬头 / 下载文件名
#
# 纯函数本身已被上面两组用例锁定；这里补的是「定义存在 ≠ 被调用」的盲区 ——
# 函数写好而封面 / 页眉照旧读 scheme["name"] 原值时，函数层全绿、交付件上
# 残留依旧。判据锚定 AST 调用点，不锚定注释文本（§5.14）。
# ---------------------------------------------------------------------------
class TestResidueWiring:
    def test_filename_never_carries_residue(self):
        from app.routers.export import _build_export_filename
        fn = _build_export_filename(
            _display_scheme_name({"name": "招投标项目 2026/10/7 19:56:43"}, []),
            "docx", 1)
        assert fn.startswith("导出文档_")
        assert "招投标项目" not in fn

    def test_filename_falls_back_to_project_name(self):
        from app.routers.export import _build_export_filename
        fn = _build_export_filename(
            _display_scheme_name(
                {"name": ""},
                [{"title": "项目名称", "content": "市政道路提升工程"}]),
            "docx", 3)
        assert fn.startswith("市政道路提升工程_")
        assert fn.endswith(".docx")

    def test_display_name_uses_both_exits(self):
        """方案名出口必须同时走残留清洗与项目名兜底（两个分支都在）。"""
        import ast
        from pathlib import Path
        from app.routers import export as exp
        tree = ast.parse(Path(exp.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_display_scheme_name")
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert {"_sanitize_template_residue_title",
                "_project_name_from_facts"} <= called

    def test_all_five_surfaces_are_wired(self):
        """五个外露表面全部消费清洗/兜底出口（AST 调用点判定）。"""
        import ast
        from pathlib import Path
        from app.routers import export as exp
        targets = {
            "_scheme_form_header": {"_sanitize_template_residue_title"},
            "_add_compilation_note_page": {"_sanitize_template_residue_title"},
            "_build_docx_sync": {"_sanitize_template_residue_title",
                                 "_display_scheme_name"},
            "export_docx": {"_display_scheme_name"},
            "export_pdf": {"_display_scheme_name"},
        }
        tree = ast.parse(Path(exp.__file__).read_text(encoding="utf-8"))
        fns = {n.name: n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in targets}
        assert set(fns) == set(targets)
        for name, need in targets.items():
            called = {n.func.id for n in ast.walk(fns[name])
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert not (need - called), f"{name} 未接 {sorted(need - called)}"

