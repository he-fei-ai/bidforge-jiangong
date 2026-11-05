# -*- coding: utf-8 -*-
"""R176 B2 修复：降级链可见性（provider_used / fallback_reason）+ 流式跳过接线。

覆盖：
- 首选候选成功 → provider_used=首选、fallback_reason=""（无降级）；
- 首选失败转备用 → provider_used=备用、fallback_reason 为稳定枚举
  （rate_limited / timeout / parse_failed / provider_error）；
- provider 层流式分片跳过计数 → _attempt_candidate 可见信号 →
  chat_with_fallback_meta 回传非 0 计数；
- 旧入口 chat_with_fallback 仍只返回正文（零回归）。
"""

import asyncio

import pytest
from app.services.ai import provider_factory as pf


class _Fake:
    """可控 Provider 替身：按 provider_name 返回，记录是否有流式调用。"""

    name = "fake"
    model = "m"
    temperature = 0.7
    last_usage: dict = {}
    last_finish_reason = ""

    def __init__(self, *, text="ok", err=None, stream_pieces=None):
        self.chat_text = text
        self.chat_err = err
        self.stream_pieces = list(stream_pieces or [])
        self.stream_calls = 0
        # 与 BaseProvider 同名属性：_attempt_candidate 在成功后读取
        self.last_stream_parse_skipped = 0
        self.last_stream_parse_skipped_reason = ""

    async def chat(self, messages, **kw):
        if self.chat_err is not None:
            raise self.chat_err
        return self.chat_text

    async def stream(self, messages, **kw):
        self.stream_calls += 1
        for piece in self.stream_pieces:
            yield piece


def _cand(name, mode="normal"):
    return {
        "provider_name": name,
        "api_key": "k",
        "base_url": "http://x",
        "model": "m",
        "max_tokens": 100,
        "temperature": 0.7,
        "request_mode": mode,
    }


async def _patch_chain(monkeypatch, fakes, candidates):
    """把配置解析/候选构建/门禁/Provider 构建全部短路到固定替身。"""

    async def _resolve(ai_config=None, scene=""):
        return {"provider_name": candidates[0]["provider_name"], "model": "m"}

    async def _build(cfg):
        return candidates

    async def _gate(cands, scene):
        return cands, None

    def _build_provider(name, *a, **kw):
        return fakes[name]

    monkeypatch.setattr(pf, "_resolve_chat_config", _resolve)
    monkeypatch.setattr(pf, "_build_candidates", _build)
    monkeypatch.setattr(pf, "_gate_candidates", _gate)
    monkeypatch.setattr(pf, "_build_provider", _build_provider)


class TestFallbackMeta:
    async def test_primary_success_reports_no_fallback(self, monkeypatch, db_conn):
        await _patch_chain(monkeypatch, {"primary": _Fake(text="首选答案")}, [_cand("primary")])
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["text"] == "首选答案"
        assert meta["provider_used"] == "primary"
        assert meta["fallback_reason"] == ""
        assert meta["stream_parse_skipped"] == 0

    async def test_rate_limited_falls_back_to_backup(self, monkeypatch, db_conn):
        await _patch_chain(
            monkeypatch,
            {"primary": _Fake(err=RuntimeError("HTTP 429: rate limit")), "backup": _Fake(text="备用答案")},
            [_cand("primary"), _cand("backup")],
        )
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["text"] == "备用答案"
        assert meta["provider_used"] == "backup"
        assert meta["fallback_reason"] == "rate_limited"

    async def test_timeout_maps_to_timeout(self, monkeypatch, db_conn):
        await _patch_chain(
            monkeypatch,
            {"primary": _Fake(err=asyncio.TimeoutError("timed out")), "backup": _Fake(text="备用")},
            [_cand("primary"), _cand("backup")],
        )
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["provider_used"] == "backup"
        assert meta["fallback_reason"] == "timeout"

    async def test_empty_body_maps_to_parse_failed(self, monkeypatch, db_conn):
        await _patch_chain(
            monkeypatch,
            {"primary": _Fake(err=RuntimeError("AI 返回空正文")), "backup": _Fake(text="备用")},
            [_cand("primary"), _cand("backup")],
        )
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["fallback_reason"] == "parse_failed"

    async def test_other_errors_map_to_provider_error(self, monkeypatch, db_conn):
        await _patch_chain(
            monkeypatch,
            {"primary": _Fake(err=RuntimeError("HTTP 500: boom")), "backup": _Fake(text="备用")},
            [_cand("primary"), _cand("backup")],
        )
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["fallback_reason"] == "provider_error"

    async def test_legacy_entry_returns_plain_text(self, monkeypatch, db_conn):
        """旧入口 chat_with_fallback 仍返回 str（旧调用方零回归）。"""
        await _patch_chain(monkeypatch, {"primary": _Fake(text="正文")}, [_cand("primary")])
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "正文"
        assert isinstance(out, str)


class _FakeStreamResp:
    """最小 SSE 响应替身：按行吐出，状态码恒 200。"""

    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

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
    """按顺序吐出预设响应（每次给定一次流式调用）。"""

    def __init__(self, responses):
        self._responses = list(responses)

    def stream(self, method, url, json=None, headers=None, timeout=None):
        resp = self._responses.pop(0) if self._responses else _FakeStreamResp([])
        return _FakeStreamCtx(resp)


class TestStreamParseSkippedWiring:
    async def test_skipped_count_reaches_meta(self, monkeypatch, db_conn):
        """真实 provider 解析含非法分片的流 → 计数一路传到上层 meta（非 0）。"""
        from app.services.ai.providers import openai_compatible as oc

        # 混合流：非法 JSON 分片 + 正常内容分片（正常内容必须留存）
        bad_stream = [
            "data: {not json",
            'data: {"choices": [{"delta": {"content": "甲"}}]}',
            "data: oops",
            'data: {"choices": [{"delta": {"content": "乙"}}]}',
            "data: [DONE]",
        ]
        client = _FakeHttpClient([_FakeStreamResp(list(bad_stream)), _FakeStreamResp(list(bad_stream))])
        monkeypatch.setattr(oc, "get_async_client", lambda *a, **k: client)
        provider = oc.OpenAICompatibleProvider("sk-x", "https://api.test.com/v1", "m", max_tokens=100, temperature=0.7)

        await _patch_chain(monkeypatch, {"primary": provider}, [_cand("primary", mode="stream")])
        meta = await pf.chat_with_fallback_meta([{"role": "user", "content": "hi"}])
        assert meta["text"] == "甲乙"
        assert meta["stream_parse_skipped"] == 2
        assert "首条非法 SSE 分片" in meta["stream_parse_skipped_reason"]
        # 旧入口同样拿到完整正文（跳过不影响正常内容）
        out = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
        assert out == "甲乙"


class TestClassifyFallbackReason:
    @pytest.mark.parametrize(
        "err,expected",
        [
            (None, ""),
            (RuntimeError("HTTP 429: Too Many Requests"), "rate_limited"),
            (asyncio.TimeoutError("timed out"), "timeout"),
            (RuntimeError("HTTP 408: request timeout"), "timeout"),
            (RuntimeError("JSON 解析失败"), "parse_failed"),
            (RuntimeError("AI 返回空正文"), "parse_failed"),
            (RuntimeError("HTTP 500: boom"), "provider_error"),
            (ValueError("unexpected"), "provider_error"),
        ],
    )
    def test_enum_is_stable(self, err, expected):
        assert pf._classify_fallback_reason(err) == expected
