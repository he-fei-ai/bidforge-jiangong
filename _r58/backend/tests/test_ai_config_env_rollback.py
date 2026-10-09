"""AI 配置 · 多环境（G5）与配置版本回滚（G6）回归测试（2026-09-23）。

覆盖：
A. 多环境
   1. ``normalize_env`` 白名单归一（非法值必须报错而不是静默写空）
   2. 环境为空（默认）时**零过滤** —— 主配置与降级链的选取与引入前逐字节一致
   3. 环境非空时：主配置优先取该环境专用配置，取不到回落通用配置
   4. 环境非空时：降级链只纳入「通用 + 该环境」，不跨环境降级
   5. 场景路由指向其它环境的配置 → 视为未配置（回落主配置）
   6. ``PUT /ai/env`` 持久化 + 即时失效缓存；``GET /ai/env`` 回读
B. 配置版本 / 回滚
   7. 审计快照**绝不含密钥**（明文或密文）
   8. 结构化 diff 只列真正变化的字段
   9. 回滚可把配置恢复到某次变更之前；回滚本身也留痕
  10. **回滚不复活密钥、也不清掉现有密钥**（安全硬约束）
  11. 边界：create 记录不可回滚（400）、记录不存在（404）、不属于该配置（400）、
      快照里的越界数值仍被收敛（不让历史脏值重新落库）
"""
import contextlib
import json

import app.services.ai.provider_factory as pf
import pytest
from app.models import (
    ActiveEnvIn,
    AIConfigIn,
    ConfigRollbackIn,
    SceneRouteUpdate,
)
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key


@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _insert(db, cid, provider="openai", model="gpt-4o", is_active=0,
                  priority=0, key="sk-test", base_url="https://api.test.com/v1",
                  env=""):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model,
         env, is_active, priority))
    await db.commit()


def _payload(**over) -> AIConfigIn:
    data = {
        "provider_name": "deepseek", "plan": "pay_as_you_go",
        "api_key": "sk-abc", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "max_tokens": 8192, "temperature": 0.7,
        "timeout": 900, "concurrency": 4, "is_active": True, "env": "",
    }
    data.update(over)
    return AIConfigIn(**data)


async def _row(db, cid):
    cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (cid,))
    r = await cur.fetchone()
    return dict(r) if r else None


async def _audits(db, action=""):
    sql = "SELECT * FROM ai_config_audit_logs"
    params: tuple = ()
    if action:
        sql += " WHERE action=?"
        params = (action,)
    cur = await db.execute(sql + " ORDER BY created_at ASC, rowid ASC", params)
    return [dict(r) for r in await cur.fetchall()]


# ===========================================================================
# A. 多环境
# ===========================================================================
class TestNormalizeEnv:
    def test_empty_is_valid(self):
        assert pf.normalize_env("") == ""
        assert pf.normalize_env(None) == ""
        assert pf.normalize_env("   ") == ""

    def test_strips_and_accepts_common_labels(self):
        assert pf.normalize_env(" prod ") == "prod"
        assert pf.normalize_env("test_env-1") == "test_env-1"

    def test_rejects_illegal_chars(self):
        for bad in ("prod env", "prod/../x", "生产环境", "a;b"):
            with pytest.raises(ValueError):
                pf.normalize_env(bad)

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            pf.normalize_env("x" * 33)


