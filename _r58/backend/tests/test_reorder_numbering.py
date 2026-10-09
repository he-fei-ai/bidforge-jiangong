"""目录拖拽排序后章节编号回写回归测试

覆盖 reorder_sections / renumber_sections_after_reorder：
- 拖拽改变顺序后，sections.outline_json.id 必须按新顺序重排，
  否则正文生成提示词经 _section_outline_number 读取的「当前章节编号」会错乱。
"""
import asyncio
import json
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers.sections import renumber_sections_after_reorder, reorder_sections


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, sid
    await db.execute("DELETE FROM sections WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()
    # ✅ 关闭全局连接：aiosqlite worker 为非守护线程，不关闭会阻塞解释器退出
    #   （表现为 pytest 全绿后进程挂起）。
    await close_db()


async def _seed(db, sid, pid, nodes: list):
    """nodes: [(dbid, parent_id, title, sort_order, level)]"""
    for nid, parent, title, so, level in nodes:
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
            " sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nid, sid, pid, parent, title, level, so, "empty",
             json.dumps({"id": str(level), "confidence": 0.9}, ensure_ascii=False), 1500))
    await db.commit()


async def _outline_ids(db, sid) -> dict:
    cur = await db.execute(
        "SELECT id, outline_json FROM sections WHERE scheme_id=? ORDER BY sort_order", (sid,))
    return {r["id"]: json.loads(r["outline_json"])["id"] for r in await cur.fetchall()}


class TestReorderNumbering:
    async def test_reorder_rewrites_outline_id(self, ctx):
        db, sid = ctx
        pid = "proj"
        # 三个一级章节 + 第 2 章下一个子节
        s1, s2, s3, s2c = (uuid.uuid4().hex for _ in range(4))
        await _seed(db, sid, pid, [
            (s1, "", "工程概况", 0, 1),
            (s2, "", "施工计划", 1, 1),
            (s2c, s2, "进度安排", 0, 2),
            (s3, "", "安全保证措施", 2, 1),
        ])
        # 拖拽：把第 3 章移到最前（顺序变为 s3, s1, s2）
        await reorder_sections(sid, {"order": [s3, s1, s2, s2c]}, db)
        ids = await _outline_ids(db, sid)
        assert ids[s3] == "1", ids
        assert ids[s1] == "2", ids
        assert ids[s2] == "3", ids
        assert ids[s2c] == "3.1", ids  # 第 2 章（现第 3 章）下子节应为 3.1

    async def test_helper_idempotent(self, ctx):
        db, sid = ctx
        pid = "proj"
        s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(db, sid, pid, [
            (s1, "", "A", 0, 1),
            (s2, "", "B", 1, 1),
        ])
        await renumber_sections_after_reorder(db, sid)
        ids = await _outline_ids(db, sid)
        assert ids[s1] == "1" and ids[s2] == "2"

    async def test_confidence_preserved(self, ctx):
        db, sid = ctx
        s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(db, sid, "proj", [
            (s1, "", "A", 1, 1),
            (s2, "", "B", 0, 1),
        ])
        await reorder_sections(sid, {"order": [s2, s1]}, db)
        cur = await db.execute("SELECT id, outline_json FROM sections WHERE scheme_id=?", (sid,))
        rows = {r["id"]: json.loads(r["outline_json"]) for r in await cur.fetchall()}
        # 顺序交换后，confidence 等既有字段应保留，仅 id/level 变化
        assert rows[s2]["id"] == "1" and rows[s2]["confidence"] == 0.9
        assert rows[s1]["id"] == "2" and rows[s1]["confidence"] == 0.9


if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-q"]))
