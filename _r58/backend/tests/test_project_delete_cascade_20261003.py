# -*- coding: utf-8 -*-
"""删除项目的级联清理护栏（2026-10-03 · 解析提取模块数据泄漏收口）

背景（生产事实，非推测）
------------------------
`projects.delete_project` 此前只清理「方案级」与少数「项目级」表，
**解析提取域的两张主表**（`bid_analysis_items` / `bid_sections`）、
**四层存储的三张派生表**（`doc_chunks` / `doc_extractions` /
`doc_validation_reports`）以及 `consistency_scan_cache` 全部漏删，
且 `doc_storage` 的四层目录树（`data/projects/{pid}/`）无人清理。

后果：删除项目后这些表仍保留已删项目的数据 —— `/bid-analysis/results`
会返回已删项目的提取结果（下游据此"复活"旧项目），一致性扫描缓存
会命中早已不存在的章节，四层磁盘产物永久残留占空间。

三条护栏（新增判据前先查是否已有唯一实现，见 AGENTS.md §5.14）
--------------------------------------------------------------------
1. **端到端**：造一个带完整派生数据的项目，删除后逐表断言清零；
2. **磁盘**：四层目录树随项目删除一起清理，且路径守卫不误删管理层外文件；
3. **parity 静态锁**：遍历 `schema_sql` 里所有含 `project_id` / `scheme_id`
   列的表，断言它们都在 delete_project 的清理登记表内 —— 这才是「下次再漏一张表」
   的真正防线（登记表逐张写死会让后人漏一张而无人发现）。

fail-soft 语义：任何一张派生表清理失败都不允许让项目删除失败
（用户删不掉自己项目是死锁），失败表名记入返回值 `cleanup_errors`。
"""
from __future__ import annotations

import re
import sqlite3

import pytest

# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

async def _mk(db, name: str = "测试项目", pid: str = "") -> str:
    """建项目并返回 id（只填 NOT NULL 列）。"""
    import uuid
    pid = pid or str(uuid.uuid4())
    await db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (pid, name))
    await db.commit()
    return pid


async def _mk_scheme(db, pid: str) -> str:
    import uuid
    sid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)",
        (sid, pid, "测试方案"))
    await db.commit()
    return sid


async def _mk_doc(db, pid: str) -> str:
    """造一条 project_documents 记录（含 doc_id，供派生表挂载）。"""
    import uuid
    did = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO project_documents (id, project_id, file_name, file_type)"
        " VALUES (?, ?, ?, ?)", (did, pid, "样本.pdf", "pdf"))
    await db.commit()
    return did


