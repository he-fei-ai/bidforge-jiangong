"""删章节（delete_section）后代遍历的健壮性护栏（2026-10-03）。

背景（真实缺陷 · 已复现）：delete_section 用 `while pending` 逐层收集被删章节的
全部后代再一次性 DELETE。旧实现**没有 visited 去重**：当库内存在环形 parent_id
脏数据（A.parent=B 且 B.parent=A）时，每弹出一个节点都会把它的父/子重新收回
pending，永不相交于空 → all_ids / pending 无限增长，请求永久挂起并占住连接池
（用户表现：删这一章整个后端卡死，只能重启）。

环形脏数据的现实来源（同文件 _build_tree 注释即列举）：
- update_section 的环检测 2026-09-18 才加，此前遗留的历史数据；
- 外部脚本直改库；
- 库复制后 id 碰撞。

对照口径：update_section 的同型后代遍历（环检测）一直有
`if r["id"] not in descendants` 去重 —— 两处一份有守卫一份没有，属判据分叉。
本文件锁死 delete_section 侧的守卫，并顺带验证正常（无环）级联删除行为不回退。
"""
import asyncio
import uuid

import pytest
import aiosqlite

import app.db as _appdb
from app.schema_sql import SCHEMA_SQL
from app.db import _migrate
from app.routers.sections import delete_section


@pytest.fixture
async def ctx(monkeypatch):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    await _migrate(conn)
    await conn.commit()

    async def fake_get_conn():
        return conn

    monkeypatch.setattr(_appdb, "get_conn", fake_get_conn)
    import app.services.ai.task_registry as _tr
    monkeypatch.setattr(_tr, "get_conn", fake_get_conn)

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await conn.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await conn.commit()
    yield conn, sid
    await conn.close()


async def _seed(conn, sid, pid, rows):
    """rows: [(id, parent_id, title, level)]"""
    for nid, parent, title, level in rows:
        await conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " level, sort_order, status, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, sid, pid, parent, title, level, 0, "empty", 1500))
    await conn.commit()


async def _remaining(conn, sid) -> set:
    cur = await conn.execute("SELECT id FROM sections WHERE scheme_id=?", (sid,))
    return {r["id"] for r in await cur.fetchall()}


class TestDeleteSectionCycleGuard:
    async def test_cyclic_parent_data_does_not_hang(self, ctx):
        """环形 A<->B：删除必须收敛返回，而不是无限循环挂起。"""
        conn, sid = ctx
        A, B = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(conn, sid, "p", [
            (A, B, "A", 2),   # A 的父是 B
            (B, A, "B", 2),   # B 的父是 A —— 成环
        ])
        # 若遍历无 visited 去重，这里会永久阻塞 → wait_for 抛 TimeoutError
        await asyncio.wait_for(delete_section(sid, A, conn), timeout=5)
        remaining = await _remaining(conn, sid)
        assert A not in remaining

    async def test_reachable_descendants_still_deleted(self, ctx):
        """正常（无环）三级链：删除父节点应级联删掉全部后代，行为不回退。"""
        conn, sid = ctx
        root, child, grand, other = (uuid.uuid4().hex for _ in range(4))
        await _seed(conn, sid, "p", [
            (root, "", "根", 1),
            (child, root, "子", 2),
            (grand, child, "孙", 3),
            (other, "", "另一根", 1),
        ])
        await asyncio.wait_for(delete_section(sid, root, conn), timeout=5)
        remaining = await _remaining(conn, sid)
        assert remaining == {other}
        for gone in (root, child, grand):
            assert gone not in remaining

    async def test_visited_nodes_enqueued_once(self, ctx):
        """菱形：删除集合内每个后代只入队一次，DELETE 参数无重复 id。"""
        conn, sid = ctx
        top, left, right, bottom = (uuid.uuid4().hex for _ in range(4))
        await _seed(conn, sid, "p", [
            (top, "", "顶", 1),
            (left, top, "左", 2),
            (right, top, "右", 2),
            (bottom, left, "底", 3),
        ])
        await asyncio.wait_for(delete_section(sid, top, conn), timeout=5)
        remaining = await _remaining(conn, sid)
        assert remaining == set()
