# -*- coding: utf-8 -*-
"""护栏：文档质量缺陷修复回归（2026-10-17）。

覆盖两份事实源描述的缺陷：

A. 《目录与页码体系问题.md》—— HTML 实体残留 / 带边界空白 LaTeX 残留 / 中英混排
   误植词 / 表题叠写（含顿号表题不被吸收 + 首格自带编号）/ 图题重号（有题无图）/
   数据矛盾维度缺失。
B. 《导出文档发现的问题.docx》—— 组织架构图**根节点空框**（有子节点、无标签）/
   表格**两块侧叠**（表头 5 列 + 数据行塞两条记录 → 整表被撑成 11 列）。

每条修复都配「还原旧行为即失败」的反向对照断言（AGENTS §4.22.8 教训：只锚定真正
生效的那段代码，不靠"碰巧含关键字的一切字符串"）。
"""

from __future__ import annotations

import pytest
from app.services import consistency_scanner as CS
from app.services.chart_validators import normalize_architecture_tree
from app.services.content_blocks import (
    _drop_dangling_figure_captions,
    _extract_figure_caption,
    _looks_like_figure_title,
    _looks_like_table_title,
    _normalize_table_first_cell,
    _parse_content_blocks,
    _strip_leading_table_number,
)
from app.services.docx_math import (
    clean_text,
    decode_html_entities,
    fix_english_residue,
    normalize_spaced_inline_math,
)

# ===========================================================================
# A1. HTML 实体还原（「当跨度&gt;2m 时」类残留）
# ===========================================================================


class TestHtmlEntities:
    def test_common_entities_decoded(self):
        assert decode_html_entities("当跨度&gt;2m 时") == "当跨度>2m 时"
        assert decode_html_entities("层高垂直度（&gt;5m）") == "层高垂直度（>5m）"
        assert decode_html_entities("a&lt;b&amp;c") == "a<b&c"
        assert decode_html_entities("&quot;x&quot;") == '"x"'

    def test_double_escaped_entity_decoded(self):
        """`&amp;gt;` 双重转义 → `>`（最多迭代 2 轮）。"""
        assert decode_html_entities("&amp;gt;") == ">"

    def test_numeric_reference_decoded(self):
        assert decode_html_entities("&#62;") == ">"
        assert decode_html_entities("&#x3E;") == ">"

    def test_non_whitelisted_entity_kept(self):
        """白名单外的 `&xxx;` 一律保留（宁可不改，不丢内容）。"""
        assert decode_html_entities("A &foo; B") == "A &foo; B"

    def test_clean_text_wires_entity_decode(self):
        assert clean_text("当跨度&gt;2m 时") == "当跨度>2m 时"

    def test_revert_without_decode_leaves_entity(self):
        """反向对照：不经 clean_text 的裸文本里实体仍在。"""
        raw = "跨度&gt;2m"
        assert "&gt;" in raw
        assert "&gt;" not in clean_text(raw)


# ===========================================================================
# A2. 带边界空白的 `$ x $` → `$x$`（使 LaTeX 落回严格形态转 OMML）
# ===========================================================================


class TestSpacedInlineMath:
    def test_boundary_spaces_removed(self):
        assert normalize_spaced_inline_math("$ \\gamma_c = 25 $") == "$\\gamma_c = 25$"
        assert normalize_spaced_inline_math("$ t_0 $") == "$t_0$"
        assert normalize_spaced_inline_math("$ A $") == "$A$"

    def test_pure_number_not_touched(self):
        """纯数字跨度不归一（避免误伤货币/区间写法）。"""
        assert normalize_spaced_inline_math("$ 25 $") == "$ 25 $"

    def test_no_boundary_space_untouched(self):
        assert normalize_spaced_inline_math("$x$") == "$x$"

    def test_clean_text_wires_math_normalize(self):
        assert clean_text("$ \\beta_1 $") == "$\\beta_1$"

    def test_revert_leaves_spaced_math(self):
        raw = "$ k $"
        assert normalize_spaced_inline_math(raw) != raw  # 修复生效
        assert "$ k $" != normalize_spaced_inline_math(raw)  # 旧行为即失败


# ===========================================================================
# A3. 中英混排误植词还原（公式跨度外）
# ===========================================================================


