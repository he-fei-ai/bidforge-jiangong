"""护栏（2026-11-07）：close_db() 必须回收「借出未归还」的池连接。

生产路径：lifespan 关闭段不等待/取消 task_registry 的后台 AI 任务；若某个任务
恰好停在 `write_tx_conn()` / `read_db()` 的借出窗口内，close_db() 只看空闲池，
该连接会漏掉，aiosqlite 的非守护工作线程让进程退不出去；任务随后归还时还会把
仍然存活的连接重新塞回已关闭的池里。

两条用例分别覆盖写池与读池：关闭后连接线程必须退出、计数归零，且「迟到归还」
不得重新入池。
"""

from __future__ import annotations

import asyncio
import threading
import time

import app.db as db


async def _wait_thread_stopped(thread: threading.Thread, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return not thread.is_alive()


async def test_close_db_closes_borrowed_write_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "inflight_write.db")
    await db.close_db()

    conn = await db._acquire_pool_conn()
    try:
        assert conn in db._db_pool_borrowed
        assert db._db_pool_total == 1

        await db.close_db()
        assert db._db_pool_total == 0
        assert db._db_pool == []
        assert db._db_pool_borrowed == set()
        assert await _wait_thread_stopped(conn._thread)

        # 模拟后台任务在 close_db 之后才归还：不得重新入池、计数不得变负
        await db._release_pool_conn(conn)
        assert db._db_pool == []
        assert db._db_pool_total == 0
    finally:
        # 修复被回退时也要保证本用例能退出，避免护栏自己挂住
        try:
            await conn.close()
        except Exception:
            pass
        await db.close_db()


async def test_close_db_closes_borrowed_read_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "inflight_read.db")
    await db.close_db()

    conn = await db._acquire_read_conn()
    try:
        assert conn in db._read_pool_borrowed
        assert db._read_pool_total == 1

        await db.close_db()
        assert db._read_pool_total == 0
        assert db._read_pool == []
        assert db._read_pool_borrowed == set()
        assert await _wait_thread_stopped(conn._thread)

        # 迟到归还同样不得回池
        await db._release_read_conn(conn)
        assert db._read_pool == []
        assert db._read_pool_total == 0
    finally:
        try:
            await conn.close()
        except Exception:
            pass
        await db.close_db()
