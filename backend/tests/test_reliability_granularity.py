"""可靠性统计粒度升级回归（2026-09-24）。

目标：可靠性统计不再只按 ``provider_name`` 聚合，而是按
``config_id + provider_name + model + base_url`` 的配置身份隔离。

锁定：
1. 同一 provider 的两条不同配置，成败统计互不污染；
2. 无 config_id 时按 provider/model/base_url 组合键隔离；
3. ai_audit_logs 真实落库 config_id / base_url；
4. /ai/stats 按四元组聚合；
5. 旧库缺 config_id/base_url 列时仍能写入审计（降级不丢数据）。
"""
import uuid

import app.services.ai.provider_factory as pf
import pytest
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key


async def _insert(db, cid, provider="deepseek", model="deepseek-chat",
                  base_url="https://api.deepseek.com/v1", is_active=0):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,0,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key("sk-x"), base_url,
         model, is_active))
    await db.commit()


class TestReliabilityIdentity:
    def test_same_provider_different_config_is_isolated(self):
        key_a = pf._reliability_key("deepseek", "deepseek-chat",
                                    "config-a", "https://api.deepseek.com/v1")
        key_b = pf._reliability_key("deepseek", "deepseek-chat",
                                    "config-b", "https://api.deepseek.com/v1")
        assert key_a != key_b
        assert key_a == "cfg:config-a"

    def test_without_config_uses_provider_model_base_url(self):
        key = pf._reliability_key(
            "deepseek", "deepseek-chat", "", "https://api.deepseek.com/v1")
        assert key == "prov:deepseek|model:deepseek-chat|url:https://api.deepseek.com/v1"
        other = pf._reliability_key(
            "deepseek", "deepseek-reasoner", "", "https://api.deepseek.com/v1")
        assert key != other

    def test_snapshot_uses_composite_key(self):
        pf._provider_reliability.clear()
        key_a = pf._reliability_key("p", "m", "cfg-a", "")
        key_b = pf._reliability_key("p", "m", "cfg-b", "")
        pf._provider_reliability[key_a] = {"ok": pf._PROVIDER_DEAD_MIN_SAMPLES, "fail": 0}
        pf._provider_reliability[key_b] = {"ok": 0, "fail": pf._PROVIDER_DEAD_MIN_SAMPLES}
        assert pf._provider_success_rate("p", "m", "cfg-a", "") == 1.0
        assert pf._provider_success_rate("p", "m", "cfg-b", "") == 0.0
        assert pf._is_dead_provider("p", "m", "cfg-b", "") is True
        assert pf._is_dead_provider("p", "m", "cfg-a", "") is False
        pf._provider_reliability.clear()


class TestAuditGranularity:
    async def test_flush_persists_config_id_and_base_url(self, db_conn):
        pf._audit_buffer.clear()
        await pf._log_audit(
            "deepseek", "deepseek-chat", "chat", 1.0, True,
            prompt_tokens=10, completion_tokens=5, scene="content_draft",
            config_id="config-a", base_url="https://api.deepseek.com/v1")
        await pf.flush_audit_buffer()
        cur = await db_conn.execute(
            "SELECT config_id, base_url, scene FROM ai_audit_logs")
        row = await cur.fetchone()
        assert row["config_id"] == "config-a"
        assert row["base_url"] == "https://api.deepseek.com/v1"
        assert row["scene"] == "content_draft"

    async def test_stats_group_by_config_identity(self, db_conn):
        for cid, base_url, ok in (
            ("cfg-a", "https://api.a.com/v1", 1),
            ("cfg-a", "https://api.a.com/v1", 1),
            ("cfg-b", "https://api.b.com/v1", 0),
        ):
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
                " config_id, base_url, success) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "deepseek", "deepseek-chat", "chat",
                 cid, base_url, ok))
        await db_conn.commit()
        res = await ai_router.ai_stats(days=30, db=db_conn)
        rows = {(r["config_id"], r["base_url"]): r for r in res["by_provider"]}
        assert rows[("cfg-a", "https://api.a.com/v1")]["calls"] == 2
        assert rows[("cfg-a", "https://api.a.com/v1")]["success_rate"] == 100.0
        assert rows[("cfg-b", "https://api.b.com/v1")]["calls"] == 1
        assert rows[("cfg-b", "https://api.b.com/v1")]["success_rate"] == 0.0

    async def test_legacy_missing_columns_still_flush(self, monkeypatch):
        import aiosqlite

        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE ai_audit_logs (id TEXT, provider_name TEXT, model TEXT,"
            " action TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,"
            " cached_tokens INTEGER, duration REAL, success INTEGER, error TEXT,"
            " scene TEXT, created_at TEXT)")
        await conn.commit()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_write_tx_conn():
            yield conn

        monkeypatch.setattr(pf, "write_tx_conn", fake_write_tx_conn)
        pf._audit_buffer.clear()
        await pf._log_audit("deepseek", "m", "chat", 1.0, True,
                            config_id="cfg-a", base_url="https://api.a.com/v1")
        await pf.flush_audit_buffer()
        cur = await conn.execute("SELECT COUNT(*) FROM ai_audit_logs")
        assert (await cur.fetchone())[0] == 1, "旧库缺新列也必须降级写入"
        await conn.close()
