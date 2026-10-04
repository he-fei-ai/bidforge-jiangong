"""审核预检误报收口护栏（2026-10-02）

来源：生产库 ``preflight_runs`` / ``consistency_conflicts`` 实证。

两个缺陷的共同根因是 AGENTS.md §4.3/§4.7/§4.13/§4.14/§4.22 反复记录的同构陷阱
——**同一业务判据在 2~3 处各自实现**：

P0-1 ``preflight_engine`` 三处各自实现「本章是否有正文」
    - ``check_deliverability`` 用 ``has_children``（正确）
    - ``preflight_stats`` 把「正文为空」当「未生成」（把父节点算成未生成）
    - ``check_completeness`` 只看标题命中章节自身 content（**误报 3 block + 3 high**）
    统一到 ``build_section_tree_index`` 的「有效正文」单一事实源。

P1-1 ``consistency_scanner._PERSON_RE`` 把岗位后的**谓语**当成人名
    → 生产 CON-SCAN-4/5/6 三条 medium 冲突全部为误报。

护栏要点：判据必须指向**真正下发给模型 / 真正产出结论的那份数据**
（effective content / ``program_prescan`` 候选），而不是碰巧含关键字的一切字符串
（AGENTS.md §5.14 教训）。
"""
from __future__ import annotations

import pytest

from app.services.preflight_engine import (
    PreflightContext,
    build_section_tree_index,
    check_completeness,
    check_deliverability,
    preflight_stats,
)
from app.services.consistency_scanner import (
    _looks_like_person_name,
    _PERSON_RE,
    program_prescan,
)

BODY = "本节依据现行标准与设计文件编制，明确施工工艺参数、质量控制要点与安全技术措施。" * 10


def _mk(sid, title, parent="", content="", wc=0):
    return {"id": sid, "title": title, "parent_id": parent,
            "content": content, "word_count": wc}


def _production_shape():
    """复刻生产目录形态：6 个 L1 法定章节为父节点，正文全部落在二级子节。

    对应 ``preflight_runs`` scheme=d3c1a897… 的 section_count=63 / leaf_count=36 /
    generated_count=36 —— **叶子 100% 生成成功**。
    """
    sections = []
    tree = {
        "一、工程概况": ["1.1 项目概况", "1.2 周边环境"],
        "二、编制依据": ["2.1 法律法规", "2.2 标准规范"],
        "三、施工计划": ["3.1 施工进度", "3.2 材料计划"],
        "四、施工工艺技术": ["4.1 工艺流程", "4.2 操作要求"],
        "五、安全保证措施": ["5.1 组织措施", "5.2 技术措施"],
        "六、计算书及相关图纸": ["6.1 受力计算", "6.2 附图"],
    }
    n = 0
    for i, (parent, kids) in enumerate(tree.items(), 1):
        pid = f"P{i}"
        sections.append(_mk(pid, parent))          # 父节点：正文为空（结构必然）
        for kid in kids:
            n += 1
            sections.append(_mk(f"L{n}", kid, pid, BODY, 600))
    return sections, n


# ---------------------------------------------------------------------------
# P0-1 有效正文（单一事实源）
# ---------------------------------------------------------------------------
class TestEffectiveContentSingleSource:
    def test_parent_effective_content_includes_descendants(self):
        sections, n = _production_shape()
        _, eff = build_section_tree_index(sections)
        assert len(eff) == len(sections)
        for i in range(1, 7):
            assert BODY in eff[f"P{i}"]
        assert n == 12

    def test_own_content_also_counted(self):
        sections = [_mk("A", "父", "", BODY, 600), _mk("B", "子", "A", BODY, 600)]
        _, eff = build_section_tree_index(sections)
        # 自身 + 子孙 都要在
        assert eff["A"].count(BODY) == 2
        assert eff["B"] == BODY

    def test_three_level_chain_rolls_up(self):
        """三层：L1 <- L2 <- L3，有效正文须逐级上卷。"""
        sections = [
            _mk("L1", "一层"),
            _mk("L2", "二层", "L1", "AAA"),
            _mk("L3", "三层", "L2", "BBB"),
        ]
        _, eff = build_section_tree_index(sections)
        assert "AAA" in eff["L1"] and "BBB" in eff["L1"]
        assert "BBB" in eff["L2"]

    def test_parent_cycle_does_not_hang(self):
        """脏数据成环必须 fail-soft（预检不可抛异常）。"""
        sections = [_mk("A", "甲", "B"), _mk("B", "乙", "A")]
        parent_ids, eff = build_section_tree_index(sections)
        assert parent_ids == {"A", "B"}
        assert isinstance(eff.get("A"), str)

    def test_self_cycle_does_not_recurse_forever(self):
        sections = [_mk("A", "甲", "A", "内容")]
        _, eff = build_section_tree_index(sections)
        assert eff["A"] == "内容"

    def test_missing_parent_is_tolerated(self):
        """悬挂 parent_id：不得抛异常，子节点内容仍应可取。"""
        sections = [_mk("A", "甲", "GHOST", "AAA")]


