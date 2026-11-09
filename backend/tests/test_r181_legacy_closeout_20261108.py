"""R181 遗留收口（2026-11-08）：G2-webp / G8-T3 / T4 三项净新增修复的护栏与功能实证。

对应 R176 登记遗留项的最优方案执行：
- G2-webp：扩展名白名单补齐 WebP（PIL 原生解码 → 既有 OCR 通道，零新增依赖）；
- G8/T3：目录识别 AI 兜底截断阈值 settings 化（默认 6000，行为不变）；
- T4：uploaded_outlines 诊断回放只读端点（暴露已落库的 parse_warnings）。

V4（doc_pipeline 四层存储）经逐行审计已确认 R13 全链路加固到位，本轮无新增改动。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from app import db as _appdb
from app.db import get_conn, init_db
from app.routers import upload_outline as uo
from app.services import file_parser as fp
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# G2-webp
# ---------------------------------------------------------------------------
def test_webp_in_supported_extensions():
    assert "webp" in {e.lower() for e in fp.SUPPORTED_EXTENSIONS}


def test_webp_signature_valid_accepts_real_and_rejects_other_riff():
    webp_prefix = b"RIFF\x99\x00\x00\x00WEBP" + b"\x00" * 4
    assert fp.signature_valid("webp", webp_prefix) is True
    # 其它 RIFF 容器（wav/avi 等）不得被当作 webp 放行（防止 .wav 改名 .webp 绕过）
    assert fp.signature_valid("webp", b"RIFF\x99\x00\x00\x00WAVE" + b"\x00" * 4) is False
    assert fp.signature_valid("webp", b"RIFF\x99\x00\x00\x00XXXX" + b"\x00" * 4) is False


def test_webp_sniff_type():
    assert fp._sniff_type(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "webp"


def test_webp_routes_to_image_ocr(monkeypatch):
    sentinel = "<OCR_TEXT>"
    monkeypatch.setattr(fp, "_parse_image_ocr", lambda content, ftype, diag=None: sentinel)
    text, _diag = fp.parse_file_content_ex(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32, "webp")
    assert text == sentinel, "webp 必须路由到图片 OCR 通道，而非落默认文本分支"


# ---------------------------------------------------------------------------
# G8/T3
# ---------------------------------------------------------------------------
def test_ai_fallback_max_chars_default_is_6000():
    from app.config import settings

    assert settings.outline_ai_fallback_max_chars == 6000


def test_ai_fallback_no_longer_hardcoded():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "upload_outline.py").read_text(
        encoding="utf-8"
    )
    # 旧的硬编码 6000 切片必须消失，改为 settings 引用（默认值仍 6000，行为不变）
    assert "raw_text[:6000]" not in src, "AI 兜底截断阈值不得再硬编码 6000"
    assert "settings.outline_ai_fallback_max_chars" in src


# ---------------------------------------------------------------------------
# T4
# ---------------------------------------------------------------------------
@pytest.fixture
async def up_db(tmp_path):
    _appdb.DB_PATH = tmp_path / "r181.sqlite"
    await init_db()
    db = await get_conn()
    yield db


async def test_upload_diagnostics_returns_parse_warnings(up_db):
    uid = "u1"
    warnings = ["PDF 截页：仅解析前 20 页", "OCR 兜底识别"]
    await up_db.execute(
        "INSERT INTO uploaded_outlines (id, project_id, file_name, file_type, raw_text,"
        " parsed_json, confidence, status, parse_warnings)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, "p", "a.pdf", "pdf", "正文", json.dumps({"outline": []}), 0.8, "parsed", json.dumps(warnings)),
    )
    await up_db.commit()
    res = await uo.get_upload_diagnostics(uid, db=up_db)
    assert res["upload_id"] == uid
    assert res["file_name"] == "a.pdf"
    assert res["file_type"] == "pdf"
    assert res["status"] == "parsed"
    assert res["parse_warnings"] == warnings


async def test_upload_diagnostics_404_for_missing(up_db):
    with pytest.raises(HTTPException) as ei:
        await uo.get_upload_diagnostics("nope", db=up_db)
    assert ei.value.status_code == 404
