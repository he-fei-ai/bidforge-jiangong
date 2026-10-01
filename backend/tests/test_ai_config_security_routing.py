"""AI 配置模块 · 安全 / 审计 / 模型路由 / 缓存一致性 回归测试（2026-09-23）。

覆盖本轮新增与修复项：
1. ``clamp_warnings`` —— 数值越界被静默收敛时必须回传提示（原实现只回 ok:true）
2. ``DELETE /ai/config/{id}/key`` —— 密钥清除（原实现只能整体删配置，无法收回密钥）
3. 配置变更审计 ``ai_config_audit_logs`` —— 留痕 + **不含明文密钥**（脱敏）
4. 场景模型路由 ``ai_scene_routes`` —— 默认不影响行为、白名单校验、删配置回落
5. ``chat_with_fallback`` 按 scene 选配置（原实现 scene 只写审计、不参与选模型）
6. 缓存代际号 —— 读库期间被失效的旧结果不得写回缓存（TOCTOU 竞态）
7. ``_resolve_conn_credentials`` —— 「配置已删除」不得被误报成「Base URL 为空」
"""
import contextlib

import pytest

import app.services.ai.provider_factory as pf
from app.routers import ai_config as ai_router
from app.routers.ai_config import audit
from app.models import (
    AIConfigIn, ProviderModelsIn, SceneRouteUpdate, FallbackChainUpdate,
)
from app.services.crypto import encrypt_api_key


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def patch_write_tx_conn(db_conn, monkeypatch):
    """save_ai_config 走 write_tx_conn（独立写池），测试里指向内存连接。"""
    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn
    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _insert(db, cid, provider="openai", model="gpt-4o", is_active=0,
                  priority=0, key="sk-test", base_url="https://api.test.com/v1",
                  concurrency=4):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, concurrency) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model,
         is_active, priority, concurrency))
    await db.commit()


def _payload(**over) -> AIConfigIn:
    data = {
        "provider_name": "deepseek", "plan": "pay_as_you_go",
        "api_key": "sk-abc", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "max_tokens": 8192, "temperature": 0.7,
        "timeout": 900, "concurrency": 4, "is_active": False,
    }
    data.update(over)
    return AIConfigIn(**data)


async def _audit_rows(db, action=""):
    sql = "SELECT * FROM ai_config_audit_logs"
    params: tuple = ()
    if action:
        sql += " WHERE action=?"
        params = (action,)
    cur = await db.execute(sql + " ORDER BY created_at ASC", params)
    return [dict(r) for r in await cur.fetchall()]


# ===========================================================================
# 1. 数值越界：必须回传提示，不能静默收敛
# ===========================================================================
class TestClampWarnings:
    def test_empty_when_in_range(self):
        assert pf.clamp_warnings({"max_tokens": 8192, "temperature": 0.7,
                                  "timeout": 900, "concurrency": 4}) == []

    def test_empty_when_fields_absent(self):
        """未提交的字段不算越界（保持旧请求体兼容）。"""
        assert pf.clamp_warnings({}) == []

    def test_reports_out_of_range_fields(self):
        warns = pf.clamp_warnings({"concurrency": 8, "max_tokens": 999999,
                                   "temperature": 9, "timeout": 1})
        text = " ".join(warns)
        assert "并发数 8 超出允许范围 1~5" in text
        assert "最大 Token 999999" in text
        assert "温度 9" in text
        assert "超时（秒） 1" in text
        assert len(warns) == 4

    def test_reports_illegal_value(self):
        warns = pf.clamp_warnings({"max_tokens": "abc"})
        assert len(warns) == 1 and "取值非法" in warns[0]


class TestSaveConfigReturnsWarnings:
    async def test_save_clamps_and_warns(self, db_conn):
        res = await ai_router.save_config(_payload(concurrency=8, max_tokens=999999),
                                          db=db_conn)
        assert res["ok"] is True
        assert any("并发数" in w for w in res["warnings"])
        assert any("最大 Token" in w for w in res["warnings"])
        cur = await db_conn.execute(
            "SELECT concurrency, max_tokens FROM ai_config WHERE id=?", (res["id"],))
        row = await cur.fetchone()
        assert row["concurrency"] == 5      # 已收敛到全局上限
        assert row["max_tokens"] == 200000

    async def test_save_valid_values_has_no_warnings(self, db_conn):
        res = await ai_router.save_config(_payload(), db=db_conn)
        assert res["warnings"] == []


