"""文本模型配置模块 R183 修复回归（2026-11-07）。

覆盖本轮修复项（每项先在旧实现上跑红，再修复转绿）：
  BUG-R183-01  连通性探测把上游原文（可能含 ``Bearer <key>``）原样放进
               ``/ai/config/test`` 的 ``error`` / ``warning``，未经脱敏；
  BUG-R183-02  探测超时硬编码 120s，完全不读表单/已存配置的 ``timeout``；
  BUG-R183-03  流式探测凑够片段即 ``break``，未显式关闭 async generator，
               底层 HTTP 连接要等 GC 才释放；
  BUG-R183-04  ``test_config`` / ``fetch-models`` 只听表单的 provider_name，
               只传 ``config_id`` 时不回读库里的 provider（Anthropic 类配置
               会被当成 OpenAI 兼容接口探测）；
  BUG-R183-05  配置尚未保存 Key 时改 Base URL，被误报成「必须重新输入 Key」，
               而不是真实的「API Key 为空」；
  DEBT-R183-01 ``decrypt_api_key`` 用 ``except (InvalidToken, Exception)``
               吞掉全部异常且无任何日志，非解密类故障不可观测。

同时保留一条热更新端到端用例：保存配置 → 失效缓存 → 运行时
``_load_active_config`` 取到新值（证明配置确实生效，而非仅落库）。
"""

import asyncio
import contextlib
import logging

import app.services.ai.provider_factory as pf
import pytest
from app.models import AIConfigIn, AIConfigTest, ProviderModelsIn
from app.routers import ai_config as ai_router
from app.routers.ai_config import connectivity
from app.routers.ai_config import models as models_router

_SECRET = "sk-live-R183SECRET1234567890"


@pytest.fixture(autouse=True)
def _patch_write_tx(db_conn, monkeypatch):
    """save_ai_config 走独立写池；测试指向内存连接。"""

    @contextlib.asynccontextmanager
    async def _fake():
        yield db_conn

    monkeypatch.setattr(pf, "write_tx_conn", _fake)


async def _insert(
    db,
    cid,
    *,
    provider="custom",
    model="m",
    base_url="https://api.test.com/v1",
    key="sk-test",
    timeout=60,
    is_active=0,
):
    from app.services.crypto import encrypt_api_key

    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, timeout, concurrency, enabled)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model, is_active, 0, timeout, 4),
    )
    await db.commit()


async def _pre_ok(url):
    return {"ok": True, "step": "ok", "host": "api.test.com", "ip": "1.2.3.4", "port": 443}


class _ProviderBase:
    model = "m"
    temperature = 0.7
    last_usage: dict = {}


# ===========================================================================
# BUG-R183-01 —— 上游原文回显必须脱敏
# ===========================================================================
class TestProbeErrorRedaction:
    async def test_success_warning_redacts_failed_probe_text(self, db_conn, monkeypatch):
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)

        class _P(_ProviderBase):
            async def stream(self, messages):
                raise RuntimeError(f"upstream 401 body: Authorization: Bearer {_SECRET}")
                yield ""  # pragma: no cover

            async def chat(self, messages, **kw):
                return "ok"

        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _P())
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1", model="m", api_key="sk-x"),
            db=db_conn,
        )
        assert res["ok"] is True
        warning = res.get("warning") or ""
        assert "流式" in warning, "原有回退提示不能丢"
        assert _SECRET not in warning, "warning 不得回显上游明文密钥"
        assert "sk-****" in warning, "密钥本体必须被掩码，但保留错误上下文便于排查"

    async def test_failure_error_redacts_upstream_text(self, db_conn, monkeypatch):
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)

        class _P(_ProviderBase):
            async def stream(self, messages):
                yield ""

            async def chat(self, messages, **kw):
                raise Exception(f"gateway body api_key={_SECRET}")

        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _P())
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1", model="m", api_key="sk-x"),
            db=db_conn,
        )
        assert res["ok"] is False
        assert _SECRET not in (res.get("error") or "")
        assert _SECRET not in (res.get("raw") or "")
        assert _SECRET not in (res.get("suggestion") or "")

    def test_models_fetch_error_is_redacted(self):
        """模型列表异常路径同一口径脱敏（不依赖具体网络错误形态）。"""
        from app.routers.ai_config._common import _classify_error

        c = _classify_error(Exception(f"proxy rejected api_key={_SECRET}"))
        assert _SECRET not in c["message"]
        assert _SECRET not in c["raw"]


