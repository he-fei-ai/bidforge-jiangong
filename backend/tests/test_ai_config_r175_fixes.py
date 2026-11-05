# -*- coding: utf-8 -*-
"""AI 配置模块 · R175（2026-11-03）修复回归测试。

覆盖本轮修复的 10 项缺陷中**可独立验证**的部分：

1. SSRF 防护（``normalize_base_url``）：回环 / 私网 / 链路本地 / 元数据 / 0.0.0.0 /
   localhost 被拒；公网通过；域名解析到内网被拒；开关打开后放行。
2. BUG-01：``disabled_providers`` JSON 损坏 → GET 不 500 且回传 ``disabled_error``；
   PUT 空集整体覆盖即完成恢复（``recovered=True``）。
3. BUG-02：坏密文 + ``legacy_key`` 能经 ``/key-rotation`` 恢复；缺 ``legacy_key``
   时明确失败且原密文不被覆盖。
4. BUG-03：``enabled=0`` 的配置不进入真实降级链；主配置不允许停用。
5. BUG-05：视觉候选按「运行时禁用厂商」过滤。
6. 上游错误体脱敏：``redact_sensitive_text`` / ``_error_summary`` 不落疑似 Key。
7. BUG-08/09/10：``env`` 32 字符上限；降级链部分提交后全表重排去重；
   ``api_key`` trim（空白 Key 不落库）。

硬约束：仅本文件 + 相关既有文件做限定回归，不跑全量。
"""

import socket

import app.services.ai.provider_factory as pf
import pytest
from app.models import (
    AIConfigIn,
    ConfigEnabledIn,
    DisabledProvidersIn,
    FallbackChainUpdate,
    KeyRotationIn,
)
from app.routers import ai_config as ai_router
from app.routers.ai_config import config as config_router
from app.routers.ai_config import runtime as runtime_router
from app.services.crypto import (
    decrypt_api_key,
    decrypt_with_legacy_key,
    encrypt_api_key,
    reencrypt_token,
)
from app.services.redaction import redact_sensitive_text
from cryptography.fernet import Fernet
from pydantic import ValidationError


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
    base_url="https://api.test.com/v1",
    enabled=1,
):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, env, is_active, enabled, priority, concurrency)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,4)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model, "", is_active, enabled, priority),
    )
    await db.commit()


