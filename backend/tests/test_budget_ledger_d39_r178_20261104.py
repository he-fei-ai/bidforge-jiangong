"""D-39 账实相符（R178 · 2026-11-04）：硬切可见化与三态账本回归。

覆盖两处「改了但账面看不见」的截断点：

1. ``knowledge.build_knowledge_text``（单条 500 / 总量 3000 硬切）
   - ``stats_out`` 精确统计：原始/保留/丢弃字符数与真实切片逐字对齐；
   - 无裁剪（正常路径）零记录；有裁剪并入既有 ``context_degraded`` 事件
     （``field=knowledge_text`` · ``reason=budget_truncated``），不新开事件名。
2. ``facts_builder._render_facts_text``（单条 per_fact=300 硬切）
   - 被截断行进 ``truncated_out``，原始/保留字符数精确；
   - ``fact_injection_audit`` 三态账本：完整注入 + 截断注入 + 丢弃 == 账本行数；
   - ``audit_entries`` 用 ``status`` 区分 truncated / dropped；
   - 终态 ``fact_injection_summary`` / ``summarize_fact_mapping`` 并入截断计数。
"""

import json

import app.routers.sse_handlers as sh
from app.routers._sse_content_payloads import build_content_checkpoint
from app.routers.knowledge import build_knowledge_text
from app.services import facts_builder as fb
from app.services.content_utils import summarize_fact_mapping

# ---------------------------------------------------------------------------
# 1. 知识库硬切：精确统计 + context_degraded 复用 + 正常路径零事件
# ---------------------------------------------------------------------------


def _empty_kn_stats() -> dict:
    return {
        "items": [],
        "truncated_items": 0,
        "dropped_items": 0,
        "original_chars": 0,
        "kept_chars": 0,
        "dropped_chars": 0,
    }


def test_knowledge_text_no_truncation_stats_all_zero():
    """正常路径：无单条超限、无总量丢弃 → 统计全 0（调用方据此零事件）。"""
    content = "X" * 200
    stats: dict = {}
    text = build_knowledge_text([{"name": "制度A", "content": content}], stats_out=stats)

    assert text == f"【制度A】\n{content}"
    assert stats == _empty_kn_stats()


def test_knowledge_text_per_item_truncation_exact():
    """单条超 500：保留字符数 == 实际进入文本的切片长度，不做估算。"""
    content = "X" * 800
    stats: dict = {}
    text = build_knowledge_text([{"name": "制度A", "content": content}], per_item=500, stats_out=stats)

    kept_in_text = text.split("\n", 1)[1]
    assert kept_in_text == content[:500]
    assert stats["truncated_items"] == 1
    assert stats["dropped_items"] == 0
    assert stats["original_chars"] == 800
    assert stats["kept_chars"] == len(kept_in_text) == 500
    assert stats["dropped_chars"] == 300
    assert stats["items"] == [
        {
            "name": "制度A",
            "state": "truncated",
            "original_chars": 800,
            "kept_chars": 500,
            "dropped_chars": 300,
        }
    ]


def test_knowledge_text_total_budget_drop_exact():
    """总量 3000 触顶：后续条目整条不入文本，逐条记 dropped（kept=0）。"""
    rows = [
        {"name": "A", "content": "甲" * 400},
        {"name": "B", "content": "乙" * 400},
        {"name": "C", "content": "丙" * 400},
    ]
    stats: dict = {}
    text = build_knowledge_text(rows, max_total=500, stats_out=stats)

    # 第一块 len("【A】\n") + 400 = 404 放得下；第二块 404 + 404 > 500 → 丢弃
    assert "【A】" in text
    assert "【B】" not in text and "【C】" not in text
    assert stats["truncated_items"] == 0
    assert stats["dropped_items"] == 2
    assert stats["original_chars"] == 800
    assert stats["kept_chars"] == 0
    assert stats["dropped_chars"] == 800
    assert [i["state"] for i in stats["items"]] == ["dropped", "dropped"]
    assert [i["name"] for i in stats["items"]] == ["B", "C"]


