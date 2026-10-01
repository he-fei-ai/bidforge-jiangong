"""统一编号服务回归测试（编号统一 · 2026-09-25）

覆盖：
1. renumber_outline_nodes —— 目录树编号唯一实现（strip_titles / 非法节点跳过 / 深度防护）
2. json_response 薄包装兼容性（同一对象 / 同一行为）
3. renumber_section_outline_ids —— DB 侧重排（保留 confidence / level 同步）
4. 存储态→展示态映射（stored_id_to_display / stored_id_to_prefix / stored_outline_id）
5. renumber_section_body_subheadings —— 正文子标题编号落库前规范化
   （与导出 _compute_subheading 同口径、幂等、围栏/列表/段落无损）
6. heading 块 src_line（精确重写源行的依据）
7. 附录组标题自定义样式（不进 TOC 域）
8. 导出预检 section_number_mismatch（存储编号 vs 结构重算编号）
9. heading_v2 / heading_templates 字符表别名（唯一事实源）
"""
import json

import pytest

from app.services import numbering as nb
from app.services.ai import json_response as jr
from app.services.ai.heading_templates import ALPHABET as HT_ALPHABET
from app.services.ai.heading_templates import CHINESE_NUMBERS as HT_CHINESE
from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
from app.services.numbering import (
    renumber_outline_nodes,
    renumber_section_body_subheadings,
    renumber_section_outline_ids,
    stored_id_to_display,
    stored_id_to_prefix,
    stored_outline_id,
    strip_outline_numbering,
)


# ============================================================
# 1. 目录树编号唯一实现
# ============================================================
class TestRenumberOutlineNodes:
    def test_basic_numbering(self):
        nodes = [{"title": "a", "children": [{"title": "a1"}]}, {"title": "b"}]
        out = renumber_outline_nodes(nodes)
        assert out[0]["id"] == "1" and out[0]["level"] == 1
        assert out[0]["children"][0]["id"] == "1.1"
        assert out[1]["id"] == "2"

    def test_strip_titles(self):
        nodes = [{"title": "第一章 工程概况", "children": [{"title": "1.1 编制依据"}]}]
        renumber_outline_nodes(nodes)
        assert nodes[0]["title"] == "工程概况"
        assert nodes[0]["children"][0]["title"] == "编制依据"

    def test_strip_titles_false_keeps_title(self):
        nodes = [{"title": "第一章 工程概况"}]
        renumber_outline_nodes(nodes, strip_titles=False)
        assert nodes[0]["title"] == "第一章 工程概况"

    def test_invalid_nodes_do_not_occupy_numbers(self):
        nodes = [{"title": "a"}, "非法字符串", None, {"title": "b"}]
        out = renumber_outline_nodes(nodes)
        assert out[0]["id"] == "1"
        assert out[3]["id"] == "2"  # 非法节点不占号 → 无断号

    def test_children_normalization(self):
        nodes = [{"title": "a", "children": None}, {"title": "b", "children": "坏类型"}]
        renumber_outline_nodes(nodes)
        assert nodes[0]["children"] == []
        assert nodes[1]["children"] == []

    def test_non_list_passthrough(self):
        assert renumber_outline_nodes("not a list") == "not a list"
        assert renumber_outline_nodes(None) is None

    def test_depth_protection(self):
        # 循环引用：children 互相引用，深度防护必须终止且不抛异常
        a: dict = {"title": "a"}
        b: dict = {"title": "b"}
        a["children"] = [b]
        b["children"] = [a]
        renumber_outline_nodes([a])  # 不抛 RecursionError 即通过


