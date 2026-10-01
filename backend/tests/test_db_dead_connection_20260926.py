"""连接生命周期 · 「已关闭连接被当活连接复用」回归测试（2026-09-26）

背景（全量套件长期红着的 8 个用例的根因）：
  test_perf_content_pipeline / test_reasoning_effort_passthrough 单独跑 100% 通过，
  只有在全量套件里失败，报 `ValueError: no active connection`。

根因不在用例，而在 `app/db.py` 的连接缓存/池：
  - aiosqlite 的 `Connection._conn` 是 **property**，连接关闭后访问它直接抛
    `ValueError("no active connection")`；
  - `_probe_conn` / `_read_probe` 旧实现只把 `disk I/O error` 判为坏连接，
    **已关闭连接**抛的异常落在「非 IO 类异常 → 视为可用」分支里被放行；
  - `get_conn` 只认自定义 `_poisoned` 标记，且 30s 健康检查 TTL 会让死连接
    在窗口内被原样发出。
  于是「池/全局缓存里躺着已关闭的连接」被当成好连接发给业务代码，错误在
  业务深处才炸，且信息完全指不到根因（典型「日志不准确 / 排障方向被带偏」）。

本文件锁定三件事：死连接可被识别、探针不再误放行、连接池不再回流死连接。
"""
import asyncio

import aiosqlite
import pytest

import app.db as db


class TestDeadConnDetection:
    def test_open_conn_is_not_dead(self):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            try:
                assert db._is_dead_conn(c) is False
            finally:
                await c.close()
        asyncio.run(_t())

    def test_closed_conn_is_dead(self):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            assert db._is_dead_conn(c) is True
        asyncio.run(_t())

    def test_none_is_dead(self):
        assert db._is_dead_conn(None) is True

    def test_detection_never_touches_conn_property(self):
        """死连接判定必须只读不抛 —— 碰 `._conn` 就会抛 "no active connection"。"""
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            with pytest.raises(ValueError):
                _ = c._conn            # 反例基准：property 会抛
            assert db._is_dead_conn(c) is True   # 本函数不抛
        asyncio.run(_t())

    @pytest.mark.parametrize("msg", [
        "no active connection", "Connection closed", "CONNECTION CLOSED",
    ])
    def test_closed_error_wording(self, msg):
        assert db._is_closed_conn_error(ValueError(msg)) is True

    def test_unrelated_error_is_not_closed(self):
        assert db._is_closed_conn_error(ValueError("database is locked")) is False


class TestProbesRejectDeadConn:
    def test_write_probe_rejects_closed(self):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            assert await db._probe_conn(c) is False
        asyncio.run(_t())

    def test_read_probe_rejects_closed(self):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            assert await db._read_probe(c) is False
        asyncio.run(_t())

    def test_write_probe_accepts_open(self):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            try:
                assert await db._probe_conn(c) is True
            finally:
                await c.close()
        asyncio.run(_t())


class TestGlobalConnRebuiltWhenDead:
    @pytest.mark.asyncio
    async def test_get_conn_rebuilds_closed_global(self, tmp_path, monkeypatch):
        """全局连接被外部关闭后，get_conn 必须重建（不把死连接发出去）。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
        first = await db.get_conn()
        await first.close()                       # 模拟外部/异常路径关闭
        # 健康检查 TTL 尚未到期（30s），旧实现在此窗口内直接返回死连接
        assert db._last_health_check > 0
        second = await db.get_conn()
        assert second is not first
        assert db._is_dead_conn(second) is False
        await second.execute("CREATE TABLE IF NOT EXISTS t(x)")
        await second.close()
        db._conn = None
        db._conn_path = None

    @pytest.mark.asyncio
    async def test_close_db_is_idempotent(self, tmp_path, monkeypatch):
        """close_db 重复调用不得抛（否则残留池连接永远不被清理）。"""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t2.db")
        conn = await db.get_conn()
        await conn.close()            # 先把它关掉，制造「已关闭的全局连接」
        await db.close_db()            # 不应抛
        await db.close_db()
        assert db._conn is None


class TestPoolNeverKeepsDeadConn:
    @pytest.mark.asyncio
    async def test_release_closes_dead_conn_instead_of_pooling(self, monkeypatch):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            db._db_pool.clear()
            db._db_pool_total = 0
            await db._release_pool_conn(c)
            assert db._db_pool == [], "死连接不得回到写池"
        await _t()

    @pytest.mark.asyncio
    async def test_release_read_closes_dead_conn(self, monkeypatch):
        async def _t():
            c = await aiosqlite.connect(":memory:")
            await c.close()
            db._read_pool.clear()
            db._read_pool_total = 0
            await db._release_read_conn(c)
            assert db._read_pool == [], "死连接不得回到读池"
        await _t()
