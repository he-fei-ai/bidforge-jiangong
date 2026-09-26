"""AI 配置「请求方式」（普通请求 / 流式请求）专项测试。

功能背景：
    每个厂商平台配置新增「请求方式」下拉：
      - normal：普通请求（一次性等待完整响应，历史路径）
      - stream：流式请求（后端以 SSE 分片接收并**拼接为完整结果**后再返回）

    关键约束：**流式只影响后端与厂商之间的传输方式**，应用侧
    （章节正文、事实提取、图表生成等）仍等待完整结果后继续流程。

覆盖范围：
1. normalize_request_mode / request_mode_label 归一化（白名单、脏值、大小写、空格）
2. save_ai_config 落库 request_mode（新增/更新/脏值/缺省）
3. _fallback_chain 每条候选各自携带 request_mode（主配置配了流式不代表降级候选也支持）
4. chat_with_fallback 按请求方式分派：
   - stream：分片拼接成完整正文；应用侧拿到的仍是完整结果
   - stream 失败/空流：自动回退普通请求（请求方式不该成为可用性开关）
   - normal：只走 chat，不触碰流式接口
5. provider.stream 增强：参数覆盖 / usage 解析 / stream_options 被拒后自动重试
6. 「测试连接」按所选请求方式优先探测 + 另一种方式兜底
7. 导入导出与 /ai/health 贯通 request_mode
"""
import contextlib
import json

import pytest

import app.services.ai.provider_factory as pf
from app.routers import ai_config as ai_router
from app.models import AIConfigIn, AIConfigTest, ConfigImportIn
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


@pytest.fixture(autouse=True)
def reset_ai_caches():
    """进程内缓存/可靠性统计是全局态，用例之间必须互不污染（顺序相关幽灵失败）。"""
    pf._provider_reliability.clear()
    pf.invalidate_config_cache()
    pf.reset_stream_capability()
    pf.reset_quota_cooldown()
    yield
    pf._provider_reliability.clear()
    pf.invalidate_config_cache()
    pf.reset_stream_capability()
    pf.reset_quota_cooldown()


async def _insert(db, cid, provider="openai", model="gpt-4o", is_active=0,
                  priority=0, key="sk-test", base_url="https://api.test.com/v1",
                  request_mode="normal"):
    await db.execute(
        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url,"
        " model, is_active, priority, remark, timeout, concurrency, request_mode)"
        " VALUES (?,?,?,?,?,?,?,?,?,60,4,?)",
        (cid, provider, "pay_as_you_go", encrypt_api_key(key), base_url, model,
         is_active, priority, "", request_mode))
    await db.commit()


def _payload(**over):
    data = {
        "id": "", "provider_name": "deepseek", "plan": "pay_as_you_go",
        "api_key": "sk-abc", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "max_tokens": 8192, "temperature": 0.7,
        "timeout": 900, "concurrency": 4, "is_active": True,
        "priority": None, "remark": "", "request_mode": "normal",
    }
    data.update(over)
    return data


class _FakeProvider:
    """可控的 Provider 替身：记录两种调用方式各自被调用的次数。"""

    name = "fake"
    model = "m"
    temperature = 0.7
    last_usage: dict = {}
    last_finish_reason = ""

    def __init__(self, *, stream_pieces=None, stream_err=None,
                 chat_text="CHAT", chat_err=None):
        self.stream_pieces = list(stream_pieces or [])
        self.stream_err = stream_err
        self.chat_text = chat_text
        self.chat_err = chat_err
        self.stream_calls = 0
        self.chat_calls = 0

    async def stream(self, messages, **kw):
        self.stream_calls += 1
        if self.stream_err is not None:
            raise self.stream_err
        for piece in self.stream_pieces:
            yield piece

    async def chat(self, messages, **kw):
        self.chat_calls += 1
        if self.chat_err is not None:
            raise self.chat_err
        return self.chat_text


