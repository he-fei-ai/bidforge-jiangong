"""测试 _retry_db_locked 重试辅助函数（2026-09-23 目录生成模块修复）"""
import asyncio
import sqlite3

import pytest
from app.routers.sse_handlers import _retry_db_locked


class TestRetryDbLocked:
    """_retry_db_locked：对 database is locked 等瞬态错误做有限重试。"""

    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        """无异常时直接返回结果，不重试。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return "ok"

        result = await _retry_db_locked(factory)
        assert result == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retry_on_locked(self):
        """database is locked 时重试，最终成功。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        result = await _retry_db_locked(factory, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert calls == 3

    @pytest.mark.asyncio
    async def test_retry_on_disk_io_error(self):
        """disk I/O error 也触发重试。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise sqlite3.OperationalError("disk I/O error")
            return "ok"

        result = await _retry_db_locked(factory, max_retries=2, base_delay=0.01)
        assert result == "ok"
        assert calls == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_other_error(self):
        """非瞬态错误（如语法错误）直接抛出，不重试。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("no such table: foo")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await _retry_db_locked(factory, max_retries=3, base_delay=0.01)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """重试次数耗尽后抛出最后一次异常。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await _retry_db_locked(factory, max_retries=2, base_delay=0.01)
        assert calls == 3  # 初始 1 次 + 2 次重试

    @pytest.mark.asyncio
    async def test_max_retries_zero(self):
        """max_retries=0 时不重试，直接抛出。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await _retry_db_locked(factory, max_retries=0, base_delay=0.01)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_non_sqlite_error_passthrough(self):
        """非 sqlite3 异常原样抛出。"""
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise ValueError("bad value")

        with pytest.raises(ValueError, match="bad value"):
            await _retry_db_locked(factory, max_retries=3, base_delay=0.01)
        assert calls == 1