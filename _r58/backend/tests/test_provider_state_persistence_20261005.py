"""A2（2026-10-05）Provider 运行时状态持久化护栏测试

背景：`_quota_cool_until` / `_quota_last_probe` 与 `AnalysisCircuitBreaker._per_provider`
是纯内存 dict，进程重启即清零。若重启发生在冷却窗口或熔断窗口内，冷却/熔断
「蒸发」→ 下一次调用又白烧一次 429 或一次死配置网络往返。

本测试验证：
F1. flush 写入非默认状态（quota_cooldown + circuit_breaker 双 kind）
F2. warmup 从 DB 恢复仍在窗口内的状态
F3. warmup 跳过已过期状态（cool_until 已过、OPEN 已过期）
F4. flush 跳过默认状态（避免全 0 默认态刷屏）
F5. flush 顺手清理 24h 未更新的陈旧行
F6. 表缺失时 warmup / flush 均静默降级，不影响主流程
"""
from __future__ import annotations

import json
import time

import pytest
from app.services.ai import provider_factory as pf
from app.services.ai.workflows_base import AnalysisCircuitBreaker


@pytest.fixture
def cb():
    """独立的熔断器实例（不污染全局 circuit_breaker）。"""
    return AnalysisCircuitBreaker()


@pytest.fixture(autouse=True)
def _reset_and_teardown(db_conn):
    """每个测试前后清空全局熔断器的 per-provider 状态（配额冷却由 conftest 已重置）。"""
    pf.circuit_breaker._per_provider.clear()
    yield
    pf.circuit_breaker._per_provider.clear()


class TestSnapshotRestore:
    """AnalysisCircuitBreaker 的 snapshot/restore 契约。"""

    def test_snapshot_is_deep_copy(self, cb) -> None:
        cb.record_failure("p1")
        cb.record_failure("p1")
        snap = cb.snapshot_state()
        cb.record_success("p1")
        assert snap["p1"]["state"] == "CLOSED" or snap["p1"]["state"] != cb._per_provider["p1"]["state"]
        assert snap["p1"]["failures"] == 2, "快照应保留写入时的 failures 计数"
        assert cb._per_provider["p1"]["failures"] == 0, "原对象应被 record_success 重置"

    def test_restore_downgrades_expired_open_to_half_open(self, cb) -> None:
        snap = {
            "p1": {
                "state": "OPEN", "failures": 5,
                "opened_at": time.time() - 9999,
                "effective_cooldown": 15,
                "last_failure_at": 0.0,
            }
        }
        n = cb.restore_state(snap)
        assert n == 1
        assert cb._per_provider["p1"]["state"] == "HALF_OPEN"

    def test_restore_keeps_fresh_open(self, cb) -> None:
        snap = {
            "p1": {
                "state": "OPEN", "failures": 5,
                "opened_at": time.time() - 1,
                "effective_cooldown": 15,
                "last_failure_at": 0.0,
            }
        }
        cb.restore_state(snap)
        assert cb._per_provider["p1"]["state"] == "OPEN"

    def test_restore_ignores_non_dict_entries(self, cb) -> None:
        assert cb.restore_state(None) == 0  # type: ignore[arg-type]
        assert cb.restore_state([]) == 0  # type: ignore[arg-type]
        assert cb.restore_state({"p1": "not-a-dict"}) == 0


class TestFlushWrites:
    """flush_provider_state_to_db 写入非默认状态。"""

    async def test_flush_writes_quota_and_circuit_breaker(self, db_conn) -> None:
        # 写入 quota_cooldown
        pf._note_quota_failure("deepseek", RuntimeError("HTTP 429"))
        # 写入 circuit_breaker OPEN
        pf.circuit_breaker.record_failure("deepseek", is_429=True)
        pf.circuit_breaker.record_failure("deepseek", is_429=True)
        pf.circuit_breaker.record_failure("deepseek", is_429=True)
        pf.circuit_breaker.record_failure("deepseek", is_429=True)
        pf.circuit_breaker.record_failure("deepseek", is_429=True)
        # 现在 state 应为 OPEN
        assert pf.circuit_breaker._per_provider["deepseek"]["state"] == "OPEN"

        await pf.flush_provider_state_to_db()

        cur = await db_conn.execute(
            "SELECT provider_name, kind, payload_json FROM ai_provider_state ORDER BY kind")
        rows = await cur.fetchall()
        assert len(rows) == 2
        quota_row = [r for r in rows if r[1] == pf._STATE_KIND_QUOTA][0]
        cb_row = [r for r in rows if r[1] == pf._STATE_KIND_CB][0]
        assert quota_row[0] == "deepseek"
        assert cb_row[0] == "deepseek"
        quota_payload = json.loads(quota_row[2])
        cb_payload = json.loads(cb_row[2])
        assert quota_payload["cool_until"] > time.time()
        assert cb_payload["state"] == "OPEN"
        assert cb_payload["failures"] == 5

    async def test_flush_skips_default_state(self, db_conn) -> None:
        """默认态（无冷却、无熔断）不写任何行，避免刷屏。"""
        # 明确设置 CLOSED 状态
        pf.circuit_breaker.record_success("deepseek")
        assert pf.circuit_breaker._per_provider["deepseek"]["state"] == "CLOSED"

        await pf.flush_provider_state_to_db()

        cur = await db_conn.execute("SELECT COUNT(*) FROM ai_provider_state")
        row = await cur.fetchone()
        assert row[0] == 0, "默认态不应产生任何 DB 行"

    async def test_flush_upserts_existing_row(self, db_conn) -> None:
        """同一 provider + kind 的行 UPSERT 不产生重复。"""
        pf._note_quota_failure("p1", RuntimeError("HTTP 429"))
        await pf.flush_provider_state_to_db()
        pf._note_quota_failure("p1", RuntimeError("HTTP 429"))  # 新冷却窗口
        await pf.flush_provider_state_to_db()

        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM ai_provider_state WHERE provider_name='p1'")
        row = await cur.fetchone()
        assert row[0] == 1, "同一 provider + kind 应始终只有一行（UPSERT）"


