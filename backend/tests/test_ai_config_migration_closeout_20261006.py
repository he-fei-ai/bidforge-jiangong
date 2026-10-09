"""AI 配置模块 · 迁移完整性 / 审计筛选 / 配置体检 / 场景路由批量（2026-10-06）。

本轮 4 项收口的护栏（含 A/B 反向验证要点）：

G1 【P0】``/ai/config/export`` 只导出 ``ai_config`` 单表 —— 24 条
   ``ai_scene_routes``（场景→模型路由）与 ``ai_runtime_settings``（当前环境 +
   被禁用厂商清单）迁移后全部丢失，用户要逐场景手工重配。现导出/导入都
   **加法式**带上这两个键（缺省 = 不迁移，旧行为逐字不变），并新增
   ``/config/import/dry-run`` 预演（与真导入共用 ``_classify_import_item``
   单一判据，不另写一份）。
G2 【P1】``/ai/config/audit-logs`` 只有 ``action`` 筛选维度 —— 答不出
   「这条配置一共被改过几次、最近一次是谁改的」。现加 ``config_id`` 筛选 +
   回传 ``config_ids``。
G3 【P1】只有 ``GET /ai/health``（只看当前使用那一条）与
   ``POST /ai/config/precheck-all``（只看网络）—— 无法回答「这条备选到底
   还能不能用上」。现新增 ``GET /ai/configs/health``：密钥三态 + 环境 + 地址 +
   网络 + 厂商开关一次性给出，判据全部复用运行时同源实现。
G14 【P1】``/ai/scene-routes`` 只有单条 PUT，批量设置 20+ 场景要手工点
    二十次。现抽出 ``_apply_scene_route`` 作单条/批量**唯一**写出口，新增
    ``POST /scene-routes/batch``（单条失败不中断整批）。
"""
from __future__ import annotations

import contextlib
import inspect
import json

import app.routers.ai_config.connectivity as conn_module
import app.services.ai.provider_factory as pf
import pytest
from app.models import (
    AIConfigIn,
    ConfigImportIn,
    SceneRouteBatchIn,
    SceneRouteUpdate,
)
from app.routers import ai_config as ai_router
from app.routers.ai_config.scene_routes import _apply_scene_route
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    """把 ``write_tx_conn`` 指向测试库，并复位禁用厂商缓存（conftest 未覆盖它）。"""
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)
    pf._disabled_cache["data"] = None
    pf._disabled_cache["ts"] = 0.0


def _scene_keys() -> list[str]:
    """取 ``KNOWN_SCENES`` 里真实存在的场景键（避免测试里写死字面量后漂移）。"""
    return sorted(pf.KNOWN_SCENES.keys())


async def _one(db, sql, *params):
    cur = await db.execute(sql, params)
    return await cur.fetchone()


async def _count(db, table: str) -> int:
    return int((await _one(db, f"SELECT COUNT(*) FROM {table}"))[0] or 0)


async def _create_config(db, *, provider="deepseek", model="deepseek-v3",
                         api_key="sk-test-0001", env="", is_active=True,
                         base_url="") -> str:
    res = await ai_router.save_config(
        AIConfigIn(provider_name=provider, model=model, api_key=api_key,
                   base_url=base_url, env=env, is_active=is_active), db=db)
    assert res["ok"] is True
    return res["id"]


def _mock_network(monkeypatch, ok: bool = True) -> list[str]:
    """替掉真实 DNS/TCP 预检（测试环境无外网，且单次最长 5s）。"""
    calls: list[str] = []

    async def _fake(url: str) -> dict:
        calls.append(url)
        return {"ok": ok, "step": "ok" if ok else "dns",
                "ip": "1.2.3.4" if ok else "",
                "message": "" if ok else "DNS 解析失败：无法找到 example.com"}
    monkeypatch.setattr(conn_module, "_dns_precheck_async", _fake)
    return calls


