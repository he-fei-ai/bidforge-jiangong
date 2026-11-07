# -*- coding: utf-8 -*-
"""R184b（2026-11-07）图表模块 · 图像生成适配器上游错误体密钥回显脱敏。

泄露面：`image_providers` 的 7 个平台适配器在非 200 分支把
`(await response.text())[:200]` 原样拼进 `error_msg`：
  · `logger.error(error_msg)` 落服务端日志；
  · `error` 与 `raw_response["error"]` 随适配器结果返回给 `image_engine`。
上游代理/厂商 401/500 错误体可能回显 `Authorization: Bearer sk-…`。

本文件用假 aiohttp 会话让每个平台都返回同一个含密钥的错误体，断言
「日志 + 返回的 error + raw_response.error」三处均无密钥原文、且保留掩码标记。
修复前 7 个平台应全部失败。
"""

import logging

import pytest
from app.services.ai.image_providers import (
    agnes,
    baidu,
    dashscope,
    hunyuan,
    stepfun,
    volcano,
    zhipu,
)

SECRET = "sk-live-abc1234567890"
BODY = '{"error":"Unauthorized: Authorization: Bearer ' + SECRET + '"}'

_ADAPTERS = [
    (pytest.param(agnes, agnes.AgnesAdapter, id="agnes")),
    (pytest.param(baidu, baidu.BaiduAdapter, id="baidu")),
    (pytest.param(dashscope, dashscope.DashScopeAdapter, id="dashscope")),
    (pytest.param(hunyuan, hunyuan.HunyuanAdapter, id="hunyuan")),
    (pytest.param(stepfun, stepfun.StepFunAdapter, id="stepfun")),
    (pytest.param(volcano, volcano.VolcanoAdapter, id="volcano")),
    (pytest.param(zhipu, zhipu.ZhipuAdapter, id="zhipu")),
]


class _FakeResponse:
    def __init__(self, status: int, body: str, data: dict | None = None):
        self.status = status
        self._body = body
        self._data = data or {}

    async def text(self):
        return self._body

    async def json(self):
        return self._data


class _FakePostCtx:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def post(self, *args, **kwargs):
        # 百度先走 OAuth 取 token：让 token 成功、图片端点失败，
        # 才能命中 error_text 分支（否则会在 token 缺失处提前返回）。
        url = str(args[0] if args else kwargs.get("url", ""))
        if "oauth" in url:
            return _FakePostCtx(_FakeResponse(200, "", {"access_token": "fake-token"}))
        return _FakePostCtx(_FakeResponse(500, BODY))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.parametrize("mod,cls", _ADAPTERS)
async def test_upstream_error_body_secret_masked(monkeypatch, caplog, mod, cls):
    """非 200 错误体中的密钥不得进入日志、error 字段或 raw_response。"""
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *_a, **_kw: _FakeSession())
    adapter = cls(
        {"api_key": SECRET, "secret_token": "fake-secret", "base_url": "https://upstream.test"}
    )

    with caplog.at_level(logging.ERROR):
        result = await adapter.generate("施工流程图")

    assert result["success"] is False
    err = str(result.get("error") or "")
    raw_err = str((result.get("raw_response") or {}).get("error") or "")
    assert SECRET not in caplog.text, "密钥原文进入服务端日志"
    assert SECRET not in err, f"密钥原文进入 error 字段: {err}"
    assert SECRET not in raw_err, f"密钥原文进入 raw_response: {raw_err}"
    assert "sk-****" in err, f"掩码后仍应保留错误上下文: {err}"
