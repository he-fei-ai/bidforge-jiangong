"""管理面接口（``/admin/*``）鉴权依赖。

背景（R179 · 2026-11-05）
----------------------
``/admin/prompts/*`` 挂在与 ``/api/v1/*`` 不同的根前缀下，虽有
``ApiAuthMiddleware`` 兜底，但后者**只在 ``API_AUTH_TOKEN`` 非空时生效**；
本地单机默认（token 为空）时，任何可达网络的客户端都能调用
``POST /admin/prompts/sync-from-code``（可 ``force=true`` 覆盖用户编辑的提示词）。
本模块为管理面提供独立的 fail-closed 闸门：

1. ``ADMIN_API_TOKEN`` 为空（默认）→ **只允许回环来源**
   （``127.0.0.1`` / ``::1`` / IPv4-mapped ``::ffff:127.0.0.1``）。
   非回环一律 403 —— 本机单机使用零变化，服务被暴露到局域网/公网时默认关闭。
2. ``ADMIN_API_TOKEN`` 非空 → 必须携带 ``X-Admin-Token`` 或
   ``Authorization: Bearer <token>``，使用 ``hmac.compare_digest`` 常数时间比较；
   放行后不再限制来源（显式配置 token 即表示允许远程管理）。
3. **不信任 ``X-Forwarded-For``**：仅当直连对端命中
   ``TRUSTED_PROXY_IPS`` 白名单时才采信该头（复用
   ``audit_service.client_ip_of`` 的既有口径），否则一律只看 ASGI 直连对端，
   避免客户端伪造来源 IP 绕过回环判定。

错误响应沿用本仓既有惯例：``HTTPException`` → ``{"detail": "..."}``。
选 403（而非 401）作为「来源不被允许」的状态码，是为了不触发前端
``authGuard`` 的 401 全局短路（该短路会拦截后续所有请求）。
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request, status

from app.config import settings
from app.services.audit_service import client_ip_of

logger = logging.getLogger(__name__)

#: 回环来源白名单。显式列举而非按网段匹配：``127.0.0.2`` 等地址虽然也属
#: 127/8，但在容器/代理场景下容易被误配为对外监听地址，故不纳入。
#: ``::ffff:127.0.0.1`` 是 IPv4-mapped IPv6 形态，语义上仍是本机回环。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _extract_admin_token(headers) -> str:
    """从请求头提取管理凭据：优先 ``X-Admin-Token``，其次 ``Authorization: Bearer``。"""
    x_admin = (headers.get("x-admin-token") or "").strip()
    if x_admin:
        return x_admin
    authorization = (headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def is_loopback_host(host: str) -> bool:
    """判断来源主机是否为回环地址（供单测与守卫复用）。"""
    return (host or "").strip() in _LOOPBACK_HOSTS


async def require_admin_access(request: Request) -> None:
    """FastAPI 依赖：管理面接口统一鉴权（fail-closed）。"""
    token = (settings.admin_api_token or "").strip()
    if token:
        provided = _extract_admin_token(request.headers)
        # ⚠️ 用 bytes 比较：``hmac.compare_digest`` 对 str 要求两侧纯 ASCII，
        #    非 ASCII 凭据会抛 TypeError → 500。bytes 形态既无反例，也仍是常数时间。
        if provided and hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")):
            return
        logger.warning(
            "拒绝未授权管理请求 %s（来源 %s）",
            getattr(request, "url", ""),
            client_ip_of(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：请在请求头携带 X-Admin-Token 或 Authorization: Bearer <admin token>",
            headers={
                "WWW-Authenticate": 'Bearer realm="scheme-assistant-admin"',
                "Cache-Control": "no-store",
            },
        )

    host = client_ip_of(request)
    if not is_loopback_host(host):
        logger.warning(
            "拒绝非回环来源的管理请求 %s（来源 %s）",
            getattr(request, "url", ""),
            host or "<unknown>",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "管理接口默认仅允许本机回环访问；如需远程管理，请设置 ADMIN_API_TOKEN " "并在请求头携带 X-Admin-Token"
            ),
        )