# ===========================================================================
# 1. 归一化
# ===========================================================================
class TestNormalizeRequestMode:
    def test_accepts_whitelist(self):
        assert pf.normalize_request_mode("normal") == "normal"
        assert pf.normalize_request_mode("stream") == "stream"

    def test_is_case_and_space_insensitive(self):
        assert pf.normalize_request_mode(" STREAM ") == "stream"
        assert pf.normalize_request_mode("Normal") == "normal"

    def test_dirty_values_fall_back_to_normal(self):
        """脏值不能原样落库 —— 否则界面显示乱码、运行时却静默按普通请求走。"""
        for raw in (None, "", "  ", "sse", "streaming", 123, ["stream"], "stream "):
            if isinstance(raw, str) and raw.strip().lower() == "stream":
                continue
            assert pf.normalize_request_mode(raw) == "normal", raw

    def test_label(self):
        assert pf.request_mode_label("stream") == "流式请求"
        assert pf.request_mode_label("normal") == "普通请求"
        assert pf.request_mode_label("garbage") == "普通请求"


# ===========================================================================
# 2. 落库
# ===========================================================================
class TestSaveRequestMode:
    async def test_default_is_normal(self, db_conn):
        cid = await pf.save_ai_config(_payload(request_mode=""))
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == "normal"

    async def test_stream_is_persisted(self, db_conn):
        cid = await pf.save_ai_config(_payload(request_mode="stream"))
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == "stream"

    async def test_dirty_value_normalized_on_write(self, db_conn):
        cid = await pf.save_ai_config(_payload(request_mode="SSE"))
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE id=?", (cid,))
        assert (await cur.fetchone())[0] == "normal"

    async def test_update_switches_mode(self, db_conn):
        await _insert(db_conn, "c1", is_active=1, request_mode="normal")
        await pf.save_ai_config(_payload(id="c1", request_mode="stream"))
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE id='c1'")
        assert (await cur.fetchone())[0] == "stream"

    async def test_route_passes_request_mode(self, db_conn):
        """路由层不能把新字段吞掉（Pydantic 模型缺字段时会被静默丢弃）。"""
        res = await ai_router.save_config(
            AIConfigIn(id="", provider_name="deepseek", plan="pay_as_you_go",
                       api_key="sk-x", base_url="https://api.deepseek.com/v1",
                       model="deepseek-chat", is_active=True,
                       request_mode="stream"),
            db=db_conn)
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE id=?", (res["id"],))
        assert (await cur.fetchone())[0] == "stream"


# ===========================================================================
# 3. 降级链逐条携带请求方式
# ===========================================================================
class TestFallbackChainCarriesMode:
    async def test_each_candidate_has_own_mode(self, db_conn):
        await _insert(db_conn, "main", provider="openai", is_active=1,
                      request_mode="stream")
        await _insert(db_conn, "alt", provider="deepseek", priority=1,
                      request_mode="normal")
        chain = await pf._fallback_chain()
        alt = next(c for c in chain if c["provider_name"] == "deepseek")
        assert alt["request_mode"] == "normal"

    async def test_legacy_rows_default_to_normal(self, db_conn):
        """旧库行 request_mode 为 NULL/空时，不能把 None 传下去。"""
        await _insert(db_conn, "main", is_active=1)
        await db_conn.execute(
            "UPDATE ai_config SET request_mode='' WHERE id='main'")
        await db_conn.commit()
        pf.invalidate_config_cache()
        cfg = await pf._load_active_config()
        assert pf.normalize_request_mode(cfg.get("request_mode")) == "normal"


