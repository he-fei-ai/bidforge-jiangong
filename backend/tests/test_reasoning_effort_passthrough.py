# -*- coding: utf-8 -*-
"""对齐 OpenBidKit reasoning_effort 能力：推理类模型「思考力度」透传测试（2026-09-22）

验证 chat_with_fallback 在 reasoning_effort 非空时，将 reasoning_effort 作为
extra_body["reasoning_effort"] 透传至 provider；为空时（默认）不透传，完全向后兼容。
"""
import pytest

import app.services.ai.provider_factory as pf


class _RecProvider:
    """记录每次 chat/stream 调用收到的 extra_body 的哑 Provider。"""
    name = "fake"

    def __init__(self, *a, **k):
        self.calls: list[dict] = []

    async def chat(self, messages, temperature=None, json_mode=False,
                   max_tokens=None, extra_body=None, **kw):
        self.calls.append({"extra_body": extra_body})
        return "ok"

    async def stream(self, messages, temperature=None, json_mode=False,
                     max_tokens=None, extra_body=None, **kw):
        self.calls.append({"extra_body": extra_body})
        yield "ok"


def _fake_config(api_key="k", request_mode="normal"):
    return {
        "provider_name": "openai",
        "api_key": api_key,
        "base_url": "https://x.example/v1",
        "model": "m",
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout": 60,
        "request_mode": request_mode,
    }


@pytest.fixture
def patched(monkeypatch):
    fake = _RecProvider()
    monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: fake)
    monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")
    async def _no_fallback():
        return []
    monkeypatch.setattr(pf, "_fallback_chain", _no_fallback)
    # _log_audit 会写 DB，单测里跳过（不影响 extra_body 透传验证）
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(pf, "_log_audit", _noop)
    return fake


async def test_reasoning_effort_passthrough(patched):
    """非空 reasoning_effort 透传为 extra_body['reasoning_effort']。"""
    await pf.chat_with_fallback(
        [{"role": "user", "content": "hi"}],
        ai_config=_fake_config(), reasoning_effort="high")
    assert patched.calls, "provider 应被调用"
    assert patched.calls[-1]["extra_body"] == {"reasoning_effort": "high"}


async def test_reasoning_effort_default_no_injection(patched):
    """默认（空串）不发送 extra_body，向后兼容。"""
    await pf.chat_with_fallback(
        [{"role": "user", "content": "hi"}], ai_config=_fake_config())
    assert patched.calls[-1]["extra_body"] is None


async def test_reasoning_effort_from_settings(patched, monkeypatch):
    """未显式传参时，复用 settings.ai_reasoning_effort（默认空串=不发送）。"""
    await pf.chat_with_fallback(
        [{"role": "user", "content": "hi"}], ai_config=_fake_config())
    assert patched.calls[-1]["extra_body"] is None

    monkeypatch.setattr(pf.settings, "ai_reasoning_effort", "medium")
    patched.calls.clear()
    await pf.chat_with_fallback(
        [{"role": "user", "content": "hi"}], ai_config=_fake_config())
    assert patched.calls[-1]["extra_body"] == {"reasoning_effort": "medium"}


async def test_reasoning_effort_stream_mode(patched):
    """流式模式下同样透传 extra_body。"""
    await pf.chat_with_fallback(
        [{"role": "user", "content": "hi"}],
        ai_config=_fake_config(request_mode="stream"), reasoning_effort="low")
    assert patched.calls[-1]["extra_body"] == {"reasoning_effort": "low"}
