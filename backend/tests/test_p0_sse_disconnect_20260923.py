"""P0 断连修复回归（2026-09-23）——事务泄漏 + 僵尸任务不落终态。

事故根因链（已确认）：
  with_heartbeat._gen_to_queue 的 finally 裸 await queue.put(("done", None))
  → 消费者断开后队列满 → 生产者永久悬挂 → 外层 gather 永不返回 →
  内层 event_stream 永不被 aclose → 其 finally（finish_task / 事务收敛）
  永不执行 → 僵尸 running 任务 + 全局共享连接挂着未提交写事务 →
  全站写 500 "database is locked"。

本文件覆盖其中两处修复的反例回归（R1 with_heartbeat 收尾语义在
test_sse_utils.py::TestWithHeartbeatDisconnectSafety）：
  R2. db.settle_global_conn —— 全局连接悬挂写事务兜底回滚
  R3. task_registry.reap_orphan_tasks —— 运行期僵尸任务周期回收
"""
from datetime import datetime, timedelta

import app.db as db_mod
import pytest
from app.services.ai import task_registry as _tr


# ============================================================
# R2. settle_global_conn：全局共享连接悬挂写事务的兜底收敛
# ============================================================
class TestSettleGlobalConn:
    """settle_global_conn 只在 in_transaction=True 时回滚，且绝不抛异常。"""

    async def test_rolls_back_dangling_transaction(self, monkeypatch):
        calls = {"rb": 0}

        class _FakeConn:
            in_transaction = True

            async def rollback(self):
                calls["rb"] += 1

        monkeypatch.setattr(db_mod, "_conn", _FakeConn())
        await db_mod.settle_global_conn("unit_test")
        assert calls["rb"] == 1, "悬挂写事务必须被回滚（解冻全站写的关键）"

    async def test_noop_when_not_in_transaction(self, monkeypatch):
        calls = {"rb": 0}

        class _FakeConn:
            in_transaction = False

            async def rollback(self):
                calls["rb"] += 1

        monkeypatch.setattr(db_mod, "_conn", _FakeConn())
        await db_mod.settle_global_conn("unit_test")
        assert calls["rb"] == 0, "无活动事务时绝不能误回滚别人的写"

    async def test_noop_when_no_connection(self, monkeypatch):
        monkeypatch.setattr(db_mod, "_conn", None)
        await db_mod.settle_global_conn("unit_test")  # 不得抛异常

    async def test_swallows_rollback_error(self, monkeypatch):
        class _FakeConn:
            in_transaction = True

            async def rollback(self):
                raise RuntimeError("db is locked")

        monkeypatch.setattr(db_mod, "_conn", _FakeConn())
        # 收敛失败只记 WARNING，绝不允许把异常抛回事件流 finally
        await db_mod.settle_global_conn("unit_test")


# ============================================================
# R3. reap_orphan_tasks：运行期僵尸任务周期回收
# ============================================================
class TestReapOrphanTasks:
    """DB running + 无内存态 + 超时未更新 → 落终态 stopped；三层防误伤。"""

    async def _insert(self, db_conn, task_id: str, *, status: str, updated_at: str):
        await db_conn.execute(
            "INSERT INTO task_registry (id, task_type, status, progress, "
            "created_at, updated_at) VALUES (?, 'facts_generation', ?, 55.0, ?, ?)",
            (task_id, status, updated_at, updated_at))
        await db_conn.commit()

    async def _status(self, db_conn, task_id: str) -> str:
        cur = await db_conn.execute(
            "SELECT status FROM task_registry WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return row["status"] if row is not None else "<missing>"

    async def test_reaps_stale_orphan(self, db_conn):
        stale = (datetime.now() - timedelta(seconds=2000)).isoformat()
        await self._insert(db_conn, "p0-zombie-1", status="running", updated_at=stale)
        n = await _tr.reap_orphan_tasks(max_stale_seconds=900)
        assert n >= 1
        assert await self._status(db_conn, "p0-zombie-1") == "stopped"

    async def test_reaps_stale_paused_orphan(self, db_conn):
        stale = (datetime.now() - timedelta(seconds=2000)).isoformat()
        await self._insert(db_conn, "p0-zombie-2", status="paused", updated_at=stale)
        await _tr.reap_orphan_tasks(max_stale_seconds=900)
        assert await self._status(db_conn, "p0-zombie-2") == "stopped"

    async def test_fresh_running_untouched(self, db_conn):
        """刚注册/刚有进度的运行中任务绝不能被误杀（防误伤第一层）"""
        now = datetime.now().isoformat()
        await self._insert(db_conn, "p0-fresh-1", status="running", updated_at=now)
        await _tr.reap_orphan_tasks(max_stale_seconds=900)
        assert await self._status(db_conn, "p0-fresh-1") == "running"

    async def test_completed_task_untouched(self, db_conn):
        """已完成任务不在扫描范围（幂等 UPDATE 只碰 running/paused）"""
        stale = (datetime.now() - timedelta(seconds=2000)).isoformat()
        await self._insert(db_conn, "p0-done-1", status="completed", updated_at=stale)
        await _tr.reap_orphan_tasks(max_stale_seconds=0)
        assert await self._status(db_conn, "p0-done-1") == "completed"

    async def test_in_memory_task_untouched(self, db_conn):
        """本进程内存态存在的任务绝不能被回收（防误伤第二层）"""
        stale = (datetime.now() - timedelta(seconds=2000)).isoformat()
        await self._insert(db_conn, "p0-mem-1", status="running", updated_at=stale)
        _tr._tasks["p0-mem-1"] = {"task_id": "p0-mem-1", "task_type": "facts_generation"}
        try:
            await _tr.reap_orphan_tasks(max_stale_seconds=0)
            assert await self._status(db_conn, "p0-mem-1") == "running"
        finally:
            _tr._tasks.pop("p0-mem-1", None)
            await db_conn.execute(
                "DELETE FROM task_registry WHERE id = 'p0-mem-1'")
            await db_conn.commit()

    async def test_returns_zero_on_empty(self, db_conn):
        assert await _tr.reap_orphan_tasks(max_stale_seconds=900) == 0
