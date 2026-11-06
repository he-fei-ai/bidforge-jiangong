"""可复核底稿（R179 Phase D，2026-11-06）。

主文档仍由既有导出渲染链路生成（``_prepare_export`` + ``_build_docx_sync``）；
本模块只在同一条 docx 末尾追加三类附属产物：

1. **缺陷与修复记录**：只读 ``scheme_defects`` / ``fix_decisions``（Phase C 账本）；
2. **规范引用溯源附录**：Phase E 条款库尚未建立 → **显式声明「无法判定」**，
   绝不输出空表冒充「无引用问题」；E 落地后替换 :func:`load_clause_traces` 的实现即可；
3. **人工复核清单**：事实待确认（低置信度 / 未 resolve）、缺陷待决策
   （``fix_policy=protected`` 或仍为 ``suggested``）、标准待核实
   （``verification_status`` 非 verified —— 该数据源目前同样不存在，显式声明）。

失败语义：任一数据源表缺失或查询失败，只在对应附录写「无法判定」并记 WARNING，
绝不阻断整份底稿产出（空数据 / 表缺失 / 查询失败都必须得到可打开的文件）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.services.facts_extractor import FACT_SCOPE_PREDICATE, resolve_scheme_project_id

logger = logging.getLogger(__name__)

#: 低置信度阈值：低于该值的事实进入「事实待确认」清单。
LOW_CONFIDENCE = 0.8

#: Phase E 条款库表名（见 docs/条款级规范库_技术预研_R179_20261106.md）。
CLAUSE_LIBRARY_TABLE = "standard_clauses"

#: 条款库未建立时的固定声明（逐字锁定，禁止用空表代替）。
CLAUSE_LIBRARY_DECLARATION = "尚未建立条款库，无法判定"

#: 标准核实数据源未建立时的固定声明（Phase E ``standard_clauses`` 尚未建表）。
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
    """Phase E 预留取数接口：条款库未建立时返回显式声明（items 恒为空）。"""
    if not await _table_exists(db, CLAUSE_LIBRARY_TABLE):
        return {"available": False, "items": [], "statement": CLAUSE_LIBRARY_DECLARATION}
    # Phase E 落地后：在此按 scheme / 章节 → 引用 → 条款 取溯源行并返回 available=True。
    return {
        "available": False,
        "items": [],
        "statement": "条款库已建立（standard_clauses），引用溯源取数适配待 Phase E 接入，当前无法判定",
    }


async def load_standard_pending(db, scheme_id: str) -> dict:
    """标准待核实：``verification_status`` 非 verified（Phase E 数据源预留）。

    表不存在 → 显式声明「无法判定」；表存在 → 取库内未核 / 待核条款清单。
    """
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
    return {"available": True, "items": rows, "statement": err}


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
    if not clauses["available"]:
        doc.add_paragraph(clauses["statement"])
        doc.add_paragraph(
            "本附录为 Phase E（条款库）预留的取数接口：条款库就绪后按「章节 → 引用 → 条款」"
            "逐条溯源；当前不输出空表，以免把「无法判定」误读为「无引用问题」。"
        )
        return
    _table(doc, ["章节", "引用", "条款", "核实状态"], [[_cell(v) for v in row] for row in clauses["items"]])


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