# ===========================================================================
# G1-1 · 导出带上场景路由与运行时设置
# ===========================================================================
class TestExportMigration:
    async def test_export_carries_scene_routes_and_runtime(self, db_conn):
        cid = await _create_config(db_conn)
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene=_scene_keys()[0], config_id=cid), db=db_conn)
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        await pf.save_runtime_setting(
            pf.RUNTIME_DISABLED_PROVIDERS_KEY, json.dumps(["deepseek"]))

        exp = await ai_router.export_config(db=db_conn)

        assert isinstance(exp["scene_routes"], list) and len(exp["scene_routes"]) == 1
        assert exp["scene_routes"][0]["scene"] == _scene_keys()[0]
        assert exp["scene_routes"][0]["config_id"] == cid
        assert exp["runtime"][pf.RUNTIME_ACTIVE_ENV_KEY] == "prod"
        assert exp["runtime"][pf.RUNTIME_DISABLED_PROVIDERS_KEY] == '["deepseek"]'

    async def test_export_empty_when_no_routes_no_runtime(self, db_conn):
        await _create_config(db_conn)
        exp = await ai_router.export_config(db=db_conn)
        assert exp["scene_routes"] == [], "无路由时导出空数组（不是缺键）"
        assert exp["runtime"] == {}, "从未设置过运行时开关时导出空对象"

    async def test_export_never_leaks_secret(self, db_conn):
        """导出载荷里不得出现任何密钥形态字段（含密文）—— 迁移靠的是重新填 Key。"""
        await _create_config(db_conn, api_key="sk-secret-1234")
        exp = await ai_router.export_config(db=db_conn)
        assert exp["api_key_included"] is False
        blob = json.dumps(exp, ensure_ascii=False)
        assert "sk-secret-1234" not in blob
        for item in exp["items"]:
            assert "api_key" not in item
            assert "api_key_encrypted" not in item

    async def test_export_backward_compat_keys_unchanged(self, db_conn):
        """加法式变更：既有契约键一个都不能丢（前端 AIConfigExport 依赖它们）。"""
        await _create_config(db_conn)
        exp = await ai_router.export_config(db=db_conn)
        for k in ("version", "exported_at", "count", "api_key_included", "items"):
            assert k in exp, f"既有契约键 {k} 不得丢失"
        assert exp["count"] == 1 and len(exp["items"]) == 1
# ===========================================================================
# G1-2 · 导入迁移场景路由（config_id 必须重新映射）与运行时设置
# ===========================================================================
class TestImportMigration:
    async def test_legacy_payload_without_new_keys_is_unchanged(self, db_conn):
        """不传 scene_routes / runtime → 行为与引入前逐字一致（什么都不迁移）。"""
        await _create_config(db_conn, provider="deepseek", model="old-model")
        res = await ai_router.import_config(ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat", "base_url": "",
        }]), db=db_conn)
        assert res["imported"] == 1
        assert await _count(db_conn, "ai_scene_routes") == 0
        assert await _count(db_conn, "ai_runtime_settings") == 0
        assert res["migrated_scene_routes"] == 0
        assert res["migrated_runtime_keys"] == []


    async def test_import_migrates_scene_routes_with_new_id(self, db_conn):
        """场景路由随配置迁移，config_id 必须映射到本次导入新生成的 id。"""
        scene = _scene_keys()[0]
        cid = await _create_config(db_conn)
        await ai_router.update_scene_route(SceneRouteUpdate(scene=scene, config_id=cid),
                                           db=db_conn)
        exp = await ai_router.export_config(db=db_conn)
        # 模拟「迁移到新机器」：本机清空配置表
        await db_conn.execute("DELETE FROM ai_config")
        await db_conn.commit()

        res = await ai_router.import_config(
            ConfigImportIn(items=exp["items"], scene_routes=exp["scene_routes"]),
            db=db_conn)

        assert res["imported"] == 1
        assert res["migrated_scene_routes"] == 1, "路由必须随配置一并迁移"
        row = await _one(db_conn, "SELECT config_id FROM ai_scene_routes WHERE scene=?", scene)
        assert row is not None
        assert row[0] != cid, "不得复用源机器的 id"
        cur = await db_conn.execute("SELECT id FROM ai_config")
        ids = [r[0] for r in await cur.fetchall()]
        assert row[0] in ids, "路由指向的 id 必须在本机真实存在（否则界面永久显示「配置已删除」）"

    async def test_import_scene_route_missing_config_skipped(self, db_conn):
        """原配置不在本批导入中 → 跳过并说明原因（绝不写指向不存在配置的僵尸行）。"""
        res = await ai_router.import_config(ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
        }], scene_routes=[{"scene": _scene_keys()[0], "config_id": "not-imported-id"}]),
            db=db_conn)
        assert res["imported"] == 1
        assert res["migrated_scene_routes"] == 0
        assert any("原配置未包含" in s for s in res["skip_reasons"])
        assert await _count(db_conn, "ai_scene_routes") == 0

    async def test_import_scene_route_unknown_scene_skipped(self, db_conn):
        """场景不在白名单 → 跳过（与 PUT /scene-routes 的 400 同源判据）。"""
        res = await ai_router.import_config(ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
        }], scene_routes=[{"scene": "no_such_scene_xyz", "config_id": "x"}]),
            db=db_conn)
        assert res["migrated_scene_routes"] == 0
        assert any("白名单" in s for s in res["skip_reasons"])

    async def test_import_migrates_runtime_settings(self, db_conn):
        res = await ai_router.import_config(ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
        }], runtime={
            pf.RUNTIME_ACTIVE_ENV_KEY: "prod",
            pf.RUNTIME_DISABLED_PROVIDERS_KEY: json.dumps(["deepseek", "openai"]),
            "future_unknown_key": "should-be-ignored",
        }), db=db_conn)
        assert sorted(res["migrated_runtime_keys"]) == sorted(
            [pf.RUNTIME_ACTIVE_ENV_KEY, pf.RUNTIME_DISABLED_PROVIDERS_KEY])
        assert await pf.load_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY) == "prod"
        assert pf.parse_disabled_providers(
            await pf.load_runtime_setting(pf.RUNTIME_DISABLED_PROVIDERS_KEY)
        ) == {"deepseek", "openai"}
        assert await pf.load_runtime_setting("future_unknown_key") is None, (
            "未知运行时键必须忽略（前向兼容未来新增的运行时设置）")

    async def test_import_runtime_invalid_env_not_written(self, db_conn):
        """非法环境名跳过该键，不影响其它键与配置主体导入（脏值不能绕过 PUT /env 校验）。"""
        res = await ai_router.import_config(ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
        }], runtime={
            pf.RUNTIME_ACTIVE_ENV_KEY: "中文！非法",
            pf.RUNTIME_DISABLED_PROVIDERS_KEY: json.dumps(["deepseek"]),
        }), db=db_conn)
        assert res["imported"] == 1
        assert pf.RUNTIME_ACTIVE_ENV_KEY not in res["migrated_runtime_keys"]
        assert pf.RUNTIME_DISABLED_PROVIDERS_KEY in res["migrated_runtime_keys"]
        assert await pf.load_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY) is None


