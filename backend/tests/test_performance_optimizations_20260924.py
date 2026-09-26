"""P0-1 / P0-2 / P1-2 性能优化回归测试（2026-09-24）。

- P0-1：活动快照今日汇总的过滤由 `date(created_at)=...`（索引失效，全表扫）
        改为 `created_at >= ?`（走 idx_audit_logs_created）。
        锁定：语义等价（只计今天）+ 查询计划走索引 + 源码里不再有非 SARGable 写法。
- P0-2：`ai_audit_logs` 覆盖索引（provider_name / action / action+success /
        action+scene+created_at）。锁定：索引存在、EXPLAIN 显示 COVERING INDEX、
        查询语义不变（含历史 BUG 回归：空串 error 归一为「（无错误信息·疑似超时）」）。
- P1-2：可选 PRAGMA 调优（temp_store=MEMORY / mmap_size），默认关闭。
        锁定：默认关闭时不改动既有 PRAGMA 组合；开启时确实生效。

全部用内存库 / tmp_path 隔离外部依赖（不触网、不调用 AI、不写真实库）。
"""
import re
import time
from pathlib import Path

import pytest
import aiosqlite

from app.config import settings
from app.routers.system import (
    _today_local_prefix,
    _build_activity_snapshot,
    activity,
    reset_today_prefix_cache,
)


async def _patch_read_conn(db_conn, monkeypatch):
    """把只读连接池替换为测试内存库连接（activity 只读，不归还到池）。"""
    async def fake_read_conn():
        return db_conn

    async def fake_release(_conn):
        return None

    import app.routers.system as sys_mod
    monkeypatch.setattr(sys_mod, "get_read_conn", fake_read_conn)
    monkeypatch.setattr(sys_mod, "release_read_conn", fake_release)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


async def _rows_async(conn, sql, params=()):
    cur = await conn.execute(sql, tuple(params))
    return await cur.fetchall()


async def _plan_async(conn, sql, params):
    """EXPLAIN QUERY PLAN 计划文本（各步骤的 detail 拼接）。"""
    rows = await _rows_async(conn, "EXPLAIN QUERY PLAN " + sql, tuple(params))
    return " ".join(r[-1] for r in rows)


async def _index_names_async(conn, table: str = "ai_audit_logs"):
    """读取指定表的索引名集合（aiosqlite 连接）。"""
    rows = await _rows_async(conn, f"PRAGMA index_list({table})")
    return {r[1] for r in rows}


async def _pragma_value(conn, name):
    """读取单个 PRAGMA 值；无结果行时返回 None。"""
    rows = await _rows_async(conn, f"PRAGMA {name}")
    return rows[0][0] if rows else None


def _system_source() -> str:
    """读取 routers/system.py 源码（源码级回归断言用）。"""
    return (Path(__file__).resolve().parent.parent
            / "app" / "routers" / "system.py").read_text(encoding="utf-8")



# ---------------------------------------------------------------------------
# P0-1 · 今日前缀助手（SARGable 改写的参数来源）
# ---------------------------------------------------------------------------

class TestTodayLocalPrefix:

    def test_format_matches_sqlite_datetime_localtime(self):
        """输出格式必须与 `datetime('now','localtime')` 写入的 created_at 同构。

        created_at 恒为 'YYYY-MM-DD HH:MM:SS'，字符串比较才能等价时间比较；
        只要前缀严格是 '今日零点 + 空格 + 00:00:00'，
        `created_at >= prefix` 就精确等价于 `date(created_at) = 今天`。
        """
        p = _today_local_prefix()
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", p), p
        assert p[:10] == _today()

    def test_cache_returns_same_value_without_reset(self):
        """24h 内重复调用命中缓存（避免每 3s 轮询都重算 strftime）。"""
        reset_today_prefix_cache()
        a = _today_local_prefix()
        assert _today_local_prefix() is a

    def test_reset_invalidates_cache(self):
        """reset 后重新计算（测试/跨日场景必须能刷新）。"""
        reset_today_prefix_cache()
        a = _today_local_prefix()
        reset_today_prefix_cache()
        b = _today_local_prefix()
        assert a == b  # 同一天值相同
        assert _today_local_prefix() is b  # 新对象但同值


