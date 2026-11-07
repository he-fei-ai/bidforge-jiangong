# -*- coding: utf-8 -*-
"""R184（2026-11-07）图表渲染模块 · 上游错误体密钥回显脱敏回归。

泄露面（父线程转入）：`backend/app/services/ai/mermaid_service.py` 的 Docker /
Railway 非 200 分支把 `resp.text[:200]` 原样拼进 `MermaidRenderError`：
  · 该异常在 `mermaid_renderer.render_mermaid_to_bytes` 的
    `logger.warning(... %s, e)` 落日志；
  · `/api/v1/charts/...` 修复入口以 `last_error = str(error)` 回填前端与提示词。
上游代理或厂商 401/403 错误体可能回显请求头 `Authorization: Bearer sk-…`。

本文件逐点证实（修复前均应失败）：
1. Docker 401 错误体含 `Authorization: Bearer sk-…` → 抛出的异常文本必须掩码；
2. Railway 500 错误体含 `api_key=sk-…`（非 4xx 会进 last_error 日志）→
   异常文本与 WARNING 日志都必须掩码。
"""

import logging

import pytest
from app.services.ai.mermaid_service import MermaidRenderError, MermaidServiceClient

SECRET = "sk-live-abc1234567890"
BEARER_BODY = '{"error":"Incorrect API key provided: Authorization: Bearer ' + SECRET + '"}'
KEY_FIELD_BODY = '{"error":{"message":"invalid key","api_key":"' + SECRET + '"}}'


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": "application/json"}
        self.content = b""

    def json(self):
        return {}


class _FakeAsyncClient:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    async def post(self, *_args, **_kwargs):
        return self._resp


def _client(resp: _FakeResponse) -> MermaidServiceClient:
    c = MermaidServiceClient(docker_url="http://docker.test", railway_url="http://railway.test")

    async def _get_client():
        return _FakeAsyncClient(resp)

    c._get_client = _get_client
    return c


async def test_docker_error_body_secret_masked(monkeypatch):
    """Docker 401：异常文本不得携带密钥原文，但保留可诊断上下文。"""
    c = _client(_FakeResponse(401, BEARER_BODY))

    async def _true():
        return True

    async def _false():
        return False

    monkeypatch.setattr(c, "check_docker_available", _true)
    monkeypatch.setattr(c, "check_railway_available", _false)
    monkeypatch.setattr(c, "check_mmdc_available", lambda: False)

    with pytest.raises(MermaidRenderError) as ei:
        await c.render_mermaid_via_http(code="graph TD\n A-->B")

    msg = str(ei.value)
    assert SECRET not in msg, f"密钥原文进入异常文本: {msg}"
    assert "sk-****" in msg or "Bearer ****" in msg, f"掩码后仍应保留错误上下文: {msg}"
    assert "HTTP 401" in msg


async def test_railway_error_body_secret_masked_in_message_and_log(monkeypatch, caplog):
    """Railway 500：异常文本与 WARNING 日志（last_error）都不得携带密钥原文。"""
    c = _client(_FakeResponse(500, KEY_FIELD_BODY))

    async def _true():
        return True

    async def _false():
        return False

    monkeypatch.setattr(c, "check_docker_available", _false)
    monkeypatch.setattr(c, "check_railway_available", _true)
    monkeypatch.setattr(c, "check_mmdc_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(MermaidRenderError) as ei:
            await c.render_mermaid_via_http(code="graph TD\n A-->B")

    msg = str(ei.value)
    assert SECRET not in msg, f"密钥原文进入异常文本: {msg}"
    assert SECRET not in caplog.text, "密钥原文进入 WARNING 日志"
    assert "sk-****" in msg, f"掩码后仍应保留错误上下文: {msg}"
    assert "HTTP 500" in msg
