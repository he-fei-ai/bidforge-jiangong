# -*- coding: utf-8 -*-
"""AI 配置模块 · R179（2026-11-05）深度审计修复回归测试。

覆盖本轮新增/修复的 6 类问题：

1. ``save_ai_config`` 不消费 ``enabled`` —— API 传 ``enabled=false`` 被静默忽略；
   停用「当前使用」配置必须显式失败而不是静默丢弃。
2. ``resolve_scene_config`` 不校验 ``enabled`` —— 停用配置仍被场景路由拉进真实调用。
3. 「激活」与「启用」不一致 —— toggle 把停用配置设为当前使用后仍是 disabled。
4. 导入导出 ``enabled`` 不保真 —— 停用配置迁移后被静默启用（含 overwrite 路径）。
5. 配置体检把「用户停用」配置报成可用 —— ``usable`` / summary 口径不一致。
6. 回滚可能制造「当前使用 + 已停用」矛盾；运行时 ``effective_provider_count``
   把仅有停用配置的厂商算作有效。

硬约束：仅本文件 + 相关既有文件做限定回归，不跑全量 pytest。
"""

import app.services.ai.provider_factory as pf
import pytest
from app.models import (
    AIConfigIn,
    ConfigEnabledIn,
    ConfigImportIn,
    ConfigRollbackIn,
    SceneRouteUpdate,
)
from app.routers.ai_config import audit as audit_router
from app.routers.ai_config import config as config_router
from app.routers.ai_config import connectivity as connectivity_router
from app.routers.ai_config import runtime as runtime_router
from app.routers.ai_config import scene_routes as scene_router
from app.services.crypto import encrypt_api_key


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _insert(
    db,
    cid,
    provider="openai",
    model="gpt-4o",
    is_active=0,
    priority=0,
    key="sk-test",
    base_url="https://1.1.1.1/v1",
    enabled=1,
):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, enabled, priority, concurrency)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model, "", is_active, enabled, priority),
    )
    await db.commit()


async def _row(db, cid):
    cur = await db.execute("SELECT * FROM ai_config WHERE id=?", (cid,))
    return dict(await cur.fetchone())


def _config_payload(**over):
    data = {
        "provider_name": "deepseek",
        "plan": "pay_as_you_go",
        "api_key": "sk-abc",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout": 900,
        "concurrency": 4,
        "is_active": False,
    }
    data.update(over)
    return data


# ===========================================================================
# 1. 保存链路：enabled 显式生效，且不得停用当前使用中的配置
# ===========================================================================
class TestSaveEnabled:
    async def test_create_with_enabled_false_persists_disabled(self, db_conn):
        cid = await pf.save_ai_config(_config_payload(enabled=False))
        assert (await _row(db_conn, cid))["enabled"] == 0, "保存接口传入 enabled=false 必须落库，不得被静默忽略"

    async def test_create_without_enabled_defaults_to_enabled(self, db_conn):
        cid = await pf.save_ai_config(_config_payload())
        assert (await _row(db_conn, cid))["enabled"] == 1, "未提供 enabled 时保持原默认行为（默认启用）"

    async def test_update_without_enabled_keeps_existing_value(self, db_conn):
        cid = await pf.save_ai_config(_config_payload(enabled=False))
        await pf.save_ai_config(_config_payload(id=cid, model="deepseek-reasoner"))
        row = await _row(db_conn, cid)
        assert row["enabled"] == 0, "未提供 enabled 的更新不得改写已有停用状态"
        assert row["model"] == "deepseek-reasoner"

    async def test_update_with_enabled_false_persists(self, db_conn):
        cid = await pf.save_ai_config(_config_payload())
        await pf.save_ai_config(_config_payload(id=cid, enabled=False))
        assert (await _row(db_conn, cid))["enabled"] == 0, "更新路径同样必须消费 enabled"

    async def test_active_config_cannot_be_disabled_via_save(self, db_conn):
        with pytest.raises(ValueError) as ei:
            await pf.save_ai_config(_config_payload(is_active=True, enabled=False))
        assert "当前使用" in str(ei.value)

    async def test_route_layer_surfaces_400_for_active_disable(self, db_conn):
        with pytest.raises(Exception) as ei:
            await config_router.save_config(
                AIConfigIn(**_config_payload(is_active=True, enabled=False)), request=None, db=db_conn
            )
        assert getattr(ei.value, "status_code", None) == 400, "非法停用应以 400 明确拒绝，而不是静默失效"

    async def test_save_takes_effect_immediately_for_active_config(self, db_conn):
        """端到端「保存 → 热生效」：保存后下一次取当前配置必须看到新模型，不得命中旧缓存。"""
        cid = await pf.save_ai_config(_config_payload(is_active=True))
        first = await pf._load_active_config()
        assert first is not None and first["model"] == "deepseek-chat"

        await pf.save_ai_config(_config_payload(id=cid, model="deepseek-reasoner", is_active=True))
        second = await pf._load_active_config()
        assert second is not None and second["model"] == "deepseek-reasoner", "保存后必须立即失效配置缓存"


