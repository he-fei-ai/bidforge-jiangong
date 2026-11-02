"""R150 统一 AI 网关护栏（2026-10-31）

锁定本轮四类改造（全部加法式，既有调用面零改动）：
  A 错误分类单一事实源（error_taxonomy）—— 用生产库实拍错误文本做判据锁；
  B 网关级瞬态退避（_attempt_candidate）—— 429/5xx/超时 同候选指数退避
    严格一次、Retry-After 感知、非瞬态零重试、成功则不进配额冷却；
  C 降级告警（ai.degraded）—— 主配置未胜出 → system_logs WARN 结构化事件；
  D 场景分档路由继承（SCENE_TIERS / TIER_ANCHOR_SCENES）—— 显式 > 锚点继承 >
    主配置，双向覆盖护栏；
  E 统一门面（ai_gateway）—— chat 透传契约、refresh 热更新、分类 re-export
    同源；
  F 流式网关（chat_stream_with_fallback）—— 归一化事件序列、流失败→普通回退、
    部分增量产出后中断语义（不换候选重拼）、候选降级告警；
  G 高并发 + 429 风暴模拟 —— 全局信号量上限不被突破；退避开启后主候选
    自愈、降级候选零调用（对照退避关闭 = 旧行为）；记账完整无泄漏。

测试策略：直调真实函数体（不 mock 被测函数本身），仅注入
_build_provider / _resolve_chat_config / _fallback_chain / system_log_buffer
等外部依赖；asyncio.sleep 打桩记录退避时长（不真实等待）。
涉审计链路的用例一律挂 db_conn —— 审计缓冲满/到期会 flush 落库，
不 patch DB 会写到真实生产库（R150 首版实测踩中后全面加固）。
"""
import asyncio

import app.services.ai.provider_factory as pf
import app.services.observability as _obs
import pytest
from app.services.ai import ai_gateway as gw
from app.services.ai import error_taxonomy as et
from app.services.ai.providers.base import VendorHTTPError


# ============================================================
# 公共基建
# ============================================================
class FakeProvider:
    """脚本化假 Provider：chat/stream 按脚本逐次返回或抛错。"""

    name = "openai_compatible"

    def __init__(self, name="fake", model="fake-model", script=None,
                 stream_script=None):
        self.name = name
        self.model = model
        self.script = list(script or ["ok"])
        self.stream_script = list(stream_script or [])
        self.calls = 0
        self.stream_calls = 0
        self.last_usage = {"prompt_tokens": 11, "completion_tokens": 7,
                           "cached_tokens": 3}
        self.last_finish_reason = "stop"

    async def chat(self, messages, **kw):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, Exception):
            raise step
        return step

    async def stream(self, messages, **kw):
        self.stream_calls += 1
        for step in self.stream_script:
            if isinstance(step, Exception):
                raise step
            if step:
                yield step


class BudgetProvider(FakeProvider):
    """确定式假 Provider：**每条消息**首次调用抛 429，之后恒成功。

    状态键 = 消息内容本身，与并发交错/到达顺序无关 —— 共享实例在
    asyncio.gather 下的重试-成功配对是确定的（脚本式 pop 会因交错
    拿到彼此的步骤，预算式会把重试也计入预算，均不可复现）。
    """

    def __init__(self, name="fake", model="fake-model", budget=0):
        super().__init__(name=name, model=model, script=["成功正文"])
        self.remaining_failures = budget
        self.failed_once: set = set()

    async def chat(self, messages, **kw):
        self.calls += 1
        key = str(messages)
        if key not in self.failed_once and self.remaining_failures > 0:
            self.failed_once.add(key)
            self.remaining_failures -= 1
            raise VendorHTTPError(429, "rate limited")
        return "成功正文"


class Recorder:
    """system_log_buffer 替身：记录 enqueue 的事件。"""

    def __init__(self):
        self.events = []

    def enqueue(self, **event):
        self.events.append(event)
        return True


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    """每个用例前清空网关进程内状态（熔断/冷却/能力记忆/可靠性/审计缓冲）。"""
    pf._quota_cool_until.clear()
    pf._quota_last_probe.clear()
    pf._thinking_dead_until.clear()
    pf._stream_fail_streak.clear()
    pf._stream_unsupported_at.clear()
    pf._provider_reliability.clear()
    pf._det_dead_streak.clear()
    pf._audit_buffer.clear()
    pf.circuit_breaker.record_success("")   # 清空全部 provider 的熔断状态
    import app.services.ai.provider_factory as _pfmod
    with _pfmod._all_circuit_lock:
        _pfmod._all_circuit_consecutive = 0
        _pfmod._all_circuit_dead_until = 0.0
    yield
    pf.circuit_breaker.record_success("")


