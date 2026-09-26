# -*- coding: utf-8 -*-
"""调用次数优化 · 第 1 批回归锁（2026-09-22）

覆盖：
  O1  审计落库兜底：旧库缺 scene 列时降级写入（审计记录不丢）
  O12 一致性扫描缓存表：_migrate 在旧库上幂等补 scene 列 + 建 consistency_scan_cache
  O9  确定性错误不重试：402/403/404 等章节级重试循环直接放弃
"""
import aiosqlite
import pytest

import app.services.ai.provider_factory as pf
import app.routers.sse_handlers as sh
from app.db import _migrate

LEGACY_AUDIT_DDL = (
    "CREATE TABLE ai_audit_logs ("
    "id TEXT, provider_name TEXT, model TEXT, action TEXT,"
    " prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER,"
    " duration REAL, success INTEGER, created_at TEXT DEFAULT (datetime('now','localtime')),"
    " error TEXT DEFAULT '')")


class TestNonRetryableError:
    """O9：确定性错误分类（纯函数）。"""

    @pytest.mark.parametrize("msg", [
        'HTTP 402: {"error":{"message":"Insufficient Balance"}}',
        'HTTP 403: {"error":{"code":11200,"message":"no valid authorization"}}',
        "HTTP 404: {\"status\":404,\"title\":\"Not Found\"}",
        "HTTP 401: invalid api key",
        "You exceeded your current quota, please check your plan and billing details",
        'HTTP 404: {"error":{"code":"UnsupportedModel"}}',
    ])
    def test_deterministic_errors(self, msg):
        assert pf.is_non_retryable_error(RuntimeError(msg)) is True

    @pytest.mark.parametrize("msg", [
        "HTTP 429: rate limit",            # 限流可重试（退避后仍可能成功）
        "HTTP 500: boom",                  # 服务端瞬时错误
        "ReadTimeout",                      # 网络抖动
        "所有 AI 提供商调用失败：HTTP 429",
        "",
    ])
    def test_retryable_errors(self, msg):
        assert pf.is_non_retryable_error(RuntimeError(msg)) is False


class TestAuditFlushDegrade:
    """O1：审计落库降级（旧库缺 scene 列）。"""

    async def test_flush_on_legacy_db_does_not_drop_rows(self, monkeypatch):
        """旧库（10 列）也能写入：scene 丢弃，其余 10 列完整落库。

        ✅ P10 修复配套：审计批量落库已从共享 get_conn() 迁移到独立
           write_tx_conn()，本用例改为 patch write_tx_conn 返回遗留（10 列）内存库。
        """
        from contextlib import asynccontextmanager
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute(LEGACY_AUDIT_DDL)
        await conn.commit()

        @asynccontextmanager
        async def fake_write_tx_conn():
            try:
                yield conn
            except BaseException:
                # 与生产 write_tx_conn 保持一致：失败写必须整体回滚。
                await conn.rollback()
                raise

        monkeypatch.setattr(pf, "write_tx_conn", fake_write_tx_conn)
        pf._audit_buffer.clear()
        pf._audit_legacy_warned = False

        await pf._log_audit("agnes", "agnes-2.5-flash", "chat", 1.0, True,
                            prompt_tokens=100, completion_tokens=50,
                            error="", scene="content_draft")
        await pf.flush_audit_buffer()

        cur = await conn.execute(
            "SELECT provider_name, prompt_tokens, error FROM ai_audit_logs")
        rows = await cur.fetchall()
        assert len(rows) == 1, "审计记录不得因缺 scene 列而丢失"
        assert rows[0][0] == "agnes" and rows[0][1] == 100
        await conn.close()

    async def test_flush_on_migrated_db_writes_scene(self, db_conn, monkeypatch):
        """迁移后的库（11 列）正常写入 scene。"""
        await _insert_config(db_conn)
        fake = _FakeProvider()
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        await pf.chat_with_fallback([{"role": "user", "content": "hi"}],
                                    scene="outline_draft")
        await pf.flush_audit_buffer()
        cur = await db_conn.execute(
            "SELECT scene FROM ai_audit_logs WHERE action='chat'")
        assert (await cur.fetchone())[0] == "outline_draft"


