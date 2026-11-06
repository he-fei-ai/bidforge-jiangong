"""R179 Phase E · 条款级规范库（2026-11-06）。

覆盖最小闭环的三条验收线：
1. **基表 + 导入校验**：六项必填、版权护栏、``clause_uid`` 唯一、摘录长度 CHECK；
2. **检索**：精确条文号优先，未命中走 LIKE 降级，空库 / 无命中都不得冒充「无问题」；
3. **迁移与接线**：老库（仅 projects）连跑两次迁移幂等且 ``user_version`` 不变；
   D 的「规范引用溯源附录」在方案零登记时不得列举库内条款。

另有「咬住」用例直接打 DB 约束（重复 ``clause_uid`` / 501 字摘录 / 来源缺失），
供变异验证使用：去掉 UNIQUE 或 CHECK 后对应用例必须变红。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import aiosqlite
import pytest
from app.config import Settings, settings
from app.db import _migrate
from app.routers import export_dossier, standards_admin
from app.routers import projects as projects_router
from app.services import clause_store, dossier
from docx import Document
from fastapi import HTTPException
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# 夹具与装配
# ---------------------------------------------------------------------------


def _valid_clause(**overrides) -> dict:
    row = {
        "clause_uid": "c-5-2-1",
        "standard_code": "GB 50007-2011",
        "standard_name": "建筑地基基础设计规范",
        "version": "2011",
        "clause_no": "第5.2.1条",
        "chapter": "5 地基计算",
        "clause_title": "地基承载力验算",
        "excerpt": "基础底面压力应符合设计要求。",
        "excerpt_type": "summary",
        "mandatory": 1,
        "applicability": "天然地基",
        "license_note": "仅摘录要点，禁止全文转载",
        "source_url": "https://example.com/gb50007/5.2.1",
        "page_ref": "",
        "source_kind": "public",
        "verification_status": "unverified",
    }
    row.update(overrides)
    return row


async def _seed_scheme(db, name: str = "条款库底稿") -> tuple[str, str]:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (pid, name))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)", (sid, pid, name))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, content, sort_order) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), sid, pid, "工程概况", "正文段落一", 0),
    )
    await db.commit()
    return pid, sid


async def _build_dossier(db, monkeypatch, tmp_path, sid: str, *, clause_enabled: bool | None = None) -> Path:
    monkeypatch.setattr(export_dossier.settings, "dossier_enabled", True)
    if clause_enabled is not None:
        monkeypatch.setattr(export_dossier.settings, "clause_library_enabled", clause_enabled)
    monkeypatch.setattr(export_dossier, "EXPORTS_DIR", Path(tmp_path))
    resp = await export_dossier.export_dossier(sid, db=db)
    assert isinstance(resp, FileResponse) and resp.path and Path(resp.path).exists()
    return Path(resp.path)


def _spy_clause_queries(monkeypatch) -> list[str]:
    """记录所有触达 ``standard_clauses`` 的查询（含 sqlite_master 探存）。"""
    calls: list[str] = []
    original = dossier._fetch_rows

    async def wrapped(db, sql, params=(), *, what):
        if "standard_clauses" in str(what) or "standard_clauses" in sql:
            calls.append(what)
        return await original(db, sql, params, what=what)

    monkeypatch.setattr(dossier, "_fetch_rows", wrapped)
    return calls


def _docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _find_table(path: Path, headers: list[str]) -> list[list[str]] | None:
    doc = Document(str(path))
    for table in doc.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        if rows and rows[0] == headers:
            return rows
    return None


#: 绕过服务层直插，专门咬 DDL 约束（变异验证用）。
_RAW_INSERT = (
    "INSERT INTO standard_clauses (clause_uid, standard_code, standard_code_norm,"
    " standard_base_norm, version, clause_no, clause_no_norm, excerpt, excerpt_hash,"
    " applicability, license_note, source_url, page_ref, source_kind,"
    " verification_status, status)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


async def _raw_insert(
    db,
    *,
    uid: str,
    clause_no: str = "5.2.1",
    excerpt: str = "要点",
    source_url: str = "https://example.com",
    page_ref: str = "",
    source_kind: str = "public",
) -> None:
    await db.execute(
        _RAW_INSERT,
        (
            uid,
            "GB 50007-2011",
            "GB50007-2011",
            "GB50007",
            "2011",
            clause_no,
            clause_no,
            excerpt,
            "hash",
            "天然地基",
            "许可",
            source_url,
            page_ref,
            source_kind,
            "unverified",
            "active",
        ),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. 开关与表归属
# ---------------------------------------------------------------------------


def test_clause_library_switch_defaults_to_false():
    assert Settings().clause_library_enabled is False


async def test_router_returns_404_when_disabled(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", False)
    with pytest.raises(HTTPException) as search_exc:
        await standards_admin.search_clause_library(db=db_conn)
    assert search_exc.value.status_code == 404
    with pytest.raises(HTTPException) as import_exc:
        await standards_admin.import_clause_library(standards_admin.ImportIn(clauses=[]), db=db_conn)
    assert import_exc.value.status_code == 404


def test_clause_table_is_global_not_cascade_registered():
    """条款库是全局数据：不按 project_id / scheme_id 归属，故不登记级联清理。"""
    assert "standard_clauses" not in projects_router._PROJECT_SCOPED_TABLES
    assert "standard_clauses" not in projects_router._SCHEME_SCOPED_TABLES


# ---------------------------------------------------------------------------
# 2. 导入校验拒绝矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("version", {"version": ""}),
        ("license_note", {"license_note": ""}),
        ("excerpt", {"excerpt": "基" * 501}),
        ("excerpt_type", {"excerpt_type": "full"}),
        ("applicability", {"applicability": ""}),
    ],
)
async def test_import_rejects_each_required_field(db_conn, field, override):
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(db_conn, [_valid_clause(**override)], confirm_license=True)
    assert any(err["field"] == field for err in excinfo.value.errors)


async def test_import_rejects_duplicate_clause_uid_in_batch(db_conn):
    rows = [
        _valid_clause(clause_uid="dup-uid", clause_no="5.2.1"),
        _valid_clause(clause_uid="dup-uid", clause_no="5.2.2"),
    ]
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(db_conn, rows, confirm_license=True)
    assert any(err["field"] == "clause_uid" for err in excinfo.value.errors)


async def test_import_rejects_duplicate_clause_uid_against_existing_db(db_conn):
    await _raw_insert(db_conn, uid="existing-uid")
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(
            db_conn, [_valid_clause(clause_uid="existing-uid", clause_no="5.2.2")], confirm_license=True
        )
    assert any(err["field"] == "db" and "UNIQUE" in err["message"] for err in excinfo.value.errors)


async def test_import_requires_confirm_license(db_conn):
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=False)
    assert excinfo.value.errors[0]["field"] == "confirm_license"


async def test_import_accepts_page_ref_only_internal_source(db_conn):
    row = _valid_clause(
        clause_uid="internal-1",
        source_url="",
        page_ref="内部资料 p.12",
        source_kind="internal",
    )
    result = await clause_store.import_clauses(db_conn, [row], confirm_license=True)
    assert result["accepted"] == 1
    found = await clause_store.search_clauses(db_conn, standard_code="GB 50007-2011", version="2011", clause_no="5.2.1")
    hit = found.hits[0]
    assert hit.source_url == ""
    assert hit.page_ref == "内部资料 p.12"
    assert hit.source_kind == "internal"


async def test_import_rejects_when_source_url_and_page_ref_both_empty(db_conn):
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(db_conn, [_valid_clause(source_url="", page_ref="")], confirm_license=True)
    assert any(err["field"] == "source_url" for err in excinfo.value.errors)


async def test_import_rejects_internal_source_without_page_ref(db_conn):
    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_store.import_clauses(
            db_conn,
            [_valid_clause(source_kind="internal", source_url="https://example.com/x", page_ref="")],
            confirm_license=True,
        )
    assert any(err["field"] == "page_ref" for err in excinfo.value.errors)


# --- 咬住 DDL 约束（变异时这两条必须变红） ---


async def test_ddl_unique_clause_uid(db_conn):
    await _raw_insert(db_conn, uid="same-uid", clause_no="5.2.1")
    with pytest.raises(sqlite3.IntegrityError):
        await _raw_insert(db_conn, uid="same-uid", clause_no="5.2.2")


async def test_ddl_excerpt_length_check(db_conn):
    with pytest.raises(sqlite3.IntegrityError):
        await _raw_insert(db_conn, uid="long-excerpt", excerpt="基" * 501)


async def test_ddl_requires_source_url_or_page_ref(db_conn):
    with pytest.raises(sqlite3.IntegrityError):
        await _raw_insert(db_conn, uid="no-source", source_url="", page_ref="")


async def test_ddl_internal_source_requires_page_ref(db_conn):
    with pytest.raises(sqlite3.IntegrityError):
        await _raw_insert(db_conn, uid="internal-no-page", source_url="https://example.com/x", source_kind="internal")


# ---------------------------------------------------------------------------
# 3. 检索：精确 / LIKE 降级 / 空库 / 无命中
# ---------------------------------------------------------------------------


async def test_exact_clause_lookup_returns_contract_fields(db_conn):
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    result = await clause_store.search_clauses(
        db_conn, standard_code="GB 50007-2011", version="2011", clause_no="第 5.2.1 条"
    )
    assert result.status == "ok" and result.degraded is False
    hit = result.hits[0]
    assert hit.match_type == "exact"
    assert hit.clause_uid == "c-5-2-1"
    assert hit.standard_code == "GB 50007-2011" and hit.version == "2011"
    assert hit.clause_no == "第5.2.1条"
    assert hit.excerpt and hit.source_url


async def test_like_fallback_when_exact_misses(db_conn):
    row = _valid_clause(
        clause_uid="like-1",
        clause_no="5.2.1",
        excerpt="基坑开挖应进行整体稳定性验算，防止隆起与管涌。",
    )
    await clause_store.import_clauses(db_conn, [row], confirm_license=True)
    result = await clause_store.search_clauses(
        db_conn,
        standard_code="GB 50007-2011",
        version="2011",
        clause_no="9.9.9",
        query="基坑 稳定性",
    )
    assert result.status == "ok" and result.degraded is True
    assert result.hits[0].match_type == "like"
    assert result.hits[0].clause_uid == "like-1"


async def test_empty_library_returns_no_corpus_not_compliant(db_conn):
    result = await clause_store.search_clauses(db_conn, query="基坑")
    assert result.status == "no_corpus"
    assert result.hits == []
    assert "无法判定" in result.message


async def test_no_match_is_explicit(db_conn):
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    result = await clause_store.search_clauses(db_conn, query="完全不存在的检索词")
    assert result.status == "no_match"
    assert result.hits == []
    assert "不能作为合规依据" in result.message


async def test_router_search_returns_exact_hit_when_enabled(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    payload = await standards_admin.search_clause_library(
        standard_code="GB 50007-2011", version="2011", clause_no="5.2.1", q="", limit=10, db=db_conn
    )
    assert payload["status"] == "ok"
    assert payload["hits"][0]["match_type"] == "exact"
    assert payload["hits"][0]["source_url"]


# ---------------------------------------------------------------------------
# 4. 迁移幂等（仅含 projects 的旧库）
# ---------------------------------------------------------------------------


async def test_migration_is_idempotent_on_projects_only_db(tmp_path):
    import app.db as app_db

    legacy = tmp_path / "legacy.db"
    snapshot = list(app_db._MIGRATION_DEGRADED)
    conn = await aiosqlite.connect(legacy)
    try:
        await conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '')")
        await conn.commit()
        before = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]

        await _migrate(conn)
        await conn.commit()
        after_first = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]

        await _migrate(conn)
        await conn.commit()
        after_second = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]

        assert before == after_first == after_second
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='standard_clauses'")
        assert (await cur.fetchone()) is not None
    finally:
        # 老库只有 projects，_migrate 会为缺表记录降级；还原全局状态，避免污染后续用例。
        app_db._MIGRATION_DEGRADED.clear()
        app_db._MIGRATION_DEGRADED.extend(snapshot)
        await conn.close()


# ---------------------------------------------------------------------------
# 5. D 预留分支接真实取数
# ---------------------------------------------------------------------------


async def test_appendix_never_lists_library_clauses_without_scheme_registration(db_conn, monkeypatch, tmp_path):
    """钉住假阳性：库里有条款、但该方案零登记时，附录不得把库内条目当引用。"""
    _, sid = await _seed_scheme(db_conn)
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    path = await _build_dossier(db_conn, monkeypatch, tmp_path, sid, clause_enabled=True)

    text = _docx_text(path)
    assert dossier.CLAUSE_LIBRARY_DECLARATION in text
    # 附录二不得把库内摘录当作本文引用；标准待核实节可能出现标准号，但不会出现摘录。
    assert "基础底面压力应符合设计要求。" not in text
    assert _find_table(path, ["章节", "引用", "条款", "核实状态", "来源链接"]) is None


async def test_load_clause_traces_never_queries_library_even_when_switch_on(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    calls = _spy_clause_queries(monkeypatch)
    result = await dossier.load_clause_traces(db_conn, "any-scheme")
    assert result["available"] is False
    assert result["statement"] == dossier.CLAUSE_LIBRARY_DECLARATION
    assert calls == []


@pytest.mark.parametrize(
    ("dossier_on", "clause_on"),
    [(False, False), (False, True), (True, False), (True, True)],
)
async def test_switch_matrix_appendix_and_clause_queries(db_conn, monkeypatch, tmp_path, dossier_on, clause_on):
    """四种开关组合：关闭导出 → 404；导出开启 → 附录恒为声明态。"""
    _, sid = await _seed_scheme(db_conn)
    await clause_store.import_clauses(db_conn, [_valid_clause()], confirm_license=True)
    monkeypatch.setattr(settings, "dossier_enabled", dossier_on)
    monkeypatch.setattr(settings, "clause_library_enabled", clause_on)
    monkeypatch.setattr(export_dossier, "EXPORTS_DIR", Path(tmp_path))
    calls = _spy_clause_queries(monkeypatch)

    if not dossier_on:
        with pytest.raises(HTTPException) as excinfo:
            await export_dossier.export_dossier(sid, db=db_conn)
        assert excinfo.value.status_code == 404
        assert calls == []
        return

    resp = await export_dossier.export_dossier(sid, db=db_conn)
    path = Path(resp.path)
    text = _docx_text(path)
    assert dossier.CLAUSE_LIBRARY_DECLARATION in text
    assert _find_table(path, ["章节", "引用", "条款", "核实状态", "来源链接"]) is None
    if clause_on:
        # 只允许「标准待核实」探库；附录本身绝不查询条款库。
        assert calls
    else:
        assert calls == []
