"""AI 配置模块测试（本轮深度审查修复项回归）

覆盖范围：
1. provider_factory.normalize_base_url —— 协议白名单 / 补协议 / 去尾斜杠 / 空主机
2. save_ai_config —— remark 落库、priority 不被动清、新配置 priority 递增、入参校验
3. _fallback_chain —— 非活跃配置参与降级（原实现恒为空 → 静默失效）
4. 审计日志 —— token 三列真实落库（原实现恒为 0）
5. 路由层 —— key 脱敏 / toggle 幂等 / 删除后 priority 重排 / 日志筛选分页 /
   配置导出导入 / 错误分类不再子串误判
"""
import contextlib
import json

import pytest

import app.services.ai.provider_factory as pf
from app.routers import ai_config as ai_router
from app.models import (
    AIConfigIn, AIConfigTest, ProviderModelsIn, AuditLogCleanup, ConfigImportIn,
    FallbackChainUpdate,
)
from app.services.crypto import encrypt_api_key


# ---------------------------------------------------------------------------
# fixtures
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
                  remark="", plan="pay_as_you_go"):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, remark, timeout, concurrency)"
        " VALUES (?,?,?,?,?,?,?,?,?,60,4)",
        (cid, provider, plan, encrypt_api_key(key), base_url, model,
         is_active, priority, remark))
    await db.commit()


def _base_payload(**over):
    data = {
        "id": "", "provider_name": "deepseek", "plan": "pay_as_you_go",
        "api_key": "sk-abc", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "max_tokens": 8192, "temperature": 0.7,
        "timeout": 900, "concurrency": 4, "is_active": True,
        "priority": None, "remark": "",
    }
    data.update(over)
    return data


# ===========================================================================
# 1. normalize_base_url
# ===========================================================================
class TestNormalizeBaseUrl:
    def test_adds_https_when_scheme_missing(self):
        assert pf.normalize_base_url("api.deepseek.com/v1") == "https://api.deepseek.com/v1"

    def test_strips_trailing_slash(self):
        assert pf.normalize_base_url("https://api.x.com/v1/") == "https://api.x.com/v1"

    def test_empty_returns_empty(self):
        assert pf.normalize_base_url("   ") == ""

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            pf.normalize_base_url("file:///etc/passwd")

    def test_rejects_missing_host(self):
        with pytest.raises(ValueError):
            pf.normalize_base_url("http:///v1")


