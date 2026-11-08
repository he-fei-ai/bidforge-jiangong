"""关闭收口护栏（2026-11-07）：任务终态必须先于 ``close_db()`` 写入。

锁死的反直觉点：
  关闭收尾之后，库里是 ``failed``，且 ``_conn is None`` —— 终态写入**不允许**
  依赖 ``close_db()`` 之后 ``get_conn()`` 静默重建连接来“补”出来。将来若有人
  顺手堵住“关闭后仍可新建连接”（P3），没有这条护栏，终态会静默退化成
  ``running``，而且很难联想到是那次改动干的。
"""

import ast
import asyncio
import io
import os
import sqlite3
import threading

import aiosqlite
import app.db as db
import pytest
from app.services.ai import task_registry as tr

_REGISTRY_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "services",
    "ai",
    "task_registry.py",
)
_DB_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "db.py",
)


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


class _BadExceptionTask:
    """假任务：cancelled() 为 False、exception() 抛错。

    真实 asyncio.Task 的 exception() 只会在「已取消」时抛 CancelledError，
    而该分支已被 ``if not t.cancelled()`` 挡掉 —— 走不到关闭收口的静默兜底，
    因此必须用假任务才能覆盖「取回异常自身失败」这条路径。
    """

    def __init__(self):
        self._loop = asyncio.get_running_loop()

    def done(self) -> bool:
        return False

    def get_loop(self):
        return self._loop

    def cancel(self):
        pass

    def cancelled(self) -> bool:
        return False

    def exception(self):
        raise RuntimeError("取回任务异常失败（模拟）")


@pytest.mark.asyncio
async def test_shutdown_inflight_counts_exception_retrieval_failure(tmp_path, monkeypatch):
    """关闭收口吞掉「取回异常」失败时必须计数：零错误与没发生要可区分。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "retrieval.db")
    tr._tasks.clear()
    tr._detached_tasks.clear()
    bad = _BadExceptionTask()
    tr._detached_tasks.add(bad)

    async def fake_wait(tasks, timeout=None):
        assert bad in tasks
        return {bad}, set()

    monkeypatch.setattr(asyncio, "wait", fake_wait)
    before = tr.get_shutdown_exception_retrieval_failures()
    try:
        finalized = await tr.shutdown_inflight()
        assert finalized == 0
        assert tr.get_shutdown_exception_retrieval_failures() == before + 1
    finally:
        tr._tasks.clear()
        tr._detached_tasks.clear()
        await db.close_db()


class TestShutdownSwallowGuard:
    """源码护栏：关闭收口吞异常必须带 noqa 且留降级计数。

    变异判据：去掉 ``# noqa: BLE001`` → 本用例红；去掉计数自增 → 本用例红。
    通用 ``test_silent_broad_except_guard`` 只抓「分支体纯空转」的宽异常，
    分支体有计数自增后它不再拦截「noqa 缺失」这一半，故需本专用护栏。
    """

    def test_noqa_and_counter_coexist(self):
        src = io.open(_REGISTRY_SRC, encoding="utf-8-sig").read()
        lines = src.splitlines()
        tree = ast.parse(src, filename=_REGISTRY_SRC)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "shutdown_inflight":
                target = node
                break
        assert target is not None, "task_registry.shutdown_inflight 不存在"

        handlers = [
            h
            for h in ast.walk(target)
            if isinstance(h, ast.ExceptHandler)
            and isinstance(h.type, ast.Name)
            and h.type.id == "Exception"
            and any(
                isinstance(s, ast.AugAssign)
                and isinstance(s.target, ast.Name)
                and s.target.id == "_shutdown_exception_retrieval_failures"
                for s in h.body
            )
        ]
        assert len(handlers) == 1, "shutdown_inflight 中未找到「except Exception + 失败计数自增」分支"
        header = lines[handlers[0].lineno - 1]
        assert "# noqa: BLE001" in header, f"关闭收口宽异常缺 noqa: BLE001 标注：{header!r}"


@pytest.mark.asyncio
async def test_acquire_pool_conn_cancel_rolls_back_reservation(tmp_path, monkeypatch):
    """窗口 1：预留配额后、连接入池前被取消 → 配额必须精确回退，取消必须上抛。

    `_db_pool_total` 是获取闸门（`total < _POOL_MAX_SIZE` 才允许新建），只增不减
    会在累计到上限后让获取路径永久 wait（P1）。`CancelledError` 继承
    `BaseException`，原先的 `except Exception` 捕获不到 → 配额永久 +1。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cancel_new.db")
    monkeypatch.setattr(db, "_db_pool_path", db.DB_PATH)
    db._db_pool.clear()
    db._db_pool_borrowed.clear()
    db._db_pool_total = 0

    started = asyncio.Event()
    calls = {"n": 0}

    async def _hanging_connect(*_a, **_k):
        calls["n"] += 1
        started.set()
        if calls["n"] == 1:
            await asyncio.sleep(3600)  # 模拟建连期间挂住（真实 IO 窗口）
        # ✅ 第二次进入立刻抛普通异常：若实现吞掉取消并回到重试循环，任务会在此
        #    结束而不是变成「取消免疫」任务 —— 后者会让 pytest 收尾 gather 永久
        #    挂死，把「吞取消」这个缺陷表现成超时而非可判定的红。
        raise RuntimeError("取消后仍继续重试建连（吞掉了取消）")

    monkeypatch.setattr(aiosqlite, "connect", _hanging_connect)
    task = asyncio.create_task(db._acquire_pool_conn())
    try:
        await asyncio.wait_for(started.wait(), 2.0)
        task.cancel()
        # 有界等待：若实现吞掉取消，任务会在建连上继续挂起且「取消免疫」——
        # 直接 await 会永久卡住，这里只用 3s 预算并断言任务必须已结束。
        done, _pending = await asyncio.wait({task}, timeout=3.0)
        assert done, "取消被吞：任务在 3s 内未结束（会让关闭/停止流程卡住）"
        assert task.cancelled(), "取消未原样上抛（被当成普通异常处理了）"
        assert db._db_pool_total == 0, "取消后写池配额未回退（获取闸门会被永久占满）"
        assert db._db_pool == []
        assert db._db_pool_borrowed == set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
        db._db_pool.clear()
        db._db_pool_borrowed.clear()
        db._db_pool_total = 0


