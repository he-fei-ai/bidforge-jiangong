# -*- coding: utf-8 -*-
"""R83 护栏：R80 §6-1 数据字典双口径（requirement 标注，不与 fact 误互斥）。

设计事实：``build_global_data_dictionary`` 的键即压形后的 ``value_text``（无对象
维度时），故「要求≤90天」与「采用85天」本就是两个独立键、天然不互斥——§6-1 的
真实价值是给 **requirement 条目标注口径**（独立建键、值位留空由渲染回退注记），
防止 AI 把招标/规范要求值误当本方案采用值引用；同值的要求/采用行则把要求源收进
``requirements`` 桶而非 ``conflicting``。

  A. requirement-only 条目：独立建键、value 空、requirements 收要求值、口径标记
     requirement、渲染带「招标/规范要求口径」注记且不含「另有候选取值」；
  B. requirement + fact 不同取值 → 两个独立键，均不进 conflicting；
  C. requirement + fact 同键（同值）→ 要求源进 requirements、不冲突、口径为 fact；
  D. iter_consistency_values 产出携带 value_kind；
  E. dict 行输入 + 脏 value_kind 归一 ''；
  F. 采用值 + 要求口径同键共存 → 渲染单列「另含招标/规范要求口径」。

运行：pytest tests/test_data_dict_value_kind_r83_20261016.py -q
"""
from __future__ import annotations

from app.services import content_data_contract as c


# 6 元组注入契约：(group_type, title, content, confidence, chapter, value_kind)
# ⚠️ requirement 内容须可被抽取（"不"非桥接系词，会阻断"不超过90"之类取值——
#    那是既有抽取限制，与本护栏无关；此处用"工期: 90日历天"形式保证可抽）。
def _fact(vk: str, content: str, title: str = "工期") -> tuple:
    return ("proj", title, content, 1.0, "overview", vk)


class TestRequirementCaliberAnnotation:
    def test_requirement_only_entry_annotated(self):
        d = c.build_global_data_dictionary([
            _fact("requirement", "- **工期**: 90日历天"),
        ])
        e = d["工期90天"]
        assert e["value"] == "", e["value"]
        assert e["value_kind"] == "requirement"
        assert e["requirements"] == ["工期90天"], e["requirements"]
        assert e["conflicting"] == []
        block = c.render_data_dictionary_block(d)
        assert "招标/规范要求口径" in block
        assert "另有候选取值" not in block  # 要求值不是 conflicting

    def test_requirement_and_fact_separate_keys(self):
        """不同取值 → 两个独立字典键，天然不互斥（无虚假冲突）。"""
        d = c.build_global_data_dictionary([
            _fact("fact", "- **工期**: 85日历天"),
            _fact("requirement", "- **工期**: 90日历天"),
        ])
        assert "工期85天" in d and "工期90天" in d
        assert d["工期85天"]["conflicting"] == []
        assert d["工期90天"]["value_kind"] == "requirement"
        assert d["工期85天"]["value_kind"] == "fact"

    def test_requirement_same_key_into_requirements(self):
        """同键（同值 85 天）的要求源与采用源 → 不冲突，要求源进 requirements。"""
        d = c.build_global_data_dictionary([
            _fact("requirement", "- **工期**: 85日历天"),
            _fact("fact", "- **工期**: 85日历天"),
        ])
        e = d["工期85天"]
        assert e["value"] == "工期85天", e["value"]
        assert e["value_kind"] == "fact"
        assert e["conflicting"] == []
        assert "工期85天" in e["requirements"]


class TestValueKindThreading:
    def test_iter_yields_value_kind(self):
        hits = list(c.iter_consistency_values([
            _fact("requirement", "- **工期**: 90日历天"),
        ]))
        assert hits, "应当抽到一条"
        assert all(h["value_kind"] == "requirement" for h in hits), hits

    def test_dict_rows_value_kind(self):
        d = c.build_global_data_dictionary([
            {"title": "工期", "content": "- **工期**: 85日历天",
             "confidence": 1.0, "chapter": "overview", "value_kind": "fact"},
            {"title": "工期", "content": "- **工期**: 90日历天",
             "confidence": 1.0, "chapter": "overview", "value_kind": "requirement"},
        ])
        assert "工期85天" in d and "工期90天" in d
        assert d["工期85天"]["value_kind"] == "fact"
        assert d["工期90天"]["value_kind"] == "requirement"

    def test_illformed_value_kind_normalized(self):
        """脏 value_kind 归一 ''（与 normalize_value_kind 同口径）。"""
        d = c.build_global_data_dictionary([
            {"title": "工期", "content": "- **工期**: 85日历天",
             "confidence": 1.0, "chapter": "overview", "value_kind": "bogus"},
        ])
        assert d["工期85天"]["value_kind"] == "", d["工期85天"]["value_kind"]


class TestRenderRequirementFallback:
    def test_adopted_and_requirement_both_shown(self):
        """采用值 + 要求口径同主题（不同键）共存 → 各自展示，不相互标冲突。"""
        d = c.build_global_data_dictionary([
            _fact("fact", "- **工期**: 85日历天"),
            _fact("requirement", "- **工期**: 90日历天"),
        ])
        block = c.render_data_dictionary_block(d)
        assert "工期85天" in block
        assert "工期90天" in block
