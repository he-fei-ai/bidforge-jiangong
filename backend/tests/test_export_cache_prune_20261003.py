"""T1（2026-10-03）· 导出缓存保留策略共享 helper 回归测试。

根因：export_pdf 分支此前只 INSERT INTO export_cache 从不裁剪 —— DOCX 分支
有完整的 prune（删陈旧行 + 连带删磁盘文件），PDF 分支完全没有 → DB 行与
EXPORTS_DIR 磁盘文件随 PDF 导出次数无限增长。

修法：把 DOCX 内联裁剪逻辑逐字提取为 `_prune_export_cache` 共享 helper，
DOCX（行为零变化）与 PDF（补齐同一策略）共同调用，跨格式合计最多 5 份/方案。
"""
import ast
import uuid
from pathlib import Path

import pytest

import app.db as _appdb
import app.routers.export as export_mod
from app.db import close_db, get_conn, init_db

EXPORT_PY = Path(export_mod.__file__)
KEEP = 5  # 保留策略：最多 5 份/方案（与前端 tooltip 说明对齐）


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "export_prune.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    try:
        yield db, sid, tmp_path
    finally:
        await close_db()


async def _insert_cache(db, sid, path, created_at):
    """显式 created_at 插入（裁剪按 created_at DESC, rowid DESC 排序）。"""
    await db.execute(
        "INSERT INTO export_cache "
        "(id, project_id, scheme_id, config_hash, content_fingerprint, cache_key, "
        " result_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "", sid, "cfg", f"fp-{uuid.uuid4().hex[:8]}",
         f"{sid}_cfg", str(path), created_at))


async def _row_count(db, sid):
    cur = await db.execute("SELECT COUNT(*) AS n FROM export_cache WHERE scheme_id=?",
                           (sid,))
    return dict(await cur.fetchone())["n"]


async def test_prune_keeps_five_newest_and_deletes_stale_files(ctx):
    """超过 5 份时：保留最新 5 行，更旧行删除且磁盘文件连带清理。"""
    db, sid, tmp_path = ctx
    files = []
    for i in range(7):
        f = tmp_path / f"old_{i}.docx"
        f.write_bytes(b"x")
        files.append(f)
        await _insert_cache(db, sid, f, f"2026-10-01 10:00:{i:02d}")
    await db.commit()

    protect = tmp_path / "current.docx"
    protect.write_bytes(b"new")
    await export_mod._prune_export_cache(db, sid, protect_path=protect)

    assert await _row_count(db, sid) == KEEP
    # 最早的 2 份（i=0,1）行与文件都应被清理
    assert not files[0].exists()
    assert not files[1].exists()
    # 保留的 5 份与当前产物文件完好
    for f in files[2:]:
        assert f.exists()
    assert protect.exists()


async def test_prune_zombie_rows_pruned_even_within_top5(ctx):
    """文件已丢失的僵尸行即使排在最新 5 位内也照裁（口径与旧 DOCX 逻辑一致）。"""
    db, sid, tmp_path = ctx
    real = []
    for i in range(3):
        f = tmp_path / f"real_{i}.docx"
        f.write_bytes(b"x")
        real.append(f)
        await _insert_cache(db, sid, f, f"2026-10-01 10:0{i}:00")
    ghosts = [tmp_path / f"ghost_{i}.docx" for i in range(3)]
    for i, g in enumerate(ghosts):
        await _insert_cache(db, sid, g, f"2026-10-01 09:0{i}:00")  # 不落盘
    await db.commit()

    await export_mod._prune_export_cache(db, sid, protect_path=tmp_path / "cur.docx")

    assert await _row_count(db, sid) == 3
    for f in real:
        assert f.exists()


async def test_prune_protected_file_not_deleted_even_if_row_stale(ctx):
    """当前产物文件受 keep_paths 保护：即便其行被判 stale，文件也不得被删。"""
    db, sid, tmp_path = ctx
    for i in range(7):
        f = tmp_path / f"old_{i}.docx"
        f.write_bytes(b"x")
        await _insert_cache(db, sid, f, f"2026-10-01 10:00:{i:02d}")
    protect = tmp_path / "current.pdf"
    protect.write_bytes(b"new")
    # 把当前产物登记为一行并置旧 created_at，模拟「同秒插入次序异常」场景：
    # 行会被判 stale，但 keep_paths 必须保住文件（误删 → FileResponse 404/500）。
    await _insert_cache(db, sid, protect, "2026-10-01 08:00:00")
    await db.commit()

    await export_mod._prune_export_cache(db, sid, protect_path=protect)

    assert protect.exists()


async def test_prune_fail_soft_on_none_cursor(ctx, monkeypatch):
    """R13 守卫：db.execute 返回 None（连接/事务异常）时 fail-soft 跳过，不抛错。"""
    db, sid, tmp_path = ctx
    f = tmp_path / "a.docx"
    f.write_bytes(b"x")
    await _insert_cache(db, sid, f, "2026-10-01 10:00:00")
    await db.commit()

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(db, "execute", _none)
    await export_mod._prune_export_cache(db, sid, protect_path=tmp_path / "cur.docx")
    # 不抛异常即通过；文件未被误删
    assert f.exists()


def _route_fn(name):
    tree = ast.parse(EXPORT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in export.py")


@pytest.mark.parametrize("route", ["export_docx", "export_pdf"])
def test_both_branches_call_shared_prune(route):
    """静态 parity 锁：DOCX 与 PDF 两条导出链都必须调用共享裁剪 helper，
    防止未来新格式分支再次漏接保留策略（重演 T1）。"""
    calls = [n.func.id for n in ast.walk(_route_fn(route))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_prune_export_cache" in calls, f"{route} 未接入共享缓存裁剪"


def test_helper_no_inline_duplicate_in_routes():
    """防分叉：裁剪判定逻辑必须只存在于共享 helper，路由分支内不得重抄
    （扫标志性的「keep_paths」局部变量，路由函数体内出现即说明有人复制了一份）。"""
    for route in ("export_docx", "export_pdf"):
        names = {n.id for n in ast.walk(_route_fn(route)) if isinstance(n, ast.Name)}
        assert "keep_paths" not in names, f"{route} 内联复制了裁剪逻辑"
