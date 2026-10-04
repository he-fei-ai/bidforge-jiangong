"""统一代理配置工具（自招投标方案平台移植）

从 app.config 的 proxy_host / proxy_port 读取代理配置，
为所有外部 HTTP 客户端（httpx / aiohttp / requests）提供一致的代理支持。
"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger(__name__)


def get_proxy_url() -> str | None:
    """返回应使用的代理 URL。未配置时返回 None。"""
    if settings.proxy_host and settings.proxy_port:
        return f"http://{settings.proxy_host}:{settings.proxy_port}"
    return None


def build_httpx_proxy_settings() -> dict:
    """构建 httpx 客户端可用的代理字典。返回空字典表示不使用代理。"""
    proxy_url = get_proxy_url()
    if not proxy_url:
        return {}
    logger.debug("build_httpx_proxy_settings: 代理已启用 -> %s", proxy_url)
    return {"proxy": proxy_url}


def get_aiohttp_proxy() -> str | None:
    """返回 aiohttp.ClientSession 可用的代理 URL。返回 None 表示不使用代理。"""
    return get_proxy_url()


def aiohttp_session_kwargs() -> dict:
    """返回 aiohttp.ClientSession 的代理参数字典。

    当未配置代理时返回空字典，调用方直接 **kwargs 展开即可。
    """
    proxy_url = get_proxy_url()
    if proxy_url:
        logger.debug("aiohttp_session_kwargs: 代理已启用 -> %s", proxy_url)
    return {"proxy": proxy_url} if proxy_url else {}