# ============================================================
# 2. json_response 薄包装兼容性
# ============================================================
class TestJsonResponseCompat:
    def test_reexports_are_same_objects(self):
        # 唯一事实源：json_response 的名字必须与 numbering 同一对象（防复制漂移）
        assert jr.strip_outline_numbering is nb.strip_outline_numbering
        assert jr.renumber_outline_nodes is nb.renumber_outline_nodes
        assert jr._STRIP_NUMBER_RE is nb._STRIP_NUMBER_RE
        assert jr._PURE_NUMBER_TITLE_RE is nb._PURE_NUMBER_TITLE_RE

    def test_wrapper_same_behavior(self):
        nodes = [{"title": "2.1 相关法律法规", "children": [{"title": "x"}]}]
        out = jr.renumber_outline(nodes)
        assert out[0]["id"] == "1"
        assert out[0]["title"] == "相关法律法规"

    def test_wrapper_non_list(self):
        assert jr.renumber_outline("bad") == "bad"

    def test_strip_safety_cases(self):
        assert strip_outline_numbering("2023年规范") == "2023年规范"
        assert strip_outline_numbering("3D打印施工方案") == "3D打印施工方案"
        assert strip_outline_numbering("1.1") == "1.1"
        assert strip_outline_numbering("第一章 工程概况") == "工程概况"


# ============================================================
# 3. DB 侧重排（renumber_section_outline_ids）
# ============================================================
class TestRenumberSectionOutlineIds:
    def test_updates_and_confidence_preserved(self):
        tree = [
            {"id": "x1", "outline_json": json.dumps({"id": "5", "confidence": 0.9}),
             "children": [
                 {"id": "x2", "outline_json": json.dumps({"id": "5.3"}), "children": []},
             ]},
            {"id": "x3", "outline_json": None, "children": []},
        ]
        updates = renumber_section_outline_ids(tree)
        assert [u[2] for u in updates] == ["x1", "x2", "x3"]
        oj1 = json.loads(updates[0][0])
        assert oj1["id"] == "1" and oj1["confidence"] == 0.9  # 编号刷新 + 保留既有字段
        assert updates[0][1] == 1
        oj2 = json.loads(updates[1][0])
        assert oj2["id"] == "1.1" and updates[1][1] == 2      # level 与编号深度同步
        oj3 = json.loads(updates[2][0])
        assert oj3["id"] == "2" and updates[2][1] == 1

    def test_bad_outline_json_tolerated(self):
        tree = [{"id": "x", "outline_json": "{坏 JSON", "children": []}]
        updates = renumber_section_outline_ids(tree)
        assert json.loads(updates[0][0])["id"] == "1"


# ============================================================
# 4. 存储态 → 展示态映射
# ============================================================
class TestStoredIdMapping:
    def test_display(self):
        assert stored_id_to_display("1") == "第一章"
        assert stored_id_to_display("3") == "第三章"
        assert stored_id_to_display("12") == "第十二章"
        assert stored_id_to_display("3.2") == "2"
        assert stored_id_to_display("3.2.4") == "2.4"
        assert stored_id_to_display("3.2.4.5") == "2.4.5"

    def test_display_invalid(self):
        assert stored_id_to_display("") == ""
        assert stored_id_to_display("abc") == ""

    def test_prefix(self):
        assert stored_id_to_prefix("3") == "3"
        assert stored_id_to_prefix("3.2") == "2"
        assert stored_id_to_prefix("3.2.4") == "2.4"
        assert stored_id_to_prefix("坏") == ""

    def test_stored_outline_id(self):
        assert stored_outline_id({"outline_json": json.dumps({"id": "2.1"})}) == "2.1"
        # UUID / 非点分路径 / 非法 JSON / 缺失 → 空
        assert stored_outline_id({"outline_json": json.dumps(
            {"id": "6fa8-...-uuid"})}) == ""
        assert stored_outline_id({"outline_json": "{坏"}) == ""
        assert stored_outline_id({"outline_json": ""}) == ""
        assert stored_outline_id({}) == ""
        # dict 形态容错
        assert stored_outline_id({"outline_json": {"id": "2"}}) == "2"