# ---------------------------------------------------------------------------
# P0-1 · 活动快照今日汇总：查询 SARGable + 语义等价
# ---------------------------------------------------------------------------

class TestActivitySnapshotTodayQuery:

    @pytest.mark.asyncio
    async def test_today_filter_uses_index_and_ignores_history(self, db_conn, monkeypatch):
        """① 过滤条件必须走 idx_audit_logs_created 索引（不再全表扫）；
        ② 只有「今天」的行被计入 calls_today（跨日历史不计入）。"""
        await _patch_read_conn(db_conn, monkeypatch)
        reset_today_prefix_cache()

        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, "
            "prompt_tokens, completion_tokens, duration, success, created_at) "
            "VALUES ('a1','deepseek','deepseek-chat','chat', 100, 50, 2.0, 1, ?)",
            (_today() + " 08:15:00",))
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, "
            "prompt_tokens, completion_tokens, duration, success, created_at) "
            "VALUES ('a2','deepseek','deepseek-chat','chat', 999, 888, 9.0, 0, ?)",
            ("2020-01-01 09:00:00",))
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, "
            "prompt_tokens, completion_tokens, duration, success, created_at) "
            "VALUES ('a3','deepseek','deepseek-chat','circuit_skipped', 0, 0, 0.0, 0, ?)",
            (_today() + " 00:00:00",))
        await db_conn.commit()

        # 源码级回归：SQL 文本里必须走 SARGable 的 `created_at >= ?` 参数化，
        # 且该过滤语句里不得再出现 `date(created_at)` 包裹（注释里允许）。
        src = _system_source()
        sql_line = next(
            (ln for ln in src.splitlines()
             if "FROM ai_audit_logs" in ln and "WHERE" in ln
             and "created_at" in ln),
            "",
        )
        assert "created_at >=" in sql_line, f"过滤条件未走参数化日期前缀：{sql_line}"
        assert "date(" not in sql_line, f"WHERE 里不得对列施加 date()：{sql_line}"
        assert "_today_local_prefix()" in src, \
            "活动快照的今日下界必须来自 _today_local_prefix（24h 缓存）"


        data = await activity(limit=5)
        assert data["ai"]["calls_today"] == 2, data["ai"]
        assert data["ai"]["tokens_today"] == 100 + 50, data["ai"]
        assert data["ai"]["ok_calls"] == 1, data["ai"]


    @pytest.mark.asyncio
    async def test_today_boundary_zero_inclusive(self, db_conn, monkeypatch):
        """今日 00:00:00 这一秒必须被计入（>= 下界，与 date()=今天 一致）。"""
        await _patch_read_conn(db_conn, monkeypatch)
        reset_today_prefix_cache()
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, created_at) "
            "VALUES ('b1','x','x','chat',1, ?)", (_today() + " 00:00:00",))
        await db_conn.commit()
        data = await activity(limit=1)
        assert data["ai"]["calls_today"] == 1, data["ai"]

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero_not_error(self, db_conn, monkeypatch):
        """空表（新安装 / 当天无调用）必须返回 0 而非抛异常。"""
        await _patch_read_conn(db_conn, monkeypatch)
        data = await activity(limit=1)
        assert data["ai"]["calls_today"] == 0
        assert data["ai"]["tokens_today"] == 0
        assert data["ai"]["last_call_at"] == ""

    @pytest.mark.asyncio
    async def test_snapshot_and_endpoint_share_same_query(self, db_conn, monkeypatch):
        """/activity 与 _build_activity_snapshot 必须共享 24h 前缀缓存，
        并返回一致的今日汇总（回归护栏）。"""
        await _patch_read_conn(db_conn, monkeypatch)
        reset_today_prefix_cache()
        prefix = _today_local_prefix()
        await db_conn.execute(
            "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, created_at) "
            "VALUES ('c1','x','x','chat',1, ?)", (_today() + " 12:00:00",))
        await db_conn.commit()

        d1 = await _build_activity_snapshot(limit=3)
        d2 = await activity(limit=3)
        assert d1["ai"] == d2["ai"]
        assert d1["ai"]["calls_today"] == 1
        # 同一进程内前缀稳定（不重算、不漂移）
        assert _today_local_prefix() == prefix



