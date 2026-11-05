"""R176：`db.py` 连接池探活移出条件变量锁的并发与计数安全锁定。

核心不变量：
  · 探活发生在 ``_db_pool_cv`` **锁外**（否则一次慢探活会串行化所有获取）；
  · 探活失败必须回锁 ``total -= 1`` 并 ``notify()``（不泄漏 / 不睡死）；
  · 探活期间连接已被调用方独占（出池后不存在第二条分发路径）。
"""

import asyncio
import time

import aiosqlite
import app.db as D
import pytest
import pytest_asyncio


@pytest_asyncio.fixture()
async def pool(tmp_path, monkeypatch):
    """把池状态换成独立临时库，测后关闭全部连接并恢复全局状态。

    aiosqlite 的连接线程是**非 daemon**：漏关任何一条都会让 pytest 进程
    结束后永久不退出（表现为"测试全过但命令超时"），故必须 await close。
    """
    monkeypatch.setattr(D, "DB_PATH", tmp_path / "pool.db")
    saved = (D._db_pool[:], D._db_pool_total, D._db_pool_path)
    # asyncio.Condition 绑定创建时的 event loop；pytest 每个用例一个 loop，
    # 必须重建，否则第二个用例会报 "bound to a different event loop"。
    saved_cv = D._db_pool_cv
    D._db_pool_cv = asyncio.Condition()
    D._db_pool.clear()
    D._db_pool_total = 0
    D._db_pool_path = None
    yield tmp_path / "pool.db"
    for c in D._db_pool:
        try:
            await c.close()
        except Exception:
            pass
    D._db_pool[:] = saved[0]
    D._db_pool_total = saved[1]
    D._db_pool_path = saved[2]
    D._db_pool_cv = saved_cv


async def _seed(n: int, path) -> None:
    for _ in range(n):
        D._db_pool.append(await aiosqlite.connect(path))
    D._db_pool_total = n
    D._db_pool_path = D.DB_PATH


@pytest.mark.asyncio
async def test_probe_runs_outside_lock(pool, monkeypatch):
    """A 探活阻塞时，B 仍能从池里取到另一条连接（不被单条件变量串行化）。"""
    await _seed(2, pool)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def _slow_probe(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release.wait()
        return True

    monkeypatch.setattr(D, "_probe_conn", _slow_probe)
    a_task = asyncio.create_task(D._acquire_pool_conn())
    await asyncio.wait_for(started.wait(), 1.0)

    # 若探活在锁内，B 会等 A 的探活结束（这里给 0.5s 判定窗口）
    b_conn = await asyncio.wait_for(D._acquire_pool_conn(), 0.5)
    assert b_conn is not None

    release.set()
    a_conn = await asyncio.wait_for(a_task, 1.0)
    await D._release_pool_conn(a_conn)
    await D._release_pool_conn(b_conn)


@pytest.mark.asyncio
async def test_probe_failure_returns_count_and_no_deadlock(pool, monkeypatch):
    """探活全失败：计数归零、总耗时远小于超时（无死锁），且不抛未处理异常。"""
    await _seed(D._POOL_MAX_SIZE, pool)

    async def _fail(_conn):
        return False

    async def _no_setup(_conn):
        return None

    monkeypatch.setattr(D, "_probe_conn", _fail)
    # 并发新建时 WAL/PRAGMA 会撞写锁（与本测试目的无关），仅验证计数与不死锁
    monkeypatch.setattr(D, "_setup_connection", _no_setup)

    async def _one():
        try:
            conn = await D._acquire_pool_conn()
        except RuntimeError:
            return "failed"
        await D._release_pool_conn(conn)
        return "ok"

    results = await asyncio.wait_for(asyncio.gather(*[_one() for _ in range(D._POOL_MAX_SIZE + 4)]), 8.0)
    # 探活恒失败 → 新建连接也会在 _PROBE_MAX_ATTEMPTS 内耗尽后抛 RuntimeError
    assert set(results) == {"failed"}
    assert D._db_pool_total == 0, f"计数泄漏: {D._db_pool_total}"
    assert len(D._db_pool) == 0


@pytest.mark.asyncio
async def test_probe_failure_notify_wakes_waiters(pool, monkeypatch):
    """探活失败释放容量后必须 notify：等待者不能永久睡死。"""
    await _seed(1, pool)
    D._db_pool_total = D._POOL_MAX_SIZE  # 容量占满，后来者只能等待
    calls = {"n": 0}

    async def _first_fail_then_ok(_conn):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(D, "_probe_conn", _first_fail_then_ok)

    async def _one():
        conn = await D._acquire_pool_conn()
        await D._release_pool_conn(conn)
        return True

    started = time.perf_counter()
    got = await asyncio.wait_for(asyncio.gather(*[_one() for _ in range(D._POOL_MAX_SIZE)]), 5.0)
    assert all(got), "有等待者未被唤醒"
    assert time.perf_counter() - started < 5.0
    assert D._db_pool_total <= D._POOL_MAX_SIZE, f"计数超上限: {D._db_pool_total}"


@pytest.mark.asyncio
async def test_read_pool_probe_failure_returns_count(pool, monkeypatch):
    """只读池同口径：探活失败回锁归还计数、不泄漏。"""
    saved = (D._read_pool[:], D._read_pool_total, D._read_pool_path)
    saved_cv = D._read_pool_cv
    D._read_pool_cv = asyncio.Condition()
    D._read_pool.clear()
    D._read_pool_total = 0
    D._read_pool_path = None
    try:
        for _ in range(D._READ_POOL_MAX_SIZE):
            D._read_pool.append(await aiosqlite.connect(pool))
        D._read_pool_total = D._READ_POOL_MAX_SIZE
        D._read_pool_path = D.DB_PATH

        async def _fail(_conn):
            return False

        async def _no_setup(_conn):
            return None

        monkeypatch.setattr(D, "_read_probe", _fail)
        monkeypatch.setattr(D, "_setup_connection", _no_setup)

        async def _one():
            try:
                conn = await D._acquire_read_conn()
            except RuntimeError:
                return "failed"
            await D._release_read_conn(conn)
            return "ok"

        results = await asyncio.wait_for(asyncio.gather(*[_one() for _ in range(D._READ_POOL_MAX_SIZE + 2)]), 8.0)
        assert "ok" not in results
        assert D._read_pool_total == 0
    finally:
        for c in D._read_pool:
            try:
                await c.close()
            except Exception:
                pass
        D._read_pool[:] = saved[0]
        D._read_pool_total = saved[1]
        D._read_pool_path = saved[2]
        D._read_pool_cv = saved_cv