# ===========================================================================
# 2. 密钥清除
# ===========================================================================
class TestClearConfigKey:
    async def test_clear_removes_ciphertext(self, db_conn):
        await _insert(db_conn, "c1", key="sk-secret-0001")
        res = await ai_router.clear_config_key("c1", db=db_conn)
        assert res["ok"] is True and res["warning"] == ""
        cur = await db_conn.execute(
            "SELECT api_key_encrypted FROM ai_config WHERE id='c1'")
        assert (await cur.fetchone())[0] == ""
        rows = await _audit_rows(db_conn, "clear_key")
        assert len(rows) == 1 and rows[0]["config_id"] == "c1"
        # 脱敏断言：审计里不得出现明文密钥
        assert "sk-secret-0001" not in rows[0]["detail"]

    async def test_clear_is_idempotent_when_no_key(self, db_conn):
        await _insert(db_conn, "c1", key="")
        res = await ai_router.clear_config_key("c1", db=db_conn)
        assert res["unchanged"] is True
        assert await _audit_rows(db_conn, "clear_key") == []

    async def test_clear_active_config_warns(self, db_conn):
        await _insert(db_conn, "c1", is_active=1, key="sk-secret-0002")
        res = await ai_router.clear_config_key("c1", db=db_conn)
        assert res["warning"], "清除当前使用配置的 Key 必须给出告警，不能静默失效"

    async def test_clear_unknown_config_404(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.clear_config_key("ghost", db=db_conn)


# ===========================================================================
# 3. 配置变更审计
# ===========================================================================
class TestConfigAudit:
    async def test_save_writes_create_audit_without_plaintext(self, db_conn):
        res = await ai_router.save_config(_payload(api_key="sk-super-secret"),
                                         db=db_conn)
        rows = await _audit_rows(db_conn, "create")
        assert len(rows) == 1
        assert rows[0]["config_id"] == res["id"]
        assert "密钥=已填写" in rows[0]["detail"]
        assert "sk-super-secret" not in rows[0]["detail"]

    async def test_update_action_distinguished_from_create(self, db_conn):
        await _insert(db_conn, "c1")
        await ai_router.save_config(_payload(id="c1", is_active=False), db=db_conn)
        rows = await _audit_rows(db_conn, "update")
        assert len(rows) == 1 and rows[0]["config_id"] == "c1"

    async def test_toggle_and_delete_are_audited(self, db_conn):
        await _insert(db_conn, "main", is_active=1)
        await _insert(db_conn, "c2")
        await ai_router.toggle_config("c2", db=db_conn)
        assert len(await _audit_rows(db_conn, "toggle")) == 1
        # 删除：此时 c2 是当前使用，先切回 main 才能删
        await ai_router.toggle_config("main", db=db_conn)
        await ai_router.delete_config("c2", db=db_conn)
        rows = await _audit_rows(db_conn, "delete")
        assert len(rows) == 1 and rows[0]["detail"].startswith("删除配置")

    async def test_fallback_chain_audited(self, db_conn):
        for cid in ("a", "b"):
            await _insert(db_conn, cid)
        await ai_router.update_fallback_chain(
            FallbackChainUpdate(chain=[{"id": "a"}, {"id": "b"}]), db=db_conn)
        assert len(await _audit_rows(db_conn, "fallback_chain")) == 1

    async def test_audit_logs_endpoint_lists_and_filters(self, db_conn):
        await _insert(db_conn, "c1")
        await ai_router.toggle_config("c1", db=db_conn)
        await ai_router.clear_config_key("c1", db=db_conn)
        res = await ai_router.config_audit_logs(db=db_conn)
        assert res["total"] == 2
        assert {i["action"] for i in res["items"]} == {"toggle", "clear_key"}
        assert all(i["action_label"] for i in res["items"])

        only_clear = await ai_router.config_audit_logs(action="clear_key", db=db_conn)
        assert only_clear["total"] == 1
        assert only_clear["items"][0]["action"] == "clear_key"


# ===========================================================================
# 4/5. 场景模型路由
# ===========================================================================
class TestSceneRoutes:
    async def test_default_all_unconfigured(self, db_conn):
        res = await ai_router.list_scene_routes(db=db_conn)
        assert res["configured_count"] == 0
        assert all(i["config_id"] == "" for i in res["items"])
        assert {i["scene"] for i in res["items"]} == set(pf.KNOWN_SCENES)

    async def test_default_resolution_returns_none(self, db_conn):
        """未配置任何路由时，行为必须与引入该功能前一致（回落主配置）。"""
        assert await pf.resolve_scene_config("content_draft") is None

    async def test_put_rejects_unknown_scene(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.update_scene_route(
                SceneRouteUpdate(scene="not_a_scene", config_id="c1"), db=db_conn)

    async def test_put_rejects_unknown_config(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.update_scene_route(
                SceneRouteUpdate(scene="content_draft", config_id="ghost"), db=db_conn)

    async def test_put_then_resolve_and_clear(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek", model="deepseek-chat")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id="c1"), db=db_conn)
        cfg = await pf.resolve_scene_config("content_draft")
        assert cfg is not None and cfg["id"] == "c1"
        # 其他场景不受影响
        assert await pf.resolve_scene_config("facts_extract") is None

        listed = await ai_router.list_scene_routes(db=db_conn)
        assert listed["configured_count"] == 1
        routed = [i for i in listed["items"] if i["scene"] == "content_draft"][0]
        assert routed["model"] == "deepseek-chat" and routed["missing"] is False

        # 清除路由 → 恢复共用主配置
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id=""), db=db_conn)
        assert await pf.resolve_scene_config("content_draft") is None
        assert (await ai_router.list_scene_routes(db=db_conn))["configured_count"] == 0

    async def test_resolution_falls_back_when_config_deleted(self, db_conn):
        """路由指向的配置被删（DB 直删）时必须回落主配置，而不是报错。"""
        await _insert(db_conn, "c1")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="chart_fix", config_id="c1"), db=db_conn)
        await db_conn.execute("DELETE FROM ai_config WHERE id='c1'")
        await db_conn.commit()
        pf.invalidate_config_cache()
        assert await pf.resolve_scene_config("chart_fix") is None

        listed = await ai_router.list_scene_routes(db=db_conn)
        item = [i for i in listed["items"] if i["scene"] == "chart_fix"][0]
        assert item["missing"] is True

    async def test_route_writes_audit(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek", model="deepseek-chat")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="content_draft", config_id="c1"), db=db_conn)
        rows = await _audit_rows(db_conn, "scene_route")
        assert len(rows) == 1 and "content_draft" in rows[0]["detail"]


class TestChatWithFallbackUsesSceneRoute:
    async def test_primary_becomes_routed_config(self, db_conn, monkeypatch):
        # provider 名刻意取不常见值，避免被其他用例的可靠性统计污染
        await _insert(db_conn, "main", provider="mainprov", model="main-model",
                      is_active=1)
        await _insert(db_conn, "fast", provider="routeprov", model="routed-model")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="facts_extract", config_id="fast"), db=db_conn)
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")

        attempted: list[str] = []

        async def fake_attempt(c, messages, **kw):
            attempted.append(c.get("model", ""))
            return "ok", None

        monkeypatch.setattr(pf, "_attempt_candidate", fake_attempt)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}],
                                         scene="facts_extract")
        assert out == "ok"
        assert attempted[0] == "routed-model"

    async def test_scene_config_id_is_not_duplicated_in_fallback_chain(
            self, db_conn, monkeypatch):
        await _insert(db_conn, "scene", provider="sceneprov", is_active=1)
        pf.invalidate_config_cache()
        attempts = []

        async def _fake_attempt(candidate, messages, **kwargs):
            attempts.append(candidate.get("config_id", ""))
            raise RuntimeError("故障注入")

        async def _fallback():
            cur = await db_conn.execute("SELECT * FROM ai_config WHERE id='scene'")
            row = dict(await cur.fetchone())
            return [{
                "config_id": row["id"], "provider_name": row["provider_name"],
                "api_key": "sk-scene", "base_url": row["base_url"],
                "model": row["model"], "max_tokens": 8192,
                "temperature": 0.7, "timeout": 60,
                "request_mode": row.get("request_mode") or "normal",
            }]

        monkeypatch.setattr(pf, "_primary_api_key", lambda _cfg: "sk-scene")
        monkeypatch.setattr(pf, "_fallback_chain", _fallback)
        monkeypatch.setattr(pf, "_attempt_candidate", _fake_attempt)
        with pytest.raises(Exception):
            await pf.chat_with_fallback(
                [{"role": "user", "content": "hi"}], scene="content_draft")
        assert attempts == ["scene"], "同一场景配置不得因同时存在于 fallback 而重复尝试"

    async def test_no_route_uses_active_config(self, db_conn, monkeypatch):
        await _insert(db_conn, "main", provider="mainprov", model="main-model",
                      is_active=1)
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        attempted: list[str] = []

        async def fake_attempt(c, messages, **kw):
            attempted.append(c.get("model", ""))
            return "ok", None

        monkeypatch.setattr(pf, "_attempt_candidate", fake_attempt)
        await pf.chat_with_fallback([{"role": "user", "content": "hi"}],
                                    scene="facts_extract")
        assert attempted[0] == "main-model"

    async def test_explicit_ai_config_still_wins(self, db_conn, monkeypatch):
        """调用方显式传 ai_config 时必须优先（场景路由不得覆盖显式入参）。"""
        await _insert(db_conn, "main", provider="mainprov", model="main-model",
                      is_active=1)
        await _insert(db_conn, "fast", provider="routeprov", model="routed-model")
        await ai_router.update_scene_route(
            SceneRouteUpdate(scene="facts_extract", config_id="fast"), db=db_conn)
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        attempted: list[str] = []

        async def fake_attempt(c, messages, **kw):
            attempted.append(c.get("model", ""))
            return "ok", None

        monkeypatch.setattr(pf, "_attempt_candidate", fake_attempt)
        # 注意：ai_config 入参的形状是 **ai_config 表的行**（密钥列为 api_key_encrypted），
        # 而不是 provider 候选 dict —— 传错形状会因取不到 Key 被静默跳过。
        await pf.chat_with_fallback(
            [{"role": "user", "content": "hi"}],
            {"provider_name": "mainprov", "api_key_encrypted": encrypt_api_key("sk-x"),
             "base_url": "https://api.test.com/v1", "model": "explicit-model"},
            scene="facts_extract")
        assert attempted[0] == "explicit-model"


