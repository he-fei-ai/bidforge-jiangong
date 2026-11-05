"""R176 · B1 实施护栏（2026-11-04）

覆盖本轮两处改动：

D2-1（缓存命中路径读取收窄）
  · ``_prepare_export`` 的章节查询由 ``SELECT *`` 收窄为 6 列；
  · **指纹逐字节不变**：同一份数据分别用全列行与收窄行构造 prep，
    ``_content_fingerprint`` 返回值必须完全相等；
  · 反向证明：6 列任一处变化都会改变指纹，被裁掉的 4 列变化**不改变**指纹。

P1-3（事件循环同步阻塞下沉）
  · ``audit_content`` / PIL ``verify()`` / PDF 整文件读 / PDF 整文件写
    必须以 ``asyncio.to_thread`` 形态调用（AST 锁调用形态，不锚字符串计数）；
  · 原同步调用不得残留。

R176 补漏（命中分支完整性校验下沉）
  · ``_cache_row_viable``（DOCX 整包 CRC / PDF 头尾校验）必须在工作线程执行；
  · 判据不得放宽：截断 DOCX / PDF 经命中判据仍判不可用（调用方重建）；
  · 记忆化按 ``(path, size, mtime_ns)`` 失效，绝不把旧结论套用到重写后的文件。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
import zipfile
from pathlib import Path

import pytest
from app.routers import export as E
from app.routers._export_cache import _content_fingerprint

APP = Path(E.__file__).resolve()

# 收窄后必须保留的列（与 ``_prepare_export`` 查询逐字同源）
NARROW_COLUMNS = ("id", "parent_id", "sort_order", "level", "title", "content")
# 明确裁掉、且**不得**影响指纹的四列（只被 collect_export_issues 消费）
DROPPED_COLUMNS = ("status", "review_status", "word_count", "outline_json")


def _prep(sections: list[dict]) -> dict:
    """构造 ``_content_fingerprint`` 所需的最小 prep（各维度取稳定值）。"""
    return {
        "config": {"font_size": 12},
        "scheme": {"name": "方案A", "project_id": "p1"},
        "fe_codes": ["graph TD;A-->B"],
        "fe_images": {},
        "chart_fp": [],
        "sections": sections,
        "global_facts": [],
        "appendix_sources": [],
        "global_facts_status": {"ok": True},
        "image_config": {},
    }


def _sec(**over) -> dict:
    base = {"id": "s1", "parent_id": "", "sort_order": 0, "level": 1, "title": "第1章", "content": "正文内容"}
    base.update(over)
    return base


def _fp(sections: list[dict]) -> tuple[str, str]:
    return _content_fingerprint(_prep(sections))


def _fn_ast(name: str) -> ast.AST:
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def _to_thread_targets(node: ast.AST) -> set[str]:
    """收集 ``asyncio.to_thread(<target>, ...)`` 的 target 名（Name / Attribute）。"""
    out: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "to_thread"):
            continue
        if not call.args:
            continue
        target = call.args[0]
        if isinstance(target, ast.Name):
            out.add(target.id)
        elif isinstance(target, ast.Attribute):
            out.add(target.attr)
    return out


# ===========================================================================
# D2-1 · 必需列清单与指纹等价
# ===========================================================================


def test_sections_query_uses_explicit_columns():
    """缓存命中路径不得再用 ``SELECT *``（全表列随 schema 增长而漂移）。"""
    src = inspect.getsource(E._prepare_export)
    assert '"SELECT * FROM sections' not in src
    for col in NARROW_COLUMNS:
        assert col in src
    assert "FROM sections WHERE scheme_id=?" in src


@pytest.mark.parametrize(
    "key,new_value",
    [
        ("id", "s2"),
        ("parent_id", "p-root"),
        ("sort_order", 99),
        ("level", 2),
        ("title", "另一个标题"),
        ("content", "换一段正文"),
    ],
)
def test_each_kept_column_is_fingerprint_relevant(key, new_value):
    """6 列任一变化都必须改变指纹 —— 收窄不能删掉指纹真正依赖的列。"""
    assert _fp([_sec()]) != _fp([_sec(**{key: new_value})]), f"{key} 不参与指纹：收窄后可能被误删"


@pytest.mark.parametrize(
    "key,new_value",
    [
        ("status", "generated"),
        ("review_status", "approved"),
        ("word_count", 500),
        ("outline_json", '{"id": "9.9"}'),
    ],
)
def test_dropped_columns_do_not_affect_fingerprint(key, new_value):
    """被裁掉的 4 列变化不得改变指纹（证明它们确非指纹维度）。"""
    assert _fp([_sec()]) == _fp([_sec(**{key: new_value})]), f"{key} 意外参与指纹：收窄会改变命中判据"


@pytest.mark.asyncio
async def test_prepare_export_returns_narrow_rows(db_conn):
    """真实查询只返回 6 列，且下游 Plan 不需要被裁掉的列。"""
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("r176s", "p", "方案"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title,"
        " status, content, word_count, review_status, outline_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r176c", "r176s", "", 0, 1, "第1章", "generated", "正文", 120, "draft", '{"id": "1"}'),
    )
    await db_conn.commit()

    plan = await E._prepare_export("r176s", {"config": {}}, db_conn)
    assert len(plan["sections"]) == 1
    assert set(plan["sections"][0]) == set(NARROW_COLUMNS)


@pytest.mark.asyncio
async def test_fingerprint_byte_identical_full_vs_narrow(db_conn):
    """同一份数据：全列行 vs 收窄行的指纹必须逐字节相同。"""
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("r176f", "p", "方案"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title,"
        " status, content, word_count, review_status, outline_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r176fc", "r176f", "", 0, 1, "第1章", "generated", "正文", 120, "draft", '{"id": "1"}'),
    )
    await db_conn.commit()

    cur = await db_conn.execute("SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order, level, id", ("r176f",))
    full_rows = [dict(r) for r in await cur.fetchall()]
    cur = await db_conn.execute(
        "SELECT id, parent_id, sort_order, level, title, content"
        " FROM sections WHERE scheme_id=? ORDER BY sort_order, level, id",
        ("r176f",),
    )
    narrow_rows = [dict(r) for r in await cur.fetchall()]
    assert set(DROPPED_COLUMNS) <= set(full_rows[0]) - set(narrow_rows[0])

    plan = await E._prepare_export("r176f", {"config": {}}, db_conn)
    narrow_prep = dict(plan)
    full_prep = dict(plan)
    full_prep["sections"] = full_rows

    narrow_fp = _content_fingerprint(narrow_prep)
    full_fp = _content_fingerprint(full_prep)
    assert narrow_fp == full_fp, "收窄后指纹漂移 —— 缓存命中判据被改变"
    assert narrow_fp[0] == full_fp[0] and narrow_fp[1] == full_fp[1]


# ===========================================================================
# P1-3 · 同步阻塞下沉线程池
# ===========================================================================


def test_audit_content_offloaded_in_prepare_export():
    node = _fn_ast("_prepare_export")
    assert "audit_content" in _to_thread_targets(node), "audit_content 仍在事件循环同步执行"


def test_pil_verify_offloaded_in_render():
    node = _fn_ast("_prepare_export_render")
    assert "_verify_image_bytes" in _to_thread_targets(node), "PIL verify 未下沉线程池"
    src = inspect.getsource(E._prepare_export_render)
    assert "PILImage.open(data).verify()" not in src, "旧同步调用残留"


def test_pdf_read_write_offloaded_in_export_pdf():
    node = _fn_ast("export_pdf")
    targets = _to_thread_targets(node)
    assert "_read_path_bytes" in targets, "PDF 整文件读未下沉线程池"
    assert "write_bytes" in targets, "PDF 整文件写未下沉线程池"
    src = inspect.getsource(E.export_pdf)
    assert 'with open(tmp_pdf, "rb") as f:' not in src, "旧同步读残留"
    assert "_tmp_pdf.write_bytes(pdf_bytes)" not in src, "旧同步写残留"


def test_blocking_helpers_create_fresh_handles():
    """helper 必须在工作线程内新建句柄，避免跨线程复用。"""
    read_src = inspect.getsource(E._read_path_bytes)
    assert 'with open(path, "rb") as f:' in read_src
    verify_src = inspect.getsource(E._verify_image_bytes)
    assert "PILImage.open(data).verify()" in verify_src


# ===========================================================================
# R176 补漏 · 命中分支完整性校验下沉线程（判据不放宽）
# ===========================================================================


def _make_docx(path: Path) -> Path:
    """最小可用 DOCX（含 testzip 与必需部件都能通过的 ZIP 结构）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    return path


