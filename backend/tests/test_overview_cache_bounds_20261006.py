"""审核与预检 · 进程内幂等缓存有界化（2026-10-06）回归锁。

背景（遗留清单第 2 项）：
    ``_OVERVIEW_RECENT`` / ``_PREFLIGHT_RECENT`` 按 scheme_id 索引、每条存的是
    **整份 payload**（findings + dimensions + stats），TTL（120s）只保证「过期后
    不再命中」，**不回收内存**；而方案删除时无人清理条目 —— 长跑进程里按「用过的
    方案数」无界增长（``_OVERVIEW_LOCKS`` 是刻意不清理的，见其 docstring，本测试
    同时锁死「invalidate 绝不能顺手把锁一起删」）。

修法：
    1. ``RECENT_CACHE_MAX_ENTRIES = 128`` 上限 + ``_evict_recent_cache``
       （先清过期项 → 仍在的压到上限内，淘汰最旧保留最新）；
    2. ``_cache_put`` 作**唯一写入口**（读写语义零变化：未到上限零淘汰）；
    3. ``invalidate_overview_cache(scheme_id)`` 由 ``schemes.delete_scheme`` /
       ``projects.delete_project`` 在删除后调用，回收指向已删方案的死键。

本文件覆盖 12 条断言组，已做 5 项 A/B 反向验证（见文件末尾 A/B 说明）。
"""
from __future__ import annotations

import inspect
import time
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers import compliance as _compliance_mod
from app.routers import projects as _projects_mod
from app.routers import schemes as _schemes_mod
from app.routers.compliance import (
    _OVERVIEW_LOCKS,
    _OVERVIEW_RECENT,
    _PREFLIGHT_RECENT,
    OVERVIEW_CACHE_TTL,
    RECENT_CACHE_MAX_ENTRIES,
    _cache_put,
    _evict_recent_cache,
    _overview_lock,
    invalidate_overview_cache,
    readiness_overview,
)

# asyncio_mode = auto（pytest.ini）→ 异步用例无需标记；
# 模块级 pytestmark 会给同步用例刷 13 条 PytestWarning，故不设。


# ---------------------------------------------------------------------------
# 纯内存单元（不依赖 DB）
# ---------------------------------------------------------------------------

def _fill(store: dict, n: int) -> list[str]:
    """按写入顺序塞 n 条（时间戳单调递增，最旧在前）。"""
    keys = []
    for i in range(n):
        sid = f"scheme-{i:04d}"
        _cache_put(store, sid, (f"fp-{i}", time.monotonic(), {"i": i}))
        keys.append(sid)
    return keys


class TestCacheBounded:
    """有界性：上限、淘汰方向（保留最新）、过期回收。"""

    def setup_method(self):
        _OVERVIEW_RECENT.clear()
        _PREFLIGHT_RECENT.clear()

    def teardown_method(self):
        _OVERVIEW_RECENT.clear()
        _PREFLIGHT_RECENT.clear()

    def test_put_beyond_cap_evicts_oldest_keeps_newest(self):
        # 塞到上限之上 10 条 → 必须回落到上限，且淘汰的是**最旧**那批
        keys = _fill(_OVERVIEW_RECENT, RECENT_CACHE_MAX_ENTRIES + 10)
        assert len(_OVERVIEW_RECENT) == RECENT_CACHE_MAX_ENTRIES
        # 最旧的 10 条被淘汰
        for k in keys[:10]:
            assert k not in _OVERVIEW_RECENT
        # 最新的 118 条全在（含刚写入的那条）
        for k in keys[10:]:
            assert k in _OVERVIEW_RECENT
        # 保留下来的最新条目内容逐字未变（淘汰不改写 payload）
        newest = _OVERVIEW_RECENT[keys[-1]]
        assert newest[0] == f"fp-{RECENT_CACHE_MAX_ENTRIES + 9}"
        assert newest[2] == {"i": RECENT_CACHE_MAX_ENTRIES + 9}

    def test_within_cap_zero_eviction(self):
        # 恰好到上限一条不少（正常负载永不触碰淘汰分支）
        keys = _fill(_OVERVIEW_RECENT, RECENT_CACHE_MAX_ENTRIES)
        assert len(_OVERVIEW_RECENT) == RECENT_CACHE_MAX_ENTRIES
        assert all(k in _OVERVIEW_RECENT for k in keys)

    def test_expired_entries_pruned_on_put(self):
        # 手工放一条早已过期的（读侧 TTL 判定下它永远不可能命中 → 删除等价）
        _OVERVIEW_RECENT["dead"] = ("fp-dead", time.monotonic() - OVERVIEW_CACHE_TTL - 1.0,
                                    {"payload": "stale"})
        _cache_put(_OVERVIEW_RECENT, "live", ("fp-live", time.monotonic(), {"payload": "ok"}))
        assert "dead" not in _OVERVIEW_RECENT
        assert "live" in _OVERVIEW_RECENT

    def test_fresh_entries_not_pruned_by_ttl_sweep(self):
        # 反例：TTL 内的条目不能被回收（否则缓存等于白做）
        _cache_put(_OVERVIEW_RECENT, "fresh", ("fp", time.monotonic(), {"a": 1}))
        _evict_recent_cache(_OVERVIEW_RECENT)
        assert "fresh" in _OVERVIEW_RECENT

    def test_evict_is_safe_on_empty_store(self):
        _evict_recent_cache({})
        _evict_recent_cache(_OVERVIEW_RECENT)


