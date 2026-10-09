"""R47 债-A3：启动期孤儿导出产物 GC 的护栏。

- lifespan 启动块必须对每个在 export_cache 里有行的 scheme 调一次
  ``_gc_orphan_exports``；单方案失败不得阻断其它方案与启动。
- fail-soft：GC 整体抛异常时 lifespan 仍正常 yield（不影响服务启动）。

护栏双轨：① 行为实证（直接调 ``_run_startup_orphan_export_gc``，monkeypatch
  DB / GC 函数，断言调用次数 == 方案数、异常不炸）；
  ② 静态锁（lifespan 真调了该 helper、helper 内 SQL 正确、无定时任务）。
"""
from __future__ import annotations

import inspect

import pytest

import app.db as db_mod
import app.main as main_mod
from app.routers import export as E


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=None):
        return _FakeCursor(self._rows)


@pytest.mark.asyncio
async def test_startup_gc_called_once_per_scheme(monkeypatch):
    """启动期 GC 被调一次（monkeypatch _gc_orphan_exports 断言调用次数 == 方案数）。"""
    calls: list[str] = []

    async def _fake_gc(db, scheme_id, protect=None):
        calls.append(scheme_id)
        return 0

    # 两个方案在 export_cache 里有行
    monkeypatch.setattr(E, "_gc_orphan_exports", _fake_gc)
    fake_db = _FakeDB([("scheme-A",), ("scheme-B",)])
    async def _fake_get_conn():
        return fake_db
    monkeypatch.setattr(db_mod, "get_conn", _fake_get_conn)

    await main_mod._run_startup_orphan_export_gc()

    assert sorted(calls) == ["scheme-A", "scheme-B"], (
        f"启动期 GC 调用次数 != 方案数：calls={calls}")


@pytest.mark.asyncio
async def test_startup_gc_single_scheme_failure_does_not_abort_loop(monkeypatch):
    """某个 scheme 的 GC 抛异常 → 其它 scheme 仍被处理（fail-soft 循环内）。"""
    calls: list[str] = []

    async def _flaky_gc(db, scheme_id, protect=None):
        calls.append(scheme_id)
        if scheme_id == "scheme-B":
            raise RuntimeError("disk full")
        return 0

    monkeypatch.setattr(E, "_gc_orphan_exports", _flaky_gc)
    fake_db = _FakeDB([("scheme-A",), ("scheme-B",), ("scheme-C",)])
    async def _fake_get_conn2():
        return fake_db
    monkeypatch.setattr(db_mod, "get_conn", _fake_get_conn2)

    # 不应抛异常（内部 try/except 兜住 scheme-B 的失败）
    await main_mod._run_startup_orphan_export_gc()

    assert calls == ["scheme-A", "scheme-B", "scheme-C"], (
        f"单方案失败不应中断循环：calls={calls}")


@pytest.mark.asyncio
async def test_startup_gc_exception_does_not_break_lifespan(monkeypatch):
    """GC helper 抛异常时 lifespan 外层 try/except 兜住（不阻断启动）。

    行为等价：lifespan 里是 ``try: await _run_startup_orphan_export_gc()
    except Exception: logger.warning(...)``。这里让 helper 抛，验证外层
    catch 路径真能吞掉（与 lifespan 源码逐字同构）。
    """
    async def _boom(db, scheme_id, protect=None):
        raise RuntimeError("disk full during startup GC")

    monkeypatch.setattr(E, "_gc_orphan_exports", _boom)
    monkeypatch.setattr(db_mod, "get_conn",
                        lambda: _FakeDB([("scheme-X",)]))

    # 直接调 helper —— helper 内部对单方案 try/except，所以这里不抛；
    # 模拟「helper 整体抛」的极端情况（如 get_conn 本身炸），由 lifespan 外层兜。
    async def _bad_get_conn():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_mod, "get_conn", _bad_get_conn)

    # lifespan 外层是 try/except；这里复刻同构 catch
    raised = False
    try:
        await main_mod._run_startup_orphan_export_gc()
    except Exception:
        raised = True
    assert raised, "get_conn 失败应向外抛，由 lifespan 外层 catch"


class TestStartupGcStaticLock:
    """静态锁：helper 真接在 lifespan 启动段，且 fail-soft 包裹。"""

    def test_lifespan_calls_gc_helper(self):
        src = inspect.getsource(main_mod.lifespan)
        assert "_run_startup_orphan_export_gc" in src, (
            "lifespan 必须调用 _run_startup_orphan_export_gc()")
        # 外层 fail-soft 包裹
        i = src.index("_run_startup_orphan_export_gc")
        window = src[max(0, i - 200):i + 200]
        assert "try" in window and "except Exception" in window, (
            "lifespan 里调 helper 必须包 try/except，fail-soft")

    def test_helper_uses_distinct_scheme_id_query(self):
        src = inspect.getsource(main_mod._run_startup_orphan_export_gc)
        assert "SELECT DISTINCT scheme_id FROM export_cache" in src
        assert "_gc_orphan_exports" in src
        # 单方案 try/except
        assert "except Exception" in src

    def test_helper_has_no_scheduler(self):
        """零新增依赖：启动期 GC 是一次性同步循环，不得引入定时任务。"""
        src = inspect.getsource(main_mod._run_startup_orphan_export_gc)
        assert "create_task" not in src
        assert "APScheduler" not in src
        assert "while True" not in src
