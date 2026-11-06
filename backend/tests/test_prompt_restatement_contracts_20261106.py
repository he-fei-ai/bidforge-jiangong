"""提示词重述 · 契约护栏（2026-11-06）。

本文件锁定 7 条重述提示词在**契约层**的行为，不锁措辞：占位符齐全、
输出键名与必填字段在、关键禁止项在、指令不重复、长度在界内。措辞可以改，
但只要某条约束从提示词里消失，对应断言必须变红。
"""

from __future__ import annotations

import pytest
from app.services.ai.prompts._registry import _ALL_PROMPTS, PROMPT_VARIABLE_CONTRACTS


def _body(key: str) -> str:
    return _ALL_PROMPTS[key]["default_content"]


def _assert_contract_placeholders(key: str) -> None:
    body = _body(key)
    declared = set(PROMPT_VARIABLE_CONTRACTS[key])
    for var in declared:
        assert "{" + var + "}" in body, f"{key} 缺少已声明占位符 {{{var}}}"


def _assert_length(key: str, low: int, high: int) -> None:
    n = len(_body(key))
    assert low <= n <= high, f"{key} 长度 {n} 越界（{low}~{high}）"


class TestFactsKnowledgePatchContract:
    """知识库补充：只产补丁、不重生成。"""

    KEY = "facts_knowledge_patch_system"

    def test_placeholders_complete(self):
        assert set(PROMPT_VARIABLE_CONTRACTS[self.KEY]) == {"current_facts", "knowledge_text"}
        _assert_contract_placeholders(self.KEY)

    def test_output_schema_keys_present(self):
        body = _body(self.KEY)
        assert '"patches"' in body
        for field in ("target_fact_id", "name", "value", "mode"):
            assert field in body, f"输出契约缺少字段 {field}"

    def test_mode_whitelist_present(self):
        body = _body(self.KEY)
        for mode in ("append", "prepend", "replace"):
            assert mode in body, f"缺 mode 取值 {mode}"

    def test_key_prohibitions_present(self):
        body = _body(self.KEY)
        assert "不得杜撰" in body, "缺「不得杜撰」禁令"
        assert "商务报价" in body, "缺商务报价禁令"
        assert "硬性要求" in body and "招标文件" in body, "缺「不得覆盖招标文件硬性要求」约束"

    def test_instructions_not_duplicated(self):
        body = _body(self.KEY)
        assert body.count("不得杜撰") == 1
        assert body.count("只输出一个 JSON 对象") == 1

    def test_length_within_bound(self):
        _assert_length(self.KEY, 400, 1200)


class TestFactsFinalizeContract:
    """最终整理：去重 + 要求句改写 + 工期类兜底。"""

    KEY = "facts_finalize_system"

    def test_placeholders_complete(self):
        assert set(PROMPT_VARIABLE_CONTRACTS[self.KEY]) == {"current_facts"}
        _assert_contract_placeholders(self.KEY)

    def test_output_schema_keys_present(self):
        body = _body(self.KEY)
        assert '"facts"' in body
        for field in ("fact_key", "name", "value"):
            assert field in body, f"输出契约缺少字段 {field}"

    def test_retention_and_scale_constraints_present(self):
        body = _body(self.KEY)
        for term in ("工期", "运维期", "交货时间"):
            assert term in body, f"缺兜底保留项 {term}"
        assert "大致相同" in body, "缺「输出条数大致相同」约束"
        assert "不得新增" in body, "缺「不得新增」约束"

    def test_instructions_not_duplicated(self):
        body = _body(self.KEY)
        assert body.count("大致相同") == 1
        assert body.count("只输出") == 1

    def test_length_within_bound(self):
        _assert_length(self.KEY, 400, 1200)


class TestBidSectionPromptsContract:
    """多标段识别：只基于带行号文本；合并：不编造、不改行号。"""

    EXTRACT = "bid_section_extract_system"
    MERGE = "bid_section_merge_system"

    def test_both_are_static_prompts(self):
        assert PROMPT_VARIABLE_CONTRACTS[self.EXTRACT] == []
        assert PROMPT_VARIABLE_CONTRACTS[self.MERGE] == []
        for key in (self.EXTRACT, self.MERGE):
            body = _body(key)
            assert "{" not in body and "}" not in body, f"{key} 不应含占位符"

    def test_extract_covers_all_section_units(self):
        body = _body(self.EXTRACT)
        for unit in ("标段", "标包", "分包", "采购包", "包件", "标的"):
            assert unit in body, f"识别对象缺 {unit}"

    def test_extract_is_bounded_to_input_text(self):
        body = _body(self.EXTRACT)
        assert "带行号" in body, "缺「带行号文本」这一输入边界"
        assert "文本之外" in body, "缺「文本之外不作依据」约束"

    def test_merge_forbids_fabrication_and_line_edit(self):
        body = _body(self.MERGE)
        assert "合并" in body
        assert "不得引入新的标段" in body, "缺「不得编造新标段」约束"
        assert "不得改写" in body and "行号" in body, "缺「不得改写行号」约束"

    def test_length_within_bound(self):
        _assert_length(self.EXTRACT, 30, 150)
        _assert_length(self.MERGE, 20, 150)


