"""_migrate 对不存在表的幂等跳过回归（2026-09-23 · 后台日志告警噪音修复）。

背景：后台日志反复出现
    "迁移 uploaded_outlines.parse_warnings 失败: no such table: uploaded_outlines"
根因：迁移循环对「表尚不存在」的场景不做区分 —— PRAGMA table_info 返回空集后
照样执行 ALTER TABLE。缺表只应出现在新库/半初始化环境（schema DDL 已含同名列），
此时正确行为是幂等跳过；表存在但缺列的旧库迁移路径保持不变。
"""
import aiosqlite
import pytest
from app.db import _migrate
from app.schema_sql import SCHEMA_SQL


@pytest.fixture
async def memory_conn():
    """独立内存库：完全不执行 schema DDL，模拟「表尚未创建」的迁移场景。"""
    conn = await aiosqlite.connect(":memory:")
    yield conn
    await conn.close()


class TestMigrateMissingTable:
    async def test_missing_table_no_warning_no_error(self, memory_conn, caplog):
        """空库上跑迁移：不抛异常，且不得再出现 no such table 的迁移告警。"""
        import logging
        with caplog.at_level(logging.WARNING):
            await _migrate(memory_conn)  # 不应抛异常
        noisy = [r for r in caplog.records
                 if "no such table" in r.getMessage()
                 and r.getMessage().startswith("迁移")]
        assert not noisy, f"缺表迁移不应告警: {[r.getMessage() for r in noisy]}"

    async def test_existing_table_missing_column_still_migrated(self, memory_conn):
        """回归保护：表存在但缺列的旧库，ALTER 补列行为必须保持不变。"""
        await memory_conn.execute(
            "CREATE TABLE uploaded_outlines ("
            "id TEXT PRIMARY KEY, parsed_json TEXT DEFAULT '{}')")
        await memory_conn.commit()
        await _migrate(memory_conn)
        cur = await memory_conn.execute("PRAGMA table_info(uploaded_outlines)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "parse_warnings" in cols, "旧库缺列仍必须补齐 parse_warnings"


class TestStartupSequenceOnLegacyDb:
    """init_db 启动序列（executescript(SCHEMA_SQL) → _migrate）在旧库上的回归。

    2026-09-25 实测事故：dev 运行库还是 2026-09-24 之前的形状（global_facts 无
    chapter 列），而 SCHEMA_SQL 里写了
    ``CREATE INDEX idx_global_facts_chapter ON global_facts(chapter)``。
    索引 DDL 在补列迁移之前执行 → sqlite3.OperationalError: no such column:
    chapter → lifespan 失败、后端整站起不来。规则：**依赖迁移补列的索引只能放
    _migrate（补列之后），不得放 SCHEMA_SQL**。
    """

    async def test_legacy_db_startup_sequence_adds_columns_and_indexes(self, tmp_path):
        db_file = tmp_path / "legacy.db"
        # 先造一个「旧形状」库：global_facts 缺 09-24 四维列，ai_config 缺 priority
        setup = await aiosqlite.connect(db_file)
        await setup.execute(
            "CREATE TABLE global_facts ("
            "id TEXT PRIMARY KEY, project_id TEXT DEFAULT '', scheme_id TEXT DEFAULT '', "
            "group_id TEXT DEFAULT '', group_title TEXT DEFAULT '', title TEXT DEFAULT '', "
            "content TEXT DEFAULT '', category TEXT DEFAULT '', source_ref TEXT DEFAULT '', "
            "is_simulated INTEGER DEFAULT 0, confidence REAL DEFAULT 1.0, "
            "is_resolved INTEGER DEFAULT 1, has_conflict INTEGER DEFAULT 0, "
            "conflict_keys TEXT DEFAULT '', fact_key TEXT DEFAULT '', "
            "chunk_hash TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        await setup.execute(
            "CREATE TABLE ai_config ("
            "id TEXT PRIMARY KEY, provider_name TEXT NOT NULL, "
            "api_key_encrypted TEXT DEFAULT '', base_url TEXT DEFAULT '', model TEXT DEFAULT '', "
            "is_active INTEGER DEFAULT 0, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        await setup.commit()
        await setup.close()

        conn = await aiosqlite.connect(db_file)
        try:
            # 与 app.db.init_db 完全一致的启动序列（修复前此处直接抛 OperationalError）
            await conn.executescript(SCHEMA_SQL)
            await _migrate(conn)
            await conn.commit()

            cur = await conn.execute("PRAGMA table_info(global_facts)")
            gf_cols = {r[1] for r in await cur.fetchall()}
            assert {"chapter", "fact_attr", "source_kind", "is_shared"} <= gf_cols
            cur = await conn.execute("PRAGMA index_list(global_facts)")
            gf_idx = {r[1] for r in await cur.fetchall()}
            assert "idx_global_facts_chapter" in gf_idx

            cur = await conn.execute("PRAGMA table_info(ai_config)")
            ac_cols = {r[1] for r in await cur.fetchall()}
            assert "priority" in ac_cols
            cur = await conn.execute("PRAGMA index_list(ai_config)")
            ac_idx = {r[1] for r in await cur.fetchall()}
            assert "idx_ai_config_priority" in ac_idx
        finally:
            await conn.close()