# ===========================================================================
# 2. save_ai_config
# ===========================================================================
class TestSaveAiConfig:
    async def test_remark_is_persisted(self, db_conn):
        """原实现 UPDATE/INSERT 不含 remark 列，备注填写后静默丢失。"""
        cid = await pf.save_ai_config(_base_payload(remark="主力模型"))
        cur = await db_conn.execute("SELECT remark FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == "主力模型"

    async def test_priority_preserved_when_not_provided(self, db_conn):
        """priority=None 表示本次保存不改动降级顺序（否则每次保存都清零）。"""
        await _insert(db_conn, "c1", priority=5, is_active=1)
        await pf.save_ai_config(_base_payload(id="c1", is_active=True, remark="改备注"))
        cur = await db_conn.execute("SELECT priority, remark FROM ai_config WHERE id='c1'")
        row = await cur.fetchone()
        assert row[0] == 5
        assert row[1] == "改备注"

    async def test_new_config_appended_to_chain_end(self, db_conn):
        await _insert(db_conn, "c1", priority=0)
        await _insert(db_conn, "c2", priority=1)
        cid = await pf.save_ai_config(_base_payload(provider_name="zhipu",
                                                    model="glm-4-plus", is_active=False))
        cur = await db_conn.execute("SELECT priority FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == 2

    async def test_rejects_empty_model(self, db_conn):
        with pytest.raises(ValueError):
            await pf.save_ai_config(_base_payload(model="  "))

    async def test_rejects_custom_without_base_url(self, db_conn):
        with pytest.raises(ValueError):
            await pf.save_ai_config(_base_payload(provider_name="custom", base_url=""))

    async def test_rejects_coding_plan_without_base_url(self, db_conn):
        with pytest.raises(ValueError):
            await pf.save_ai_config(_base_payload(plan="coding_plan", base_url=""))

    async def test_clamps_out_of_range_numbers(self, db_conn):
        cid = await pf.save_ai_config(_base_payload(
            max_tokens=99999999, temperature=9.9, timeout=1, concurrency=999,
            is_active=False))
        cur = await db_conn.execute(
            "SELECT max_tokens, temperature, timeout, concurrency FROM ai_config WHERE id=?",
            (cid,))
        row = await cur.fetchone()
        assert row[0] == 200000
        assert row[1] == 2.0
        assert row[2] == 10
        # ✅ concurrency 上界跟随 settings.max_concurrency（2026-09-17 P0 收敛为 5），
        #    不再写死数值，避免调全局并发上限时测试失真
        from app.config import settings as _settings
        assert row[3] == _settings.max_concurrency

    async def test_keeps_old_key_when_api_key_empty(self, db_conn):
        await _insert(db_conn, "c1", key="sk-original", is_active=1)
        await pf.save_ai_config(_base_payload(id="c1", api_key="", remark="x"))
        cur = await db_conn.execute(
            "SELECT api_key_encrypted FROM ai_config WHERE id='c1'")
        from app.services.crypto import decrypt_api_key
        assert decrypt_api_key((await cur.fetchone())[0]) == "sk-original"


# ===========================================================================
# 3. _fallback_chain
# ===========================================================================
class TestFallbackChain:
    async def test_includes_inactive_configs(self, db_conn):
        """核心回归：全局只有 1 条 is_active=1 时，降级链不能为空。"""
        await _insert(db_conn, "main", provider="openai", is_active=1, priority=0)
        await _insert(db_conn, "alt1", provider="deepseek", is_active=0, priority=1)
        await _insert(db_conn, "alt2", provider="zhipu", is_active=0, priority=2)

        chain = await pf._fallback_chain()
        assert [c["provider_name"] for c in chain[:2]] == ["deepseek", "zhipu"]

    async def test_primary_excluded_and_ordered_by_priority(self, db_conn):
        await _insert(db_conn, "main", provider="openai", is_active=1)
        await _insert(db_conn, "b", provider="zhipu", priority=5)
        await _insert(db_conn, "a", provider="deepseek", priority=3)
        chain = await pf._fallback_chain()
        names = [c["provider_name"] for c in chain]
        assert "openai" not in names
        assert names.index("deepseek") < names.index("zhipu")

    async def test_skips_configs_without_key(self, db_conn):
        await _insert(db_conn, "main", provider="openai", is_active=1)
        await _insert(db_conn, "nokey", provider="zhipu", key="")
        chain = await pf._fallback_chain()
        assert all(c["provider_name"] != "zhipu" for c in chain)


# ===========================================================================
# 4. 审计日志 token 落库
# ===========================================================================
class TestAuditTokenAccounting:
    async def test_tokens_persisted(self, db_conn):
        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = 1e18  # 阻止 10s 兜底自动 flush
        await pf._log_audit("deepseek", "deepseek-chat", "chat", 1.5, True,
                            prompt_tokens=120, completion_tokens=45, cached_tokens=30)
        await pf.flush_audit_buffer()
        cur = await db_conn.execute(
            "SELECT prompt_tokens, completion_tokens, cached_tokens, success"
            " FROM ai_audit_logs")
        row = await cur.fetchone()
        assert tuple(row) == (120, 45, 30, 1)

    async def test_positional_legacy_signature_still_works(self, db_conn):
        pf._audit_buffer.clear()
        pf._audit_last_flush["ts"] = 1e18
        await pf._log_audit("x", "m", "chat", 0.5, False)
        await pf.flush_audit_buffer()
        cur = await db_conn.execute("SELECT prompt_tokens, success FROM ai_audit_logs")
        assert tuple(await cur.fetchone()) == (0, 0)

    def test_extract_usage_normalizes(self):
        class P:
            last_usage = {"prompt_tokens": 5, "completion_tokens": 6, "cached_tokens": 1}
        assert pf.extract_usage(P()) == {
            "prompt_tokens": 5, "completion_tokens": 6, "cached_tokens": 1}

        class Empty:
            last_usage = {}
        assert pf.extract_usage(Empty())["prompt_tokens"] == 0
        assert pf.extract_usage(object())["prompt_tokens"] == 0


class TestProviderUsageNormalization:
    def test_openai_style(self):
        from app.services.ai.providers.base import BaseProvider
        p = BaseProvider("k", "https://x/v1", "m")
        p._set_usage({"prompt_tokens": 7, "completion_tokens": 2,
                      "prompt_tokens_details": {"cached_tokens": 1}})
        assert p.last_usage == {"prompt_tokens": 7, "completion_tokens": 2, "cached_tokens": 1}

    def test_anthropic_style(self):
        from app.services.ai.providers.base import BaseProvider
        p = BaseProvider("k", "https://x/v1", "m")
        p._set_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3})
        assert p.last_usage == {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 3}

    def test_none_resets(self):
        from app.services.ai.providers.base import BaseProvider
        p = BaseProvider("k", "https://x/v1", "m")
        p._set_usage(None)
        assert p.last_usage["prompt_tokens"] == 0