# ===========================================================================
# 6. 缓存代际号：读库期间被失效的旧结果不得写回缓存
# ===========================================================================
class TestCacheGenerationGuard:
    async def test_stale_active_config_not_cached(self, db_conn, monkeypatch):
        await _insert(db_conn, "c1", is_active=1)
        real_get_conn = pf.get_conn

        async def fake_get_conn():
            # 模拟「本协程正在读库时，另一个协程保存了配置并失效了缓存」
            pf.invalidate_config_cache()
            return await real_get_conn()

        monkeypatch.setattr(pf, "get_conn", fake_get_conn)
        cfg = await pf._load_active_config()
        assert cfg is not None and cfg["id"] == "c1"
        assert pf._config_cache["data"] is None, "过期结果被写回缓存会钉住一整个 TTL"

    async def test_stale_fallback_chain_not_cached(self, db_conn, monkeypatch):
        await _insert(db_conn, "c1", is_active=1)
        await _insert(db_conn, "c2", priority=1)
        real_get_conn = pf.get_conn

        async def fake_get_conn():
            pf.invalidate_config_cache()
            return await real_get_conn()

        monkeypatch.setattr(pf, "get_conn", fake_get_conn)
        chain = await pf._fallback_chain()
        assert any(c["provider_name"] == "openai" for c in chain)
        assert pf._fallback_cache["data"] is None

    async def test_invalidate_bumps_generation(self):
        before = pf._cache_generation
        pf.invalidate_config_cache()
        assert pf._cache_generation == before + 1
        assert pf._scene_route_cache["data"] is None