class TestWarmupReads:
    """warmup_provider_state_from_db 恢复仍在窗口内的状态。"""

    async def test_warmup_restores_active_quota_and_cb(self, db_conn) -> None:
        """模拟一次进程重启：先写 DB → 清空内存 → warmup 恢复。"""
        future_ts = time.time() + 60
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', ?, ?, datetime('now','localtime'))",
            (pf._STATE_KIND_QUOTA, json.dumps({"cool_until": future_ts, "last_probe": 0.0})),
        )
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p2', ?, ?, datetime('now','localtime'))",
            (pf._STATE_KIND_CB, json.dumps({
                "state": "OPEN", "failures": 5,
                "opened_at": time.time() - 1, "effective_cooldown": 15,
                "last_failure_at": 0.0,
            })),
        )
        await db_conn.commit()

        await pf.warmup_provider_state_from_db()

        with pf._quota_lock:
            assert pf._quota_cool_until["p1"] == future_ts
        assert pf.circuit_breaker._per_provider["p2"]["state"] == "OPEN"

    async def test_warmup_skips_expired_quota(self, db_conn) -> None:
        """已过期的 cool_until 不恢复（等价于「冷却已自然过期」）。"""
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', ?, ?, datetime('now','localtime'))",
            (pf._STATE_KIND_QUOTA, json.dumps({"cool_until": time.time() - 10, "last_probe": 0.0})),
        )
        await db_conn.commit()

        await pf.warmup_provider_state_from_db()

        with pf._quota_lock:
            assert "p1" not in pf._quota_cool_until

    async def test_warmup_downgrades_expired_open(self, db_conn) -> None:
        """OPEN 且 opened_at+cooldown 已过期 → 恢复为 HALF_OPEN。"""
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', ?, ?, datetime('now','localtime'))",
            (pf._STATE_KIND_CB, json.dumps({
                "state": "OPEN", "failures": 5,
                "opened_at": time.time() - 9999, "effective_cooldown": 15,
                "last_failure_at": 0.0,
            })),
        )
        await db_conn.commit()

        await pf.warmup_provider_state_from_db()

        assert pf.circuit_breaker._per_provider["p1"]["state"] == "HALF_OPEN"

    async def test_warmup_skips_closed_state(self, db_conn) -> None:
        """CLOSED 状态不恢复（等价于「已经恢复，无需恢复」）。"""
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', ?, ?, datetime('now','localtime'))",
            (pf._STATE_KIND_CB, json.dumps({"state": "CLOSED", "failures": 0})),
        )
        await db_conn.commit()

        await pf.warmup_provider_state_from_db()

        assert "p1" not in pf.circuit_breaker._per_provider


class TestCleanup:
    """flush 顺手清理 24h 未更新的陈旧行。"""

    async def test_flush_deletes_stale_rows(self, db_conn) -> None:
        """超过 24h 未更新的行在下次 flush 时被清理。"""
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('stale', 'quota_cooldown', '{}', datetime('now','localtime','-25 hours'))")
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('fresh', 'quota_cooldown', '{}', datetime('now','localtime'))")
        await db_conn.commit()

        pf._note_quota_failure("p1", RuntimeError("HTTP 429"))
        await pf.flush_provider_state_to_db()

        cur = await db_conn.execute(
            "SELECT provider_name FROM ai_provider_state ORDER BY provider_name")
        names = [r[0] for r in await cur.fetchall()]
        assert "stale" not in names, "24h 未更新的陈旧行应被清理"
        assert "fresh" in names
        assert "p1" in names


class TestGracefulDegradation:
    """表缺失或异常时静默降级，绝不影响主流程。"""

    async def test_warmup_missing_table_does_not_raise(self, db_conn) -> None:
        await db_conn.execute("DROP TABLE ai_provider_state")
        await db_conn.commit()
        # 不抛异常即可
        await pf.warmup_provider_state_from_db()

    async def test_flush_missing_table_does_not_raise(self, db_conn, caplog) -> None:
        import logging
        await db_conn.execute("DROP TABLE ai_provider_state")
        await db_conn.commit()

        pf._note_quota_failure("p1", RuntimeError("HTTP 429"))
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf.flush_provider_state_to_db()
        # 应有告警但不抛异常
        assert any("Provider 状态快照写入失败" in r.message for r in caplog.records)

    async def test_warmup_malformed_json_does_not_crash(self, db_conn) -> None:
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', 'quota_cooldown', 'not-json', datetime('now','localtime'))")
        await db_conn.commit()
        await pf.warmup_provider_state_from_db()  # 不抛异常即可
        with pf._quota_lock:
            assert "p1" not in pf._quota_cool_until

    async def test_warmup_bad_row_shape_does_not_crash(self, db_conn) -> None:
        # 缺 kind 列或 payload_json 空 → 应跳过
        await db_conn.execute(
            "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
            "VALUES('p1', '', '', datetime('now','localtime'))")
        await db_conn.commit()
        await pf.warmup_provider_state_from_db()
        with pf._quota_lock:
            assert "p1" not in pf._quota_cool_until