@pytest.fixture
def fake_crypto(monkeypatch):
    """解密打桩：真实 _build_candidates/_primary_api_key 拿到可用 Key。"""
    monkeypatch.setattr(pf, "decrypt_api_key", lambda v: "k" if v else "")


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(_obs, "system_log_buffer", r)
    return r


def _cand(provider="fakeA", model="m-a", cid="c1", primary=True, **kw):
    d = {"config_id": cid, "provider_name": provider, "api_key": "k",
         "api_key_encrypted": "enc-k",
         "base_url": "https://x/v1", "model": model, "max_tokens": 100,
         "temperature": 0.2, "timeout": 30, "request_mode": "normal"}
    if primary:
        d["_is_primary"] = True
    d.update(kw)
    return d


def _patch_providers(monkeypatch, factory):
    """monkeypatch pf._build_provider：按调用方工厂路由到假实例。"""
    def _build(pname, api_key, base_url, model, **kw):
        return factory(pname, model)
    monkeypatch.setattr(pf, "_build_provider", _build)


def _async_return(value):
    async def _fn(*a, **kw):
        return value
    return _fn


def _cfg_patch(monkeypatch, cfg):
    """patch pf._resolve_chat_config（真实为 async def，须回协程）。"""
    async def _fn(ai_config, scene):
        return cfg
    monkeypatch.setattr(pf, "_resolve_chat_config", _fn)


def _no_sleep(monkeypatch) -> list:
    """打桩 asyncio.sleep（记录退避时长，不真实等待）。"""
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


def _cands(n):
    return [_cand()] + ([_cand(provider="backup", model="m-b", cid="c2",
                               primary=False)] if n > 1 else [])


# ============================================================
# A · 错误分类（生产库实拍文本）
# ============================================================
class TestErrorTaxonomy:
    @pytest.mark.parametrize("text,cls", [
        # —— 生产库实拍（ai_audit_logs 近 7 天失败原因 TOP）——
        ('HTTP 429: {"error":{"code":"1302","message":"您的账户已达到速率限制，请您控制请求频率"}}', "rate_limit"),
        ('HTTP 429: {"error":{"code":"","message":"请登录platform进行实名认证以获取更高rpm"}}', "rate_limit"),
        ('HTTP 429: {"error":{"message":"今日文本额度已用完，当前处于限速模式"}}', "rate_limit"),  # 429 优先于正文语义
        ("ReadTimeout（无错误信息，常见于请求超时）", "timeout"),
        ("ReadTimeout", "timeout"),
        ("推理模型把 max_tokens(1536) 全部消耗在思考过程（finish_reason=length），正文为空：请调大 max_tokens", "thinking_exhausted"),
        ("厂商返回空 content（finish_reason=stop）: {'choices': []}", "empty_content"),
        ("配额冷却，跳过本次调用", "cooldown_skip"),
        ("思考吞噬冷却，跳过本次调用", "thinking_cooldown_skip"),
        ("熔断器 OPEN，跳过本次调用", "circuit_open"),
        ("运行时开关已禁用该厂商", "provider_disabled"),
        ('HTTP 401: {"message":"该令牌状态不可用"}', "auth"),
        ('HTTP 404: {"detail":"Function Not Found"}', "not_found"),
        ("HTTP 500: Internal Server Error", "server_error"),
        ("HTTP 503: Service Unavailable", "server_error"),
        ("所有 AI 提供商调用失败：无可用候选", "all_failed"),
        # —— 边界 ——
        ("", "other"),
        ("某种完全未知的错误形态", "other"),
    ])
    def test_classify_production_samples(self, text, cls):
        assert et.classify_error(text) == cls

    def test_transient_set_excludes_deterministic_dead(self):
        # 401/402/404/余额耗尽 绝不入瞬态集（R109 生产实证：重试 100% 无用）
        assert not ({"auth", "not_found", "quota_depleted", "json_mode",
                     "empty_content", "thinking_exhausted"}
                    & set(et.TRANSIENT_CLASSES))
        assert set(et.TRANSIENT_CLASSES) == {"rate_limit", "server_error",
                                             "timeout", "network"}

    def test_closed_class_set(self):
        assert set(et.ERROR_CLASSES) >= set(et.TRANSIENT_CLASSES)
        assert len(set(et.ERROR_CLASSES)) == len(et.ERROR_CLASSES)

    def test_vendor_http_error_text_compatible(self):
        # 文本形态与旧实现逐字一致（文本判据零感知）；类型继承
        # httpx.HTTPStatusError —— openai_compatible.stream() 的降级重试
        # 按该类型捕获（test_ai_config_request_mode 锁定），不可换基类。
        import httpx
        e = VendorHTTPError(429, "body", 3.0)
        assert isinstance(e, httpx.HTTPStatusError)
        assert str(e) == "HTTP 429: body"
        assert e.status == 429 and e.retry_after == 3.0
        assert e.response.status_code == 429       # 既有捕获点依赖的属性
        assert pf._status_from_error(e) == 429
        assert pf.is_non_retryable_error(VendorHTTPError(401, "bad")) is True
        assert pf.is_non_retryable_error(VendorHTTPError(429, "rl")) is False

    def test_source_anchor_definition_call_form(self):
        # 源码锚断「定义处调用形态」而非关键字（R144 教训②）
        import inspect
        src = __import__("inspect").getsource(et.classify_error)
        assert "json_mode_unsupported(s)" in src   # 中文判据复用 json_mode_compat
        assert '_has_status(s, "429")' in src