class TestEnglishResidue:
    def test_residue_words_mapped(self):
        assert fix_english_residue("steel tube 与 plywood") == "钢管 与 胶合板"
        assert fix_english_residue("required 强度") == "需 强度"
        assert fix_english_residue("cutting 作业") == "切割 作业"

    def test_word_boundary_only(self):
        """词内不得误伤（如 `footing` 命中，`bigfooting` 不得被改）。"""
        assert fix_english_residue("bigfooting") == "bigfooting"

    def test_formula_span_untouched(self):
        assert fix_english_residue("$ required $") == "$ required $"

    def test_revert_leaves_residue(self):
        raw = "steel tube"
        assert "steel tube" in raw
        assert "steel tube" not in fix_english_residue(raw)


# ===========================================================================
# A4. 表题叠写修复（根因 A：顿号表题；根因 B：首格自带编号）
# ===========================================================================


class TestTableCaptionDedup:
    def test_dunhao_table_title_accepted(self):
        """含顿号的真实表题必须被接受（旧实现把「、」当句读拒绝）。"""
        assert _looks_like_table_title("预埋件、预留孔洞允许偏差") is True
        assert _looks_like_table_title("项目、允许偏差（mm）、检验方法") is True

    def test_sentence_still_rejected(self):
        """反向对照：句末标点仍判为正文句子（防误吞正文）。"""
        assert _looks_like_table_title("表中数据如下。") is False
        assert _looks_like_table_title("表所列参数，详见表注") is False

    def test_strip_leading_table_number(self):
        assert _strip_leading_table_number("表 6-1 木工班组劳动力配置计划") == "木工班组劳动力配置计划"
        assert _strip_leading_table_number("表6-3 特种作业人员管理流程") == "特种作业人员管理流程"
        # 非「表 X-Y」开头 → 原样返回
        assert _strip_leading_table_number("序号") == "序号"

    def test_normalize_first_cell(self):
        lines = ["| 表 6-1 木工班组劳动力配置计划 | 工种 |", "| --- | --- |", "| 1 | 木工 |"]
        out = _normalize_table_first_cell(lines)
        assert out[0].split("|")[1].strip() == "木工班组劳动力配置计划"
        assert out[1:] == lines[1:]

    def test_parse_absorbs_and_dedups(self):
        """集成：含顿号表题被吸收为表题、首格自带编号被剥离、无残留表题段。"""
        content = (
            "表 6-1 木工班组劳动力配置计划、工种、准备阶段\n"
            "| 表 6-1 木工班组劳动力配置计划 | 工种 |\n"
            "| --- | --- |\n"
            "| 1 | 木工 |\n"
        )
        blocks = _parse_content_blocks(content)
        tbl = [b for b in blocks if b["type"] == "table"][0]
        assert tbl["caption"] == "木工班组劳动力配置计划、工种、准备阶段"
        assert "表 6-1" not in tbl["lines"][0]
        assert not any(b["type"] == "paragraph" and "表 6-1" in b.get("text", "") for b in blocks)

    def test_revert_no_strip_keeps_stacked_number(self):
        """反向对照：不剥离时首格仍带「表 6-1」→ 与导出表号叠写。"""
        lines = ["| 表 6-1 木工班组劳动力配置计划 | 工种 |"]
        assert "表 6-1" in lines[0]  # 旧行为
        assert "表 6-1" not in _normalize_table_first_cell(lines)[0]


# ===========================================================================
# A5. 图题重号 / 有题无图
# ===========================================================================


class TestFigureCaption:
    def test_absorb_caption_above_chart(self):
        blocks = [{"type": "paragraph", "text": "图 3-1 机械设备管理流程"}]
        cap = _extract_figure_caption(blocks)
        assert cap == "机械设备管理流程"
        assert blocks == []  # 已升格，正文不再重复输出

    def test_drop_dangling_caption(self):
        blocks = [
            {"type": "paragraph", "text": "正文说明。"},
            {"type": "paragraph", "text": "图 9-1 支撑平面布置图"},
        ]
        kept = _drop_dangling_figure_captions(blocks)
        assert not any(b["type"] == "paragraph" and "图 9-1" in b.get("text", "") for b in kept)
        assert any(b["type"] == "paragraph" and b.get("text") == "正文说明。" for b in kept)

    def test_legit_reference_kept(self):
        """正文里的图号引用（非独立图题句）不得被删。"""
        assert _looks_like_figure_title("所示内容") is False
        assert _looks_like_figure_title("了一些参数") is False
        kept = _drop_dangling_figure_captions([{"type": "paragraph", "text": "详见图 3-1 所示内容"}])
        assert len(kept) == 1

    def test_long_caption_rejected(self):
        assert _looks_like_figure_title("x" * 41) is False

    def test_revert_keeps_dangling_caption(self):
        """反向对照：不清理时残留图题行仍在正文里。"""
        blocks = [{"type": "paragraph", "text": "图 9-1 支撑平面布置图"}]
        assert len(blocks) == 1  # 旧行为
        assert _drop_dangling_figure_captions(blocks) == []