# ===========================================================================
# 5. 路由层
# ===========================================================================
class TestConfigRoutes:
    async def test_get_config_masks_key(self, db_conn):
        await _insert(db_conn, "c1", key="sk-1234567890abcd", is_active=1)
        res = await ai_router.get_config(db=db_conn)
        item = res["items"][0]
        assert item["key_hint"] == "abcd"
        assert "sk-1234567890abcd" not in item["api_key"]
        assert item["has_key"] is True
        assert item["key_broken"] is False
        assert res["active_id"] == "c1"
        assert "api_key_encrypted" not in item

    async def test_get_config_flags_broken_key(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, api_key_encrypted) VALUES ('c1','openai','not-fernet')")
        await db_conn.commit()
        res = await ai_router.get_config(db=db_conn)
        assert res["items"][0]["key_broken"] is True
        assert res["items"][0]["has_key"] is False

    async def test_toggle_is_idempotent_when_already_active(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        res = await ai_router.toggle_config("c1", db=db_conn)
        assert res["is_active"] is True
        # 关键：不能因为重复点击把系统置为「无主配置」
        cur = await db_conn.execute("SELECT COUNT(*) FROM ai_config WHERE is_active=1")
        assert (await cur.fetchone())[0] == 1

    async def test_toggle_switches_active(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        await _insert(db_conn, "c2", is_active=0)
        await ai_router.toggle_config("c2", db=db_conn)
        cur = await db_conn.execute(
            "SELECT id FROM ai_config WHERE is_active=1")
        rows = await cur.fetchall()
        assert len(rows) == 1 and rows[0][0] == "c2"

    async def test_delete_resequences_priority(self, db_conn):
        await _insert(db_conn, "main", is_active=1, priority=0)
        for i, cid in enumerate(["a", "b", "c"], start=1):
            await _insert(db_conn, cid, priority=i)
        await ai_router.delete_config("b", db=db_conn)
        cur = await db_conn.execute(
            "SELECT id, priority FROM ai_config ORDER BY priority ASC")
        rows = [(r[0], r[1]) for r in await cur.fetchall()]
        assert rows == [("main", 0), ("a", 1), ("c", 2)]

    async def test_delete_active_config_rejected(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        with pytest.raises(Exception):
            await ai_router.delete_config("c1", db=db_conn)

    async def test_fallback_chain_rejects_unknown_id(self, db_conn):
        await _insert(db_conn, "c1", priority=0)
        with pytest.raises(Exception):
            await ai_router.update_fallback_chain(
                FallbackChainUpdate(chain=[{"id": "c1"}, {"id": "ghost"}]), db=db_conn)

    async def test_fallback_chain_assigns_sequential_priority(self, db_conn):
        for cid in ("a", "b", "c"):
            await _insert(db_conn, cid)
        res = await ai_router.update_fallback_chain(
            FallbackChainUpdate(chain=[{"id": "c"}, {"id": "a"}, {"id": "b"}]), db=db_conn)
        assert res["count"] == 3
        cur = await db_conn.execute("SELECT id FROM ai_config ORDER BY priority ASC")
        assert [r[0] for r in await cur.fetchall()] == ["c", "a", "b"]


class TestAuditLogRoutes:
    async def _seed_logs(self, db, rows):
        for i, (prov, action, ok) in enumerate(rows):
            await db.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
                " prompt_tokens, completion_tokens, cached_tokens, duration, success)"
                " VALUES (?,?,?,?,10,5,2,1.0,?)",
                (f"log{i}", prov, "m", action, ok))
        await db.commit()

    async def test_filters_and_pagination(self, db_conn):
        await self._seed_logs(db_conn, [
            ("deepseek", "chat", 1), ("deepseek", "chat", 0),
            ("zhipu", "extract", 1),
        ])
        res = await ai_router.audit_logs(limit=10, db=db_conn)
        assert res["total"] == 3
        assert set(res["providers"]) == {"deepseek", "zhipu"}
        assert set(res["actions"]) == {"chat", "extract"}

        res = await ai_router.audit_logs(limit=10, provider_name="deepseek", db=db_conn)
        assert res["total"] == 2

        res = await ai_router.audit_logs(limit=10, success="0", db=db_conn)
        assert res["total"] == 1

        res = await ai_router.audit_logs(limit=1, offset=1, db=db_conn)
        assert len(res["items"]) == 1 and res["total"] == 3

    async def test_cleanup_deletes_old_rows(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, action, success, created_at)"
            " VALUES ('old','x','chat',1, datetime('now','localtime','-100 days'))")
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, action, success)"
            " VALUES ('new','x','chat',1)")
        await db_conn.commit()
        res = await ai_router.cleanup_audit_logs(AuditLogCleanup(keep_days=30), db=db_conn)
        assert res["deleted"] == 1
        cur = await db_conn.execute("SELECT id FROM ai_audit_logs")
        assert [r[0] for r in await cur.fetchall()] == ["new"]

    async def test_cleanup_only_failed_keeps_success(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, action, success, created_at)"
            " VALUES ('oldfail','x','chat',0, datetime('now','localtime','-100 days'))")
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, action, success, created_at)"
            " VALUES ('oldok','x','chat',1, datetime('now','localtime','-100 days'))")
        await db_conn.commit()
        res = await ai_router.cleanup_audit_logs(
            AuditLogCleanup(keep_days=30, only_failed=True), db=db_conn)
        assert res["deleted"] == 1
        cur = await db_conn.execute("SELECT id FROM ai_audit_logs")
        assert [r[0] for r in await cur.fetchall()] == ["oldok"]


class TestStatsRoute:
    async def test_summary_includes_rates(self, db_conn):
        for i, (ok, pt, ct, cache) in enumerate([(1, 100, 50, 20), (0, 10, 0, 0)]):
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
                " prompt_tokens, completion_tokens, cached_tokens, duration, success)"
                " VALUES (?,?,?,?,?,?,?,1.0,?)",
                (f"l{i}", "deepseek", "m", "chat", pt, ct, cache, ok))
        await db_conn.commit()
        res = await ai_router.ai_stats(db=db_conn)
        s = res["summary"]
        assert s["total"] == 2
        assert s["total_tokens"] == 160
        assert s["cached_tokens"] == 20
        assert s["failed_count"] == 1
        assert s["success_rate"] == 50.0
        # ✅ 口径已修正（见 ai_config.ai_stats docstring）：缓存命中率改按
        #    cached / prompt 计算 —— 输出 token 天然无缓存，把 completion 计入
        #    分母会人为稀释命中率。此处 20 / (100+10) = 18.2%（旧断言按
        #    cached / 总 token = 12.5%，是口径修正前的期望值）。
        assert s["cache_hit_rate"] == 18.2

    async def test_empty_returns_none_rates_not_zero_division(self, db_conn):
        res = await ai_router.ai_stats(db=db_conn)
        assert res["summary"]["total"] == 0
        assert res["summary"]["success_rate"] is None
        assert res["summary"]["cache_hit_rate"] is None
        assert res["summary"]["avg_duration"] == 0.0