# ===========================================================================
# 2. 场景路由：停用配置不得被拉进真实调用
# ===========================================================================
class TestSceneRouteEnabled:
    async def test_disabled_route_target_falls_back_to_primary(self, db_conn):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1)
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)
        scene = next(iter(pf.KNOWN_SCENES))

        await scene_router.update_scene_route(
            SceneRouteUpdate(scene=scene, config_id="c-off"), request=None, db=db_conn
        )
        pf.invalidate_config_cache()
        cfg = await pf.resolve_scene_config(scene)
        assert cfg is None, "停用配置即使被设为场景路由目标，也必须回落主配置而不是继续被调用"

    async def test_enabled_route_target_still_applies(self, db_conn):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1)
        await _insert(db_conn, "c-on", provider="deepseek", model="deepseek-chat", enabled=1)
        scene = next(iter(pf.KNOWN_SCENES))

        await scene_router.update_scene_route(SceneRouteUpdate(scene=scene, config_id="c-on"), request=None, db=db_conn)
        pf.invalidate_config_cache()
        cfg = await pf.resolve_scene_config(scene)
        assert cfg is not None and cfg.get("id") == "c-on", "启用配置的场景路由行为不得回退"

    async def test_saving_route_to_disabled_config_returns_visible_warning(self, db_conn):
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)
        scene = next(iter(pf.KNOWN_SCENES))
        res = await scene_router.update_scene_route(
            SceneRouteUpdate(scene=scene, config_id="c-off"), request=None, db=db_conn
        )
        assert "停用" in (res.get("warning") or ""), "把路由指向停用配置时必须在保存响应里明确告知，不得静默"


# ===========================================================================
# 3. 激活即启用：不得产生「当前使用 + 已停用」矛盾
# ===========================================================================
class TestToggleActivatesEnabled:
    async def test_toggle_disabled_config_enables_it(self, db_conn):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1)
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)

        res = await config_router.toggle_config("c-off", request=None, db=db_conn)
        row = await _row(db_conn, "c-off")
        assert res["ok"] is True
        assert row["is_active"] == 1 and row["enabled"] == 1, "激活停用配置时必须同时恢复 enabled，避免矛盾状态"