class TestWritePathMustGoThroughCachePut:
    """静态锁：绕开 ``_cache_put`` 直接赋值 = 上限失效（A/B 反向验证项）。"""

    def test_both_caches_written_via_cache_put_only(self):
        src = inspect.getsource(_compliance_mod)
        # 旧写法（回归形态）：模块字典被直接赋值
        assert "_PREFLIGHT_RECENT[scheme_id] =" not in src, \
            "_PREFLIGHT_RECENT 必须经 _cache_put 写入（直赋值会绕过上限）"
        assert "_OVERVIEW_RECENT[scheme_id] =" not in src, \
            "_OVERVIEW_RECENT 必须经 _cache_put 写入（直赋值会绕过上限）"
        assert src.count("_cache_put(_PREFLIGHT_RECENT,") == 1
        assert src.count("_cache_put(_OVERVIEW_RECENT,") == 1

    def test_cache_put_calls_reclaim(self):
        src = inspect.getsource(_cache_put)
        assert "_evict_recent_cache(store)" in src

    def test_cap_constant_is_positive_int(self):
        assert isinstance(RECENT_CACHE_MAX_ENTRIES, int)
        assert RECENT_CACHE_MAX_ENTRIES >= 1
        # TTL 不变（回收语义不得靠缩短 TTL 实现 —— 那会改变命中行为）
        assert OVERVIEW_CACHE_TTL == 120.0


class TestInvalidateOnSchemeDelete:
    """方案删除后的死键回收 + 不得误伤锁。"""

    def setup_method(self):
        _OVERVIEW_RECENT.clear()
        _PREFLIGHT_RECENT.clear()

    def teardown_method(self):
        _OVERVIEW_RECENT.clear()
        _PREFLIGHT_RECENT.clear()

    def test_invalidate_by_scheme_targets_only_that_scheme(self):
        for sid in ("a", "b"):
            _cache_put(_OVERVIEW_RECENT, sid, ("fp", time.monotonic(), {"s": sid}))
            _cache_put(_PREFLIGHT_RECENT, sid, ("fp", time.monotonic(), {"s": sid}))
        assert invalidate_overview_cache("a") == 2
        assert "a" not in _OVERVIEW_RECENT and "a" not in _PREFLIGHT_RECENT
        assert "b" in _OVERVIEW_RECENT and "b" in _PREFLIGHT_RECENT

    def test_invalidate_unknown_scheme_returns_zero(self):
        assert invalidate_overview_cache("nope") == 0

    def test_invalidate_all_clears_both_stores(self):
        _fill(_OVERVIEW_RECENT, 3)
        _fill(_PREFLIGHT_RECENT, 3)
        assert invalidate_overview_cache(None) == 6
        assert not _OVERVIEW_RECENT and not _PREFLIGHT_RECENT

    def test_invalidate_never_touches_overview_locks(self):
        """承重：锁若在协程持有时被删 → 另一协程另取一把新锁 → 互斥失效。"""
        lock = _overview_lock("sid-x")
        lock2 = _overview_lock("sid-x")
        assert lock is lock2  # 同方案必须复用同一把锁
        invalidate_overview_cache("sid-x")
        assert _OVERVIEW_LOCKS.get("sid-x") is lock, "invalidate 不得清除锁"
        invalidate_overview_cache(None)
        assert _OVERVIEW_LOCKS.get("sid-x") is lock, "全量 invalidate 同样不得清锁"
        assert _overview_lock("sid-x") is lock

    def test_delete_scheme_source_calls_invalidate(self):
        # 静态：两条删除链路都必须接线（任一改回旧写法即失败）
        assert "invalidate_overview_cache(scheme_id)" in inspect.getsource(_schemes_mod)
        projects_src = inspect.getsource(_projects_mod)
        assert "from app.routers.compliance import invalidate_overview_cache" in projects_src
        assert "invalidate_overview_cache(sid)" in projects_src
        # 必须带 fail-soft（清理失败不得阻断删除）——两个端点各自独立 try
        assert inspect.getsource(_schemes_mod).count("invalidate_overview_cache(scheme_id)") == 1
        assert "except Exception" in projects_src