class TestConfigExportImport:
    async def test_export_excludes_api_key(self, db_conn):
        await _insert(db_conn, "c1", key="sk-secret-value")
        res = await ai_router.export_config(db=db_conn)
        assert res["count"] == 1
        assert res["api_key_included"] is False
        blob = str(res["items"])
        assert "api_key_encrypted" not in blob
        assert "sk-secret-value" not in blob

    async def test_import_creates_without_key_and_skips_duplicates(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1")
        body = ConfigImportIn(items=[
            {"provider_name": "deepseek", "model": "deepseek-chat",
             "base_url": "https://api.deepseek.com/v1"},          # 重复 → 跳过
            {"provider_name": "zhipu", "model": "glm-4-plus",
             "base_url": "https://open.bigmodel.cn/api/paas/v4"},  # 新增
            {"provider_name": "", "model": "x"},                   # 非法 → 跳过
        ])
        res = await ai_router.import_config(body, db=db_conn)
        assert res["imported"] == 1
        assert res["skipped"] == 2
        cur = await db_conn.execute(
            "SELECT api_key_encrypted, is_active FROM ai_config WHERE provider_name='zhipu'")
        row = await cur.fetchone()
        assert row[0] == ""      # 导入不带 Key
        assert row[1] == 0       # 导入后不自动启用

    async def test_import_overwrite_is_individually_rollbackable(self, db_conn):
        await _insert(db_conn, "c1", provider="deepseek", model="deepseek-chat",
                      base_url="https://api.deepseek.com/v1", remark="原备注")
        body = ConfigImportIn(overwrite=True, items=[{
            "provider_name": "deepseek", "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1", "remark": "导入覆盖",
            "max_tokens": 4096, "temperature": 0.2, "timeout": 120,
            "concurrency": 2, "request_mode": "normal",
        }])
        await ai_router.import_config(body, db=db_conn)
        cur = await db_conn.execute(
            "SELECT id, snapshot_json FROM ai_config_audit_logs "
            "WHERE action='import' AND config_id='c1' ORDER BY created_at DESC")
        row = await cur.fetchone()
        assert row is not None
        snap = json.loads(row[1])
        assert snap["before"]["remark"] == "原备注"
        assert snap["after"]["remark"] == "导入覆盖"
        listing = await ai_router.config_audit_logs(db=db_conn)
        item = next(i for i in listing["items"] if i["id"] == row[0])
        assert item["rollbackable"] is True

    async def test_import_rejects_empty_payload(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.import_config(ConfigImportIn(items=[]), db=db_conn)


class TestErrorClassification:
    def test_4040_not_misread_as_404(self):
        """原实现 `"404" in msg` 会把 4040 误判为 Base URL 错误。"""
        c = ai_router._classify_error(Exception("HTTP 4040: weird"))
        assert c["category"] != "url"

    def test_real_404_is_url_error(self):
        c = ai_router._classify_error(Exception("HTTP 404: {\"error\":\"not found\"}"))
        assert c["category"] == "url"

    def test_401_is_auth_error(self):
        assert ai_router._classify_error(Exception("HTTP 401: unauthorized"))["category"] == "auth"

    def test_429_is_ratelimit(self):
        assert ai_router._classify_error(Exception("HTTP 429: too many"))["category"] == "ratelimit"

    def test_503_is_provider_error(self):
        assert ai_router._classify_error(Exception("HTTP 503: down"))["category"] == "provider"

    def test_400_hints_model_name(self):
        c = ai_router._classify_error(Exception("HTTP 400: model not exist"))
        assert c["category"] == "request"
        assert "模型" in c["suggestion"]

    def test_structured_status_code_from_response(self):
        class Resp:
            status_code = 403
        class Err(Exception):
            response = Resp()
        assert ai_router._classify_error(Err("boom"))["category"] == "auth"


class TestPrecheckAndTestRoutes:
    async def test_test_config_rejects_empty_model(self, db_conn):
        res = await ai_router.test_config(
            AIConfigTest(provider_name="deepseek", base_url="https://api.deepseek.com/v1",
                         model="", api_key="sk-x"), db=db_conn)
        assert res["ok"] is False and "模型" in res["error"]

    async def test_test_config_rejects_bad_scheme(self, db_conn):
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="file:///etc/passwd",
                         model="m", api_key="sk-x"), db=db_conn)
        assert res["ok"] is False
        assert "http" in res["error"].lower()

    async def test_test_config_reads_model_from_db(self, db_conn):
        """编辑模式下表单未回填 model 时，应能从已存配置补齐（否则必然 400）。"""
        await _insert(db_conn, "c1", model="gpt-4o",
                      base_url="https://api.test.com/v1")
        res = await ai_router.test_config(
            AIConfigTest(config_id="c1", model="", base_url="", api_key="sk-x"),
            db=db_conn)
        # 走到 DNS 预检才可能失败（api.test.com 不存在），但不能是「模型名称为空」
        assert res.get("error") != "模型名称为空"

    async def test_precheck_without_base_url(self, db_conn):
        res = await ai_router.precheck_config(AIConfigTest(), db=db_conn)
        assert res["ok"] is False

    async def test_precheck_reads_url_from_config_id(self, db_conn, monkeypatch):
        await _insert(db_conn, "c1", base_url="https://api.test.com/v1")
        seen = {}

        async def _fake(url):
            seen["url"] = url
            return {"ok": True, "step": "ok", "host": "api.test.com", "ip": "1.2.3.4", "port": 443}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _fake)
        monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _fake)
        res = await ai_router.precheck_config(AIConfigTest(config_id="c1"), db=db_conn)
        assert res["ok"] is True
        assert seen["url"] == "https://api.test.com/v1"

    async def test_precheck_all_reports_each_config(self, db_conn, monkeypatch):
        await _insert(db_conn, "ok1", base_url="https://api.good.com/v1")
        await _insert(db_conn, "bad1", base_url="https://api.bad.com/v1",
                      provider="zhipu", model="glm-4-plus")
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, base_url) VALUES ('nourl','x','')")
        await db_conn.commit()

        async def _fake(url):
            if "bad" in url:
                return {"ok": False, "step": "dns", "message": "DNS 解析失败"}
            return {"ok": True, "step": "ok", "ip": "1.1.1.1"}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _fake)
        monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _fake)
        res = await ai_router.precheck_all(db=db_conn)
        assert res["total"] == 2
        assert res["ok_count"] == 1
        assert res["fail_count"] == 1
        assert res["skipped_no_url"] == 1
        assert {i["id"]: i["ok"] for i in res["items"]} == {"ok1": True, "bad1": False}

    async def test_fetch_models_requires_credentials(self, db_conn):
        res = await ai_router.fetch_provider_models(
            ProviderModelsIn(base_url="https://api.test.com/v1", api_key=""), db=db_conn)
        assert res["ok"] is False

    async def test_test_config_never_uses_saved_key_for_changed_base_url(self, db_conn):
        """已存 Key 只能发往其绑定地址，不能被 config_id + 新地址借走。"""
        await _insert(
            db_conn, "c1", key="sk-secret", base_url="https://saved.example.com/v1",
            model="gpt-4o")
        res = await ai_router.test_config(AIConfigTest(
            config_id="c1", base_url="https://attacker.example/v1",
            model="gpt-4o", api_key=""), db=db_conn)
        assert res["ok"] is False
        assert "API Key" in res["error"]
        assert "重新输入" in res["suggestion"]
        # 必须在 DNS/TCP 预检与 HTTP 请求之前拒绝，确保旧密钥没有外发机会。
        assert res.get("network") is None

    async def test_test_config_allows_changed_url_with_explicit_new_key(self, db_conn, monkeypatch):
        """显式输入新 Key 时仍可测试新地址，不破坏正常的自定义端点工作流。"""
        await _insert(
            db_conn, "c1", key="sk-secret", base_url="https://saved.example.com/v1",
            model="gpt-4o")

        async def _pre_ok(_url):
            return {"ok": True, "step": "ok", "host": "new.example.com", "ip": "1.2.3.4"}

        class _Provider:
            temperature = 0.7
            model = "gpt-4o"

            async def stream(self, messages):
                if False:
                    yield ""

            async def chat(self, messages, max_tokens=8192, temperature=0.7, **kwargs):
                return "连接成功"

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _Provider())
        res = await ai_router.test_config(AIConfigTest(
            config_id="c1", base_url="https://new.example.com/v1",
            model="gpt-4o", api_key="sk-new", request_mode="normal"), db=db_conn)
        assert res["ok"] is True