# ============================================================
# 5. 正文子标题编号规范化（核心：预览 = 落库 = 导出）
# ============================================================
class TestRenumberSectionBodySubheadings:
    def test_level2_renumber(self):
        content = "## 总体安排\n\n正文段落。\n\n### 关键节点\n\n更多。\n\n## 资源配置\n"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "## 2.1 总体安排" in new
        assert "### 2.1.1 关键节点" in new
        assert "## 2.2 资源配置" in new
        assert len(changes) == 3
        assert changes[0]["line"] == 1  # 1 基行号

    def test_ai_wrong_base_number_corrected(self):
        # AI 以存储编号（3.2）为基准自算 3.2.1 —— 导出口径应为展示编号 2.1
        content = "## 3.2.1 总体安排\n\n### 3.2.1.1 关键节点\n"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "## 2.1 总体安排" in new
        assert "### 2.1.1 关键节点" in new
        assert len(changes) == 2

    def test_level1_uses_chapter_prefix(self):
        content = "## 总体安排\n\n## 资源配置\n"
        new, _ = renumber_section_body_subheadings(content, "3", 1, "施工工艺")
        assert "## 3.1 总体安排" in new
        assert "## 3.2 资源配置" in new

    def test_plain_heading_and_bold_rewritten(self):
        content = "2 总体安排\n\n**3 资源配置**\n"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "2.1 总体安排" in new
        assert "2.2 资源配置" in new          # **加粗**包裹的纯文本标题整行规范化
        assert "**" not in new
        assert len(changes) == 2

    def test_fence_and_list_and_paragraph_untouched(self):
        content = (
            "```text\n### 围栏内不是标题\n```\n\n"
            "1. 先检查后作业\n"
            "2、先验收后进入\n"
            "3.1.12 身份证复印件、照片；\n"
            "# 完成后清理现场，做到工完场清。\n\n"
            "## 真标题\n"
        )
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "### 围栏内不是标题" in new            # 代码围栏内不动
        assert "1. 先检查后作业" in new               # 有序列表不动
        assert "2、先验收后进入" in new               # 中文顿号列表不动
        assert "3.1.12 身份证复印件、照片；" in new   # 句末标点 → 正文行不动
        assert "# 完成后清理现场，做到工完场清。" in new  # 降级段落不动
        assert "## 2.1 真标题" in new
        assert len(changes) == 1

    def test_leading_duplicate_title_skipped(self):
        # 正文首行自引用本节标题：导出渲染会丢弃该块，规范化不给它占号
        content = "## 进度计划\n\n## 总体安排\n"
        new, changes = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        assert "## 进度计划" in new          # 保持原样（导出时被剥）
        assert "## 2.1 总体安排" in new      # 从 2.1 起算而非 2.2
        assert len(changes) == 1

    def test_idempotent(self):
        content = "## 总体安排\n\n### 关键节点\n\n2 资源配置\n"
        once, _ = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        twice, changes2 = renumber_section_body_subheadings(once, "3.2", 2, "进度计划")
        assert twice == once
        assert changes2 == []

    def test_idempotent_with_jump_depths(self):
        # 先深后浅 / 深度跳跃：重写后行形态从 # 标题变为纯文本编号标题，
        # 相对深度算法必须保证二次运行零改动（编号与首次一致）
        content = "## 项目概况\n\n#### 立面概况\n\n## 建筑概况\n"
        once, _ = renumber_section_body_subheadings(content, "3.2", 2, "概况")
        twice, changes2 = renumber_section_body_subheadings(once, "3.2", 2, "概况")
        assert twice == once
        assert changes2 == []
        assert "2.1 项目概况" in once
        assert "2.1.1 立面概况" in once
        assert "2.2 建筑概况" in once

    def test_invalid_section_number_returns_original(self):
        content = "## 总体安排\n"
        for bad in ("", "6fa8-uuid", "abc"):
            new, changes = renumber_section_body_subheadings(content, bad, 2)
            assert new == content
            assert changes == []

    def test_export_parity(self):
        # 终极口径验证：规范化后的正文再走导出端 _compute_subheading，
        # 得到的编号必须与正文行内已有的编号逐字一致（即二次运行零变化）
        from app.routers.export import _compute_subheading, _parse_content_blocks
        content = "## 总体安排\n\n### 关键节点\n\n## 资源配置\n"
        normalized, _ = renumber_section_body_subheadings(content, "3.2", 2, "进度计划")
        blocks = _parse_content_blocks(normalized)
        counters: dict = {}
        for b in blocks:
            if b.get("type") != "heading":
                continue
            from app.routers.export import _strip_title_number
            text, _style = _compute_subheading(
                "2", 2, b.get("level", 1), counters,
                _strip_title_number(b.get("text", "")), "")
            # 导出重算的编号 == 正文行内已有编号（去掉标题文本后对比前缀）
            assert b["text"].startswith(text.split(" ")[0] + " ")


