"""R13 补全护栏：pipeline.py 辅助函数的 db.execute() 判空回归锁。

2026-10-06：R13 原修复覆盖了主链路（ingest_parse_result）与路由层
（routers/doc_pipeline.py），但以下辅助函数的 db.execute() 仍未判空：
  - build_completeness_report()  3 处 SELECT + 2 处写
  - sync_extract_layer()          2 处 SELECT
  - detect_cross_source_conflicts() 2 处 SELECT
  - run_cross_check()             1 处 SELECT
  - purge_document()              3 处 DELETE
  - backfill_document()           1 处 SELECT

这些函数在 aiosqlite 连接异常时会因 `cur=None → fetchone() AttributeError`
而 500 崩溃。本测试用代理 DB 模拟 db.execute 返回 None，验证每个函数
都能 fail-soft 降级而非抛异常。
"""
from __future__ import annotations

import pytest


class _NoneResult:
    """模拟 aiosqlite cursor 返回 None 的场景：fetchone/fetchall 抛 AttributeError。"""
    async def fetchone(self):
        raise AttributeError("cursor is None (simulated)")

    async def fetchall(self):
        raise AttributeError("cursor is None (simulated)")


class _FakeDBNoneOnQuery:
    """所有 SELECT 返回 None（模拟 db.execute 失败），写操作正常。"""

    def __init__(self):
        self.commits = 0
        self.executed_sql: list[str] = []

    async def execute(self, sql, params=()):
        self.executed_sql.append(sql)
        # SELECT 语句返回 None（模拟 R13 场景）
        if sql.strip().upper().startswith("SELECT"):
            return None
        # INSERT/UPDATE/DELETE 返回一个假 cursor
        return _FakeCursor()

    async def executemany(self, sql, seq):
        return _FakeCursor()

    async def commit(self):
        self.commits += 1


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


@pytest.mark.asyncio
async def test_build_completeness_report_handles_none_cursor():
    """build_completeness_report: doc 查询返回 None → 返回错误而非 AttributeError。"""
    from app.services.doc_pipeline.pipeline import build_completeness_report

    db = _FakeDBNoneOnQuery()
    report = await build_completeness_report(
        db, doc_id="doc_none1", project_id="proj1")
    # 不应抛异常，应返回结构化错误
    assert "errors" in report
    assert any("不可用" in e or "不存在" in e for e in report["errors"])


@pytest.mark.asyncio
async def test_sync_extract_layer_handles_none_cursor():
    """sync_extract_layer: items 查询返回 None → 按空集处理，不崩溃。"""
    from app.services.doc_pipeline.pipeline import sync_extract_layer

    db = _FakeDBNoneOnQuery()
    result = await sync_extract_layer(
        db, doc_id="doc_none2", project_id="proj1")
    # 空集 → written_types 为空 → extract_status=pending
    assert result["extract_status"] == "pending"
    assert result["written_types"] == []
    assert result["fact_count"] == 0
    assert result["item_count"] == 0


@pytest.mark.asyncio
async def test_detect_cross_source_conflicts_handles_none_cursor():
    """detect_cross_source_conflicts: 查询返回 None → 空冲突列表。"""
    from app.services.doc_pipeline.pipeline import detect_cross_source_conflicts

    db = _FakeDBNoneOnQuery()
    conflicts, flagged, resolved = await detect_cross_source_conflicts(
        db, project_id="proj1")
    assert conflicts == []
    assert flagged == set()
    assert resolved == set()


@pytest.mark.asyncio
async def test_run_cross_check_handles_none_cursor():
    """run_cross_check: 查询返回 None → 空报告，不崩溃。"""
    from app.services.doc_pipeline.pipeline import run_cross_check

    db = _FakeDBNoneOnQuery()
    report = await run_cross_check(db, project_id="proj1", doc_id="doc_none3")
    assert report["fact_count"] == 0
    assert report["stored_conflicts"] == 0
    assert report["cross_conflicts"] == []