# ===========================================================================
# 6. 本轮（2026-09-17）深度探索修复项回归
# ===========================================================================
class TestNoZeroActiveConfigGuard:
    """「零主配置」是最高危的静默失效：接口 ok:true，但所有 AI 能力全挂。"""

    async def test_save_keeps_last_active_config(self, db_conn):
        """编辑唯一的主配置并关掉开关 → 必须保留 is_active=1 且返回 warning。"""
        await _insert(db_conn, "c1", is_active=1, provider="deepseek",
                      model="deepseek-chat")
        res = await ai_router.save_config(
            AIConfigIn(id="c1", provider_name="deepseek", plan="pay_as_you_go",
                       api_key="", base_url="https://api.deepseek.com/v1",
                       model="deepseek-chat", is_active=False),
            db=db_conn)
        assert res["ok"] is True
        assert res["warning"], "关闭唯一主配置必须给出告警，不能静默成功"
        cur = await db_conn.execute("SELECT is_active FROM ai_config WHERE id='c1'")
        assert (await cur.fetchone())[0] == 1

    async def test_save_allows_deactivation_when_other_active_remains(self, db_conn):
        await _insert(db_conn, "active", is_active=1)
        await _insert(db_conn, "spare", is_active=0, provider="zhipu",
                      model="glm-4-plus")
        res = await ai_router.save_config(
            AIConfigIn(id="spare", provider_name="zhipu", plan="pay_as_you_go",
                       base_url="https://open.bigmodel.cn/api/paas/v4",
                       model="glm-4-plus", is_active=False),
            db=db_conn)
        assert res["warning"] == ""
        cur = await db_conn.execute("SELECT is_active FROM ai_config WHERE id='spare'")
        assert (await cur.fetchone())[0] == 0

    async def test_toggle_warns_when_target_has_no_key(self, db_conn):
        """切到「没填 Key」的配置不再静默成功（下一次调用必然失败）。"""
        await _insert(db_conn, "c1", is_active=1)
        await _insert(db_conn, "nokey", key="", provider="zhipu", model="glm-4-plus")
        res = await ai_router.toggle_config("nokey", db=db_conn)
        assert res["ok"] is True
        assert "API Key" in res["warning"]

    async def test_toggle_warns_when_key_broken(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, model, api_key_encrypted,"
            " base_url, is_active) VALUES ('broken','zhipu','glm-4-plus',"
            "'not-fernet','https://open.bigmodel.cn/api/paas/v4',0)")
        await db_conn.commit()
        res = await ai_router.toggle_config("broken", db=db_conn)
        assert "无法解密" in res["warning"]

    async def test_toggle_no_warning_when_key_ok(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        await _insert(db_conn, "ok", provider="zhipu", model="glm-4-plus")
        assert (await ai_router.toggle_config("ok", db=db_conn))["warning"] == ""


class TestHealthStatusSemantics:
    """key_invalid（密文解不开）与 no_key（压根没填）此前被混为一谈。"""

    async def test_status_configured(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        res = await ai_router.ai_health(db=db_conn)
        assert res["status"] == "configured"
        assert res["hint"] == ""
        assert res["key_broken"] is False

    async def test_status_no_key_when_never_filled(self, db_conn):
        await _insert(db_conn, "c1", is_active=1, key="")
        res = await ai_router.ai_health(db=db_conn)
        assert res["status"] == "no_key"
        assert res["key_broken"] is False
        assert "API Key" in res["hint"]

    async def test_status_key_invalid_when_decrypt_fails(self, db_conn):
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, model, api_key_encrypted,"
            " base_url, is_active) VALUES ('c1','deepseek','deepseek-chat',"
            "'garbage','https://api.deepseek.com/v1',1)")
        await db_conn.commit()
        res = await ai_router.ai_health(db=db_conn)
        assert res["status"] == "key_invalid"
        assert res["key_broken"] is True
        assert "FERNET_KEY" in res["hint"]

    async def test_status_not_configured_when_none_active(self, db_conn):
        assert (await ai_router.ai_health(db=db_conn))["status"] == "not_configured"

    async def test_reports_live_concurrency_alongside_config(self, db_conn):
        """配置并发与运行时并发是两个概念，此前只回一个值却标着「当前并发」。"""
        await _insert(db_conn, "c1", is_active=1)
        res = await ai_router.ai_health(db=db_conn)
        assert "concurrency" in res and "live_concurrency" in res

def _patch_models_endpoint(monkeypatch, payload, status_code=200):
    """把 httpx.AsyncClient 与 DNS 预检都替换掉，直接走 `_fetch_models_list` 的解析分支。"""
    import httpx

    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = ""

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return FakeResp()

    async def _pre_ok(url):
        return {"ok": True, "step": "ok", "host": "api.test.com", "ip": "1.2.3.4"}

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _pre_ok)
    monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _pre_ok)