# ============================================================
# 6. heading 块 src_line
# ============================================================
class TestSrcLine:
    def test_md_and_plain_heading_carry_src_line(self):
        from app.routers.export import _parse_content_blocks
        blocks = _parse_content_blocks("# A\n\n正文\n\n2.1 B\n")
        assert blocks[0]["src_line"] == 0
        assert blocks[1]["type"] == "paragraph"          # 正文
        assert blocks[2]["type"] == "heading"
        assert blocks[2]["src_line"] == 4                # 0 基源行号


# ============================================================
# 7. 附录组标题自定义样式（不进 TOC）
# ============================================================
class TestAppendixStyle:
    def test_style_outline_level_9_and_idempotent(self):
        from docx import Document
        from docx.oxml.ns import qn
        from app.routers.export import _get_or_add_appendix_heading_style
        doc = Document()
        style = _get_or_add_appendix_heading_style(doc)
        ol = style.element.get_or_add_pPr().find(qn("w:outlineLvl"))
        assert ol is not None and ol.get(qn("w:val")) == "9"   # 正文文本，不进 TOC \o
        assert style.base_style.name == "Heading 2"            # 外观继承二级标题
        # 同一文档重复获取必须复用（幂等，不重复创建）。
        # 注意：python-docx 每次按名查找都会新建包装对象，只能比较 style_id。
        again = _get_or_add_appendix_heading_style(doc)
        assert again.style_id == style.style_id
        assert len([st for st in doc.styles if st.name == "Appendix Group Heading"]) == 1
        # 可直接用于段落
        p = doc.add_paragraph("工期管理", style=style)
        assert p.text == "工期管理"


# ============================================================
# 8. 导出预检：存储编号 vs 结构重算编号
# ============================================================
class TestSectionNumberMismatch:
    def test_mismatch_detected(self):
        from app.routers.export import _detect_section_number_mismatch
        sections = [
            {"id": "a", "parent_id": "", "level": 1, "sort_order": 0,
             "title": "第一章", "outline_json": json.dumps({"id": "1"})},
            {"id": "b", "parent_id": "a", "level": 2, "sort_order": 1,
             "title": "子章", "outline_json": json.dumps({"id": "9.9"})},
        ]
        issues = _detect_section_number_mismatch(sections)
        assert len(issues) == 1
        it = issues[0]
        assert it["type"] == "section_number_mismatch"
        assert it["section_id"] == "b"
        assert it["stored"] == "9.9" and it["expected"] == "1.1"

    def test_consistent_tree_clean(self):
        from app.routers.export import _detect_section_number_mismatch
        sections = [
            {"id": "a", "parent_id": "", "level": 1, "sort_order": 0,
             "title": "一", "outline_json": json.dumps({"id": "1"})},
            {"id": "b", "parent_id": "a", "level": 2, "sort_order": 1,
             "title": "1.1", "outline_json": json.dumps({"id": "1.1"})},
            {"id": "c", "parent_id": "", "level": 1, "sort_order": 2,
             "title": "二", "outline_json": json.dumps({"id": "2"})},
        ]
        assert _detect_section_number_mismatch(sections) == []

    def test_missing_or_invalid_stored_skipped(self):
        from app.routers.export import _detect_section_number_mismatch
        sections = [
            {"id": "a", "parent_id": "", "level": 1, "sort_order": 0,
             "title": "历史行", "outline_json": ""},                       # 缺失 → 不报
            {"id": "b", "parent_id": "", "level": 1, "sort_order": 1,
             "title": "UUID行", "outline_json": json.dumps({"id": "x-y-z"})},  # 非法 → 不报
        ]
        assert _detect_section_number_mismatch(sections) == []

    def test_capped_at_20(self):
        from app.routers.export import _detect_section_number_mismatch
        sections = [
            {"id": f"s{i}", "parent_id": "", "level": 1, "sort_order": i,
             "title": f"章{i}", "outline_json": json.dumps({"id": "99"})}
            for i in range(30)
        ]
        assert len(_detect_section_number_mismatch(sections)) == 20


