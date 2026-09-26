# -*- coding: utf-8 -*-
"""调用次数优化 · 第 2 批回归锁（2026-09-22）

覆盖：
  O2  配额类错误冷却：429/402/403/404 命中后冷却窗口内跳过（零 HTTP），
      每 probe_every 秒放行一次探测；全冷却且节流未到时直接抛错；
      探测成功自动接回（冷却清除）
  O3  思考吞噬重试：finish_reason=length 且正文为空 → 翻倍 max_tokens 重试一次
"""
import time

import pytest

import app.services.ai.provider_factory as pf
from app.config import settings


class _FakeProvider:
    """可控 Provider 替身；built_mt 记录构建时的 max_tokens。"""

    temperature = 0.7
    last_finish_reason = ""

    def __init__(self, *, behavior, built_mt=0):
        self.behavior = behavior      # "ok" / "fail:402" / "thinking"
        self.built_mt = built_mt
        self.chat_calls = 0
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5,
                           "cached_tokens": 0}
        self.name = "fake"
        self.model = "m"

    async def chat(self, messages, **kw):
        self.chat_calls += 1
        if self.behavior == "ok":
            return "正文内容"
        if self.behavior == "fail:402":
            raise RuntimeError('HTTP 402: {"error":{"message":"Insufficient Balance"}}')
        if self.behavior == "thinking":
            raise RuntimeError(
                f"推理模型把 max_tokens({self.built_mt}) 全部消耗在思考过程"
                f"（finish_reason=length），正文为空：请调大 max_tokens 或改用非推理模型")
        raise RuntimeError("boom")


@pytest.fixture
def isolated(monkeypatch):
    """隔离 chat_with_fallback 外部依赖（不读 DB / 不写审计 / 清全局态）。"""

    async def _noop_flush():
        return None

    monkeypatch.setattr(pf, "_flush_audit_buffer", _noop_flush)
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()
    pf.reset_quota_cooldown()
    yield monkeypatch
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()
    pf.reset_quota_cooldown()


def _wire_single(monkeypatch, provider_factory_fn, *, max_tokens=4096):
    """单候选接线：provider_factory_fn(max_tokens) -> _FakeProvider"""
    built = []

    async def _cfg():
        return {"provider_name": "p1", "api_key": "k", "base_url": "https://x",
                "model": "m", "max_tokens": max_tokens, "temperature": 0.7,
                "timeout": 60}

    async def _chain():
        return []

    def _factory(pname, *a, **kw):
        prov = provider_factory_fn(kw.get("max_tokens") or max_tokens)
        built.append(prov.built_mt)
        return prov

    monkeypatch.setattr(pf, "_load_active_config", _cfg)
    monkeypatch.setattr(pf, "_fallback_chain", _chain)
    monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")
    monkeypatch.setattr(pf, "_build_provider", _factory)
    return built


class TestQuotaErrorClassification:
    def test_quota_errors(self):
        for msg in ('HTTP 429: rate limit', "HTTP 402: Insufficient Balance",
                    "HTTP 403: no valid authorization", "HTTP 404: Not Found"):
            assert pf._is_quota_error(RuntimeError(msg)) is True

    def test_transient_errors_not_quota(self):
        for msg in ("HTTP 500: boom", "ReadTimeout", "HTTP 502: bad gateway"):
            assert pf._is_quota_error(RuntimeError(msg)) is False

    def test_thinking_exhausted(self):
        e = RuntimeError("推理模型把 max_tokens(1536) 全部消耗在思考过程"
                         "（finish_reason=length），正文为空：请调大 max_tokens")
        assert pf._is_thinking_exhausted(e) is True
        assert pf._is_thinking_exhausted(RuntimeError("HTTP 500")) is False
        # 空正文但非 length（内容过滤）不算思考吞噬
        assert pf._is_thinking_exhausted(
            RuntimeError("厂商返回空 content（finish_reason=stop）")) is False