def test_record_knowledge_budget_zero_when_no_cut():
    """无裁剪统计（affected=0 / stats 缺省）→ 零记录，正常路径零噪音。"""
    out: list = []
    sh._record_knowledge_budget(out, None, stage="content.generate.knowledge_text")
    sh._record_knowledge_budget(out, _empty_kn_stats(), stage="content.generate.knowledge_text")
    assert out == []


def test_record_knowledge_budget_reuses_context_degraded():
    """复用既有 context_degraded 簿记：字段/原因枚举/逐条明细齐全。"""
    content = "X" * 800
    stats: dict = {}
    build_knowledge_text([{"name": "制度A", "content": content}], per_item=500, stats_out=stats)

    out: list = []
    sh._record_knowledge_budget(out, stats, stage="content.generate.knowledge_text")

    assert len(out) == 1
    rec = out[0]
    assert rec["field"] == "knowledge_text"
    assert rec["reason"] == sh.CONTEXT_DEGRADED_BUDGET == "budget_truncated"
    assert rec["segment_id"] == "knowledge_text"
    assert rec["original_chars"] == 800
    assert rec["kept_chars"] == 500
    assert rec["dropped_chars"] == 300
    assert rec["cut_items"] == 1
    assert rec["truncated_items"] == 1
    assert rec["dropped_items"] == 0
    assert rec["items"] == [
        {
            "name": "制度A",
            "state": "truncated",
            "original_chars": 800,
            "kept_chars": 500,
            "dropped_chars": 300,
        }
    ]

    events = sh._context_degraded_events("t-kn", out)
    assert len(events) == 1
    payload = json.loads(events[0].removeprefix("data: ").strip())
    assert payload["event"] == "context_degraded"
    assert payload["field"] == "knowledge_text"
    assert payload["reason"] == "budget_truncated"
    assert payload["original_chars"] == 800
    assert payload["kept_chars"] == 500
    assert payload["dropped_chars"] == 300
    assert len(payload["items"]) == 1


def test_context_degraded_events_empty_input_is_silent():
    """空簿记 → 零事件（正常路径不产生任何 context_degraded）。"""
    assert sh._context_degraded_events("t", []) == []
    assert sh._context_degraded_events("t", None) == []


# ---------------------------------------------------------------------------
# 2. 事实单条硬切：截断明细 + 三态账本
# ---------------------------------------------------------------------------


def _fact_row(
    title: str,
    content: str,
    group: str = "主体结构",
    conf: float = 0.9,
    chapter: str = "technique",
    value_kind: str = "text",
) -> tuple:
    return (group, title, content, conf, chapter, value_kind)


def test_render_facts_text_truncation_detail_exact():
    """单条 400 字被 per_fact=300 截断：明细给原始/保留/丢弃精确值。"""
    content = "X" * 400
    truncated: list = []
    text = fb._render_facts_text([_fact_row("事实A", content)], per_fact=300, truncated_out=truncated)

    assert text.count("X") == 300  # 文本里真实保留的正是 300 字
    assert truncated == [
        {
            "title": "事实A",
            "group": "主体结构",
            "original_chars": 400,
            "kept_chars": 300,
            "dropped_chars": 100,
        }
    ]


def test_render_facts_text_requirement_prefix_not_counted_as_content():
    """requirement 口径行：前缀属于格式，不算「保留的事实正文」。"""
    content = "X" * 400
    truncated: list = []
    fb._render_facts_text(
        [_fact_row("要求A", content, value_kind="requirement")], per_fact=300, truncated_out=truncated
    )

    assert truncated[0]["original_chars"] == 400
    assert truncated[0]["kept_chars"] == 300
    assert truncated[0]["dropped_chars"] == 100


