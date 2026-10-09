"""AI 配置模块 · 缺口修复回归（2026-09-25）。

覆盖本轮闭环的 7 个缺口（每项都是「静默失效 / 数据链断裂」类问题）：

B1 场景路由跨环境被运行时静默跳过 → 列表必须回传 ``active_env`` /
   ``config_env`` / ``env_mismatch``，PUT 必须回传 ``warning``（且路由照常保存）
B2 删除配置后 ``ai_scene_routes`` 残留僵尸行 → 必须级联解除并回传条数
B3 清除被场景路由引用的配置 Key → 必须提示「相关场景将回落」
B4 审计明细缺 ``scene`` 筛选（stats.by_scene 有聚合却无法下钻）→ 补筛选 +
   ``scenes`` 可选值
B5 导入时 ``set_first_active`` 未重放并发 → 必须调用 ``apply_config_concurrency``
B6 运行时环境值损坏时展示端点 500（错误配置不可恢复）→ 展示端点降级回传
   ``env_error``，同时**运行时选模仍 fail-closed**
B7 ``scene`` 筛选缺前导列索引 → 补 ``idx_ai_audit_scene_created`` 并验证生效
"""
import contextlib

import app.routers.ai_config.config as cfg_module
import app.services.ai.provider_factory as pf
import pytest
from app.models import ActiveEnvIn, ConfigImportIn, SceneRouteUpdate
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
                  env="", concurrency=4):
    enc = encrypt_api_key(key) if key else ""
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, provider, "pay_as_you_go", enc, base_url, model, env, is_active,
         priority, concurrency))
    await db.commit()


async def _route(db, scene, config_id):
    await db.execute(
        "INSERT INTO ai_scene_routes (scene, config_id, updated_at) VALUES (?,?,?)",
        (scene, config_id, "2026-09-25T00:00:00"))
    await db.commit()


async def _route_rows(db):
    cur = await db.execute("SELECT scene, config_id FROM ai_scene_routes")
    return {dict(r)["scene"]: dict(r)["config_id"] for r in await cur.fetchall()}


async def _corrupt_env(db):
    """直接写入非法环境值（模拟外部改库）：运行时读环境必须 fail-closed。"""
    await db.execute(
        "INSERT INTO ai_runtime_settings(key, value) VALUES(?,?)",
        (pf.RUNTIME_ACTIVE_ENV_KEY, "prod space"))
    await db.commit()
    pf.invalidate_config_cache()


async def _seed_logs(db, rows):
    """rows: [(id, provider, action, scene, success), ...]"""
    for lid, prov, action, scene, ok in rows:
        await db.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, scene,"
            " prompt_tokens, completion_tokens, duration, success)"
            " VALUES (?,?, 'm', ?,?,10,5,1.0,?)",
            (lid, prov, action, scene, ok))
    await db.commit()