# ===========================================================================
# 4. chat_with_fallback 按请求方式分派
# ===========================================================================
class TestChatDispatchByMode:
    async def test_stream_mode_returns_joined_full_text(self, db_conn, monkeypatch):
        """流式：分片拼接成**完整正文**后返回 —— 应用侧语义不变。"""
        await _insert(db_conn, "main", is_active=1, request_mode="stream")
        fake = _FakeProvider(stream_pieces=["你", "好", "，世界"], chat_text="不该被调用")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "你好，世界"
        assert fake.stream_calls == 1
        assert fake.chat_calls == 0

    async def test_normal_mode_never_touches_stream(self, db_conn, monkeypatch):
        await _insert(db_conn, "main", is_active=1, request_mode="normal")
        fake = _FakeProvider(chat_text="普通结果")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "普通结果"
        assert fake.stream_calls == 0 and fake.chat_calls == 1

    async def test_stream_failure_falls_back_to_chat(self, db_conn, monkeypatch):
        """平台不支持 stream 时，勾了流式也要能正常生成（不应成为可用性开关）。"""
        await _insert(db_conn, "main", is_active=1, request_mode="stream")
        fake = _FakeProvider(stream_err=RuntimeError("HTTP 400: stream not supported"),
                             chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "回退成功"
        assert fake.stream_calls == 1 and fake.chat_calls == 1

    async def test_empty_stream_falls_back_to_chat(self, db_conn, monkeypatch):
        """200 但一个分片都没有：与抛错同义，必须回退而不是把空串当成功。"""
        await _insert(db_conn, "main", is_active=1, request_mode="stream")
        fake = _FakeProvider(stream_pieces=[], chat_text="回退成功")
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "回退成功"

    async def test_both_paths_fail_raises(self, db_conn, monkeypatch):
        monkeypatch.setattr(pf.settings, "agnes_api_key", "")
        await _insert(db_conn, "main", is_active=1, request_mode="stream")
        fake = _FakeProvider(stream_err=RuntimeError("HTTP 500: boom"),
                             chat_err=RuntimeError("HTTP 500: boom"))
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])

    async def test_stream_json_mode_truncated_hint(self, db_conn, monkeypatch):
        """流式被 max_tokens 截断且正文为空时，提示要指向真正的原因。"""
        provider = pf._build_provider("deepseek", "sk-x",
                                     "https://api.deepseek.com/v1", "m")
        provider.last_finish_reason = "length"

        async def _empty_stream(messages, **kw):
            if False:  # pragma: no cover - 保持 async generator 语义
                yield ""

        provider.stream = _empty_stream  # type: ignore[assignment]
        with pytest.raises(RuntimeError) as ei:
            await pf._collect_stream(provider, [{"role": "user", "content": "x"}],
                                     temperature=None, json_mode=False, max_tokens=8)
        assert "max_tokens" in str(ei.value)