class TestModelListNormalization:
    """模型列表的 context 展示与排序（原实现会把 128000 显示成 128000K）。"""

    def test_context_formatting(self):
        assert ai_router._fmt_context(128000) == "128K"
        assert ai_router._fmt_context(10000000) == "10000K"
        assert ai_router._fmt_context(1000) == "1K"
        assert ai_router._fmt_context(1500) == "1.5K"
        assert ai_router._fmt_context(512) == "512"
        assert ai_router._fmt_context("") == ""
        assert ai_router._fmt_context("unknown") == "unknown"

    def test_context_tokens_sorts_bad_values_as_zero(self):
        assert ai_router._ctx_tokens("131072") == 131072
        assert ai_router._ctx_tokens(None) == 0
        assert ai_router._ctx_tokens(-5) == 0

    async def test_sorts_by_context_desc_then_name(self, monkeypatch):
        _patch_models_endpoint(monkeypatch, {"data": [
            {"id": "small", "context_length": 8000},
            {"id": "big", "context_length": 200000},
            {"id": "noctx"},
        ]})
        res = await ai_router._fetch_models_list("https://api.test.com/v1", "sk-x")
        assert res["ok"] is True
        assert [m["value"] for m in res["models"]] == ["big", "small", "noctx"]
        assert res["models"][0]["context"] == "200K"
        assert res["models"][0]["context_tokens"] == 200000
        assert res["truncated"] is False

    async def test_truncates_huge_model_list(self, monkeypatch):
        payload = [{"id": f"m{i:04d}"} for i in range(ai_router._MODEL_LIST_MAX + 25)]
        _patch_models_endpoint(monkeypatch, {"data": payload})
        res = await ai_router._fetch_models_list("https://api.test.com/v1", "sk-x")
        assert res["count"] == ai_router._MODEL_LIST_MAX
        assert res["truncated"] is True

    async def test_http_error_is_classified(self, monkeypatch):
        _patch_models_endpoint(monkeypatch, {}, status_code=401)
        res = await ai_router._fetch_models_list("https://api.test.com/v1", "sk-bad")
        assert res["ok"] is False
        assert res["suggestion"]




class TestFallbackChainInput:
    async def test_accepts_bare_id_strings(self, db_conn):
        for cid in ("a", "b"):
            await _insert(db_conn, cid)
        res = await ai_router.update_fallback_chain(
            FallbackChainUpdate(chain=["b", "a"]), db=db_conn)
        assert res["count"] == 2
        cur = await db_conn.execute("SELECT id FROM ai_config ORDER BY priority ASC")
        assert [r[0] for r in await cur.fetchall()] == ["b", "a"]

    async def test_duplicate_ids_deduplicated(self, db_conn):
        """重复 id 会让后一次 priority 覆盖前一次 → 保存成功但顺序与界面不符。"""
        for cid in ("a", "b"):
            await _insert(db_conn, cid)
        res = await ai_router.update_fallback_chain(
            FallbackChainUpdate(chain=[{"id": "b"}, {"id": "a"}, {"id": "b"}]),
            db=db_conn)
        assert res["count"] == 2
        cur = await db_conn.execute("SELECT id FROM ai_config ORDER BY priority ASC")
        assert [r[0] for r in await cur.fetchall()] == ["b", "a"]

    async def test_empty_chain_is_rejected(self, db_conn):
        with pytest.raises(Exception):
            await ai_router.update_fallback_chain(
                FallbackChainUpdate(chain=[]), db=db_conn)


class TestTestConfigBrokenKey:
    async def test_broken_saved_key_reports_decrypt_failure(self, db_conn):
        """密文解不开时不能说「API Key 为空」，否则用户去补填也没用。"""
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, model, api_key_encrypted,"
            " base_url) VALUES ('c1','deepseek','deepseek-chat','garbage',"
            "'https://api.deepseek.com/v1')")
        await db_conn.commit()
        res = await ai_router.test_config(
            AIConfigTest(config_id="c1", api_key="", base_url="", model=""), db=db_conn)
        assert res["ok"] is False
        assert "无法解密" in res["error"]
        assert res["category"] == "config"


