"""可复核底稿（R179 Phase D，2026-11-06）。

主文档仍由既有导出渲染链路生成（``_prepare_export`` + ``_build_docx_sync``）；
本模块只在同一条 docx 末尾追加三类附属产物：

1. **缺陷与修复记录**：只读 ``scheme_defects`` / ``fix_decisions``（Phase C 账本）；
2. **规范引用溯源附录**：本阶段没有方案级引用关联，附录**始终显式声明「无法判定」**；
   绝不把条款库现行条目当作「本文引用了什么」，也不输出空表冒充「无引用问题」；
3. **人工复核清单**：事实待确认（低置信度 / 未 resolve）、缺陷待决策
   （``fix_policy=protected`` 或仍为 ``suggested``）、标准待核实
   （``verification_status`` 非 verified；表缺失或空库时同样显式声明「无法判定」）。

失败语义：任一数据源表缺失或查询失败，只在对应附录写「无法判定」并记 WARNING，
绝不阻断整份底稿产出（空数据 / 表缺失 / 查询失败都必须得到可打开的文件）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.config import settings
from app.services.facts_extractor import FACT_SCOPE_PREDICATE, resolve_scheme_project_id

logger = logging.getLogger(__name__)

#: 低置信度阈值：低于该值的事实进入「事实待确认」清单。
LOW_CONFIDENCE = 0.8

#: Phase E 条款库表名（见 docs/条款级规范库_技术预研_R179_20261106.md）。
CLAUSE_LIBRARY_TABLE = "standard_clauses"

#: 方案级引用关联未建立时的固定声明（逐字锁定，禁止用库内条目或空表代替）。
CLAUSE_LIBRARY_DECLARATION = "尚未建立方案级引用关联，无法判定本文引用了哪些规范条款"

#: 标准核实数据源未建立 / 条款库开关关闭时的固定声明。
STANDARD_VERIFICATION_DECLARATION = "尚未建立标准核实数据源（verification_status），无法判定"

_DEFECT_COLUMNS = (
    "id, rule_id, dimension, severity, title, detail, section_id, section_title, "
    "before_text, after_text, suggestion, fix_policy, status, verdict, created_at"
)


async def _fetch_rows(db, sql: str, params=(), *, what: str) -> tuple[list[dict], str]:
    """查询并返回 ``(rows, error)``；失败时 error 非空，调用方据此写「无法判定」。

    只捕获 ``sqlite3.Error``（表缺失 / 列缺失 / 连接层错误 = 可接受的失败），
    编程错误照常上抛，不再被无条件吞掉。
    """
    try:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    except sqlite3.Error as e:
        logger.warning("底稿：%s 查询失败（按无法判定输出）: %s", what, e)
        return [], f"{type(e).__name__}: {e}"
    return [dict(r) for r in rows], ""


async def _table_exists(db, name: str) -> bool:
    rows, err = await _fetch_rows(
        db,
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
        what=f"sqlite_master:{name}",
    )
    return bool(rows) and not err


async def collect_defects(db, scheme_id: str) -> dict:
    """缺陷与修复记录（只读，不写回源表）。"""
    rows, err = await _fetch_rows(
        db,
        f"SELECT {_DEFECT_COLUMNS} FROM scheme_defects WHERE scheme_id=? ORDER BY created_at, id",
        (scheme_id,),
        what="scheme_defects",
    )
    decisions, derr = await _fetch_rows(
        db,
        "SELECT defect_id, decision, decided_by, reason, applied, created_at "
        "FROM fix_decisions WHERE scheme_id=? ORDER BY created_at, id",
        (scheme_id,),
        what="fix_decisions",
    )
    errors = [e for e in (err, derr) if e]
    return {"items": rows, "decisions": decisions, "error": "；".join(errors)}


async def collect_facts(db, scheme_id: str, project_id: str) -> dict:
    """事实待确认：低置信度或未 resolve（含同项目共享事实）。"""
    sql = (
        "SELECT id, title, content, confidence, is_resolved, source_ref, category "
        f"FROM global_facts WHERE {FACT_SCOPE_PREDICATE} "
        "AND (COALESCE(is_resolved, 1) = 0 OR COALESCE(confidence, 1.0) < ?) "
        "ORDER BY confidence, title"
    )
    rows, err = await _fetch_rows(db, sql, (scheme_id, project_id, LOW_CONFIDENCE), what="global_facts")
    return {"items": rows, "error": err}


async def load_clause_traces(db, scheme_id: str) -> dict:
    """规范引用溯源：没有方案级引用关联时只声明「无法判定」，且不查询条款库。

    附录要回答「这份方案引用了哪些条款」，而不是「条款库里有什么」。
    在 ``scheme_clause_refs`` 这类真实登记来源落地前，拿库内 active 条目填充附录
    会把「库有数据」误读为「方案有引用」，因此本阶段一律回到声明态。
    """
    return {"available": False, "items": [], "statement": CLAUSE_LIBRARY_DECLARATION}


async def load_standard_pending(db, scheme_id: str) -> dict:
    """标准待核实：``verification_status`` 非 verified（Phase E 数据源预留）。

    条款库开关关闭 → 直接声明「无法判定」，不执行任何 ``standard_clauses`` 查询；
    开关开启但表不存在 / 空表 → 同样声明「无法判定」。
    """
    if not settings.clause_library_enabled:
        return {
            "available": False,
            "items": [],
            "statement": STANDARD_VERIFICATION_DECLARATION,
        }
    if not await _table_exists(db, CLAUSE_LIBRARY_TABLE):
        return {
            "available": False,
            "items": [],
            "statement": STANDARD_VERIFICATION_DECLARATION,
        }
    rows, err = await _fetch_rows(
        db,
        "SELECT clause_uid, standard_code, clause_no, verification_status "
        "FROM standard_clauses "
        "WHERE COALESCE(status, 'active') = 'active' AND verification_status <> 'verified' "
        "ORDER BY standard_code, clause_no LIMIT 200",
        (),
        what="standard_clauses:verification_status",
    )
    if err:
        return {"available": True, "items": rows, "statement": err}
    if not rows:
        # 空集不得冒充「无待核实标准」：表存在但无数据时仍按数据源缺失处理。
        return {"available": False, "items": [], "statement": STANDARD_VERIFICATION_DECLARATION}
    return {"available": True, "items": rows, "statement": ""}


async def collect_all(db, scheme_id: str, scheme: dict | None = None) -> dict:
    """聚合三类附属产物所需的全部数据（每个来源独立降级）。

    任一来源在连接层失败也只降级该来源（写「无法判定」），绝不连带整份底稿失败。
    """
    project_id = (scheme or {}).get("project_id") or ""
    if not project_id:
        try:
            project_id = await resolve_scheme_project_id(db, scheme_id) or ""
        except sqlite3.Error as e:
            logger.warning("底稿：解析方案所属项目失败（按无法判定输出）: %s", e)
            project_id = ""

    try:
        defects = await collect_defects(db, scheme_id)
    except sqlite3.Error as e:
        logger.warning("底稿：缺陷账本读取异常（按无法判定输出）: %s", e)
        defects = {"items": [], "decisions": [], "error": f"{type(e).__name__}: {e}"}

    try:
        facts = await collect_facts(db, scheme_id, project_id)
    except sqlite3.Error as e:
        logger.warning("底稿：事实清单读取异常（按无法判定输出）: %s", e)
        facts = {"items": [], "error": f"{type(e).__name__}: {e}"}

    try:
        clauses = await load_clause_traces(db, scheme_id)
    except sqlite3.Error as e:
        logger.warning("底稿：条款库探针失败（按无法判定输出）: %s", e)
        clauses = {"available": False, "items": [], "statement": CLAUSE_LIBRARY_DECLARATION}

    standards = await load_standard_pending(db, scheme_id)
    pending_defects = [
        d
        for d in defects["items"]
        if str(d.get("fix_policy") or "") == "protected" or str(d.get("status") or "") == "suggested"
    ]
    return {
        "scheme_id": scheme_id,
        "scheme_name": str((scheme or {}).get("name") or ""),
        "defects": defects,
        "facts": facts,
        "clauses": clauses,
        "standards": standards,
        "pending_defects": pending_defects,
    }


# ---------------------------------------------------------------------------
# docx 追加：只写段落 / 表格，不重新排版正文
# ---------------------------------------------------------------------------


def _heading(doc, text: str, level: int) -> None:
    doc.add_heading(text, level=level)


def _table(doc, headers: list[str], rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row[: len(headers)]):
            cells[i].text = str(value or "")


def _cell(value) -> str:
    return "" if value is None else str(value)


def _add_defect_appendix(doc, data: dict) -> None:
    _heading(doc, "附件一：缺陷与修复记录", 2)
    defects = data["defects"]
    items = defects["items"]
    if defects["error"]:
        doc.add_paragraph(f"缺陷账本查询失败，本附件无法判定（{defects['error']}）。")
        return
    if not items:
        doc.add_paragraph("本方案当前无缺陷记录（0 条）。")
    else:
        doc.add_paragraph(f"共 {len(items)} 条缺陷；其中待决策 {len(data['pending_defects'])} 条。")
        headers = ["规则ID", "维度", "状态", "策略", "标题", "修复前（全文）", "修复后（全文）", "建议"]
        rows = [
            [
                _cell(d.get("rule_id")),
                _cell(d.get("dimension")),
                _cell(d.get("status")),
                _cell(d.get("fix_policy")),
                _cell(d.get("title")),
                _cell(d.get("before_text")),
                _cell(d.get("after_text")),
                _cell(d.get("suggestion")),
            ]
            for d in items
        ]
        _table(doc, headers, rows)
    decisions = defects["decisions"]
    if decisions:
        doc.add_paragraph("裁决记录：")
        _table(
            doc,
            ["缺陷ID", "裁决", "裁决人", "理由", "已应用", "时间"],
            [
                [
                    _cell(d.get("defect_id")),
                    _cell(d.get("decision")),
                    _cell(d.get("decided_by")),
                    _cell(d.get("reason")),
                    _cell(d.get("applied")),
                    _cell(d.get("created_at")),
                ]
                for d in decisions
            ],
        )


def _add_clause_appendix(doc, data: dict) -> None:
    _heading(doc, "附件二：规范引用溯源附录", 2)
    clauses = data["clauses"]
    doc.add_paragraph(clauses["statement"])
    doc.add_paragraph(
        "本附录只陈述方案级引用关联缺失的事实，不列出条款库现行条目，" "以免把「库内有什么」误读为「本文引用了什么」。"
    )


def _add_review_checklist(doc, data: dict) -> None:
    _heading(doc, "附件三：人工复核清单", 2)
    facts = data["facts"]
    pending = data["pending_defects"]
    standards = data["standards"]
    std_items = len(standards["items"]) if standards["available"] else 0
    std_note = f"{std_items} 项" if standards["available"] else "无法判定"
    total = len(facts["items"]) + len(pending) + std_items
    doc.add_paragraph(
        f"事实待确认 {len(facts['items'])} 项 + 缺陷待决策 {len(pending)} 项 + "
        f"标准待核实 {std_note}；合计需人工复核 {total} 项。"
    )

    _heading(doc, f"3.1 事实待确认（{len(facts['items'])} 项）", 3)
    if facts["error"]:
        doc.add_paragraph(f"事实数据查询失败，本节无法判定（{facts['error']}）。")
    elif not facts["items"]:
        doc.add_paragraph("无待确认事实（0 条）。")
    else:
        _table(
            doc,
            ["标题", "类目", "置信度", "已确认", "来源", "内容"],
            [
                [
                    _cell(f.get("title")),
                    _cell(f.get("category")),
                    _cell(f.get("confidence")),
                    "是" if int(f.get("is_resolved") or 0) else "否",
                    _cell(f.get("source_ref")),
                    _cell(f.get("content")),
                ]
                for f in facts["items"]
            ],
        )

    _heading(doc, f"3.2 缺陷待决策（{len(pending)} 项）", 3)
    if data["defects"]["error"]:
        doc.add_paragraph(f"缺陷账本查询失败，本节无法判定（{data['defects']['error']}）。")
    elif not pending:
        doc.add_paragraph("无待决策缺陷（0 条）。")
    else:
        _table(
            doc,
            ["规则ID", "状态", "策略", "标题"],
            [
                [
                    _cell(d.get("rule_id")),
                    _cell(d.get("status")),
                    _cell(d.get("fix_policy")),
                    _cell(d.get("title")),
                ]
                for d in pending
            ],
        )

    _heading(doc, "3.3 标准待核实", 3)
    if not standards["available"]:
        doc.add_paragraph(standards["statement"])
    elif standards.get("statement"):
        doc.add_paragraph(f"标准核实数据查询失败，本节无法判定（{standards['statement']}）。")
    elif not standards["items"]:
        doc.add_paragraph("无待核实标准（0 条）。")
    else:
        _table(
            doc,
            ["条款UID", "标准号", "条款号", "核实状态"],
            [
                [
                    _cell(s.get("clause_uid")),
                    _cell(s.get("standard_code")),
                    _cell(s.get("clause_no")),
                    _cell(s.get("verification_status")),
                ]
                for s in standards["items"]
            ],
        )


def append_to_docx(path: str | Path, data: dict) -> None:
    """在既有主文档末尾追加三附件（原地保存，不重排正文）。"""
    from docx import Document  # 延迟导入：仅底稿导出需要，避免影响应用启动

    doc = Document(str(path))
    doc.add_page_break()
    _heading(doc, "可复核底稿（附属产物）", 1)
    if data.get("scheme_name"):
        doc.add_paragraph(f"方案：{data['scheme_name']}")
    doc.add_paragraph(
        "本部分为机器生成的可复核材料，随主文档一同交付：缺陷与修复记录、" "规范引用溯源附录、人工复核清单。"
    )
    _add_defect_appendix(doc, data)
    _add_clause_appendix(doc, data)
    _add_review_checklist(doc, data)
    doc.save(str(path))