class TestEnvFiltering:
    async def test_default_env_does_no_filtering(self, db_conn):
        """默认（未设置环境）时：主配置与降级链的选取口径与引入前完全一致。"""
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await _insert(db_conn, "p1", provider="p1prov", env="prod")
        await _insert(db_conn, "d1", provider="d1prov", env="dev")
        assert await pf.resolve_active_env() == ""

        cfg = await pf._load_active_config()
        assert cfg["id"] == "main"
        chain = await pf._fallback_chain()
        names = {c["provider_name"] for c in chain}
        assert "p1prov" in names and "d1prov" in names, "环境为空时不得过滤任何候选"

    async def test_env_specific_primary_wins(self, db_conn):
        await _insert(db_conn, "generic", provider="gprov", is_active=1)
        await _insert(db_conn, "prod", provider="pprov", env="prod")
        await _insert(db_conn, "dev", provider="dprov", env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        assert await pf.resolve_active_env() == "prod"

        # 无 prod 专用主配置 → 回落通用主配置（系统不会因为没有专用配置而不可用）
        cfg = await pf._load_active_config()
        assert cfg["id"] == "generic"

        # 把 prod 专用配置设为主配置 → 优先取它
        await db_conn.execute("UPDATE ai_config SET is_active=0")
        await db_conn.execute("UPDATE ai_config SET is_active=1 WHERE id='prod'")
        await db_conn.commit()
        pf.invalidate_config_cache()
        cfg = await pf._load_active_config()
        assert cfg["id"] == "prod"

    async def test_env_never_falls_back_to_other_environment(self, db_conn):
        """只有 dev 主配置时，prod 模式必须不可用，不能跨环境串用密钥和地址。"""
        await _insert(db_conn, "dev", provider="dprov", is_active=1, env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        assert await pf._load_active_config() is None

    async def test_health_uses_active_environment_config(self, db_conn):
        await _insert(db_conn, "dev", provider="devprov", is_active=1, env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        health = await ai_router.ai_health(db=db_conn)
        assert health["active_env"] == "prod"
        assert health["status"] == "not_configured"
        assert "id" not in health

    async def test_fallback_chain_excludes_other_envs(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await _insert(db_conn, "generic", provider="gprov")
        await _insert(db_conn, "prod", provider="pprov", env="prod")
        await _insert(db_conn, "dev", provider="dprov", env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")

        names = {c["provider_name"] for c in await pf._fallback_chain()}
        assert "gprov" in names, "通用配置在任何环境下都必须可用"
        assert "pprov" in names
        assert "dprov" not in names, "环境非空时不得跨环境降级"

    async def test_scene_route_skipped_for_other_env(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await _insert(db_conn, "dev", provider="dprov", env="dev")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id="dev"), db=db_conn)
        assert (await pf.resolve_scene_config("content_draft"))["id"] == "dev"

        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        assert await pf.resolve_scene_config("content_draft") is None, \
            "切到 prod 后，路由到 dev 的场景必须回落主配置"

        # 切回通用（空）→ 路由重新生效（行为可逆，无残留）
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "")
        assert (await pf.resolve_scene_config("content_draft"))["id"] == "dev"

    async def test_invalid_active_env_fails_closed(self, db_conn, monkeypatch):
        import sqlite3

        await db_conn.execute(
            "INSERT INTO ai_runtime_settings(key, value) VALUES(?,?)",
            (pf.RUNTIME_ACTIVE_ENV_KEY, "prod space"))
        await db_conn.commit()
        pf.invalidate_config_cache()
        with pytest.raises(ValueError, match="配置损坏"):
            await pf.resolve_active_env()

    async def test_runtime_env_overrides_settings_default(self, db_conn, monkeypatch):
        monkeypatch.setattr(pf.settings, "active_env", "test", raising=False)
        pf.invalidate_config_cache()
        assert await pf.resolve_active_env() == "test", "无运行时行时应回落 settings"
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        assert await pf.resolve_active_env() == "prod", "运行时行优先于 settings"


class TestEnvRoutes:
    async def test_get_env_lists_labels(self, db_conn):
        await _insert(db_conn, "a", env="prod")
        await _insert(db_conn, "b", env="dev")
        await _insert(db_conn, "c")
        res = await ai_router.get_active_env(db=db_conn)
        assert res["active_env"] == ""
        assert res["envs"] == ["dev", "prod"]
        assert res["routed"] is False

    async def test_put_env_persists_and_audits(self, db_conn):
        res = await ai_router.set_active_env(ActiveEnvIn(env=" prod "), db=db_conn)
        assert res["ok"] is True and res["active_env"] == "prod"
        assert (await ai_router.get_active_env(db=db_conn))["active_env"] == "prod"
        rows = await _audits(db_conn, "env")
        assert len(rows) == 1 and "prod" in rows[0]["detail"]

    async def test_put_env_rejects_illegal(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.set_active_env(ActiveEnvIn(env="bad env"), db=db_conn)

    async def test_put_env_empty_restores_legacy_behaviour(self, db_conn):
        await ai_router.set_active_env(ActiveEnvIn(env="prod"), db=db_conn)
        res = await ai_router.set_active_env(ActiveEnvIn(env=""), db=db_conn)
        assert res["active_env"] == ""
        assert (await ai_router.get_active_env(db=db_conn))["routed"] is False


class TestSaveConfigEnv:
    async def test_save_persists_env(self, db_conn):
        res = await ai_router.save_config(_payload(env="prod"), db=db_conn)
        assert (await _row(db_conn, res["id"]))["env"] == "prod"

    async def test_save_rejects_illegal_env(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.save_config(_payload(env="prod env"), db=db_conn)

    async def test_update_clears_env_when_empty(self, db_conn):
        await _insert(db_conn, "c1", env="prod")
        await ai_router.save_config(_payload(id="c1", env=""), db=db_conn)
        assert (await _row(db_conn, "c1"))["env"] == ""

    async def test_get_config_exposes_env_fields(self, db_conn):
        await _insert(db_conn, "c1", env="prod", is_active=1)
        res = await ai_router.get_config(db=db_conn)
        assert res["items"][0]["env"] == "prod"
        assert res["envs"] == ["prod"]
        assert "active_env" in res


# ===========================================================================
# B. 配置版本 / 回滚
# ===========================================================================
class TestAuditSnapshot:
    async def test_snapshot_never_contains_secret(self, db_conn):
        res = await ai_router.save_config(
            _payload(api_key="sk-super-secret-key"), db=db_conn)
        rows = await _audits(db_conn, "create")
        assert len(rows) == 1
        snap = rows[0]["snapshot_json"]
        assert "sk-super-secret-key" not in snap
        assert "gAAAA" not in snap, "密文同样不得进快照"
        assert '"has_key": 1' in snap
        # 解密后的密文列也不应在快照里
        row = await _row(db_conn, res["id"])
        assert row["api_key_encrypted"] not in snap

    async def test_long_snapshot_is_valid_json_and_remark_is_bounded(self, db_conn):
        """超长备注不得把 snapshot_json 截成非法 JSON。"""
        long_remark = "测" * 5000
        await ai_router.save_config(_payload(remark=long_remark), db=db_conn)
        rows = await _audits(db_conn, "create")
        snap = rows[0]["snapshot_json"]
        parsed = json.loads(snap)
        assert len(parsed["after"]["remark"]) == 2000
        assert parsed["after"]["remark"] == long_remark[:2000]

    async def test_changes_only_lists_real_diffs(self, db_conn):
        await _insert(db_conn, "c1", model="gpt-4o")
        await ai_router.save_config(
            _payload(id="c1", model="gpt-4o-mini", max_tokens=4096), db=db_conn)
        logs = await ai_router.config_audit_logs(db=db_conn)
        update = [i for i in logs["items"] if i["action"] == "update"][0]
        fields = {c["field"] for c in update["changes"]}
        # 真正变化的字段必须在列
        assert {"model", "max_tokens"} <= fields
        # 未变化的字段绝不能出现（否则 diff 只是把整行贴出来，没有信息量）
        assert not ({"temperature", "timeout", "concurrency",
                     "env", "remark"} & fields), "未变化的字段不应出现在 diff 中"
        model_change = [c for c in update["changes"] if c["field"] == "model"][0]
        assert model_change["before"] == "gpt-4o"
        assert model_change["after"] == "gpt-4o-mini"
        assert model_change["label"] == "模型"
        assert "snapshot_json" not in update, "原始快照不应回传前端"

    async def test_changes_empty_when_no_snapshot(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_config_audit_logs (id, action, detail, created_at)"
            " VALUES ('legacy','update','历史记录', datetime('now','localtime'))")
        await db_conn.commit()
        logs = await ai_router.config_audit_logs(db=db_conn)
        assert logs["items"][0]["changes"] == []

    async def test_clear_key_snapshot_reflects_key_change(self, db_conn):
        await _insert(db_conn, "c1", key="sk-x")
        await ai_router.clear_config_key("c1", db=db_conn)
        rows = await _audits(db_conn, "clear_key")
        assert '"has_key": 1' in rows[0]["snapshot_json"]
        assert '"has_key": 0' in rows[0]["snapshot_json"]


class TestRollback:
    async def _create_then_update(self, db):
        """建一条配置再改模型，返回（创建审计 id, 更新审计 id, 配置 id）。"""
        res = await ai_router.save_config(
            _payload(model="v1-model", max_tokens=8192), db=db)
        cid = res["id"]
        await ai_router.save_config(
            _payload(id=cid, model="v2-model", max_tokens=4096), db=db)
        creates = await _audits(db, "create")
        updates = await _audits(db, "update")
        return creates[0]["id"], updates[0]["id"], cid

    async def test_rollback_restores_previous_values(self, db_conn):
        _create_id, update_id, cid = await self._create_then_update(db_conn)
        assert (await _row(db_conn, cid))["model"] == "v2-model"

        res = await ai_router.rollback_config(
            cid, ConfigRollbackIn(audit_id=update_id), db=db_conn)
        assert res["ok"] is True
        row = await _row(db_conn, cid)
        assert row["model"] == "v1-model"
        assert row["max_tokens"] == 8192
        assert res["restored"]["model"] == "v1-model"
        assert any(c["field"] == "model" for c in res["changes"])

        # 回滚本身留痕，且可再次回滚回去
        rollbacks = await _audits(db_conn, "rollback")
        assert len(rollbacks) == 1 and update_id[:8] in rollbacks[0]["detail"]

    async def test_rollback_does_not_touch_api_key(self, db_conn):
        await _insert(db_conn, "c1", model="m1", key="sk-original")
        enc_before = (await _row(db_conn, "c1"))["api_key_encrypted"]
        await ai_router.save_config(
            _payload(id="c1", model="m2", api_key=""), db=db_conn)
        update_id = (await _audits(db_conn, "update"))[0]["id"]

        await ai_router.rollback_config(
            "c1", ConfigRollbackIn(audit_id=update_id), db=db_conn)
        assert (await _row(db_conn, "c1"))["api_key_encrypted"] == enc_before

    async def test_rollback_does_not_resurrect_cleared_key(self, db_conn):
        """安全硬约束：回滚字段变更**不得**把已清除的 Key 变回来。"""
        await _insert(db_conn, "c1", key="sk-original")
        await ai_router.clear_config_key("c1", db=db_conn)
        # 再用一次字段变更制造一条可回滚记录
        await ai_router.save_config(_payload(id="c1", model="m2", api_key=""), db=db_conn)
        update_id = (await _audits(db_conn, "update"))[0]["id"]

        await ai_router.rollback_config(
            "c1", ConfigRollbackIn(audit_id=update_id), db=db_conn)
        assert (await _row(db_conn, "c1"))["api_key_encrypted"] == ""

    async def test_rollback_clamps_out_of_range_snapshot(self, db_conn):
        """历史快照里的越界数值不得被原样写回（回滚同样走白名单归一）。"""
        await _insert(db_conn, "c1", key="sk-x")
        await db_conn.execute(
            "INSERT INTO ai_config_audit_logs"
            " (id, action, config_id, detail, snapshot_json, created_at)"
            " VALUES ('bad','update','c1','脏快照', ?, datetime('now','localtime'))",
            ('{"before": {"concurrency": 99, "max_tokens": 999999}, "after": {}}',))
        await db_conn.commit()

        res = await ai_router.rollback_config(
            "c1", ConfigRollbackIn(audit_id="bad"), db=db_conn)
        row = await _row(db_conn, "c1")
        assert row["concurrency"] == 5
        assert row["max_tokens"] == 200000
        assert res["restored"]["concurrency"] == 5

    async def test_rollback_create_record_rejected(self, db_conn):
        create_id, _update_id, cid = await self._create_then_update(db_conn)
        with pytest.raises(Exception):
            await ai_router.rollback_config(
                cid, ConfigRollbackIn(audit_id=create_id), db=db_conn)

    async def test_rollback_unknown_audit_404(self, db_conn):
        await _insert(db_conn, "c1")
        with pytest.raises(Exception):
            await ai_router.rollback_config(
                "c1", ConfigRollbackIn(audit_id="ghost"), db=db_conn)

    async def test_rollback_other_config_rejected(self, db_conn):
        _create_id, update_id, _cid = await self._create_then_update(db_conn)
        await _insert(db_conn, "other")
        with pytest.raises(Exception):
            await ai_router.rollback_config(
                "other", ConfigRollbackIn(audit_id=update_id), db=db_conn)

    async def test_rollback_missing_audit_id_400(self, db_conn):
        await _insert(db_conn, "c1")
        with pytest.raises(Exception):
            await ai_router.rollback_config(
                "c1", ConfigRollbackIn(audit_id="  "), db=db_conn)

    async def test_rollback_deleted_config_404(self, db_conn):
        await _insert(db_conn, "c1")
        await ai_router.clear_config_key("c1", db=db_conn)   # 产生一条带快照的审计
        ck_id = (await _audits(db_conn, "clear_key"))[0]["id"]
        await db_conn.execute("DELETE FROM ai_config WHERE id='c1'")
        await db_conn.commit()
        with pytest.raises(Exception):
            await ai_router.rollback_config(
                "c1", ConfigRollbackIn(audit_id=ck_id), db=db_conn)

    async def test_rollback_restores_env_and_is_env_aware(self, db_conn):
        await _insert(db_conn, "c1", env="dev")
        await ai_router.save_config(_payload(id="c1", env="prod"), db=db_conn)
        update_id = (await _audits(db_conn, "update"))[0]["id"]
        await ai_router.rollback_config(
            "c1", ConfigRollbackIn(audit_id=update_id), db=db_conn)
        assert (await _row(db_conn, "c1"))["env"] == "dev"


# ===========================================================================
# 「当前使用」标记的可选还原（include_active）
# ===========================================================================
class TestRollbackActiveFlag:
    async def test_default_does_not_touch_active_flag(self, db_conn):
        """默认行为：回滚字段不碰「当前使用」标记（主配置唯一性有系统级守卫）。"""
        await _insert(db_conn, "a", is_active=1)
        await _insert(db_conn, "b", is_active=0)
        await ai_router.toggle_config("b", db=db_conn)      # b 变为当前使用
        toggle_id = (await _audits(db_conn, "toggle"))[0]["id"]

        res = await ai_router.rollback_config(
            "b", ConfigRollbackIn(audit_id=toggle_id), db=db_conn)
        assert res["active_restored"] is None
        assert (await _row(db_conn, "b"))["is_active"] == 1, "未显式要求时不得改动主配置标记"
        assert "is_active" not in res["restored"]

    async def test_include_active_restores_previous_flag(self, db_conn):
        """显式 include_active=true：可把某条配置还原为「当前使用」。"""
        await _insert(db_conn, "a", provider="aprov", is_active=1)
        await _insert(db_conn, "b", provider="bprov", is_active=0)
        # 更新 a 时它还是「当前使用」→ 该记录的 before.is_active = 1
        await ai_router.save_config(
            _payload(id="a", provider_name="aprov", model="m2"), db=db_conn)
        update_id = (await _audits(db_conn, "update"))[0]["id"]
        await ai_router.toggle_config("b", db=db_conn)      # a 变为非当前使用
        assert (await _row(db_conn, "a"))["is_active"] == 0

        res = await ai_router.rollback_config(
            "a", ConfigRollbackIn(audit_id=update_id, include_active=True), db=db_conn)
        assert res["active_restored"] is True
        assert (await _row(db_conn, "a"))["is_active"] == 1
        assert (await _row(db_conn, "b"))["is_active"] == 0, "主配置必须全局唯一"
        assert "is_active" in res["restored"]
        # 真的改了标记 → 留痕必须写明
        rows = await _audits(db_conn, "rollback")
        assert rows and "当前使用" in rows[0]["detail"]

    async def test_include_active_keeps_flag_when_it_would_zero_out(self, db_conn):
        """守卫：还原为「关闭」会变成零主配置时，保留现状并明确告警。"""
        await _insert(db_conn, "a", is_active=0)
        await _insert(db_conn, "b", is_active=1)
        await ai_router.toggle_config("a", db=db_conn)      # a 变为当前使用（before.is_active=0）
        toggle_id = (await _audits(db_conn, "toggle"))[0]["id"]

        res = await ai_router.rollback_config(
            "a", ConfigRollbackIn(audit_id=toggle_id, include_active=True), db=db_conn)
        assert res["warning"], "会导致零主配置时必须告警"
        assert (await _row(db_conn, "a"))["is_active"] == 1, "不得把系统置为无主配置"
        assert res["active_restored"] is None

    async def test_rollback_audit_detail_reflects_actual_change(self, db_conn):
        """留痕要如实：只有**真的改了**标记才写「含当前使用标记」，避免虚假记录。"""
        # 场景一：请求 include_active 但标记本就一致 → 不得写「含当前使用标记」
        await _insert(db_conn, "a", provider="aprov", is_active=1)
        await ai_router.save_config(
            _payload(id="a", provider_name="aprov", model="m2"), db=db_conn)
        same_id = (await _audits(db_conn, "update"))[0]["id"]
        res = await ai_router.rollback_config(
            "a", ConfigRollbackIn(audit_id=same_id, include_active=True), db=db_conn)
        assert res["active_restored"] is None
        rows = await _audits(db_conn, "rollback")
        assert rows and "当前使用" not in rows[0]["detail"]
