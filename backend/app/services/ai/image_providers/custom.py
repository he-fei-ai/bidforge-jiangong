"""自定义厂商 / OpenAI 兼容图像生成适配器（覆盖 DALL-E-3 与任意 OpenAI 兼容端点）。"""
import logging
from typing import Any

import aiohttp

from app.services.redaction import redact_sensitive_text

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint

logger = logging.getLogger(__name__)


def build_openai_payload(prompt: str, model: str, size: str) -> dict:
    """构造 OpenAI images/generations 请求体（纯函数，可单测）。"""
    return {"model": model or "dall-e-3", "prompt": prompt, "n": 1, "size": size or "1024x1024", "quality": "standard"}


class CustomAdapter(ImageProviderAdapter):
    provider_id = "custom"
    default_model = ""
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        self.base_url = config.get("base_url", "")
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model") or self.default_model
        self.default_size = config.get("default_size", "1024x1024")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.0)

    def _extract_url(self, data: dict) -> str | None:
        """从 OpenAI 兼容响应提取图片：data[0].url，无 url 则 b64_json → data URL。"""
        items = data.get("data") or []
        if not items:
            return None
        first = items[0]
        url = first.get("url")
        if url:
            return url
        b64 = first.get("b64_json")
        if b64:
            return f"data:image/png;base64,{b64}"
        return None

    async def generate(self, prompt, model=None, size=None, ratio=None, reference_images=None, **kwargs):
        use_model = model or self.model or "dall-e-3"
        pixel = size or self.default_size
        # 档位 1K/2K/3K/4K 转像素（OpenAI 兼容端点多为像素式；档位时按长边近似）
        if str(pixel) in {"1K", "2K", "3K", "4K"}:
            pixel = {"1K": "1024x1024", "2K": "1024x1024", "3K": "1024x1024", "4K": "2048x2048"}[str(pixel)]
        payload = build_openai_payload(prompt, use_model, pixel)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()) as session:
                async with session.post(build_endpoint(self.base_url, "images", "generations"), headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        url = self._extract_url(data)
                        if url:
                            return {"success": True, "image_url": url, "image_base64": None,
                                    "cost": self.price_per_image, "provider": self.provider_id,
                                    "model": use_model, "raw_response": data, "error": None}
                        return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                                "provider": self.provider_id, "model": use_model, "raw_response": data,
                                "error": "API 返回中缺少图片 URL"}
                    err = redact_sensitive_text(await resp.text())[:200]
                    return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                            "provider": self.provider_id, "model": use_model,
                            "raw_response": {"status": resp.status, "error": err}, "error": f"HTTP {resp.status}: {err}"}
        except Exception as e:
            return {"success": False, "image_url": None, "image_base64": None, "cost": 0.0,
                    "provider": self.provider_id, "model": use_model, "raw_response": {}, "error": f"custom 调用失败: {e!s}"}

    async def test_connection(self, api_key, base_url):
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(build_endpoint(base_url, "models"), headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug("custom connection test failed: %s", e)
            return False

    async def list_models(self, api_key, base_url):
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(build_endpoint(base_url, "models"), headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        except Exception:
            pass
        return [self.default_model] if self.default_model else []
