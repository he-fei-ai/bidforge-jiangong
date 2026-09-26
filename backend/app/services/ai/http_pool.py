"""AI Provider 的 httpx 连接池（进程级复用）。

背景（性能瓶颈 P0-4 · 2026-09-17）：
  原实现每个 Provider 在 ``chat()`` 内 ``async with httpx.AsyncClient(...)``
  新建客户端，请求结束即销毁连接池 —— 每次 AI 调用都要重做一轮
  DNS + TCP + TLS 握手（HTTPS 至少 2 个 RTT）。实测 2080 次调用即 2080 次握手，
  以会话级 269 次调用计，仅此一项就浪费 1.5~5 分钟。

设计：
- 按 ``(base_url, proxy)`` 维度缓存常驻 ``httpx.AsyncClient``，复用连接与 TLS 会话；
- 单次调用超时**不在此处固定**，由调用方通过 ``client.post(..., timeout=...)``
  按请求传入（httpx 支持 per-request timeout），因此连接池可跨不同超时复用；
- 缓存按事件循环隔离：pytest 等场景会为每个用例新建事件循环，
  旧 client 绑定在已关闭的 loop 上，必须重建（否则 ``RuntimeError: Event loop is closed``）；
- 进程退出由 ``aclose_all_clients()`` 统一关闭（main.py lifespan 调用）。

注：``httpx.AsyncClient`` 构造是同步且惰性的（不发起网络），
因此本模块全部为同步函数，无 await 点，无需加锁。
"""
import asyncio
import logging

import httpx

logger = logging.getLogger("ai_http_pool")

# 连接池默认超时（仅兜底；实际每次请求都会显式传入 timeout）
_DEFAULT_TIMEOUT = 300.0
# 单池连接上限：与并发档位（<=8）留足余量
_MAX_CONNECTIONS = 32
_MAX_KEEPALIVE = 16
_KEEPALIVE_EXPIRY = 90.0

# (base_url, proxy) → (loop, client)
_clients: dict[tuple[str, str], tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_async_client(base_url: str, proxy: str | None = None) -> httpx.AsyncClient:
    """取（或建）``(base_url, proxy)`` 维度的共享 ``httpx.AsyncClient``。"""
    key = ((base_url or "").rstrip("/"), proxy or "")
    loop = _current_loop()
    entry = _clients.get(key)
    if entry is not None:
        cached_loop, client = entry
        # 同一事件循环且客户端仍可用 → 直接复用
        if cached_loop is loop and not client.is_closed:
            return client
        # 事件循环已更换（pytest / 重启用例）或客户端已关闭 → 丢弃旧实例。
        # 旧实例绑定在已关闭的 loop 上，无法 await aclose()，只能尽力而为。
        _clients.pop(key, None)
        try:
            if cached_loop is not None and cached_loop.is_closed():
                pass  # 已关闭的 loop 上不做任何 await
        except Exception:
            pass

    client = httpx.AsyncClient(
        timeout=_DEFAULT_TIMEOUT,
        proxy=proxy,
        limits=httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        ),
    )
    _clients[key] = (loop, client)
    logger.debug("创建 AI HTTP 连接池: %s（proxy=%s）", key[0], key[1] or "-")
    return client


async def aclose_all_clients() -> None:
    """关闭全部缓存连接池（进程退出 / lifespan 关闭钩子）。"""
    entries = list(_clients.values())
    _clients.clear()
    for _loop, client in entries:
        try:
            await client.aclose()
        except Exception as e:  # pragma: no cover - 关闭失败不影响退出
            logger.debug("关闭 AI HTTP 连接池失败（已忽略）: %s", e)


def reset_clients_for_test() -> None:
    """清空缓存（供单测断言池复用语义；不关闭底层连接）。"""
    _clients.clear()