# ===========================================================================
# B1 场景路由跨环境可见性
# ===========================================================================
class TestSceneRouteEnvMismatch:
    async def test_list_reports_active_env_and_mismatch(self, db_conn):
        await _insert(db_conn, "dev", env="dev")
        await _route(db_conn, "content_draft", "dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        pf.invalidate_config_cache()

        res = await ai_router.list_scene_routes(db=db_conn)
        assert res["active_env"] == "prod"
        item = [i for i in res["items"] if i["scene"] == "content_draft"][0]
        assert item["config_env"] == "dev"
        assert item["env_mismatch"] is True, "跨环境路由必须可见（运行时会被跳过）"
        assert item["missing"] is False

    async def test_general_env_never_mismatches(self, db_conn):
        """通用环境（空串）= 零过滤：与引入多环境前口径一致，不得标红。"""
        await _insert(db_conn, "g", env="dev")
        await _route(db_conn, "content_draft", "g")
        res = await ai_router.list_scene_routes(db=db_conn)
        assert res["active_env"] == ""
        item = [i for i in res["items"] if i["scene"] == "content_draft"][0]
        assert item["env_mismatch"] is False

    async def test_same_env_and_unrouted_not_mismatched(self, db_conn):
        await _insert(db_conn, "p", env="prod")
        await _route(db_conn, "content_draft", "p")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        pf.invalidate_config_cache()
        res = await ai_router.list_scene_routes(db=db_conn)
        by = {i["scene"]: i for i in res["items"]}
        assert by["content_draft"]["env_mismatch"] is False
        # 未配置路由的场景既不算跨环境，也不该带出环境标签
        assert by["facts_extract"]["env_mismatch"] is False
        assert by["facts_extract"]["config_env"] == ""

    async def test_deleted_config_is_missing_not_mismatch(self, db_conn):
        """missing 与 env_mismatch 互斥：目标配置不存在时只报 missing。"""
        await _route(db_conn, "content_draft", "ghost")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        pf.invalidate_config_cache()
        item = [i for i in (await ai_router.list_scene_routes(
            db=db_conn))["items"] if i["scene"] == "content_draft"][0]
        assert item["missing"] is True
        assert item["env_mismatch"] is False

    async def test_put_warns_on_cross_env_but_still_saves(self, db_conn):
        await _insert(db_conn, "dev", env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        pf.invalidate_config_cache()

        res = await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id="dev"), db=db_conn)
        assert res["ok"] is True
        assert "暂不生效" in res["warning"], "跨环境必须在设置时就告知，不再只回 ok"
        assert await _route_rows(db_conn) == {"content_draft": "dev"}, \
            "路由仍应保存（用户可能稍后切换环境）"
        assert await pf.resolve_scene_config("content_draft") is None, \
            "运行时语义不变：跨环境路由不生效"

    async def test_put_warns_when_target_has_no_key(self, db_conn):
        await _insert(db_conn, "nokey", key="")
        res = await ai_router.update_scene_route(
            SceneRouteUpdate(scene="facts_extract", config_id="nokey"), db=db_conn)
        assert "API Key" in res["warning"]

    async def test_put_no_warning_when_healthy(self, db_conn):
        await _insert(db_conn, "ok")
        res = await ai_router.update_scene_route(
            SceneRouteUpdate(scene="outline_draft", config_id="ok"), db=db_conn)
        assert res["warning"] == ""

    async def test_put_clear_returns_empty_warning(self, db_conn):
        await _insert(db_conn, "ok")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="outline_draft", config_id="ok"), db=db_conn)
        res = await ai_router.update_scene_route(
            SceneRouteUpdate(scene="outline_draft", config_id=""), db=db_conn)
        assert res["warning"] == ""
        assert await _route_rows(db_conn) == {}

    async def test_scene_route_audit_keeps_warning_text(self, db_conn):
        """审计里也要能追到「当时为什么提示不生效」（脱敏摘要，不含密钥）。"""
        await _insert(db_conn, "dev", env="dev")
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        pf.invalidate_config_cache()
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id="dev"), db=db_conn)
        cur = await db_conn.execute(
            "SELECT detail FROM ai_config_audit_logs WHERE action='scene_route'")
        detail = (await cur.fetchone())[0]
        assert "暂不生效" in detail
        assert "sk-test" not in detail


# ===========================================================================
# B2 删除配置级联解除场景路由
# ===========================================================================
class TestDeleteConfigClearsSceneRoutes:
    async def test_zombie_routes_removed(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "c1")
        await _route(db_conn, "content_draft", "c1")
        await _route(db_conn, "chart_fix", "c1")
        await _route(db_conn, "facts_extract", "active")

        res = await ai_router.delete_config("c1", db=db_conn)
        assert res["ok"] is True
        assert res["cleared_scene_routes"] == 2
        assert await _route_rows(db_conn) == {"facts_extract": "active"}, \
            "只解除被删配置的引用，其它场景不受影响"

        listed = await ai_router.list_scene_routes(db=db_conn)
        by = {i["scene"]: i for i in listed["items"]}
        assert by["content_draft"]["config_id"] == ""
        assert by["content_draft"]["missing"] is False, \
            "路由行已删除，不该再显示「配置已删除」红标"
        assert listed["configured_count"] == 1

    async def test_delete_without_routes_reports_zero(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "lonely")
        res = await ai_router.delete_config("lonely", db=db_conn)
        assert res["cleared_scene_routes"] == 0

    async def test_delete_audit_mentions_cleared_count(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "c1")
        await _route(db_conn, "content_draft", "c1")
        await ai_router.delete_config("c1", db=db_conn)
        cur = await db_conn.execute(
            "SELECT detail FROM ai_config_audit_logs WHERE action='delete'")
        assert "1 条场景路由" in (await cur.fetchone())[0]


