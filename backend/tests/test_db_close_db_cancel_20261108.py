"""窗口二护栏（2026-11-08）：``close_db()`` 在「等连接关闭」的 await 上被取消。

原语义：``close_db`` 逐条 ``await conn.close()``。取消落在其中任一次 await 时，
``except Exception`` 接不住 ``CancelledError``，**其后尚未处理的连接一条都不会关**；
aiosqlite 的 ``_connection_worker_thread`` 是非守护线程，每条漏关的连接都会把
进程退出拖住。

新语义：先把「停止哨兵」（``Connection.stop()``，除创建 future 外纯同步）发给
**每一条**连接，再进入可被取消的 await 段等线程退出。哨兵已入队 ⇒ 线程注定退出，
取消不再影响清理结果；``CancelledError`` 仍原样上抛。
"""

import asyncio
import threading

import aiosqlite
import app.db as db
import pytest

pytestmark = pytest.mark.asyncio


def _live_aiosqlite_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive() and "_connection_worker_thread" in t.name
    ]


async def _wait_no_worker_threads(timeout: float = 3.0) -> list:
    """有限等待 aiosqlite 工作线程退出（返回残留列表）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        residue = _live_aiosqlite_threads()
        if not residue:
            return []
        await asyncio.sleep(0.05)
    return _live_aiosqlite_threads()


class _HangingConn:
    """把取消精确压在「等待连接关闭」这一次 await 上（两种实现通用）。

    ``close()`` 永挂 —— 修复前的实现停在它上面；
    ``stop()`` 返回永不完成的 future —— 修复后的实现停在它上面。
    两条路径都保证测试的取消落在真实可取消的 await 点上，
    而不是靠时序碰运气。
    """

    def __init__(self):
        self._connection = object()
        self._running = True
        self.close_called = False
        self.stop_called = False

    async def close(self):
        self.close_called = True
        await asyncio.sleep(3600)

    def stop(self):
        self.stop_called = True
        return asyncio.get_running_loop().create_future()


async def test_close_db_cancelled_while_closing_conn_still_stops_all_threads(tmp_path, monkeypatch):
    """取消落在关闭 await 上：所有池连接的工作线程仍必须退出，取消照常上抛。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "close_cancel.db")

    w1 = await aiosqlite.connect(db.DB_PATH)
    await db._setup_connection(w1)
    w2 = await aiosqlite.connect(db.DB_PATH)
    await db._setup_connection(w2)
    r1 = await aiosqlite.connect(db.DB_PATH)
    await db._setup_connection(r1)
    await r1.execute("PRAGMA query_only=ON")

    # 排在最前面 = 两种实现都先在这里停住
    hanging = _HangingConn()
    monkeypatch.setattr(db, "_db_pool", [hanging, w1])
    monkeypatch.setattr(db, "_db_pool_borrowed", {w2})
    monkeypatch.setattr(db, "_db_pool_total", 3)
    monkeypatch.setattr(db, "_read_pool", [r1])
    monkeypatch.setattr(db, "_read_pool_total", 1)

    task = asyncio.create_task(db.close_db())
    try:
        await asyncio.sleep(0.05)  # 让 close_db 走到「等连接关闭」
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3.0)

        # 取消已发生，账目仍必须结清（否则读池/写池容量永久缩水）
        assert db._db_pool_total == 0
        assert db._read_pool_total == 0
        assert db._db_pool == []
        assert db._db_pool_borrowed == set()
        assert db._read_pool == []
        assert db._read_pool_borrowed == set()

        # 关键断言（修复前为红）：三条真实连接都收到了停止信号、线程已退出
        assert await _wait_no_worker_threads() == []
        for conn in (w1, w2, r1):
            assert db._is_dead_conn(conn)
        assert db._db_pool_total == len(db._db_pool) + len(db._db_pool_borrowed)
        assert db._read_pool_total == len(db._read_pool) + len(db._read_pool_borrowed)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for conn in (w1, w2, r1):
            try:
                await conn.close()
            except Exception:
                pass
        await db.close_db()


async def test_close_db_cancel_does_not_swallow_caller_cancel(tmp_path, monkeypatch):
    """关闭流程自身被取消时不得把 CancelledError 吞掉（巡检任务取消不算）。

    修复前的实现里 ``except asyncio.CancelledError: pass`` 包住 ``await probe``，
    外部取消 close_db 时也会被一并吞下 —— 关闭协程“假装成功返回”。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "close_cancel2.db")

    started = asyncio.Event()

    async def _slow_probe():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # 模拟真实巡检任务：收到取消后还要收尾一小段时间才退出，
            # 保证外部取消 close_db 时它确实还停在 ``await probe`` 上。
            await asyncio.sleep(0.3)
            raise

    probe = asyncio.create_task(_slow_probe())
    monkeypatch.setattr(db, "_probe_task", probe)
    await asyncio.wait_for(started.wait(), 1.0)

    task = asyncio.create_task(db.close_db())
    try:
        await asyncio.sleep(0.05)  # 让 close_db 停在 await probe
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3.0)
        assert db._probe_task is None
    finally:
        if not probe.done():
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
        db._probe_task = None
        await db.close_db()
