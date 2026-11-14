# R227（2026-11-13）目录生成模块第 17 轮深度探索增强 —— D6/D2 收口护栏。
#
# 分组：
#   TestLockedUpdate   D6  update_section 锁定守卫（只拦结构变更，正文/标题/解锁/force 豁免）
#   TestLockedDelete   D6  delete_section 锁定守卫（force 逃生门）
#   TestLockedReorder  D6  reorder_sections 锁定守卫（locked 一级章位置改变 → 409）
#   TestStaleCascade   D2  scheme_defects 删章级联标 stale（保留账本留痕，不物理删除）
#
# 设计说明：
#   - 全部行为实证（直接调用端点函数），不依赖 AST 静态断言；
#   - 测试数据：s1/s2 为一级章（s1 locked=1、s2 locked=0），s3/s4 为 s1 的二级子章；
#   - 豁免场景的核心断言是「不抛 409 锁定冲突」—— 后续复杂链路（renumber /
#     重规范化）在内存库上应可跑通，若抛其他异常即暴露真实缺陷而非被掩埋。
#
# 用户裁决（2026-11-13 问询）：
#   D6 → 只拦结构变更（移动/重排/删除），正文与标题编辑豁免；
#   D2 → 标记 stale（保留账本留痕，显示层标「章节已删除」）。

import pytest
from app.models import SectionUpdate
from app.routers.sections import delete_section, reorder_sections, update_section
from fastapi import HTTPException

PROJECT = "p-r227"
SCHEME = "s-r227"


async def _seed(conn):
    """插入 project / scheme / 4 章节（s1 locked=1 一级、s2 unlocked 一级、
    s3/s4 为 s1 的二级子章），并提交。"""
    await conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (PROJECT, "R227"))
    await conn.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget,outline_source)"
        " VALUES(?,?,?,?,?,?)",
        (SCHEME, PROJECT, "R227", "目录已确认", 30000, "manual"),
    )
    # (id, parent, title, level, sort_order, locked, outline_num)
    rows = [
        ("s1", "", "第一章", 1, 0, 1, "1"),
        ("s2", "", "第二章", 1, 1, 0, "2"),
        ("s3", "s1", "1.1 节", 2, 0, 0, "1.1"),
        ("s4", "s1", "1.2 节", 2, 1, 0, "1.2"),
    ]
    for sid, parent, title, level, so, locked, num in rows:
        await conn.execute(
            "INSERT INTO sections (id,scheme_id,project_id,parent_id,title,level,sort_order,locked,outline_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, SCHEME, PROJECT, parent, title, level, so, locked, f'{{"id":"{num}","title":"{title}"}}'),
        )
    await conn.commit()


async def _seed_defects(conn):
    """插入 4 条缺陷行：d1→s1、d2→s3（s1 后代）、d3→s2（不删）、d4→scheme 级('')。"""
    defects = [
        ("d1", "s1", "第一章缺陷"),
        ("d2", "s3", "子节缺陷"),
        ("d3", "s2", "第二章缺陷"),
        ("d4", "", "方案级缺陷"),
    ]
    for did, sid, title in defects:
        await conn.execute(
            "INSERT INTO scheme_defects (id,project_id,scheme_id,section_id,section_title,title,fingerprint)"
            " VALUES(?,?,?,?,?,?,?)",
            (did, PROJECT, SCHEME, sid, title, title, f"fp-{did}"),
        )
    await conn.commit()


async def _expect_409(fn):
    with pytest.raises(HTTPException) as exc:
        await fn()
    assert exc.value.status_code == 409, "必须是 409 锁定冲突"


# ---------------------------------------------------------------------------
# D6 · update_section 锁定守卫
# ---------------------------------------------------------------------------


class TestLockedUpdate:
    async def test_locked_l1_struct_change_409(self, db_conn):
        """locked=1 一级章 + 结构字段（parent_id）变更 + 非 force → 409。"""
        await _seed(db_conn)
        await _expect_409(
            lambda: update_section(SCHEME, "s1", SectionUpdate(parent_id="s2"), force=False, db=db_conn)
        )

    async def test_locked_l1_struct_change_force_ok(self, db_conn):
        """locked=1 + 结构变更 + force=True → 放行（逃生门）。"""
        await _seed(db_conn)
        result = await update_section(SCHEME, "s1", SectionUpdate(parent_id="s2"), force=True, db=db_conn)
        assert result.get("ok") is True

    async def test_locked_l1_title_edit_exempt(self, db_conn):
        """locked=1 + 仅标题变更 → 豁免（不属结构变更）。"""
        await _seed(db_conn)
        result = await update_section(SCHEME, "s1", SectionUpdate(title="第一章（改）"), force=False, db=db_conn)
        assert result.get("ok") is True

    async def test_locked_l1_content_edit_exempt(self, db_conn):
        """locked=1 + 仅正文变更 → 豁免。"""
        await _seed(db_conn)
        result = await update_section(SCHEME, "s1", SectionUpdate(content=""), force=False, db=db_conn)
        assert result.get("ok") is True

    async def test_locked_l1_explicit_unlock_ok(self, db_conn):
        """locked=1 + 结构变更 + 同请求显式解锁（locked=0）→ 放行。"""
        await _seed(db_conn)
        result = await update_section(
            SCHEME, "s1", SectionUpdate(parent_id="s2", locked=0), force=False, db=db_conn
        )
        assert result.get("ok") is True

    async def test_unlocked_l1_struct_change_ok(self, db_conn):
        """locked=0 一级章 + 结构变更 → 放行（守卫只拦锁定章）。"""
        await _seed(db_conn)
        result = await update_section(SCHEME, "s2", SectionUpdate(sort_order=0), force=False, db=db_conn)
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# D6 · delete_section 锁定守卫
# ---------------------------------------------------------------------------