# ===========================================================================
# B3 清除 Key 时提示被场景路由引用
# ===========================================================================
class TestClearKeyReferenceWarning:
    async def test_warns_when_referenced(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "c1")
        await _route(db_conn, "content_draft", "c1")
        await _route(db_conn, "chart_fix", "c1")

        res = await ai_router.clear_config_key("c1", db=db_conn)
        assert res["ok"] is True
        assert "2 条场景路由" in res["warning"]
        assert "但它是当前的" not in res["warning"], \
            "非当前使用配置不应混入「主配置被清空」的告警"

    async def test_active_and_referenced_both_reported(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _route(db_conn, "content_draft", "active")
        res = await ai_router.clear_config_key("active", db=db_conn)
        assert "当前使用" in res["warning"] and "1 条场景路由" in res["warning"]

    async def test_no_warning_when_unreferenced(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "free")
        res = await ai_router.clear_config_key("free", db=db_conn)
        assert res["warning"] == ""

    async def test_unchanged_short_circuit_has_no_warning(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "nokey", key="")
        await _route(db_conn, "content_draft", "nokey")
        res = await ai_router.clear_config_key("nokey", db=db_conn)
        assert res["unchanged"] is True and res["warning"] == "", \
            "本来就没有 Key 时不产生新影响，不应打扰"


# ===========================================================================
# B4 审计明细按场景下钻
# ===========================================================================
class TestAuditLogSceneFilter:
    async def test_scene_filter_and_options(self, db_conn):
        await _seed_logs(db_conn, [
            ("l1", "deepseek", "chat", "content_draft", 1),
            ("l2", "deepseek", "chat", "content_draft", 0),
            ("l3", "deepseek", "chat", "facts_extract", 1),
            ("l4", "zhipu", "extract", "", 1),
        ])
        res = await ai_router.audit_logs(limit=50, db=db_conn)
        assert res["total"] == 4
        assert set(res["scenes"]) == {"content_draft", "facts_extract"}, \
            "空场景不参与下拉选项"

        res = await ai_router.audit_logs(limit=50, scene="content_draft", db=db_conn)
        assert res["total"] == 2
        assert {i["scene"] for i in res["items"]} == {"content_draft"}

    async def test_scene_combines_with_other_filters(self, db_conn):
        await _seed_logs(db_conn, [
            ("l1", "deepseek", "chat", "content_draft", 1),
            ("l2", "deepseek", "chat", "content_draft", 0),
            ("l3", "zhipu", "chat", "content_draft", 0),
        ])
        res = await ai_router.audit_logs(
            limit=50, scene="content_draft", provider_name="deepseek",
            success="0", db=db_conn)
        assert res["total"] == 1 and res["items"][0]["id"] == "l2"

    async def test_blank_scene_filter_ignored(self, db_conn):
        await _seed_logs(db_conn, [("l1", "deepseek", "chat", "content_draft", 1)])
        res = await ai_router.audit_logs(limit=50, scene="   ", db=db_conn)
        assert res["total"] == 1, "空白筛选值应视为未筛选（与既有筛选一致）"

    async def test_unknown_scene_returns_empty_not_error(self, db_conn):
        await _seed_logs(db_conn, [("l1", "deepseek", "chat", "content_draft", 1)])
        res = await ai_router.audit_logs(limit=50, scene="no_such_scene", db=db_conn)
        assert res["total"] == 0 and res["items"] == []


# ===========================================================================
# B5 导入激活后重放并发
# ===========================================================================
class TestImportReplaysConcurrency:
    async def test_set_first_active_reapplies_concurrency(self, db_conn, monkeypatch):
        calls: list[int] = []

        async def _spy():
            calls.append(1)
        monkeypatch.setattr(cfg_module, "apply_config_concurrency", _spy)

        res = await ai_router.import_config(ConfigImportIn(
            items=[{"provider_name": "deepseek", "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1", "concurrency": 3}],
            set_first_active=True), db=db_conn)
        assert res["imported"] == 1
        assert calls, "导入激活主配置后必须重放并发（否则界面上的并发数不生效）"

    async def test_import_without_activation_still_syncs(self, db_conn, monkeypatch):
        """未勾选激活时调用同样安全（重放的是既有主配置，行为等价）。"""
        calls: list[int] = []

        async def _spy():
            calls.append(1)
        monkeypatch.setattr(cfg_module, "apply_config_concurrency", _spy)

        await _insert(db_conn, "active", is_active=1, concurrency=2)
        res = await ai_router.import_config(ConfigImportIn(
            items=[{"provider_name": "deepseek", "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1"}]), db=db_conn)
        assert res["imported"] == 1
        assert calls == [1]


# ===========================================================================
# B6 环境值损坏：展示端点可恢复 + 运行时仍 fail-closed
# ===========================================================================
class TestEnvCorruptRecovery:
    async def test_config_list_degrades_instead_of_500(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _corrupt_env(db_conn)
        res = await ai_router.get_config(db=db_conn)
        assert res["count"] == 1 and res["active_env"] == ""
        assert "配置损坏" in res["env_error"], "必须如实回传原因供前端提示"

    async def test_env_endpoint_reports_error(self, db_conn):
        await _insert(db_conn, "a", env="prod")
        await _corrupt_env(db_conn)
        res = await ai_router.get_active_env(db=db_conn)
        assert res["env_error"] and res["active_env"] == ""
        assert res["envs"] == ["prod"], "环境标签清单仍要能读出来"

    async def test_health_distinguishes_env_corrupt_from_not_configured(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _corrupt_env(db_conn)
        res = await ai_router.ai_health(db=db_conn)
        assert res["status"] == "env_corrupt", \
            "不能把「环境损坏」误报成「尚未配置模型」"
        assert "配置损坏" in res["hint"] and "清空" in res["hint"]
        assert res["env_error"]

    async def test_runtime_endpoint_stays_available(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _corrupt_env(db_conn)
        res = await ai_router.get_runtime(db=db_conn)
        assert res["env_error"] and res["active_env"] == ""
        assert res["configured_providers"] == ["openai"]

    async def test_runtime_selection_still_fails_closed(self, db_conn):
        """硬约束：展示端点降级 ≠ 放宽运行时。选模仍必须拒绝损坏环境值。"""
        await _insert(db_conn, "active", is_active=1)
        await _corrupt_env(db_conn)
        with pytest.raises(ValueError, match="配置损坏"):
            await pf.resolve_active_env()
        # 存在场景路由时同样 fail-closed：绝不因环境值损坏就按通用环境选模型
        await _route(db_conn, "content_draft", "active")
        with pytest.raises(ValueError, match="配置损坏"):
            await pf.resolve_scene_config("content_draft")
        with pytest.raises(ValueError, match="配置损坏"):
            await pf._load_active_config()

    async def test_reset_env_recovers_all_endpoints(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _corrupt_env(db_conn)
        res = await ai_router.set_active_env(ActiveEnvIn(env=""), db=db_conn)
        assert res["ok"] is True
        assert await pf.resolve_active_env() == ""
        assert (await ai_router.get_config(db=db_conn))["env_error"] == ""
        assert (await ai_router.get_active_env(db=db_conn))["env_error"] == ""
        assert (await ai_router.ai_health(db=db_conn))["status"] != "env_corrupt"
        assert (await ai_router.get_runtime(db=db_conn))["env_error"] == ""
        assert (await ai_router.list_scene_routes(db=db_conn))["active_env"] == ""

    async def test_scene_routes_survive_corrupt_env(self, db_conn):
        """环境损坏时场景路由页仍要打得开（展示端点不得连带 500）。"""
        await _insert(db_conn, "c1")
        await _route(db_conn, "content_draft", "c1")
        await _corrupt_env(db_conn)
        res = await ai_router.list_scene_routes(db=db_conn)
        assert res["active_env"] == "" and res["configured_count"] == 1
        item = [i for i in res["items"] if i["scene"] == "content_draft"][0]
        assert item["env_mismatch"] is False, "无法判定生效环境时不臆测跨环境"


# ===========================================================================
# B7 scene 筛选索引
# ===========================================================================
class TestSceneFilterIndex:
    async def test_index_created_by_migrate(self, db_conn):
        cur = await db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_ai_audit_scene_created'")
        assert await cur.fetchone(), "迁移必须补出以 scene 为前导列的覆盖索引"

    async def test_scene_query_uses_the_index(self, db_conn):
        await _seed_logs(db_conn, [("l1", "deepseek", "chat", "content_draft", 1)])
        cur = await db_conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM ai_audit_logs"
            " WHERE scene=? ORDER BY created_at DESC LIMIT 50", ("content_draft",))
        plan = " ".join(str(r[3]) for r in await cur.fetchall())
        assert "idx_ai_audit_scene_created" in plan, f"未走预期索引：{plan}"
