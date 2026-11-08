"""窗口一护栏（2026-11-08）：读池回退分支在「回退取锁」await 上被取消。

原语义：探活取消后的回退是 ``async with _read_pool_cv: _read_pool_total -= 1``。
条件变量取锁本身是一次可取消的等待 —— 取消恰好落在取锁瞬间时，递减与
``conn.close()`` 全部被跳过：配额漏回退（``_read_pool_total`` 永久 +1），
连接漏线程（工作线程没有收到停止信号）。而在外层再加一次补偿，又会与
「已经成功回退」的分支重复递减 —— 取消时这两种状态无法用计数区分。

新语义：递减放进无 await 的同步段（取消打不进来），是否该结算由
``_read_pool_inflight`` 这个显式在途标记判定（不在集合 ⇒ 已结算）→ 回退可重入，
重复补偿不会二次递减；取消依旧原样上抛，不吞。
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


async def _close_quietly(conn) -> None:
    try:
        await conn.close()
    except Exception:
        pass


async def _wait_no_worker_threads(timeout: float = 3.0) -> list:
    """有限等待 aiosqlite 工作线程退出（返回残留列表）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        residue = _live_aiosqlite_threads()
        if not residue:
            return []
        await asyncio.sleep(0.05)
    return _live_aiosqlite_threads()


async def test_read_pool_refund_lock_cancel_refunds_and_closes(tmp_path, monkeypatch):
    """取消落在回退取锁瞬间：配额必须回退、连接工作线程必须收到停止信号。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "read_refund.db")
    monkeypatch.setattr(db, "_read_pool_path", None)

    conn = await aiosqlite.connect(db.DB_PATH)
    await db._setup_connection(conn)
    await conn.execute("PRAGMA query_only=ON")
    # 模拟「已出池、尚未移交借出」的正常在途态
    monkeypatch.setattr(db, "_read_pool", [conn])
    monkeypatch.setattr(db, "_read_pool_total", 1)
    # raising=False：修复前的代码没有这个集合，红跑要能走到真实断言
    # （否则红只会红在 AttributeError，证明不了配额/线程漏）。
    monkeypatch.setattr(db, "_read_pool_inflight", {conn}, raising=False)

    probe_entered = asyncio.Event()

    async def _probe_stuck(_conn):
        probe_entered.set()
        await asyncio.sleep(3600)  # 探活挂住，等测试取消
        return True

    monkeypatch.setattr(db, "_read_probe", _probe_stuck)

    task = asyncio.create_task(db._acquire_read_conn())
    try:
        await asyncio.wait_for(probe_entered.wait(), timeout=3.0)
        # 测试侧先持锁：把回退分支逼到「取锁」这一步
        await db._read_pool_cv.acquire()
        try:
            task.cancel()  # 第一次取消：落在探活 await 上
            await asyncio.sleep(0.05)  # 让回退走到取锁（旧实现）或同步结算（新实现）
            task.cancel()  # 第二次取消：落在回退取锁 await 上（旧实现的致命点）
        finally:
            db._read_pool_cv.release()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3.0)

        # ① 配额回退（旧实现此处置为 1 → 红）
        assert db._read_pool_total == 0
        # ② 连接必须被关闭（工作线程收到停止信号）
        assert db._is_dead_conn(conn)
        # ③ 在途标记必须清空
        assert conn not in db._read_pool_inflight
        # ④ 没有残留 aiosqlite 工作线程
        assert await _wait_no_worker_threads() == []
        # ⑤ 守恒：总量 == 池内 + 在途借出
        assert db._read_pool_total == len(db._read_pool) + len(db._read_pool_borrowed)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        db._read_pool_inflight.discard(conn)
        await _close_quietly(conn)
        await db.close_db()