# ===========================================================================
# G2 · 配置变更审计支持 config_id 筛选 + config_ids 清单
# ===========================================================================
class TestAuditConfigIdFilter:
    async def _seed(self, db_conn):
        c1 = await _create_config(db_conn, provider="deepseek", model="m1", is_active=True)
        c2 = await _create_config(db_conn, provider="openai", model="m2", is_active=False)
        # 给 c1 再留一条 update，保证「c1 有 2 条、c2 有 1 条」
        await ai_router.save_config(
            AIConfigIn(id=c1, provider_name="deepseek", model="m1b"), db=db_conn)
        return c1, c2

    async def test_no_filter_returns_all_rows(self, db_conn):
        c1, c2 = await self._seed(db_conn)
        res = await ai_router.config_audit_logs(db=db_conn)
        assert res["total"] == 3
        assert res["config_id"] == "", "默认不筛选（向后兼容）"
        assert len(res["config_ids"]) == 2

    async def test_filter_by_config_id(self, db_conn):
        c1, c2 = await self._seed(db_conn)
        r1 = await ai_router.config_audit_logs(db=db_conn, config_id=c1)
        assert r1["total"] == 2
        assert all(i["config_id"] == c1 for i in r1["items"])
        r2 = await ai_router.config_audit_logs(db=db_conn, config_id=c2)
        assert r2["total"] == 1
        r_none = await ai_router.config_audit_logs(db=db_conn, config_id="not-exist")
        assert r_none["total"] == 0
        assert r_none["config_ids"] == []

    async def test_config_ids_has_labels_and_counts(self, db_conn):
        c1, c2 = await self._seed(db_conn)
        res = await ai_router.config_audit_logs(db=db_conn)
        by_id = {c["value"]: c for c in res["config_ids"]}
        assert set(by_id) == {c1, c2}
        assert "deepseek" in by_id[c1]["label"], "标签应取审计行里留存的供应商/模型"
        assert by_id[c1]["count"] == 2 and by_id[c2]["count"] == 1, "count 为该时间窗内变更次数"