class TestQuotaCooldownState:
    def test_active_after_failure(self, monkeypatch):
        monkeypatch.setattr(pf, "QUOTA_PROBE_EVERY", 10.0)
        assert pf._quota_cooldown_active("p1") is False
        pf._note_quota_failure("p1", RuntimeError("HTTP 402"))
        assert pf._quota_cooldown_active("p1") is True
        pf.reset_quota_cooldown()
        assert pf._quota_cooldown_active("p1") is False

    def test_probe_throttle_releases_once(self, monkeypatch):
        """冷却中每 probe_every 秒放行一次探测。"""
        monkeypatch.setattr(pf, "QUOTA_PROBE_EVERY", 0.05)
        pf._note_quota_failure("p1", RuntimeError("HTTP 402"))
        assert pf._quota_cooldown_active("p1") is True   # 节流未到期
        time.sleep(0.06)
        assert pf._quota_cooldown_active("p1") is False  # 节流到期，放行探测
        assert pf._quota_cooldown_active("p1") is True   # 立即再查 → 仍在冷却
        pf.reset_quota_cooldown()


class TestQuotaCooldownE2E:
    async def test_second_call_makes_zero_http(self, isolated, monkeypatch):
        """402 失败后，冷却窗口内的后续调用零 HTTP（直接抛错）。"""
        monkeypatch.setattr(pf, "QUOTA_PROBE_EVERY", 10.0)
        _wire_single(monkeypatch,
                     lambda mt: _FakeProvider(behavior="fail:402", built_mt=mt))
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        calls_1 = pf._ai_live["total"]
        assert calls_1 >= 1
        with pytest.raises(RuntimeError, match="冷却"):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert pf._ai_live["total"] == calls_1, "冷却期内不得发起任何真实调用"

    async def test_probe_recovers_on_success(self, isolated, monkeypatch):
        """节流到期后探测一次，成功即清除冷却。"""
        monkeypatch.setattr(pf, "QUOTA_PROBE_EVERY", 0.05)
        state = {"behavior": "fail:402"}

        def _factory(mt):
            return _FakeProvider(behavior=state["behavior"], built_mt=mt)

        _wire_single(monkeypatch, _factory)
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        state["behavior"] = "ok"
        time.sleep(0.06)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "正文内容"
        assert pf._quota_cooldown_active("p1") is False

    async def test_cooldown_disabled_is_legacy(self, isolated, monkeypatch):
        """ai_fail_cooldown_seconds=0 → 关闭冷却（旧行为：每次都尝试）。"""
        monkeypatch.setattr(pf, "QUOTA_FAIL_COOLDOWN", 0.0)
        _wire_single(monkeypatch,
                     lambda mt: _FakeProvider(behavior="fail:402", built_mt=mt))
        before = pf._ai_live["total"]
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert pf._ai_live["total"] - before == 2, "冷却关闭时每次都应发起真实调用"


class TestThinkingExhaustedRetry:
    async def test_doubles_max_tokens_and_recovers(self, isolated, monkeypatch):
        """思考吞噬：max_tokens 翻倍重试一次后成功。"""
        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        def _factory(mt):
            behavior = "thinking" if mt < 4096 else "ok"
            return _FakeProvider(behavior=behavior, built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "正文内容"
        assert built == [2048, 4096], "必须先以原 max_tokens 尝试，翻倍后重试"

    async def test_disabled_is_legacy(self, isolated, monkeypatch):
        """ai_retry_on_thinking_exhausted=False → 不重试（旧行为）。"""
        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", False)

        def _factory(mt):
            return _FakeProvider(behavior="thinking", built_mt=mt)

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert built == [2048]

    async def test_cap_blocks_second_retry(self, isolated, monkeypatch):
        """达到上限后不再重试（不做二重放大）。"""
        monkeypatch.setattr(settings, "ai_retry_on_thinking_exhausted", True)
        monkeypatch.setattr(settings, "ai_reasoning_max_tokens", 4096)

        def _factory(mt):
            return _FakeProvider(behavior="thinking", built_mt=mt)  # 永远思考吞噬

        built = _wire_single(monkeypatch, _factory, max_tokens=2048)
        with pytest.raises(RuntimeError):
            await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert built == [2048, 4096], "翻倍重试一次后必须停止"