class TestImportHardening:
    async def test_numbers_are_clamped(self, db_conn):
        """导入文件不受表单校验约束，越界值必须在此收敛。"""
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "max_tokens": 10 ** 9, "temperature": 99, "timeout": 1,
            "concurrency": 999, "plan": "not_a_plan",
        }])
        res = await ai_router.import_config(body, db=db_conn)
        assert res["imported"] == 1
        cur = await db_conn.execute(
            "SELECT plan, max_tokens, temperature, timeout, concurrency"
            " FROM ai_config WHERE provider_name='zhipu'")
        plan, mt, temp, to, conc = await cur.fetchone()
        assert plan == "pay_as_you_go"      # 白名单归一
        assert mt == 200000                 # _RANGE["max_tokens"] 上界
        assert temp == 2.0                  # temperature 上界
        assert to == 10                     # timeout 下界
        # ✅ concurrency 上界跟随 settings.max_concurrency（2026-09-17 P0 收敛为 5）
        from app.config import settings as _settings
        assert conc == _settings.max_concurrency

    async def test_warns_when_no_active_config_after_import(self, db_conn):
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
        }])
        res = await ai_router.import_config(body, db=db_conn)
        assert res["imported"] == 1
        assert "当前使用" in res["warning"]

    async def test_no_warning_when_active_config_exists(self, db_conn):
        await _insert(db_conn, "main", is_active=1)
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
        }])
        res = await ai_router.import_config(body, db=db_conn)
        assert res["warning"] == ""



class TestPrimaryKeyResolution:
    """主配置没填 Key 时不得借用「内置 agnes 的 Key」去请求其他厂商的地址。"""

    def test_uses_stored_key(self):
        cfg = {"provider_name": "deepseek",
               "api_key_encrypted": encrypt_api_key("sk-deepseek")}
        assert pf._primary_api_key(cfg) == "sk-deepseek"

    def test_does_not_borrow_agnes_key_for_other_vendor(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        assert pf._primary_api_key(
            {"provider_name": "deepseek", "api_key_encrypted": ""}) == ""

    def test_borrows_agnes_key_only_for_builtin_agnes(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        assert pf._primary_api_key({"provider_name": "agnes"}) == "sk-agnes"
        assert pf._primary_api_key({"provider_name": "AGNES"}) == "sk-agnes"

    def test_broken_ciphertext_is_not_silently_replaced(self, monkeypatch):
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        assert pf._primary_api_key(
            {"provider_name": "deepseek", "api_key_encrypted": "garbage"}) == ""

    async def test_chat_never_sends_agnes_key_to_other_vendor(self, db_conn, monkeypatch):
        """回归：原先 deepseek 会被喂上 agnes 的 Key → 必然 401，
        还会把这次注定失败的调用记到 deepseek 的熔断与审计上。"""
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        await _insert(db_conn, "main", provider="deepseek", model="deepseek-chat",
                      key="", is_active=1)
        built: list[tuple] = []

        class _FakeProvider:
            last_usage: dict = {}

            async def chat(self, *a, **kw):
                raise RuntimeError("HTTP 401: invalid api key")

        def _fake_build(name, key, base_url, model, **kw):
            built.append((name, key))
            return _FakeProvider()

        monkeypatch.setattr(pf, "_build_provider", _fake_build)
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert built, "内置 agnes 兜底候选应当被尝试"
        assert all(name != "deepseek" for name, _key in built)

    async def test_vision_providers_skip_keyless_primary(self, db_conn, monkeypatch):
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        await _insert(db_conn, "main", provider="deepseek", model="deepseek-chat",
                      key="", is_active=1)
        seen: list[str] = []

        class _FakeProvider:
            def supports_vision(self):
                return False

        def _fake_build(name, key, base_url, model, **kw):
            seen.append(name)
            return _FakeProvider()

        monkeypatch.setattr(pf, "_build_provider", _fake_build)
        await pf.get_vision_providers()
        assert "deepseek" not in seen


# ===========================================================================
# 7. 本轮（2026-09-17 第二轮）AI 配置模块修复项回归
# ===========================================================================
class TestProbeFallsBackToChat:
    """「测试连接」不能只认流式：正文生成走的是非流式 chat()。

    实测：平台不支持 stream（或流式参数受限）时，用户 Key/地址/模型全填对，
    测试连接却报「HTTP 400 模型名称不存在」，与真实可用性完全相反。
    """

    @staticmethod
    def _patch_provider(monkeypatch, *, stream_err, chat_result="", chat_err=None):
        async def _pre_ok(url):
            return {"ok": True, "step": "ok", "host": "api.test.com",
                    "ip": "1.2.3.4", "port": 443}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _pre_ok)

        class _P:
            model = "m"
            temperature = 0.7
            last_usage: dict = {}

            async def stream(self, messages):
                if stream_err:
                    raise stream_err
                yield ""

            async def chat(self, messages, **kw):
                if chat_err:
                    raise chat_err
                return chat_result

        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _P())
        return _P

    async def test_stream_failure_falls_back_to_chat(self, db_conn, monkeypatch):
        self._patch_provider(monkeypatch,
                             stream_err=RuntimeError("HTTP 400: stream not supported"),
                             chat_result="ok")
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x"), db=db_conn)
        assert res["ok"] is True
        assert res["mode"] == "chat_probe"
        assert res["response"].startswith("ok")
        assert "流式" in (res.get("warning") or "")

    async def test_stream_ok_reports_stream_mode(self, db_conn, monkeypatch):
        class _P:
            model = "m"
            temperature = 0.7
            last_usage: dict = {}

            async def stream(self, messages):
                yield "ok"

            async def chat(self, messages, **kw):
                return "unused"

        async def _pre_ok(url):
            return {"ok": True, "step": "ok", "host": "api.test.com", "ip": "1.2.3.4"}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _P())
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x"), db=db_conn)
        assert res["ok"] is True and res["mode"] == "stream_probe"

    async def test_both_fail_reports_chat_error(self, db_conn, monkeypatch):
        """两条路都失败时，以正文生成实际走的非流式错误为准（而不是流式的 400）。"""
        self._patch_provider(monkeypatch,
                             stream_err=RuntimeError("HTTP 400: stream not supported"),
                             chat_err=RuntimeError("HTTP 402: Insufficient Balance"))
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x"), db=db_conn)
        assert res["ok"] is False
        assert res["category"] == "balance", res
        assert "402" in res["raw"]