class TestMigrateBackfill:
    """O12：_migrate 幂等补齐 scene 列 + consistency_scan_cache 表。"""

    async def test_migrate_backfills_scene_and_scan_cache(self):
        conn = await aiosqlite.connect(":memory:")
        await conn.execute(LEGACY_AUDIT_DDL)
        await conn.commit()
        await _migrate(conn)
        await conn.commit()
        cur = await conn.execute("PRAGMA table_info(ai_audit_logs)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "scene" in cols, "迁移后必须补上 scene 列"
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='consistency_scan_cache'")
        assert await cur.fetchone(), "迁移后必须建 consistency_scan_cache 表"
        await _migrate(conn)  # 幂等：再跑一次不报错
        await conn.close()


def _patch_outline_runtime(monkeypatch, fake_collect):
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)

    async def fake_aws(coro, push_stats):
        return await coro

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(sh, "_await_with_stats", fake_aws)
    monkeypatch.setattr(sh, "wait_resume", noop)
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)


class TestChapterRetryGate:
    """O9：目录子目录重试闸门（确定性错误直接失败，不重试）。"""

    async def test_402_not_retried(self, monkeypatch):
        calls = []

        async def fake_collect(*a, **k):
            calls.append(1)
            raise RuntimeError("HTTP 402: Insufficient Balance")

        _patch_outline_runtime(monkeypatch, fake_collect)
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        st, children = await sh._fetch_chapter_children(
            0, {"title": "t"}, sub_prompt=[{"role": "user", "content": "p"}],
            task_id="t", sem=None, validate_fn=None, timeout=1.0,
            push_stats=None)
        assert st == "failed" and children == []
        assert len(calls) == 1, "确定性错误不得发起重试"

    async def test_429_still_retried(self, monkeypatch):
        """429 限流不在确定性错误之列：保持既有「重试一次」行为。"""
        calls = []

        async def fake_collect(*a, **k):
            calls.append(1)
            raise RuntimeError("HTTP 429: rate limit")

        _patch_outline_runtime(monkeypatch, fake_collect)
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        st, _ = await sh._fetch_chapter_children(
            0, {"title": "t"}, sub_prompt=[{"role": "user", "content": "p"}],
            task_id="t", sem=None, validate_fn=None, timeout=1.0,
            push_stats=None)
        assert st == "failed"
        assert len(calls) == 2, "限流错误仍应重试一次（旧行为不变）"

    async def test_legacy_flag_restores_retry_on_quota_error(self, monkeypatch):
        """ai_retry_on_quota_error=True 时恢复旧行为（402 也重试）。"""
        calls = []

        async def fake_collect(*a, **k):
            calls.append(1)
            raise RuntimeError("HTTP 402: Insufficient Balance")

        _patch_outline_runtime(monkeypatch, fake_collect)
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", True)
        st, _ = await sh._fetch_chapter_children(
            0, {"title": "t"}, sub_prompt=[{"role": "user", "content": "p"}],
            task_id="t", sem=None, validate_fn=None, timeout=1.0,
            push_stats=None)
        assert st == "failed"
        assert len(calls) == 2


class _FakeProvider:
    name = "agnes"
    model = "agnes-2.5-flash"
    temperature = 0.7
    last_usage: dict = {}
    last_finish_reason = ""

    async def chat(self, messages, **kw):
        return "ok"


async def _insert_config(db):
    from app.services.crypto import encrypt_api_key
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, remark, timeout, concurrency, request_mode)"
        " VALUES ('c1','agnes','pay_as_you_go',?,?,?,?,0,'',60,4,'normal')",
        (encrypt_api_key("sk-test"), "https://api.test.com/v1",
         "agnes-2.5-flash", 1))
    await db.commit()
