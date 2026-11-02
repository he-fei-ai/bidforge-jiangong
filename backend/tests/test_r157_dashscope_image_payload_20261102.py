# -*- coding: utf-8 -*-
"""R157（2026-11-02）：DashScope 文生图请求体按模型能力自适应 + 404 显式报错。

背景（生产日志实证）：
  - `HTTP 400: {"code":"invalid_request","message":"quality 不是文生图队列支持的字段"}`
    —— 旧实现无条件下发 OpenAI 风格的 `quality`，qwen-image-3.x 队列不接受；
  - `HTTP 404: {"code":"model_not_found","message":"当前分组不支持模型 qwen-image-3.0-pro"}`
    —— 模型未开通属配置错误，旧日志混在通用 "HTTP 404" 里，看不出该改哪个配置项。

本文件锁定：
  A. qwen-image-3 系（能力表登记为不支持 quality）→ 请求体**不含** quality；
  B. 未登记 ability 的模型族 → **保持**旧行为（仍下发 quality），不做行为扩大；
  C. 404 model_not_found → ERROR 级日志且消息点名配置项 IMAGE_MODEL。
全用例走 fake transport，不访问外网。
"""
from types import SimpleNamespace

import pytest
from app.services.ai.image_providers import dashscope as ds


class _FakeResponse:
    """最小 aiohttp response 替身：只用 status / json() / text() 三个面。"""

    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return self._text


class _FakeSession:
    """记录 post 请求体，返回预置响应。"""

    def __init__(self, response: _FakeResponse, captured: list[dict]):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def post(self, url: str, **kwargs):
        self._captured.append({"url": url, "json": kwargs.get("json")})
        return self._response


def _patch_transport(monkeypatch, response: _FakeResponse) -> list[dict]:
    """把模块级 aiohttp 换成 fake，返回捕获的请求列表。"""
    captured: list[dict] = []
    monkeypatch.setattr(
        ds,
        "aiohttp",
        SimpleNamespace(
            ClientSession=lambda **kwargs: _FakeSession(response, captured),
            ClientTimeout=lambda **kwargs: None,
        ),
    )
    return captured


def _adapter(model: str) -> ds.DashScopeAdapter:
    return ds.DashScopeAdapter(
        {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model": model,
            "default_size": "1024x1024",
        }
    )


_OK_RESPONSE = {"data": [{"url": "https://example.invalid/a.png"}]}


@pytest.mark.asyncio
async def test_qwen_image_3_payload_has_no_quality(monkeypatch):
    """A：qwen-image-3 系走文生图队列，请求体必须剔除 quality。"""
    captured = _patch_transport(monkeypatch, _FakeResponse(200, _OK_RESPONSE))

    result = await _adapter("qwen-image-3.0-pro").generate("基坑支护示意图")

    assert result["success"] is True
    payload = captured[0]["json"]
    assert "quality" not in payload
    # 队列需要的模型/尺寸字段仍在，剔除只针对能力表登记项
    assert payload["model"] == "qwen-image-3.0-pro"
    assert payload["size"] == "1024x1024"
    assert payload["parameters"]["style"] == "technical_diagram"


@pytest.mark.asyncio
async def test_quality_kept_for_unregistered_model_family(monkeypatch):
    """B：未登记能力的模型族保持旧行为（仍下发 quality，不扩大改动面）。"""
    captured = _patch_transport(monkeypatch, _FakeResponse(200, _OK_RESPONSE))

    result = await _adapter("qwen-image-2.0").generate("基坑支护示意图")

    assert result["success"] is True
    assert captured[0]["json"]["quality"] == "standard"


def test_unsupported_fields_extension_table():
    """能力表为数据驱动：新模型族只需登记一行，不改 generate 主流程。"""
    assert ds.DashScopeAdapter._unsupported_fields("qwen-image-3.0-pro") == frozenset(
        {"quality"}
    )
    assert ds.DashScopeAdapter._unsupported_fields("qwen-image-3.5-turbo") == frozenset(
        {"quality"}
    )
    assert ds.DashScopeAdapter._unsupported_fields("qwen-image-2.0") == frozenset()


@pytest.mark.asyncio
async def test_404_model_not_found_is_error_with_config_hint(monkeypatch, caplog):
    """C：model_not_found 必须 ERROR 级留痕，且点名应调整的配置项。"""
    body = (
        '{"error":{"code":"model_not_found","message":"当前分组不支持模型 '
        'qwen-image-3.0-pro，请更换模型后重试"}}'
    )
    _patch_transport(monkeypatch, _FakeResponse(404, text=body))

    with caplog.at_level("ERROR", logger=ds.logger.name):
        result = await _adapter("qwen-image-3.0-pro").generate("基坑支护示意图")

    assert result["success"] is False
    assert "IMAGE_MODEL" in result["error"]
    assert "model_not_found" in result["error"]
    assert any(
        r.levelname == "ERROR" and "IMAGE_MODEL" in r.getMessage() for r in caplog.records
    )