# ===========================================================================
# BUG-R183-02 —— 探测超时跟随配置（不再硬编码 120）
# ===========================================================================
class TestProbeTimeoutWiring:
    def _capture(self, monkeypatch):
        seen = {}

        def _build(provider_name, api_key, base_url, model, **kw):
            seen.update(kw)
            seen["provider_name"] = provider_name

            class _P(_ProviderBase):
                async def stream(self, messages):
                    raise RuntimeError("stream unsupported")
                    yield ""  # pragma: no cover

                async def chat(self, messages, **kw):
                    return "ok"

            return _P()

        monkeypatch.setattr(pf, "_build_provider", _build)
        return seen

    async def test_saved_timeout_is_used(self, db_conn, monkeypatch):
        await _insert(db_conn, "c30", timeout=30)
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        seen = self._capture(monkeypatch)
        res = await ai_router.test_config(AIConfigTest(config_id="c30", api_key="sk-x"), db=db_conn)
        assert seen["timeout"] == 30, f"探测必须使用已存配置的 timeout，实际 {seen.get('timeout')}"
        assert res["ok"] is True
        assert res["config_timeout_s"] == 30

    async def test_form_timeout_overrides_saved(self, db_conn, monkeypatch):
        await _insert(db_conn, "c900", timeout=900)
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        seen = self._capture(monkeypatch)
        res = await ai_router.test_config(
            AIConfigTest(config_id="c900", api_key="sk-x", timeout=45), db=db_conn
        )
        assert seen["timeout"] == 45, "表单值应优先于库值"
        assert res["config_timeout_s"] == 45

    async def test_probe_budget_capped_for_frontend_contract(self, db_conn, monkeypatch):
        """配置 timeout=900 时探测仍受前端 260s 契约约束（上限 120s），并如实回传两者。"""
        await _insert(db_conn, "c900b", timeout=900)
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        seen = self._capture(monkeypatch)
        res = await ai_router.test_config(AIConfigTest(config_id="c900b", api_key="sk-x"), db=db_conn)
        assert seen["timeout"] == 120
        assert res["probe_timeout_s"] == 120 and res["config_timeout_s"] == 900

    async def test_invalid_timeout_falls_back_to_default(self, db_conn, monkeypatch):
        """手工构造的越界超时仍走 clamp 单一出口（下限 10s）。"""
        await _insert(db_conn, "c0", timeout=0)
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        seen = self._capture(monkeypatch)
        res = await ai_router.test_config(AIConfigTest(config_id="c0", api_key="sk-x", timeout=1), db=db_conn)
        assert seen["timeout"] == 10, "1 秒应按下限 10 秒收敛"
        assert res["config_timeout_s"] == 10


# ===========================================================================
# BUG-R183-03 —— 流式探测提前退出必须关闭 async generator
# ===========================================================================
class TestProbeStreamClosed:
    async def test_stream_generator_closed_after_early_break(self, db_conn, monkeypatch):
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        state = {"closed": False}

        class _P(_ProviderBase):
            async def stream(self, messages):
                try:
                    yield "x" * 100  # 超过 80 字符 → 探测立刻 break
                    await asyncio.sleep(0)
                finally:
                    state["closed"] = True

            async def chat(self, messages, **kw):
                return "unused"

        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _P())
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1", model="m", api_key="sk-x"),
            db=db_conn,
        )
        assert res["ok"] is True and res["mode"] == "stream_probe"
        assert state["closed"] is True, "提前 break 后必须显式 aclose，不能把连接留给 GC"