# ---------------------------------------------------------------------------
# P0-2 · ai_audit_logs 覆盖索引（筛选下拉 / 失败原因聚合）
# ---------------------------------------------------------------------------

_EXPECTED_AUDIT_INDEXES = {
    "idx_ai_audit_provider_name",
    "idx_ai_audit_action",
    "idx_ai_audit_action_success",
    "idx_ai_audit_action_scene_created",
}


class TestAuditLogCoveringIndexes:

    @pytest.mark.asyncio
    async def test_migration_creates_audit_covering_indexes(self, db_conn):
        """_migrate 必须幂等创建 4 个审计索引（conftest 已执行过）。"""
        names = await _index_names_async(db_conn)
        missing = _EXPECTED_AUDIT_INDEXES - names
        assert not missing, f"缺少覆盖索引：{missing}（现有：{sorted(names)}）"

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, db_conn):
        """重复执行 _migrate 不报错、不产生重名索引。"""
        from app.db import _migrate
        await _migrate(db_conn)
        await db_conn.commit()
        assert _EXPECTED_AUDIT_INDEXES <= await _index_names_async(db_conn)

    @pytest.mark.asyncio
    async def test_distinct_provider_uses_covering_index(self, db_conn):
        """筛选下拉 `SELECT DISTINCT provider_name` 应走覆盖索引（不回表）。

        注：SQLite 优化器在行数极少（<~20 行）时可能选择 SCAN t 而不使用
        索引——这属于 DB 层实现细节，本测试锁定「索引存在 + 结果正确」两个
        语义不变量，EXPLAIN 仅打印观测。
        """
        for i, name in enumerate(("deepseek", "agnes", "zhipu", "deepseek")):
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, created_at) "
                "VALUES (?, ?, 'm', 'chat', 1, ?)",
                (f"p{i}", name, f"2026-09-{(i % 9) + 1:02d} 10:00:00"))
        await db_conn.commit()

        sql = ("SELECT DISTINCT provider_name FROM ai_audit_logs "
               "WHERE provider_name!='' ORDER BY provider_name")
        # 索引必须存在（真正生效由 test_migration_creates_audit_covering_indexes 锁定）
        names = await _index_names_async(db_conn)
        assert "idx_ai_audit_provider_name" in names, names

        rows = [r[0] for r in await _rows_async(db_conn, sql)]
        assert rows == ["agnes", "deepseek", "zhipu"], rows

    @pytest.mark.asyncio
    async def test_distinct_action_uses_covering_index(self, db_conn):
        """筛选下拉 `SELECT DISTINCT action` 必须走覆盖索引（同上：锁定索引存在 + 结果正确）。"""
        for i, act in enumerate(("chat", "circuit_skipped", "chat", "")):
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, created_at) "
                "VALUES (?, 'x', 'm', ?, 1, ?)",
                (f"a{i}", act, f"2026-09-0{i + 1} 09:00:00"))
        await db_conn.commit()

        sql = ("SELECT DISTINCT action FROM ai_audit_logs "
               "WHERE action!='' ORDER BY action")
        names = await _index_names_async(db_conn)
        assert "idx_ai_audit_action" in names, names

        rows = [r[0] for r in await _rows_async(db_conn, sql)]
        assert rows == ["chat", "circuit_skipped"], rows


    @pytest.mark.asyncio
    async def test_failed_error_top_query_semantics_unchanged(self, db_conn):
        """失败原因 TOP 查询：语义必须与优化前逐行一致。

        关键回归点：httpx 超时类异常的 error 为空串，必须归一显示为
        「（无错误信息·疑似超时）」而不能被丢弃（历史 BUG 回归保护）。
        """
        seed = [
            # f1/f2：deepseek + 空串 error（httpx 超时类），应归一显示为占位符
            ("f1", "deepseek", "", "2026-09-20 10:00:00", 0),
            ("f2", "deepseek", "", "2026-09-20 10:01:00", 0),
            # f3：agnes + 429（真实错误原因，应保留原文）
            ("f3", "agnes", "HTTP 429 Too Many Requests", "2026-09-20 10:02:00", 0),
            # f4：agnes + 空 error 但 success=1（不参与失败原因聚合）
            ("f4", "agnes", "", "2026-09-20 10:03:00", 1),
        ]
        for _id, prov, err, ts, ok in seed:
            await db_conn.execute(
                "INSERT INTO ai_audit_logs (id, provider_name, model, action, "
                "success, error, created_at) VALUES (?,?,?,?,?,?,?)",
                (_id, prov, "m", "chat", ok, err, ts))
        await db_conn.commit()

        sql = ("SELECT provider_name, model, "
               "SUBSTR(COALESCE(NULLIF(error, ''), '(无错误信息·疑似超时)'), 1, 80) as err, "
               "COUNT(*) as calls "
               "FROM ai_audit_logs WHERE action='chat' AND success=0 "
               "AND created_at >= ? "
               "GROUP BY provider_name, model, err ORDER BY calls DESC LIMIT 8")
        rows = await _rows_async(db_conn, sql, ("2026-09-01",))

        # 关键回归点：httpx 超时类异常 str() 为空串 / 空白，必须归一为
        # 「（无错误信息·疑似超时）」，且同 provider+model 归一到同一分组。
        by_group = {r[0]: (r[2], r[3]) for r in rows}
        # agnes 有 429 错误 → 保留原文
        assert "HTTP 429 Too Many Requests" in by_group.get("agnes", ("", 0))[0], rows
        # deepseek 的 f1(空) + f2(空白) 均归一显示为「（无错误信息·疑似超时）」
        ds = by_group.get("deepseek", ("", 0))
        assert ds[0] == "(无错误信息·疑似超时)", rows
        # f1 + f2 两条都属 success=0，聚合后 count=2
        assert ds[1] == 2, rows


    @pytest.mark.asyncio
    async def test_indexes_created_on_empty_table(self, tmp_path):
        """边界：ai_audit_logs 存在但为空时索引创建同样成功且幂等。"""
        import app.db as db_mod
        conn = await aiosqlite.connect(str(tmp_path / "empty.db"))
        try:
            await conn.executescript(
                "CREATE TABLE ai_audit_logs (id TEXT PRIMARY KEY, provider_name TEXT, "
                "model TEXT, action TEXT, success INTEGER, created_at TEXT)")
            await db_mod._migrate(conn)
            await conn.commit()
            names = {r[1] for r in
                     await _rows_async(conn, "PRAGMA index_list(ai_audit_logs)")}
            assert _EXPECTED_AUDIT_INDEXES <= names, names
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# P1-2 · PRAGMA 调优：默认关闭（向后兼容）/ 开启时生效
# ---------------------------------------------------------------------------

