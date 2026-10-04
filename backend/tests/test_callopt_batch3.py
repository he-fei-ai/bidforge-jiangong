# -*- coding: utf-8 -*-
"""调用次数优化 · 第 3 批回归锁（2026-09-22 · O8 修复重试按严重度分级）

基线实测（test_baseline_calls.py 场景 A）：省重试（retry_invalid=False）会让
3/12 的高危冲突留在交付文档里 —— 错误参数/标号直接进文档，质量代价远大于
省下的调用。O8：组内含 high 级冲突时校验不过仍重试一次；medium/low 维持不重试。
"""
import app.services.repair_agent as ra
import pytest


def _conflict(cid, sid="s0", severity="high"):
    return {
        "id": cid, "conflict_type": "numeric", "severity": severity,
        "topic": "檐口高度", "authoritative_value": "42.5m",
        "authoritative_source": "全局事实", "status": "pending",
        "occurrences": [{"section_id": sid, "section_title": f"章节{sid}",
                         "value": "18.5m", "text": "檐口高度 18.5m"}],
    }


async def _seed(db, n=1):
    for i in range(n):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
            " VALUES (?,?,?,?,2,?)",
            (f"s{i}", "sch1", f"章节s{i}", "原文：檐口高度 18.5m。", i))
    await db.commit()


def _patch(monkeypatch, *, calls, fail_times=99):
    """fake repair_section：前 fail_times 次返回仍含错误值的正文。"""
    state = {"n": 0}

    async def fake_repair(**kw):
        state["n"] += 1
        calls.append(kw.get("section_id"))
        if state["n"] <= fail_times:
            return "修复后正文：檐口高度 18.5m。"
        return "修复后正文：檐口高度 42.5m。"

    monkeypatch.setattr(ra, "repair_section", fake_repair)


def _reject_185(**kw):
    """校验替身：正文仍含错误值 18.5m → 不通过（签名须吃关键字参数）。"""
    return (False, ["仍含错误值"]) if "18.5m" in str(kw.get("after", "")) else (True, [])


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID", False)
    monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID_BY_SEVERITY", True)
    monkeypatch.setattr(ra, "validate_repair", _reject_185)


class TestGroupHasHighSeverity:
    def test_high(self):
        assert ra._group_has_high_severity(
            {"conflicts": [{"severity": "high"}]}) is True

    def test_mixed_contains_high(self):
        assert ra._group_has_high_severity(
            {"conflicts": [{"severity": "low"}, {"severity": "HIGH"}]}) is True

    def test_medium_low_empty(self):
        for confs in ([{"severity": "medium"}], [{"severity": "low"}], [], None):
            assert ra._group_has_high_severity({"conflicts": confs}) is False


class TestSeverityGradedRetry:
    async def test_high_conflict_retried_once(self, db_conn, monkeypatch):
        """high 冲突校验不过 → 重试 1 次（2 次调用），第 2 次修复成功。"""
        await _seed(db_conn, 1)
        calls = []
        _patch(monkeypatch, calls=calls, fail_times=1)
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                                  conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 2, "high 级冲突校验不过必须重试一次"
        assert res["repaired"] == 1 and res["failed"] == 0

    async def test_medium_conflict_not_retried(self, db_conn, monkeypatch):
        """medium/low 冲突维持旧行为：校验不过不重试（1 次调用）。"""
        await _seed(db_conn, 1)
        calls = []
        _patch(monkeypatch, calls=calls, fail_times=99)
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                                  conflicts=[_conflict("C001", severity="medium")],
                                  mode="auto", severity_threshold="low")
        assert len(calls) == 1, "medium/low 冲突不得重试"
        assert res["failed"] == 1

    async def test_gate_off_is_legacy(self, db_conn, monkeypatch):
        """分级开关关闭 → high 也不重试（回到省调用行为）。"""
        monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID_BY_SEVERITY", False)
        await _seed(db_conn, 1)
        calls = []
        _patch(monkeypatch, calls=calls, fail_times=99)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 1

    async def test_retry_on_invalid_true_is_full_legacy(self, db_conn, monkeypatch):
        """consistency_repair_retry_on_invalid=True → 所有严重度都重试（旧行为）。"""
        monkeypatch.setattr(ra, "REPAIR_RETRY_ON_INVALID", True)
        await _seed(db_conn, 1)
        calls = []
        _patch(monkeypatch, calls=calls, fail_times=99)
        await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                            conflicts=[_conflict("C001", severity="medium")],
                            mode="auto", severity_threshold="low")
        assert len(calls) == 2

    async def test_ai_exception_still_retries(self, db_conn, monkeypatch):
        """AI 调用本身异常（超时/网络）仍重试一次 —— 不受分级开关影响。"""
        await _seed(db_conn, 1)
        calls = []
        state = {"n": 0}

        async def flaky(**kw):
            state["n"] += 1
            calls.append(kw.get("section_id"))
            if state["n"] == 1:
                raise RuntimeError("ReadTimeout")
            return "修复后正文：檐口高度 42.5m。"

        monkeypatch.setattr(ra, "repair_section", flaky)
        res = await ra.run_repair(db_conn, scheme_id="sch1", scan_id="scan1",
                                  conflicts=[_conflict("C001")], mode="auto")
        assert len(calls) == 2 and res["repaired"] == 1

    async def test_mixed_group_uses_max_severity(self, db_conn, monkeypatch):
        """同章混合严重度：组内含 high 即允许重试。"""
        await _seed(db_conn, 1)
        calls = []
        _patch(monkeypatch, calls=calls, fail_times=1)
        res = await ra.run_repair(
            db_conn, scheme_id="sch1", scan_id="scan1",
            conflicts=[_conflict("C001", severity="high"),
                       _conflict("C002", severity="low")],
            mode="auto", severity_threshold="low")
        assert len(calls) == 2
        assert res["repaired"] == 2
