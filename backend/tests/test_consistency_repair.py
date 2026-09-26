"""全文一致性 Agent 修复：纯函数单测（无 AI / 无 DB）。

覆盖扫描合并去重、修复校验（空白归一化）、冲突仲裁规则、修复过滤与分组。
"""
import pytest

from app.services.consistency_scanner import merge_conflicts
from app.services.repair_validator import validate_repair
from app.services.conflict_arbiter import match_global_facts, majority_value
from app.services.repair_agent import filter_conflicts, group_by_section


# ---------------- 合并去重 ----------------
def _occ(section_id, value, text=""):
    return {"section_id": section_id, "section_title": section_id,
            "text": text, "position": 0, "value": value}


def test_merge_dedup_ai_and_prescan_same_discrepancy():
    """AI 命名为「项目总工期」、预扫描命名为「工期」，涉及同一章节+取值 → 合并为 1 处。"""
    ai = [{
        "conflict_type": "numeric", "topic": "项目总工期", "value": "120 日历天",
        "text": "总工期为 120 日历天", "position": 10,
        "section_occurrences": [_occ("S1", "120 日历天", "总工期为 120 日历天")],
        "source": "ai_scan",
    }]
    prescan = [{
        "conflict_type": "numeric", "topic": "工期", "value": "120 日历天",
        "text": "总工期为 120 日历天", "position": 0,
        "section_occurrences": [_occ("S1", "120 日历天", "总工期为 120 日历天")],
        "source": "program_prescan",
    }]
    out = merge_conflicts(ai, prescan)
    assert len(out) == 1
    assert out[0]["topic"] == "项目总工期"  # 取更具体的 AI 主题
    assert len(out[0]["occurrences"]) == 1


def test_merge_keeps_distinct_discrepancies():
    ai = [
        {"conflict_type": "numeric", "topic": "项目总工期", "value": "120 日历天",
         "text": "t", "position": 1,
         "section_occurrences": [_occ("S1", "120 日历天")], "source": "ai_scan"},
        {"conflict_type": "param", "topic": "混凝土强度等级", "value": "C30",
         "text": "t", "position": 2,
         "section_occurrences": [_occ("S2", "C30")], "source": "ai_scan"},
    ]
    out = merge_conflicts(ai, [])
    assert len(out) == 2


def test_merge_safety_topic_is_high():
    ai = [{
        "conflict_type": "numeric", "topic": "混凝土强度等级", "value": "C30",
        "text": "t", "position": 1,
        "section_occurrences": [_occ("S2", "C30")], "source": "ai_scan",
    }]
    out = merge_conflicts(ai, [])
    assert out[0]["severity"] == "high"


# ---------------- 修复校验（空白归一化） ----------------
def test_validate_repair_whitespace_insensitive():
    before = "本工程总工期为 120 日历天，混凝土强度等级为 C30。"
    # 模型输出在数值与单位间保留了空格，权威值无空格
    after = "本工程总工期为 90 日历天，混凝土强度等级为 C35。"
    ok, problems = validate_repair(
        before=before, after=after,
        wrong_values=["120 日历天", "C30"], authoritative_value="90日历天")
    assert ok is True, problems


def test_validate_repair_detects_residual():
    before = "总工期为 120 日历天。"
    after = "总工期为 120 日历天。"  # 错误值仍残留（含空格）
    ok, problems = validate_repair(
        before=before, after=after,
        wrong_values=["120 日历天"], authoritative_value="90日历天")
    assert ok is False
    assert any("冲突取值" in p for p in problems)


def test_validate_repair_empty_after_fails():
    ok, problems = validate_repair(
        before="正文", after="", wrong_values=[], authoritative_value="")
    assert ok is False


# ---------------- 仲裁规则 ----------------
def test_match_global_facts_picks_fact_backed_value():
    conflict = {
        "topic": "项目总工期",
        "occurrences": [{"value": "120 日历天"}, {"value": "90 日历天"}],
    }
    facts = "建设工期安排：本工程建设工期为 90 日历天（详见合同附件）。"
    hit = match_global_facts(conflict, facts)
    assert hit is not None
    assert hit["value"] == "90 日历天"


def test_match_global_facts_no_facts_returns_none():
    conflict = {"topic": "工期", "occurrences": [{"value": "120 日历天"}]}
    assert match_global_facts(conflict, "") is None


def test_majority_value():
    c = {"occurrences": [{"value": "A"}, {"value": "A"}, {"value": "B"}]}
    assert majority_value(c) == ("A", 2)
    # 平局（2:2）无多数 → 返回出现最多者但计数未过半
    c2 = {"occurrences": [{"value": "A"}, {"value": "B"}]}
    val, cnt = majority_value(c2)
    assert cnt == 1


# ---------------- 修复过滤 / 分组 ----------------
def _conflict(cid, auth="", severity="high", status="pending"):
    return {
        "id": cid, "conflict_type": "numeric", "topic": cid,
        "severity": severity,
        "authoritative_value": auth, "authoritative_source": "x",
        "occurrences": [{"section_id": "S1", "section_title": "S1",
                         "value": "wrong", "text": "t"}],
        "status": status,
    }


def test_filter_conflicts_by_severity_and_authority():
    conflicts = [
        _conflict("C1", auth="90", severity="high"),
        _conflict("C2", auth="90", severity="medium"),
        _conflict("C3", auth="", severity="high"),      # 无权威值
        _conflict("C4", auth="90", severity="high", status="accepted"),
    ]
    high = filter_conflicts(conflicts, severity_threshold="high")
    assert {c["id"] for c in high} == {"C1"}
    medium = filter_conflicts(conflicts, severity_threshold="medium")
    assert {c["id"] for c in medium} == {"C1", "C2"}


def test_group_by_section_skips_authoritative_occurrences():
    conflict = {
        "id": "C1", "conflict_type": "numeric", "topic": "t",
        "authoritative_value": "90",
        "occurrences": [
            {"section_id": "S1", "value": "120", "section_title": "S1"},  # 需改
            {"section_id": "S2", "value": "90", "section_title": "S2"},   # 已是权威值
        ],
    }
    groups = group_by_section([conflict])
    assert "S2" not in groups
    assert "S1" in groups
    assert len(groups["S1"]["conflicts"]) == 1