# ============================================================
# 9. 字符表别名（唯一事实源）
# ============================================================
class TestCharTableAliases:
    def test_heading_v2_and_templates_share_numbering_tables(self):
        assert HeadingNumberingGeneratorV2.CHINESE_NUMBERS is nb.CHINESE_NUMBERS
        assert HeadingNumberingGeneratorV2.ALPHABET is nb.ALPHABET
        assert HT_CHINESE is nb.CHINESE_NUMBERS
        assert HT_ALPHABET is nb.ALPHABET

    def test_generator_still_formats(self):
        gen = HeadingNumberingGeneratorV2()
        assert gen.update_counter(1, "root") == "第一章"
        assert gen.update_counter(2, "root") == "1"
        assert gen.update_counter(3, "c") == "1.1"

    def test_config_default_on(self):
        # 新增配置项默认开启（预览/落库/导出三处同源为任务要求的统一行为）
        from app.config import settings
        assert settings.content_subheading_renumber is True
        # E3 / E6 Tier 1 新增配置项：默认启用统一行为与预检
        assert settings.body_subheading_demote_with_children is True
        assert settings.crossref_stale_detect_enabled is True


# ============================================================
# 11. E3 · 有 DB 子章节时正文子标题降级（2026-09-25）
# ============================================================
class TestBodySubheadingDemotion:
    """有 DB 子章节的章节，正文子标题降级为节内 body 命名空间。

    降级后：正文子标题第一层用「1）、2）、」（L6），更深层用「a、/b、」（L7 字母序列）。
    这样正文子标题（L6+）与 DB 子章节（L3+）在同一文档内使用两个独立的命名空间——
    不再出现「1.1 正文标题」与「1.1 DB 子章节」成对撞号。
    """

    def test_rel0_demotes_to_l6(self):
        """has_children=True、相对深度 0 → 1）、标题（L6，顿号、无空格）。"""
        from app.routers.export import _compute_subheading
        sub = {}
        text, style = _compute_subheading("2", 2, 1, sub, "总体安排",
                                         sec_id="sec-1", has_children=True)
        assert text == "1）、总体安排"
        assert style == 6  # L6 Heading
        text2, _ = _compute_subheading("2", 2, 1, sub, "资源配置",
                                       sec_id="sec-1", has_children=True)
        assert text2 == "2）、资源配置"

    def test_rel1_demotes_to_l7_lowercase(self):
        """has_children=True、相对深度 1 → a、标题（L7，顿号、无空格）。"""
        from app.routers.export import _compute_subheading
        sub = {}
        _compute_subheading("2", 2, 1, sub, "总体安排", has_children=True)  # rel0 → 1）
        text, style = _compute_subheading("2", 2, 2, sub, "劳动组织",
                                         sec_id="sec-1", has_children=True)  # rel1
        assert text == "a、劳动组织"
        assert style == 7

    def test_rel2_continues_l7(self):
        """has_children=True、相对深度 2 → b、标题（L7 字母序列续排）。"""
        from app.routers.export import _compute_subheading
        sub = {}
        _compute_subheading("2", 2, 1, sub, "总体安排", has_children=True)
        _compute_subheading("2", 2, 2, sub, "劳动组织", has_children=True)
        text, style = _compute_subheading("2", 2, 3, sub, "工种分配",
                                         sec_id="sec-1", has_children=True)
        assert text == "b、工种分配"
        assert style == 7

    def test_no_demote_when_no_children(self):
        """has_children=False（无子章节）时保持旧格式 X.X.X。"""
        from app.routers.export import _compute_subheading
        sub = {}
        text, style = _compute_subheading("2", 2, 1, sub, "总体安排",
                                         sec_id="sec-1", has_children=False)
        # 无子章节时不降级，沿用原点分编号
        assert text == "2.1 总体安排"
        assert style == 3  # L3 Heading
        text2, _ = _compute_subheading("2", 2, 2, sub, "劳动组织",
                                       sec_id="sec-1", has_children=False)
        assert text2 == "2.1.1 劳动组织"

    def test_idempotent_after_demote(self):
        """降级输出再跑一次同样参数 → 结果不变（幂等）。"""
        from app.routers.export import _compute_subheading
        sub = {}
        t1, s1 = _compute_subheading("2", 2, 1, sub, "总体安排",
                                     sec_id="sec-1", has_children=True)
        sub2 = {}  # 计数器重置
        t2, s2 = _compute_subheading("2", 2, 1, sub2, "总体安排",
                                     sec_id="sec-1", has_children=True)
        assert t1 == t2 == "1）、总体安排"
        assert s1 == s2 == 6

    def test_mixed_depth_counter_resets_properly(self):
        """混合深度：rel0 重开后从新序列 1）起，rel1 从 a、起。"""
        from app.routers.export import _compute_subheading
        sub = {}
        _compute_subheading("2", 2, 1, sub, "A", has_children=True)  # 1）
        _compute_subheading("2", 2, 2, sub, "A1", has_children=True)  # a、
        _compute_subheading("2", 2, 2, sub, "A2", has_children=True)  # b、
        # rel0 回跳 → rel0 计数器续排（不是重置）
        t, _ = _compute_subheading("2", 2, 1, sub, "B", has_children=True)
        # 相对深度 0 计数器在原 sub 上继续递增；L6 格式为 'N）、标题'（顿号）
        parts = t.split("）", 1)
        assert parts[1].strip() == "、B"
        assert int(parts[0].strip()) >= 2