# ---------------------------------------------------------------------------
# 端到端（真实 DB）：命中语义零变化 + 删除即回收
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "overview-cache-bounds.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "深基坑支护专项方案", "目录已确认"))
    await db.commit()
    for store in (_OVERVIEW_RECENT, _PREFLIGHT_RECENT):
        store.pop(sid, None)
    yield db, pid, sid
    for store in (_OVERVIEW_RECENT, _PREFLIGHT_RECENT):
        store.pop(sid, None)
    _OVERVIEW_LOCKS.pop(sid, None)
    await close_db()


class TestEndToEnd:
    async def test_cache_hit_semantics_unchanged_by_cap(self, db_ctx):
        """上限不得改变命中语义：TTL 内第二次请求仍返回 cached=True。"""
        db, _pid, sid = db_ctx
        first = await readiness_overview(sid, db=db)
        assert first.get("cached") is False
        assert sid in _OVERVIEW_RECENT
        second = await readiness_overview(sid, db=db)
        assert second.get("cached") is True
        # payload 是上次那份（含 findings/score 等关键字段）
        assert second.get("score") == first.get("score")

    async def test_delete_scheme_reclaims_dead_keys(self, db_ctx):
        """端到端：删方案 → 两份缓存的死键被回收，且锁不被动。"""
        db, pid, sid = db_ctx
        await readiness_overview(sid, db=db)           # 灌入 _OVERVIEW_RECENT
        _cache_put(_PREFLIGHT_RECENT, sid, ("fp", time.monotonic(), {"x": 1}))
        lock = _overview_lock(sid)
        assert sid in _OVERVIEW_RECENT and sid in _PREFLIGHT_RECENT

        resp = await _schemes_mod.delete_scheme(pid, sid, db=db)
        assert resp["ok"] is True

        assert sid not in _OVERVIEW_RECENT, "删除方案后 /overview 缓存死键必须回收"
        assert sid not in _PREFLIGHT_RECENT, "删除方案后 /preflight 缓存死键必须回收"
        assert _OVERVIEW_LOCKS.get(sid) is lock, "删除不得误清除并发锁（可能正被持有）"

    async def test_delete_scheme_is_failsoft_when_cache_import_fails(self, db_ctx,
                                                                     monkeypatch):
        """缓存清理失败（如 import 异常）不得阻断删除本身（fail-soft）。"""
        db, pid, sid = db_ctx
        await readiness_overview(sid, db=db)
        import builtins

        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name == "app.routers.compliance":
                raise ImportError("模拟导入失败")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom)
        resp = await _schemes_mod.delete_scheme(pid, sid, db=db)
        monkeypatch.undo()
        assert resp["ok"] is True
        # 数据确实删掉了（删除不受影响）
        cur = await db.execute("SELECT id FROM schemes WHERE id=?", (sid,))
        assert await cur.fetchone() is None


# ---------------------------------------------------------------------------
# A/B 反向验证（执行记录，非用例）：
#   A1 _evict_recent_cache 的上限 while 循环条件改为超大数      → test_put_beyond_cap_* 失败
#   A2 删掉过期回收 for 循环                                    → test_expired_entries_pruned_on_put 失败
#   A3 invalidate_overview_cache 改成空实现                     → 4 例失败（含端到端删除）
#   A4 invalidate 顺手 _OVERVIEW_LOCKS.clear()                  → test_invalidate_never_touches_* 失败
#   A5 /preflight 写入点改回直接赋值                            → test_both_caches_written_via_* 失败
# 每项变异 try/finally 逐次还原 + sha256 字节一致 + 还原后复跑全绿。
# ---------------------------------------------------------------------------