def test_render_facts_text_low_conf_prefix_does_not_shrink_kept():
    """低置信度前缀不侵占事实正文：kept 仍等于真实切片长度。"""
    content = "X" * 400
    truncated: list = []
    text = fb._render_facts_text([_fact_row("事实A", content, conf=0.3)], per_fact=300, truncated_out=truncated)

    assert text.count("X") == 300
    assert truncated[0]["kept_chars"] == 300


def test_fact_audit_three_state_identity():
    """三态账本：完整注入 + 截断注入 + 丢弃 == 账本输入行数。"""
    rows = [
        _fact_row("A", "甲" * 2, group="工程概况"),
        _fact_row("B", "乙" * 300),
        _fact_row("C", "丙" * 5, group="监测"),
        _fact_row("D", "丁" * 5, group="安全"),
        _fact_row("E", "戊" * 5, group="环保"),
    ]
    audit = fb.fact_injection_audit(rows, max_total=48, per_fact=300)

    assert audit["rows_total"] == 5
    assert audit["ledger_rows_total"] == 5
    assert audit["complete_injected_count"] == 1
    assert audit["truncated_count"] == 3
    assert audit["budget_dropped_count"] == 1
    assert (audit["complete_injected_count"] + audit["truncated_count"] + audit["budget_dropped_count"]) == audit[
        "ledger_rows_total"
    ]

    statuses = [e["status"] for e in audit["audit_entries"]]
    assert statuses.count("truncated") == 3
    assert statuses.count("dropped") == 1
    assert "complete" not in statuses


def test_fact_audit_truncated_entries_carry_char_counts():
    """截断行明细带原始/保留/丢弃字符数，且保留数 == 实际渲染切片。"""
    content = "X" * 400
    audit = fb.fact_injection_audit([_fact_row("事实A", content)], max_total=6000, per_fact=300)

    assert audit["truncated_count"] == 1
    assert audit["complete_injected_count"] == 0
    assert audit["ledger_rows_total"] == 1
    item = audit["truncated_facts"][0]
    assert item["original_chars"] == 400
    assert item["kept_chars"] == 300
    assert item["dropped_chars"] == 100
    assert audit["rendered_text"].count("X") == item["kept_chars"]


def test_fact_injection_summary_includes_truncated_sections():
    """终态 fact_injection_summary：截断计数并入摘要（与丢弃分列）。"""
    payload = build_content_checkpoint(
        "completed",
        "完成",
        ckpt={},
        task_id="t",
        total=2,
        done_ids={"a", "b"},
        failed_ids=set(),
        failed_reasons={},
        prog={},
        run_stats={},
        word_tol=None,
        fact_autofix_summary={},
        dispatched_order=[],
        expected_order=[],
        fact_audit_by_leaf={
            "a": {"budget_dropped_count": 2, "truncated_count": 1},
            "b": {"budget_dropped_count": 0, "truncated_count": 2},
        },
        order_warned={},
    )

    summary = payload["fact_injection_summary"]
    assert summary["sections"] == 2
    assert summary["budget_dropped_total"] == 2
    assert summary["budget_dropped_sections"] == 1
    assert summary["truncated_total"] == 3
    assert summary["truncated_sections"] == 2


def test_summarize_fact_mapping_includes_truncated_totals():
    """事实映射摘要同口径：截断总量/章节数加法式并入。"""
    stats = summarize_fact_mapping(
        [{"section_id": "a", "fact_count": 2}, {"section_id": "b", "fact_count": 1}],
        {
            "a": {"budget_dropped_count": 1, "truncated_count": 2},
            "b": {"budget_dropped_count": 0, "truncated_count": 1},
        },
    )

    assert stats["sections"] == 2
    assert stats["fact_refs"] == 3
    assert stats["budget_dropped_total"] == 1
    assert stats["truncated_total"] == 3
    assert stats["truncated_sections"] == 2