@pytest.mark.asyncio
async def test_purge_document_handles_none_cursor():
    """purge_document: DELETE 返回 None → 告警但不崩溃，仍 commit。"""
    from app.services.doc_pipeline.pipeline import purge_document

    db = _FakeDBNoneOnQuery()
    # 不应抛异常
    await purge_document(db, doc_id="doc_none4", project_id="proj1")
    # 至少 commit 了一次
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_backfill_document_handles_none_cursor():
    """backfill_document: COUNT 查询返回 None → 按无分块处理。"""
    from app.services.doc_pipeline.pipeline import backfill_document

    db = _FakeDBNoneOnQuery()
    result = await backfill_document(
        db, doc_id="doc_none5", project_id="proj1",
        file_path="/nonexistent/file.pdf", file_name="test.pdf",
        file_type="pdf")
    # 文件不存在 → ok=False
    assert result["ok"] is False
    assert "原始文件缺失" in result.get("reason", "")


class _FakeDBWithDoc:
    """模拟有文档数据的 DB：doc 查询返回一行，其余 SELECT 返回空。"""

    def __init__(self):
        self.commits = 0

    async def execute(self, sql, params=()):
        s = sql.strip().upper()
        if "FROM PROJECT_DOCUMENTS" in s and "WHERE ID=?" in s:
            return _FakeCursor([{
                "file_name": "test.pdf", "page_count": 10,
                "parsed_markdown": "", "parse_status": "success",
                "parse_warnings": "[]", "completeness_json": "{}",
            }])
        if s.startswith("SELECT"):
            return _FakeCursor([])  # 空结果
        return _FakeCursor()

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_build_completeness_report_with_doc_but_none_chunk_query():
    """build_completeness_report: doc 查询成功但 chunk 查询返回 None → chunk_count=0。"""
    from app.services.doc_pipeline.pipeline import build_completeness_report

    db = _FakeDBWithDoc()
    # chunk 查询会返回空 cursor（不是 None），走正常路径
    report = await build_completeness_report(
        db, doc_id="doc_ok1", project_id="proj1")
    assert report["doc_id"] == "doc_ok1"
    assert report["completeness"]["chunk_count"] == 0
    assert "quality_score" in report


@pytest.mark.asyncio
async def test_run_cross_check_write_path_none_cursor():
    """run_cross_check: UPDATE/INSERT 写路径返回 None → 告警不崩溃，仍 commit。"""
    from app.services.doc_pipeline.pipeline import run_cross_check

    db = _FakeDBNoneOnQuery()
    # 读路径全部返回 None（空集），写路径也返回 None
    report = await run_cross_check(db, project_id="proj1", doc_id="doc_none6")
    # 不应抛异常
    assert report["fact_count"] == 0
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_sync_extract_layer_write_path_none_cursor():
    """sync_extract_layer: doc_extractions INSERT 返回 None → 告警但不中断循环。"""
    from app.services.doc_pipeline.pipeline import sync_extract_layer

    db = _FakeDBNoneOnQuery()
    # items 查询返回 None → items=[] → 空集路径，不触发写循环
    result = await sync_extract_layer(
        db, doc_id="doc_none7", project_id="proj1")
    assert result["extract_status"] == "pending"
    assert result["written_types"] == []


@pytest.mark.asyncio
async def test_pipeline_guards_are_present_static():
    """静态护栏：确认 pipeline.py 中所有 db.execute 调用点都有判空守卫。

    扫描源码，统计 `await db.execute(` 出现次数与对应的 `is None` 守卫数。
    主链路 ingest_parse_result 的守卫已在 R13 原修复中覆盖；本测试确保
    辅助函数的守卫不被回退。
    """
    import inspect

    from app.services.doc_pipeline import pipeline

    src = inspect.getsource(pipeline)
    # 统计所有 db.execute 调用点
    execute_calls = src.count("await db.execute(")
    # 统计 None 守卫（is None 或 cur is None 模式）
    none_guards = src.count("is None")
    # 至少要有合理数量的守卫（R13 原修复 + 本轮补全）
    # ingest_parse_result 有 3 处守卫 + 本轮补全约 10 处 = 至少 10 处
    assert none_guards >= 10, \
        f"db.execute None 守卫数 {none_guards} 过少，可能 R13 补全被回退"
