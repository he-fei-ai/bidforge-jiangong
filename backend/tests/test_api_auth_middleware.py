"""统一 API 鉴权中间件回归测试。

背景：`settings.api_auth_token` 与前端 `X-API-Key` 注入链路早已存在，
但后端**从未有任何中间件读取该配置** —— "非空时所有 API 必须带 token"
只是一个写在注释里的空契约，默认空值下服务完全裸奔（监听 0.0.0.0 时
任何可达客户端都能读写项目数据、上传文档、触发 AI 调用）。

本文件锁定中间件行为：
- token 为空 → 完全放行（本地单机默认行为不变）
- token 非空 → 除豁免路径外一律 401，且支持 X-API-Key / Bearer 两种携带方式
- 豁免路径（/health、接口文档）无需凭据
- CORS 预检 OPTIONS 不得被鉴权拦成 401（否则浏览器跨域调用整体失效）
"""
import pytest
from app.config import settings
from app.middleware import setup_middleware
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "s3cret-token-for-tests"


def _make_client(monkeypatch, token: str) -> TestClient:
    monkeypatch.setattr(settings, "api_auth_token", token, raising=False)
    app = FastAPI()

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/projects")
    async def list_projects():
        return {"items": ["p1"]}

    @app.post("/api/v1/global-facts/upload-documents")
    async def upload():
        return {"ok": True}

    setup_middleware(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# token 为空：行为必须与改造前完全一致
# ---------------------------------------------------------------------------

def test_empty_token_allows_everything(monkeypatch):
    client = _make_client(monkeypatch, "")
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.post("/api/v1/global-facts/upload-documents").status_code == 200


# ---------------------------------------------------------------------------
# token 非空：拦截与放行
# ---------------------------------------------------------------------------

def test_token_required_when_configured(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects")
    assert resp.status_code == 401
    body = resp.json()
    assert "X-API-Key" in body["detail"]
    # 401 语义必须可被客户端识别，且不允许缓存
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert resp.headers.get("cache-control") == "no-store"


def test_x_api_key_header_accepted(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects", headers={"X-API-Key": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["items"] == ["p1"]


def test_bearer_token_accepted(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


def test_bearer_prefix_is_case_insensitive(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects",
                      headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200


def test_wrong_or_missing_credential_rejected(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    assert client.get("/api/v1/projects",
                      headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/projects",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/v1/projects",
                      headers={"Authorization": TOKEN}).status_code == 401


def test_write_endpoints_also_protected(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    assert client.post("/api/v1/global-facts/upload-documents").status_code == 401
    assert client.post("/api/v1/global-facts/upload-documents",
                       headers={"X-API-Key": TOKEN}).status_code == 200


def test_health_is_exempt(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    assert client.get("/api/v1/health").status_code == 200


def test_cors_preflight_not_blocked_by_auth(monkeypatch):
    """回归：预检 OPTIONS 不带自定义头，若被鉴权拦截，跨域调用会整体失效。"""
    client = _make_client(monkeypatch, TOKEN)
    resp = client.options(
        "/api/v1/projects",
        headers={"Origin": "http://lan-host:5175",
                 "Access-Control-Request-Method": "GET"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_health_response_not_leaked_when_unauthorized(monkeypatch):
    """未授权请求不得触达业务处理函数（响应体必须是鉴权错误而非业务数据）。"""
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects")
    assert "items" not in resp.text


def test_non_ascii_api_key_returns_401_not_500(monkeypatch):
    """回归：非 ASCII 凭据必须走「拒绝」而不是「崩溃」。

    `secrets.compare_digest` 对 str 形态要求两侧都是纯 ASCII，一旦客户端发来
    含非 ASCII 字节的 X-API-Key，它会抛 TypeError。若未捕获，请求会变成 500，
    既把内部异常暴露成可探测信号，又给日志刷堆栈（可低成本刷噪声）。
    修法：比较前统一编码成 bytes（仍是常数时间）。

    注意：header 值必须用 **bytes** 传（httpx 对 str 会强制 ASCII 编码，发不出去）。
    """
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects", headers={"X-API-Key": b"t\xf6k\xe9n"})
    assert resp.status_code == 401, f"期望 401，实际 {resp.status_code}: {resp.text[:120]}"


def test_non_ascii_bearer_token_returns_401_not_500(monkeypatch):
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects",
                      headers={"Authorization": b"Bearer t\xf6k\xe9n"})
    assert resp.status_code == 401


@pytest.mark.parametrize("raw", [
    b"t\xf6k\xe9n",          # 非 ASCII（latin-1 可表示）
    b"\xff\xfe\xfd",         # 高位字节
    b"   ",                  # 全空白 → strip 后为空
    b"x" * 5000,             # 超长
    b"",                     # 空
])
def test_malformed_credentials_never_produce_5xx(monkeypatch, raw):
    """任何畸形凭据都只能得到 401：鉴权中间件不得因输入异常而崩溃。"""
    client = _make_client(monkeypatch, TOKEN)
    resp = client.get("/api/v1/projects", headers={"X-API-Key": raw})
    assert resp.status_code == 401, f"{raw!r} -> {resp.status_code}"