async def _seed_all_derived(db, pid: str, sid: str, did: str) -> dict:
    """把 delete_project 需要清理的每一张派生表都塞一行，用于逐表清零断言。

    新增派生表时在此补一行即可。末尾另造一个「另一项目」的同表数据，
    供防误删断言使用。
    """
    seed: dict = {}
    await db.execute(
        "INSERT INTO bid_analysis_items (id, project_id, item_id, status, content)"
        " VALUES (?,?,?,?,?)",
        (f"{pid}_projectBasicInfo", pid, "projectBasicInfo", "success", "提取内容"))
    seed["bid_analysis_items"] = 1
    await db.execute(
        "INSERT INTO bid_sections (id, project_id, scheme_id, is_multi, status)"
        " VALUES (?,?,?,?,?)", (f"{pid}_section", pid, sid, 1, "success"))
    seed["bid_sections"] = 1
    await db.execute(
        "INSERT INTO doc_extractions"
        " (extraction_id, doc_id, project_id, extract_type, extract_data)"
        " VALUES (?,?,?,?,?)",
        ("ext-1", did, pid, "project_info", "{}"))
    seed["doc_extractions"] = 1
    await db.execute(
        "INSERT INTO doc_validation_reports"
        " (id, doc_id, project_id, kind, report_json) VALUES (?,?,?,?,?)",
        ("rep-1", did, pid, "cross_check", "{}"))
    seed["doc_validation_reports"] = 1
    await db.execute(
        "INSERT INTO export_presets (id, project_id, name, config_json)"
        " VALUES (?,?,?,?)", ("preset-1", pid, "默认", "{}"))
    seed["export_presets"] = 1
    await db.execute(
        "INSERT INTO knowledge_base (id, project_id, scheme_id, name, content)"
        " VALUES (?,?,?,?,?)", ("kb-1", pid, sid, "知识条目", "内容"))
    seed["knowledge_base"] = 1
    # doc_id 级（只有 doc_id，无 project_id）
    await db.execute(
        "INSERT INTO doc_chunks (chunk_id, doc_id, chunk_type, text)"
        " VALUES (?,?,?,?)", ("ck-1", did, "section", "分块文本"))
    seed["doc_chunks"] = 1
    # 方案级（按 scheme_id 清理）
    await db.execute(
        "INSERT INTO consistency_scan_cache"
        " (section_id, scheme_id, content_hash, context_hash, rows_json)"
        " VALUES (?,?,?,?,?)", ("sec-1", sid, "h1", "c1", "[]"))
    seed["consistency_scan_cache"] = 1
    await db.execute(
        "INSERT INTO chart_predictions"
        " (id, scheme_id, section_id, chart_type) VALUES (?,?,?,?)",
        ("chart-1", sid, "sec-1", "flowchart"))
    seed["chart_predictions"] = 1
    await db.execute(
        "INSERT INTO consistency_conflicts"
        " (id, scheme_id, scan_id, conflict_type, severity, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("cc-1", sid, "scan-1", "numeric", "medium", "2026-10-03 00:00:00"))
    seed["consistency_conflicts"] = 1
    await db.execute(
        "INSERT INTO placeholder_baselines (id, scheme_id)"
        " VALUES (?,?)", ("pb-1", sid))
    seed["placeholder_baselines"] = 1

    # 另一项目的同表数据（防误删断言）
    other_pid = await _mk(db, name="另一个项目")
    await db.execute(
        "INSERT INTO bid_analysis_items (id, project_id, item_id, status, content)"
        " VALUES (?,?,?,?,?)", (f"{other_pid}_x", other_pid, "x", "success", "x"))
    other_sid = await _mk_scheme(db, other_pid)
    await db.execute(
        "INSERT INTO consistency_scan_cache"
        " (section_id, scheme_id, content_hash, context_hash, rows_json)"
        " VALUES (?,?,?,?,?)", ("sec-2", other_sid, "h2", "c2", "[]"))
    seed["_other_pid"] = other_pid
    seed["_other_sid"] = other_sid
    await db.commit()
    return seed


async def _count(db, table: str, col: str, val: str) -> int:
    cur = await db.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {col}=?", (val,))
    row = await cur.fetchone()
    return int(row["n"] if row else 0)


# ---------------------------------------------------------------------------
# 1. 端到端级联清理
# ---------------------------------------------------------------------------

async def test_delete_project_cleans_every_derived_table(db_conn):
    """删除项目后，所有带 project_id / scheme_id / doc_id 的派生表必须清零。"""
    from app.routers.projects import delete_project

    pid = await _mk(db_conn)
    sid = await _mk_scheme(db_conn, pid)
    did = await _mk_doc(db_conn, pid)
    await _seed_all_derived(db_conn, pid, sid, did)

    result = await delete_project(pid, db=db_conn)
    assert result["ok"] is True
    assert not result.get("cleanup_errors"), \
        f"清理不应有失败项，实际: {result.get('cleanup_errors')}"

    # 项目本身与方案、文档档案已删
    assert await _count(db_conn, "projects", "id", pid) == 0
    assert await _count(db_conn, "schemes", "project_id", pid) == 0
    assert await _count(db_conn, "project_documents", "project_id", pid) == 0

    # 项目级表（本轮此前全部漏删）
    for table in ("bid_analysis_items", "bid_sections", "doc_extractions",
                  "doc_validation_reports", "export_presets", "knowledge_base"):
        assert await _count(db_conn, table, "project_id", pid) == 0, \
            f"{table} 仍残留已删项目的行"

    # doc_id 级派生表（只有 doc_id 列，必须靠 doc_id 定向清理）
    assert await _count(db_conn, "doc_chunks", "doc_id", did) == 0, \
        "doc_chunks 仍残留已删项目的分块"
    assert await _count(db_conn, "doc_extractions", "doc_id", did) == 0
    assert await _count(db_conn, "doc_validation_reports", "doc_id", did) == 0

    # 方案级表（consistency_scan_cache 曾长期漏删）
    for table in ("consistency_scan_cache", "chart_predictions",
                  "consistency_conflicts"):
        assert await _count(db_conn, table, "scheme_id", sid) == 0, \
            f"{table} 仍残留已删方案的行"
    assert await _count(db_conn, "placeholder_baselines", "scheme_id", sid) == 0


async def test_delete_project_does_not_touch_other_projects(db_conn):
    """清理必须严格限定在本项目：同表里其它项目的数据一行都不能动。"""
    from app.routers.projects import delete_project

    pid = await _mk(db_conn, name="目标")
    sid = await _mk_scheme(db_conn, pid)
    seed = await _seed_all_derived(db_conn, pid, sid, await _mk_doc(db_conn, pid))

    await delete_project(pid, db=db_conn)

    other_pid = seed["_other_pid"]
    assert await _count(db_conn, "projects", "id", other_pid) == 1
    assert await _count(db_conn, "bid_analysis_items", "project_id",
                        other_pid) == 1
    assert await _count(db_conn, "consistency_scan_cache", "scheme_id",
                        seed["_other_sid"]) == 1


async def test_delete_project_missing_returns_404(db_conn):
    from app.routers.projects import delete_project
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await delete_project("不存在的项目", db=db_conn)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# 2. 四层存储磁盘清理
# ---------------------------------------------------------------------------

async def test_delete_project_removes_four_layer_disk_tree(
        db_conn, tmp_path, monkeypatch):
    """四层目录树（data/projects/{pid}/）必须随项目删除一起清理。"""
    from app.routers.projects import delete_project
    from app.services.doc_pipeline import doc_storage as store

    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
    pid = await _mk(db_conn, pid="pid-disk")

    d = store.doc_dir(pid, "doc-1")
    for layer in store.ALL_LAYERS:
        (store.layer_dir(pid, "doc-1", layer)).mkdir(parents=True, exist_ok=True)
        (store.layer_dir(pid, "doc-1", layer) / f"doc-1_{layer}.md").write_text(
            "x", encoding="utf-8")
    store.index_path(pid).parent.mkdir(parents=True, exist_ok=True)
    store.index_path(pid).write_text("[]", encoding="utf-8")
    assert store.index_path(pid).exists()
    assert d.exists()

    result = await delete_project(pid, db=db_conn)
    assert result.get("doc_trees_removed") is True
    # 整棵项目文档根目录消失（含 documents/ 与 documents_index.json）
    assert not (tmp_path / "projects" / "pid-disk").exists()


async def test_delete_project_disk_failure_does_not_block_delete(
        db_conn, tmp_path, monkeypatch, caplog):
    """磁盘清理失败只降级告警，不允许阻断项目删除（用户删不掉项目是死锁）。"""
    from app.routers.projects import delete_project
    from app.services.doc_pipeline import doc_storage as store

    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
    pid = await _mk(db_conn, pid="pid-disk-2")

    def _raise(_pid):
        raise OSError("模拟磁盘权限不足")

    monkeypatch.setattr(store, "delete_project_docs_root", _raise)
    caplog.set_level("WARNING", logger="projects")

    result = await delete_project(pid, db=db_conn)
    assert result["ok"] is True
    assert await _count(db_conn, "projects", "id", pid) == 0
    assert any("四层存储" in r.message for r in caplog.records)


def test_delete_project_docs_root_rejects_path_traversal(tmp_path, monkeypatch):
    """路径守卫：../ 或绝对路径不得绕过词法检查误删管理层外文件。"""
    from app.services.doc_pipeline import doc_storage as store

    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
    victim = tmp_path / "outside" / "keep.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("必须保留", encoding="utf-8")

    for bad in ("..", "../outside", "/etc", "a" * 300 + "/../outside"):
        assert store.delete_project_docs_root(bad) is False, f"{bad} 未被拦截"
    assert victim.read_text(encoding="utf-8") == "必须保留"


def test_delete_project_docs_root_is_idempotent(tmp_path, monkeypatch):
    """目录不存在时返回 False（幂等，不视为失败）。"""
    from app.services.doc_pipeline import doc_storage as store
    monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
    assert store.delete_project_docs_root("从未存在过") is False


# ---------------------------------------------------------------------------
# 3. parity 静态锁：schema 里所有派生表都必须在清理登记表内
# ---------------------------------------------------------------------------

#: 按 project_id 清理的表，同时带 scheme_id —— 删项目已覆盖其方案级行，
#: 不必再进 _SCHEME_SCOPED_TABLES（否则 DELETE 会重复执行同一批行）。
_PROJECT_SCOPED_ALSO_HAS_SCHEME_ID = {
    "bid_analysis_items", "bid_sections", "uploaded_outlines",
    "global_facts", "knowledge_base",
}

#: 外键 ON DELETE CASCADE 已覆盖的表（不属于「孤儿残留」范畴）。
#: ⚠️ 这个豁免清单必须显式留痕 —— 隐式豁免会让后人加一张带 project_id /
#: scheme_id 的表而绕过本护栏（见 AGENTS.md §5.14「护栏判据选错锚点比不写护栏更糟」）。
_CASCADE_COVERED = {
    "projects",             # 主表自身
    "project_documents",    # REFERENCES projects(id) ON DELETE CASCADE
    "placeholder_baselines",  # REFERENCES schemes(id) ON DELETE CASCADE
}

#: 全局审计日志，按设计跨项目保留（不是项目级派生数据）。
_AUDIT_LOGS = {"ai_audit_logs", "ai_config_audit_logs", "prompt_audit_logs"}


def _cleaned_tables(P) -> set:
    """delete_project 实际会清理到的表（三套登记表 + 豁免并集）。"""
    out = set(P._PROJECT_SCOPED_TABLES) | set(P._SCHEME_SCOPED_TABLES)
    out |= set(P._PROJECT_SCOPED_TABLES)  # 同集，保留显式意图
    out |= {"doc_chunks", "doc_extractions", "doc_validation_reports"}
    return out


def _tables_with_column(schema_sql: str, col: str) -> set:
    """从建表 SQL 里找出含指定列的所有表名。"""
    out = set()
    for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;",
            schema_sql, re.S | re.I):
        name, body = m.group(1), m.group(2)
        for line in body.splitlines():
            line = line.split("--")[0].strip()
            if re.match(rf"^{col}\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC)", line, re.I):
                out.add(name)
                break
    return out


