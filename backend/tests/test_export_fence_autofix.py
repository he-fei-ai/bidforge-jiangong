"""导出链路中「未闭合围栏自动修复」开关的接线验证（2026-09-22）。

背景：auto_fix_unclosed_fences 此前仅被单测覆盖，从未接入导出链路，
导致预检报出 DLV-07 后只能靠用户手工补全。本次在 _prepare_export 中
新增可选开关 auto_fix_unclosed_fences（默认关闭，向后兼容），
在体检与 blocks 缓存之前对每章正文补齐未闭合 ``` / ~~~ 围栏。

本文件验证：
  · 开关开启时，未闭合围栏被追加闭合标记，原正文不丢、DLV 检测清零；
  · 开关默认关闭时，正文完全不改写（向后兼容，零回归）。
"""
from __future__ import annotations

import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers.export import _prepare_export
from app.services.content_utils import find_unclosed_fences


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "export-fence-autofix.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "s", "目录已确认"))
    await db.commit()
    yield db, pid, sid
    # ✅ 关闭全局连接：aiosqlite worker 为非守护线程，不关闭会阻塞解释器退出
    #   （表现为 pytest 全绿后进程挂起）。
    await close_db()


async def _add_section(db, sid, sec_id, content):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " level, status, word_count, word_budget, content, review_status,"
        " sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, "", "", "第一章", 1, "generated", 100, 0,
         content, "", 0))
    await db.commit()


async def test_export_auto_fix_applies_when_enabled(db_ctx):
    """开关开启：未闭合 ~~~ 围栏被追加闭合，原正文保留，DLV 检测清零。"""
    db, _pid, sid = db_ctx
    sec_id = uuid.uuid4().hex
    raw = "前言\n~~~python\ndef f(): pass"  # 未闭合波浪围栏
    await _add_section(db, sid, sec_id, raw)

    prep = await _prepare_export(
        sid, {"config": {"auto_fix_unclosed_fences": True}}, db)
    sec = next(s for s in prep["sections"] if s["id"] == sec_id)

    assert raw in sec["content"], "原正文不应被改动/丢失"
    assert sec["content"].rstrip().endswith("~~~"), "应追加 ~~~ 闭合标记"
    assert find_unclosed_fences(sec["content"]) == [], "修复后不应再有未闭合围栏"


async def test_export_auto_fix_default_off_does_not_rewrite(db_ctx):
    """默认关闭（向后兼容）：正文完全不改写。"""
    db, _pid, sid = db_ctx
    sec_id = uuid.uuid4().hex
    raw = "前言\n```\n未完成代码"
    await _add_section(db, sid, sec_id, raw)

    prep = await _prepare_export(sid, {"config": {}}, db)
    sec = next(s for s in prep["sections"] if s["id"] == sec_id)
    assert sec["content"] == raw, "默认关闭时不应改写正文"
