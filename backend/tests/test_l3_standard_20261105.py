# -*- coding: utf-8 -*-
"""《L3 以下“少目录、多正文”编制标准》落地回归（2026-11-05）。

覆盖：
A 目录规模统计 / 确定性收敛（单节、单章、全文软目标/硬开关、占位保护、编号连续）；
B 正文标准下限（普通 1500 / 关键 2500）、自然段计数、提示词注入；
C 校验侧 DLV-17（目录过深/节点过多）、DLV-18（正文单薄）命中与不命中；
D 配置开关关闭后逐字回退（不收敛、不注入、不报规则）。
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.services import l3_standard
from app.services.audit_rules import _PROGRAM_EMITTED_RULE_IDS, RULE_VERSION, get_rule
from app.services.content_checkpoint import build_content_system_checkpoint_block
from app.services.preflight_engine import PreflightContext, check_deliverability


def _node(title: str, children: list | None = None, *, desc: str = "") -> dict:
    return {"title": title, "description": desc or f"{title}的说明", "children": children or []}


def _tree(chapters: int = 2, sections: int = 2, leaves: int = 3) -> list:
    return [
        _node(
            f"第{i + 1}章 章标题",
            [_node(f"第{i + 1}.{j + 1}节 节标题", [_node(f"第{i + 1}.{j + 1}.{k + 1}条 三级标题") for k in range(leaves)]) for j in range(sections)],
        )
        for i in range(chapters)
    ]


# ===========================================================================
# A 目录规模统计 / 收敛
# ===========================================================================
class TestTreeStats:
    def test_counts_depth_and_limits(self):
        st = l3_standard.tree_stats(_tree(2, 2, 3))
        assert st["l3_total"] == 12
        assert st["max_depth"] == 3
        assert st["over_depth"] == 0
        assert st["chapters"][0]["l3"] == 6
        assert len(st["sections"]) == 4

    def test_deeper_than_l3_is_reported(self):
        outline = [_node("第1章", [_node("1.1 节", [_node("1.1.1 三级", [_node("1.1.1.1 四级")])])])]
        st = l3_standard.tree_stats(outline)
        assert st["max_depth"] == 4
        assert st["over_depth"] == 1


class TestConverge:
    def test_section_cap_not_merged_by_default(self):
        """默认 L3 不做任何硬性要求：单节 8 个三级也不合并，只由 DLV-17 告警。"""
        outline = [_node("第1章", [_node("1.1 节", [_node(f"三级{i}") for i in range(8)])])]
        res = l3_standard.converge_tree(outline)
        kids = outline[0]["children"][0]["children"]
        assert len(kids) == 8
        assert res["merged"] == 0
        assert res["skipped"] is True

    def test_section_cap_merges_when_enforced(self, monkeypatch):
        monkeypatch.setattr(settings, "outline_l3_converge_enforced", True)
        outline = [_node("第1章", [_node("1.1 节", [_node(f"三级{i}") for i in range(8)])])]
        res = l3_standard.converge_tree(outline)
        kids = outline[0]["children"][0]["children"]
        assert len(kids) == settings.outline_l3_max_per_section
        assert res["merged"] == 3
        assert "含：" in kids[-1]["description"]
        assert "三级7" in kids[-1]["description"]

    def test_ids_are_continuous_after_enforced_converge(self, monkeypatch):
        monkeypatch.setattr(settings, "outline_l3_converge_enforced", True)
        outline = [_node("第1章", [_node("1.1 节", [_node(f"三级{i}") for i in range(7)])])]
        l3_standard.converge_tree(outline)
        kids = outline[0]["children"][0]["children"]
        assert [k["id"] for k in kids] == [f"1.1.{i + 1}" for i in range(len(kids))]

    def test_chapter_cap_respects_two_per_section_when_enforced(self, monkeypatch):
        # 2 个二级 × 4 个三级 = 8；有效单章上限 = max(5, 2×2)=5，收敛到 5 且每节≥2
        monkeypatch.setattr(settings, "outline_l3_converge_enforced", True)
        outline = [_node("第1章", [_node(f"1.{j}节", [_node(f"三级{j}-{k}") for k in range(4)]) for j in range(2)])]
        l3_standard.converge_tree(outline)
        counts = [len(sec["children"]) for sec in outline[0]["children"]]
        assert sum(counts) == 5
        assert min(counts) >= 2

    def test_total_cap_not_merged_by_default(self):
        """全文 40 只是建议：默认连单章超限都不合并，L3 数量原样保留。"""
        outline = _tree(10, 2, 3)  # 60 个三级
        res = l3_standard.converge_tree(outline)
        assert l3_standard.tree_stats(outline)["l3_total"] == 60
        assert res["merged"] == 0

    def test_total_cap_enforced_when_switch_on(self, monkeypatch):
        """显式打开 outline_l3_converge_enforced 才恢复硬收敛到 40。"""
        monkeypatch.setattr(settings, "outline_l3_converge_enforced", True)
        outline = _tree(10, 2, 3)
        l3_standard.converge_tree(outline)
        assert l3_standard.tree_stats(outline)["l3_total"] == settings.outline_l3_max_total

    def test_placeholder_never_merged(self, monkeypatch):
        monkeypatch.setattr(settings, "outline_l3_converge_enforced", True)
        outline = [
            _node(
                "第1章",
                [
                    _node(
                        "1.1 节",
                        [_node(f"三级{i}") for i in range(5)] + [_node("附图及节点详图（另行补充内容）")],
                    )
                ],
            )
        ]
        l3_standard.converge_tree(outline)
        titles = [k["title"] for k in outline[0]["children"][0]["children"]]
        assert "附图及节点详图（另行补充内容）" in titles

    def test_switch_off_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "l3_standard_enabled", False)
        outline = [_node("第1章", [_node("1.1 节", [_node(f"三级{i}") for i in range(8)])])]
        res = l3_standard.converge_tree(outline)
        assert len(outline[0]["children"][0]["children"]) == 8
        assert res["skipped"] is True

    def test_deep_nodes_folded_into_parent_by_default(self):
        """L4/L5 节点默认折叠成父级 L3 的「（1）（2）」段首引导，文字不丢失。"""
        outline = [
            _node(
                "第1章",
                [
                    _node(
                        "1.1 节",
                        [
                            _node(
                                "1.1.1 三级",
                                [
                                    _node("1.1.1.1 四级甲", [_node("1.1.1.1.1 五级甲")], desc="四级甲说明"),
                                    _node("1.1.1.2 四级乙", desc="四级乙说明"),
                                ],
                                desc="三级主说明",
                            )
                        ],
                    )
                ],
            )
        ]
        res = l3_standard.converge_tree(outline)
        leaf = outline[0]["children"][0]["children"][0]
        assert leaf["children"] == []
        assert res["folded"] == 3
        assert l3_standard.tree_stats(outline)["max_depth"] == 3
        # 所有被折叠的文字都在父级 description 中保留
        assert "1.1.1.1 四级甲" in leaf["description"]
        assert "四级甲说明" in leaf["description"]
        assert "1.1.1.1.1 五级甲" in leaf["description"]
        assert "1.1.1.2 四级乙" in leaf["description"]
        assert leaf["description"].startswith("三级主说明；")
        # 段首引导格式
        assert "（1）" in leaf["description"] and "（2）" in leaf["description"]

    def test_fold_keeps_numbering_continuous(self):
        outline = [
            _node("第1章", [_node("1.1 节", [_node("1.1.1 三级", [_node("1.1.1.1 四级")])])]),
            _node("第2章", [_node("2.1 节", [_node("2.1.1 三级")])]),
        ]
        l3_standard.converge_tree(outline)
        assert outline[0]["id"] == "1"
        assert outline[0]["children"][0]["id"] == "1.1"
        assert outline[0]["children"][0]["children"][0]["id"] == "1.1.1"
        assert outline[1]["children"][0]["children"][0]["id"] == "2.1.1"


# ===========================================================================
# B 正文下限 / 自然段 / 提示词
# ===========================================================================
class TestContentFloor:
    def test_normal_and_key_floor(self):
        assert l3_standard.word_floor("施工工艺流程") == 1500
        assert l3_standard.word_floor("结构加固施工方法") == 2500
        assert l3_standard.word_floor("消防调试要点") == 2500

    def test_key_keywords_cover_lifting_scaffold_deep_pit(self):
        """2026-11-05 口径：吊装/脚手架/深基坑（含常见变体）按关键 L3 2500 字。"""
        for title in ("起重吊装施工", "落地脚手架搭设", "深基坑支护施工", "脚手架拆除"):
            assert l3_standard.is_key_l3_title(title) is True, title
            assert l3_standard.word_floor(title) == 2500, title

    def test_plain_title_not_key(self):
        assert l3_standard.is_key_l3_title("施工工艺流程") is False
        assert l3_standard.word_floor("施工工艺流程") == 1500

    def test_apply_floor_only_raises(self):
        assert l3_standard.apply_word_floor(1200, "施工工艺流程") == 1500
        assert l3_standard.apply_word_floor(2500, "施工工艺流程") == 2500
        assert l3_standard.apply_word_floor(800, "结构加固施工方法") == 2500

    def test_floor_switch_off(self, monkeypatch):
        monkeypatch.setattr(settings, "content_l3_word_floor_enabled", False)
        assert l3_standard.apply_word_floor(1200, "施工工艺流程") == 1200

    def test_paragraph_counter_ignores_structure(self):
        content = (
            "## 小标题\n\n"
            "第一段正文，只做计数用。\n\n"
            "第二段正文，只做计数用。\n\n"
            "| 项目 | 参数 |\n|---|---|\n| 支护 | C30 |\n\n"
            "- 列表项一\n- 列表项二\n\n"
            "```\n代码块\n```\n\n"
            "第三段正文，只做计数用。\n"
        )
        assert l3_standard.count_body_paragraphs(content) == 3

    def test_prompt_block_injected_by_default(self):
        block = build_content_system_checkpoint_block("深基坑专项施工方案", "")
        assert "少目录、多正文" in block
        assert "不少于 1500 字" in block
        assert "不少于 2500 字" in block
        assert "3-5 个自然段" in block

    def test_prompt_block_switch_off(self, monkeypatch):
        monkeypatch.setattr(settings, "l3_standard_enabled", False)
        block = build_content_system_checkpoint_block("深基坑专项施工方案", "")
        assert "少目录、多正文" not in block


class TestOutlinePrompt:
    def test_outline_prompts_mark_l3_scale_as_advisory(self):
        from app.services.ai.prompts._cache import get_prompt

        short = get_prompt("outline_short_system")
        assert "宜 ≤40" in short
        assert "三级数量不设硬性要求" in short
        assert "严禁生成第四级及更深目录节点" in short
        batch = get_prompt("outline_sublevel_batch_system")
        assert "全文三级宜 ≤40" in batch
        assert "数量不设硬性要求" in batch
        assert "严禁生成第四级及更深目录节点" in batch


# ===========================================================================
# C 校验侧 DLV-17 / DLV-18
# ===========================================================================
def _sec(sid, title, level, parent_id="", content="", wc=0, budget=0, status="generated"):
    return {
        "id": sid,
        "title": title,
        "level": level,
        "parent_id": parent_id,
        "content": content,
        "word_count": wc,
        "word_budget": budget,
        "status": status,
    }


def _deliverability(sections):
    return check_deliverability(PreflightContext(sections=sections))


def _ids(findings):
    return [f["rule_id"] for f in findings]


class TestDLV17:
    def test_over_limit_flagged(self):
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            *[_sec(f"l{i}", f"1.1.{i} 三级", 3, "s1", content="正文", wc=10) for i in range(8)],
        ]
        finds = _deliverability(sections)
        assert "DLV-17" in _ids(finds)
        f = next(x for x in finds if x["rule_id"] == "DLV-17")
        assert "超过每节" in f["detail"]

    def test_compliant_outline_not_flagged(self):
        sections = [_sec("c1", "第1章", 1), _sec("s1", "1.1 节", 2, "c1")]
        for i in range(4):
            sections.append(
                _sec(f"l{i}", f"1.1.{i} 三级", 3, "s1", content="正文" * 200, wc=800)
            )
        finds = _deliverability(sections)
        assert "DLV-17" not in _ids(finds)

    def test_l4_node_flagged(self):
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 三级", 3, "s1"),
            _sec("d1", "1.1.1.1 四级", 4, "l1"),
        ]
        finds = _deliverability(sections)
        assert "DLV-17" in _ids(finds)

    def test_total_over_soft_target_is_flagged(self):
        """全文 45 > 40 只做 DLV-17 提示（收敛侧默认不合并）。"""
        sections = []
        for c in range(9):
            sections.append(_sec(f"c{c}", f"第{c + 1}章", 1))
            sections.append(_sec(f"s{c}", f"{c + 1}.1 节", 2, f"c{c}"))
            for k in range(5):
                sections.append(
                    _sec(f"l{c}-{k}", f"{c + 1}.1.{k + 1} 三级", 3, f"s{c}", content="正文", wc=10)
                )
        finds = _deliverability(sections)
        assert "DLV-17" in _ids(finds)
        f = next(x for x in finds if x["rule_id"] == "DLV-17")
        assert "全文三级条目 45 个" in f["detail"]


class TestDLV18:
    def test_thin_words_and_paragraphs_flagged(self):
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 三级", 3, "s1", content="太短了。", wc=4),
        ]
        finds = _deliverability(sections)
        assert "DLV-18" in _ids(finds)
        f = next(x for x in finds if x["rule_id"] == "DLV-18")
        assert "字数不足" in "".join(f["evidence"])
        assert "段落不足" in "".join(f["evidence"])

    def test_key_l3_uses_2500_threshold(self):
        content = "\n\n".join(["段" * 200] * 4)  # ~800 字，普通达标、关键不达标
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 结构加固施工方法", 3, "s1", content=content, wc=800),
        ]
        finds = _deliverability(sections)
        assert "DLV-18" in _ids(finds)

    def test_lifting_title_uses_2500_threshold(self):
        """“起重吊装”按关键 L3：800 字应被 DLV-18 命中。"""
        content = "\n\n".join(["段" * 200] * 4)
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 起重吊装施工", 3, "s1", content=content, wc=800),
        ]
        finds = _deliverability(sections)
        assert "DLV-18" in _ids(finds)

    def test_placeholder_not_flagged(self):
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec(
                "l1",
                "1.1.1 计算书（另行补充）",
                3,
                "s1",
                content="另行补充内容。",
                wc=6,
                status="placeholder",
            ),
        ]
        finds = _deliverability(sections)
        assert "DLV-18" not in _ids(finds)

    def test_standard_content_not_flagged(self):
        content = "\n\n".join(["本段正文用于校验，内容长度足够。" * 8] * 4)
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 普通三级", 3, "s1", content=content, wc=1600),
        ]
        finds = _deliverability(sections)
        assert "DLV-18" not in _ids(finds)


# ===========================================================================
# D 注册表一致性 / 开关回退
# ===========================================================================
class TestRegistry:
    def test_rules_registered_and_version_bumped(self):
        assert get_rule("DLV-17") is not None
        assert get_rule("DLV-18") is not None
        assert "DLV-17" in _PROGRAM_EMITTED_RULE_IDS
        assert "DLV-18" in _PROGRAM_EMITTED_RULE_IDS
        assert RULE_VERSION >= "1.11.0"

    def test_switch_off_silences_rules(self, monkeypatch):
        monkeypatch.setattr(settings, "l3_standard_enabled", False)
        sections = [
            _sec("c1", "第1章", 1),
            _sec("s1", "1.1 节", 2, "c1"),
            _sec("l1", "1.1.1 三级", 3, "s1", content="太短了。", wc=4),
        ]
        finds = _deliverability(sections)
        assert "DLV-17" not in _ids(finds)
        assert "DLV-18" not in _ids(finds)
