"""T4（2026-10-03）· 孤儿数据 GC 维护工具回归测试。

覆盖：分类纯函数 / 孤儿行查询 / 回收站移动+manifest / 硬删 / main 端到端
（dry-run 零改动、trash 模式可回退、硬删模式、超量硬上限拒绝）/ 零运行时挂钩静态锁。
"""
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from tools import gc_orphan_doc_trees as gc

# ---------------------------------------------------------------- classify


def test_classify_orphans_basic():
    live = {"p1", "p2"}
    names = ["p1", "p2", "dead1", "dead0", ".trash", "p0"]
    orphans, kept = gc.classify_orphans(names, live)
    assert orphans == ["dead0", "dead1", "p0"]  # 排序稳定、可快照对比
    assert kept == [".trash", "p1", "p2"]


def test_classify_orphans_empty_live_never_blanket():
    """live 集合为空时孤儿=全部（防御靠 GC_HARD_CAP，不在分类层吞掉）。"""
    orphans, _ = gc.classify_orphans(["a", ".trash"], set())
    assert orphans == ["a"]


def test_classify_orphans_protected_never_orphan():
    orphans, _ = gc.classify_orphans([".trash"], set(), protected_names={".trash"})
    assert orphans == []


# ---------------------------------------------------------------- db rows


def test_find_orphan_uploaded_outlines():
    """判据四态：在库 / 项目已删（真孤儿）/ 空待关联（保留）/ scheme 可恢复（保留）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE schemes(id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE uploaded_outlines(id TEXT PRIMARY KEY,"
        " project_id TEXT DEFAULT '', scheme_id TEXT DEFAULT '')")
    conn.execute("INSERT INTO projects VALUES('p1')")
    conn.execute("INSERT INTO schemes VALUES('s1')")
    for rid, pid, sid in [
        ("r_live", "p1", ""),
        ("r_dead", "gone", ""),
        ("r_pending", "", ""),          # 空 project_id = 合法待关联态，保留
        ("r_rescuable", "gone", "s1"),  # scheme 仍存活可反查恢复，保留
    ]:
        conn.execute("INSERT INTO uploaded_outlines VALUES(?,?,?)", (rid, pid, sid))
    assert gc.find_orphan_uploaded_outlines(conn) == ["r_dead"]
    conn.close()


# ---------------------------------------------------------------- trash / delete


def _mk_tree(root: Path, name: str, files: int = 1) -> Path:
    d = root / name / "documents" / "d1" / "parsed"
    d.mkdir(parents=True, exist_ok=True)
    (d / "f.json").write_text("{}", encoding="utf-8")
    return root / name


def test_move_to_trash_and_manifest(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    for n in ("dead1", "dead2", "live1"):
        _mk_tree(root, n)
    trash = root / ".trash" / "orphan-gc-test"
    moved, failed = gc.move_to_trash(root, ["dead1", "dead2", "ghost"], trash)
    assert moved == ["dead1", "dead2", "ghost"]  # 不存在的条目按已清理计
    assert failed == []
    assert not (root / "dead1").exists()
    assert (trash / "dead1").exists()
    assert (root / "live1").exists()
    manifest = json.loads((trash / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["moved"] == moved and manifest["failed"] == []


def test_move_to_trash_dst_conflict_recorded_as_failed(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    _mk_tree(root, "dead1")
    trash = root / ".trash" / "gc"
    _mk_tree(trash, "dead1")  # 回收站已有同名 → 记失败不中断
    moved, failed = gc.move_to_trash(root, ["dead1"], trash)
    assert moved == [] and failed == ["dead1"]
    assert (root / "dead1").exists()  # 原目录完好


def test_delete_trees(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    _mk_tree(root, "dead1")
    _mk_tree(root, "live1")
    removed, failed = gc.delete_trees(root, ["dead1", "ghost"])
    assert removed == ["dead1", "ghost"] and failed == []
    assert not (root / "dead1").exists()
    assert (root / "live1").exists()


# ---------------------------------------------------------------- main 端到端


@pytest.fixture
def env(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    (backend / "data").mkdir(parents=True)
    db = backend / "data" / "scheme_assistant.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE schemes(id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE uploaded_outlines(id TEXT PRIMARY KEY,"
        " project_id TEXT DEFAULT '', scheme_id TEXT DEFAULT '')")
    conn.execute("INSERT INTO projects VALUES('p1')")
    conn.execute("INSERT INTO uploaded_outlines VALUES('r_dead','gone','')")
    conn.commit()
    conn.close()
    docs = backend / "data" / "projects"
    for n in ("p1", "dead1", "dead2"):
        _mk_tree(docs, n)
    (docs / ".trash").mkdir()
    monkeypatch.setattr(gc, "DB_PATH", db)
    monkeypatch.setattr(gc, "DOCS_ROOT", docs)
    return {"backend": backend, "docs": docs, "db": db}


def test_main_dry_run_changes_nothing(env, capsys):
    assert gc.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "orphans=2" in out
    assert (env["docs"] / "dead1").exists()  # 未动任何数据
    assert (env["docs"] / "dead2").exists()


def test_main_apply_trash_mode_reversible(env, capsys):
    assert gc.main(["--apply"]) == 0
    assert not (env["docs"] / "dead1").exists()
    assert not (env["docs"] / "dead2").exists()
    assert (env["docs"] / "p1").exists()
    trash = env["docs"] / ".trash"
    gc_dirs = [d for d in trash.iterdir() if d.name.startswith("orphan-gc-")]
    assert len(gc_dirs) == 1
    manifest = json.loads((gc_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["moved"]) == ["dead1", "dead2"]
    # 孤儿行兜底清理
    conn = sqlite3.connect(str(env["db"]))
    n = conn.execute("SELECT COUNT(*) FROM uploaded_outlines").fetchone()[0]
    conn.close()
    assert n == 0
    # 可回退：把回收站内容整体移回即恢复
    for name in manifest["moved"]:
        (gc_dirs[0] / name).rename(env["docs"] / name)
    assert (env["docs"] / "dead1").exists()


def test_main_apply_delete_mode(env, capsys):
    assert gc.main(["--apply", "--mode", "delete"]) == 0
    assert not (env["docs"] / "dead1").exists()
    assert (env["docs"] / "p1").exists()


def test_main_aborts_over_hard_cap(env, monkeypatch, capsys):
    monkeypatch.setattr(gc, "GC_HARD_CAP", 1)
    assert gc.main([]) == 3  # 未加 --force → 拒绝执行
    assert "ABORT" in capsys.readouterr().out
    assert (env["docs"] / "dead1").exists()
    assert gc.main(["--apply", "--force"]) == 0  # 显式 force 才执行


def test_main_db_missing(env, monkeypatch):
    monkeypatch.setattr(gc, "DB_PATH", env["db"].with_name("nope.sqlite"))
    assert gc.main([]) == 2


# ---------------------------------------------------------------- 静态锁


def test_no_runtime_wiring():
    """零运行时挂钩锁：app/ 内不得 import 本工具（GC 只离线执行，
    删除项目时的实时清理由 doc_storage.delete_project_docs_root 负责）。"""
    import app
    app_root = Path(app.__file__).parent
    hits = [
        str(p.relative_to(app_root.parent))
        for p in app_root.rglob("*.py")
        if "gc_orphan_doc_trees" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == []
