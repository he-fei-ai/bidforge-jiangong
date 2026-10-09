"""R4 回归测试：流式请求命中确定性错误时不重试。

背景：logs/backend.log 中「流式请求失败，自动回退普通请求重试」出现 132 次。
     当错误是 402/403/404/quota/invalid key 等**确定性错误**时，
     对同一 provider 走普通请求同样 100% 失败，重试白烧一次 HTTP。
     本轮修复：`ai_retry_on_non_retryable=True`（默认）时，
     命中 `is_non_retryable_error` 的错误直接抛出让上层候选链切换。

测试要点：
1. 500/timeout 类瞬时故障仍走普通请求重试（旧行为保留）；
2. 402/403/quota/invalid key 类确定性错误直接抛异常（跳过重试）；
3. `ai_retry_on_non_retryable=False` 时恢复旧行为（无论什么错误都回退普通请求）。
"""
from __future__ import annotations

import app.services.ai.provider_factory as pf
import pytest


class _FakeProvider:
    """最小 provider：chat() 和流式都会失败，可断言被调用了多少次。"""

    name = "fake-provider"
    model = "fake-model"

    def __init__(self, stream_error=None, chat_error=None):
        self.stream_error = stream_error
        self.chat_error = chat_error
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, **kwargs):
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return "ok"

    async def _fake_stream(self, messages, **kwargs):
        self.stream_calls += 1
        if self.stream_error is not None:
            raise self.stream_error
        return "ok-stream"


@pytest.fixture(autouse=True)
def _reset_stream_state():
    """每个用例前后清理流式失败记账，避免用例间污染。"""
    pf.reset_stream_capability()
    yield
    pf.reset_stream_capability()


def _patch_collect_stream():
    """把 _collect_stream 打桩到 provider._fake_stream，隔离真实 HTTP。返回原函数。"""
    original = pf._collect_stream

    async def fake_collect(p, messages, **kwargs):
        return await p._fake_stream(messages, **kwargs)

    pf._collect_stream = fake_collect
    return original


async def test_r4_non_retryable_stream_error_raises_without_chat_fallback():
    """确定性错误（HTTP 402 Insufficient Balance）应直接抛异常，不走 chat 重试。"""
    provider = _FakeProvider(
        stream_error=Exception("HTTP 402 Insufficient Balance"),
        chat_error=Exception("should not be called"),
    )
    original = _patch_collect_stream()
    try:
        old_flag = pf.ai_retry_on_non_retryable
        pf.ai_retry_on_non_retryable = True
        with pytest.raises(Exception, match="402"):
            await pf._call_provider(
                provider, [{"role": "user", "content": "hi"}],
                temperature=0.5, json_mode=True, max_tokens=100,
                request_mode="stream",
            )
    finally:
        pf._collect_stream = original
        pf.ai_retry_on_non_retryable = old_flag

    assert provider.stream_calls == 1
    assert provider.chat_calls == 0, "确定性错误不应触发 chat 重试"


async def test_r4_500_stream_error_still_retries_via_chat():
    """5xx 类瞬时故障仍应回退普通请求（旧行为保留）。"""
    provider = _FakeProvider(
        stream_error=Exception("HTTP 500: boom"),
        chat_error=None,
    )
    original = _patch_collect_stream()
    try:
        old_flag = pf.ai_retry_on_non_retryable
        pf.ai_retry_on_non_retryable = True
        result = await pf._call_provider(
            provider, [{"role": "user", "content": "hi"}],
            temperature=0.5, json_mode=True, max_tokens=100,
            request_mode="stream",
        )
    finally:
        pf._collect_stream = original
        pf.ai_retry_on_non_retryable = old_flag

    assert result == "ok", "500 应回退 chat 且 chat 返回 ok"
    assert provider.stream_calls == 1
    assert provider.chat_calls == 1, "500 应触发 chat 重试"


async def test_r4_invalid_api_key_raises():
    """invalid api key 是确定性错误，直接抛异常。"""
    provider = _FakeProvider(
        stream_error=Exception("invalid api key"),
        chat_error=Exception("should not be called"),
    )
    original = _patch_collect_stream()
    try:
        old_flag = pf.ai_retry_on_non_retryable
        pf.ai_retry_on_non_retryable = True
        with pytest.raises(Exception, match="invalid api key"):
            await pf._call_provider(
                provider, [{"role": "user", "content": "hi"}],
                temperature=0.5, json_mode=False, max_tokens=100,
                request_mode="stream",
            )
    finally:
        pf._collect_stream = original
        pf.ai_retry_on_non_retryable = old_flag

    assert provider.chat_calls == 0


async def test_r4_flag_false_keeps_old_behavior():
    """ai_retry_on_non_retryable=False 时恢复旧行为：一律回退 chat。"""
    provider = _FakeProvider(
        stream_error=Exception("HTTP 403 unauthorized"),
        chat_error=None,
    )
    original = _patch_collect_stream()
    try:
        old_flag = pf.ai_retry_on_non_retryable
        pf.ai_retry_on_non_retryable = False  # 旧行为
        result = await pf._call_provider(
            provider, [{"role": "user", "content": "hi"}],
            temperature=0.5, json_mode=False, max_tokens=100,
            request_mode="stream",
        )
    finally:
        pf._collect_stream = original
        pf.ai_retry_on_non_retryable = old_flag

    assert result == "ok", "旧行为下确定性错误仍会回退 chat"
    assert provider.chat_calls == 1


def test_r4_default_flag_is_true():
    """默认 True（保守修复），可通过 settings 关闭。"""
    assert pf.ai_retry_on_non_retryable is True