def _migrate_tables_with_column(col: str) -> set:
    """_migrate 补列（旧库 CREATE TABLE 被 IF NOT EXISTS 跳过）里的表。

    schema_sql 里没有这些列、只在 db.py::_migrate 的 migrations 三元组里声明，
    若只看 schema_sql 会漏掉它们 —— 这正是「登记表漏一张表」的典型盲区。
    """
    import inspect

    import app.db as _db
    src = inspect.getsource(_db)
    return set(re.findall(r'"\s*(\w+)"\s*,\s*"' + col + r'"\s*,', src))


def test_all_project_id_tables_are_cleaned_by_delete_project():
    """schema + 迁移里每张带 project_id 的表都必须被 delete_project 清理。

    这是本轮漏删的真正防线：登记表逐张写死，后人新增一张带 project_id 的表
    而忘了登记，本用例会立刻失败。
    （本用例已实际抓出 `uploaded_outlines` 长期漏删。）
    """
    from app.routers import projects as P
    from app.schema_sql import SCHEMA_SQL

    tables = _tables_with_column(SCHEMA_SQL, "project_id") | \
        _migrate_tables_with_column("project_id")
    missing = tables - _cleaned_tables(P) - _CASCADE_COVERED - _AUDIT_LOGS
    assert not missing, (
        f"以下带 project_id 的表未被 delete_project 清理，删项目后会残留孤儿行: "
        f"{sorted(missing)}")


