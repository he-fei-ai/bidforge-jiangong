"""CORS / 请求日志 / API 鉴权中间件"""
import json
import logging
import secrets
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.utils.log_context import clear_context, new_trace_id, set_context

logger = logging.getLogger("http")

# 无需鉴权的路径（精确匹配）。仅保留健康检查与接口文档：
#   - /health 供探活与前端启动自检使用；
#   - /docs、/redoc、/openapi.json 供内网排障查阅，本身不含业务数据。
# 其余 /api/v1/* 在配置了 API_AUTH_TOKEN 后一律要求携带凭据。
_AUTH_EXEMPT_PATHS = frozenset({
    "/api/v1/health",
    "/docs", "/redoc", "/openapi.json",
})
_AUTH_EXEMPT_PREFIXES = ("/docs/", "/redoc/")


def _extract_token(headers: list[tuple[bytes, bytes]]) -> str:
    """从请求头提取凭据：优先 X-API-Key，其次 Authorization: Bearer <token>。"""
    api_key = ""
    authorization = ""
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        if name == "x-api-key":
            api_key = raw_value.decode("latin-1").strip()
        elif name == "authorization":
            authorization = raw_value.decode("latin-1").strip()
    if api_key:
        return api_key
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


class ApiAuthMiddleware:
    """统一 API 鉴权（纯 ASGI，零额外任务跳转）。

    行为约定：
    - `API_AUTH_TOKEN` 为空 → 完全放行（本地单机默认，兼容既有前端与调用方式）；
    - 非空 → 除豁免路径外，所有请求必须携带 `X-API-Key: <token>`
      或 `Authorization: Bearer <token>`，否则返回 401。
    - 比较使用 `secrets.compare_digest`（常数时间），避免时序侧信道。
    - 预检 `OPTIONS` 直接放行（由外层 CORSMiddleware 处理，且预检不带自定义头）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._token = (settings.api_auth_token or "").strip()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._token:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        if method == "OPTIONS" or path in _AUTH_EXEMPT_PATHS \
                or path.startswith(_AUTH_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        provided = _extract_token(scope.get("headers") or [])
        # ⚠️ 用 bytes 比较而不是 str：`secrets.compare_digest` 对 str 要求两侧都是
        #    纯 ASCII，攻击者只要发一个非 ASCII 字节的 X-API-Key（例如 0xE9），
        #    就会让它抛 TypeError → 未捕获 → 500，既暴露异常堆栈又污染日志。
        #    bytes 形态没有这个限制，且依然是常数时间比较。
        if provided and secrets.compare_digest(provided.encode("utf-8"),
                                              self._token.encode("utf-8")):
            await self.app(scope, receive, send)
            return

        logger.warning("拒绝未授权请求 %s %s（客户端地址 %s）", method, path,
                       (scope.get("client") or ("?",))[0])
        body = json.dumps(
            {"detail": "未授权：请在请求头携带 X-API-Key 或 Authorization: Bearer <token>"},
            ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
                # 让浏览器端能读到 401 语义，且不缓存
                (b"cache-control", b"no-store"),
                (b"www-authenticate", b'Bearer realm="scheme-assistant"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def log_auth_configuration() -> None:
    """启动时输出鉴权配置状态，避免"以为开了鉴权其实没开"。"""
    token = (settings.api_auth_token or "").strip()
    if token:
        logger.info("API 鉴权已启用：所有 /api/v1/* 请求需携带 X-API-Key 或 Bearer Token")
    else:
        logger.warning(
            "API 鉴权未启用（API_AUTH_TOKEN 为空）：任何可达网络客户端均可读写项目数据。"
            "若服务监听 0.0.0.0 或暴露到内网/公网，请在 backend/.env 设置 API_AUTH_TOKEN")


class RequestLogMiddleware:
    """纯 ASGI 请求日志 + 全链路追踪中间件（P1-4 替代 BaseHTTPMiddleware 消除额外任务跳转）

    ✅ 可观测性（2026-09-23）：为每个 HTTP 请求绑定 trace_id 并透传全链路：
    - 优先复用上游传入的 X-Trace-Id / X-Request-Id（网关/跨服务串联）；
      清洗为 [字母数字-] 且限长 64，防异常超长头污染日志；
    - 否则生成 12 位短 ID（log_context.new_trace_id）；
    - 响应头回写 X-Trace-Id（CORS expose_headers 已放行，前端可读取）；
    - 入口先 clear_context 再绑定，杜绝 keep-alive 同连接上一请求的
      project/doc/task 残留串入本次日志；SSE 等长任务在路由层另行覆盖。
    - 本行请求日志显式携带 [trace=…]，与 TraceContextFilter（main.py 启动挂载）
      的自动前缀双保险，grep 串联全链路。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _incoming_trace_id(headers: list[tuple[bytes, bytes]]) -> str:
        for raw_name, raw_value in headers:
            name = raw_name.decode("latin-1").lower()
            if name in ("x-trace-id", "x-request-id"):
                tid = "".join(
                    ch for ch in raw_value.decode("latin-1").strip()
                    if ch.isalnum() or ch == "-")[:64]
                if tid:
                    return tid
        return ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.time()
        status_code = 500
        method = scope.get("method", "")
        path = scope.get("path", "")

        # 请求入口重建干净链路上下文 → 绑定本次 trace_id（同任务内 await 路由，
        # contextvars 天然透传到路由层与 handler 内的子调用）。
        clear_context()
        trace_id = self._incoming_trace_id(scope.get("headers") or [])
        if trace_id:
            set_context(trace_id=trace_id)
        else:
            trace_id = new_trace_id()

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers") or [])
                if not any(h[0].lower() == b"x-trace-id" for h in headers):
                    headers.append((b"x-trace-id", trace_id.encode("latin-1")))
                message = dict(message, headers=headers)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("请求异常 %s %s [trace=%s]", method, path, trace_id)
            raise
        cost = (time.time() - start) * 1000
        if path not in ("/api/v1/health",):
            logger.info("%s %s -> %s (%.0fms) [trace=%s]", method, path,
                        status_code, cost, trace_id)


def setup_middleware(app):
    from starlette.middleware.cors import CORSMiddleware

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    # 开发环境全放行（Vite proxy 同源转发时不会真的跨域，这里主要为直接跨域访问和 LAN origin 场景）
    # ✅ 跨域部署时浏览器默认只暴露简单响应头；图表渲染统计/修复统计/缓存状态
    # 必须显式 expose，否则前端读取恒为 undefined，统计与降级提示静默失效。
    _expose_headers = ["X-Chart-Render-Stats", "X-Fix-Stats", "X-Cache-Status",
                       # 导出文件名/轮次（blob 下载时前端用它设置 a.download，否则跨域下读不到）
                       "X-Export-Filename", "X-Export-Round",
                       # ✅ 可观测性（2026-09-23）：请求级追踪 ID，前端/网关可读取用于报障定位
                       "X-Trace-Id"]
    if "*" in origins or not origins:
        cors_kwargs = dict(
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=_expose_headers,
        )
    else:
        cors_kwargs = dict(
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=_expose_headers,
        )
    # ⚠️ add_middleware 的注册顺序 = 内层 → 外层（最后注册的最先看到请求）。
    #    这里刻意让 CORS 处于最外层：预检 OPTIONS 由 CORSMiddleware 直接短路应答，
    #    否则会被鉴权中间件拦成 401，导致浏览器端跨域调用全部失败。
    #    实际执行顺序：CORS → RequestLog → ApiAuth → 路由。
    app.add_middleware(ApiAuthMiddleware)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(CORSMiddleware, **cors_kwargs)