# ===========================================================================
# 8. 场景白名单漂移护栏
# ===========================================================================
class TestKnownScenesDrift:
    """KNOWN_SCENES 必须与代码中真实使用的 `scene="..."` 字面量一致。

    为什么必须锁：场景模型路由按 scene **精确匹配**。某个 AI 调用点若用了
    未登记的 scene，界面上就无法为它指定模型（配了也不生效）—— 这种漂移
    tsc / 单测都发现不了，只有本用例能拦住。
    """

    def _used_scenes(self) -> set[str]:
        """从 app/ 源码中提取**真实**的 `scene="..."` 字面量。

        ✅ 修复（2026-09-26 · 护栏自身缺陷）：旧实现用正则直接扫全文，
        会把**注释与文档字符串里出现的 `scene="content"`** 当成真实调用点。
        典型触发：修复「僵尸场景 content」时，我在 provider_factory 的注释里
        写了 `无任何 scene="content" 调用点`，护栏随即把 content 判为
        「已使用」→ `test_all_used_scenes_registered` 报「未登记」，
        形成"注释越详细、护栏越糊涂"的死循环。

        现改为**按行剥离注释**后再匹配：
        - 去掉 `#` 行注释（含 `#` 之后的全部内容）；
        - 跳过三引号文档字符串块。
        真实的 `scene="x"` 一定出现在可执行代码里，不会被这两者吞掉。
        """
        import re
        from pathlib import Path
        app_dir = Path(__file__).resolve().parents[1] / "app"
        pat = re.compile(r'scene\s*=\s*["\']([A-Za-z0-9_\-]+)["\']')
        # 用 chr 拼装三引号，避免本函数的 docstring 自身被提前闭合
        dq3 = chr(34) * 3
        sq3 = chr(39) * 3
        doc_markers = (dq3, sq3)
        used: set[str] = set()
        for p in app_dir.rglob("*.py"):
            in_doc: str | None = None
            for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw
                # ① 文档字符串块：整行剥离（同一行内开闭也一并处理）
                for mk in doc_markers:
                    if in_doc is None:
                        if mk in line:
                            # 同一行既开又闭：取两个标记之间的内容再继续
                            if line.count(mk) >= 2:
                                line = line.split(mk)[1].rsplit(mk, 1)[0]
                            else:
                                in_doc = mk
                                line = ""
                    else:
                        if mk in line:
                            in_doc = None
                        line = ""
                if in_doc is not None:
                    continue
                # ② 行注释：'#' 之后的都是说明文字
                code = line.split("#", 1)[0]
                used |= set(pat.findall(code))
        return used

    def test_all_used_scenes_registered(self):
        missing = sorted(self._used_scenes() - set(pf.KNOWN_SCENES))
        assert not missing, f"以下 scene 未登记到 KNOWN_SCENES（场景路由将无法覆盖）：{missing}"

    def test_registered_scenes_are_actually_used(self):
        """反向护栏：登记了却没人用 = 界面上的死选项（配置散落的另一种形态）。"""
        unused = sorted(set(pf.KNOWN_SCENES) - self._used_scenes())
        assert not unused, f"KNOWN_SCENES 中存在代码里未使用的 scene：{unused}"

    def test_scene_route_whitelist_covers_real_scene(self, db_conn):
        """真实存在的 scene 必须能配路由（不能因白名单过窄被 400 拒绝）。

        ✅ 2026-09-26：断言清单移除 "content"。该条目曾以「白名单会校验它
        确实被引用，故保留」为由留在 KNOWN_SCENES，实际**无任何
        scene="content" 调用点**（正文只用 content_draft / content_continue /
        content_shrink）——注释与事实相反，是 AGENTS.md §4.5 禁止的死选项。
        本用例自身正是它「应当被移除」的反向证据：若仍登记，恰恰能通过，
        说明这条断言无法区分「真实场景」与「僵尸场景」，
        真正的护栏是上方两条双向漂移用例（test_all_used_scenes_registered /
        test_registered_scenes_are_actually_used）。
        """
        for scene in ("content_draft", "content_continue", "content_shrink",
                      "outline_draft", "outline_level1", "outline_sublevel",
                      "chart_fix", "consistency_repair", "facts_extract"):
            assert scene in pf.KNOWN_SCENES, f"{scene} 应在白名单内"

    def test_zombie_total_dispatch_scenes_removed(self):
        """反向护栏：总调度型「僵尸场景」不得回到白名单。

        目录侧早已移除 "outline"、正文侧本轮移除 "content"：调用点全部细分
        打标后，总调度入口只会让用户在「场景模型路由」里配了却不生效。
        """
        for zombie in ("outline", "content"):
            assert zombie not in pf.KNOWN_SCENES, (
                f"僵尸总调度场景 {zombie!r} 不应登记：无任何 scene={zombie!r} 调用点，"
                f"配了也不生效")