def test_all_scheme_id_tables_are_cleaned_by_delete_project():
    """schema + 迁移里每张带 scheme_id 的表都必须被 delete_project 清理。"""
    from app.routers import projects as P
    from app.schema_sql import SCHEMA_SQL

    tables = _tables_with_column(SCHEMA_SQL, "scheme_id") | \
        _migrate_tables_with_column("scheme_id")
    missing = tables - _cleaned_tables(P) - _CASCADE_COVERED \
        - _PROJECT_SCOPED_ALSO_HAS_SCHEME_ID - _AUDIT_LOGS
    assert not missing, (
        f"以下带 scheme_id 的表未被 delete_project 清理，删项目后会残留孤儿行: "
        f"{sorted(missing)}")


def test_scheme_scoped_tables_include_consistency_scan_cache():
    """consistency_scan_cache 曾长期漏删，此用例锁定它不得再次脱落。"""
    from app.routers import projects as P
    assert "consistency_scan_cache" in P._SCHEME_SCOPED_TABLES


def test_project_scoped_tables_cover_extraction_domain():
    """解析提取域与目录上传的两张表必须在项目级登记表内（本轮的核心泄漏点）。"""
    from app.routers import projects as P
    assert "bid_analysis_items" in P._PROJECT_SCOPED_TABLES
    assert "bid_sections" in P._PROJECT_SCOPED_TABLES
    assert "uploaded_outlines" in P._PROJECT_SCOPED_TABLES


