"""目录生成并发自我守卫测试（2026-10-03）

修复目标：generate_outline / generate_content 此前只被 save-outline / reorder /
delete / update 等"写 sections 表"的端点反向守卫（它们检查
outline_generation_in_progress / content_generation_in_progress），自身从不检查
"是否已有同型任务在跑"。两路并发同型生成会：
  · 目录：各自写 task_registry.checkpoint_json，较慢那路收尾可能用空/旧 checkpoint
    覆盖先完成那路的成果；
  · 正文：落库走 _persist_section 的 _db_write_lock，而该锁每任务实例独立，
    两路并发各自持独立锁 → DB 写无互斥，章节正文互相覆盖/丢失更新。
现与下游写库端点同口径：同型任务在跑（running/paused）即 409 拒绝重入。
"""
import pytest
from app.routers.sse_handlers import generate_content, generate_outline
from fastapi import HTTPException


class _FakeReq:
    async def json(self):
        return {}


async def _seed_scheme(db, sid="s1", pid="p1"):
    await db.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)", (pid, "项目"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, status) "
        "VALUES (?, ?, ?, ?)",
        (sid, pid, "测试方案", "草稿"))
    await db.commit()


@pytest.mark.asyncio
async def test_generate_outline_rejects_concurrent(db_conn, monkeypatch):
    await _seed_scheme(db_conn)
    monkeypatch.setattr(
        "app.routers.sections.outline_generation_in_progress",
        lambda sid: "running")
    with pytest.raises(HTTPException) as exc:
        await generate_outline("s1", None, db_conn)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_generate_content_rejects_concurrent(db_conn, monkeypatch):
    await _seed_scheme(db_conn)
    monkeypatch.setattr(
        "app.routers.sections.content_generation_in_progress",
        lambda sid: "running")
    with pytest.raises(HTTPException) as exc:
        await generate_content("s1", _FakeReq(), db_conn)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_guard_helper_contract(db_conn, monkeypatch):
    """守卫不应误伤空闲态：真实 helper 在 _tasks 为空时返回 None（非 running）。"""
    await _seed_scheme(db_conn)
    # 真实 helper 依赖 task_registry._tasks，测试间由 conftest 清空，应为 None
    from app.routers.sections import outline_generation_in_progress
    assert outline_generation_in_progress("s1") is None
    # 守卫在空闲态不抛 409（后续会因无 AI 而走别的分支，但一定不是 409 守卫）
    try:
        await generate_outline("s1", None, db_conn)
    except HTTPException as e:
        assert e.status_code != 409, "空闲态不应被并发守卫拦截"
