"""A1（2026-10-05）审计溢出队列护栏测试

背景：2026-09-23 上线的 retry_db_op 已解决 `database is locked` 瞬态锁
导致的审计永久丢失，但若同一批审计记录连续遭遇三次瞬态锁（罕见但真实，
例如并发 DDL + 冷启动 + 慢盘三重叠加），retry_db_op 耗尽后仍会丢失。

本测试验证四条护栏：
F1. 溢出队列的审计行在后续 flush 中真正落库（不丢）
F2. 溢出队列容量护栏：超过上限时最旧批次被驱逐且计数累计
F3. 滞留时长护栏：超过 AUDIT_OVERFLOW_MAX_AGE 的批次被清理
F4. 公开 flush 入口也会触发溢出队列清理（回归护栏）
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest
from app.services.ai import provider_factory as pf


def _reset_audit_state() -> None:
    """清理审计相关模块状态，避免测试间互相污染。"""
    with pf._audit_lock:
        pf._audit_buffer.clear()
        pf._audit_overflow.clear()
    pf._audit_overflow_evicted = 0


def _make_row(i: int) -> tuple:
    """构造一条 13 字段的审计行（与 _audit_buffer.append 保持一致）。"""
    return (
        f"audit-{i}", "deepseek", "deepseek-chat", "chat",
        100, 50, 0, 0.5, 1, "", "test_scene", "cfg-1", "https://example.com")


@pytest.fixture(autouse=True)
def _clean_audit_state():
    _reset_audit_state()
    yield
    _reset_audit_state()


class TestAuditOverflowRecovery:
    """F1：审计批量落库重试耗尽后，行进入溢出队列，下一轮 flush 能落库。"""

    async def test_overflow_rows_later_flush_into_db(
        self, db_conn, caplog, monkeypatch,
    ) -> None:
        """DB 锁期间行进溢出队列保留；DB 恢复后下一次 flush 从溢出队列落库。

        模拟序列：
          flush#1: DB 锁 → 主缓冲失败 → 入溢出队列 → drain 再失败 → 保留
          flush#2: DB 仍锁 → 溢出队列 drain 失败 → 仍保留
          flush#3: DB 恢复 → 溢出队列 drain 成功 → 落库 → 溢出队列清空
        """
        original_flush = pf._flush_audit_rows_once
        db_state = {"broken": True}

        async def flaky(rows):
            if db_state["broken"]:
                raise sqlite3.OperationalError("database is locked")
            await original_flush(rows)

        monkeypatch.setattr(pf, "retry_db_op",
                            lambda coro_factory, **_kw: coro_factory())
        monkeypatch.setattr(pf, "_flush_audit_rows_once", flaky)

        for i in range(3):
            pf._audit_buffer.append(_make_row(i))

        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._flush_audit_buffer()

        assert pf._audit_buffer == [], "主缓冲应已清空"
        assert len(pf._audit_overflow) == 1, "第一次失败后行应留在溢出队列"
        overflow_ids = {r[0] for r in pf._audit_overflow[0][1]}
        assert overflow_ids == {"audit-0", "audit-1", "audit-2"}

        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._flush_audit_buffer()

        assert len(pf._audit_overflow) == 1, "DB 仍锁时行应继续留在溢出队列"

        # DB 恢复：溢出队列 drain 成功
        db_state["broken"] = False
        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._flush_audit_buffer()

        assert len(pf._audit_overflow) == 0, "溢出队列应已清空"
        cur = await db_conn.execute(
            "SELECT id FROM ai_audit_logs WHERE id LIKE 'audit-%' ORDER BY id")
        stored_ids = [row[0] for row in await cur.fetchall()]
        assert stored_ids == ["audit-0", "audit-1", "audit-2"], (
            f"溢出队列恢复后审计行应全部落库，实际: {stored_ids}")

    async def test_no_silent_loss_when_db_locks(
        self, db_conn, caplog, monkeypatch,
    ) -> None:
        """核心不变式：无论 DB 锁多少轮，主缓冲不静默丢失 ——
        要么进 DB，要么在溢出队列里等待，且每次失败都留下告警。
        """
        async def always_fail(rows):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(pf, "retry_db_op",
                            lambda coro_factory, **_kw: coro_factory())
        monkeypatch.setattr(pf, "_flush_audit_rows_once", always_fail)

        for i in range(2):
            pf._audit_buffer.append(_make_row(i))

        with caplog.at_level(logging.WARNING, logger="provider_factory"):
            await pf._flush_audit_buffer()

        # 2 条审计行：无论 flush 结果如何，主缓冲 + 溢出队列的条数总和
        # 应等于 2，绝不允许 < 2（那意味着静默丢失）
        total_in_system = len(pf._audit_buffer) + sum(
            len(r) for _ts, r in pf._audit_overflow)
        assert total_in_system == 2, (
            f"审计行必须始终存在于系统内（主缓冲或溢出队列），实际 {total_in_system}")

        # 必须有告警记录（对齐 AGENTS.md「静默丢失零容忍」）
        warn_lines = [
            rec.getMessage() for rec in caplog.records
            if rec.name == "provider_factory"
            and rec.levelno == logging.WARNING
            and "溢出队列" in rec.getMessage()
        ]
        assert warn_lines, "溢出路径必须留下可检索的告警"


class TestAuditOverflowGuards:
    """F2/F3：溢出队列容量与滞留时长双护栏。"""

    async def test_capacity_guard_evicts_oldest_batch(self, db_conn) -> None:
        """容量护栏：队列达到上限时新批次入队触发最旧批次驱逐，计数累计。"""
        # 预填至满（MAX_BATCHES），再入一条 → 驱逐一次
        ts = time.time()
        with pf._audit_lock:
            for _ in range(pf.AUDIT_OVERFLOW_MAX_BATCHES):
                pf._audit_overflow.append((ts, [_make_row(0)]))

        evicted_before = pf._audit_overflow_evicted
        pf._enqueue_audit_overflow([_make_row(999)], "test-eject")

        assert len(pf._audit_overflow) == pf.AUDIT_OVERFLOW_MAX_BATCHES, (
            f"溢出队列长度应保持 {pf.AUDIT_OVERFLOW_MAX_BATCHES}，"
            f"实际 {len(pf._audit_overflow)}")
        assert pf._audit_overflow_evicted == evicted_before + 1, (
            "容量驱逐应累计计数")

    async def test_age_guard_evicts_stale_batch(self, db_conn) -> None:
        """滞留时长护栏：超过 AUDIT_OVERFLOW_MAX_AGE 的批次被清理。"""
        old_ts = time.time() - pf.AUDIT_OVERFLOW_MAX_AGE - 1
        fresh_ts = time.time()
        with pf._audit_lock:
            pf._audit_overflow.append((old_ts, [_make_row(1)]))
            pf._audit_overflow.append((fresh_ts, [_make_row(2)]))

        before = pf._audit_overflow_evicted
        pf._expire_audit_overflow()

        assert pf._audit_overflow_evicted == before + 1
        assert len(pf._audit_overflow) == 1
        # 幸存的应是新批次
        assert pf._audit_overflow[0][1][0][0] == "audit-2"


class TestPublicFlushEntryPoint:
    """F4：溢出队列清理不应被公开 flush 入口阻断（回归护栏）。"""

    async def test_flush_audit_buffer_public_exposes_overflow_drain(
        self, db_conn, monkeypatch,
    ) -> None:
        """`flush_audit_buffer` 公开入口也触发溢出消费。"""
        async def fake_flush_once(rows):
            return None

        monkeypatch.setattr(pf, "retry_db_op",
                            lambda coro_factory, **_kw: coro_factory())
        monkeypatch.setattr(pf, "_flush_audit_rows_once", fake_flush_once)

        # 手工塞一条溢出条目
        with pf._audit_lock:
            pf._audit_overflow.append((time.time(), [_make_row(42)]))

        await pf.flush_audit_buffer()

        assert len(pf._audit_overflow) == 0, "公开 flush 入口应清空溢出队列"
