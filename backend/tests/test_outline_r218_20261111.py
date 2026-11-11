"""R218（2026-11-11）· 目录生成模块第三轮深度探索增强 —— 护栏。

覆盖范围（均基于活体代码或纯函数行为，不采信报告结论）：
- D-A：list_sections 的 schemes SELECT 补 name/type（工作台头部方案名/类型恒空白）。
- C-1：_UNIT_TAIL_CHARS 补「条口道节排列面孔眼遍趟」11 量词 +
      _UNIT_TAIL_EXEMPT_NEXT 精准豁免（条→条件/条款、节→节点/节能）。
- D-B：create_section / update_section 标题长度上限（OUTLINE_TITLE_MAX_LEN，
      超限 400，绝不静默截断）。
- N1：heading_v1 L1 章号不再 min(30) 硬钳（字表内中文、超出回退阿拉伯，
      绝不产出重复章号）。
- N2：_outline_structure_report / _repair_outline_structure_numbering 复用
      模块级 _collect_section_ids（R216 收口漏结构自检路径）。
- N3：_sort_children 深度护栏（_TREE_SORT_MAX_DEPTH = numbering._MAX_TREE_DEPTH）。
- N4：_STRIP_NUMBER_RE 补 Unicode 罗马 + 中文大写（仅后接分隔符才剥）。
- O-1：all_failed 出口 _fail_payload / _fail_event 补 failed_count。
- O-2：sse_checkpoint SQL 参数化 _RESUMABLE_TASK_STATUSES（单一事实源）。
- O-3：子目录提示词 facts 行集复用指纹侧 _fp_facts_rows（不再二次查询）。
- O-4：全续跑轮次 continue 前补 _prog["sub_done"]（显示级进度滞后）。
- C-2：content_plan 注入展示态编号（stored_id_to_display，与正文主链同口径）。
- C-3：_sec_no / _sec_prefix 脏数据兜底 SECTION_NUMBER_MISSING。
- C-4：_TABLE_CAPTION_RE 与 _export_docx_parts._LEADING_TABLE_NUM_RE 对齐
      （两侧行为逐字一致）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO / "backend" / "app"


def _src(rel: str) -> str:
    return (BACKEND_APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A · 编号剥离：Unicode 罗马 + 中文大写（N4）
# ---------------------------------------------------------------------------


class TestNumberingRomanAndUppercase:
    def test_unicode_roman_stripped(self):
        """`Ⅰ. 总则` 的 Unicode 罗马编号必须被剥离（此前在剥离类外 ⇒ 双重编号）。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("Ⅰ. 总则") == "总则"
        assert strip_outline_numbering("Ⅱ. 施工组织") == "施工组织"

    def test_chinese_uppercase_stripped(self):
        """`壹、总则` / `貳、` / `陸、` 中文大写编号必须被剥离。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("壹、总则") == "总则"
        assert strip_outline_numbering("貳、施工组织") == "施工组织"
        assert strip_outline_numbering("陸、质量保证") == "质量保证"

    def test_unicode_roman_adjacent_not_stripped(self):
        """紧邻字符不剥（`Ⅳ级` 是普通词，不是编号）。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("Ⅳ级") == "Ⅳ级"

    def test_ascii_roman_deliberately_not_stripped(self):
        """ASCII 罗马（`I. 总则`）**刻意不剥**（与导入识别侧一致，防「I 型钢」误剥）。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("I. 总则") == "I. 总则"

    def test_year_and_floor_not_stripped(self):
        """年份（2023 年度）与层数（十二层平面）不得被剥。"""
        from app.services.numbering import strip_outline_numbering

        assert strip_outline_numbering("2023 年度安全生产计划") == "2023 年度安全生产计划"
        assert strip_outline_numbering("十二层平面") == "十二层平面"

    def test_strip_re_contains_unicode_roman_and_uppercase(self):
        """源码棘轮：剥离类必须含 Unicode 罗马与中文大写（防被回退 ASCII-only）。"""
        s = _src("services/numbering.py")
        assert "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅬⅭⅮⅯ" in s
        assert "壹贰叁肆伍陆柒捌玖拾佰仟零貳參陸" in s


# ---------------------------------------------------------------------------
# A2 · heading_v1 L1 章号 31+ 回退阿拉伯（N1）
# ---------------------------------------------------------------------------


class TestHeadingV1ChapterBeyondThirty:
    def _gen(self, n: int) -> str:
        from app.services.ai.heading_v1 import HeadingNumberingGenerator

        return HeadingNumberingGenerator.generate_number(1, [n, 0, 0, 0, 0, 0, 0, 0])

    def test_within_table_uses_chinese(self):
        """字表内用中文数字：1→第一章、30→第三十章。"""
        assert self._gen(1) == "第一章"
        assert self._gen(30) == "第三十章"

    def test_beyond_table_falls_back_arabic(self):
        """超出字表回退阿拉伯：31→第31章、35→第35章（绝不重复「第三十章」）。"""
        assert self._gen(31) == "第31章"
        assert self._gen(35) == "第35章"
        assert self._gen(40) == "第40章"

    def test_no_min_thirty_clamp(self):
        """源码棘轮：旧 `min(30, counters[0])` 硬钳不得回流。"""
        s = _src("services/ai/heading_v1.py")
        assert "min(30" not in s
        assert "CHINESE_NUMBERS[n]" in s


# ---------------------------------------------------------------------------
# B · 树构建（N2 / N3）
# ---------------------------------------------------------------------------


class TestSectionTreeBuild:
    def test_sort_children_depth_guard(self):
        """N3：_sort_children 递归必须带 _TREE_SORT_MAX_DEPTH 深度护栏。"""
        s = _src("routers/sections.py")
        assert "_TREE_SORT_MAX_DEPTH" in s
        assert "if _depth > _TREE_SORT_MAX_DEPTH" in s

    def test_sort_children_reuses_numbering_max_depth(self):
        """N3：深度上限必须复用 numbering._MAX_TREE_DEPTH（单一事实源，不新增常量）。"""
        s = _src("routers/sections.py")
        assert "_MAX_TREE_DEPTH as _TREE_SORT_MAX_DEPTH" in s

    def test_structure_report_reuses_collect_section_ids(self):
        """N2：_outline_structure_report 复用模块级 _collect_section_ids（不新增遍历）。"""
        s = _src("routers/sections.py")
        # 局部 _collect_mounted 定义必须已删除（模块级唯一实现 _collect_section_ids）
        assert "def _collect_mounted" not in s
        assert "def _collect_section_ids" in s
        assert "_collect_section_ids(tree)" in s

    def test_repair_structure_numbering_reuses_collect_section_ids(self):
        """N2：_repair_outline_structure_numbering 同口径复用模块级。"""
        s = _src("routers/sections.py")
        assert "_collect_section_ids(_tree_probe)" in s


# ---------------------------------------------------------------------------
# C · 列表 SELECT / 标题长度上限（D-A / D-B）
# ---------------------------------------------------------------------------


class TestListSectionsAndTitleCap:
    def test_scheme_select_includes_name_type(self):
        """D-A：list_sections 的 schemes SELECT 必须含 name/type（工作台头部恒空白修复）。"""
        s = _src("routers/sections.py")
        assert "name, type FROM schemes WHERE id=?" in s

    def test_title_length_cap_both_paths(self):
        """D-B：create_section 与 update_section 两处都必须有标题长度上限校验。"""
        s = _src("routers/sections.py")
        assert s.count("> OUTLINE_TITLE_MAX_LEN") >= 2

    def test_outline_title_max_len_value(self):
        """D-B：上限常量取值（outline_utils.OUTLINE_TITLE_MAX_LEN）。"""
        from app.services.outline_utils import OUTLINE_TITLE_MAX_LEN

        assert OUTLINE_TITLE_MAX_LEN == 200


# ---------------------------------------------------------------------------
# D · 量词 / 表题（C-1 / C-4）
# ---------------------------------------------------------------------------


class TestUnitTailQuantity:
    def test_new_quantity_chars_guard(self):
        """C-1：`3 条施工便道`/`3 节电池` 正文首字（条/节）判数量词 ⇒ 数字保留。"""
        from app.services.content_blocks import _tail_starts_with_quantity

        assert _tail_starts_with_quantity("条施工便道") is True
        assert _tail_starts_with_quantity("节电池") is True

    def test_exempt_common_nouns(self):
        """C-1：`3 条件`/`3 节点` 是编号+名词标题，豁免（不误降级为正文）。"""
        from app.services.content_blocks import _tail_starts_with_quantity

        assert _tail_starts_with_quantity("条件") is False
        assert _tail_starts_with_quantity("条款") is False
        assert _tail_starts_with_quantity("节点") is False
        assert _tail_starts_with_quantity("节能") is False

    def test_existing_quantity_and_exempt_still_hold(self):
        """C-1：既有量词（台）与豁免（人→力）行为不回归。"""
        from app.services.content_blocks import _tail_starts_with_quantity

        assert _tail_starts_with_quantity("台挖掘机进场") is True
        assert _tail_starts_with_quantity("人力资源配置") is False

    def test_unit_chars_source_guard(self):
        """C-1 源码棘轮：11 个新量词必须都在 _UNIT_TAIL_CHARS。"""
        s = _src("services/content_blocks.py")
        assert "条口道节排列面孔眼遍趟" in s

    def test_unit_exempt_source_guard(self):
        """C-1 源码棘轮：新量词的精准豁免表必须在位。"""
        s = _src("services/content_blocks.py")
        assert '"条": "件款目文例"' in s
        assert '"节": "点能日气奏拍"' in s


class TestTableCaptionRegexAligned:
    def _both_strip(self, text: str) -> tuple[str, str]:
        # 循环导入：export ↔ _export_docx_parts，必须先初始化 export 再取导出侧实现
        import app.routers.export as _e  # noqa: F401
        from app.routers._export_docx_parts import _strip_leading_table_number as _dp_strip
        from app.services.content_blocks import _strip_leading_table_number as _cb_strip

        return _cb_strip(text), _dp_strip(text)

    @pytest.mark.parametrize(
        "text",
        [
            "表６-１ 配置计划",
            "表格 6-1 配置计划",
            "表 6-1 配置计划",
            "表：配置计划",
            "表6-1配置计划",
            "表 施工计划",
            "表\u200b6-1 配置计划",
            "表 3-1 主要施工机械设备表",
            "表3-1主要施工机械设备表",
        ],
    )
    def test_both_sides_identical(self, text):
        """C-4：正文侧与导出侧 `_strip_leading_table_number` 行为逐字一致。"""
        cb, dp = self._both_strip(text)
        assert cb == dp

    def test_table_caption_re_has_fullwidth_and_ge(self):
        """C-4 源码棘轮：_TABLE_CAPTION_RE 必须认全角数字与「格」字。"""
        s = _src("services/content_blocks.py")
        assert "０-９" in s
        assert "格?" in s


# ---------------------------------------------------------------------------
# E · SSE / checkpoint（O-1 / O-2 / O-3 / O-4）
# ---------------------------------------------------------------------------


class TestAllFailedExitCounts:
    def test_fail_payload_has_failed_count(self):
        """O-1：all_failed 出口 _fail_payload 补 failed_count（前端统计口径）。"""
        s = _src("routers/sse_handlers.py")
        assert s.count('"failed_count": len(failed_chapters)') >= 2

    def test_fail_payload_failed_count_key(self):
        """O-1：checkpoint 侧与 SSE 事件侧都带 failed_count（两处出口同口径）。"""
        s = _src("routers/sse_handlers.py")
        assert '"failed_count": len(failed_chapters)' in s


class TestCheckpointStatusParameterized:
    def test_resumable_status_parameterized(self):
        """O-2：SQL 必须参数化引用 _RESUMABLE_TASK_STATUSES（单一事实源）。"""
        s = _src("routers/sse_checkpoint.py")
        assert "_status_ph = \",\".join(\"?\" for _ in _RESUMABLE_TASK_STATUSES)" in s
        assert "status IN ({_status_ph})" in s
        assert "_RESUMABLE_TASK_STATUSES," in s

    def test_no_hardcoded_status_literal(self):
        """O-2：硬编码 `status IN ('stopped','failed')` 字面量不得回流。"""
        s = _src("routers/sse_checkpoint.py")
        assert "status IN ('stopped','failed')" not in s


class TestSubdirectoryFactsReuseFingerprintRows:
    def test_reuses_fp_facts_rows(self):
        """O-3：子目录提示词 facts 行集复用指纹侧 _fp_facts_rows（不二次查询）。"""
        s = _src("routers/sse_handlers.py")
        assert "_outline_facts_rows = list(_fp_facts_rows)" in s


class TestFullResumeRoundSubDone:
    def test_sub_done_before_continue(self):
        """O-4：全续跑轮次 continue 前必须更新 sub_done（进度显示不滞后）。"""
        s = _src("routers/sse_handlers.py")
        # O-4 新增的 continue 前更新 + 既有 :2338 的更新，至少两处
        assert s.count('_prog["sub_done"] = len(full_outline)') >= 2


# ---------------------------------------------------------------------------
# F · 提示词编号（C-2 / C-3）
# ---------------------------------------------------------------------------


class TestContentPlanDisplayNumber:
    def test_uses_stored_id_to_display(self):
        """C-2：编排提示词 section_number 必须转展示态（stored_id_to_display）。"""
        s = _src("services/content_plan.py")
        assert "stored_id_to_display" in s
        assert "section_number=_sec_display" in s


class TestSectionNumberMissingFallback:
    def test_sec_no_missing_fallback(self):
        """C-3：_sec_no / _sec_prefix 脏数据必须兜底 SECTION_NUMBER_MISSING。"""
        s = _src("routers/sse_handlers.py")
        assert "_sec_no = SECTION_NUMBER_MISSING" in s
        assert "_sec_prefix = SECTION_NUMBER_MISSING" in s

    def test_sec_stored_is_source_of_truth(self):
        """C-3：合法性判据必须是 _section_outline_number（stored_outline_id 唯一事实源）。"""
        s = _src("routers/sse_handlers.py")
        assert "_sec_stored = _section_outline_number(leaf)" in s

    def test_section_number_for_prompt_still_reexported(self):
        """C-3：sse_handlers 仍 re-export _section_number_for_prompt（既有测试导入契约）。"""
        s = _src("routers/sse_handlers.py")
        assert "_section_number_for_prompt," in s


# ---------------------------------------------------------------------------
# G · 跨模块：_section_outline_number 对脏数据 fail-closed（C-3 判据行为）
# ---------------------------------------------------------------------------


class TestSectionOutlineNumberFailClosed:
    def test_dirty_stored_id_returns_empty(self):
        """C-3 判据行为：_section_outline_number 对非法/空存储编号返回 ''（兜底触发前提）。"""
        from app.routers.sse_handlers import _section_outline_number

        assert _section_outline_number({"id": "not-a-number"}) == ""
        assert _section_outline_number({}) == ""

    def test_valid_stored_id_returns_number(self):
        """C-3 判据行为：合法存储编号原样返回（合法时兜底不触发，行为逐字不变）。"""
        import json

        from app.routers.sse_handlers import _section_outline_number

        # stored_outline_id 读 outline_json（JSON 字符串内 id）；无该字段即无存储编号
        sec = {"id": "sec-2-4", "outline_json": json.dumps({"id": "2.4"})}
        assert _section_outline_number(sec) == "2.4"
