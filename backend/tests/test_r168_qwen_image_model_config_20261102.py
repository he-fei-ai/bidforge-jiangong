# -*- coding: utf-8 -*-
"""R168（2026-11-02）：qwen_image 伪 Provider 的型号配置化 + 未配置不入降级链。

背景（P1-3 残余）：
  旧实现把 ``_model_override`` 写死为 ``"qwen-image-3.0-pro"``，IMAGE_MODEL
  配了别家型号时仍会拿 qwen 型号去打 dashscope，且型号换代必须改代码。
现改为：
  A. 型号一律取自既有配置读取路径（``_cfg_model`` = IMAGE_MODEL 或
     model_configs.image_model）；家族用前缀判断，新增型号无需改代码；
  B. 未配置 / 配置为他家型号时，qwen_image 不进入降级链（不再顶默认型号）。
全用例用 fake adapter，不访问外网。
"""
import pytest
from app.services.ai import image_engine as ie
from app.services.ai.image_providers.registry import ProviderRegistry


@pytest.fixture(autouse=True)
def _clear_image_cache():
    """隔离图片缓存，避免用例间命中成功/失败缓存。"""
    ie._IMAGE_CACHE.clear()
    ie._IMAGE_FAIL_CACHE.clear()
    yield
    ie._IMAGE_CACHE.clear()
    ie._IMAGE_FAIL_CACHE.clear()


def test_qwen_image_not_in_chain_when_model_not_configured():
    """A：未配置/配置为他家型号 → qwen_image 伪 Provider 不入链。"""
    for cfg_model in ("", "   ", "agnes-image-2.1-flash", "dashscope-text-model"):
        resolved = ie._resolve_qwen_image_model(cfg_model)
        assert resolved == ""
        queue = ie._build_image_provider_queue("agnes", resolved)
        # agnes 本身在降级链内，不作第一优先级重复前置
        assert queue == ["dashscope", "agnes"]
        assert "qwen_image" not in queue


def test_qwen_image_in_chain_with_configured_model_value():
    """B：配置为 qwen-image 家族型号 → 入链，且型号值原样取自配置。"""
    configured = "qwen-image-9.9-preview"  # 代码里不存在的型号，证明非硬编码
    resolved = ie._resolve_qwen_image_model(configured)
    assert resolved == configured
    queue = ie._build_image_provider_queue("volcengine", resolved)
    assert queue == ["volcengine", "qwen_image", "dashscope", "agnes"]


@pytest.mark.asyncio
async def test_generate_uses_configured_model_not_hardcoded(monkeypatch):
    """B：端到端——qwen_image 候选把配置型号传给 dashscope 适配器。"""
    seen_provider_ids: list[str] = []
    seen_models: list[str] = []

    class _FakeAdapter:
        supported_sizes = {"2K"}

        async def generate(self, prompt, model, size, ratio):
            return {"success": True, "image_url": "https://example.invalid/qwen.png"}

    def _fake_get_adapter(provider_id, config):
        seen_provider_ids.append(provider_id)
        seen_models.append(config["model"])
        return _FakeAdapter()

    monkeypatch.setattr(ProviderRegistry, "get_adapter", _fake_get_adapter)

    url = await ie.generate_illustration_image(
        None,
        "R168 配置驱动型号验证",
        "custom",
        "16:9",
        "qwen_image",
        {
            "image_enabled": True,
            "image_api_key_enc": "test-key",
            "image_model": "qwen-image-9.9-preview",
            "image_default_size": "2K",
            "image_price_per_image": 0.0,
        },
    )

    assert url == "https://example.invalid/qwen.png"
    # 伪 Provider 映射到 dashscope 适配器，型号来自配置而非写死
    assert seen_provider_ids == ["dashscope"]
    assert seen_models == ["qwen-image-9.9-preview"]
