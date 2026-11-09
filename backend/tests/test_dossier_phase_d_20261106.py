# -*- coding: utf-8 -*-
"""R179 Phase D · 可复核底稿导出（2026-11-06）。

覆盖三条验收口径：
1. **开关默认关闭**：关闭时入口首行 404，既有导出链路零差异（不写 export_cache / export_round）；
2. **附属产物完整**：缺陷记录（rule_id / 维度 / 状态 / 前后全文）、条款库显式声明、
   人工复核清单三类齐全，且清单条数与 DB 查询**逐条一致**；
3. **失败仍可交付**：空数据、表缺失、查询失败三种情况都产出可打开的 docx；
   另有 export.py 知识库附录列名修正的「咬住」用例（改回旧列名必须变红）。
"""

from __future__ import annotations

import sqlite3
import uuid
import zipfile
from pathlib import Path

import pytest
from app.config import settings
from app.routers import export as export_router
from app.routers import export_dossier
from app.services import dossier
from app.services.facts_extractor import FACT_SCOPE_PREDICATE
from docx import Document
from fastapi import HTTPException
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


async def _seed_scheme(db, name: str = "底稿方案") -> tuple[str, str]:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (pid, name))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)", (sid, pid, name))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, content, sort_order)" " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), sid, pid, "工程概况", "正文段落一\n正文段落二", 0),
    )
    await db.commit()
    return pid, sid


async def _add_defect(
    db,
    pid: str,
    sid: str,
    *,
    rule_id: str = "STD-02",
    dimension: str = "合规性",
    title: str = "未引用强制性条文",
    status: str = "suggested",
    fix_policy: str = "free",
    before: str = "修复前全文-A\n第二行仍在",
    after: str = "修复后全文-B",
) -> str:
    did = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO scheme_defects"
        " (id, project_id, scheme_id, source_kind, rule_id, dimension, severity, title,"
        "  before_text, after_text, suggestion, fix_policy, policy_categories, status, verdict, fingerprint)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            did,
            pid,
            sid,
            "manual",
            rule_id,
            dimension,
            "high",
            title,
            before,
            after,
            "建议内容",
            fix_policy,
            "[]",
            status,
            "pending",
            did,
        ),
    )
    await db.commit()
    return did


async def _add_fact(
    db,
    pid: str,
    sid: str,
    title: str,
    *,
    confidence: float = 0.5,
    is_resolved: int = 0,
) -> None:
    await db.execute(
        "INSERT INTO global_facts"
        " (id, project_id, scheme_id, title, content, category, source_ref, confidence, is_resolved)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), pid, sid, title, f"{title}-内容", "尺寸", "招标文件", confidence, is_resolved),
    )
    await db.commit()


async def _build(db, monkeypatch, tmp_path, sid: str) -> Path:
    monkeypatch.setattr(export_dossier.settings, "dossier_enabled", True)
    monkeypatch.setattr(export_dossier, "EXPORTS_DIR", Path(tmp_path))
    # ✅ R195 重锚：底稿链接入了 DLV 阻断闸门（与 /export/docx 同口径），
    #    最小种子数据无审核记录 → DLV-09 high 阻断。本文件聚焦附属产物，
    #    闸门放行由桩替代；闸门阻断行为由 tests/test_export_r195_20261109.py 实证。
    async def _gate_pass(_sid, _db, _body):
        return [], {"checked": True, "override": False}

    monkeypatch.setattr(export_router, "_export_high_issue_gate", _gate_pass)
    resp = await export_dossier.export_dossier(sid, db=db)
    assert isinstance(resp, FileResponse)
    assert resp.path and Path(resp.path).exists()
    assert str(resp.filename).endswith("_可复核底稿.docx")
    return Path(resp.path)


def _docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def _docx_tables(path: Path) -> list[list[list[str]]]:
    doc = Document(str(path))
    return [[[cell.text for cell in row.cells] for row in table.rows] for table in doc.tables]


def _find_table(path: Path, first_header: str) -> list[list[str]] | None:
    for table in _docx_tables(path):
        if table and table[0] and table[0][0] == first_header:
            return table
    return None


def _find_table_by_headers(path: Path, headers: list[str]) -> list[list[str]] | None:
    """按完整表头定位（附件一与 3.2 首列同为「规则ID」，单列定位会串表）。"""
    for table in _docx_tables(path):
        if table and table[0] == headers:
            return table
    return None


# ---------------------------------------------------------------------------
# 1. 开关与既有链路隔离
# ---------------------------------------------------------------------------


async def test_switch_default_off_and_returns_404(db_conn, monkeypatch):
    """开关默认 False；关闭时 handler 首行 404（不触碰 DB / 不产文件）。"""
    monkeypatch.setattr(settings, "dossier_enabled", False)
    with pytest.raises(HTTPException) as excinfo:
        await export_dossier.export_dossier("any-scheme", db=db_conn)
    assert excinfo.value.status_code == 404


async def test_default_config_value_is_false():
    """配置默认值必须是关闭（防止上线即开启）。"""
    from app.config import Settings

    assert Settings().dossier_enabled is False


