# -*- coding: utf-8 -*-
"""AI 配置模块 · 2026-09-27 缺陷回归。

覆盖本轮定位并修复的两处「配了/展示得了，却存不进去 / 查不到」静默失效：

1. **运行时厂商开关的恢复路径自我锁死**（`routers/ai_config/runtime.py`）
   ``GET /runtime`` 的 ``providers`` 并入「当前已禁用」集合（注释明确要求
   "后者可能既非配置也非预设，需能取消勾选"），而 ``PUT /runtime/disabled-providers``
   的白名单此前只取 ``已配置 ∪ 内置预设``。某厂商被禁用后其配置被删除
   （或内置预设改名/下架），它就成了"界面能勾选、但 PUT 报 400 未知厂商"——
   **唯一能把它移出禁用集的入口，恰好拒绝执行该操作**，禁用集不可逆。

2. **chart-json 无 type 字段的幽灵图**（`routers/_chart_pipeline.py`）
   详见 ``test_chart_json_registration_parity_20260927.py``（同批修复，同批回归）。
   本文件只放一条端到端护栏，确保两处修复同属"配置/登记口径单一事实来源"这一主线。
"""
import contextlib

import app.services.ai.provider_factory as pf
import pytest
from app.models import DisabledProvidersIn
from app.routers.ai_config import runtime as runtime_router
from app.services.crypto import encrypt_api_key


@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _insert(db, cid, provider="openai", model="gpt-4o", is_active=0, priority=0):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key("sk-test"),
         "https://api.test.com/v1", model, "", is_active, priority))
    await db.commit()


class TestDisabledProviderRecoveryPath:
    """缺陷 1：禁用集必须永远可被自身清空（恢复路径不得自我锁死）。"""

    async def test_get_exposes_disabled_orphan_provider(self, db_conn):
        """先禁用某厂商，再删掉它的配置 → GET 仍应把它列为可选项。"""
        await _insert(db_conn, "c1", provider="legacy_vendor")
        await pf.save_disabled_providers(["legacy_vendor"])
        await db_conn.execute("DELETE FROM ai_config WHERE id='c1'")
        await db_conn.commit()
        pf.invalidate_config_cache()

        data = await runtime_router.get_runtime(db=db_conn)
        assert "legacy_vendor" in data["providers"], (
            "已禁用厂商即使配置被删也必须可勾选，否则用户无法恢复")
        assert "legacy_vendor" in data["disabled_providers"]

    async def test_put_echoing_current_state_succeeds(self, db_conn):
        """回归核心失败模式：界面把当前勾选状态原样回存时不得 400。

        该端点是「**整体覆盖**」语义，前端提交的是"当前勾选中的厂商列表"。
        孤儿厂商（已禁用但配置已删）默认处于**勾选**状态，用户点一次"保存"
        就会把它原样回传 —— 旧实现的 `known` 不含它，直接 400，
        于是这个页面变成"改任何东西都保存失败"。
        """
        await _insert(db_conn, "c1", provider="legacy_vendor")
        await pf.save_disabled_providers(["legacy_vendor"])
        await db_conn.execute("DELETE FROM ai_config WHERE id='c1'")
        await db_conn.commit()
        pf.invalidate_config_cache()

        shown = (await runtime_router.get_runtime(db=db_conn))
        # 模拟前端：原样回传当前勾选集合
        res = await runtime_router.set_disabled_providers(
            DisabledProvidersIn(providers=shown["disabled_providers"]),
            request=None, db=db_conn)
        assert res["ok"] is True, "回存当前勾选状态必须成功（否则页面完全不可用）"
        assert res["disabled_providers"] == ["legacy_vendor"]

    async def test_put_accepts_every_provider_shown_by_get(self, db_conn):
        """通用不变量：GET 展示的每个厂商，PUT 都必须接受（界面能点=后端能存）。

        每次循环前**复位**禁用集：PUT 是"整体覆盖"语义，上一轮的写入会改变
        下一轮看到的状态（不复位则测的是别的场景，而非"能否被接受"）。
        """
        await _insert(db_conn, "c1", provider="openai")
        original = ["legacy_vendor"]
        await pf.save_disabled_providers(original)
        pf.invalidate_config_cache()

        shown = sorted((await runtime_router.get_runtime(db=db_conn))["providers"])
        assert "legacy_vendor" in shown, "前置条件：孤儿厂商必须被 GET 展示"
        for name in shown:
            await pf.save_disabled_providers(original)   # 复位到同一初始状态
            pf.invalidate_config_cache()
            res = await runtime_router.set_disabled_providers(
                DisabledProvidersIn(providers=[name]), request=None, db=db_conn)
            assert res["ok"] is True, f"{name} 由 GET 展示却被 PUT 拒绝"

    async def test_put_still_rejects_truly_unknown_provider(self, db_conn):
        """修复不得放宽成"什么都收"：真正不存在的厂商仍应 400。"""
        with pytest.raises(Exception) as ei:
            await runtime_router.set_disabled_providers(
                DisabledProvidersIn(providers=["definitely_not_a_provider_xyz"]),
                request=None, db=db_conn)
        assert getattr(ei.value, "status_code", None) == 400

    async def test_put_still_rejects_illegal_charset(self, db_conn):
        """非法字符集仍应 400（normalize_provider_name 白名单不被绕过）。"""
        with pytest.raises(Exception) as ei:
            await runtime_router.set_disabled_providers(
                DisabledProvidersIn(providers=["bad name!"]), request=None, db=db_conn)
        assert getattr(ei.value, "status_code", None) == 400

    async def test_env_corrupt_does_not_break_runtime_endpoint(self, db_conn, monkeypatch):
        """环境值损坏时本端点仍可用（它是用户「恢复通用环境」的操作入口）。"""

        async def _boom():
            raise ValueError("运行时环境配置损坏，请重新选择环境")

        monkeypatch.setattr(runtime_router, "resolve_active_env", _boom)
        data = await runtime_router.get_runtime(db=db_conn)
        assert data["active_env"] == ""
        assert data["env_error"], "必须回传 env_error 供前端提示重置"