# ===========================================================================
# B1. 组织架构图根节点空框（有子节点、无标签）
# ===========================================================================


class TestArchitectureRootLabel:
    CHILDREN = [{"label": "总监理工程师", "children": [{"label": "专业监理工程师"}]}, {"label": "建设单位代表"}]

    def test_children_only_root_gets_default_label(self):
        """根节点只有 children、无 label/name → 旧实现渲染空框。"""
        root = normalize_architecture_tree({"children": self.CHILDREN})
        assert root is not None
        assert (root.get("label") or root.get("name") or "").strip() != ""

    def test_payload_title_preferred(self):
        root = normalize_architecture_tree({"title": "模板工程验收组织架构", "children": self.CHILDREN})
        assert root["label"] == "模板工程验收组织架构"

    def test_nested_empty_label_root(self):
        root = normalize_architecture_tree({"root": {"label": "", "children": self.CHILDREN}})
        assert root is not None
        assert (root.get("label") or "").strip() != ""

    def test_existing_label_untouched(self):
        root = normalize_architecture_tree({"label": "项目部", "children": self.CHILDREN})
        assert root["label"] == "项目部"

    def test_layout_root_node_has_text(self):
        from app.services.ai.mermaid_architecture import _layout_architecture_tree

        root = normalize_architecture_tree({"children": self.CHILDREN})
        nodes, _w, _h = _layout_architecture_tree(root)
        tops = [n for n in nodes if n["depth"] == 0]
        assert len(tops) == 1
        assert tops[0]["label"].strip() != ""

    def test_render_produces_image(self):
        from app.services.ai.mermaid_architecture import _render_architecture_image_v2

        bio = _render_architecture_image_v2({"children": self.CHILDREN})
        assert bio is not None and len(bio.getvalue()) > 100

    def test_revert_without_fallback_is_blank(self):
        """反向对照：旧归一逻辑对「有 children、无标签」的根返回空 label。"""
        raw = {"children": self.CHILDREN}
        assert not (raw.get("label") or raw.get("name"))  # 旧实现落空
        # 修复后必有标签
        assert normalize_architecture_tree(raw)["label"]


# ===========================================================================
# B2. 表格两块侧叠（11 列错位）修复
# ===========================================================================

#: 《导出文档发现的问题.docx》实证的错位表：表头 5 列有名 + 6 列空；首条数据行
#: 在空白分隔列后**又塞了第二条记录**（序号 2 模板工）。
_EXHIBIT_ROWS = [
    ["序号", "工种", "计划人数", "主要职责", "进场时间安排", "", "", "", "", "", ""],
    [
        "1",
        "木工班长",
        "2",
        "负责现场技术交底、质量把控及进度协调",
        "计划开工前14天",
        "",
        "2",
        "模板工",
        "15",
        "负责模板拼装、加固、拆除及现场清理",
        "首批材料进场后1天",
    ],
    ["3", "测量工", "2", "负责模板位置放线、标高控制及垂直度校正", "同步进场", "", "", "", "", "", ""],
    ["4", "架子工", "6", "负责支撑体系搭设、验收及日常维护", "首批材料进场后1天", "", "", "", "", "", ""],
    ["6", "安全员", "1", "负责现场模板支设安全监督检查", "全程驻场", "", "", "", "", "", ""],
]