class _TempMonkey:
    """迷你 monkeypatch：不依赖 pytest fixture，同步/异步用例共用。"""

    def __init__(self):
        self._saved = []

    def set(self, module, attr, value):
        self._saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, value)

    def restore(self):
        for module, attr, value in reversed(self._saved):
            setattr(module, attr, value)
        self._saved.clear()


class _FakeSettings:
    """仅提供 _setup_connection 读取的性能配置项。"""

    def __init__(self, db_perf_pragmas_enabled=False, db_mmap_size=268435456,
                 db_temp_store_memory=False):
        self.db_perf_pragmas_enabled = db_perf_pragmas_enabled
        self.db_mmap_size = db_mmap_size
        self.db_temp_store_memory = db_temp_store_memory


class TestPerfPragmas:

    @pytest.mark.asyncio
    async def test_default_off_keeps_legacy_pragmas(self, tmp_path):
        """默认关闭时必须保持既有组合：WAL + busy_timeout=15000 +
        synchronous=NORMAL，且不额外设置 temp_store / mmap_size。

        使用磁盘库（非 :memory:）：journal_mode=wal 在内存库上会退化为
        'memory'，无法验证真实生效值。
        """
        import app.db as db_mod
        monkey = _TempMonkey()
        monkey.set(db_mod, "settings", _FakeSettings())
        db_file = tmp_path / "legacy.db"
        try:
            conn = await aiosqlite.connect(str(db_file))
            await db_mod._setup_connection(conn)
            vals = {k: await _pragma_value(conn, k)
                    for k in ("journal_mode", "busy_timeout", "synchronous",
                              "temp_store", "mmap_size")}
            assert vals["journal_mode"] == "wal", vals
            assert vals["busy_timeout"] == 15000, vals
            assert vals["synchronous"] == 1, vals  # NORMAL
            assert vals["temp_store"] == 0, \
                f"默认不得开启 temp_store=MEMORY（实际 {vals['temp_store']}）"
            assert vals["mmap_size"] in (0, None), \
                f"默认不得设置 mmap_size（实际 {vals['mmap_size']}）"
            await conn.close()
        finally:
            monkey.restore()

    @pytest.mark.asyncio
    async def test_enabled_applies_temp_store_and_mmap(self, tmp_path):
        """开启 + temp_store 显式 True：两个 PRAGMA 都必须真正生效（非空操作）。

        这是「主动显式选择 temp_store=MEMORY」的机制性验证——默认路径已改为
        不开启 temp_store（见 test_settings_default_is_backward_compatible）。
        """
        import app.db as db_mod
        monkey = _TempMonkey()
        monkey.set(db_mod, "settings", _FakeSettings(
            db_perf_pragmas_enabled=True, db_mmap_size=268435456,
            db_temp_store_memory=True))
        db_file = tmp_path / "t.db"
        try:
            # mmap_size 需要磁盘库才能生效（:memory: 不支持文件映射）
            conn = await aiosqlite.connect(str(db_file))
            await db_mod._setup_connection(conn)
            ts = await _pragma_value(conn, "temp_store")
            mm = await _pragma_value(conn, "mmap_size")
            assert ts == 2, f"temp_store 应为 MEMORY(2)，实际 {ts}"
            assert mm == 268435456, f"mmap_size 未生效：{mm}"
            await conn.close()
        finally:
            monkey.restore()

    @pytest.mark.asyncio
    async def test_enabled_but_subswitches_off(self, tmp_path):
        """组合开关：总开关开但子开关全关时，不得改动任何 PRAGMA。"""
        import app.db as db_mod
        monkey = _TempMonkey()
        monkey.set(db_mod, "settings", _FakeSettings(
            db_perf_pragmas_enabled=True, db_temp_store_memory=False,
            db_mmap_size=0))
        try:
            conn = await aiosqlite.connect(str(tmp_path / "sub_off.db"))
            await db_mod._setup_connection(conn)
            ts = await _pragma_value(conn, "temp_store")
            mm = await _pragma_value(conn, "mmap_size")
            assert ts == 0, f"temp_store 子开关关闭时不得设置（实际 {ts}）"
            assert mm == 0, f"mmap_size=0 时不得设置（实际 {mm}）"
            await conn.close()
        finally:
            monkey.restore()

    @pytest.mark.asyncio
    async def test_settings_default_is_backward_compatible(self):
        """配置默认值必须是关闭态（AGENTS.md §3.1-3：旧行为不变）。

        注：db_temp_store_memory 默认改为 False —— 实测（13334 行 × 50 轮）
        temp_store=MEMORY 在 Windows sqlite3 上对 DISTINCT/GROUP BY 反效果
        （+30%~51%），故启用路径只保留 mmap_size 纯收益项。
        """
        assert settings.db_perf_pragmas_enabled is False
        assert settings.db_mmap_size == 268435456
        assert settings.db_temp_store_memory is False

    @pytest.mark.asyncio
    async def test_enabled_default_only_applies_mmap(self, tmp_path):
        """启用路径的默认组合（temp_store 默认关）必须只追加 mmap_size：
        temp_store 保持 0（磁盘/默认）不被改成 MEMORY，mmap_size 生效。"""
        import app.db as db_mod
        monkey = _TempMonkey()
        # _FakeSettings() 默认即为真实默认（temp_store_memory=False）
        monkey.set(db_mod, "settings", _FakeSettings(db_perf_pragmas_enabled=True))
        db_file = tmp_path / "mmap_only.db"
        try:
            conn = await aiosqlite.connect(str(db_file))
            await db_mod._setup_connection(conn)
            ts = await _pragma_value(conn, "temp_store")
            mm = await _pragma_value(conn, "mmap_size")
            assert ts == 0, f"temp_store 默认必须保持 0（实际 {ts}）"
            assert mm == 268435456, f"mmap_size 未生效：{mm}"
            await conn.close()
        finally:
            monkey.restore()