# ============================================================
# B · 瞬态退避（_attempt_candidate）
# ============================================================
class TestTransientBackoff:
    async def test_429_backoff_retry_recovers(self, monkeypatch, rec, db_conn):
        """429 一次 → 退避后同候选重试成功：不进配额冷却、不记熔断失败。"""
        sleeps = _no_sleep(monkeypatch)
        p = FakeProvider(script=[VendorHTTPError(429, "rate limited"), "成功正文"])
        _patch_providers(monkeypatch, lambda pn, m: p)
        text, err = await pf._attempt_candidate(
            _cand(), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is None and text == "成功正文"
        assert p.calls == 2                       # 严格一次重试
        assert len(sleeps) == 1
        assert 0.1 <= sleeps[0] <= pf._TRANSIENT_BACKOFF_CAP
        assert pf._quota_cooldown_active("fakeA") is False   # 自愈 → 零冷却
        assert pf.circuit_breaker.allow_request("fakeA") is True
        # 降级告警不该出现（单候选内自愈不算降级）
        assert not [e for e in rec.events if e.get("event") == "ai.degraded"]

    async def test_retry_after_header_respected(self, monkeypatch, db_conn):
        """Retry-After: 2 → 睡 2s；超过封顶 → 不等待直接失败换候选。"""
        sleeps = _no_sleep(monkeypatch)
        p = FakeProvider(script=[VendorHTTPError(429, "rl", retry_after=2.0), "ok"])
        _patch_providers(monkeypatch, lambda pn, m: p)
        text, err = await pf._attempt_candidate(
            _cand(), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is None and sleeps == [2.0]

        p2 = FakeProvider(script=[VendorHTTPError(429, "rl", retry_after=999.0)])
        _patch_providers(monkeypatch, lambda pn, m: p2)
        text, err = await pf._attempt_candidate(
            _cand(cid="c9"), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is not None and p2.calls == 1   # 等不起 → 不退避

    async def test_non_transient_never_backoff(self, monkeypatch, db_conn):
        """401 确定性错误零重试，直接失败并触发配额冷却（旧行为不变）。"""
        sleeps = _no_sleep(monkeypatch)
        p = FakeProvider(script=[RuntimeError("HTTP 401: 该令牌状态不可用")])
        _patch_providers(monkeypatch, lambda pn, m: p)
        text, err = await pf._attempt_candidate(
            _cand(), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is not None and p.calls == 1 and sleeps == []
        assert pf._quota_cooldown_active("fakeA") is True

    async def test_backoff_strict_once_then_failure_path(
            self, monkeypatch, db_conn):
        """持续 429：退避重试一次仍失败 → 走原失败路径（冷却 + 熔断记账）。"""
        sleeps = _no_sleep(monkeypatch)
        p = FakeProvider(script=[VendorHTTPError(429, "rl")] * 5)
        _patch_providers(monkeypatch, lambda pn, m: p)
        text, err = await pf._attempt_candidate(
            _cand(), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is not None
        assert p.calls == 2 and len(sleeps) == 1   # 严格一次
        assert pf._quota_cooldown_active("fakeA") is True

    async def test_timeout_backoff_retry(self, monkeypatch, db_conn):
        """ReadTimeout 属瞬态：同候选退避重试一次。"""
        _no_sleep(monkeypatch)
        p = FakeProvider(script=[TimeoutError(), "恢复后的正文"])
        _patch_providers(monkeypatch, lambda pn, m: p)
        text, err = await pf._attempt_candidate(
            _cand(), [{"role": "user", "content": "hi"}],
            temperature=None, json_mode=False, max_tokens=None,
            req_timeout=30, scene="test")
        assert err is None and p.calls == 2


# ============================================================
# C · 降级告警（ai.degraded）
# ============================================================
class TestDegradationAlert:
    async def test_primary_fails_fallback_wins_alert(self, monkeypatch, rec,
                                                     db_conn, fake_crypto):
        """主配置失败 → 备用胜出：一条 ai.degraded WARN，含 primary/winner 明细。"""
        _cfg_patch(monkeypatch, _cand(provider="primary", cid="c1"))
        monkeypatch.setattr(pf, "_fallback_chain", _async_return(
            [_cand(provider="backup", model="m-b", cid="c2", primary=False)]))
        by_name = {}

        def factory(pn, m):
            if pn not in by_name:
                # 401 = 非瞬态，主候选必然终败（500 会被 R150 退避自愈，
                # 就轮不到备用候选出场了 —— 这正是设计意图）
                by_name[pn] = FakeProvider(
                    name=pn, model=m,
                    script=([RuntimeError("HTTP 401: 令牌不可用")]
                            if pn == "primary" else ["备用正文"]))
            return by_name[pn]

        _patch_providers(monkeypatch, factory)
        text = await pf.chat_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft")
        assert text == "备用正文"
        degraded = [e for e in rec.events if e.get("event") == "ai.degraded"]
        assert len(degraded) == 1
        ev = degraded[0]
        assert ev["level"] == "WARN" and ev["status"] == "degraded"
        assert ev["detail"]["winner"]["provider"] == "backup"
        assert ev["detail"]["primary"]["provider"] == "primary"
        assert ev["detail"]["attempts_failed"] >= 1
        assert "content_draft" in ev["message"]

    async def test_primary_wins_no_alert(self, monkeypatch, rec, db_conn, fake_crypto):
        """主配置胜出：零 ai.degraded 事件。"""
        _cfg_patch(monkeypatch, _cand(provider="primary", cid="c1"))
        monkeypatch.setattr(pf, "_fallback_chain", _async_return([]))
        _patch_providers(monkeypatch,
                         lambda pn, m: FakeProvider(name=pn, model=m))
        text = await pf.chat_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft")
        assert text == "ok"
        assert not [e for e in rec.events if e.get("event") == "ai.degraded"]

    async def test_tools_path_alert(self, monkeypatch, rec, db_conn, fake_crypto):
        """工具调用链同样接线：主配置终败 → 备用胜出告警。"""
        _cfg_patch(monkeypatch, _cand(provider="primary", cid="c1"))
        monkeypatch.setattr(pf, "_fallback_chain", _async_return(
            [_cand(provider="backup", model="m-b", cid="c2", primary=False)]))
        by_name = {}

        class ToolsProvider(FakeProvider):
            def supports_tools(self):
                return True

        def factory(pn, m):
            if pn not in by_name:
                by_name[pn] = ToolsProvider(
                    name=pn, model=m,
                    script=([RuntimeError("HTTP 401: 令牌不可用")]
                            if pn == "primary"
                            else [{"role": "assistant", "content": None,
                                   "tool_calls": []}]))
            return by_name[pn]

        _patch_providers(monkeypatch, factory)
        result = await pf.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
            scene="outline_agent")
        assert result["role"] == "assistant"
        degraded = [e for e in rec.events if e.get("event") == "ai.degraded"]
        assert len(degraded) == 1
        assert degraded[0]["detail"]["winner"]["provider"] == "backup"


# ============================================================
# D · 场景分档路由继承
# ============================================================
class TestSceneTierRouting:
    def test_bijective_coverage_guard(self):
        """SCENE_TIERS 与 KNOWN_SCENES 双向一致 + tier 封闭 + 锚点在档内。"""
        assert set(pf.SCENE_TIERS) == set(pf.KNOWN_SCENES)
        assert set(pf.SCENE_TIERS.values()) <= set(pf.VALID_SCENE_TIERS)
        for tier, anchor in pf.TIER_ANCHOR_SCENES.items():
            assert anchor in pf.KNOWN_SCENES
            assert pf.SCENE_TIERS[anchor] == tier
        assert pf.scene_tier("outline_level1") == "complex"
        assert pf.scene_tier("不存在的场景") == ""

    async def test_anchor_inheritance(self, monkeypatch, db_conn):
        """同档锚点配了路由 → 未配置的同档场景继承；跨档不继承。"""
        await db_conn.execute(
            "INSERT INTO ai_config (id, provider_name, api_key_encrypted,"
            " base_url, model, is_active) VALUES ('cfg-anchor','openai','k',"
            "'https://x/v1','anchor-model',0)")
        await db_conn.commit()
        monkeypatch.setattr(
            pf, "load_scene_routes",
            _async_return({"outline_draft": "cfg-anchor"}))
        cfg = await pf.resolve_scene_config("outline_level1")
        assert cfg is not None and cfg["id"] == "cfg-anchor"
        # 不同档：content_draft（throughput）不继承 complex 锚点
        assert await pf.resolve_scene_config("content_draft") is None

    async def test_explicit_route_beats_inheritance(self, monkeypatch, db_conn):
        """显式路由永远优先于锚点继承。"""
        for cid, model in (("cfg-explicit", "explicit-model"),
                           ("cfg-anchor", "anchor-model")):
            await db_conn.execute(
                "INSERT INTO ai_config (id, provider_name, api_key_encrypted,"
                " base_url, model, is_active) VALUES"
                f" ('{cid}','openai','k','https://x/v1','{model}',0)")
        await db_conn.commit()
        monkeypatch.setattr(
            pf, "load_scene_routes",
            _async_return({"outline_draft": "cfg-anchor",
                           "outline_level1": "cfg-explicit"}))
        cfg = await pf.resolve_scene_config("outline_level1")
        assert cfg["id"] == "cfg-explicit"

    async def test_no_routes_falls_back_to_none(self, monkeypatch):
        """无任何路由：行为与引入继承前逐字一致（返回 None → 主配置）。"""
        monkeypatch.setattr(pf, "load_scene_routes", _async_return({}))
        assert await pf.resolve_scene_config("outline_level1") is None


# ============================================================
# E · 统一门面（ai_gateway）
# ============================================================
class TestGatewayFacade:
    async def test_chat_passthrough_contract(self, monkeypatch):
        seen = {}

        async def fake_chat(messages, **kw):
            seen.update(kw)
            seen["messages"] = messages
            return "正文"

        monkeypatch.setattr(pf, "chat_with_fallback", fake_chat)
        out = await gw.chat([{"role": "user", "content": "hi"}],
                            scene="content_draft", json_mode=True,
                            temperature=0.1, max_tokens=99, timeout=60)
        assert out == "正文"
        assert seen["scene"] == "content_draft" and seen["json_mode"] is True
        assert seen["max_tokens"] == 99 and seen["temperature"] == 0.1

    async def test_refresh_invalidates_all_caches(self):
        pf._config_cache["data"] = {"stale": True}
        pf._scene_route_cache["data"] = {"a": "b"}
        pf._fallback_cache["data"] = [{"stale": True}]
        out = await gw.refresh()
        assert out["ok"] is True
        assert pf._config_cache["data"] is None
        assert pf._scene_route_cache["data"] is None
        assert pf._fallback_cache["data"] is None

    async def test_classify_error_reexport_same_source(self):
        """门面 re-export 与叶子模块同源（防两份取值漂移）。"""
        assert gw.classify_error is et.classify_error
        assert gw.ERROR_CLASSES == et.ERROR_CLASSES
        assert gw.TRANSIENT_CLASSES == et.TRANSIENT_CLASSES

    def test_gateway_snapshot_shape(self):
        snap = gw.gateway_snapshot()
        assert {"live", "concurrency", "circuit_breaker",
                "scene_tiers", "tier_anchors"} <= set(snap)
        assert snap["tier_anchors"]["complex"] == "outline_draft"


# ============================================================
# F · 流式网关（chat_stream_with_fallback）
# ============================================================
class TestStreamGateway:
    async def _collect(self, gen):
        events = []
        async for ev in gen:
            events.append(ev)
        return events

    def _patch_chain(self, monkeypatch, n=1):
        cands = _cands(n)

        async def _rcfg(ai_config, scene):
            return cands[0]

        async def _bc(cfg):
            return cands

        async def _gate(c, scene):
            return cands, None
        monkeypatch.setattr(pf, "_resolve_chat_config", _rcfg)
        monkeypatch.setattr(pf, "_build_candidates", _bc)
        monkeypatch.setattr(pf, "_gate_candidates", _gate)

    async def test_normalized_event_sequence(self, monkeypatch, rec, db_conn):
        """真流式：delta 按序透传，done 带全文/usage/finish_reason。"""
        self._patch_chain(monkeypatch, 1)
        p = FakeProvider(stream_script=["你好", "，", "世界"])
        _patch_providers(monkeypatch, lambda pn, m: p)
        events = await self._collect(pf.chat_stream_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft"))
        deltas = [e for e in events if e["type"] == "delta"]
        done = [e for e in events if e["type"] == "done"]
        assert [d["text"] for d in deltas] == ["你好", "，", "世界"]
        assert len(done) == 1
        assert done[0]["text"] == "你好，世界"
        assert done[0]["usage"]["prompt_tokens"] == 11
        assert done[0]["finish_reason"] == "stop"
        assert done[0]["degraded_normal"] is False
        assert not [e for e in rec.events if e.get("event") == "ai.degraded"]

    async def test_stream_failure_falls_back_to_normal(
            self, monkeypatch, rec, db_conn):
        """零增量产出时流失败 → 同候选普通请求回退，单个 delta + degraded_normal。"""
        self._patch_chain(monkeypatch, 1)
        p = FakeProvider(script=["普通模式全文"],
                         stream_script=[RuntimeError(
                             "HTTP 400: stream not supported")])
        _patch_providers(monkeypatch, lambda pn, m: p)
        events = await self._collect(pf.chat_stream_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft"))
        deltas = [e for e in events if e["type"] == "delta"]
        done = events[-1]
        assert [d["text"] for d in deltas] == ["普通模式全文"]
        assert done["type"] == "done" and done["degraded_normal"] is True

    async def test_partial_yield_failure_aborts_whole_call(
            self, monkeypatch, db_conn):
        """部分增量产出后断流：整体失败，绝不换候选重拼（跨厂商拼接文本）。"""
        self._patch_chain(monkeypatch, 2)
        backup = FakeProvider(name="backup", model="m-b", script=["B 的全文"])

        def factory(pn, m):
            if pn == "backup":
                return backup
            return FakeProvider(name=pn, model=m,
                                stream_script=["部分", RuntimeError("连接中断")])
        _patch_providers(monkeypatch, factory)
        events = []
        with pytest.raises(RuntimeError, match="流式输出中断"):
            async for ev in pf.chat_stream_with_fallback(
                    [{"role": "user", "content": "hi"}], scene="content_draft"):
                events.append(ev)
        texts = [e["text"] for e in events if e["type"] == "delta"]
        assert "部分" in texts and not any(t == "B 的全文" for t in texts)
        assert backup.calls == 0   # 未换候选

    async def test_primary_fails_next_candidate_wins_with_alert(
            self, monkeypatch, rec, db_conn):
        """候选 1 全挂（流 401 非瞬态直接失败）→ 候选 2 胜出：降级告警 + done。"""
        self._patch_chain(monkeypatch, 2)
        bad = FakeProvider(name="fakeA", model="m-a",
                           script=[RuntimeError("HTTP 401: 令牌不可用")],
                           stream_script=[RuntimeError("HTTP 401: 令牌不可用")])
        good = FakeProvider(name="backup", model="m-b", script=["备用正文"],
                            stream_script=["备", "用", "正", "文"])

        def factory(pn, m):
            return bad if pn == "fakeA" else good
        _patch_providers(monkeypatch, factory)
        events = await self._collect(pf.chat_stream_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft"))
        done = events[-1]
        assert done["type"] == "done" and done["text"] == "备用正文"
        assert done["provider"] == "backup"
        degraded = [e for e in rec.events if e.get("event") == "ai.degraded"]
        assert len(degraded) == 1
        assert degraded[0]["detail"]["winner"]["provider"] == "backup"

    async def test_empty_stream_is_failure(self, monkeypatch, db_conn):
        """空流 = 失败（与 chat 空正文同语义），换下一候选。"""
        self._patch_chain(monkeypatch, 2)
        empty = FakeProvider(name="fakeA", model="m-a", script=["x"],
                             stream_script=[])
        good = FakeProvider(name="backup", model="m-b", script=["ok"],
                            stream_script=["ok"])

        def factory(pn, m):
            return empty if pn == "fakeA" else good
        _patch_providers(monkeypatch, factory)
        events = await self._collect(pf.chat_stream_with_fallback(
            [{"role": "user", "content": "hi"}], scene="content_draft"))
        assert events[-1]["type"] == "done" and events[-1]["provider"] == "backup"


# ============================================================
# G · 高并发 + 429 风暴模拟（验证降级与重试生效）
# ============================================================
class TestConcurrencyAndStorm:
    async def test_global_semaphore_bound(self, monkeypatch, db_conn, fake_crypto):
        """12 个并发调用：在飞峰值不超过全局信号量容量。"""
        cap = pf.concurrency_controller.current
        state = {"cur": 0, "peak": 0}

        class Slow(BudgetProvider):
            async def chat(self, messages, **kw):
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
                try:
                    await asyncio.sleep(0.02)
                    return "ok"
                finally:
                    state["cur"] -= 1

        monkeypatch.setattr(pf, "_build_provider",
                            lambda pn, k, u, m, **kw: Slow(name=pn, model=m))
        _cfg_patch(monkeypatch, _cand())
        monkeypatch.setattr(pf, "_fallback_chain", _async_return([]))
        await asyncio.gather(*[
            pf.chat_with_fallback([{"role": "user", "content": f"m{i}"}],
                                  scene="content_draft")
            for i in range(12)])
        assert state["peak"] <= cap
        assert state["peak"] >= min(2, cap)   # 确实并发过，不是串行

    async def test_429_storm_backoff_keeps_fallback_idle(
            self, monkeypatch, rec, db_conn, fake_crypto):
        """429 风暴对照实验（改造前 vs 改造后的微缩模型）：

        - 退避开启（R150 行为）：主候选 429 一次自愈 → 备用候选**零调用**；
        - 退避关闭（= 旧行为，_TRANSIENT_RETRY_MAX=0）：每次调用都打穿主候选
          → 备用候选被调用 10 次。
        同一批 429 故障，改造后降级链压力降为 0 ——「成功率提升、备用模型
        成本下降」的机制级证据。
        """
        _no_sleep(monkeypatch)
        _cfg_patch(monkeypatch, _cand())
        monkeypatch.setattr(pf, "_fallback_chain", _async_return(
            [_cand(provider="backup", model="m-b", cid="c2", primary=False)]))
        primary = BudgetProvider(name="fakeA", model="m-a", budget=10)
        backup = BudgetProvider(name="backup", model="m-b", budget=0)

        def factory(pn, m):
            return primary if pn == "fakeA" else backup
        _patch_providers(monkeypatch, factory)
        # —— R150 行为：退避开启 ——
        texts = await asyncio.gather(*[
            pf.chat_with_fallback([{"role": "user", "content": f"m{i}"}],
                                  scene="content_draft")
            for i in range(10)])
        assert all(t == "成功正文" for t in texts)
        assert primary.calls == 20                     # 10 任务 × (1 失败 + 1 重试)
        assert backup.calls == 0                       # 降级候选零调用

        # —— 旧行为：退避关闭（重置阶段1留下的状态，保证实验只差退避开关）——
        primary.remaining_failures = 10
        primary.failed_once.clear()
        primary.calls = 0
        backup.calls = 0
        pf._provider_reliability.clear()   # 否则 50% 成功率样本触发主配置后置
        pf._audit_buffer.clear()
        monkeypatch.setattr(pf, "_TRANSIENT_RETRY_MAX", 0)
        await asyncio.gather(*[
            pf.chat_with_fallback([{"role": "user", "content": f"m{i}"}],
                                  scene="content_draft")
            for i in range(10)])
        assert primary.calls == 10                     # 全部首发失败
        assert backup.calls == 10                      # 每次都打穿到备用

    async def test_storm_accounting_complete(self, monkeypatch, db_conn, fake_crypto):
        """风暴后记账完整：失败/成功审计行成对落库、in_flight 无泄漏。"""
        _no_sleep(monkeypatch)
        p = BudgetProvider(name="fakeA", model="m-a", budget=5)
        monkeypatch.setattr(pf, "_build_provider", lambda pn, k, u, m, **kw: p)
        _cfg_patch(monkeypatch, _cand())
        monkeypatch.setattr(pf, "_fallback_chain", _async_return([]))
        for i in range(5):
            text = await pf.chat_with_fallback(
                [{"role": "user", "content": f"m{i}"}], scene="content_draft")
            assert text == "成功正文"
        assert pf.get_ai_live_stats()["in_flight"] == 0
        await pf.flush_audit_buffer()
        cur = await db_conn.execute(
            "SELECT success, COUNT(*) n FROM ai_audit_logs"
            " WHERE scene='content_draft' AND provider_name='fakeA'"
            " GROUP BY success ORDER BY success")
        rows = {r[0]: r[1] for r in await cur.fetchall()}
        assert rows.get(0) == 5 and rows.get(1) == 5   # 429 失败行 + 成功行成对


# ============================================================
# I · /ai/stats 盘点扩展（行为级：P95 + by_error_class）
# ============================================================
class TestStatsInventoryExtension:
    async def test_stats_p95_and_error_class(self, monkeypatch, db_conn):
        """种子审计行 → ai_stats 返回 p95_duration_ok 与 by_error_class 结构化计数。"""
        from app.routers.ai_config.usage import ai_stats
        rows = [
            # fakeA：成功 2 次（10s、30s → P95=30）、429 失败 1 次
            ("fakeA", "m-a", 10.0, 1, "HTTP 429: x", "s1"),
            ("fakeA", "m-a", 30.0, 1, "", "s1"),
            ("fakeA", "m-a", 0.0, 0, "HTTP 429: 限流", "s1"),
            # fakeB：成功 1 次（5s）、超时失败 1 次
            ("fakeB", "m-b", 5.0, 1, "", "s2"),
            ("fakeB", "m-b", 0.0, 0, "ReadTimeout（无错误信息，常见于请求超时）", "s2"),
            # 门禁跳过（circuit_skipped）单独归类
            ("fakeC", "m-c", 0.0, 0, "配额冷却，跳过本次调用", "s3"),
        ]
        for pn, m, dur, ok, err, sc in rows:
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
                " prompt_tokens, completion_tokens, cached_tokens, duration,"
                " success, created_at, error, scene) VALUES"
                " (?, ?, ?, 'chat', 100, 50, 10, ?, ?,"
                " datetime('now','localtime'), ?, ?)",
                (f"r{pn}{dur}{ok}", pn, m, dur, ok, err, sc))
        # circuit_skipped 行
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
            " duration, success, created_at, error, scene) VALUES"
            " ('skip1', 'fakeC', 'm-c', 'circuit_skipped', 0, 0,"
            " datetime('now','localtime'), '配额冷却，跳过本次调用', 's3')")
        await db_conn.commit()

        out = await ai_stats(days=30, db=db_conn)
        by_p = {(p["provider_name"], p["model"]): p for p in out["by_provider"]}
        assert by_p[("fakeA", "m-a")]["p95_duration_ok"] == 30.0
        assert by_p[("fakeB", "m-b")]["p95_duration_ok"] == 5.0
        cls_map = {c["error_class"]: c for c in out["by_error_class"]}
        assert cls_map["rate_limit"]["failed_calls"] == 1
        assert cls_map["timeout"]["failed_calls"] == 1
        assert cls_map["cooldown_skip"]["skipped_calls"] == 1
        assert cls_map["rate_limit"]["label"] == "限流(429)"
        # 排序按 total 降序（rate_limit 与 timeout/cooldown 并列时的顺序不锁）


# ============================================================
# H · 源码形态锚（防「定义被删、测试还在 mock」假绿）
# ============================================================
class TestSourceAnchors:
    def test_provider_factory_anchors(self):
        import inspect
        src = inspect.getsource(pf)
        assert "瞬态错误（%s），%.1fs 后同候选退避重试" in src
        assert 'event="ai.degraded"' in src
        # 三个调用链路都接线了降级告警（chat / tools / stream）
        assert src.count("_notify_degradation(scene=scene") == 3
        assert "async def chat_stream_with_fallback(" in src
        assert "SCENE_TIERS: dict[str, str]" in src
        assert "def _inherit_tier_route(" in src

    def test_usage_p95_and_error_class_anchors(self):
        import inspect

        from app.routers.ai_config import usage as usage_mod
        src = inspect.getsource(usage_mod)
        # 断「定义处调用形态」而非关键字 —— 只断关键字会被自己的注释满足
        # （R149 教训③：M5/M8 首轮 MISSED 即因锚被字面满足）
        assert 'p["p95_duration_ok"] = _p95(' in src
        assert '"by_error_class": by_error_class,' in src
        assert 'classify_error(r["error"] or "")' in src

    def test_adapter_raises_vendor_http_error(self):
        import inspect

        from app.services.ai.providers import anthropic_compatible as ac
        from app.services.ai.providers import openai_compatible as oc
        assert inspect.getsource(oc).count("raise VendorHTTPError(") == 2
        assert "raise VendorHTTPError(" in inspect.getsource(ac)