class TestLockedDelete:
    async def test_delete_locked_l1_409(self, db_conn):
        """locked=1 一级章删除 → 409（删除属结构变更）。"""
        await _seed(db_conn)
        await _expect_409(lambda: delete_section(SCHEME, "s1", force=False, db=db_conn))

    async def test_delete_locked_l1_force_ok(self, db_conn):
        """locked=1 + force=True → 放行。"""
        await _seed(db_conn)
        result = await delete_section(SCHEME, "s1", force=True, db=db_conn)
        assert result.get("ok") is True

    async def test_delete_unlocked_ok(self, db_conn):
        """locked=0 一级章删除 → 放行。"""
        await _seed(db_conn)
        result = await delete_section(SCHEME, "s2", force=False, db=db_conn)
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# D6 · reorder_sections 锁定守卫
# ---------------------------------------------------------------------------


class TestLockedReorder:
    async def test_reorder_locked_l1_moved_409(self, db_conn):
        """locked 一级章被重排（新同级序号 ≠ 当前）→ 409。"""
        await _seed(db_conn)
        await _expect_409(
            lambda: reorder_sections(SCHEME, {"order": ["s2", "s1", "s3", "s4"]}, force=False, db=db_conn)
        )

    async def test_reorder_locked_l1_force_ok(self, db_conn):
        """locked 一级章被重排 + force=True → 放行。"""
        await _seed(db_conn)
        result = await reorder_sections(SCHEME, {"order": ["s2", "s1", "s3", "s4"]}, force=True, db=db_conn)
        assert result.get("ok") is True

    async def test_reorder_locked_l1_unchanged_ok(self, db_conn):
        """locked 一级章位置不变（仅其下同级子章重排）→ 放行。"""
        await _seed(db_conn)
        result = await reorder_sections(SCHEME, {"order": ["s1", "s4", "s3", "s2"]}, force=False, db=db_conn)
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# D2 · scheme_defects 删章级联标 stale
# ---------------------------------------------------------------------------


class TestStaleCascade:
    async def test_delete_l1_marks_self_and_descendants_stale(self, db_conn):
        """删除 locked 一级章（s1）→ 指向 s1 及其后代 s3 的缺陷行 stale=1；
        未删章（d3→s2）与 scheme 级（d4）不受影响。"""
        await _seed(db_conn)
        await _seed_defects(db_conn)
        await delete_section(SCHEME, "s1", force=True, db=db_conn)
        cur = await db_conn.execute("SELECT id, stale FROM scheme_defects WHERE scheme_id=?", (SCHEME,))
        rows = {r[0]: int(r[1]) for r in await cur.fetchall()}
        assert rows["d1"] == 1, "指向被删一级章的缺陷行必须标记 stale"
        assert rows["d2"] == 1, "指向被删后代章节的缺陷行必须标记 stale"
        assert rows["d3"] == 0, "未删章节的缺陷行不得标记 stale"
        assert rows["d4"] == 0, "scheme 级缺陷行（section_id=''）不得标记 stale"

    async def test_delete_unlocked_marks_its_own_only(self, db_conn):
        """删除未锁定一级章（s2）→ 仅指向 s2 的 d3 stale=1，其余不受影响。"""
        await _seed(db_conn)
        await _seed_defects(db_conn)
        await delete_section(SCHEME, "s2", force=False, db=db_conn)
        cur = await db_conn.execute("SELECT id, stale FROM scheme_defects WHERE scheme_id=?", (SCHEME,))
        rows = {r[0]: int(r[1]) for r in await cur.fetchall()}
        assert rows["d3"] == 1, "指向被删章节的缺陷行必须标记 stale"
        assert rows["d1"] == 0
        assert rows["d2"] == 0
        assert rows["d4"] == 0