class TestStatsExcludesCircuitSkipped:
    """熔断跳过不是「调用失败」，不能计入调用次数与失败率。"""

    async def _seed(self, db):
        rows = [
            ("chat", 1, ""),
            ("chat", 0, "HTTP 429: rate limit"),
            ("circuit_skipped", 0, "熔断器 OPEN，跳过本次调用"),
        ]
        for i, (action, ok, err) in enumerate(rows):
            await db.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
                " prompt_tokens, completion_tokens, cached_tokens, duration, success, error)"
                " VALUES (?,?,?,?,?,?,?,1.0,?,?)",
                (f"k{i}", "agnes", "agnes-2.5-flash", action, 10, 0, 0, ok, err))
        await db.commit()

    async def test_calls_exclude_skipped(self, db_conn):
        await self._seed(db_conn)
        res = await ai_router.ai_stats(db=db_conn)
        p = res["by_provider"][0]
        assert p["calls"] == 2, "熔断跳过不应计为一次真实调用"
        assert p["fail_count"] == 1
        assert p["skipped_count"] == 1
        assert p["success_rate"] == 50.0
        assert res["summary"]["total"] == 2
        assert res["summary"]["failed_count"] == 1
        assert res["summary"]["skipped_count"] == 1

    async def test_by_error_excludes_skipped(self, db_conn):
        """「熔断器 OPEN」不该挤占失败原因 TOP（它压根不是一次请求）。"""
        await self._seed(db_conn)
        res = await ai_router.ai_stats(db=db_conn)
        errs = [e["err"] for e in res["by_error"]]
        assert errs and all("熔断器" not in e for e in errs), errs

    async def test_skipped_only_provider_still_visible(self, db_conn):
        """只被熔断跳过、从未真实调用过的配置也要出现在分布里。"""
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " prompt_tokens, completion_tokens, cached_tokens, duration, success, error)"
            " VALUES ('z1','zhipu','glm-4.7','circuit_skipped',0,0,0,0,0,'熔断器 OPEN')")
        await db_conn.commit()
        res = await ai_router.ai_stats(db=db_conn)
        names = {(p["provider_name"], p["model"]) for p in res["by_provider"]}
        assert ("zhipu", "glm-4.7") in names


class TestErrorSummary:
    """httpx 超时类异常 str() 为空 —— 失败原因不能整块消失。"""

    def test_keeps_real_message(self):
        assert pf._error_summary(RuntimeError("HTTP 402: x")) == "HTTP 402: x"

    def test_empty_message_falls_back_to_type(self):
        class _Silent(Exception):
            def __str__(self):
                return ""

        out = pf._error_summary(_Silent())
        assert "_Silent" in out

    def test_timeout_is_called_out(self):
        class _ReadTimeout(Exception):
            def __str__(self):
                return ""

        assert "超时" in pf._error_summary(_ReadTimeout())


class TestFallbackChainKeepsBuiltin:
    async def test_builtin_survives_truncation(self, db_conn, monkeypatch):
        """配置 ≥ ai_fallback_chain_max 条时，内置兜底不能被 chain[:N] 切掉。"""
        monkeypatch.setattr(pf.settings, "agnes_api_key", "sk-agnes")
        monkeypatch.setattr(pf.settings, "agnes_base_url", "https://api.agnes-ai.cn/v1")
        monkeypatch.setattr(pf.settings, "agnes_model", "agnes-2.5-flash")
        for i in range(4):
            await _insert(db_conn, f"c{i}", provider=f"p{i}", model=f"m{i}",
                          base_url=f"https://p{i}.test/v1")
        pf._provider_reliability.clear()
        pf.invalidate_config_cache()
        try:
            chain = await pf._fallback_chain()
        finally:
            pf._provider_reliability.clear()
            pf.invalidate_config_cache()
        assert any(c.get("api_key") == "sk-agnes" for c in chain), chain
        assert len(chain) <= 4


class TestAllCandidatesOpenStillProbes:
    async def test_one_probe_is_sent_instead_of_fail_fast(self, db_conn, monkeypatch):
        """全部候选被熔断时旧实现直接抛错、一次请求都不发（3180 条空转）。"""
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        await _insert(db_conn, "main", provider="deepseek", model="m1",
                      key="sk-1", is_active=1)
        await _insert(db_conn, "alt", provider="zhipu", model="m2", key="sk-2")
        pf._provider_reliability.clear()
        pf.invalidate_config_cache()
        cb = pf.circuit_breaker
        for name in ("deepseek", "zhipu"):
            for _ in range(cb.failure_threshold + 1):
                cb.record_failure(name)
        assert not cb.allow_request("deepseek") and not cb.allow_request("zhipu")

        calls: list[str] = []

        class _Fake:
            model = "m"
            last_usage: dict = {}

            async def chat(self, *a, **kw):
                calls.append("chat")
                return "ok"

        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _Fake())
        try:
            out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        finally:
            pf._provider_reliability.clear()
            pf.invalidate_config_cache()
            cb.record_success("")   # 复位，避免污染其它用例
        assert out == "ok"
        assert calls, "全熔断时必须放行一次探测，而不是整批空转失败"

