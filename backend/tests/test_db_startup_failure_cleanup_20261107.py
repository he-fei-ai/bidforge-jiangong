"""护栏（2026-11-07）：启动初始化失败路径必须回收 aiosqlite 连接。

生产影响：lifespan 在 `yield` 之前抛错时走不到关闭段，若 `get_conn()` 已建出
连接却不回收，aiosqlite 的非守护 `_connection_worker_thread` 会让失败进程
「起不来也不退出」，容器/systemd 只能等到超时。

两条用例分别覆盖：
1. `_setup_connection` 失败：关闭本次新建连接、重置 `_conn/_conn_path`；
2. `_migrate` 失败：`init_db()` 清理连接后**原样**抛出异常（类型与消息不变）。
"""

from __future__ import annotations

import asyncio
import threading
import time

import app.db as db
import pytest


async def _wait_thread_stopped(thread: threading.Thread, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return not thread.is_alive()


async def test_get_conn_setup_failure_closes_new_connection(tmp_path, monkeypatch):
    """连接建好但 setup 失败：不得把半成品挂到全局，且工作线程必须退出。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "setup_fail.db")
    await db.close_db()

    captured: dict[str, threading.Thread] = {}

    async def _boom(conn):
        captured["thread"] = conn._thread
        raise RuntimeError("setup boom")

    monkeypatch.setattr(db, "_setup_connection", _boom)
    try:
        with pytest.raises(RuntimeError, match="^setup boom$"):
            await db.get_conn()
        assert db._conn is None
        assert db._conn_path is None
        assert captured["thread"].daemon is False
        assert await _wait_thread_stopped(captured["thread"])
    finally:
        await db.close_db()


async def test_init_db_migration_failure_closes_and_reraises(tmp_path, monkeypatch):
    """迁移失败：异常照原样抛出，且全局连接已关闭、工作线程已退出。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "migrate_fail.db")
    await db.close_db()

    captured: dict[str, threading.Thread] = {}

    async def _boom(conn):
        captured["thread"] = conn._thread
        raise RuntimeError("migrate boom")

    monkeypatch.setattr(db, "_migrate", _boom)
    try:
        with pytest.raises(RuntimeError, match="^migrate boom$"):
            await db.init_db()
        assert db._conn is None
        assert db._conn_path is None
        assert captured["thread"].daemon is False
        assert await _wait_thread_stopped(captured["thread"])
    finally:
        await db.close_db()
