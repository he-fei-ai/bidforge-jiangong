"""AI 配置 · 运行时「厂商开关」回归测试（✅ 2026-09-23 新增）。

背景：某厂商临时欠费 / 被限流 / 故障 / 合规下线时，此前只能「删除配置」（丢密钥、
事后重填）或「等熔断器冷却」。本功能提供不删配置、立即生效、可随时恢复的开关，
落在 ``ai_runtime_settings``（键 ``disabled_providers``）。

锁定行为：
1. 默认（未设置）= 空集 → **不跳过任何候选**，与引入前完全一致；
2. 被禁用的厂商在候选过滤阶段直接跳过（连探测都不发），并记
   ``action="provider_disabled"`` 审计；
3. ``provider_disabled`` **不计入可靠性统计**（否则重新启用后会被「死配置剔除」
   继续排除，表现为「开了也没用」）；
4. 全部候选被禁用 → 抛出可执行的错误（而不是无信息量的 skipped_*=0）；
5. 写入校验：非法字符集 / 未知厂商 → 400；空数组 = 全部恢复；
6. 解析兼容 JSON 数组与逗号分隔（人工改库）两种写法，脏值跳过不判废整份设置。
"""
import contextlib
import json

import app.services.ai.provider_factory as pf
import pytest
from app.models import DisabledProvidersIn
from app.routers import ai_config as ai_router
from app.services.crypto import encrypt_api_key


@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)


@pytest.fixture(autouse=True)
def no_builtin_agnes(monkeypatch):
    """屏蔽内置兜底厂商 agnes。

    ⚠️ 本机 `.env` 配了 ``AGNES_API_KEY``，`_fallback_chain()` 会自动追加 agnes 候选 ——
    不做屏蔽时「全部候选被禁用」这类用例会拿 agnes 去发**真实付费请求**，
    测试既可能产生费用、也不再是确定性的（断网/欠费就会随机失败）。
    """
    monkeypatch.setattr(pf.settings, "agnes_api_key", "", raising=False)


async def _insert(db, cid, provider="openai", model="gpt-4o", is_active=0,
                  priority=0, key="sk-test", env=""):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key),
         "https://api.test.com/v1", model, env, is_active, priority))
    await db.commit()


async def _audits(db, action):
    cur = await db.execute(
        "SELECT * FROM ai_config_audit_logs WHERE action=? ORDER BY created_at ASC",
        (action,))
    return [dict(r) for r in await cur.fetchall()]


# ===========================================================================
# 1/6. 归一化与解析
# ===========================================================================
class TestNormalizeAndParse:
    def test_normalize_accepts_common_names(self):
        assert pf.normalize_provider_name(" deepseek ") == "deepseek"
        assert pf.normalize_provider_name("qwen-max.v2") == "qwen-max.v2"
        assert pf.normalize_provider_name("") == ""
        assert pf.normalize_provider_name(None) == ""

    def test_normalize_rejects_illegal(self):
        for bad in ("deep seek", "openai;drop", "厂商", "a/b"):
            with pytest.raises(ValueError):
                pf.normalize_provider_name(bad)
        with pytest.raises(ValueError):
            pf.normalize_provider_name("x" * 65)

    def test_parse_json_and_csv_forms(self):
        assert pf.parse_disabled_providers('["a","b"]') == {"a", "b"}
        assert pf.parse_disabled_providers("a, b ,c") == {"a", "b", "c"}
        assert pf.parse_disabled_providers("") == set()
        assert pf.parse_disabled_providers(None) == set()

    def test_parse_skips_dirty_entries_without_discarding_all(self):
        # 一个脏值不应让整份设置失效（人工改库很容易写成这样）
        assert pf.parse_disabled_providers("good, bad name, also_good") == {"good", "also_good"}
        assert pf.parse_disabled_providers('["ok", "bad name"]') == {"ok"}

    def test_parse_invalid_json_fails_closed(self):
        with pytest.raises(ValueError, match="配置损坏"):
            pf.parse_disabled_providers("[not-json")