class TestDryRun:
    async def test_dry_run_makes_no_writes(self, db_conn):
        """预演绝不产生任何写入（含审计）—— 否则「预演」就不是预演。"""
        body = ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat", "base_url": "",
        }], scene_routes=[{"scene": _scene_keys()[0], "config_id": "abc"}],
            runtime={pf.RUNTIME_ACTIVE_ENV_KEY: "prod"})
        res = await ai_router.import_config_dry_run(body, db=db_conn)
        assert res["ok"] is True
        for table in ("ai_config", "ai_scene_routes", "ai_runtime_settings",
                      "ai_config_audit_logs"):
            assert await _count(db_conn, table) == 0, f"预演不得写入 {table}"

    async def test_dry_run_matches_real_import(self, db_conn):
        """承重例：预演与真导入必须给出同一处置结果（判据单一出口）。"""
        body = ConfigImportIn(items=[
            {"provider_name": "deepseek", "model": "deepseek-chat", "base_url": ""},
            # 批内重复：第二条必须被判成 skip
            {"provider_name": "deepseek", "model": "deepseek-chat", "base_url": ""},
            # 包月套餐无地址：联动校验拒绝
            {"provider_name": "deepseek", "model": "bad-plan",
             "base_url": "", "plan": "coding_plan"},
            # 缺必填字段
            {"provider_name": "", "model": "no-provider"},
        ])
        dry = await ai_router.import_config_dry_run(body, db=db_conn)
        real = await ai_router.import_config(body, db=db_conn)

        assert dry["total"] == 4
        assert [p["action"] for p in dry["planned"]] == ["new", "skip", "skip", "skip"]
        assert dry["would_import"] == real["imported"] == 1
        assert dry["skipped"] == real["skipped"] == 3
        assert dry["invalid_plan_items"] == real["invalid_plan_items"] == 1
        assert real["imported"] + real["skipped"] == 4
        assert await _count(db_conn, "ai_config") == real["imported"]

    async def test_dry_run_reports_route_and_runtime_plan(self, db_conn):
        body = ConfigImportIn(items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
            # 导出文件 items 携带原始 id —— 场景路由据此映射到「本批会落库的配置」
            "id": "id-from-export",
        }], scene_routes=[
            {"scene": _scene_keys()[0], "config_id": "id-from-export"},
            {"scene": _scene_keys()[0], "config_id": "not-in-batch"},
            {"scene": "no_such_scene_xyz", "config_id": "id-from-export"},
        ], runtime={pf.RUNTIME_ACTIVE_ENV_KEY: "prod", "future_unknown_key": 1})
        dry = await ai_router.import_config_dry_run(body, db=db_conn)
        assert dry["scene_routes"]["planned"] == 1
        assert dry["scene_routes"]["skipped"] == 2
        assert dry["runtime"]["planned"] == [pf.RUNTIME_ACTIVE_ENV_KEY]
        assert dry["runtime"]["skipped"] == ["future_unknown_key"]


