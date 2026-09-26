"""F-CONTENT-STANDARD · Task 1：生成标准数据层与 PATCH 接口测试

覆盖：
- 新列在全新库（SCHEMA_SQL）与模拟旧库（_migrate 补列）两种初始态下的默认值与幂等性；
- 方案 / 章节 PATCH 合法值落库与回读、章节 '' 清空覆盖、非法值 422（Pydantic 校验）；
- 章节树轻量列表与方案快照携带 generation_standard。
"""
import pytest
import aiosqlite
from pydantic import ValidationError

from app.db import _migrate
from app.models import SchemeUpdate, SectionUpdate
from app.routers.schemes import get_scheme, update_scheme
from app.routers.sections import update_section, list_sections


async def _insert_project_scheme(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


async def _insert_section(db, sid="s1", sec_id="sec1", title="章节1"):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title) VALUES (?,?,?)",
        (sec_id, sid, title))
    await db.commit()


# ============================================================
# 迁移与默认值
# ============================================================
class TestMigration:
    """新库 DDL + 旧库幂等补列"""

    async def test_new_schema_defaults(self, db_conn):
        """SCHEMA_SQL 建表后默认值：scheme=precise，section 两列均为 ''。"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn)

        cur = await db_conn.execute(
            "SELECT generation_standard FROM schemes WHERE id='s1'")
        assert (await cur.fetchone())[0] == "precise"

        cur = await db_conn.execute(
            "SELECT generation_standard, last_generation_standard"
            " FROM sections WHERE id='sec1'")
        assert tuple(await cur.fetchone()) == ("", "")

    async def test_migrate_idempotent_on_new_db(self, db_conn):
        """全新库再跑一遍 _migrate 不报错（列已存在时幂等跳过）。"""
        await _migrate(db_conn)
        await _migrate(db_conn)
        await db_conn.commit()

    async def test_legacy_db_backfill(self):
        """模拟 2026-09-26 前的旧库（无三列）：_migrate 补列并对历史行填默认值。"""
        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.executescript(
                "CREATE TABLE schemes (id TEXT PRIMARY KEY, project_id TEXT, name TEXT);"
                "CREATE TABLE sections (id TEXT PRIMARY KEY, scheme_id TEXT, title TEXT);"
                "INSERT INTO schemes VALUES ('s1','p1','旧方案');"
                "INSERT INTO sections VALUES ('sec1','s1','旧章节');")
            await _migrate(conn)
            await conn.commit()

            cur = await conn.execute(
                "SELECT generation_standard FROM schemes WHERE id='s1'")
            # 历史方案回填 precise（与旧行为等价：既有提示词本就强约束事实引用）
            assert (await cur.fetchone())[0] == "precise"
            cur = await conn.execute(
                "SELECT generation_standard, last_generation_standard"
                " FROM sections WHERE id='sec1'")
            assert tuple(await cur.fetchone()) == ("", "")

            # 再跑一遍幂等无异常
            await _migrate(conn)
        finally:
            await conn.close()


# ============================================================
# 方案级 PATCH
# ============================================================
class TestSchemeStandardPatch:

    async def test_patch_fuzzy_and_readback(self, db_conn):
        await _insert_project_scheme(db_conn)
        await update_scheme(
            "p1", "s1", SchemeUpdate(generation_standard="fuzzy"), db=db_conn)
        row = await get_scheme("p1", "s1", db=db_conn)
        assert row["generation_standard"] == "fuzzy"

    async def test_patch_precise(self, db_conn):
        await _insert_project_scheme(db_conn)
        await update_scheme(
            "p1", "s1", SchemeUpdate(generation_standard="precise"), db=db_conn)
        row = await get_scheme("p1", "s1", db=db_conn)
        assert row["generation_standard"] == "precise"

    async def test_patch_invalid_rejected(self):
        """非法值在模型层即拒绝（FastAPI 映射为 422）。"""
        with pytest.raises(ValidationError):
            SchemeUpdate(generation_standard="strict")
        with pytest.raises(ValidationError):
            SchemeUpdate(generation_standard="")


# ============================================================
# 章节级 PATCH
# ============================================================
class TestSectionStandardPatch:

    async def test_patch_section_override_and_clear(self, db_conn):
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn)

        await update_section(
            "s1", "sec1", SectionUpdate(generation_standard="fuzzy"), db=db_conn)
        cur = await db_conn.execute(
            "SELECT generation_standard FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())[0] == "fuzzy"

        # '' = 显式清除覆盖，回落方案级
        await update_section(
            "s1", "sec1", SectionUpdate(generation_standard=""), db=db_conn)
        cur = await db_conn.execute(
            "SELECT generation_standard FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())[0] == ""

    async def test_patch_section_invalid_rejected(self):
        with pytest.raises(ValidationError):
            SectionUpdate(generation_standard="balanced")

    async def test_section_patch_cannot_touch_last_used(self, db_conn):
        """last_generation_standard 不在 SectionUpdate 白名单内，PATCH 无法篡改。"""
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn)
        # 模型层即不接受该字段（Pydantic 默认额外字段忽略，不会落库）
        payload = SectionUpdate.model_validate(
            {"generation_standard": "fuzzy", "last_generation_standard": "precise"})
        assert not hasattr(payload, "last_generation_standard")
        await update_section("s1", "sec1", payload, db=db_conn)
        cur = await db_conn.execute(
            "SELECT last_generation_standard FROM sections WHERE id='sec1'")
        assert (await cur.fetchone())[0] == ""


# ============================================================
# 章节树 / 方案快照下发
# ============================================================
class TestTreeProjection:

    async def test_tree_and_scheme_carry_standard(self, db_conn):
        await _insert_project_scheme(db_conn)
        await _insert_section(db_conn)
        await update_scheme(
            "p1", "s1", SchemeUpdate(generation_standard="fuzzy"), db=db_conn)
        await update_section(
            "s1", "sec1", SectionUpdate(generation_standard="precise"), db=db_conn)

        # 轻量列表（生成中轮询口径，不含 content）
        light = await list_sections("s1", include_content=False, db=db_conn)
        assert light["scheme"]["generation_standard"] == "fuzzy"
        assert light["tree"][0]["generation_standard"] == "precise"
        assert "content" not in light["tree"][0]

        # 全量列表同样携带
        full = await list_sections("s1", include_content=True, db=db_conn)
        assert full["tree"][0]["generation_standard"] == "precise"
