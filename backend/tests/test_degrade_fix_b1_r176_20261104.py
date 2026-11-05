"""B1 修复执行记录（R176 · 2026-11-04）的回归护栏。

覆盖三项：
  · B1-1 导出预检 fail-fast（DB 读失败不得再返回「0 问题」假通过）；
  · B1-2 全局事实查询失败 → 不写导出缓存 + 响应头留痕；
  · B1-3 缓存完整性：截断 PDF 不得命中，写入前同样复核。
"""

import inspect
import json

import app.routers.export as E
import pytest


# ---------------------------------------------------------------------------
# B1-1 · /export/check 读失败 → 503（不再用空集冒充「预检通过」）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_check_returns_503_when_read_fails(monkeypatch):
    async def _boom(scheme_id, db, *, strict_read=False):
        assert strict_read is True, "预检必须走严格读（fail-fast），否则读失败会降级为空集"
        raise E.DBReadError("db.execute 返回 None（collect_export_issues:sections）")

    monkeypatch.setattr(E, "collect_export_issues", _boom)
    resp = await E.export_check("s1", db=object())
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["read_degraded"] is True
    assert body["error"] == "export_check_read_failed"
    assert body["detail"]
    assert resp.headers.get("Retry-After") == "5"


@pytest.mark.asyncio
async def test_export_check_success_marks_read_degraded_false(db_conn, monkeypatch):
    async def _ok(scheme_id, db, *, strict_read=False):
        return {"issues": [], "section_count": 0, "global_facts_status": {"ok": True, "code": "ok"}}

    monkeypatch.setattr(E, "collect_export_issues", _ok)
    result = await E.export_check("s1", db=db_conn)
    assert result["read_degraded"] is False


# ---------------------------------------------------------------------------
# B1-2 · 全局事实查询失败不得写缓存（DOCX / PDF 同口径）
# ---------------------------------------------------------------------------
def test_facts_query_failure_blocks_cache_write_both_chains():
    for fn in (E.export_docx, E.export_pdf):
        src = inspect.getsource(fn)
        assert "_facts_query_failed" in src, f"{fn.__name__} 未把「事实查询失败」纳入缓存写入门控"
        assert '"query_failed"' in src, f"{fn.__name__} 未按 global_facts_status.code=query_failed 判定降级"
    docx_src = inspect.getsource(E.export_docx)
    assert "global-facts-query-failed" in docx_src, "DOCX 降级响应未回传 X-Export-Degraded: global-facts-query-failed"
    pdf_src = inspect.getsource(E.export_pdf)
    assert "global-facts-query-failed" in pdf_src, "PDF 降级响应未回传 X-Export-Degraded: global-facts-query-failed"


# ---------------------------------------------------------------------------
# B1-3 · PDF 完整性判据（与 DOCX 的 ZIP 判据同型）
# ---------------------------------------------------------------------------
def test_viable_rejects_truncated_pdf(tmp_path):
    p = tmp_path / "truncated.pdf"
    # size>0 但缺 %%EOF（写入中断/磁盘满的典型形态）
    p.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n")
    assert E._cache_row_viable(str(p)) is False


def test_viable_rejects_pdf_without_header(tmp_path):
    p = tmp_path / "no_header.pdf"
    p.write_bytes(b"\x00" * 512 + b"%%EOF")
    assert E._cache_row_viable(str(p)) is False


def test_viable_accepts_minimal_valid_pdf(tmp_path):
    p = tmp_path / "ok.pdf"
    p.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    assert E._cache_row_viable(str(p)) is True


def test_viable_treats_unknown_suffix_as_before(tmp_path):
    """非 docx/pdf 后缀保持旧行为（size>0 即可用），避免影响既有产物类型。"""
    p = tmp_path / "x.bin"
    p.write_bytes(b"anything")
    assert E._cache_row_viable(str(p)) is True


def test_write_paths_recheck_integrity_before_persist():
    for fn in (E.export_docx, E.export_pdf):
        src = inspect.getsource(fn)
        assert (
            "_cache_row_viable(str(out_path))" in src
        ), f"{fn.__name__} 写入缓存前未复核产物完整性（坏缓存仍可能落库）"


# ---------------------------------------------------------------------------
# B1-4 · 迁移失败显式「降级模式」标志 + 健康检查消费
# ---------------------------------------------------------------------------
def test_migration_failure_sets_degraded_flag():
    import app.db as dbmod

    dbmod._MIGRATION_DEGRADED.clear()
    dbmod._record_migration_degraded("schemes.old_col", RuntimeError("disk I/O error"))
    st = dbmod.migration_status()
    assert st["degraded"] is True
    assert st["count"] == 1
    assert st["items"][0]["scope"] == "schemes.old_col"


def test_migration_missing_table_is_not_degraded():
    """新库/半初始化环境的「no such table」是幂等跳过，不得误标降级。"""
    import app.db as dbmod

    dbmod._MIGRATION_DEGRADED.clear()
    dbmod._record_migration_degraded("prompt_audit_logs", RuntimeError("no such table: prompt_audit_logs"))
    assert dbmod.migration_status()["degraded"] is False


def test_migration_duplicate_column_is_not_degraded():
    """列已存在（并发/前次迁移已补上）同样幂等跳过，不得误标降级。"""
    import app.db as dbmod

    dbmod._MIGRATION_DEGRADED.clear()
    dbmod._record_migration_degraded("schemes.new_col", RuntimeError("duplicate column name: new_col"))
    assert dbmod.migration_status()["degraded"] is False


@pytest.mark.asyncio
async def test_health_exposes_migration_degraded_mode():
    import app.db as dbmod
    from app.main import health

    dbmod._MIGRATION_DEGRADED.clear()
    assert (await health())["status"] == "ok"

    dbmod._record_migration_degraded("facts_chunk_failures 建表", RuntimeError("disk full"))
    res = await health()
    assert res["status"] == "degraded"
    assert res["migrations"]["count"] == 1
    dbmod._MIGRATION_DEGRADED.clear()