# ===========================================================================
# BUG-R183-04 —— 只传 config_id 时应回读库里的 provider_name
# ===========================================================================
class TestProviderFromDb:
    async def test_provider_name_read_from_db_when_omitted(self, db_conn, monkeypatch):
        await _insert(db_conn, "canth", provider="anthropic", model="claude-3", base_url="https://api.anthropic.com/v1")
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        seen = {}

        def _build(provider_name, api_key, base_url, model, **kw):
            seen["provider_name"] = provider_name

            class _P(_ProviderBase):
                async def stream(self, messages):
                    raise RuntimeError("stream unsupported")
                    yield ""  # pragma: no cover

                async def chat(self, messages, **kw):
                    return "ok"

            return _P()

        monkeypatch.setattr(pf, "_build_provider", _build)
        res = await ai_router.test_config(AIConfigTest(config_id="canth", api_key="sk-x"), db=db_conn)
        assert seen["provider_name"] == "anthropic"
        assert res["provider"] == "anthropic"


# ===========================================================================
# BUG-R183-05 —— 未存 Key 时改地址不应误报「必须重新输入 Key」
# ===========================================================================
class TestChangedUrlWithoutSavedKey:
    async def test_reports_empty_key_instead_of_url_change(self, db_conn):
        await _insert(db_conn, "nokey", key="", base_url="https://api.saved.com/v1")
        res = await ai_router.test_config(
            AIConfigTest(
                config_id="nokey",
                provider_name="custom",
                base_url="https://api.changed.com/v1",
                model="m",
                api_key="",
            ),
            db=db_conn,
        )
        assert res["ok"] is False
        assert res["error"] == "API Key 为空", f"实际：{res}"

    async def test_url_change_still_blocks_reuse_of_saved_key(self, db_conn):
        """阳性对照：库里有 Key 时，改地址仍必须拒绝复用旧 Key。"""
        await _insert(db_conn, "withkey", key="sk-saved", base_url="https://api.saved.com/v1")
        res = await ai_router.test_config(
            AIConfigTest(
                config_id="withkey",
                provider_name="custom",
                base_url="https://api.changed.com/v1",
                model="m",
                api_key="",
            ),
            db=db_conn,
        )
        assert res["ok"] is False and "Base URL 已变更" in res["error"]

    async def test_fetch_models_reports_empty_key_when_no_saved_key(self, db_conn):
        await _insert(db_conn, "nokey2", key="", base_url="https://api.saved.com/v1")
        res = await models_router.fetch_provider_models(
            ProviderModelsIn(base_url="https://api.changed.com/v1", api_key="", config_id="nokey2"), db=db_conn
        )
        assert res["ok"] is False and res["error"] == "API Key 不能为空", f"实际：{res}"


# ===========================================================================
# DEBT-R183-01 —— 解密异常不再静默
# ===========================================================================
class TestDecryptObservability:
    def test_unexpected_decrypt_failure_is_logged(self, caplog):
        import app.services.crypto as crypto

        def _boom():
            raise RuntimeError("cryptography backend broken")

        mp = pytest.MonkeyPatch()
        mp.setattr(crypto, "_get_fernet", _boom)
        try:
            with caplog.at_level(logging.WARNING, logger="crypto"):
                assert crypto.decrypt_api_key("gAAAA-not-a-token") == ""
        finally:
            mp.undo()
        assert crypto.decrypt_api_key and any("解密失败" in r.message for r in caplog.records), (
            "非 InvalidToken 的解密故障必须留痕，不能静默吞掉"
        )


# ===========================================================================
# 端到端：保存配置 → 失效缓存 → 运行时取到新值
# ===========================================================================
class TestHotReloadEndToEnd:
    async def test_saved_config_is_what_runtime_reads(self, db_conn):
        from app.services.crypto import decrypt_api_key

        res = await ai_router.save_config(
            AIConfigIn(
                provider_name="custom",
                base_url="https://api.test.com/v1",
                model="m1",
                api_key="sk-a",
                timeout=30,
                is_active=True,
            ),
            db=db_conn,
        )
        pf.invalidate_config_cache()
        cfg = await pf._load_active_config()
        assert cfg and cfg["model"] == "m1" and cfg["timeout"] == 30
        assert decrypt_api_key(cfg["api_key_encrypted"]) == "sk-a"

        await ai_router.save_config(
            AIConfigIn(
                id=res["id"],
                provider_name="custom",
                base_url="https://api.test.com/v1",
                model="m2",
                api_key="",
                timeout=45,
                is_active=True,
            ),
            db=db_conn,
        )
        pf.invalidate_config_cache()
        cfg2 = await pf._load_active_config()
        assert cfg2["model"] == "m2" and cfg2["timeout"] == 45
        assert decrypt_api_key(cfg2["api_key_encrypted"]) == "sk-a", "留空 Key 时必须保留旧密文"