def test_schemes_deleted_explicitly_not_only_by_cascade():
    """schemes 必须显式删除，不能只依赖 ON DELETE CASCADE。

    理由：测试连接与部分历史库未开 PRAGMA foreign_keys=ON，
    届时级联不生效 —— 方案行会残留，而所有按 scheme_id 清理的派生表
    （consistency_scan_cache / chart_predictions 等）也会跟着残留。
    """
    from app.routers import projects as P
    assert "schemes" in P._PROJECT_SCOPED_TABLES


# ---------------------------------------------------------------------------
# 4. fail-soft：单表失败不阻断删除
# ---------------------------------------------------------------------------

async def test_single_table_failure_does_not_block_project_delete(db_conn):
    """某张派生表清理失败（如旧库缺表）时，项目仍必须删成功，失败表名留痕。"""
    from app.routers import projects as P

    pid = await _mk(db_conn, pid="pid-fs")
    real = db_conn.execute

    async def _patched_execute(sql, params=None):
        if isinstance(sql, str) and "bid_analysis_items" in sql:
            raise sqlite3.OperationalError("no such table: bid_analysis_items")
        return await real(sql, params)

    db_conn.execute = _patched_execute
    try:
        result = await P.delete_project(pid, db=db_conn)
    finally:
        db_conn.execute = real

    assert result["ok"] is True
    assert await _count(db_conn, "projects", "id", pid) == 0
    assert "bid_analysis_items" in result.get("cleanup_errors", [])


async def test_deleted_project_cannot_be_deleted_twice(db_conn):
    """重复删除同一项目返回 404（幂等语义：第二次视为不存在）。"""
    from app.routers.projects import delete_project
    from fastapi import HTTPException

    pid = await _mk(db_conn, pid="pid-twice")
    assert (await delete_project(pid, db=db_conn))["ok"] is True
    with pytest.raises(HTTPException) as ei:
        await delete_project(pid, db=db_conn)
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# 5. 导出缓存磁盘清理（2026-10-03 · PDF 漏清收口）
# ---------------------------------------------------------------------------

async def test_delete_project_cleans_docx_and_pdf_export_cache_files(
        db_conn, tmp_path, monkeypatch):
    """删除方案时，EXPORTS_DIR 下 `{scheme_id}_*.docx` 与 `{scheme_id}_*.pdf`
    导出缓存文件都应被清理；**其它项目**的方案文件不得误删（前缀隔离）。

    ✅ 修复点：旧实现只 glob `.docx`，PDF 产物（export.py 4809 的 out_path
    同样落在 EXPORTS_DIR，命名 `{scheme_id}_*.pdf`）删除方案后永久孤儿。
    """
    import app.config as cfg
    import app.routers.projects as P
    monkeypatch.setattr(cfg, "EXPORTS_DIR", tmp_path)

    pid = await _mk(db_conn, pid="pid-exp")
    sid = await _mk_scheme(db_conn, pid)

    # 其它项目（保留）的方案，用于验证前缀隔离不误删
    keep_pid = await _mk(db_conn, name="保留项目", pid="pid-keep")
    keep_sid = await _mk_scheme(db_conn, keep_pid)

    (tmp_path / f"{sid}_abc123_def456.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / f"{sid}_abc123_def456.pdf").write_bytes(b"%PDF")
    (tmp_path / f"{keep_sid}_keep000_keep111.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / "unrelated.txt").write_bytes(b"x")

    result = await P.delete_project(pid, db=db_conn)
    assert result["ok"] is True

    assert not (tmp_path / f"{sid}_abc123_def456.docx").exists(), \
        "本方案 docx 缓存未清理"
    assert not (tmp_path / f"{sid}_abc123_def456.pdf").exists(), \
        "本方案 pdf 缓存未清理（修复点）"
    assert (tmp_path / f"{keep_sid}_keep000_keep111.docx").exists(), \
        "其它项目的导出缓存被误删"
    assert (tmp_path / "unrelated.txt").exists(), \
        "EXPORTS_DIR 内的无关文件被误删"