# ---------------------------------------------------------------------------
# P0-1 CMP-* 不得对「父章节正文为空」报 block
# ---------------------------------------------------------------------------
class TestCmpNoFalseBlockOnParentNodes:
    def test_no_empty_body_finding_when_children_filled(self):
        sections, _ = _production_shape()
        ctx = PreflightContext(scheme_id="s", sections=sections)
        fs = check_completeness(ctx)
        empty_hits = [f for f in fs if "正文为空" in (f.get("detail") or "")]
        assert empty_hits == [], f"父章节正文由子节承载，不应报『正文为空』：{empty_hits}"

    def test_generation_success_not_reported_as_missing(self):
        """反向断言：叶子全部有正文时，CMP-01/02/03/04/05 一律不得报。"""
        sections, _ = _production_shape()
        ctx = PreflightContext(scheme_id="s", sections=sections)
        reported = {f["rule_id"] for f in check_completeness(ctx)}
        assert not ({"CMP-01", "CMP-02", "CMP-03", "CMP-04", "CMP-05"} & reported)

    def test_genuinely_empty_statutory_chapter_still_reported(self):
        """反向断言（防「改宽松了」）：真·空的法定章节仍必须报。"""
        sections = [_mk("P1", "五、安全保证措施"), _mk("L1", "5.1 组织措施", "P1")]
        ctx = PreflightContext(scheme_id="s", sections=sections)
        fs = check_completeness(ctx)
        assert any(f["rule_id"] == "CMP-05" and "正文为空" in f["detail"] for f in fs)

    def test_really_absent_chapter_still_reported(self):
        """反向断言：章节根本不存在，仍报「未找到」。"""
        ctx = PreflightContext(scheme_id="s",
                               sections=[_mk("L1", "1.1 概述", "", BODY, 600)])
        fs = check_completeness(ctx)
        assert any("未找到" in f["detail"] for f in fs)

    def test_same_title_multiple_sections_still_aggregated(self):
        """保留 2026-09-22 的同名章节聚合语义（不得被本次修复破坏）。"""
        sections = [
            _mk("A", "安全保证措施", "", "", 0),
            _mk("B", "安全保证措施", "", BODY, 600),
        ]


# ---------------------------------------------------------------------------
# P0-1 preflight_stats 口径
# ---------------------------------------------------------------------------
class TestPreflightStatsDenominator:
    def test_parent_nodes_not_counted_as_ungenerated(self):
        sections, n = _production_shape()
        ctx = PreflightContext(scheme_id="s", word_budget=30000, sections=sections)
        st = preflight_stats(ctx)
        assert st["leaf_count"] == n == 12
        assert st["generated_count"] == len(sections)
        assert st["empty_ratio"] == 0.0, "叶子 100% 有正文，empty_ratio 必须为 0"

    def test_matches_production_scale_expectation(self):
        """63 章节 / 36 叶子 / 36 已生成 -> empty_ratio 不得为 42.9%。"""
        sections = []
        for i in range(27):
            sections.append(_mk(f"P{i}", f"父{i}"))
        for i in range(36):
            sections.append(_mk(f"L{i}", f"叶{i}", f"P{i % 27}", BODY, 1200))
        ctx = PreflightContext(scheme_id="s", sections=sections)
        st = preflight_stats(ctx)
        assert st["section_count"] == 63
        assert st["leaf_count"] == 36
        assert st["empty_ratio"] == 0.0

    def test_real_empty_leaf_still_counted(self):
        sections = [
            _mk("P", "父"), _mk("L1", "叶1", "P", BODY, 600), _mk("L2", "叶2", "P", "", 0),
        ]
        st = preflight_stats(PreflightContext(scheme_id="s", sections=sections))
        assert st["generated_count"] == 2
        assert st["empty_ratio"] == 50.0

    def test_emptiness_criterion_matches_deliverability(self):
        """两处「空章节」判据必须同源（单一事实源护栏）。"""
        sections, _ = _production_shape()
        ctx = PreflightContext(scheme_id="s", sections=sections)
        st = preflight_stats(ctx)
        dlv_empty = [
            f for f in check_deliverability(ctx) if "空章节" in (f.get("title") or "")
        ]
        assert st["empty_ratio"] == 0.0 and not dlv_empty