async def _seed_scheme(db, scheme_id: str) -> None:
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (scheme_id, "p1", "方案甲"))
    await db.commit()


async def _seed_cache_row(db, scheme_id: str, config_hash: str, content_fp: str, path: Path) -> None:
    await db.execute(
        "INSERT INTO export_cache (id, scheme_id, config_hash," " content_fingerprint, result_path) VALUES (?,?,?,?,?)",
        (f"{scheme_id}-cache", scheme_id, config_hash, content_fp, str(path)),
    )
    await db.commit()


def _patch_rebuild(monkeypatch, exports_dir, build, *, convert=None, content: str = "abc12345") -> None:
    """把导出链路收敛到「未命中 → 重建」所需的最小桩。

    只替换渲染/落库/收尾，**不替换**``_cache_row_viable`` —— 命中判定与其
    线程化校验保持真实实现，这样坏缓存被拒才是真信号。
    """

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(E, "EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(E, "_prepare_export", _prep_for_hit)
    monkeypatch.setattr(E, "_content_fingerprint", lambda _prep: ("cfg", content))
    monkeypatch.setattr(E, "_guard_numbering_consistency", _noop)
    monkeypatch.setattr(E, "_prepare_export_render", _noop)
    monkeypatch.setattr(E, "_build_docx_task", lambda out_path, _prep: (str(out_path),))
    monkeypatch.setattr(E, "_build_docx_sync", build)
    monkeypatch.setattr(E, "_persist_export_cache_row", _noop)
    monkeypatch.setattr(E, "_finalize_export_delivery", _noop)
    if convert is not None:
        monkeypatch.setattr(E, "_convert_docx_to_pdf", convert)


def test_hit_branch_integrity_check_offloaded_to_thread():
    """DOCX / PDF 两个命中分支都只能经 to_thread 调 ``_cache_row_viable``。"""
    for fn in (E.export_docx, E.export_pdf):
        node = _fn_ast(fn.__name__)
        assert "_cache_row_viable" in _to_thread_targets(
            node
        ), f"{fn.__name__} 命中分支的完整性校验仍在事件循环同步执行"
        assert "_cache_row_viable(cached[0])" not in inspect.getsource(fn), f"{fn.__name__} 仍残留裸同步调用形态"


@pytest.mark.asyncio
async def test_integrity_check_executes_in_worker_thread(monkeypatch, tmp_path):
    """校验必须真的在工作线程执行（不得跑在事件循环所在的主线程）。"""
    seen: list[str] = []

    def _probe(path, size, mtime_ns):
        seen.append(threading.current_thread().name)
        return True

    import app.routers._export_cache as C

    monkeypatch.setattr(C, "_pdf_integrity_ok", _probe)
    p = tmp_path / "probe.pdf"
    p.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")

    assert await asyncio.to_thread(C._cache_row_viable, str(p)) is True
    assert seen, "完整性校验未被调用（判据被绕过）"
    assert seen[0] != threading.main_thread().name, f"完整性校验跑在主线程 {seen[0]}：命中路径仍被同步阻塞"


@pytest.mark.asyncio
async def test_truncated_docx_rejected_through_hit_check(tmp_path):
    """截断 DOCX 经验证仍判不可用 → 命中分支拒绝，调用方走重建。"""
    good = _make_docx(tmp_path / "ok.docx")
    assert await asyncio.to_thread(E._cache_row_viable, str(good)) is True

    broken = tmp_path / "broken.docx"
    data = good.read_bytes()
    # 去掉后半截 = 中央目录缺失，ZIP 无法解析（写盘中断/磁盘满的典型形态）
    broken.write_bytes(data[: max(1, len(data) // 2)])
    assert (
        await asyncio.to_thread(E._cache_row_viable, str(broken)) is False
    ), "截断 DOCX 仍判可用缓存 → 命中后交付打不开的文档"


@pytest.mark.asyncio
async def test_truncated_pdf_rejected_through_hit_check(tmp_path):
    """截断 PDF（缺 trailer）经验证仍判不可用；完整 PDF 可用。"""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n")
    assert await asyncio.to_thread(E._cache_row_viable, str(broken)) is False

    good = tmp_path / "ok.pdf"
    good.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    assert await asyncio.to_thread(E._cache_row_viable, str(good)) is True


@pytest.mark.asyncio
async def test_memoized_verdict_not_reused_after_rewrite(tmp_path):
    """记忆化键含 (path, size, mtime_ns)：文件重写后必须重新判定。"""
    p = _make_docx(tmp_path / "x.docx")
    assert await asyncio.to_thread(E._cache_row_viable, str(p)) is True
    p.write_bytes(b"not a zip")
    assert (
        await asyncio.to_thread(E._cache_row_viable, str(p)) is False
    ), "旧结论被套用到重写后的文件（记忆化键未覆盖 size/mtime）"


@pytest.mark.asyncio
async def test_concurrent_hits_do_not_corrupt_memo(tmp_path):
    """并发首次命中（记忆化未预热）不得崩溃/写坏缓存字典。"""
    p = _make_docx(tmp_path / "c.docx")
    results = await asyncio.gather(*[asyncio.to_thread(E._cache_row_viable, str(p)) for _ in range(32)])
    assert all(r is True for r in results)


@pytest.mark.asyncio
async def test_corrupt_docx_cache_row_falls_back_to_rebuild(db_conn, tmp_path, monkeypatch):
    """缓存行指向截断 DOCX：命中被拒 → 走全量重建，绝不交付坏产物。"""
    await _seed_scheme(db_conn, "rb1")
    good = _make_docx(tmp_path / "src.docx")
    data = good.read_bytes()
    broken = tmp_path / "cached_broken.docx"
    broken.write_bytes(data[: max(1, len(data) // 2)])
    await _seed_cache_row(db_conn, "rb1", "cfg", "abc12345", broken)

    calls = {"build": 0}

    def _fake_build(path):
        calls["build"] += 1
        _make_docx(Path(path))
        return {"failed": 0}

    _patch_rebuild(monkeypatch, tmp_path, _fake_build)

    resp = await E.export_docx("rb1", {}, db_conn)
    assert resp.headers["X-Cache-Status"] == "miss", "坏缓存仍被当作命中返回（截断产物会直接交给用户）"
    assert calls["build"] == 1, "命中被拒后没有重建产物"
    assert Path(resp.path) != broken
    assert await asyncio.to_thread(E._cache_row_viable, resp.path) is True


@pytest.mark.asyncio
async def test_corrupt_pdf_cache_row_falls_back_to_rebuild(db_conn, tmp_path, monkeypatch):
    """缓存行指向截断 PDF：命中被拒 → 重新构建并转换，不交付坏 PDF。"""
    await _seed_scheme(db_conn, "rb2")
    broken = tmp_path / "cached_broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n")
    await _seed_cache_row(db_conn, "rb2", "cfg", "abc12345|fmt=pdf", broken)

    calls = {"convert": 0}

    def _fake_build(path):
        _make_docx(Path(path))
        return {"failed": 0}

    def _fake_convert(_src, dst):
        calls["convert"] += 1
        Path(dst).write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    _patch_rebuild(monkeypatch, tmp_path, _fake_build, convert=_fake_convert)

    resp = await E.export_pdf("rb2", {}, db_conn)
    assert resp.headers["X-Cache-Status"] == "miss", "坏 PDF 缓存仍被当作命中返回"
    assert calls["convert"] == 1, "命中被拒后没有重新转换 PDF"


# ===========================================================================
# R176 · D2-3 · 命中路径「轮次自增 + 审核留痕」合并为一个事务
# ===========================================================================


async def _prep_for_hit(scheme_id, body, db, cancel_event=None, defer_audit=False) -> dict:
    """命中路径所需的最小 prep（不触发任何渲染/取数）。"""
    return {
        "scheme": {"id": scheme_id, "name": "方案甲", "project_id": "p1"},
        "config": {},
        "sections": [],
        "fe_codes": [],
        "fe_images": {},
        "chart_fp": [],
        "global_facts": [],
        "appendix_sources": [],
        "global_facts_status": {"ok": True},
        "image_config": {},
        "content_audit": {},
        "render_stats": {},
    }


async def _seed_hit_row(db_conn, tmp_path) -> str:
    """方案 + 一条指向真实可用 DOCX 的缓存行。"""
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("d23s", "p1", "方案甲"))
    docx = _make_docx(tmp_path / "cached.docx")
    await db_conn.execute(
        "INSERT INTO export_cache (id, scheme_id, config_hash, content_fingerprint," " result_path) VALUES (?,?,?,?,?)",
        ("cache1", "d23s", "cfg", "content", str(docx)),
    )
    await db_conn.commit()
    return str(docx)


def _patch_hit(monkeypatch) -> None:
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(E, "_prepare_export", _prep_for_hit)
    monkeypatch.setattr(E, "_content_fingerprint", lambda prep: ("cfg", "content"))
    monkeypatch.setattr(E, "_guard_numbering_consistency", _noop)


@pytest.mark.asyncio
async def test_hit_path_commits_round_and_trace_together(db_conn, tmp_path, monkeypatch):
    """正常命中：round 恰好 +1，且同一事务落下一条留痕。"""
    await _seed_hit_row(db_conn, tmp_path)
    _patch_hit(monkeypatch)

    resp = await E.export_docx("d23s", {}, db_conn)
    assert resp.headers["X-Cache-Status"] == "hit"

    cur = await db_conn.execute("SELECT export_round FROM schemes WHERE id='d23s'")
    assert (await cur.fetchone())[0] == 1

    cur = await db_conn.execute("SELECT comment FROM review_records WHERE scheme_id='d23s'")
    rows = await cur.fetchall()
    assert len(rows) == 1, "命中路径没有落审核留痕（审计链断裂）"
    assert "第 1 轮" in rows[0][0]


@pytest.mark.asyncio
async def test_hit_path_write_failure_rolls_back_both(db_conn, tmp_path, monkeypatch):
    """留痕写入失败 → 轮次自增一并回滚，且**不交付**缓存产物（503）。"""
    from fastapi import HTTPException

    await _seed_hit_row(db_conn, tmp_path)
    _patch_hit(monkeypatch)

    async def _boom(*_a, **_k):
        raise RuntimeError("模拟留痕写入失败")

    monkeypatch.setattr(E, "_write_export_review_trace", _boom)

    with pytest.raises(HTTPException) as ei:
        await E.export_docx("d23s", {}, db_conn)
    assert ei.value.status_code == 503, "写失败仍假报命中成功，审计契约被破坏"

    # UPDATE 已执行、随后整体回滚 → round 必须回到 0（全成或全不成）
    cur = await db_conn.execute("SELECT export_round FROM schemes WHERE id='d23s'")
    assert (await cur.fetchone())[0] == 0
    cur = await db_conn.execute("SELECT COUNT(*) FROM review_records WHERE scheme_id='d23s'")
    assert (await cur.fetchone())[0] == 0


def test_hit_branches_use_single_transaction_helper():
    """两个命中分支都只能经 ``_bump_export_round_and_trace`` 写库（一次提交）。"""
    for fn in (E.export_docx, E.export_pdf):
        src = inspect.getsource(fn)
        assert "_bump_export_round_and_trace(" in src
        # 命中分支若仍直接调留痕函数，就退化为两次独立提交
        assert "_record_export_review_trace(" not in src


def test_merge_helper_commits_once_and_rolls_back_on_failure():
    src = inspect.getsource(E._bump_export_round_and_trace)
    assert src.count("await db.commit()") == 1, "轮次与留痕必须共用一次 commit"
    assert "await db.rollback()" in src
    assert "return None" in src
