"""请求级 trace_id 全链路追踪回归测试（可观测性，2026-09-23）。

背景（日志审计）：REST 请求日志 `GET /path -> 200 (12ms)` 无任何关联 ID，
无法与后续业务日志（TraceContextFilter 自动前缀）串联成一条链路。
现由 RequestLogMiddleware 统一绑定：上游传入则复用，否则生成；
响应头回写 X-Trace-Id；请求日志显式携带 [trace=…]。

纯 ASGI 直测，不依赖 HTTP 客户端。
"""
import logging

from app.middleware import RequestLogMiddleware
from app.utils.log_context import context_suffix


def _scope(headers=()) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/ping",
        "headers": [(k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in headers],
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _make_send(captured: list):
    async def send(message: dict) -> None:
        captured.append(message)
    return send


async def _app_ok(scope, receive, send):
    """路由层探针：把当前链路上下文写进响应体，验证 contextvars 透传。"""
    suffix = context_suffix()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": suffix.encode()})


def _start_of(captured: list) -> dict:
    return next(m for m in captured if m["type"] == "http.response.start")


def _body_of(captured: list) -> str:
    return next(m for m in captured
                if m["type"] == "http.response.body")["body"].decode()


async def test_trace_id_generated_and_returned():
    """未携带上游 ID：应生成 trace 并回写响应头，且透传进路由层上下文。"""
    mw = RequestLogMiddleware(_app_ok)
    captured: list = []
    await mw(_scope(), _receive, _make_send(captured))

    start = _start_of(captured)
    names = {h[0] for h in start["headers"]}
    assert b"x-trace-id" in names, "响应必须回写 X-Trace-Id"

    tid = dict(start["headers"])[b"x-trace-id"].decode()
    assert len(tid) == 12 and tid.isalnum(), "生成的 trace 应为 12 位短 ID"
    assert _body_of(captured) == f"trace={tid}", \
        "路由层应能看到中间件绑定的 trace（contextvars 透传）"


async def test_incoming_trace_id_reused():
    """上游传入 X-Trace-Id：应原样复用（跨服务串联），不另生成。"""
    mw = RequestLogMiddleware(_app_ok)
    captured: list = []
    await mw(_scope([("X-Trace-Id", "gw-abc123")]), _receive, _make_send(captured))

    start = _start_of(captured)
    hdr = {h[0]: h[1] for h in start["headers"]}
    assert hdr[b"x-trace-id"] == b"gw-abc123"
    assert _body_of(captured) == "trace=gw-abc123"


async def test_incoming_trace_id_sanitized():
    """异常超长/含特殊字符的传入 ID：清洗为安全字符并限长 64。"""
    mw = RequestLogMiddleware(_app_ok)
    captured: list = []
    dirty = "a" * 100 + "\n<bad>"
    await mw(_scope([("x-request-id", dirty)]), _receive, _make_send(captured))

    tid = dict(_start_of(captured)["headers"])[b"x-trace-id"].decode()
    assert tid == "a" * 64, "应截断到 64 且剔除非法字符（\\n 与 <> 被清洗）"


async def test_request_log_contains_trace(caplog):
    """请求日志行必须显式携带 [trace=…]，可与业务日志 grep 串联。"""
    mw = RequestLogMiddleware(_app_ok)
    captured: list = []
    with caplog.at_level(logging.INFO, logger="http"):
        await mw(_scope(), _receive, _make_send(captured))
    assert any("[trace=" in r.getMessage() for r in caplog.records), \
        "请求日志应包含 [trace=…] 前缀"


async def test_response_header_not_duplicated():
    """路由层已写同头时不重复追加（中间件让位，不覆盖业务语义）。"""
    async def app_with_header(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"x-trace-id", b"from-app")]})
        await send({"type": "http.response.body", "body": b""})

    mw = RequestLogMiddleware(app_with_header)
    captured: list = []
    await mw(_scope(), _receive, _make_send(captured))
    headers = [h[1] for h in _start_of(captured)["headers"]
               if h[0] == b"x-trace-id"]
    assert headers == [b"from-app"]