# ===========================================================================
# R183-接线证据 —— 解析出的 timeout 必须真正进入发往厂商的 HTTP 请求
# ===========================================================================
class TestRequestTimeoutActuallyUsed:
    """前面只证明「传给了 _build_provider」，这里证明「传进了 httpx 请求」。

    注入点：``OpenAICompatibleProvider.chat`` 调用
    ``get_async_client(...)`` 后执行 ``client.post(..., timeout=self.timeout)``。
    用假客户端记录 post 的 timeout 实参，等价于观察真实出站请求的读超时。
    """

    class _FakeResponse:
        status_code = 200
        headers: dict = {}
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    class _FakeClient:
        def __init__(self):
            self.timeouts = []

        async def post(self, url, json=None, headers=None, timeout=None):
            self.timeouts.append(timeout)
            return TestRequestTimeoutActuallyUsed._FakeResponse()

    async def _probe(self, db_conn, monkeypatch, cid, row_timeout, form_timeout):
        await _insert(db_conn, cid, timeout=row_timeout)
        monkeypatch.setattr(connectivity, "_dns_precheck_async", _pre_ok)
        from app.services.ai.providers import openai_compatible as oai

        fake = self._FakeClient()
        monkeypatch.setattr(oai, "get_async_client", lambda base_url, proxy=None: fake)
        kwargs = {"timeout": form_timeout} if form_timeout is not None else {}
        res = await ai_router.test_config(
            AIConfigTest(config_id=cid, api_key="sk-x", request_mode="normal", **kwargs), db=db_conn
        )
        assert res["ok"] is True, res
        assert fake.timeouts, "探测必须真的发出 HTTP 请求"
        return fake.timeouts[0]

    async def test_db_timeout_20_reaches_http_request(self, db_conn, monkeypatch):
        """配置 timeout=20 → 真实请求 timeout 就是 20（不是硬编码 120/默认 900）。"""
        actual = await self._probe(db_conn, monkeypatch, "w20", 20, None)
        assert actual == 20, f"实际出站 timeout={actual}"
        assert actual not in (120, 900)

    async def test_form_timeout_beats_db(self, db_conn, monkeypatch):
        actual = await self._probe(db_conn, monkeypatch, "w45", 30, 45)
        assert actual == 45, f"表单 45 必须优先于库内 30，实际 {actual}"

    async def test_db_timeout_beats_default(self, db_conn, monkeypatch):
        actual = await self._probe(db_conn, monkeypatch, "w60", 60, None)
        assert actual == 60, f"库内 60 必须优先于默认 900，实际 {actual}"

    async def test_no_db_timeout_falls_back_to_default_probe_cap(self, db_conn, monkeypatch):
        """库内无有效 timeout → 默认 900 → 受前端 260s 契约封顶为 120。"""
        actual = await self._probe(db_conn, monkeypatch, "w0", 0, None)
        assert actual == 120, f"默认超时应收敛到探测上限 120，实际 {actual}"


# ===========================================================================
# 第 3 项 —— Provider 抛出点脱敏（异常文本会被下游 SSE/接口 str(e) 透出）
# ===========================================================================
class TestProviderOriginRedaction:
    async def test_upstream_401_body_redacted_before_raise(self, monkeypatch):
        from app.services.ai.providers import openai_compatible as oai
        from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider

        class _Resp:
            status_code = 401
            headers: dict = {}
            text = f'{{"error":"Incorrect API key provided: {_SECRET}"}}'

        class _Client:
            async def post(self, url, json=None, headers=None, timeout=None):
                return _Resp()

        monkeypatch.setattr(oai, "get_async_client", lambda base_url, proxy=None: _Client())
        provider = OpenAICompatibleProvider(api_key="sk-x", base_url="https://api.test.com/v1", model="m")
        with pytest.raises(RuntimeError) as ei:
            await provider.chat([{"role": "user", "content": "hi"}])
        assert _SECRET not in str(ei.value), f"异常文本仍含明文：{ei.value}"
        assert "sk-****" in str(ei.value)
