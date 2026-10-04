"""preflight_runs 去重清理工具护栏（2026-10-03 · R36 遗留项收口）。

覆盖三层：
  ① 判据保守性 —— 11 列全等才成组，指纹/结论不同永不误删（AI 波动行保留）；
  ② --before 时间窗 —— 修复上线后的正常重复总检不得被抹掉；
  ③ CLI 安全轨 —— dry-run 不动数据 / apply 前自动备份 / 超硬上限拒绝执行。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import cleanup_preflight_runs as mod  # noqa: E402

_CREATE = """
CREATE TABLE IF NOT EXISTS preflight_runs (
    id TEXT PRIMARY KEY,
    scheme_id TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    content_fingerprint TEXT DEFAULT '',
    rule_version TEXT DEFAULT '',
    total REAL DEFAULT 0,
    grade TEXT DEFAULT '',
    verdict TEXT DEFAULT '',
    released INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    counts TEXT DEFAULT '{}',
    dimensions TEXT DEFAULT '[]',
    findings TEXT DEFAULT '[]',
    stats TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_CREATE)
    conn.commit()
    conn.close()
    return db


def insert(db: Path, *, row_id: str, scheme: str = "sc1", fp: str = "F1",
           total: float = 82.0, findings: str = "[]",
           created_at: str = "2026-10-01 10:00:00") -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO preflight_runs (id, scheme_id, content_fingerprint,"
        " rule_version, total, grade, verdict, released, blocked, counts,"
        " dimensions, findings, stats, created_at)"
        " VALUES (?, ?, ?, '1.8.0', ?, 'B', 'ok', 0, 1, '{}', '[]', ?, '{}', ?)",
        (row_id, scheme, fp, total, findings, created_at))
    conn.commit()
    conn.close()


def rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT id FROM preflight_runs ORDER BY rowid").fetchall()
    finally:
        conn.close()