class TestClientIp:
    class _Req:
        def __init__(self, peer, headers=None):
            self.client = type("Client", (), {"host": peer})()
            self.headers = headers or {}

    def test_untrusted_peer_cannot_spoof_forwarded_for(self, monkeypatch):
        monkeypatch.setattr(audit.settings, "trusted_proxy_ips", "", raising=False)
        req = self._Req("10.0.0.8", {"x-forwarded-for": "1.2.3.4"})
        assert audit.client_ip_of(req) == "10.0.0.8"

    def test_trusted_proxy_can_supply_forwarded_for(self, monkeypatch):
        monkeypatch.setattr(
            audit.settings, "trusted_proxy_ips", "10.0.0.8,127.0.0.1", raising=False)
        req = self._Req("10.0.0.8", {"x-forwarded-for": "1.2.3.4, 10.0.0.8"})
        assert audit.client_ip_of(req) == "1.2.3.4"


# ===========================================================================
# 7. 错误归因：配置已删除 ≠ Base URL 为空
# ===========================================================================
class TestCredentialResolutionErrors:
    async def test_missing_config_reports_reason(self, db_conn):
        base_url, api_key, err = await ai_router._resolve_conn_credentials(
            db_conn, ProviderModelsIn(config_id="ghost"))
        assert (base_url, api_key) == ("", "")
        assert "配置不存在" in err

    async def test_fetch_models_reports_deleted_config(self, db_conn):
        res = await ai_router.fetch_provider_models(
            ProviderModelsIn(config_id="ghost"), db=db_conn)
        assert res["ok"] is False
        assert "配置不存在" in res["error"]
        assert "Base URL" not in res["error"]

    async def test_custom_models_reports_deleted_config(self, db_conn):
        res = await ai_router.fetch_custom_models(
            ProviderModelsIn(config_id="ghost"), db=db_conn)
        assert res["ok"] is False and "配置不存在" in res["error"]

    async def test_existing_config_reads_db_credentials(self, db_conn):
        await _insert(db_conn, "c1", key="sk-from-db",
                      base_url="https://api.db.com/v1")
        base_url, api_key, err = await ai_router._resolve_conn_credentials(
            db_conn, ProviderModelsIn(config_id="c1"))
        assert err == ""
        assert base_url == "https://api.db.com/v1"
        assert api_key == "sk-from-db"

    async def test_changed_url_cannot_borrow_saved_key(self, db_conn):
        await _insert(db_conn, "c1", key="sk-secret",
                      base_url="https://api.db.com/v1")
        base_url, api_key, err = await ai_router._resolve_conn_credentials(
            db_conn, ProviderModelsIn(
                config_id="c1", base_url="https://attacker.example/v1"))
        assert base_url == ""
        assert api_key == ""
        assert "重新输入" in err

    async def test_changed_url_can_use_explicit_new_key(self, db_conn):
        await _insert(db_conn, "c1", key="sk-secret",
                      base_url="https://api.db.com/v1")
        base_url, api_key, err = await ai_router._resolve_conn_credentials(
            db_conn, ProviderModelsIn(
                config_id="c1", base_url="https://new.example/v1", api_key="sk-new"))
        assert err == ""
        assert base_url == "https://new.example/v1"
        assert api_key == "sk-new"