# ===========================================================================
# G3 · GET /ai/configs/health —— 全部配置的可用性体检（密钥/环境/地址/网络/开关）
# ===========================================================================
class TestConfigsHealth:
    async def test_summary_arithmetic_consistent(self, db_conn, monkeypatch):
        _mock_network(monkeypatch)
        await _create_config(db_conn, provider="deepseek", model="m1", is_active=True)
        await _create_config(db_conn, provider="openai", model="m2",
                             api_key="", is_active=False)
        h = await ai_router.configs_health(db=db_conn)

        assert h["summary"]["total"] == len(h["items"]) == 2
        assert h["summary"]["active"] == 1
        assert h["summary"]["usable"] == sum(1 for i in h["items"] if i["usable"])
        assert h["summary"]["no_key"] == sum(
            1 for i in h["items"] if not i["has_key"] and not i["key_broken"])
        assert h["summary"]["key_broken"] == sum(1 for i in h["items"] if i["key_broken"])
        assert h["summary"]["no_url"] == sum(1 for i in h["items"] if i["no_url"])
        assert h["summary"]["out_of_env"] == sum(1 for i in h["items"] if not i["in_current_env"])
        assert h["summary"]["disabled"] == sum(1 for i in h["items"] if i["disabled"])
        assert h["summary"]["network_ok"] == sum(1 for i in h["items"] if i["network_ok"])
        assert h["summary"]["network_unreachable"] == sum(
            1 for i in h["items"] if i["base_url"] and not i["network_ok"])
        for i in h["items"]:
            # 密钥三态互斥：有 Key / 密文解不开 / 都没有
            assert i["has_key"] + i["key_broken"] <= 1

    async def test_key_three_states(self, db_conn, monkeypatch):
        _mock_network(monkeypatch)
        await _create_config(db_conn, provider="deepseek", model="m-ok",
                             api_key="sk-real-0001", is_active=True)
        await _create_config(db_conn, provider="deepseek", model="m-nokey",
                             api_key="", is_active=False)
        c3 = await _create_config(db_conn, provider="deepseek", model="m-broken",
                                  api_key="", is_active=False)
        # 模拟 FERNET_KEY 更换 / data/secret_key.key 被删：密文解不开
        await db_conn.execute("UPDATE ai_config SET api_key_encrypted=?"
                              " WHERE id=?", ("not-a-fernet-token", c3))
        await db_conn.commit()

        h = await ai_router.configs_health(db=db_conn)
        by_model = {i["model"]: i for i in h["items"]}
        assert by_model["m-ok"]["has_key"] is True and by_model["m-ok"]["key_broken"] is False
        assert by_model["m-ok"]["key_hint"] == "0001", "密钥提示取后 4 位（与 /ai/config 同口径）"
        assert by_model["m-nokey"]["has_key"] is False and by_model["m-nokey"]["key_broken"] is False
        assert by_model["m-broken"]["has_key"] is False and by_model["m-broken"]["key_broken"] is True
        assert h["summary"]["no_key"] == 1 and h["summary"]["key_broken"] == 1

    async def test_env_url_disabled_gates(self, db_conn, monkeypatch):
        _mock_network(monkeypatch)
        await pf.save_runtime_setting(pf.RUNTIME_ACTIVE_ENV_KEY, "prod")
        await pf.save_runtime_setting(
            pf.RUNTIME_DISABLED_PROVIDERS_KEY, json.dumps(["deepseek"]))
        pf._disabled_cache["data"] = None
        pf._disabled_cache["ts"] = 0.0

        await _create_config(db_conn, provider="deepseek", model="m-dev",
                             env="dev", is_active=True)
        c_bad = await _create_config(db_conn, provider="openai", model="m-nourl",
                                     api_key="", is_active=False)
        # 已知供应商会自动回填预设地址，直接清空以构造「缺地址」形态
        await db_conn.execute("UPDATE ai_config SET base_url='' WHERE id=?", (c_bad,))
        await db_conn.commit()

        h = await ai_router.configs_health(db=db_conn)
        assert h["active_env"] == "prod"
        by_model = {i["model"]: i for i in h["items"]}
        dev = by_model["m-dev"]
        assert dev["in_current_env"] is False, "env=dev 而当前环境为 prod"
        assert dev["disabled"] is True, "厂商被运行时开关禁用"
        assert dev["usable"] is False, "跨环境 + 被禁用 → 不可用"
        bad = by_model["m-nourl"]
        assert bad["no_url"] is True
        assert bad["network_step"] == "no_url", "缺地址不发起网络预检"
        assert bad["usable"] is False, "缺 Key + 缺地址 → 不可用"
        assert h["summary"]["out_of_env"] == 1
        assert h["summary"]["disabled"] == 1
        assert h["summary"]["no_url"] == 1

    async def test_network_precheck_failure_degrades(self, db_conn, monkeypatch):
        _mock_network(monkeypatch, ok=False)
        await _create_config(db_conn)
        h = await ai_router.configs_health(db=db_conn)
        assert h["items"][0]["network_ok"] is False
        assert h["items"][0]["network_step"] == "dns"
        assert h["summary"]["network_unreachable"] == 1

    async def test_env_corrupt_degrades_without_500(self, db_conn, monkeypatch):
        _mock_network(monkeypatch)
        await _create_config(db_conn)

        async def _boom():
            raise ValueError("环境值损坏")
        monkeypatch.setattr(pf, "resolve_active_env", _boom)

        h = await ai_router.configs_health(db=db_conn)
        assert h["active_env"] == ""
        assert h["summary"]["total"] == 1
        assert h["items"][0]["in_current_env"] is True

    def test_network_precheck_reuses_runtime_helper(self):
        """静态锁：网络判据必须复用 _dns_precheck_async，不得在体检端点重抄一份。"""
        src = inspect.getsource(conn_module.configs_health)
        assert "_dns_precheck_async" in src
        assert "getaddrinfo" not in src and "create_connection" not in src
        assert "resolve_active_env" in src and "resolve_disabled_providers" in src


