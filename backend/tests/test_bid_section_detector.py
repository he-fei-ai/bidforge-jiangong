"""标段检测（bid_section_detector）单元测试。

覆盖中文数字归一、显式总数声明、16 种定义模式、括号编号、合并列举排除、
文号行排除，以及 detect_bid_sections 的端到端判定。这些规则直接驱动
/bid-analysis/check-sections 与前端「多标段」提示，历史上无任何单测覆盖
（全仓 grep 0 命中），属高风险盲区，现补齐回归基线。
"""
import pytest
from app.services.bid_section_detector import (
    count_bracket_sections,
    count_definition_sections,
    detect_bid_sections,
    detect_total_section_count,
    normalize_chinese_number,
)

# ---------------------------------------------------------------------------
# 中文数字归一
# ---------------------------------------------------------------------------

class TestNormalizeChineseNumber:
    @pytest.mark.parametrize("s,expect", [
        ("3", 3),
        ("12", 12),
        ("99", 99),
        ("一", 1),
        ("九", 9),
        ("十", 10),
        ("十一", 11),
        ("二十", 20),
        ("二十三", 23),
        ("壹", 1),
        ("伍", 5),
    ])
    def test_valid(self, s, expect):
        assert normalize_chinese_number(s) == expect

    @pytest.mark.parametrize("s", ["0", "100", "零", "", "abc", "一二三", "十十"])
    def test_invalid(self, s):
        assert normalize_chinese_number(s) is None


# ---------------------------------------------------------------------------
# 显式总数声明
# ---------------------------------------------------------------------------

class TestDetectTotalSectionCount:
    def test_explicit_section(self):
        assert detect_total_section_count("本项目划分为 3 个标段") == 3

    def test_explicit_package_chinese(self):
        assert detect_total_section_count("共分为五个标包") == 5

    def test_explicit_subpackage(self):
        assert detect_total_section_count("共2个分包") == 2

    def test_single_section_ignored(self):
        # 单标段不构成多标段依据，返回 None（1 由 detect_bid_sections 单独处理）
        assert detect_total_section_count("本项目划分为 1 个标段") is None

    def test_none(self):
        assert detect_total_section_count("没有任何标段声明") is None

    def test_bare_definition_not_total(self):
        # ✅ 回归（2026-09-25）：裸写的定义式「三标段：」不得被误判为"声明共3段"
        assert detect_total_section_count("一标段：\n二标段：\n三标段：") is None

    def test_prefers_section_over_package(self):
        # 同时出现"标段2"与"包5"时，标段优先
        text = "本项目划分为 2 个标段，另含 5 个包"
        assert detect_total_section_count(text) == 2


# ---------------------------------------------------------------------------
# 定义模式
# ---------------------------------------------------------------------------

class TestCountDefinitionSections:
    def test_chinese_section(self):
        cnt, tags = count_definition_sections("一标段：\n二标段：\n三标段：")
        assert cnt == 3
        assert "标段:1" in tags and "标段:3" in tags

    def test_arabic_section(self):
        cnt, _ = count_definition_sections("1标段：\n2标段：")
        assert cnt == 2

    def test_dedup(self):
        # 同一编号重复出现只计一次
        cnt, _ = count_definition_sections("一标段：\n一标段：")
        assert cnt == 1

    def test_combined_mention_excluded(self):
        # "一、二、三标段" 这类列举不应被当作 3 个独立定义
        cnt, _ = count_definition_sections("一、二、三标段均需分别报价")
        assert cnt == 0

    def test_unit_priority(self):
        # "包" 最短放最后，不应抢先命中"标包"
        cnt, tags = count_definition_sections("一标包：\n二标包：")
        assert cnt == 2
        assert all(t.startswith("标包") for t in tags)


# ---------------------------------------------------------------------------
# 括号编号
# ---------------------------------------------------------------------------

class TestCountBracketSections:
    def test_simple_brackets(self):
        cnt, tags = count_bracket_sections("【1】第一章\n【2】第二章")
        assert cnt == 2
        assert "【1】" in tags

    def test_nested_brackets(self):
        # 存在多级编号时以子项为准，避免父子重复计数
        cnt, tags = count_bracket_sections("【2-1】节\n【2-2】节\n【2-3】节")
        assert cnt == 3
        assert all(t.startswith("【2-") for t in tags)

    def test_document_number_line_excluded(self):
        # 文号行（以"号/文"结尾）不应被计为标段
        cnt, _ = count_bracket_sections("【2024】15 号\n这是文号行")
        assert cnt == 0


# ---------------------------------------------------------------------------
# detect_bid_sections 端到端
# ---------------------------------------------------------------------------

class TestDetectBidSections:
    def test_empty(self):
        r = detect_bid_sections("")
        assert r["has_multiple"] is False
        assert r["detected_count"] == 0
        assert r["sections"] == []

    def test_single_declared(self):
        # 单标段声明（count<2）不构成多标段依据，total_declared 为 None
        r = detect_bid_sections("本项目划分为 1 个标段")
        assert r["has_multiple"] is False
        assert r["total_declared"] is None
        assert r["by_total"] is False

    def test_multi_declared(self):
        r = detect_bid_sections("本项目共划分为 4 个标段")
        assert r["has_multiple"] is True
        assert r["total_declared"] == 4
        assert r["detected_count"] == 4
        assert r["by_total"] is True
        assert r["sections"] == ["标段:1", "标段:2", "标段:3", "标段:4"]

    def test_definition_patterns_multi(self):
        r = detect_bid_sections("一标段：xxx\n二标段：yyy\n三标段：zzz")
        assert r["has_multiple"] is True
        assert r["by_total"] is False
        assert r["detected_count"] == 3

    def test_single_definition_not_multi(self):
        r = detect_bid_sections("一标段：xxx")
        assert r["has_multiple"] is False

    def test_brackets_multi(self):
        r = detect_bid_sections("【1】a\n【2】b\n【3】c")
        assert r["has_multiple"] is True
        assert r["detected_count"] == 3
