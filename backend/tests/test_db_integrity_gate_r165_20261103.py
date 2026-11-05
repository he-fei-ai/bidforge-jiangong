"""R165 护栏（2026-11-03 · 启动期主库完整性闸门）。

生产实证（2026-11-01，logs/backend.log 15:28:12 起）：主库 system_logs B 树损坏
且无 -wal 文件 ⇒ ``_self_heal_corrupt_wal`` 因「无 WAL 直接 return False」完全不
探测，损坏静默运行数小时：system_logs 写每请求 fail-soft WARNING、普通写路径
database is locked、SSE 生成链路 RuntimeError —— 前端只看到「后端连接超时
（30s 无响应）」，日志里没有可行动的启动期告警。

本护栏锁定 ``db._startup_db_integrity_gate`` 四象限：
  A) 完好库零成本放行 + 逃生开关 DB_INTEGRITY_GATE=0 + 库不存在；
  B) 损坏可归属可再生缓存表 → 备份 + DROP 重建（业务表行数与内容分毫不动）；
  C) 不可归属 / 业务表 / 错误数达探针上限 / 混合损坏 → 绝不触碰（字节不变）；
  D) init_db 接线顺序 + 重建白名单封闭集 + 重建 DDL 与 SCHEMA_SQL 列级 parity。
另含一条**真损坏注入**的安全性质测试：损坏后门闸要么放弃（字节不变）、
要么修复后 quick_check 全绿且业务数据完好 —— 不依赖具体错误消息格式。
"""

import ast
import asyncio
import hashlib
import inspect
import logging
import sqlite3
import textwrap

import pytest
from app import db as appdb
from app.schema_sql import SCHEMA_SQL


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch):
    monkeypatch.delenv("DB_INTEGRITY_GATE", raising=False)


