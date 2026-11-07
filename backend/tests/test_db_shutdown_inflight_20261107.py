"""关闭收口护栏（2026-11-07）：任务终态必须先于 ``close_db()`` 写入。

锁死的反直觉点：
  关闭收尾之后，库里是 ``failed``，且 ``_conn is None`` —— 终态写入**不允许**
  依赖 ``close_db()`` 之后 ``get_conn()`` 静默重建连接来“补”出来。将来若有人
  顺手堵住“关闭后仍可新建连接”（P3），没有这条护栏，终态会静默退化成
  ``running``，而且很难联想到是那次改动干的。
"""

import asyncio
import sqlite3
import threading

import app.db as db
import pytest
from app.services.ai import task_registry as tr


def _read_row(db_path, task_id: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT status, message, progress FROM task_registry WHERE id=?", (task_id,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"task_registry 里查不到任务 {task_id}"
    return dict(row)


def _live_aiosqlite_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive() and "_connection_worker_thread" in t.name
    ]


@pytest.mark.asyncio
async def test_lifespan_shutdown_writes_failed_before_close_db(tmp_path, monkeypatch):
    """真实 lifespan：关闭钩子先落 failed，再关连接；终态不靠重建连接。"""
    from app.main import app, lifespan

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "shutdown_inflight.db")
    tr._tasks.clear()
    tr._detached_tasks.clear()
    entered = asyncio.Event()
    release = asyncio.Event()
    child_task = None

    async def holding_child():
        # 持有借出写连接跨过关闭点：旧行为下 close_db 会关掉它，任务随后撞
        # ValueError；若终态写在 close_db 之后，就只能靠重建连接“补”出来。
        async with db.write_tx_conn():
            entered.set()
            await release.wait()

    try:
        async with lifespan(app):
            tid = await tr.register_task("content", "project_1", "scheme_1")
            child_task = asyncio.create_task(holding_child())
            assert tr.register_child_task(tid, child_task)
            await asyncio.wait_for(entered.wait(), 2.0)
            assert _read_row(db.DB_PATH, tid)["status"] == "running"

        # lifespan 退出：钩子（close_db 之前）应把在途任务落 failed
        row = _read_row(db.DB_PATH, tid)
        assert row["status"] == "failed"
        assert row["message"] == "服务关闭，任务中断"
        # 反直觉点：终态落库没有依赖关闭后重建连接
        assert db._conn is None
        assert db._conn_path is None
        assert db._db_pool_total == 0
        assert _live_aiosqlite_threads() == []
        assert tid not in tr._tasks
    finally:
        release.set()
        if child_task is not None:
            await asyncio.gather(child_task, return_exceptions=True)
        tr._tasks.clear()
        tr._detached_tasks.clear()
        await db.close_db()


@pytest.mark.asyncio
async def test_shutdown_inflight_cancels_tracked_reaper(tmp_path, monkeypatch):
    """mark_stream_ended 的游离回收任务必须在关闭段被取消，不能关闭后补写。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reaper.db")
    tr._tasks.clear()
    tr._detached_tasks.clear()
    try:
        await db.init_db()
        tid = await tr.register_task("content", "project_1", "scheme_1")
        assert tr.mark_stream_ended(tid, grace_seconds=5.0)
        assert len(tr._detached_tasks) == 1
        reaper = next(iter(tr._detached_tasks))

        await tr.shutdown_inflight()
        assert reaper.done() and reaper.cancelled()
        assert tr._detached_tasks == set()

        row = _read_row(db.DB_PATH, tid)
        assert row["status"] == "failed"
        assert row["message"] == "服务关闭，任务中断"
    finally:
        tr._tasks.clear()
        tr._detached_tasks.clear()
        await db.close_db()


@pytest.mark.asyncio
async def test_close_db_cancels_integrity_probe_task(tmp_path, monkeypatch):
    """close_db 必须取消运行期完整性巡检任务（此前从未被取消）。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "probe.db")
    started = asyncio.Event()

    async def _probe_loop():
        started.set()
        await asyncio.sleep(3600)

    probe = asyncio.create_task(_probe_loop())
    monkeypatch.setattr(db, "_probe_task", probe)
    await asyncio.wait_for(started.wait(), 1.0)
    try:
        await db.close_db()
        assert db._probe_task is None
        assert probe.cancelled()
    finally:
        db._probe_task = None
        if not probe.done():
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
        await db.close_db()
