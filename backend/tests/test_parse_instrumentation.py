"""解析提取模块增强：全链路日志埋点 + 信息调用完整性门控（2026-09-23）。

覆盖：
1. app.utils.log_context：关联 ID 透传与日志前缀过滤器。
2. facts_extractor.resolve_fact_is_resolved：AUTO_RESOLVE_EXTRACTED_FACTS 门控。
"""
from __future__ import annotations

import logging

from app.utils.log_context import (
    new_trace_id, set_context, context_suffix, TraceContextFilter, install_filter,
    clear_context,
)
from app.services.facts_extractor import FactItem, resolve_fact_is_resolved


# ---------------------------------------------------------------------------
# 1. 全链路日志埋点
# ---------------------------------------------------------------------------

def test_trace_id_generation_and_suffix():
    clear_context()
    tid = new_trace_id()
    assert len(tid) == 12
    set_context(project_id="p1", doc_id="d1", scheme_id="s1", task_id="t1")
    suffix = context_suffix()
    assert "trace=" in suffix and "pid=p1" in suffix and "doc=d1" in suffix
    assert "sid=s1" in suffix and "task=t1" in suffix


def test_suffix_empty_when_no_context():
    clear_context()
    assert context_suffix() == ""


def test_trace_context_filter_prefixes_message():
    clear_context()
    rec = logging.LogRecord(
        "mod", logging.INFO, __file__, 1, "原始消息", None, None)
    flt = TraceContextFilter()
    set_context(trace_id="abc123", project_id="pX", doc_id="dX")
    assert flt.filter(rec) is True
    assert rec.msg == "[trace=abc123 pid=pX doc=dX] 原始消息"


def test_trace_context_filter_idempotent():
    clear_context()
    rec = logging.LogRecord(
        "mod", logging.INFO, __file__, 1, "msg", None, None)
    flt = TraceContextFilter()
    set_context(trace_id="abc123", project_id="pY")
    flt.filter(rec)
    before = rec.msg
    flt.filter(rec)  # 第二次不应重复加前缀
    assert rec.msg == before


def test_install_filter_idempotent():
    clear_context()
    root = logging.getLogger("test_install_" + new_trace_id())
    root.setLevel(logging.DEBUG)
    h = logging.Handler()
    root.addHandler(h)
    install_filter(root)
    install_filter(root)  # 第二次不应重复挂载
    assert sum(1 for f in h.filters if isinstance(f, TraceContextFilter)) == 1


# ---------------------------------------------------------------------------
# 2. 信息调用完整性门控（AUTO_RESOLVE_EXTRACTED_FACTS）
# ---------------------------------------------------------------------------

def _item(**kw) -> FactItem:
    return FactItem(name="测试事实", value="100", **kw)


def test_resolve_off_keeps_pending_by_default():
    # 默认（向后兼容）：无论是否模拟/矛盾，均未确认（待审核）。
    assert resolve_fact_is_resolved(_item(is_simulated=False, has_conflict=False), False) == 0
    assert resolve_fact_is_resolved(_item(is_simulated=True, has_conflict=False), False) == 0
    assert resolve_fact_is_resolved(_item(is_simulated=False, has_conflict=True), False) == 0


def test_resolve_on_auto_confirms_clean_facts():
    # 开启后：非模拟、无矛盾 → 自动确认（进入生成链路）。
    assert resolve_fact_is_resolved(_item(is_simulated=False, has_conflict=False), True) == 1


def test_resolve_on_keeps_safety_gate_for_simulated_and_conflict():
    # 安全闸门对高风险项依然生效：模拟值 / 矛盾值 即使开启也保持待审核。
    assert resolve_fact_is_resolved(_item(is_simulated=True, has_conflict=False), True) == 0
    assert resolve_fact_is_resolved(_item(is_simulated=False, has_conflict=True), True) == 0
    assert resolve_fact_is_resolved(_item(is_simulated=True, has_conflict=True), True) == 0