@pytest.mark.asyncio
async def test_acquire_pool_conn_cancel_during_probe_rolls_back(tmp_path, monkeypatch):
    """窗口 2：从池里 pop 之后、探活期间被取消 → 连接必须 close 且配额归位。

    pop 出池时 `_db_pool_total` 不递减（该连接仍计入「已创建未关闭」），所以取消
    路径必须自己回退一次并关闭连接，否则连接泄漏 + 配额永久占用。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cancel_probe.db")
    # ⚠️ 必须与 DB_PATH **同一对象类型**（Path）：写成 str 会让 `_acquire_pool_conn`
    #    顶部的 `_db_pool_path != DB_PATH` 恒真 → 先把池排空再走「新建连接」分支，
    #    根本到不了「pop 后探活」这个窗口（这是本用例第一版的真实缺陷）。
    monkeypatch.setattr(db, "_db_pool_path", db.DB_PATH)
    db._db_pool.clear()
    db._db_pool_borrowed.clear()

    conn = await aiosqlite.connect(str(tmp_path / "cancel_probe.db"))
    db._db_pool.append(conn)
    db._db_pool_total = 1

    started = asyncio.Event()

    async def _hanging_probe(_conn):
        started.set()
        await asyncio.sleep(3600)
        return True

    monkeypatch.setattr(db, "_probe_conn", _hanging_probe)
    task = asyncio.create_task(db._acquire_pool_conn())
    try:
        await asyncio.wait_for(started.wait(), 2.0)
        assert db._db_pool == [], "前置条件：连接应已被 pop 出池"
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=3.0)
        assert done, "取消被吞：探活任务在 3s 内未结束"
        assert task.cancelled(), "取消未原样上抛"
        assert db._db_pool_total == 0, "探活取消后写池配额未回退"
        assert db._is_dead_conn(conn), "探活取消后连接未被关闭（连接泄漏）"
        assert conn not in db._db_pool_borrowed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
        try:
            await conn.close()
        except Exception:
            pass
        db._db_pool.clear()
        db._db_pool_borrowed.clear()
        db._db_pool_total = 0


class TestAcquireCancelSafetyGuard:
    """源码护栏：`_acquire_pool_conn` 的取消回退必须捕获 BaseException 且原样重抛。

    为什么不能只靠运行时用例：实现一旦吞掉取消，任务会变成「取消免疫」——
    在挂起的建连上无限重入，`asyncio` 收尾也无法结束它，用例表现为挂死而不是
    红。所以「没吞取消」这一半必须用静态护栏锁住。

    变异判据：`except BaseException` 改回 `except Exception` → 本用例红（找不到
    两个 BaseException 回退分支）；删掉分支末尾的 `raise` → 本用例红。
    """

    def _acquire_handlers(self):
        """只取「回退配额」的 BaseException 分支，排除嵌套的 close 清理分支。

        `except BaseException as close_err:` 也在 `_acquire_pool_conn` 内，但它
        只负责关闭连接、不动 `_db_pool_total`；按「分支体内是否有
        `_db_pool_total -= 1`」过滤，保证计数与语义一一对应（当前应为 2 个：
        窗口1 预留回退 / 窗口2 探活回退）。
        """
        src = io.open(_DB_SRC, encoding="utf-8-sig").read()
        tree = ast.parse(src, filename=_DB_SRC)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_acquire_pool_conn":
                target = node
                break
        assert target is not None, "app.db._acquire_pool_conn 不存在"
        handlers = []
        for h in ast.walk(target):
            if not (
                isinstance(h, ast.ExceptHandler)
                and isinstance(h.type, ast.Name)
                and h.type.id == "BaseException"
            ):
                continue
            mutates_total = any(
                isinstance(s, ast.AugAssign)
                and isinstance(s.target, ast.Name)
                and s.target.id == "_db_pool_total"
                for s in ast.walk(h)
            )
            if mutates_total:
                handlers.append(h)
        return handlers

    def test_cancel_rollbacks_catch_base_exception(self):
        handlers = self._acquire_handlers()
        assert len(handlers) == 2, (
            "取消安全回退分支缺失：应为 2 个 (窗口1 预留回退 / 窗口2 探活回退)，实际 %d 个 —— "
            "CancelledError 继承 BaseException，用 except Exception 接不住" % len(handlers)
        )

    def test_cancel_rollbacks_reraise(self):
        handlers = self._acquire_handlers()
        assert len(handlers) == 2, "前置条件不满足：先修好 BaseException 捕获"
        for h in handlers:
            assert isinstance(h.body[-1], ast.Raise), (
                "第 %d 行的取消回退分支末尾不是 raise —— 吞掉取消会让关闭/停止流程卡住（比泄漏更糟）"
                % h.lineno
            )



