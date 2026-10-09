# -*- coding: utf-8 -*-
"""R49 · 陈旧 AI 结论排除 + 截断正文收口回归护栏（2026-10-07）

两类生产库实证缺陷：

P0 陈旧 AI 结论计入评分
    生产方案 d3c1a897… 的 10-01 AI 合规/一致性审计结论，在正文已多次修改的
    10-07 总检里仍被计入分数（CON-04「与项目关键事实一致」的 detail 指向
    「抹灰底层及接茬验收」等已不存在的章节；SAF-02/05/07 声称正文缺乏的
    专项安全措施实际存在）。总检聚合取「最近一批」AI 结果却**不比对其对应
    的正文版本** → 用户据旧清单整改不存在的缺陷。
    修法：AI 结论行落正文指纹（compliance_check / consistency_audit 新增
    content_fingerprint 列），聚合时与当前正文指纹比对，不一致即跳过并记入
    stale_ai_sources；历史行空指纹 fail-open（与 _run_is_stale 同约定）。

P1 截断正文被静默接受
    AI 返回非空正文但 finish_reason=length（被 max_tokens 截断）时，旧实现
    直接返回、零日志 —— 生产方案 5 个章节句尾停在半句。现记 WARNING 让截断
    可观测，并按 ai_reasoning_max_tokens 上限翻倍 max_tokens 重试一次
    （严格一次，仍截断/失败时返回已获取的较长正文）。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

import app.db as _appdb
import app.services.ai.provider_factory as pf
import pytest
from app.config import settings
from app.db import close_db, get_conn, init_db
from app.routers.compliance import (
    _ai_row_is_stale,
    _content_fingerprint,
    _readiness_overview_compute,
)


# ===========================================================================
# 一、_ai_row_is_stale 纯函数判据
# ===========================================================================
class TestAiRowIsStale:
    def test_matching_fingerprint_is_fresh(self):
        assert _ai_row_is_stale({"content_fingerprint": "abc"}, "abc") is False

    def test_differing_fingerprint_is_stale(self):
        assert _ai_row_is_stale({"content_fingerprint": "old"}, "new") is True

    def test_row_without_fingerprint_fail_open(self):
        """历史行（无指纹）不得判过期：无法判定就不打扰用户（与 _run_is_stale 同约定）。"""
        assert _ai_row_is_stale({}, "new") is False
        assert _ai_row_is_stale({"content_fingerprint": ""}, "new") is False

    def test_current_fingerprint_unavailable_fail_open(self):
        """当前指纹算不出来（空串）同样 fail-open，避免把整库旧行全判成过期。"""
        assert _ai_row_is_stale({"content_fingerprint": "old"}, "") is False


# ===========================================================================
# 二、总检聚合：过期 AI 来源整体排除
# ===========================================================================
@pytest.fixture
async def ov_db(tmp_path):
    _appdb.DB_PATH = tmp_path / "r49-stale.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget)"
        " VALUES(?,?,?,?,0)", (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "", "一、工程概况", "", 1,
         "generated", 200, 0, "本工程概况正文" * 50, "", 0))
    await db.commit()
    yield db, sid
    await close_db()


async def _insert_ai_row(db, sid, *, fingerprint="", batch_id=None):
    await db.execute(
        "INSERT INTO compliance_check (id, scheme_id, project_id, check_type,"
        " rule_id, item, severity, result, batch_id, created_at,"
        " content_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "compliance", "SAF-02", "专项安全技术措施",
         "high", json.dumps({"hit": False, "rule_id": "SAF-02",
                            "item": "专项安全技术措施",
                            "severity": "high",
                            "evidence": "旧正文证据（已不存在）"},
                            ensure_ascii=False),
         batch_id or uuid.uuid4().hex, datetime.now().isoformat(timespec="seconds"),
         fingerprint))
    await db.commit()


async def _insert_consistency_audit(db, sid, *, fingerprint=""):
    await db.execute(
        "INSERT INTO consistency_audit (id, project_id, scheme_id, score,"
        " content_fingerprint, issues, created_at) VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "", sid, 90.0, fingerprint,
         json.dumps([{"dimension": "与项目关键事实一致", "severity": "medium",
                      "fact": "旧事实", "content_quote": "旧引文",
                      "section_title": "抹灰底层及接茬验收"}], ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")))
    await db.commit()


class TestOverviewSkipsStaleAiRows:
    async def test_stale_compliance_batch_excluded_and_reported(self, ov_db):
        db, sid = ov_db
        await _insert_ai_row(db, sid, fingerprint="stale-old-fingerprint")
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "compliance" not in payload["sources"], payload["sources"]
        assert payload["stale_ai_sources"] == ["compliance"]
        assert not [f for f in payload["findings"]
                    if f["rule_id"] == "SAF-02"], "过期结论不得计入评分"

    async def test_fresh_batch_included(self, ov_db):
        db, sid = ov_db
        fp = await _content_fingerprint(db, sid)
        await _insert_ai_row(db, sid, fingerprint=fp)
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "compliance" in payload["sources"]
        assert payload["stale_ai_sources"] == []
        assert [f for f in payload["findings"] if f["rule_id"] == "SAF-02"]

    async def test_historical_row_without_fingerprint_fail_open(self, ov_db):
        db, sid = ov_db
        await _insert_ai_row(db, sid, fingerprint="")
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "compliance" in payload["sources"]
        assert payload["stale_ai_sources"] == []

    async def test_mixed_rows_in_one_batch_only_fresh_counted(self, ov_db):
        db, sid = ov_db
        fp = await _content_fingerprint(db, sid)
        batch = uuid.uuid4().hex
        await _insert_ai_row(db, sid, fingerprint="stale-old", batch_id=batch)
        await _insert_ai_row(db, sid, fingerprint=fp, batch_id=batch)
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "compliance" in payload["sources"], "同一批里仍有新鲜行，来源不能整体丢弃"
        assert payload["stale_ai_sources"] == ["compliance"]
        assert [f for f in payload["findings"] if f["rule_id"] == "SAF-02"]

    async def test_stale_consistency_audit_excluded(self, ov_db):
        db, sid = ov_db
        await _insert_consistency_audit(db, sid, fingerprint="stale-old")
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "consistency" not in payload["sources"]
        assert "consistency" in payload["stale_ai_sources"]
        assert not [f for f in payload["findings"]
                    if f["rule_id"].startswith("CON-04")]

    async def test_fresh_consistency_audit_included(self, ov_db):
        db, sid = ov_db
        fp = await _content_fingerprint(db, sid)
        await _insert_consistency_audit(db, sid, fingerprint=fp)
        payload = await _readiness_overview_compute(db, sid, persist=False)
        assert "consistency" in payload["sources"]
        assert [f for f in payload["findings"] if f["rule_id"].startswith("CON-04")]


# ===========================================================================
# 三、截断正文：可观测 + 翻倍重试一次
# ===========================================================================
class _FakeProvider:
    temperature = 0.7
    last_finish_reason = ""

    def __init__(self, *, behavior, built_mt=0):
        self.behavior = behavior
        self.built_mt = built_mt
        self.chat_calls = 0
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5,
                           "cached_tokens": 0}
        self.name = "fake"
        self.model = "m"

    async def chat(self, messages, **kw):
        self.chat_calls += 1
        self.last_finish_reason = "stop"
        if self.behavior == "truncated":
            self.last_finish_reason = "length"
            return "半截正文内容"
        if self.behavior == "full":
            return "完整正文内容（未被截断）"
        if self.behavior == "trunc-then-fail":
            if self.built_mt < 4096:
                self.last_finish_reason = "length"
                return "半截正文内容"
            raise RuntimeError("HTTP 500: boom")
        raise RuntimeError("boom")


@pytest.fixture
def isolated(monkeypatch):
    """隔离 chat_with_fallback 外部依赖（不读 DB / 不写审计 / 清全局态）。"""

    async def _noop_flush():
        return None

    monkeypatch.setattr(pf, "_flush_audit_buffer", _noop_flush)
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()
    pf.reset_quota_cooldown()
    yield monkeypatch
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()
    pf.reset_quota_cooldown()


def _wire_single(monkeypatch, provider_factory_fn, *, max_tokens=4096):
    built = []

    async def _cfg():
        return {"provider_name": "p1", "api_key": "k", "base_url": "https://x",
                "model": "m", "max_tokens": max_tokens, "temperature": 0.7,
                "timeout": 60}

    async def _chain():
        return []

    def _factory(pname, *a, **kw):
        prov = provider_factory_fn(kw.get("max_tokens") or max_tokens)
        built.append(prov.built_mt)
        return prov

    monkeypatch.setattr(pf, "_load_active_config", _cfg)
    monkeypatch.setattr(pf, "_fallback_chain", _chain)
    monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")
    monkeypatch.setattr(pf, "_build_provider", _factory)
    return built


class TestPartialTruncationRetry:
    async def test_truncated_retries_with_doubled_max_tokens(self, isolated,
                                                             monkeypatch, caplog):
        monkeypatch.setattr(settings, "ai_retry_on_partial_truncation", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        def _factory(mt):
            return _FakeProvider(behavior="truncated" if mt < 4096 else "full",
                                 built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        with caplog.at_level(logging.WARNING,
                             logger="app.services.ai.provider_factory"):
            out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "完整正文内容（未被截断）"
        assert built == [2048, 4096], "必须先以原 max_tokens 尝试，截断后翻倍重试一次"
        assert any("正文疑似被 max_tokens 截断" in r.message for r in caplog.records), \
            "截断必须可观测（WARNING）"

    async def test_disabled_is_legacy(self, isolated, monkeypatch):
        """ai_retry_on_partial_truncation=False → 不重试（旧行为，仅告警）。"""
        monkeypatch.setattr(settings, "ai_retry_on_partial_truncation", False)

        def _factory(mt):
            return _FakeProvider(behavior="truncated", built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "半截正文内容"
        assert built == [2048]

    async def test_not_truncated_never_retries(self, isolated, monkeypatch):
        monkeypatch.setattr(settings, "ai_retry_on_partial_truncation", True)

        def _factory(mt):
            return _FakeProvider(behavior="full", built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "完整正文内容（未被截断）"
        assert built == [2048], "正常结束不得触发重试"

    async def test_only_one_retry_even_if_still_truncated(self, isolated,
                                                          monkeypatch):
        """达到上限后不再重试（不做二重放大），返回已获取的较长正文。"""
        monkeypatch.setattr(settings, "ai_retry_on_partial_truncation", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        def _factory(mt):
            return _FakeProvider(behavior="truncated", built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "半截正文内容"
        assert built == [2048, 4096], "翻倍重试一次后必须停止"

    async def test_retry_failure_returns_best_partial(self, isolated,
                                                      monkeypatch):
        """截断重试后失败：返回已拿到的半截正文，而不是丢弃它去降级下一候选。"""
        monkeypatch.setattr(settings, "ai_retry_on_partial_truncation", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        def _factory(mt):
            return _FakeProvider(behavior="trunc-then-fail", built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "半截正文内容"
        assert built == [2048, 4096]
