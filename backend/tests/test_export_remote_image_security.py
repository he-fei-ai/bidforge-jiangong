"""导出远程图片下载的安全回归测试。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.routers.export import (
    _content_fingerprint,
    _download_remote_image,
    _global_facts_status_headers,
    _query_global_facts,
    _validate_remote_image_url,
    collect_export_issues,
)


def test_validate_remote_image_url_blocks_private_and_metadata(monkeypatch):
    """私网、云元数据和非法协议必须在发起下载前被拒绝。"""
    monkeypatch.setattr(
        "app.routers.export.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.8", 80))],
    )

    for url in (
        "http://10.0.0.8/image.png",
        "http://169.254.169.254/latest/meta-data",
        "file:///C:/Windows/win.ini",
        "ftp://example.com/image.png",
    ):
        with pytest.raises(ValueError):
            _validate_remote_image_url(url)


def test_validate_remote_image_url_allows_localhost_and_common_https(monkeypatch):
    """默认兼容本机开发地址与正常公网 HTTPS。"""
    def _resolve(host, *args, **kwargs):
        if host.lower() == "localhost":
            return [(2, 1, 6, "", ("127.0.0.1", 80))]
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("app.routers.export.socket.getaddrinfo", _resolve)

    assert _validate_remote_image_url("http://localhost:8000/image.png")
    assert _validate_remote_image_url("https://cdn.example.com/image.png")


def test_validate_remote_image_url_can_disable_localhost(monkeypatch):
    """关闭兼容开关后，本机地址也必须被拒绝。"""
    from app.config import settings

    monkeypatch.setattr(settings, "image_download_allow_localhost", False)
    monkeypatch.setattr(
        "app.routers.export.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    with pytest.raises(ValueError, match="内网"):
        _validate_remote_image_url("http://localhost/image.png")


def test_global_facts_status_changes_fingerprint_and_is_encoded():
    """查询失败与正常空事实不能共用同一个导出缓存指纹。"""
    base = {
        "config": {}, "fe_codes": [], "sections": [], "chart_fp": [],
        "scheme": {"name": "方案", "project_id": "p1"}, "global_facts": [],
    }
    failed = dict(base, global_facts_status={"ok": False, "code": "query_failed", "count": 0})
    empty = dict(base, global_facts_status={"ok": True, "code": "empty", "count": 0})

    assert _content_fingerprint(failed)[1] != _content_fingerprint(empty)[1]
    header = _global_facts_status_headers(failed["global_facts_status"])
    assert "query_failed" in header["X-Global-Facts-Status"]


@pytest.mark.asyncio
async def test_download_remote_image_rejects_redirect_to_private_address(monkeypatch):
    """重定向目标必须逐跳复检，不能借公网 URL 绕过 SSRF 限制。"""
    monkeypatch.setattr(
        "app.routers.export._validate_remote_image_url",
        lambda url: (_ for _ in ()).throw(ValueError("重定向目标为私网地址"))
        if "127.0.0.1" in url else url,
    )

    class _Response:
        status_code = 302
        headers = {"location": "http://127.0.0.1/image.png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            if False:
                yield b""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.routers.export.httpx.AsyncClient", _Client, raising=False
    )
    with pytest.raises(ValueError, match="私网"):
        await _download_remote_image("https://cdn.example.com/image.png")


@pytest.mark.asyncio
async def test_download_remote_image_stops_at_content_length_limit(monkeypatch):
    """已知 Content-Length 超限时不得分配或读取完整响应体。"""
    monkeypatch.setattr(
        "app.routers.export._validate_remote_image_url", lambda url: url)

    class _Response:
        status_code = 200
        headers = {"content-length": "999999"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            raise AssertionError("超限响应不应进入响应体读取")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    with pytest.raises(ValueError, match="大小超过限制"):
        await _download_remote_image("https://cdn.example.com/image.png", max_bytes=10)


@pytest.mark.asyncio
async def test_download_remote_image_streams_within_limit(monkeypatch):
    """无 Content-Length 的分块响应也必须在累计超限时立即停止。"""
    monkeypatch.setattr(
        "app.routers.export._validate_remote_image_url", lambda url: url)

    class _Response:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield b"123456"
            yield b"789012"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    with pytest.raises(ValueError, match="大小超过限制"):
        await _download_remote_image("https://cdn.example.com/image.png", max_bytes=10)


@pytest.mark.asyncio
async def test_download_remote_image_returns_bounded_body(monkeypatch):
    """合法公网图片仍能按原有下载路径返回字节。"""
    monkeypatch.setattr(
        "app.routers.export._validate_remote_image_url", lambda url: url)

    class _Response:
        status_code = 200
        headers = {"content-length": "3"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield b"png"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url):
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    assert await _download_remote_image(
        "https://cdn.example.com/image.png", max_bytes=10) == b"png"


@pytest.mark.asyncio
async def test_query_global_facts_marks_query_failure():
    """事实查询异常仍保持空列表降级，但必须写入可观测状态。"""
    class _DB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("数据库暂不可用")

    status = {}
    facts = await _query_global_facts(_DB(), "s1", status=status)

    assert facts == []
    assert status == {"ok": False, "code": "query_failed", "count": 0}


@pytest.mark.asyncio
async def test_query_global_facts_marks_successful_empty():
    """正常无数据与查询失败必须是两种不同状态。"""
    class _Cursor:
        async def fetchall(self):
            return []

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Cursor()

    status = {}
    facts = await _query_global_facts(_DB(), "s1", status=status)

    assert facts == []
    assert status["ok"] is True
    assert status["code"] == "empty"
    assert status["count"] == 0