async def _put_runtime_setting(db, key, value):
    await db.execute(
        "INSERT INTO ai_runtime_settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await db.commit()


def _payload(**over):
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
    return AIConfigIn(**data)


def _public_addrinfo(ip: str):
    """伪造 ``socket.getaddrinfo`` 返回，用于确定性的 DNS 场景测试。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


# ===========================================================================
# 1. SSRF 防护
# ===========================================================================
class TestSsrfGuard:
    @pytest.fixture(autouse=True)
    def _force_guard_on(self, monkeypatch):
        """默认关闭例外开关，避免本机 .env 的 AI_ALLOW_PRIVATE_BASE_URL 干扰断言。"""
        monkeypatch.setattr(pf.settings, "ai_allow_private_base_url", False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434/v1",  # 回环 IPv4
            "http://10.1.2.3/v1",  # 私网 10/8
            "http://172.16.0.1/v1",  # 私网 172.16/12
            "http://192.168.1.10/v1",  # 私网 192.168/16
            "http://169.254.169.254/latest/meta-data/",  # 链路本地 / 云元数据
            "http://0.0.0.0:8000/v1",  # 未指定地址
            "http://localhost:11434/v1",  # 本机主机名
            "http://[::1]:8080/v1",  # 回环 IPv6
        ],
    )
    def test_rejects_internal_addresses(self, url):
        with pytest.raises(ValueError) as ei:
            pf.normalize_base_url(url)
        assert "安全策略拒绝" in str(ei.value)

    def test_allows_public_ip_literal(self):
        assert pf.normalize_base_url("https://1.1.1.1/v1") == "https://1.1.1.1/v1"

    def test_allows_public_domain(self, monkeypatch):
        monkeypatch.setattr(pf.socket, "getaddrinfo", lambda *a, **kw: _public_addrinfo("93.184.216.34"))
        assert pf.normalize_base_url("https://api.example.com/v1") == "https://api.example.com/v1"

    def test_rejects_domain_resolving_to_private(self, monkeypatch):
        """域名解析到内网（DNS rebinding 的静态版本）必须拒绝。"""
        monkeypatch.setattr(pf.socket, "getaddrinfo", lambda *a, **kw: _public_addrinfo("10.0.0.5"))
        with pytest.raises(ValueError):
            pf.normalize_base_url("https://evil.example.com/v1")

    def test_switch_allows_private_when_enabled(self, monkeypatch):
        """例外开关打开后，本机自建模型服务地址可保存。"""
        monkeypatch.setattr(pf.settings, "ai_allow_private_base_url", True)
        assert pf.normalize_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"

    def test_error_message_points_to_env_switch(self):
        with pytest.raises(ValueError) as ei:
            pf.normalize_base_url("http://192.168.1.10/v1")
        assert "AI_ALLOW_PRIVATE_BASE_URL" in str(ei.value), "拒绝时应给出可操作的恢复指引，不得静默放行"


# ===========================================================================
# 2. BUG-01：损坏 JSON 不得 500，PUT 可恢复
# ===========================================================================
class TestDisabledProvidersCorruption:
    async def test_corrupt_json_get_returns_error_not_raise(self, db_conn):
        await _put_runtime_setting(db_conn, pf.RUNTIME_DISABLED_PROVIDERS_KEY, "[broken-json")
        pf.invalidate_config_cache()

        data = await runtime_router.get_runtime(db=db_conn)
        assert data["disabled_providers"] == [], "损坏值按空集处理，保证页面可用"
        assert data["disabled_error"], "必须回传错误说明供前端展示与指引"

    async def test_put_empty_recovers_corrupt_value(self, db_conn):
        await _put_runtime_setting(db_conn, pf.RUNTIME_DISABLED_PROVIDERS_KEY, "[broken-json")
        pf.invalidate_config_cache()

        res = await runtime_router.set_disabled_providers(DisabledProvidersIn(providers=[]), request=None, db=db_conn)
        assert res["ok"] is True and res["count"] == 0
        assert res["recovered"] is True, "本次整体覆盖应修复损坏值"
        assert res["disabled_error_before"], "恢复前的错误应可见（留痕）"

        after = await runtime_router.get_runtime(db=db_conn)
        assert after["disabled_error"] == ""
        assert after["disabled_providers"] == []


# ===========================================================================
# 3. BUG-03：enabled=0 不进入真实降级链
# ===========================================================================
class TestFallbackEnabledFilter:
    async def test_disabled_config_absent_from_chain(self, db_conn):
        await _insert(db_conn, "primary", is_active=1, priority=0)
        await _insert(db_conn, "c-on", provider="deepseek", model="deepseek-chat", priority=1)
        await _insert(db_conn, "c-off", provider="qwen", model="qwen-max", priority=2, enabled=0)
        pf.invalidate_config_cache()

        chain = await pf._fallback_chain()
        ids = {c.get("config_id") for c in chain}
        assert "c-on" in ids
        assert "c-off" not in ids, "已停用（enabled=0）的配置不得进入真实调用链"

    async def test_route_disable_excludes_from_chain(self, db_conn):
        await _insert(db_conn, "primary", is_active=1, priority=0)
        await _insert(db_conn, "c1", provider="deepseek", model="deepseek-chat", priority=1)

        res = await config_router.set_config_enabled("c1", ConfigEnabledIn(enabled=False), request=None, db=db_conn)
        assert res["enabled"] is False
        chain = await pf._fallback_chain()
        assert "c1" not in {c.get("config_id") for c in chain}

    async def test_primary_cannot_be_disabled(self, db_conn):
        await _insert(db_conn, "primary", is_active=1)
        with pytest.raises(Exception) as ei:
            await config_router.set_config_enabled("primary", ConfigEnabledIn(enabled=False), request=None, db=db_conn)
        assert getattr(ei.value, "status_code", None) == 400


# ===========================================================================
# 4. BUG-05：视觉候选过滤禁用厂商
# ===========================================================================
class TestVisionDisabledProviders:
    async def test_vision_candidates_skip_disabled_provider(self, db_conn, monkeypatch):
        await _insert(db_conn, "primary", provider="openai", model="gpt-4o", is_active=1, key="sk-vision")
        pf.invalidate_config_cache()

        async def _disabled():
            return {"openai"}

        monkeypatch.setattr(pf, "resolve_disabled_providers", _disabled)
        providers = await pf.get_vision_providers()
        assert all(p.api_key != "sk-vision" for p in providers), "被禁用厂商不得再收到 OCR/图片请求"


# ===========================================================================
# 5. 上游错误体脱敏
# ===========================================================================
class TestRedaction:
    def test_redacts_key_shapes(self):
        text = "HTTP 401: Authorization: Bearer sk-live-ABCDEF123456 " "api_key=super-secret-123456 bearer ABCDEFGH1234"
        out = redact_sensitive_text(text)
        assert "ABCDEF123456" not in out
        assert "super-secret-123456" not in out
        assert "ABCDEFGH1234" not in out
        assert "****" in out

    def test_redacts_fernet_like_token(self):
        key_like = Fernet.generate_key().decode()
        out = redact_sensitive_text(f"master key: {key_like}")
        assert key_like not in out
        assert "<fernet-key>" in out

    def test_error_summary_redacts_before_audit(self):
        msg = 'HTTP 401: {"error":"invalid api key",' '"api_key":"sk-live-ABCDEF123456"}'
        summary = pf._error_summary(Exception(msg))
        assert "ABCDEF123456" not in summary
        assert len(summary) <= 300


# ===========================================================================
# 6. BUG-02：坏密文恢复
# ===========================================================================
class TestKeyRecovery:
    def test_reencrypt_with_legacy_key_recovers(self):
        legacy = Fernet.generate_key()
        bad_ct = Fernet(legacy).encrypt(b"sk-live-secret").decode()
        assert decrypt_api_key(bad_ct) == "", "前置：当前主密钥解不开"

        fixed = reencrypt_token(bad_ct, legacy_key=legacy.decode())
        assert fixed
        assert decrypt_api_key(fixed) == "sk-live-secret"

    def test_reencrypt_without_legacy_key_returns_empty(self):
        legacy = Fernet.generate_key()
        bad_ct = Fernet(legacy).encrypt(b"sk-live-secret").decode()
        # 密文本身可被历史密钥解开，只是当前主密钥解不开 —— 缺 legacy_key 时无法迁移
        assert decrypt_with_legacy_key(bad_ct, legacy.decode()) == "sk-live-secret"
        assert reencrypt_token(bad_ct) == ""

    async def test_route_recovers_with_legacy_key(self, db_conn):
        legacy = Fernet.generate_key().decode()
        bad_ct = Fernet(legacy.encode()).encrypt(b"sk-live-secret").decode()
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, env, is_active, enabled, priority)"
            " VALUES ('c1','deepseek','pay_as_you_go',?,"
            " 'https://api.deepseek.com/v1','deepseek-chat','',0,1,0)",
            (bad_ct,),
        )
        await db_conn.commit()

        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt", legacy_key=legacy), db=db_conn)
        assert res["ok"] is True and res["migrated"] == 1

        cur = await db_conn.execute("SELECT api_key_encrypted FROM ai_config WHERE id='c1'")
        fixed = (await cur.fetchone())[0]
        assert decrypt_api_key(fixed) == "sk-live-secret"

    async def test_route_without_legacy_key_keeps_ciphertext(self, db_conn):
        legacy = Fernet.generate_key().decode()
        bad_ct = Fernet(legacy.encode()).encrypt(b"sk-live-secret").decode()
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted,"
            " base_url, model, env, is_active, enabled, priority)"
            " VALUES ('c1','deepseek','pay_as_you_go',?,"
            " 'https://api.deepseek.com/v1','deepseek-chat','',0,1,0)",
            (bad_ct,),
        )
        await db_conn.commit()

        res = await ai_router.key_rotation(KeyRotationIn(mode="reencrypt"), db=db_conn)
        assert res["failed"] == 1 and res["migrated"] == 0

        cur = await db_conn.execute("SELECT api_key_encrypted FROM ai_config WHERE id='c1'")
        assert (await cur.fetchone())[0] == bad_ct, "解不开的密文必须原样保留，绝不覆盖成空串"


# ===========================================================================
# 7. BUG-08/09/10：输入卫生与降级链重排
# ===========================================================================
class TestInputHygiene:
    async def test_blank_api_key_not_persisted(self, db_conn):
        cid = await pf.save_ai_config(
            {
                "provider_name": "deepseek",
                "plan": "pay_as_you_go",
                "api_key": "   ",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "is_active": False,
            }
        )
        cur = await db_conn.execute("SELECT api_key_encrypted FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == "", "纯空白 Key 不得落库"

    async def test_api_key_is_trimmed(self, db_conn):
        cid = await pf.save_ai_config(
            {
                "provider_name": "deepseek",
                "plan": "pay_as_you_go",
                "api_key": "  sk-test-value  ",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "is_active": False,
            }
        )
        cur = await db_conn.execute("SELECT api_key_encrypted FROM ai_config WHERE id=?", (cid,))
        assert decrypt_api_key((await cur.fetchone())[0]) == "sk-test-value"

    def test_env_over_32_rejected(self):
        with pytest.raises(ValidationError):
            AIConfigIn(provider_name="deepseek", model="deepseek-chat", env="a" * 33)

    async def test_partial_chain_submit_reorders_all_without_duplicates(self, db_conn):
        await _insert(db_conn, "c1", priority=0)
        await _insert(db_conn, "c2", priority=0)
        await _insert(db_conn, "c3", priority=0)

        await config_router.update_fallback_chain(FallbackChainUpdate(chain=["c2"]), request=None, db=db_conn)

        cur = await db_conn.execute("SELECT id, priority FROM ai_config")
        rows = {r["id"]: r["priority"] for r in await cur.fetchall()}
        assert rows["c2"] == 0
        assert sorted(rows.values()) == [0, 1, 2], "部分提交后未传入配置也必须接续重排，不得保留重复 priority"