# ===========================================================================
# 5. provider.stream 增强
# ===========================================================================
class _FakeStreamResp:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.text = "bad request"

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("POST", "https://api.test.com/v1/chat/completions")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req, text=self.text))

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeHttpClient:
    """按顺序吐出预设响应，并记录每次请求的 payload。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads: list[dict] = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.payloads.append(dict(json or {}))
        resp = self._responses.pop(0) if self._responses else _FakeStreamResp([])
        return _FakeStreamCtx(resp)


def _sse(*chunks) -> list[str]:
    lines = [f"data: {json.dumps(c)}" if not isinstance(c, str) else c for c in chunks]
    lines.append("data: [DONE]")
    return lines


class TestProviderStreamEnhancements:
    @staticmethod
    def _provider(monkeypatch, responses):
        from app.services.ai.providers import openai_compatible as oc
        fake = _FakeHttpClient(responses)
        monkeypatch.setattr(oc, "get_async_client", lambda *a, **k: fake)
        provider = oc.OpenAICompatibleProvider("sk-x", "https://api.test.com/v1", "m",
                                               max_tokens=100, temperature=0.7)
        return provider, fake

    async def test_collects_pieces_and_usage(self, monkeypatch):
        provider, fake = self._provider(monkeypatch, [_FakeStreamResp(_sse(
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                      "prompt_tokens_details": {"cached_tokens": 3}}},
        ))])
        pieces = [p async for p in provider.stream([{"role": "user", "content": "x"}])]
        assert "".join(pieces) == "你好"
        assert provider.last_finish_reason == "stop"
        # ✅ usage 帧的 choices 为空数组，旧实现 IndexError 直接吞掉 → 用量恒为 0
        assert provider.last_usage == {"prompt_tokens": 10, "completion_tokens": 2,
                                       "cached_tokens": 3}

    async def test_overrides_are_forwarded(self, monkeypatch):
        """max_tokens / temperature / json_mode 覆盖必须生效（与 chat 口径一致）。"""
        provider, fake = self._provider(monkeypatch, [
            _FakeStreamResp(_sse({"choices": [{"delta": {"content": "ok"}}]}))])
        async for _ in provider.stream([{"role": "user", "content": "x"}],
                                       temperature=0.2, max_tokens=1234,
                                       json_mode=True):
            pass
        payload = fake.payloads[0]
        assert payload["max_tokens"] == 1234
        assert payload["temperature"] == 0.2
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}

    async def test_drops_stream_options_when_platform_rejects_it(self, monkeypatch):
        """stream_options 是可选参数：平台 400 时去掉重试，而不是判死流式。"""
        provider, fake = self._provider(monkeypatch, [
            _FakeStreamResp([], status_code=400),
            _FakeStreamResp(_sse({"choices": [{"delta": {"content": "ok"}}]})),
        ])
        pieces = [p async for p in provider.stream([{"role": "user", "content": "x"}])]
        assert "".join(pieces) == "ok"
        assert len(fake.payloads) == 2
        assert "stream_options" in fake.payloads[0]
        assert "stream_options" not in fake.payloads[1]

    async def test_other_400_is_not_swallowed(self, monkeypatch):
        provider, fake = self._provider(monkeypatch, [
            _FakeStreamResp([], status_code=401)])
        import httpx
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in provider.stream([{"role": "user", "content": "x"}]):
                pass
        assert len(fake.payloads) == 1


# ===========================================================================
# 6. 「测试连接」按请求方式探测
# ===========================================================================
class TestProbeRespectsRequestMode:
    @staticmethod
    def _patch(monkeypatch, provider):
        async def _pre_ok(url):
            return {"ok": True, "step": "ok", "host": "api.test.com",
                    "ip": "1.2.3.4", "port": 443}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(ai_router.models, "_dns_precheck_async", _pre_ok)
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: provider)

    async def test_normal_prefers_chat(self, db_conn, monkeypatch):
        fake = _FakeProvider(chat_text="ok", stream_pieces=["stream-ok"])
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x", request_mode="normal"),
            db=db_conn)
        assert res["ok"] is True
        assert res["mode"] == "chat_probe"
        assert res["request_mode"] == "normal"
        assert fake.stream_calls == 0, "普通请求模式不该先打流式接口"

    async def test_stream_prefers_stream(self, db_conn, monkeypatch):
        fake = _FakeProvider(chat_text="chat-ok", stream_pieces=["stream-ok"])
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x", request_mode="stream"),
            db=db_conn)
        assert res["ok"] is True
        assert res["mode"] == "stream_probe"
        assert res["request_mode"] == "stream"
        assert fake.chat_calls == 0

    async def test_normal_failure_falls_back_to_stream(self, db_conn, monkeypatch):
        """普通请求被平台限制时，用流式探测成功并提示改配置。"""
        fake = _FakeProvider(chat_err=RuntimeError("HTTP 400: non-stream not allowed"),
                             stream_pieces=["stream-ok"])
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x", request_mode="normal"),
            db=db_conn)
        assert res["ok"] is True
        assert res["mode"] == "stream_probe"
        assert "普通" in res["warning"]

    async def test_unspecified_mode_keeps_legacy_stream_first(self, db_conn, monkeypatch):
        """未指定请求方式（旧前端/脚本）沿用历史策略：流式优先，行为不回退。"""
        fake = _FakeProvider(chat_text="chat-ok", stream_pieces=["stream-ok"])
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x"),
            db=db_conn)
        assert res["mode"] == "stream_probe"

    async def test_mode_read_from_db_when_not_supplied(self, db_conn, monkeypatch):
        """编辑已有配置时前端没带请求方式 → 用库里存的那一条。"""
        await _insert(db_conn, "c1", request_mode="normal",
                      base_url="https://api.test.com/v1", model="m")
        fake = _FakeProvider(chat_text="ok", stream_pieces=["stream-ok"])
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(config_id="c1", api_key="sk-x"), db=db_conn)
        assert res["mode"] == "chat_probe"

    async def test_explicit_stream_failure_reports_stream_error(self, db_conn, monkeypatch):
        """显式选了流式：两条路都失败时，以流式错误为准（那才是实际链路）。"""
        fake = _FakeProvider(stream_err=RuntimeError("HTTP 402: Insufficient Balance"),
                             chat_err=RuntimeError("HTTP 400: model not exist"))
        self._patch(monkeypatch, fake)
        res = await ai_router.test_config(
            AIConfigTest(provider_name="custom", base_url="https://api.test.com/v1",
                         model="m", api_key="sk-x", request_mode="stream"),
            db=db_conn)
        assert res["ok"] is False
        assert res["category"] == "balance"
        assert "402" in res["raw"]


# ===========================================================================
# 7. 导入导出 / 健康快照
# ===========================================================================
class TestImportExportAndHealth:
    async def test_export_contains_request_mode(self, db_conn):
        await _insert(db_conn, "c1", request_mode="stream")
        res = await ai_router.export_config(db=db_conn)
        assert res["items"][0]["request_mode"] == "stream"

    async def test_import_normalizes_and_persists(self, db_conn):
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "request_mode": "stream",
        }])
        await ai_router.import_config(body, db=db_conn)
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE provider_name='zhipu'")
        assert (await cur.fetchone())[0] == "stream"

    async def test_import_legacy_file_without_mode(self, db_conn):
        """旧版本导出的文件没有该字段 → 默认普通请求，不能写 NULL。"""
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
        }])
        await ai_router.import_config(body, db=db_conn)
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE provider_name='zhipu'")
        assert (await cur.fetchone())[0] == "normal"

    async def test_import_dirty_mode_normalized(self, db_conn):
        body = ConfigImportIn(items=[{
            "provider_name": "zhipu", "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "request_mode": "SSE-please",
        }])
        await ai_router.import_config(body, db=db_conn)
        cur = await db_conn.execute(
            "SELECT request_mode FROM ai_config WHERE provider_name='zhipu'")
        assert (await cur.fetchone())[0] == "normal"

    async def test_health_reports_request_mode(self, db_conn):
        await _insert(db_conn, "c1", is_active=1, request_mode="stream")
        res = await ai_router.ai_health(db=db_conn)
        assert res["request_mode"] == "stream"
        assert res["request_mode_label"] == "流式请求"

    async def test_health_defaults_to_normal(self, db_conn):
        await _insert(db_conn, "c1", is_active=1)
        res = await ai_router.ai_health(db=db_conn)
        assert res["request_mode"] == "normal"
        assert res["request_mode_label"] == "普通请求"

    async def test_precheck_all_reports_mode(self, db_conn, monkeypatch):
        await _insert(db_conn, "c1", request_mode="stream")

        async def _fake(url):
            return {"ok": True, "step": "ok", "ip": "1.1.1.1"}

        monkeypatch.setattr(ai_router.connectivity, "_dns_precheck_async", _fake)
        res = await ai_router.precheck_all(db=db_conn)
        assert res["items"][0]["request_mode"] == "stream"
