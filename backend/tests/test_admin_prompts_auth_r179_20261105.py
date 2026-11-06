"""R179（2026-11-05）管理面接口鉴权回归护栏。

背景：``/admin/prompts/*`` 挂在与 ``/api/v1/*`` 不同的根前缀下，既有
``ApiAuthMiddleware`` 只在 ``API_AUTH_TOKEN`` 非空时才生效；本地单机默认
（token 为空）时该组端点完全裸奔（``sync-from-code?force=true`` 可直接覆盖
用户编辑的提示词）。本文件锁定新增的 fail-closed 闸门：

- ``ADMIN_API_TOKEN`` 为空（默认）→ 仅回环来源（127.0.0.1 / ::1）放行，非回环 403；
- ``ADMIN_API_TOKEN`` 非空 → 缺/错凭据 401，``X-Admin-Token`` 与 ``Bearer`` 两种形式均放行；
- 伪造 ``X-Forwarded-For`` 不能绕过（仅在 ``TRUSTED_PROXY_IPS`` 命中时采信）；
- 护栏：所有 ``/admin/*`` 路由必须携带 ``require_admin_access``，新增端点漏挂必须变红。

测试使用 ``TestClient(client=(host, port))`` 直接控制 ASGI 直连对端，
不依赖真实网络；拒绝路径在依赖阶段即返回，不会触达业务处理函数（不写库）。
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.services.admin_auth import is_loopback_host, require_admin_access
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

ADMIN_TOKEN = "admin-s3cret-token-for-tests"


@pytest.fixture(autouse=True)
def _reset_auth_config(monkeypatch):
    """每个用例独立：默认空 token、不信任任何代理。"""
    monkeypatch.setattr(settings, "admin_api_token", "", raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_ips", "", raising=False)
    yield


def _probe_app() -> FastAPI:
    """与真实路由同型：router 级挂鉴权依赖，端点本身无副作用。"""
    app = FastAPI()
    router = APIRouter(prefix="/admin/prompts", dependencies=[Depends(require_admin_access)])

    @router.get("/probe")
    async def probe():
        return {"ok": True}

    app.include_router(router)
    return app


def _real_app() -> FastAPI:
    """真实 ``/admin/prompts`` 路由（拒绝路径不会执行到 handler）。"""
    from app.routers.prompts import admin_prompts_router

    app = FastAPI()
    app.include_router(admin_prompts_router)
    return app


def _client(app: FastAPI, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app, client=(host, 50000))


# ---------------------------------------------------------------------------
# 默认空 token：只允许回环
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_empty_token_allows_loopback(host):
    resp = _client(_probe_app(), host).get("/admin/prompts/probe")
    assert resp.status_code == 200, f"回环来源 {host} 必须放行：{resp.text[:160]}"


@pytest.mark.parametrize("host", ["10.0.0.8", "192.168.1.20", "203.0.113.7", "testclient", ""])
def test_empty_token_rejects_non_loopback(host):
    resp = _client(_probe_app(), host).get("/admin/prompts/probe")
    assert resp.status_code == 403
    assert "ADMIN_API_TOKEN" in resp.json()["detail"]


@pytest.mark.parametrize("host", ["127.0.0.2", "localhost", "", "0.0.0.0"])
def test_loopback_whitelist_is_explicit(host):
    """显式白名单：127.0.0.2 等 127/8 地址与文本 host 不视为回环（fail-closed）。"""
    assert is_loopback_host(host) is False


# ---------------------------------------------------------------------------
# 伪造 X-Forwarded-For
# ---------------------------------------------------------------------------


def test_forged_xff_from_untrusted_peer_cannot_bypass():
    resp = _client(_probe_app(), "10.0.0.8").get("/admin/prompts/probe", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 403, "未配置可信代理时不得采信 X-Forwarded-For"


def test_xff_recognized_when_peer_is_trusted_proxy(monkeypatch):
    """显式配置可信代理后才采信 XFF（与 audit_service.client_ip_of 同口径）。"""
    monkeypatch.setattr(settings, "trusted_proxy_ips", "10.0.0.8", raising=False)
    resp = _client(_probe_app(), "10.0.0.8").get("/admin/prompts/probe", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 配置 token：缺/错拒绝，两种携带形式放行
# ---------------------------------------------------------------------------


def test_configured_token_missing_credential_401(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app()).get("/admin/prompts/probe")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert resp.headers.get("cache-control") == "no-store"
    assert "X-Admin-Token" in resp.json()["detail"]


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Admin-Token": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": ADMIN_TOKEN},  # 缺少 Bearer 前缀
    ],
)
def test_configured_token_wrong_credential_401(monkeypatch, headers):
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app()).get("/admin/prompts/probe", headers=headers)
    assert resp.status_code == 401


def test_configured_token_x_admin_token_ok(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app()).get("/admin/prompts/probe", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status_code == 200


@pytest.mark.parametrize("prefix", ["Bearer", "bearer"])
def test_configured_token_bearer_ok(monkeypatch, prefix):
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app()).get("/admin/prompts/probe", headers={"Authorization": f"{prefix} {ADMIN_TOKEN}"})
    assert resp.status_code == 200


def test_configured_token_allows_remote_origin(monkeypatch):
    """显式配置 token 即表示允许远程管理（不再限制来源）。"""
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app(), "10.0.0.8").get("/admin/prompts/probe", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status_code == 200


def test_non_ascii_admin_token_returns_401_not_500(monkeypatch):
    """非 ASCII 凭据必须走拒绝，不得因 compare_digest 抛 TypeError 变 500。"""
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_probe_app()).get("/admin/prompts/probe", headers={"X-Admin-Token": b"t\xf6k\xe9n"})
    assert resp.status_code == 401, f"期望 401，实际 {resp.status_code}: {resp.text[:120]}"


# ---------------------------------------------------------------------------
# 真实 /admin/prompts/* 端点
# ---------------------------------------------------------------------------


def test_real_route_rejects_non_loopback_by_default():
    resp = _client(_real_app(), "10.0.0.8").post("/admin/prompts/sync-from-code")
    assert resp.status_code == 403
    assert "ADMIN_API_TOKEN" in resp.json()["detail"]


def test_real_route_rejects_missing_token_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_token", ADMIN_TOKEN, raising=False)
    resp = _client(_real_app(), "127.0.0.1").post("/admin/prompts/sync-from-code")
    assert resp.status_code == 401
    assert "X-Admin-Token" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 回归护栏：新增 /admin/* 漏挂鉴权必须变红
# ---------------------------------------------------------------------------


def _route_has_admin_guard(route) -> bool:
    deps = getattr(route, "dependencies", None) or []
    return any(getattr(dep, "dependency", None) is require_admin_access for dep in deps)


def _iter_leaf_routes(routes):
    """展开 FastAPI 0.139 的 ``_IncludedRouter`` 惰性包装，取到真实 APIRoute。"""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_leaf_routes(inner.routes)
        elif hasattr(route, "path"):
            yield route


def test_every_admin_route_in_app_carries_guard():
    """反射遍历真实 app：任何 /admin/* 路由都必须挂着 require_admin_access。"""
    from app.main import app as main_app

    admin_routes = [r for r in _iter_leaf_routes(main_app.routes) if getattr(r, "path", "").startswith("/admin")]
    assert admin_routes, "未找到任何 /admin 路由，护栏本身失效（守卫无效）"
    for route in admin_routes:
        assert _route_has_admin_guard(route), f"{route.path} 未挂 require_admin_access"


def test_admin_prompts_router_all_routes_guarded():
    from app.routers.prompts import admin_prompts_router

    routes = [r for r in admin_prompts_router.routes if hasattr(r, "dependencies")]
    assert routes, "admin_prompts_router 无路由，护栏失效"
    for route in routes:
        assert route.path.startswith("/admin/prompts")
        assert _route_has_admin_guard(route), f"{route.path} 未挂鉴权依赖"


def test_guard_checker_detects_removed_dependency(monkeypatch):
    """变异自检：摘掉依赖后护栏判定必须为 False（证明护栏有判别力）。"""
    from app.routers.prompts import admin_prompts_router

    route = next(r for r in admin_prompts_router.routes if hasattr(r, "dependencies"))
    monkeypatch.setattr(route, "dependencies", [])
    assert _route_has_admin_guard(route) is False