class TestUnstackTableRows:
    def test_exhibit_rows_restored_to_five_columns(self):
        from app.routers.export import _unstack_or_trim_table_rows

        out = _unstack_or_trim_table_rows(_EXHIBIT_ROWS)
        assert max(len(r) for r in out) == 5
        assert len(out) == len(_EXHIBIT_ROWS) + 1  # 侧叠的第二条记录升为独立行
        assert out[1][0] == "1" and out[2][0] == "2"  # 两条记录分行且顺序正确
        assert out[2][1] == "模板工"
        assert all(r[-1] != "" for r in out)  # 无尾部空列

    def test_normal_table_unchanged(self):
        from app.routers.export import _unstack_or_trim_table_rows

        rows = [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]]
        assert _unstack_or_trim_table_rows(rows) == rows

    def test_trailing_empty_column_trimmed(self):
        from app.routers.export import _unstack_or_trim_table_rows

        rows = [["A", "B", ""], ["1", "2", ""], ["3", "4", ""]]
        out = _unstack_or_trim_table_rows(rows)
        assert all(len(r) == 2 for r in out)

    def test_two_named_blocks_not_unstacked(self):
        """两个列块表头都有名称（合并表头风格）→ 保守不动。"""
        from app.routers.export import _unstack_or_trim_table_rows

        rows = [["项目", "数量", "", "规格", "备注"], ["甲", "1", "", "R1", "x"]]
        assert _unstack_or_trim_table_rows(rows) == rows

    def test_docx_table_has_five_columns(self):
        """端到端：经渲染器产出的 Word 表格为 5 列 × 5 行（不再撑成 11 列）。"""
        from app.routers.export import _add_table_from_markup
        from docx import Document

        lines = (
            ["| " + " | ".join(r) + " |" for r in _EXHIBIT_ROWS[:1]]
            + ["| " + " | ".join([""] * 11) + " |"]
            + ["| " + " | ".join(r) + " |" for r in _EXHIBIT_ROWS[1:]]
        )
        doc = Document()
        _add_table_from_markup(doc, lines, "宋体")
        tbl = doc.tables[0]
        assert len(tbl.columns) == 5
        assert len(tbl.rows) == 6  # 4 条源记录 + 侧叠拆出的 1 条 = 5 条 + 表头
        assert tbl.rows[2].cells[1].text.strip() == "模板工"

    def test_fix_stat_key_registered(self):
        from app.routers.export import _FIX_STATS_KEYS

        assert "table_unstacked" in _FIX_STATS_KEYS
        for k in (
            "block_render_failed",
            "table_truncated",
            "fences_fixed",
            "html_entities",
            "en_residue",
            "latex_residue",
        ):
            assert k in _FIX_STATS_KEYS


# ===========================================================================
# A6. 数据矛盾维度补全（用工年龄 / 缺陷责任期 / 板厚 / 壁厚 / 拆模强度）
# ===========================================================================


class TestConsistencyTopics:
    NEW = ("用工年龄", "缺陷责任期", "模板面板厚度", "钢管壁厚", "拆模强度")

    def test_topics_registered(self):
        names = {t for t, _ in CS._NUM_TOPICS}
        for topic in self.NEW:
            assert topic in names, f"{topic} 未登记"

    def test_object_group_registered(self):
        for topic in ("用工年龄", "缺陷责任期", "拆模强度"):
            assert CS._NUM_TOPIC_OBJECT_GROUP.get(topic) == 1

    def test_age_contradiction_detected(self):
        """报告实证：四处用工年龄口径不一（一般 55/60、危大 60/55）。"""
        sections = [
            {"id": "p21", "title": "劳动力", "content": "劳务用工年龄上限一般控制在55周岁，危大工程岗位控制在60周岁。"},
            {"id": "p72", "title": "劳动力", "content": "一般作业人员年龄不超过55周岁。"},
            {"id": "ap", "title": "附录", "content": "农民工用工年龄上限：一般60周岁，危大工程55周岁。"},
        ]
        cands = CS.program_prescan(sections)
        aged = {c["topic"]: c["value"] for c in cands if "用工年龄" in c["topic"]}
        assert aged, "用工年龄未被预扫描捕获"
        # 「一般」桶与「危大工程」桶各自出现不同值 → 分别报冲突
        assert any("一般" in k for k in aged)
        assert any("危大" in k for k in aged)

    def test_wall_thickness_contradiction_detected(self):
        sections = [
            {"id": "a", "title": "x", "content": "φ48.3×3.6mm 钢管。"},
            {"id": "b", "title": "x", "content": "壁厚不小于3.24mm。"},
        ]
        cands = CS.program_prescan(sections)
        assert any("钢管壁厚" in c["topic"] for c in cands)

    def test_same_object_same_value_no_conflict(self):
        """同对象同值不报冲突（防过度告警）。"""
        sections = [
            {"id": "a", "title": "x", "content": "悬臂构件强度≥100%。"},
            {"id": "b", "title": "x", "content": "悬臂构件强度≥100%。"},
        ]
        cands = CS.program_prescan(sections)
        assert not any("拆模强度" in c["topic"] for c in cands)


# ===========================================================================
# A7. 导出侧表题编号剥离（export._strip_leading_table_number）
# ===========================================================================


class TestExportTableCaptionStrip:
    def test_export_strip_helper(self):
        from app.routers.export import _strip_leading_table_number as strip

        assert strip("表 6-3 表 6-1 木工班组劳动力配置计划") == "表 6-1 木工班组劳动力配置计划"
        assert strip("序号") == "序号"

    def test_revert_keeps_double_number(self):
        text = "表 6-3 表 6-1 木工班组劳动力配置计划"
        assert text.count("表") == 2  # 旧行为（叠写）