def open_rw(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


# ===========================================================================
# 一、判据保守性
# ===========================================================================
class TestDedupCriterion:
    def test_exact_duplicates_keep_earliest(self, tmp_path):
        """3 条 11 列全等 → 一组，keep 最早，delete 其余 2 条。"""
        db = make_db(tmp_path)
        for i, ts in enumerate(("2026-10-01 10:00:00", "2026-10-01 10:00:05",
                                "2026-10-01 10:00:09")):
            insert(db, row_id=f"r{i}", created_at=ts)
        conn = open_rw(db)
        try:
            groups = mod.find_duplicate_groups(conn)
            assert len(groups) == 1
            assert len(groups[0]["delete_rowids"]) == 2
            deleted = mod.delete_rows(conn, groups)
            assert deleted == 2
            # 保留的是最早一条（rowid 最小 = r0）
            assert rows(db) == [("r0",)]
        finally:
            conn.close()

    def test_different_fingerprint_not_grouped(self, tmp_path):
        db = make_db(tmp_path)
        insert(db, row_id="a", fp="F1")
        insert(db, row_id="b", fp="F2", created_at="2026-10-01 10:00:05")
        conn = open_rw(db)
        try:
            assert mod.find_duplicate_groups(conn) == []
        finally:
            conn.close()

    def test_ai_fluctuation_same_fp_different_findings_kept(self, tmp_path):
        """同指纹但结论不同（AI 非确定性）→ 两行都是真实历史，不得删。"""
        db = make_db(tmp_path)
        insert(db, row_id="a", findings="[]")
        insert(db, row_id="b", findings='[{"rule_id":"STD-03"}]',
               created_at="2026-10-01 10:00:05")
        conn = open_rw(db)
        try:
            assert mod.find_duplicate_groups(conn) == []
        finally:
            conn.close()

    def test_different_scheme_not_grouped(self, tmp_path):
        db = make_db(tmp_path)
        insert(db, row_id="a", scheme="sc1")
        insert(db, row_id="b", scheme="sc2", created_at="2026-10-01 10:00:05")
        conn = open_rw(db)
        try:
            assert mod.find_duplicate_groups(conn) == []
        finally:
            conn.close()

    def test_stats_part_of_identity_two_cols_removed_would_overmerge(self, tmp_path):
        """stats 是 11 列判据的一员：同结论但客观统计不同（如字数变）的行
        不得被当重复。本例是 A/B 锚点：从 _DUP_COLUMNS 摸掉任一列后，
        对应差异列的用例定向失败（这里拿 stats 举例）。"""
        db = make_db(tmp_path)
        conn = open_rw(db)
        try:
            conn.execute(
                "INSERT INTO preflight_runs (id, scheme_id, content_fingerprint,"
                " total, grade, verdict, released, blocked, counts, dimensions,"
                " findings, stats, created_at) VALUES"
                " ('a','sc1','F1',82,'B','ok',0,1,'{}','[]','[]','{\"words\":100}',"
                "  '2026-10-01 10:00:00'),"
                " ('b','sc1','F1',82,'B','ok',0,1,'{}','[]','[]','{\"words\":200}',"
                "  '2026-10-01 10:00:05')")
            conn.commit()
            assert mod.find_duplicate_groups(conn) == []
        finally:
            conn.close()


# ===========================================================================
# 二、--before 时间窗
# ===========================================================================
class TestBeforeWindow:
    def test_rows_after_before_untouched(self, tmp_path):
        """before 定在第 2 条之前：窗口外的第 3 条不参与去重、不被删。"""
        db = make_db(tmp_path)
        insert(db, row_id="old1", created_at="2026-10-01 10:00:00")
        insert(db, row_id="old2", created_at="2026-10-01 10:00:05")
        insert(db, row_id="legit", created_at="2026-10-04 09:00:00")
        conn = open_rw(db)
        try:
            groups = mod.find_duplicate_groups(
                conn, before="2026-10-02 00:00:00")
            assert len(groups) == 1
            assert len(groups[0]["delete_rowids"]) == 1
            mod.delete_rows(conn, groups)
            assert rows(db) == [("old1",), ("legit",)]
        finally:
            conn.close()

    def test_idempotent_second_pass(self, tmp_path):
        db = make_db(tmp_path)
        insert(db, row_id="a")
        insert(db, row_id="b", created_at="2026-10-01 10:00:05")
        conn = open_rw(db)
        try:
            mod.delete_rows(conn, mod.find_duplicate_groups(conn))
            assert mod.find_duplicate_groups(conn) == []
        finally:
            conn.close()


# ===========================================================================
# 三、CLI 安全轨
# ===========================================================================
class TestCliSafety:
    def test_dry_run_changes_nothing(self, tmp_path):
        db = make_db(tmp_path)
        insert(db, row_id="a")
        insert(db, row_id="b", created_at="2026-10-01 10:00:05")
        assert mod.main([], db_path=db) == 0
        assert rows(db) == [("a",), ("b",)]
        assert not list(tmp_path.glob("*_preflight_gc_bak_*.db"))

    def test_apply_deletes_with_backup(self, tmp_path):
        db = make_db(tmp_path)
        insert(db, row_id="a")
        insert(db, row_id="b", created_at="2026-10-01 10:00:05")
        assert mod.main(["--apply"], db_path=db) == 0
        assert rows(db) == [("a",)]
        backups = list(tmp_path.glob("*_preflight_gc_bak_*.db"))
        assert len(backups) == 1
        # 备份必须是真的整库快照：重复行仍在备份里
        conn = sqlite3.connect(str(backups[0]))
        try:
            assert len(conn.execute("SELECT * FROM preflight_runs").fetchall()) == 2
        finally:
            conn.close()

    def test_hard_cap_aborts_without_force(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        insert(db, row_id="a")
        insert(db, row_id="b", created_at="2026-10-01 10:00:05")
        monkeypatch.setattr(mod, "DELETE_HARD_CAP", 0)
        assert mod.main(["--apply"], db_path=db) == 3
        assert rows(db) == [("a",), ("b",)]
        assert not list(tmp_path.glob("*_preflight_gc_bak_*.db"))

    def test_force_overrides_cap(self, tmp_path, monkeypatch):
        db = make_db(tmp_path)
        insert(db, row_id="a")
        insert(db, row_id="b", created_at="2026-10-01 10:00:05")
        monkeypatch.setattr(mod, "DELETE_HARD_CAP", 0)
        assert mod.main(["--apply", "--force"], db_path=db) == 0
        assert rows(db) == [("a",)]

    def test_missing_db_returns_2(self, tmp_path):
        assert mod.main([], db_path=tmp_path / "nope.db") == 2
