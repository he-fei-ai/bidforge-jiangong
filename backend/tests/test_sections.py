"""sections.py 单元测试

覆盖 PB-2 性能优化：
- reorder_sections：executemany 批量更新 sort_order（替代 N 次单条 UPDATE）
- delete_section：递归 CTE 一次性查询所有后代 ID（替代逐层查询删除）

测试策略：
- 直接调用路由处理函数，传入测试 db 连接，绕过 FastAPI 依赖注入
- 构造多层数据验证批量操作和级联删除的正确性
- 验证 DB 实际状态而非仅返回值，确保 SQL 真正执行
"""
import pytest

from app.routers.sections import reorder_sections, delete_section


# ============================================================
# 辅助函数
# ============================================================
async def _insert_project_scheme(db, pid="p1", sid="s1"):
    """插入 project + scheme 基础数据（INSERT OR IGNORE 允许重复 project）"""
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


async def _insert_section(db, sid, sec_id, title, parent_id="", sort_order=0, level=1):
    """插入单个 section"""
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, parent_id, sort_order, level)"
        " VALUES (?,?,?,?,?,?)",
        (sec_id, sid, title, parent_id, sort_order, level))


async def _get_sort_orders(db, sid):
    """按 sort_order 升序返回 (id, sort_order) 列表"""
    cur = await db.execute(
        "SELECT id, sort_order FROM sections WHERE scheme_id=? ORDER BY sort_order",
        (sid,))
    return [dict(r) for r in await cur.fetchall()]


async def _count_sections(db, sid):
    """统计 scheme 下 section 数量"""
    cur = await db.execute("SELECT COUNT(*) FROM sections WHERE scheme_id=?", (sid,))
    return (await cur.fetchone())[0]


