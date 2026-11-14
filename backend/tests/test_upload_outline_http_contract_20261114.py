# -*- coding: utf-8 -*-
"""R243 上传目录识别路由 · HTTP 级契约护栏（2026-11-14）。

此前 ``upload_outline`` 三条路由（``parse`` / ``diagnostics`` /
``save-as-outline`` / ``save-as-library``）只有函数级测试，没有任何真实
ASGI HTTP 请求级的契约验证 —— 「路由装饰器错位 / 依赖签名变更 / multipart
字段漂移」这类只在真实请求里暴露的缺陷（历史上 facts SSE 端点曾因装饰器
错位而恒 422）在此模块零检测。本文件走 httpx.ASGITransport + 临时库，
直接锁定用户可见症状：

1. ``POST /parse``：空文件 400 / 不支持扩展名 400 / 内容与扩展名不符 400 /
   不存在的 project_id 404 / 合法文本 200 + outline + confidence；
2. ``GET /{id}/diagnostics``：不存在 id 404 / 解析成功后 200 + parse_warnings；
3. ``POST /{id}/save-as-library``：不存在的上传记录 404。

另锁 ``file_parser.IMAGE_EXTENSIONS`` 单一事实源（R243：OCR 路由与上传置信度
判定共用，且必须是 ``SUPPORTED_EXTENSIONS`` 的图片子集，防字面量漂移）。
"""

import uuid

import app.db as _appdb
import httpx
import pytest
from app.db import close_db, get_conn, init_db
from app.main import app
from app.services.file_parser import IMAGE_EXTENSIONS, SUPPORTED_EXTENSIONS


@pytest.fixture
async def upload_outline_db(tmp_path, monkeypatch):
    """临时库 + 一个真实项目；结束恢复 DB_PATH 并清理连接池。"""
    original = _appdb.DB_PATH
    _appdb.DB_PATH = tmp_path / "upload-outline-http.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid
    await close_db()
    _appdb.DB_PATH = original


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0)


def _valid_text() -> bytes:
    return "第一章 总则\n1.1 项目概况\n1.2 编制依据\n".encode("utf-8")


async def test_parse_valid_text_returns_200_with_outline(upload_outline_db):
    """合法文本：200 + id/outline/confidence，且 parse_warnings 缺省不挂。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/parse",
            files={"file": ("test.txt", _valid_text(), "text/plain")},
        )
    assert resp.status_code == 200, f"合法文本应 200：{resp.text[:300]}"
    data = resp.json()
    assert data.get("id")
    assert isinstance(data.get("outline"), list) and data["outline"]
    assert data.get("confidence") in (0.65, 0.85)
    assert "parse_warnings" not in data  # 无告警时不挂键（加法式）


async def test_parse_empty_file_is_400(upload_outline_db):
    """空文件必须 400（不是 200 + outline=[]，用户误以为识别成功）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/parse",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
    assert resp.status_code == 400, f"空文件应 400：{resp.text[:300]}"
    assert "空文件" in resp.json().get("detail", "")


async def test_parse_unsupported_extension_is_400(upload_outline_db):
    """不支持扩展名（.exe）必须 400，不得静默走文本解码。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/parse",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
    assert resp.status_code == 400, f"不支持扩展名应 400：{resp.text[:300]}"
    assert "不支持" in resp.json().get("detail", "")


async def test_parse_signature_mismatch_is_400(upload_outline_db):
    """扩展名与真实内容不符（.pdf 装文本）必须 400（防 .exe 改名 .pdf）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/parse",
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        )
    assert resp.status_code == 400, f"签名不符应 400：{resp.text[:300]}"
    assert "不符" in resp.json().get("detail", "")


async def test_parse_unknown_project_id_is_404(upload_outline_db):
    """不存在的 project_id 必须 404（建立归属链前先验存在性）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/parse",
            params={"project_id": "no-such-project"},
            files={"file": ("test.txt", _valid_text(), "text/plain")},
        )
    assert resp.status_code == 404, f"未知项目应 404：{resp.text[:300]}"
    assert "项目不存在" in resp.json().get("detail", "")


async def test_diagnostics_missing_id_is_404(upload_outline_db):
    """不存在上传记录：diagnostics 必须 404（不是 200+空）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.get("/api/v1/upload-outline/no-such-id/diagnostics")
    assert resp.status_code == 404, f"未知 id 应 404：{resp.text[:300]}"


async def test_diagnostics_round_trip_after_parse(upload_outline_db):
    """解析成功后 diagnostics 能回读持久化的 parse_warnings（R181 契约）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        created = await client.post(
            "/api/v1/upload-outline/parse",
            files={"file": ("test.txt", _valid_text(), "text/plain")},
        )
        uid = created.json()["id"]
        diag = await client.get(f"/api/v1/upload-outline/{uid}/diagnostics")
    assert diag.status_code == 200, f"diagnostics 应 200：{diag.text[:300]}"
    body = diag.json()
    assert body["upload_id"] == uid
    assert body["file_name"] == "test.txt"
    assert body["status"] == "parsed"
    assert isinstance(body["parse_warnings"], list)


async def test_save_as_library_missing_upload_is_404(upload_outline_db):
    """不存在的上传记录：save-as-library 必须 404（旧实现 0 行 UPDATE 假成功）。"""
    _db, _pid = upload_outline_db
    async with await _client() as client:
        resp = await client.post(
            "/api/v1/upload-outline/no-such-id/save-as-library",
            json={"name": "库", "outline": [{"title": "一", "level": 1, "children": []}]},
        )
    assert resp.status_code == 404, f"未知上传应 404：{resp.text[:300]}"


def test_image_extensions_single_source_subset():
    """IMAGE_EXTENSIONS 必须是 SUPPORTED_EXTENSIONS 的图片子集且含 tif/webp。"""
    assert IMAGE_EXTENSIONS <= SUPPORTED_EXTENSIONS, "图片扩展名必须都在支持全集内"
    assert "tif" in IMAGE_EXTENSIONS  # R243：此前置信度判定漏掉 tif
    assert "tiff" in IMAGE_EXTENSIONS
    assert "webp" in IMAGE_EXTENSIONS
    assert "png" in IMAGE_EXTENSIONS
