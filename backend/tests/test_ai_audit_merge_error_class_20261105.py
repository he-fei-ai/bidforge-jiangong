# -*- coding: utf-8 -*-
"""方案 B（2026-11-05）：审计合并 + error_class 分类的回归测试。

覆盖：
1. 错误 → error_class 映射（限流 / 认证 / 超时 / 截断 / 网络 / 5xx /
   响应格式 / 熔断跳过 / unknown）；截断必须先于 invalid_response 命中。
2. 一次逻辑调用（重试 + 降级）在 ai_audit_logs 只落**一条** action='chat'
   主记录，attempt_count 与 attempts_json 保留逐次诊断。
3. 最终失败记录保留最后错误类别 + 逐次失败类别。
4. 未发上游请求的快速失败（全熔断/冷却）也落一条 attempt_count=0 的记录，
   使面板「失败调用」与审计行可对账。
5. 迁移幂等：旧库缺 error_class/attempt_count/attempts_json 时补列，
   重复执行无副作用；历史 NULL 行读侧不报错。
6. 统计按 error_class 聚合（历史 NULL 归 unknown），成功率分母=逻辑调用。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from app.config import settings
from app.routers.ai_config import usage as usage_mod
from app.services.ai import provider_factory as pf


@pytest.fixture(autouse=True)
def _clean_audit_state():
    with pf._audit_lock:
        pf._audit_buffer.clear()
        pf._audit_overflow.clear()
    pf._audit_overflow_evicted = 0
    pf._audit_last_flush["ts"] = 0.0  # 让每条记录都立即到期，便于显式 flush
    yield
    with pf._audit_lock:
        pf._audit_buffer.clear()
        pf._audit_overflow.clear()
    pf._audit_overflow_evicted = 0


async def _insert_audit_row(
    conn,
    *,
    row_id: str,
    action: str = "chat",
    success: int = 0,
    duration: float = 1.0,
    error: str = "",
    error_class: str | None = "",
    attempt_count: int | None = 1,
    attempts_json: str = "",
    created_at: str | None = None,
):
    await conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
        " prompt_tokens, completion_tokens, cached_tokens, duration, success, error,"
        " scene, config_id, base_url, error_class, attempt_count, attempts_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now','localtime')))",
        (
            row_id,
            "p",
            "m",
            action,
            10,
            20,
            0,
            duration,
            success,
            error,
            "s",
            "cfg",
            "https://x",
            error_class,
            attempt_count,
            attempts_json,
            created_at,
        ),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# 1. 错误 → 类别映射
# ---------------------------------------------------------------------------


class TestErrorClassMapping:
    def test_rate_limit(self):
        assert pf._error_class("HTTP 429: 免费额度限流") == "rate_limit"
        assert pf._error_class(RuntimeError("Too Many Requests")) == "rate_limit"

    def test_auth(self):
        assert pf._error_class("HTTP 401: invalid api key") == "auth"
        assert pf._error_class("HTTP 403: forbidden") == "auth"

    def test_timeout(self):
        assert pf._error_class(TimeoutError("timed out")) == "timeout"
        assert pf._error_class("读取超时") == "timeout"

    def test_truncated_beats_invalid_response(self):
        """思考吞噬 + 空正文必须归 truncated（处置是加预算，不是重试格式）。"""
        assert pf._error_class("finish_reason=length，正文为空") == "truncated"
        assert pf._error_class("模型思考占满 max_tokens（输出被截断）") == "truncated"

    def test_network_and_upstream_5xx(self):
        assert pf._error_class("httpx.ConnectError: connection refused") == "network"
        assert pf._error_class("HTTP 502: Bad Gateway") == "upstream_5xx"
        assert pf._error_class("HTTP 503: Service Unavailable") == "upstream_5xx"

    def test_invalid_response(self):
        assert pf._error_class("invalid json: unexpected token") == "invalid_response"
        assert pf._error_class("返回空内容") == "invalid_response"

    def test_circuit_skipped(self):
        assert pf._error_class("熔断器 OPEN，跳过本次调用") == "circuit_skipped"
        assert pf._error_class("配额冷却，跳过本次调用") == "circuit_skipped"

    def test_unknown_and_empty(self):
        assert pf._error_class("") == ""
        assert pf._error_class(None) == ""
        assert pf._error_class("mystery failure") == "unknown"
        # 所有非空映射结果都必须在白名单内（前端/统计共用枚举）
        for text in ("HTTP 429", "HTTP 401", "timed out", "finish_reason=length", "HTTP 500", "bad json"):
            assert pf._error_class(text) in pf.ERROR_CLASSES


# ---------------------------------------------------------------------------
# 2/3/4. 合并审计
# ---------------------------------------------------------------------------


@pytest.fixture
def merged_pipeline(monkeypatch):
    """隔离 chat_with_fallback 外部依赖，并用「可控尝试序列」替换真实 provider。"""
    monkeypatch.setattr(settings, "ai_hedge_enabled", False)

    primary = {
        "provider_name": "cfg1",
        "api_key": "k",
        "base_url": "https://x",
        "model": "m",
        "max_tokens": 4000,
        "temperature": 0.7,
        "timeout": 60,
        "config_id": "cfg1",
        "_is_primary": True,
    }
    backup = {
        "provider_name": "cfg2",
        "api_key": "k",
        "base_url": "https://x2",
        "model": "m2",
        "max_tokens": 4000,
        "temperature": 0.7,
        "timeout": 60,
        "config_id": "cfg2",
        "_is_primary": False,
    }

    async def _resolve(ai_config, scene):
        return primary

    async def _build(cfg):
        return [dict(primary), dict(backup)]

    monkeypatch.setattr(pf, "_resolve_chat_config", _resolve)
    monkeypatch.setattr(pf, "_build_candidates", _build)
    monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")

    async def _gate(candidates, *_a, **_kw):
        return candidates, None

    monkeypatch.setattr(pf, "_gate_candidates", _gate)

    def _make_attempt(outcomes):
        seq = list(outcomes)

        async def _attempt(c, *args, **kwargs):
            scope = kwargs.get("audit_scope")
            outcome = seq.pop(0)
            pname = c.get("provider_name", "?")
            if outcome is None:
                await pf._record_attempt(
                    scope,
                    provider_name=pname,
                    model=c.get("model", ""),
                    config_id=c.get("config_id", ""),
                    base_url=c.get("base_url", ""),
                    duration=1.0,
                    success=True,
                    prompt_tokens=100,
                    completion_tokens=50,
                )
                c["_provider"] = object()
                return ("win-" + pname), None
            await pf._record_attempt(
                scope,
                provider_name=pname,
                model=c.get("model", ""),
                config_id=c.get("config_id", ""),
                base_url=c.get("base_url", ""),
                duration=0.2,
                success=False,
                error=outcome,
            )
            return None, RuntimeError(outcome)

        return _attempt

    return monkeypatch, _make_attempt


async def _flush_and_rows(db_conn):
    await pf._flush_audit_buffer()
    cur = await db_conn.execute(
        "SELECT provider_name, model, success, error, error_class, attempt_count, attempts_json,"
        " duration FROM ai_audit_logs WHERE action='chat' ORDER BY created_at, rowid"
    )
    return [dict(r) for r in await cur.fetchall()]


class TestMergedAudit:
    async def test_fallback_retry_writes_one_success_row(self, db_conn, merged_pipeline):
        monkeypatch, make_attempt = merged_pipeline
        monkeypatch.setattr(pf, "_attempt_candidate", make_attempt(["HTTP 429: 限流", None]))

        await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="s")
        rows = await _flush_and_rows(db_conn)

        assert len(rows) == 1, "一次逻辑调用（1 失败 + 1 成功）只能落一条主记录"
        row = rows[0]
        assert row["success"] == 1
        assert row["provider_name"] == "cfg2"
        assert row["attempt_count"] == 2
        assert row["error_class"] == ""
        attempts = json.loads(row["attempts_json"])
        assert [a["ok"] for a in attempts] == [0, 1]
        assert attempts[0]["error_class"] == "rate_limit"
        assert attempts[1]["provider"] == "cfg2"

    async def test_all_failed_keeps_last_class_and_each_attempt(self, db_conn, merged_pipeline):
        monkeypatch, make_attempt = merged_pipeline
        monkeypatch.setattr(
            pf,
            "_attempt_candidate",
            make_attempt(["HTTP 429: 限流", "TimeoutError: timed out"]),
        )

        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="s")
        rows = await _flush_and_rows(db_conn)

        assert len(rows) == 1
        row = rows[0]
        assert row["success"] == 0
        assert row["error_class"] == "timeout", "主记录类别=最终失败类别"
        assert row["attempt_count"] == 2
        attempts = json.loads(row["attempts_json"])
        assert {a["error_class"] for a in attempts} == {"rate_limit", "timeout"}

    async def test_fast_fail_without_upstream_records_one_row(self, db_conn, monkeypatch):
        async def _fast_fail(*_a, **_kw):
            raise RuntimeError("所有 Provider 均处于熔断冷却/配额冷却，拒绝快速失败")

        monkeypatch.setattr(pf, "_resolve_chat_config", _fast_fail)

        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="s")
        rows = await _flush_and_rows(db_conn)

        assert len(rows) == 1, "未发请求的快速失败也必须有一条主记录，才能与面板失败数对账"
        assert rows[0]["success"] == 0
        assert rows[0]["attempt_count"] == 0
        assert rows[0]["error_class"] == "circuit_skipped"

    async def test_cancelled_hedge_loser_included_in_attempt_count(self, db_conn, monkeypatch):
        """落败对冲副本被取消：进入胜者 attempts_json 且不写独立失败行。"""

        primary = {
            "provider_name": "cfg1",
            "api_key": "k",
            "base_url": "https://x",
            "model": "m",
            "max_tokens": 4000,
            "temperature": 0.7,
            "timeout": 60,
            "config_id": "cfg1",
            "_is_primary": True,
        }
        backup = {
            "provider_name": "cfg2",
            "api_key": "k",
            "base_url": "https://x2",
            "model": "m2",
            "max_tokens": 4000,
            "temperature": 0.7,
            "timeout": 60,
            "config_id": "cfg2",
            "_is_primary": False,
        }

        async def _resolve(ai_config, scene):
            return primary

        async def _build(cfg):
            return [dict(primary), dict(backup)]

        monkeypatch.setattr(settings, "ai_hedge_enabled", True)
        monkeypatch.setattr(settings, "ai_hedge_delay_seconds", 0.05)
        monkeypatch.setattr(settings, "ai_hedge_adaptive_enabled", False)
        monkeypatch.setattr(pf, "_resolve_chat_config", _resolve)
        monkeypatch.setattr(pf, "_build_candidates", _build)
        monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")

        async def _gate(candidates, *_a, **_kw):
            return candidates, None

        monkeypatch.setattr(pf, "_gate_candidates", _gate)

        async def _attempt(c, *args, **kwargs):
            scope = kwargs.get("audit_scope")
            pname = c.get("provider_name", "?")
            if pname == "cfg1":
                # 比对冲阈值(0.05s)慢，确保副本被拉起后再由 cfg1 胜出
                await asyncio.sleep(0.2)
                await pf._record_attempt(
                    scope,
                    provider_name=pname,
                    model=c.get("model", ""),
                    config_id=c.get("config_id", ""),
                    base_url=c.get("base_url", ""),
                    duration=1.0,
                    success=True,
                    prompt_tokens=100,
                    completion_tokens=50,
                )
                return ("win-cfg1"), None
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # 与 _attempt_candidate 的取消分支同口径
                scope.record(
                    provider_name=pname,
                    model=c.get("model", ""),
                    config_id=c.get("config_id", ""),
                    base_url=c.get("base_url", ""),
                    duration=0.3,
                    success=False,
                    error="调用被取消（对冲落败/任务停止）",
                    error_class="cancelled",
                )
                raise
            return ("win-cfg2"), None

        monkeypatch.setattr(pf, "_attempt_candidate", _attempt)

        await pf.chat_with_fallback([{"role": "user", "content": "hi"}], scene="s")
        rows = await _flush_and_rows(db_conn)

        assert len(rows) == 1
        row = rows[0]
        assert row["success"] == 1
        assert row["provider_name"] == "cfg1"
        assert row["attempt_count"] == 2
        attempts = json.loads(row["attempts_json"])
        assert [a["error_class"] for a in attempts] == ["", "cancelled"]


# ---------------------------------------------------------------------------
# 5. 迁移幂等 + 历史 NULL 兼容
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE ai_audit_logs (
    id TEXT PRIMARY KEY,
    provider_name TEXT,
    model TEXT,
    action TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    success INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    scene TEXT DEFAULT '',
    config_id TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


async def _columns(conn) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(ai_audit_logs)")
    return {r[1] for r in await cur.fetchall()}


class TestMigrationAndLegacyRows:
    async def test_migrate_idempotent_adds_columns(self):
        from app.db import _migrate

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_LEGACY_SCHEMA)
        await conn.commit()
        assert "error_class" not in await _columns(conn)

        await _migrate(conn)
        await conn.commit()
        cols = await _columns(conn)
        assert {"error_class", "attempt_count", "attempts_json"} <= cols

        # 第二次执行不得报错/重复加列
        await _migrate(conn)
        await conn.commit()
        assert await _columns(conn) == cols
        await conn.close()

    async def test_legacy_null_rows_are_readable(self):
        from app.db import _migrate

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_LEGACY_SCHEMA)
        # 迁移前插入的历史行（新列不存在）
        await conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, error,"
            " duration, scene, config_id, base_url) VALUES"
            " ('old','p','m','chat',0,'HTTP 429','0.2','s','cfg','https://x')"
        )
        await conn.commit()
        await _migrate(conn)
        await conn.commit()

        # 显式 NULL（模拟历史回填缺失）也必须可读
        await _insert_audit_row(
            conn,
            row_id="old-null",
            success=0,
            error="boom",
            error_class=None,
            attempt_count=None,
        )

        stats = await usage_mod.ai_stats(days=1, db=conn)
        assert stats["summary"]["total"] == 2
        assert stats["summary"]["attempts"] == 2, "历史 NULL → COALESCE 默认 1"
        assert stats["summary"]["failed_count"] == 2
        assert stats["by_error"] and stats["by_error"][0]["calls"] >= 1
        classes = {r["error_class"]: r["calls"] for r in stats["by_error_class"]}
        assert classes.get("unknown") == 2, "历史 NULL/'' 归 unknown"

        logs = await usage_mod.audit_logs(days=1, db=conn)
        # 明细行 error_class 保留原值（'' 表示未分类），不报错；筛选项归 unknown
        assert all((r["error_class"] or "") == "" for r in logs["items"])
        assert "unknown" in logs["error_classes"]
        await conn.close()


# ---------------------------------------------------------------------------
# 6. 统计聚合口径
# ---------------------------------------------------------------------------


class TestErrorClassAggregation:
    async def test_by_error_class_and_logical_denominator(self, db_conn):
        # 3 次失败逻辑调用（其中一次含 2 次尝试）+ 1 次成功（含 2 次尝试）
        await _insert_audit_row(
            db_conn, row_id="a", success=0, error="HTTP 429", error_class="rate_limit", attempt_count=2
        )
        await _insert_audit_row(
            db_conn, row_id="b", success=0, error="HTTP 429", error_class="rate_limit", attempt_count=1
        )
        await _insert_audit_row(
            db_conn, row_id="c", success=0, error="timed out", error_class="timeout", attempt_count=1
        )
        await _insert_audit_row(
            db_conn, row_id="d", success=1, duration=22.0, error_class="", attempt_count=2
        )
        # 跳过行（非 chat）不得进入任何分母
        await _insert_audit_row(
            db_conn, row_id="e", action="circuit_skipped", success=0, error_class="circuit_skipped", attempt_count=0
        )

        stats = await usage_mod.ai_stats(days=1, db=db_conn)
        summary = stats["summary"]
        assert summary["total"] == 4
        assert summary["success_count"] == 1
        assert summary["failed_count"] == 3
        assert summary["attempts"] == 6
        assert summary["attempt_failed"] == 5
        assert summary["success_rate"] == 25.0
        assert summary["attempt_success_rate"] == round(1 / 6 * 100, 1)
        assert summary["avg_duration"] == 22.0, "平均耗时只算成功逻辑调用"
        assert stats["daily"][0]["failed_count"] == 3
        assert stats["by_action"] and any(r["action"] == "circuit_skipped" for r in stats["by_action"])

        classes = {r["error_class"]: r for r in stats["by_error_class"]}
        assert classes["rate_limit"]["calls"] == 2
        assert classes["rate_limit"]["share"] == 66.7
        assert classes["timeout"]["calls"] == 1
        assert classes["timeout"]["share"] == 33.3

    async def test_activity_attempts_and_error_classes(self, db_conn, monkeypatch):
        from app.routers import system as system_mod

        monkeypatch.setattr(system_mod, "get_ai_live_stats", lambda: {})
        await _insert_audit_row(
            db_conn, row_id="a1", success=0, error="HTTP 429", error_class="rate_limit", attempt_count=2
        )
        await _insert_audit_row(db_conn, row_id="a2", success=1, attempt_count=3)
        await _insert_audit_row(
            db_conn, row_id="a3", action="circuit_skipped", error_class="circuit_skipped", attempt_count=0
        )

        ai = (await system_mod._build_activity_snapshot())["ai"]
        assert ai["real_calls_today"] == 2
        assert ai["attempts_today"] == 5
        assert ai["failed_today"] == 1
        assert ai["attempt_failed_today"] == 4
        assert ai["success_rate"] == 50.0
        assert ai["skipped_today"] == 1
        assert ai["error_classes_today"] == {"rate_limit": 1}
