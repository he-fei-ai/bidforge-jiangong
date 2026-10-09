"""目录生成运行中，单章节变更端点必须有 409 竞态守卫（2026-09-23 · 守卫缺口修复）。

背景：save-outline / apply-and-save / update_section / reset_content 已有
outline_generation_in_progress 守卫，但 create_section / delete_section /
reorder_sections（及 update_section 的目录口径）此前零守卫 —— 目录生成运行中
增删改/拖拽会被 AI 确认闸门的整表重建静默覆盖（丢失更新），并与
renumber_sections_after_reorder 构成同表写写竞态。

守卫口径：只拦 running/paused；终态残留不误拦（对齐 G12-4 / G13-2）。
"""
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.models import SectionCreate, SectionUpdate
from app.routers.sections import (
    create_section,
    delete_section,
    reorder_sections,
    update_section,
)
from app.services.ai.task_registry import finish_task, register_task
from fastapi import HTTPException


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "guard.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
                     (sid, pid, "s"))
    await db.commit()
    yield db, sid
    await db.execute("DELETE FROM sections WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM task_registry", ())
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()
    # ✅ 关闭全局连接：非守护线程不关闭会阻塞解释器退出
    await close_db()


async def _seed_section(db, sid, pid, title="工程概况") -> str:
    nid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, level,"
        " sort_order, status, outline_json, word_budget)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, sid, pid, title, 1, 0, "empty",
         '{"id": "1", "confidence": 0.9}', 1500))
    await db.commit()
    return nid


class TestOutlineGuardOnSectionEndpoints:
    async def test_create_blocked_while_outline_running(self, ctx):
        db, sid = ctx
        tid = await register_task("outline_generation", "", sid)
        try:
            with pytest.raises(HTTPException) as ei:
                await create_section(sid, SectionCreate(title="新章"), db)
            assert ei.value.status_code == 409
        finally:
            await finish_task(tid, "completed")

    async def test_update_blocked_while_outline_running(self, ctx):
        db, sid = ctx
        nid = await _seed_section(db, sid, "proj")
        tid = await register_task("outline_generation", "", sid)
        try:
            with pytest.raises(HTTPException) as ei:
                await update_section(
                    sid, nid, SectionUpdate(title="改名"), db)
            assert ei.value.status_code == 409
        finally:
            await finish_task(tid, "completed")

    async def test_delete_blocked_while_outline_running(self, ctx):
        db, sid = ctx
        nid = await _seed_section(db, sid, "proj")
        tid = await register_task("outline_generation", "", sid)
        try:
            with pytest.raises(HTTPException) as ei:
                await delete_section(sid, nid, db)
            assert ei.value.status_code == 409
        finally:
            await finish_task(tid, "completed")

    async def test_reorder_blocked_while_outline_running(self, ctx):
        db, sid = ctx
        nid = await _seed_section(db, sid, "proj")
        tid = await register_task("outline_generation", "", sid)
        try:
            with pytest.raises(HTTPException) as ei:
                await reorder_sections(sid, {"order": [nid]}, db)
            assert ei.value.status_code == 409
        finally:
            await finish_task(tid, "completed")

    async def test_all_allowed_after_terminal(self, ctx):
        """终态残留不得误拦：任务完成后四个端点必须全部正常可用。"""
        db, sid = ctx
        nid = await _seed_section(db, sid, "proj")
        tid = await register_task("outline_generation", "", sid)
        await finish_task(tid, "completed")
        created = await create_section(sid, SectionCreate(title="新章"), db)
        assert created.get("id")
        updated = await update_section(
            sid, nid, SectionUpdate(title="改名后"), db)
        assert updated.get("ok") is True
        deleted = await delete_section(sid, nid, db)
        assert deleted.get("ok") is True
        nid2 = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, level,"
            " sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (nid2, sid, "proj", "工程概况", 1, 0, "empty",
             '{"id": "1", "confidence": 0.9}', 1500))
        await db.commit()
        reordered = await reorder_sections(sid, {"order": [nid2, created["id"]]}, db)
        assert reordered.get("ok") is True

    async def test_other_scheme_task_does_not_block(self, ctx):
        """别的方案在跑目录生成，本方案的编辑不受影响。"""
        db, sid = ctx
        tid = await register_task("outline_generation", "", "other-scheme")
        try:
            created = await create_section(sid, SectionCreate(title="新章"), db)
            assert created.get("id")
        finally:
            await finish_task(tid, "completed")