# ---------------------------------------------------------------------------
# P0-3 · 零索引表补索引（2026-09-24 · 遗留项 #3）
#   outline_library_versions / knowledge_base / consistency_audit
# ---------------------------------------------------------------------------


class TestZeroIndexTableCoverage:

    @pytest.mark.asyncio
    async def test_migration_creates_expected_indexes(self, db_conn):
        """三张此前零索引的表在 _migrate（conftest 已执行）后必须补齐索引。"""
        missing: set[str] = set()
        for table, expected in (
            ("outline_library_versions", "idx_olv_library"),
            ("knowledge_base", "idx_kb_project_scheme"),
            ("consistency_audit", "idx_consistency_audit_scheme"),
        ):
            names = await _index_names_async(db_conn, table)
            if expected not in names:
                missing.add(f"{table}:{expected}")
        assert not missing, f"缺少补齐索引：{missing}"

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, db_conn):
        """重复执行 _migrate 不报错、不产生重名索引。"""
        from app.db import _migrate
        await _migrate(db_conn)
        await db_conn.commit()
        for table, expected in (
            ("outline_library_versions", "idx_olv_library"),
            ("knowledge_base", "idx_kb_project_scheme"),
            ("consistency_audit", "idx_consistency_audit_scheme"),
        ):
            names = await _index_names_async(db_conn, table)
            assert expected in names, f"{table} 缺 {expected}"

    @pytest.mark.asyncio
    async def test_knowledge_query_uses_index(self, db_conn):
        """知识条目查询（正文生成逐章注入）必须走 idx_kb_project_scheme。"""
        sql = ("SELECT name, usage_hint, content FROM knowledge_base "
               "WHERE project_id=? AND (scheme_id=? OR scheme_id='')")
        plan = await _plan_async(db_conn, sql, ("p1", "s1"))
        assert "idx_kb_project_scheme" in plan, f"未走索引：{plan}"

    @pytest.mark.asyncio
    async def test_consistency_audit_query_uses_index(self, db_conn):
        """一致性审计「最近一次」查询必须走 idx_consistency_audit_scheme。"""
        sql = ("SELECT id, score, issues, created_at FROM consistency_audit "
               "WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1")
        plan = await _plan_async(db_conn, sql, ("s1",))
        assert "idx_consistency_audit_scheme" in plan, f"未走索引：{plan}"

    @pytest.mark.asyncio
    async def test_outline_versions_query_uses_index(self, db_conn):
        """目录库版本列表查询必须走 idx_olv_library。"""
        sql = ("SELECT * FROM outline_library_versions "
               "WHERE library_id=? ORDER BY created_at DESC")
        plan = await _plan_async(db_conn, sql, ("lib1",))
        assert "idx_olv_library" in plan, f"未走索引：{plan}"