# ============================================================
# 12. E6 Tier 1 · 失效交叉引用检测（DLV-14）
# ============================================================
class TestStaleCrossRefDetection:
    """正文硬编码的图号/表号/节号与导出时真实分配集合不一致时，预检告警。

    这是「图号漂移」的主要表现之一：用户在正文里手动引用「图3-2 为进度曲线」，
    但目录重排后第 3 章可能变成第 2 章，真实图号变为「图2-1」；预检在导出前就能
    发现这类漂移，提醒用户修复或人工确认。
    """

    def test_stale_figure_ref(self):
        from app.routers.export import _detect_stale_cross_references
        sections = [
            {"id": "s1", "parent_id": "", "level": 1,
             "outline_json": '{"id": "1"}',
             "content": "正文引用 图1-1 为劳动力图；另有一处图3-2 已经漂移。"},
        ]
        # 真实分配集合：图1-1 / 图1-2（导出端实际分配）
        real = {"fig": {"图1-1", "图1-2"}, "tbl": set(), "sec": set()}
        issues = _detect_stale_cross_references(sections, real)
        # 只应检出 stale 的「图3-2」，不应误报「图1-1」
        stale_fig = [i for i in issues if "图" in str(i.get("ref") or "")]
        assert any("图3-2" in str(i.get("ref") or "") for i in stale_fig)
        assert not any("图1-1" in str(i.get("ref") or "") for i in stale_fig)

    def test_stale_table_ref(self):
        from app.routers.export import _detect_stale_cross_references
        sections = [{"id": "s1", "content": "详见 表1-1 和 表5-9。"}]
        real = {"fig": set(), "tbl": {"表1-1"}, "sec": set()}
        issues = _detect_stale_cross_references(sections, real)
        assert any("表5-9" in str(i.get("ref") or "") for i in issues)

    def test_stale_section_ref(self):
        from app.routers.export import _detect_stale_cross_references
        sections = [{"id": "s1", "content": "具体做法见 第2.3节。"}]
        real = {"fig": set(), "tbl": set(), "sec": {"第1章", "第2章", "1", "2", "2.1", "2.2"}}
        issues = _detect_stale_cross_references(sections, real)
        # 真实集合里没有 2.3 → 报告 stale
        assert any("2.3" in str(i.get("ref") or "") for i in issues)

    def test_clean_refs_no_false_positive(self):
        """所有引用均在真实分配集合内 → 空 issues。"""
        from app.routers.export import _detect_stale_cross_references
        sections = [{"id": "s1",
                     "content": "图1-1 为进度计划；表1-1 为工程量；详见第2章。"}]
        real = {"fig": {"图1-1"}, "tbl": {"表1-1"}, "sec": {"第1章", "第2章"}}
        issues = _detect_stale_cross_references(sections, real)
        assert issues == []

    def test_empty_content_skipped(self):
        """空章节 content 不参与检测。"""
        from app.routers.export import _detect_stale_cross_references
        sections = [{"id": "s1", "content": ""}]
        issues = _detect_stale_cross_references(sections,
                                                {"fig": set(), "tbl": set(), "sec": set()})
        assert issues == []