# ---------------------------------------------------------------------------
# P1-1 人名判定（谓语不得当人名）
# ---------------------------------------------------------------------------
class TestPersonNamePredicate:
    @pytest.mark.parametrize("phrase", [
        "组织各专", "审核后归", "批准后", "签发", "重排对应", "接到报告",
        "或专职安", "会同安全", "组织验收", "批准",
    ])
    def test_predicate_not_person_name(self, phrase):
        assert _looks_like_person_name(phrase) is False

    @pytest.mark.parametrize("name", ["张三", "李四", "王五", "赵六", "刘强"])
    def test_real_name_still_detected(self, name):
        assert _looks_like_person_name(name) is True

    def test_empty_is_not_name(self):
        assert _looks_like_person_name("") is False

    def test_regex_still_captures_real_names(self):
        """反向断言：不得因加拦截而丢失真实人名捕获。"""
        for text, expect in [
            ("项目经理：张三", "张三"),
            ("技术负责人为李四", "李四"),
            ("项目技术负责人 王五", "王五"),
        ]:
            m = _PERSON_RE.search(text)
            assert m, text
            assert _looks_like_person_name(m.group(2)), text
            assert m.group(2) == expect


class TestProgramPrescanNoPredicateConflicts:
    def _sections(self):
        return [
            {"id": "s1", "title": "图纸会审", "content": "技术负责人组织各专业进行图纸会审。"},
            {"id": "s2", "title": "动火审批", "content": "技术负责人审核后归档。"},
            {"id": "s3", "title": "动火批准", "content": "技术负责人批准后方可实施。"},
            {"id": "s4", "title": "签发", "content": "项目技术负责人签发。"},
        ]

    def test_no_numeric_conflict_from_predicates(self):
        """生产 CON-SCAN-4/5/6 的复现：谓语曾产生 2 条 medium 冲突。"""
        cands = program_prescan(self._sections())
        topics = {c["topic"] for c in cands}
        assert not any("负责人" in t for t in topics), topics

    def test_real_name_conflict_still_reported(self):
        """反向断言：真实跨章节人名矛盾仍必须报。"""
        sections = [
            {"id": "s1", "title": "A", "content": "项目经理：张三负责全场。"},
            {"id": "s2", "title": "B", "content": "项目经理：李四负责全场。"},
        ]
        cands = program_prescan(sections)
        assert any(c["topic"] == "项目经理" for c in cands)

    def test_role_aliases_merged(self):
        """「技术负责人」与「项目技术负责人」是同一岗位，不得拆成两桶。"""
        sections = [
            {"id": "s1", "title": "A", "content": "技术负责人：张三负责。"},
            {"id": "s2", "title": "B", "content": "项目技术负责人：张三负责。"},
        ]
        cands = program_prescan(sections)
        topics = {c["topic"] for c in cands}
        # 归一后同一岗位同一人名 -> 不应产生冲突
        assert "技术负责人" not in topics
        assert "项目技术负责人" not in topics


# ---------------------------------------------------------------------------
# 静态护栏：判据不得再分叉
# ---------------------------------------------------------------------------
class TestNoCriteriaFork:
    def test_completeness_uses_tree_index(self):
        import inspect
        from app.services import preflight_engine as pe
        src = inspect.getsource(pe.check_completeness)
        assert "build_section_tree_index" in src

    def test_stats_uses_tree_index(self):
        import inspect
        from app.services import preflight_engine as pe
        assert "build_section_tree_index" in inspect.getsource(pe.preflight_stats)

    def test_person_scan_uses_name_guard(self):
        import inspect
        from app.services import consistency_scanner as cs
        assert "_looks_like_person_name" in inspect.getsource(cs.program_prescan)

    def test_tree_index_exported(self):
        import app.services.preflight_engine as pe
        assert "build_section_tree_index" in pe.__all__

        """两处「空章节」判据必须同源（单一事实源护栏）。"""
        sections, _ = _production_shape()
        ctx = PreflightContext(scheme_id="s", sections=sections)
        st = preflight_stats(ctx)
        dlv_empty = [
            f for f in check_deliverability(ctx) if "空章节" in (f.get("title") or "")
        ]
        assert st["empty_ratio"] == 0.0 and not dlv_empty