def _sha(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _make_db(path) -> None:
    """迷你生产形库：业务表 sections + 两张可再生缓存表（DDL 取自闸门白名单）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sections (id TEXT PRIMARY KEY, content TEXT DEFAULT '')")
    for ddl in appdb._REGENERABLE_CACHE_TABLES.values():
        conn.execute(ddl)
    conn.execute("INSERT INTO sections VALUES ('s1', 'hello')")
    for i in range(50):
        conn.execute(
            "INSERT INTO consistency_scan_cache"
            "(section_id, scheme_id, content_hash, context_hash, rows_json)"
            " VALUES (?,?,?,?,?)",
            (f"c{i}", "p", "h", "h", "[]"),
        )
        conn.execute("INSERT INTO export_cache(id) VALUES (?)", (f"e{i}",))
    conn.commit()
    conn.close()


def _fake_owner(mapping):
    return lambda _db_path: mapping


def _patch_check(monkeypatch, errors):
    """只伪造**首次**探针结果；闸门重建后的复检走真 quick_check。

    闸门流程是「预检 → 重建 → 复检」，若复检也吃到伪造错误，任何重建都会被
    误判失败回退 —— 那测的是桩而不是产品行为。
    """
    real = appdb._quick_check_errors
    state = {"n": 0}

    def fake(_p):
        state["n"] += 1
        if state["n"] == 1:
            return list(errors)
        return real(_p)

    monkeypatch.setattr(appdb, "_quick_check_errors", fake)


def _gate_bak_dirs(db_path):
    parent = db_path.parent
    return sorted(parent.glob("recov_bak_*_gate"))


# ---------------- A) 完好库：零成本放行 / 逃生开关 / 库不存在 ----------------


class TestHealthyPassthrough:
    def test_healthy_db_gate_is_noop(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before, "完好库不得有任何字节改动"
        assert _gate_bak_dirs(p) == [], "完好库不得创建备份目录"

    def test_escape_switch_disables_gate(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        monkeypatch.setenv("DB_INTEGRITY_GATE", "0")
        # 即便探针报损坏，逃生开关也必须短路
        monkeypatch.setattr(appdb, "_quick_check_errors", lambda _p: ["Tree 7 page 7: x"])
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before

    def test_missing_db_is_noop(self, tmp_path, monkeypatch):
        p = tmp_path / "not_exist.db"
        monkeypatch.setattr(appdb, "DB_PATH", p)
        assert appdb._startup_db_integrity_gate() is False


# ---------------- B) 可归属缓存表损坏：备份 + 重建，业务零触碰 ----------------


class TestRebuildPath:
    def test_table_tree_corruption_rebuilt(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        _patch_check(monkeypatch, ["Tree 700 page 700 cell 0: Child page depth differs"])
        monkeypatch.setattr(
            appdb, "_root_page_owner", _fake_owner({700: ("table", "consistency_scan_cache", "consistency_scan_cache")})
        )
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is True
        conn = sqlite3.connect(str(p))
        try:
            # 重建表存在、内容按缓存语义清空、列结构与白名单 DDL 一致
            cols = [r[1] for r in conn.execute("PRAGMA table_info(consistency_scan_cache)")]
            assert cols, "重建表必须存在"
            n = conn.execute("SELECT COUNT(*) FROM consistency_scan_cache").fetchone()[0]
            assert n == 0
            # 业务表与另一缓存表分毫不动
            assert conn.execute("SELECT content FROM sections WHERE id='s1'").fetchone()[0] == "hello"
            assert conn.execute("SELECT COUNT(*) FROM export_cache").fetchone()[0] == 50
        finally:
            conn.close()
        baks = _gate_bak_dirs(p)
        assert len(baks) == 1 and (baks[0] / p.name).exists()
        assert _sha(baks[0] / p.name) == before, "备份必须是修复前的原始字节"

    def test_index_message_attributed_to_owner_table(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        _patch_check(monkeypatch, ["wrong # of entries in index idx_export_cache_lookup"])
        monkeypatch.setattr(
            appdb,
            "_root_page_owner",
            _fake_owner(
                {
                    4001: ("table", "export_cache", "export_cache"),
                    4002: ("index", "idx_export_cache_lookup", "export_cache"),
                }
            ),
        )
        assert appdb._startup_db_integrity_gate() is True
        conn = sqlite3.connect(str(p))
        try:
            assert conn.execute("SELECT COUNT(*) FROM export_cache").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 1
        finally:
            conn.close()

    def test_rebuild_failure_restores_backup(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        _patch_check(monkeypatch, ["Tree 700 page 700: x"])
        monkeypatch.setattr(
            appdb, "_root_page_owner", _fake_owner({700: ("table", "consistency_scan_cache", "consistency_scan_cache")})
        )
        # 注入一条必失败的重建 DDL（语法坏）
        monkeypatch.setattr(appdb, "_REGENERABLE_CACHE_TABLES", {"consistency_scan_cache": "CREATE TABLE broken ("})
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before, "重建失败必须还原备份（字节逐位一致）"


# ---------------- C) 不可归属 / 业务表 / 超限：绝不触碰 ----------------


class TestAbortPath:
    def test_business_table_corruption_never_touched(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        monkeypatch.setattr(appdb, "_quick_check_errors", lambda _p: ["Tree 500 page 500: Invalid leaf type"])
        monkeypatch.setattr(appdb, "_root_page_owner", _fake_owner({500: ("table", "sections", "sections")}))
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before
        assert _gate_bak_dirs(p) == [], "放弃自愈不得创建备份目录"

    def test_unattributable_message_aborts(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        # freelist 类消息：无 Tree 前缀、无索引名；页号无 owner → 无法归属
        monkeypatch.setattr(appdb, "_quick_check_errors", lambda _p: ["Freelist: page 9999 is never used"])
        monkeypatch.setattr(appdb, "_root_page_owner", _fake_owner({}))
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before

    def test_probe_limit_aborts_even_if_attributable(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        monkeypatch.setattr(
            appdb,
            "_quick_check_errors",
            lambda _p: [f"Tree 700 page {700 + i}: x" for i in range(appdb._INTEGRITY_PROBE_LIMIT)],
        )
        monkeypatch.setattr(
            appdb, "_root_page_owner", _fake_owner({700: ("table", "consistency_scan_cache", "consistency_scan_cache")})
        )
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False, "错误数达探针上限 = 范围无法确证，必须放弃"
        assert _sha(p) == before

    def test_mixed_cache_and_business_aborts(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        monkeypatch.setattr(
            appdb,
            "_quick_check_errors",
            lambda _p: [
                "Tree 700 page 700: x",  # 可归属缓存表
                "Tree 500 page 500: also broken",  # 业务表 —— 任一不可重建即整体放弃
            ],
        )
        monkeypatch.setattr(
            appdb,
            "_root_page_owner",
            _fake_owner(
                {
                    700: ("table", "consistency_scan_cache", "consistency_scan_cache"),
                    500: ("table", "sections", "sections"),
                }
            ),
        )
        before = _sha(p)
        assert appdb._startup_db_integrity_gate() is False
        assert _sha(p) == before

    def test_real_corruption_safety_property(self, tmp_path, monkeypatch):
        """真损坏注入（不依赖消息格式）：要么放弃且字节不变，要么修好且全绿。"""
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        conn = sqlite3.connect(str(p))
        rp = conn.execute(
            "SELECT rootpage FROM sqlite_master" " WHERE type='table' AND name='consistency_scan_cache'"
        ).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()
        with open(p, "r+b") as f:
            f.seek((rp - 1) * page_size)
            f.write(b"\xde\xad\xbe\xef" * (page_size // 4))
        before = _sha(p)
        changed = appdb._startup_db_integrity_gate()
        if changed:
            assert appdb._quick_check_errors(p) == [], "修复路径必须以 quick_check 全绿收尾"
            conn = sqlite3.connect(str(p))
            try:
                assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 1
            finally:
                conn.close()
        else:
            assert _sha(p) == before, "放弃路径必须字节不变"
        # 两条路径都不允许留下「半修」状态：缓存表要么存在要么不存在，但业务表恒在
        conn = sqlite3.connect(str(p))
        try:
            conn.execute("SELECT content FROM sections WHERE id='s1'").fetchone()
        finally:
            conn.close()


# ---------------- D) 接线 / 白名单封闭集 / DDL parity ----------------


class TestWiringAndWhitelist:
    def test_init_db_calls_gate_after_wal_heal(self):
        src = textwrap.dedent(inspect.getsource(appdb.init_db))
        calls = [
            n.func.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "_startup_db_integrity_gate" in calls, "init_db 必须接完整性闸门"
        assert calls.index("_self_heal_corrupt_wal") < calls.index(
            "_startup_db_integrity_gate"
        ), "闸门必须在 WAL 自愈之后"

    def test_rebuild_whitelist_is_closed_cache_set(self):
        assert set(appdb._REGENERABLE_CACHE_TABLES) == {
            "consistency_scan_cache",
            "export_cache",
        }, "重建白名单只允许可丢弃缓存表；新增条目必须同时过 DDL parity 测试"

    def test_rebuild_ddl_matches_schema_sql_columns(self):
        for name, ddl in appdb._REGENERABLE_CACHE_TABLES.items():
            assert name in SCHEMA_SQL, f"{name} 必须存在于 SCHEMA_SQL"
            c_schema = sqlite3.connect(":memory:")
            c_schema.executescript(SCHEMA_SQL)
            cols_schema = [r[1] for r in c_schema.execute(f"PRAGMA table_info({name})")]
            c_gate = sqlite3.connect(":memory:")
            c_gate.execute(ddl)
            cols_gate = [r[1] for r in c_gate.execute(f"PRAGMA table_info({name})")]
            assert cols_schema == cols_gate, f"{name} 重建 DDL 列集必须与 SCHEMA_SQL 逐列一致"

    def test_init_db_starts_runtime_probe(self):
        src = textwrap.dedent(inspect.getsource(appdb.init_db))
        calls = [
            n.func.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "_start_integrity_probe_task" in calls, "init_db 必须接运行期完整性巡检"


# ---------------- E) 运行期完整性巡检（R165b · J 盘持续丢页取证后落地） ----------------


class TestRuntimeIntegrityProbe:
    def test_no_task_created_in_test_process(self, monkeypatch):
        monkeypatch.delenv("DB_INTEGRITY_PROBE_SECONDS", raising=False)
        appdb._start_integrity_probe_task()
        assert appdb._probe_task is None, "pytest 进程绝不能创建巡检任务（防泄漏）"

    def test_disabled_by_env_even_outside_tests(self, monkeypatch):
        monkeypatch.setattr(appdb, "_in_test_process", lambda: False)
        monkeypatch.setenv("DB_INTEGRITY_PROBE_SECONDS", "0")
        appdb._start_integrity_probe_task()
        assert appdb._probe_task is None

    async def test_probe_task_starts_and_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(appdb, "_in_test_process", lambda: False)
        monkeypatch.delenv("DB_INTEGRITY_PROBE_SECONDS", raising=False)
        appdb._start_integrity_probe_task()
        task = appdb._probe_task
        assert task is not None and not task.done()
        appdb._start_integrity_probe_task()  # 幂等：不得重复创建
        assert appdb._probe_task is task
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        appdb._probe_task = None

    async def test_probe_loop_warns_on_corruption(self, monkeypatch, caplog):
        monkeypatch.setattr(appdb, "_quick_check_errors", lambda _p: ["Tree 700 page 700: x"])
        loop_task = asyncio.create_task(appdb._integrity_probe_loop(0.01))
        await asyncio.sleep(0.08)
        loop_task.cancel()
        await asyncio.wait_for(asyncio.gather(loop_task, return_exceptions=True), 2)
        msgs = [r.message for r in caplog.records if "运行期完整性巡检" in r.message]
        assert msgs and "quick_check 异常" in msgs[0], "损坏必须以 WARNING 点名"

    def test_probe_once_healthy_and_missing(self, tmp_path, monkeypatch):
        p = tmp_path / "t.db"
        _make_db(p)
        monkeypatch.setattr(appdb, "DB_PATH", p)
        assert appdb._integrity_probe_once() == [], "完好库巡检必须返回空"
        monkeypatch.setattr(appdb, "DB_PATH", tmp_path / "absent.db")
        assert appdb._integrity_probe_once() == [], "库不存在时巡检零噪声"