# ============================================================
# reorder_sections 测试
# ============================================================
class TestReorderSections:
    """reorder_sections 批量更新测试"""

    async def test_reorder_basic(self, db_conn):
        """基本重排：3 个章节反转顺序"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "章节1", sort_order=0)
        await _insert_section(db_conn, "s1", "sec2", "章节2", sort_order=1)
        await _insert_section(db_conn, "s1", "sec3", "章节3", sort_order=2)
        await db_conn.commit()

        result = await reorder_sections("s1", {"order": ["sec3", "sec1", "sec2"]},
                                        db=db_conn)
        assert result["ok"] is True

        rows = await _get_sort_orders(db_conn, "s1")
        assert rows == [
            {"id": "sec3", "sort_order": 0},
            {"id": "sec1", "sort_order": 1},
            {"id": "sec2", "sort_order": 2},
        ]

    async def test_reorder_empty_order(self, db_conn):
        """空 order 列表：不执行任何更新，返回 ok"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "章节1", sort_order=5)
        await db_conn.commit()

        result = await reorder_sections("s1", {"order": []}, db=db_conn)
        assert result["ok"] is True
        # sort_order 不变
        rows = await _get_sort_orders(db_conn, "s1")
        assert rows == [{"id": "sec1", "sort_order": 5}]

    async def test_reorder_missing_order_key(self, db_conn):
        """body 中缺少 order 键：默认空列表，不报错"""
        await _insert_project_scheme(db_conn)
        await db_conn.commit()

        result = await reorder_sections("s1", {}, db=db_conn)
        assert result["ok"] is True

    async def test_reorder_uses_executemany_batch(self, db_conn):
        """批量更新验证：50 个章节一次重排，全部生效

        PB-2 核心验证点：executemany 一次性提交 50 条更新，
        而非 50 次单独 execute+commit。
        """
        await _insert_project_scheme(db_conn)
        ids = [f"sec{i}" for i in range(50)]
        for i, sid in enumerate(ids):
            await _insert_section(db_conn, "s1", sid, f"章节{i}", sort_order=i)
        await db_conn.commit()

        # 反转顺序
        reversed_ids = list(reversed(ids))
        result = await reorder_sections("s1", {"order": reversed_ids}, db=db_conn)
        assert result["ok"] is True

        rows = await _get_sort_orders(db_conn, "s1")
        assert len(rows) == 50
        # 验证每个 sort_order 正确
        for i, expected_id in enumerate(reversed_ids):
            assert rows[i]["id"] == expected_id
            assert rows[i]["sort_order"] == i

    async def test_reorder_partial_ids(self, db_conn):
        """order 中包含不存在的 id：该条 UPDATE 影响 0 行，其余正常

        SQL 含 AND scheme_id=? 条件，跨 scheme 的 id 不会被误更新。
        """
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "章节1", sort_order=0)
        await _insert_section(db_conn, "s1", "sec2", "章节2", sort_order=1)
        await db_conn.commit()

        # "ghost" 不存在
        result = await reorder_sections("s1", {"order": ["sec2", "ghost", "sec1"]},
                                        db=db_conn)
        assert result["ok"] is True
        rows = await _get_sort_orders(db_conn, "s1")
        assert rows[0]["id"] == "sec2"
        assert rows[0]["sort_order"] == 0
        assert rows[1]["id"] == "sec1"
        assert rows[1]["sort_order"] == 2  # ghost 占了 index=1

    async def test_reorder_cross_scheme_isolation(self, db_conn):
        """跨 scheme 隔离：order 中传入其他 scheme 的 id 不影响其数据"""
        await _insert_project_scheme(db_conn, "p1", "s1")
        await _insert_project_scheme(db_conn, "p1", "s2")
        await _insert_section(db_conn, "s1", "a1", "A1", sort_order=0)
        await _insert_section(db_conn, "s2", "b1", "B1", sort_order=0)
        await db_conn.commit()

        # s1 的 reorder 中混入 s2 的 id
        result = await reorder_sections("s1", {"order": ["b1", "a1"]}, db=db_conn)
        assert result["ok"] is True

        # b1 属于 s2，不应被 s1 的 reorder 改动
        cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id=?", ("b1",))
        b1_sort = (await cur.fetchone())[0]
        assert b1_sort == 0  # 未变

        # a1 应被更新为 sort_order=1
        cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id=?", ("a1",))
        a1_sort = (await cur.fetchone())[0]
        assert a1_sort == 1

    async def test_reorder_returns_tree(self, db_conn):
        """返回值包含 tree 字段，反映重排后的结构"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "章节1", sort_order=0)
        await _insert_section(db_conn, "s1", "sec2", "章节2", sort_order=1)
        await db_conn.commit()

        result = await reorder_sections("s1", {"order": ["sec2", "sec1"]}, db=db_conn)
        assert "tree" in result
        assert len(result["tree"]) == 2


# ============================================================
# delete_section 测试（递归 CTE 级联删除）
# ============================================================
class TestDeleteSection:
    """delete_section 递归 CTE 测试

    PB-2 核心验证点：用 WITH RECURSIVE descendants 一次性查出
    所有后代 ID，再单条 DELETE ... WHERE id IN (...) 批量删除，
    替代逐层 SELECT children + DELETE 的 N+1 模式。
    """

    async def test_delete_leaf(self, db_conn):
        """删除叶子节点：仅删自身"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "章节1")
        await _insert_section(db_conn, "s1", "sec2", "章节2")
        await db_conn.commit()

        result = await delete_section("s1", "sec1", db=db_conn)
        assert result["ok"] is True
        assert await _count_sections(db_conn, "s1") == 1

    async def test_delete_parent_with_children(self, db_conn):
        """删除父节点：级联删除直接子节点"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "parent", "父", parent_id="")
        await _insert_section(db_conn, "s1", "child1", "子1", parent_id="parent")
        await _insert_section(db_conn, "s1", "child2", "子2", parent_id="parent")
        await db_conn.commit()

        result = await delete_section("s1", "parent", db=db_conn)
        assert result["ok"] is True
        assert await _count_sections(db_conn, "s1") == 0

    async def test_delete_deep_recursive_cte(self, db_conn):
        """深层递归：4 层树，删根节点全部清除

        树结构：
            root
            ├── c1
            │   ├── gc1
            │   │   └── ggc1
            │   └── gc2
            └── c2
                └── gc3
        """
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "root", "根", parent_id="")
        await _insert_section(db_conn, "s1", "c1", "子1", parent_id="root")
        await _insert_section(db_conn, "s1", "c2", "子2", parent_id="root")
        await _insert_section(db_conn, "s1", "gc1", "孙1", parent_id="c1")
        await _insert_section(db_conn, "s1", "gc2", "孙2", parent_id="c1")
        await _insert_section(db_conn, "s1", "gc3", "孙3", parent_id="c2")
        await _insert_section(db_conn, "s1", "ggc1", "曾孙1", parent_id="gc1")
        await db_conn.commit()

        result = await delete_section("s1", "root", db=db_conn)
        assert result["ok"] is True
        assert await _count_sections(db_conn, "s1") == 0

    async def test_delete_middle_level(self, db_conn):
        """删除中间层节点：仅删该子树，兄弟保留

        树结构：
            root
            ├── c1 (删除目标)
            │   └── gc1
            └── c2 (保留)
                └── gc2
        """
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "root", "根", parent_id="")
        await _insert_section(db_conn, "s1", "c1", "子1", parent_id="root")
        await _insert_section(db_conn, "s1", "c2", "子2", parent_id="root")
        await _insert_section(db_conn, "s1", "gc1", "孙1", parent_id="c1")
        await _insert_section(db_conn, "s1", "gc2", "孙2", parent_id="c2")
        await db_conn.commit()

        result = await delete_section("s1", "c1", db=db_conn)
        assert result["ok"] is True
        # root, c2, gc2 保留
        assert await _count_sections(db_conn, "s1") == 3

        cur = await db_conn.execute("SELECT id FROM sections WHERE scheme_id=? ORDER BY id",
                                    ("s1",))
        remaining = {r[0] for r in await cur.fetchall()}
        assert remaining == {"root", "c2", "gc2"}

    async def test_delete_nonexistent_id(self, db_conn):
        """删除不存在的 id：404（安全校验：章节必须归属该方案）"""
        from fastapi import HTTPException

        await _insert_project_scheme(db_conn)
        await db_conn.commit()

        with pytest.raises(HTTPException) as exc_info:
            await delete_section("s1", "nonexistent", db=db_conn)
        assert exc_info.value.status_code == 404

    async def test_delete_does_not_affect_other_scheme(self, db_conn):
        """跨 scheme 隔离：删除 s1 的节点不影响 s2

        递归 CTE 从 section_id 开始沿 parent_id 链向下查，
        不会跨 scheme（因为子节点的 scheme_id 与父一致）。
        """
        await _insert_project_scheme(db_conn, "p1", "s1")
        await _insert_project_scheme(db_conn, "p1", "s2")
        await _insert_section(db_conn, "s1", "a1", "A1", parent_id="")
        await _insert_section(db_conn, "s1", "a2", "A2", parent_id="a1")
        await _insert_section(db_conn, "s2", "b1", "B1", parent_id="")
        await _insert_section(db_conn, "s2", "b2", "B2", parent_id="b1")
        await db_conn.commit()

        result = await delete_section("s1", "a1", db=db_conn)
        assert result["ok"] is True
        # s1 清空，s2 不变
        assert await _count_sections(db_conn, "s1") == 0
        assert await _count_sections(db_conn, "s2") == 2

    async def test_delete_wide_tree(self, db_conn):
        """宽树：1 个父 + 20 个子，一次删除全部"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn, "s1", "parent", "父", parent_id="")
        for i in range(20):
            await _insert_section(db_conn, "s1", f"c{i}", f"子{i}",
                                  parent_id="parent")
        await db_conn.commit()

        result = await delete_section("s1", "parent", db=db_conn)
        assert result["ok"] is True
        assert await _count_sections(db_conn, "s1") == 0