# ===========================================================================
# 2/3/4. 调用链行为
# ===========================================================================
class TestDisabledSkipsCandidates:
    async def test_default_does_not_skip_anything(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        assert await pf.resolve_disabled_providers() == set()

        seen: list[str] = []

        async def fake_attempt(c, messages, **kw):
            seen.append(c.get("provider_name", ""))
            return "ok", None

        import app.services.ai.provider_factory as _pf
        orig = _pf._attempt_candidate
        _pf._attempt_candidate = fake_attempt
        try:
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        finally:
            _pf._attempt_candidate = orig
        assert seen == ["mainprov"]

    async def test_disabled_primary_falls_back_to_next(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", model="m-main", is_active=1)
        await _insert(db_conn, "backup", provider="backprov", model="m-back", priority=1)
        await pf.save_disabled_providers(["mainprov"])

        seen: list[str] = []

        async def fake_attempt(c, messages, **kw):
            seen.append(c.get("provider_name", ""))
            return "ok", None

        import app.services.ai.provider_factory as _pf
        orig = _pf._attempt_candidate
        _pf._attempt_candidate = fake_attempt
        try:
            out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}],
                                              scene="content_draft")
        finally:
            _pf._attempt_candidate = orig
        assert out == "ok"
        assert "mainprov" not in seen, "被禁用的厂商不得发出真实请求"
        assert seen == ["backprov"]

    async def test_disabled_does_not_pollute_reliability(self, db_conn):
        """provider_disabled 不得计入失败 —— 否则重新启用后仍被「死配置剔除」排除。"""
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await pf.save_disabled_providers(["mainprov"])
        with pytest.raises(Exception):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        # 可靠性统计是进程内的（_log_audit 同步更新），无需落库即可断言
        rate = pf._provider_success_rate("mainprov")
        assert rate is None, "被禁用不是失败，不能产生成功率样本"

    async def test_all_disabled_raises_actionable_error(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await pf.save_disabled_providers(["mainprov"])
        with pytest.raises(RuntimeError) as ei:
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        msg = str(ei.value)
        assert "运行时开关禁用" in msg and "mainprov" in msg

    async def test_disabled_provider_read_failure_fails_closed(self, monkeypatch):
        """DB 锁/I/O 故障不能被误判为空清单，从而恢复被禁用厂商。"""
        import sqlite3

        async def _locked():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(pf, "get_conn", _locked)
        pf.invalidate_config_cache()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await pf.resolve_disabled_providers()

    async def test_missing_runtime_table_remains_backward_compatible(self, monkeypatch):
        import sqlite3

        async def _missing_table():
            raise sqlite3.OperationalError("no such table: ai_runtime_settings")

        monkeypatch.setattr(pf, "get_conn", _missing_table)
        pf.invalidate_config_cache()
        assert await pf.resolve_disabled_providers() == set()

    async def test_re_enable_restores_calls(self, db_conn):
        await _insert(db_conn, "main", provider="mainprov", is_active=1)
        await pf.save_disabled_providers(["mainprov"])
        assert "mainprov" in await pf.resolve_disabled_providers()
        await pf.save_disabled_providers([])
        assert await pf.resolve_disabled_providers() == set(), "清空后必须立刻恢复"


# ===========================================================================
# 5. 路由层
# ===========================================================================
class TestRuntimeRoutes:
    async def test_get_runtime_defaults(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek")
        res = await ai_router.get_runtime(db=db_conn)
        assert res["disabled_providers"] == []
        assert res["configured_providers"] == ["deepseek"]
        assert res["effective_provider_count"] == 1
        assert res["all_configured_disabled"] is False
        # 可选项 = 已配置 ∪ 内置预设（预设很多，故只校验包含关系）
        assert "deepseek" in res["providers"]
        assert len(res["providers"]) > 1

    async def test_put_persists_and_audits(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek")
        res = await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=["deepseek", "zhipu"]), db=db_conn)
        assert res["ok"] is True
        assert res["disabled_providers"] == ["deepseek", "zhipu"]
        assert res["warning"], "禁用「当前已配置」的厂商必须提示影响"
        assert await pf.resolve_disabled_providers() == {"deepseek", "zhipu"}
        rows = await _audits(db_conn, "runtime_switch")
        assert len(rows) == 1 and "deepseek" in rows[0]["detail"]

    async def test_put_all_configured_disabled_warns_hard(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek")
        res = await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=["deepseek"]), db=db_conn)
        assert "全部" in res["warning"]
        assert (await ai_router.get_runtime(db=db_conn))["all_configured_disabled"] is True

    async def test_put_rejects_unknown_provider(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.set_disabled_providers(
                DisabledProvidersIn(providers=["not-a-real-provider-xyz"]), db=db_conn)

    async def test_put_rejects_illegal_name(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.set_disabled_providers(
                DisabledProvidersIn(providers=["bad name"]), db=db_conn)

    async def test_put_empty_restores_all(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek")
        await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=["deepseek"]), db=db_conn)
        res = await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=[]), db=db_conn)
        assert res["disabled_providers"] == [] and res["warning"] == ""
        assert await pf.resolve_disabled_providers() == set()

    async def test_put_is_idempotent_and_deduped(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek")
        res = await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=["deepseek", "deepseek", " deepseek "]),
            db=db_conn)
        assert res["disabled_providers"] == ["deepseek"]

    async def test_disabled_survives_manual_csv_edit(self, db_conn):
        """人工直接改库写成逗号分隔也必须被识别（运维常见操作）。"""
        await db_conn.execute(
            "INSERT INTO ai_runtime_settings (key, value) VALUES (?,?)",
            (pf.RUNTIME_DISABLED_PROVIDERS_KEY, "deepseek, zhipu"))
        await db_conn.commit()
        pf.invalidate_config_cache()
        assert await pf.resolve_disabled_providers() == {"deepseek", "zhipu"}

    async def test_runtime_switch_visible_in_health(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek", is_active=1)
        await ai_router.set_disabled_providers(
            DisabledProvidersIn(providers=["deepseek"]), db=db_conn)
        health = await ai_router.ai_health(db=db_conn)
        assert health["disabled_providers"] == ["deepseek"]
        assert "active_env" in health


# ===========================================================================
# 附：运行时表存储形态（便于人工排查）
# ===========================================================================
async def test_stored_value_is_json_array(db_conn):
    await _insert(db_conn, "c1", provider="deepseek")
    await ai_router.set_disabled_providers(
        DisabledProvidersIn(providers=["zhipu", "deepseek"]), db=db_conn)
    cur = await db_conn.execute(
        "SELECT value FROM ai_runtime_settings WHERE key=?",
        (pf.RUNTIME_DISABLED_PROVIDERS_KEY,))
    raw = (await cur.fetchone())[0]
    assert json.loads(raw) == ["deepseek", "zhipu"], "落库应为排序去重后的 JSON 数组"