class TestContentGenerationContract:
    """章节正文生成：输入/基调/硬性要求 + 字数与图表约束。"""

    KEY = "content_generation_system"

    def test_placeholders_complete(self):
        _assert_contract_placeholders(self.KEY)
        assert "{SHARED_SCOPE_RULES_BRIEF}" in _body(self.KEY), "共享红线段必须由运行时解析，不得在注册期焊死"

    def test_writing_tone_constraints_present(self):
        body = _body(self.KEY)
        assert "不是宣传报告" in body, "缺「技术方案而非宣传报告」定位"
        assert "网络用语" in body and "宣传口号" in body, "缺语体禁令"
        assert "具体做法" in body and "控制要点" in body, "缺颗粒度要求"

    def test_format_and_truth_constraints_present(self):
        body = _body(self.KEY)
        assert "严禁输出 HTML 标签" in body, "缺 HTML 禁令"
        assert "表 X-Y" in body, "缺表名格式契约"
        assert "严禁编造" in body, "缺不得编造约束"
        assert "不留占位标记" in body, "缺不留占位标记约束"
        assert "数据真实性红线" in body

    def test_word_and_chart_limits_present(self):
        body = _body(self.KEY)
        assert "0.9X ~ 1.1X" in body
        assert "1.2X" in body, "缺 1.2 倍硬上限"
        assert "本节合计最多 1 个图表块" in body, "配图限额单位必须是「本节」"
        assert "不超过 6 张" in body, "缺全方案 AI 配图上限"

    def test_instructions_not_duplicated(self):
        body = _body(self.KEY)
        assert body.count("不是宣传报告") == 1
        assert body.count("表 X-Y") == 1

    def test_length_within_bound(self):
        _assert_length(self.KEY, 8000, 18000)


class TestContentShrinkContract:
    """正文字数压缩：只出 replace/delete 定点操作。"""

    KEY = "content_shrink_system"

    def test_placeholders_complete(self):
        assert set(PROMPT_VARIABLE_CONTRACTS[self.KEY]) == {
            "current_words",
            "max_rounds",
            "round_no",
            "scheme_name",
            "scheme_type",
            "section_title",
            "target_words",
        }
        _assert_contract_placeholders(self.KEY)

    def test_operation_schema_present(self):
        body = _body(self.KEY)
        assert '"operations"' in body
        for field in ("operation", "target_text", "content"):
            assert field in body, f"操作契约缺少字段 {field}"
        assert "replace" in body and "delete" in body
        assert "整篇重写" in body, "缺「不得整篇重写」约束"

    def test_ratio_limits_present(self):
        body = _body(self.KEY)
        assert "25%" in body, "缺单轮压缩幅度上限"
        assert "10%" in body, "缺接近目标的提前收敛阈值"

    def test_protected_regions_present(self):
        body = _body(self.KEY)
        for region in ("图片", "Mermaid", "代码块", "表格", "列表"):
            assert region in body, f"缺保护区 {region}"

    def test_verbatim_unique_and_fact_safety(self):
        body = _body(self.KEY)
        assert "只出现一次" in body, "缺 target_text 唯一性约束"
        assert "技术参数" in body, "缺事实层禁改约束"
        assert body.count("target_text 必须逐字取自") == 1

    def test_length_within_bound(self):
        _assert_length(self.KEY, 500, 1500)


class TestOutlineJsonFixContract:
    """目录 JSON 修复：最小修改 + 结构补齐 + 三层验收。"""

    KEY = "outline_json_fix_system"

    def test_placeholders_complete(self):
        assert set(PROMPT_VARIABLE_CONTRACTS[self.KEY]) == {
            "invalid_content",
            "issues",
            "target_description",
        }
        _assert_contract_placeholders(self.KEY)

    def test_structure_repair_rules_present(self):
        body = _body(self.KEY)
        assert "三级" in body, "缺三级结构要求"
        assert "2-4" in body and "2-5" in body, "缺目录数量区间"
        assert "第四级" in body, "缺「不得补第四级节点」约束"
        assert "child → children" in body, "缺字段名纠错契约"
        assert "30 字" in body, "缺 description 长度上限"

    def test_acceptance_and_no_explanation(self):
        body = _body(self.KEY)
        for criterion in ("语法合法", "Schema", "业务规则"):
            assert criterion in body, f"缺验收项 {criterion}"
        assert "解释" in body, "缺「不附带解释文字」约束"

    def test_instructions_not_duplicated(self):
        body = _body(self.KEY)
        assert body.count("不整体重写") == 1
        assert body.count("第四级") == 1

    def test_length_within_bound(self):
        _assert_length(self.KEY, 400, 1200)