# ===========================================================================
# 4. 导入导出保真：enabled 随配置迁移
# ===========================================================================
class TestImportEnabledFidelity:
    _item = {
        "provider_name": "deepseek",
        "plan": "pay_as_you_go",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "enabled": 0,
    }

    async def test_import_preserves_disabled_state(self, db_conn):
        res = await config_router.import_config(ConfigImportIn(items=[dict(self._item)]), request=None, db=db_conn)
        assert res.get("ok") is True
        cur = await db_conn.execute("SELECT enabled FROM ai_config WHERE provider_name='deepseek'")
        assert (await cur.fetchone())[0] == 0, "导出含 enabled=0 的配置导入后必须仍是停用状态"

    async def test_import_without_enabled_key_defaults_to_enabled(self, db_conn):
        item = {k: v for k, v in self._item.items() if k != "enabled"}
        await config_router.import_config(ConfigImportIn(items=[item]), request=None, db=db_conn)
        cur = await db_conn.execute("SELECT enabled FROM ai_config WHERE provider_name='deepseek'")
        assert (await cur.fetchone())[0] == 1, "旧版导出文件（无 enabled 键）保持默认启用，向后兼容"

    async def test_overwrite_preserves_imported_enabled_state(self, db_conn):
        await _insert(
            db_conn,
            "existing",
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            enabled=1,
        )
        await config_router.import_config(
            ConfigImportIn(items=[dict(self._item)], overwrite=True), request=None, db=db_conn
        )
        cur = await db_conn.execute("SELECT enabled FROM ai_config WHERE id='existing'")
        assert (await cur.fetchone())[0] == 0, "overwrite 路径也必须应用文件中的 enabled"

    async def test_dry_run_reports_enabled(self, db_conn):
        res = await config_router.import_config_dry_run(ConfigImportIn(items=[dict(self._item)]), db=db_conn)
        assert res["planned"][0]["enabled"] == 0, "预演必须回显导入后的启用状态，避免用户盲确认"

    async def test_set_first_active_forces_enabled(self, db_conn):
        res = await config_router.import_config(
            ConfigImportIn(items=[dict(self._item)], set_first_active=True), request=None, db=db_conn
        )
        assert res.get("ok") is True
        cur = await db_conn.execute("SELECT is_active, enabled FROM ai_config WHERE provider_name='deepseek'")
        row = await cur.fetchone()
        assert row[0] == 1 and row[1] == 1, "设为当前使用的配置必须同时启用，不得出现「当前使用 + 停用」"


# ===========================================================================
# 5. 体检口径：用户停用不计入可用，且可见
# ===========================================================================
class TestHealthEnabledVisibility:
    async def test_disabled_config_not_reported_usable(self, db_conn):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1)
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)

        res = await connectivity_router.configs_health(db=db_conn)
        items = {i["id"]: i for i in res["items"]}
        assert items["c-off"]["enabled"] is False
        assert items["c-off"]["disabled_by_user"] is True
        assert items["c-off"]["usable"] is False, "停用配置不得被体检报成「可用」"
        assert items["primary"]["usable"] is True
        assert res["summary"]["disabled_by_user"] == 1
        assert res["summary"]["usable"] == 1

    async def test_precheck_all_flags_disabled(self, db_conn, monkeypatch):
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)

        async def _ok(_url):
            return {"ok": True, "error": ""}

        monkeypatch.setattr(connectivity_router, "_dns_precheck_async", _ok)
        res = await connectivity_router.precheck_all(db=db_conn)
        assert res["items"][0]["enabled"] is False
        assert res["disabled_by_user"] == 1


# ===========================================================================
# 6. 回滚与运行时统计
# ===========================================================================
class TestRollbackAndRuntime:
    async def test_rollback_does_not_create_active_disabled(self, db_conn):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1)
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)

        await config_router.toggle_config("c-off", request=None, db=db_conn)
        cur = await db_conn.execute(
            "SELECT id FROM ai_config_audit_logs WHERE config_id='c-off' ORDER BY rowid DESC LIMIT 1"
        )
        audit_id = (await cur.fetchone())[0]

        res = await audit_router.rollback_config("c-off", ConfigRollbackIn(audit_id=audit_id), request=None, db=db_conn)
        row = await _row(db_conn, "c-off")
        assert row["is_active"] == 1 and row["enabled"] == 1
        assert "已保留启用状态" in (res.get("warning") or ""), "回滚拒绝写入矛盾状态时必须明确告知原因"

    async def test_runtime_effective_count_ignores_user_disabled_only_provider(self, db_conn):
        await _insert(db_conn, "c-off", provider="deepseek", model="deepseek-chat", enabled=0)
        res = await runtime_router.get_runtime(db=db_conn)
        assert "deepseek" in res["configured_providers"]
        assert res["enabled_providers"] == []
        assert res["effective_provider_count"] == 0, "仅有停用配置的厂商不算有效厂商"