async def test_dossier_does_not_touch_export_cache(db_conn, monkeypatch, tmp_path):
    """底稿导出不得写 export_cache / 递增 export_round（既有导出链路零差异）。"""
    pid, sid = await _seed_scheme(db_conn)
    before_round = (await (await db_conn.execute("SELECT export_round FROM schemes WHERE id=?", (sid,))).fetchone())[0]
    await _build(db_conn, monkeypatch, tmp_path, sid)
    after_round = (await (await db_conn.execute("SELECT export_round FROM schemes WHERE id=?", (sid,))).fetchone())[0]
    rows = (await (await db_conn.execute("SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))).fetchone())[0]
    assert before_round == after_round
    assert rows == 0


# ---------------------------------------------------------------------------
# 2. 附属产物内容
# ---------------------------------------------------------------------------


async def test_defect_appendix_keeps_rule_dimension_status_and_full_text(db_conn, monkeypatch, tmp_path):
    pid, sid = await _seed_scheme(db_conn)
    await _add_defect(db_conn, pid, sid, rule_id="STD-02", dimension="合规性", status="suggested")
    path = await _build(db_conn, monkeypatch, tmp_path, sid)

    text = _docx_text(path)
    assert "附件一：缺陷与修复记录" in text
    assert "STD-02" in text
    assert "合规性" in text
    assert "suggested" in text
    # 前后值必须全文落地（含换行后的第二行，不能只留摘要）
    assert "修复前全文-A" in text and "第二行仍在" in text
    assert "修复后全文-B" in text
    assert "附件二：规范引用溯源附录" in text
    assert "附件三：人工复核清单" in text


async def test_docx_unzips_and_keeps_every_defect_field(db_conn, monkeypatch, tmp_path):
    """解压校验：docx 是合法 zip，document.xml 里每条缺陷的四个字段都在。"""
    pid, sid = await _seed_scheme(db_conn)
    await _add_defect(
        db_conn,
        pid,
        sid,
        rule_id="STD-02",
        dimension="合规性",
        status="suggested",
        before="前值甲-第一行\n前值甲-第二行",
        after="后值甲",
    )
    await _add_defect(
        db_conn,
        pid,
        sid,
        rule_id="STD-09",
        dimension="完整性",
        status="applied",
        fix_policy="protected",
        before="前值乙",
        after="后值乙",
    )
    path = await _build(db_conn, monkeypatch, tmp_path, sid)

    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        xml = zf.read("word/document.xml").decode("utf-8")
    for token in (
        "STD-02",
        "合规性",
        "suggested",
        "前值甲-第一行",
        "前值甲-第二行",
        "后值甲",
        "STD-09",
        "完整性",
        "applied",
        "前值乙",
        "后值乙",
    ):
        assert token in xml


async def test_clause_appendix_is_explicit_declaration_not_empty_table(db_conn, monkeypatch, tmp_path):
    _, sid = await _seed_scheme(db_conn)
    path = await _build(db_conn, monkeypatch, tmp_path, sid)
    text = _docx_text(path)
    assert dossier.CLAUSE_LIBRARY_DECLARATION in text
    # 不允许用「空表」冒充「无引用问题」：附录二不得出现溯源表格
    assert _find_table(path, "章节") is None


async def test_standard_pending_declares_missing_source(db_conn, monkeypatch, tmp_path):
    _, sid = await _seed_scheme(db_conn)
    path = await _build(db_conn, monkeypatch, tmp_path, sid)
    assert dossier.STANDARD_VERIFICATION_DECLARATION in _docx_text(path)


async def test_review_checklist_rows_match_db_row_by_row(db_conn, monkeypatch, tmp_path):
    """待确认清单必须与 DB 查询逐条一致（不是只看条数非零）。"""
    pid, sid = await _seed_scheme(db_conn)
    # 事实：f1 低置信 → 待确认；f2 未 resolve → 待确认；f3 高置信且已确认 → 排除
    await _add_fact(db_conn, pid, sid, "事实-低置信-甲", confidence=0.35, is_resolved=1)
    await _add_fact(db_conn, pid, sid, "事实-未确认-乙", confidence=0.95, is_resolved=0)
    await _add_fact(db_conn, pid, sid, "事实-已确认-丙", confidence=0.95, is_resolved=1)
    # 缺陷：d1 suggested；d2 protected；d3 已应用且 free → 不进待决策清单
    await _add_defect(db_conn, pid, sid, rule_id="R-1", status="suggested", fix_policy="free")
    await _add_defect(db_conn, pid, sid, rule_id="R-2", status="applied", fix_policy="protected")
    await _add_defect(db_conn, pid, sid, rule_id="R-3", status="applied", fix_policy="free")

    path = await _build(db_conn, monkeypatch, tmp_path, sid)

    facts_table = _find_table(path, "标题")
    assert facts_table is not None
    db_facts = [
        r[0]
        for r in await (
            await db_conn.execute(
                f"SELECT title FROM global_facts WHERE {FACT_SCOPE_PREDICATE}"
                " AND (COALESCE(is_resolved,1)=0 OR COALESCE(confidence,1.0) < ?)"
                " ORDER BY confidence, title",
                (sid, pid, dossier.LOW_CONFIDENCE),
            )
        ).fetchall()
    ]
    assert [row[0] for row in facts_table[1:]] == db_facts
    assert "事实-已确认-丙" not in [row[0] for row in facts_table[1:]]

    pending_table = _find_table_by_headers(path, ["规则ID", "状态", "策略", "标题"])
    assert pending_table is not None
    db_pending = [
        r[0]
        for r in await (
            await db_conn.execute(
                "SELECT rule_id FROM scheme_defects WHERE scheme_id=?"
                " AND (fix_policy='protected' OR status='suggested') ORDER BY created_at, id",
                (sid,),
            )
        ).fetchall()
    ]
    assert [row[0] for row in pending_table[1:]] == db_pending
    assert "R-3" not in [row[0] for row in pending_table[1:]]


# ---------------------------------------------------------------------------
# 3. 失败仍产出可打开文件
# ---------------------------------------------------------------------------


async def test_empty_data_still_produces_openable_docx(db_conn, monkeypatch, tmp_path):
    _, sid = await _seed_scheme(db_conn)
    path = await _build(db_conn, monkeypatch, tmp_path, sid)
    text = _docx_text(path)
    assert "本方案当前无缺陷记录（0 条）" in text
    assert "无待确认事实（0 条）" in text
    assert "无待决策缺陷（0 条）" in text


async def test_missing_tables_still_produce_openable_docx(db_conn, monkeypatch, tmp_path):
    _, sid = await _seed_scheme(db_conn)
    await db_conn.execute("DROP TABLE fix_decisions")
    await db_conn.execute("DROP TABLE scheme_defects")
    await db_conn.commit()
    path = await _build(db_conn, monkeypatch, tmp_path, sid)
    text = _docx_text(path)
    assert "缺陷账本查询失败，本附件无法判定" in text
    assert "缺陷账本查询失败，本节无法判定" in text


async def test_query_failure_still_produces_openable_docx(db_conn, monkeypatch, tmp_path):
    _, sid = await _seed_scheme(db_conn)

    async def boom(_db, _scheme_id):
        raise sqlite3.OperationalError("simulated query failure")

    monkeypatch.setattr(dossier, "collect_defects", boom)
    path = await _build(db_conn, monkeypatch, tmp_path, sid)
    text = _docx_text(path)
    assert "缺陷账本查询失败，本附件无法判定" in text
    assert "simulated query failure" in text


# ---------------------------------------------------------------------------
# 4. export.py 知识库附录列名修正（改回旧列名必须变红）
# ---------------------------------------------------------------------------


async def test_knowledge_base_rows_are_rendered_in_appendix(db_conn, monkeypatch):
    pid, sid = await _seed_scheme(db_conn)
    await db_conn.execute(
        "INSERT INTO knowledge_base (id, project_id, scheme_id, name, usage_hint, content)" " VALUES (?,?,?,?,?,?)",
        ("kb-1", pid, sid, "基坑支护要点", "用于支护章节", "放坡系数按 1:0.5 控制"),
    )
    await db_conn.execute(
        "INSERT INTO knowledge_base (id, project_id, scheme_id, name, usage_hint, content)" " VALUES (?,?,?,?,?,?)",
        ("kb-2", pid, sid, "临时用电", "用于临电章节", "三级配电两级保护"),
    )
    await db_conn.execute(
        "INSERT INTO doc_extractions (extraction_id, doc_id, project_id, extract_type,"
        " extract_data, status) VALUES (?,?,?,?,?,?)",
        ("ex-1", "doc-1", pid, "工程量清单", '{"value": "挖方 1200m³"}', "done"),
    )
    await db_conn.commit()
    monkeypatch.setattr(settings, "export_appendix_sources", True)

    groups = await export_router._load_appendix_sources(db_conn, sid, pid)
    group = next(g for g in groups if g["title"] == "知识库条目")
    names = [item["name"] for item in group["items"]]
    values = [item["value"] for item in group["items"]]
    assert names == ["基坑支护要点", "临时用电"]
    assert "放坡系数按 1:0.5 控制" in values
    # doc_extractions 排序键同样是列名错误（真实主键 extraction_id，旧实现 ORDER BY id）
    extract_group = next(g for g in groups if g["title"] == "解析提取成果")
    assert [item["name"] for item in extract_group["items"]] == ["工程量清单"]


async def test_appendix_programming_error_is_not_swallowed(db_conn, monkeypatch):
    """异常捕获已收窄为 sqlite3.Error：编程错误必须上抛，不能再静默吞掉。"""
    pid, sid = await _seed_scheme(db_conn)
    monkeypatch.setattr(settings, "export_appendix_sources", True)

    async def boom(_cur, *, what: str, strict: bool = False):
        raise RuntimeError("programming bug")

    monkeypatch.setattr(export_router, "_db_fetch_all", boom)
    with pytest.raises(RuntimeError):
        await export_router._load_appendix_sources(db_conn, sid, pid)