# ===========================================================================
# G14 · 场景路由批量 —— 与单条共用 _apply_scene_route 唯一写出口
# ===========================================================================
class TestSceneRouteBatch:
    async def test_batch_applies_all(self, db_conn):
        cid = await _create_config(db_conn)
        keys = _scene_keys()[:3]
        res = await ai_router.batch_update_scene_routes(
            SceneRouteBatchIn(items=[SceneRouteUpdate(scene=k, config_id=cid)
                                     for k in keys]), db=db_conn)
        assert res["ok"] is True
        assert res["applied"] == 3 and res["failed"] == 0
        cur = await db_conn.execute("SELECT scene, config_id FROM ai_scene_routes ORDER BY scene")
        got = {r["scene"]: r["config_id"] for r in await cur.fetchall()}
        assert got == {k: cid for k in keys}

    async def test_single_failure_does_not_abort_batch(self, db_conn):
        cid = await _create_config(db_conn)
        keys = _scene_keys()[:3]
        res = await ai_router.batch_update_scene_routes(SceneRouteBatchIn(items=[
            SceneRouteUpdate(scene=keys[0], config_id=cid),
            SceneRouteUpdate(scene="no_such_scene_xyz", config_id=cid),
            SceneRouteUpdate(scene=keys[2], config_id=cid),
        ]), db=db_conn)
        assert res["applied"] == 2 and res["failed"] == 1
        assert res["warning"], "存在失败项时必须给出提示"
        cur = await db_conn.execute("SELECT scene FROM ai_scene_routes ORDER BY scene")
        assert {r["scene"] for r in await cur.fetchall()} == {keys[0], keys[2]}, (
            "成功项必须落库，失败项不影响其它项")
        bad = [i for i in res["items"] if not i["ok"]][0]
        assert "未知场景" in bad["error"]

    async def test_batch_clears_route_on_empty_config_id(self, db_conn):
        cid = await _create_config(db_conn)
        keys = _scene_keys()[:2]
        for k in keys:
            await ai_router.update_scene_route(SceneRouteUpdate(scene=k, config_id=cid),
                                               db=db_conn)
        res = await ai_router.batch_update_scene_routes(SceneRouteBatchIn(items=[
            SceneRouteUpdate(scene=keys[0], config_id=""),
        ]), db=db_conn)
        assert res["applied"] == 1
        cur = await db_conn.execute("SELECT scene FROM ai_scene_routes")
        assert [r["scene"] for r in await cur.fetchall()] == [keys[1]]

    async def test_batch_empty_rejected(self, db_conn):
        with pytest.raises(HTTPException) as e:
            await ai_router.batch_update_scene_routes(
                SceneRouteBatchIn(items=[]), db=db_conn)
        assert e.value.status_code == 400

    async def test_batch_over_limit_rejected(self, db_conn):
        from app.routers.ai_config.scene_routes import _BATCH_MAX_ITEMS
        items = [SceneRouteUpdate(scene="s", config_id="")
                 for _ in range(_BATCH_MAX_ITEMS + 1)]
        with pytest.raises(HTTPException) as e:
            await ai_router.batch_update_scene_routes(
                SceneRouteBatchIn(items=items), db=db_conn)
        assert e.value.status_code == 400

    async def test_batch_writes_audit_per_applied(self, db_conn):
        cid = await _create_config(db_conn)
        keys = _scene_keys()[:2]
        await ai_router.batch_update_scene_routes(SceneRouteBatchIn(items=[
            SceneRouteUpdate(scene=k, config_id=cid) for k in keys]), db=db_conn)
        n = (await _one(db_conn,
                        "SELECT COUNT(*) FROM ai_config_audit_logs"
                        " WHERE action='scene_route'"))[0]
        assert n == 2, "每个成功项逐条留痕（保留「每次变更一行」的可追溯性）"

    def test_single_and_batch_share_one_write_exit(self):
        """静态锁：单条与批量都只能经 _apply_scene_route 写库（判据单一出口）。"""
        batch_src = inspect.getsource(ai_router.batch_update_scene_routes)
        single_src = inspect.getsource(ai_router.update_scene_route)
        assert "_apply_scene_route(" in batch_src
        assert "_apply_scene_route(" in single_src
        # 单条端点不得再自己拼 INSERT/DELETE —— 否则两侧判据会各自演进
        assert "INSERT INTO ai_scene_routes" not in single_src
        assert "DELETE FROM ai_scene_routes" not in single_src
        apply_src = inspect.getsource(_apply_scene_route)
        assert "INSERT INTO ai_scene_routes" in apply_src
        assert "DELETE FROM ai_scene_routes" in apply_src
        # 写库统一在调用方提交/写审计（单条自己提交，批量整批提交）
        assert ".commit()" not in apply_src





