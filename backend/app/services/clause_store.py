"""条款级规范库的导入校验与检索（R179 Phase E，2026-11-06）。

本阶段只做最小闭环：
1. 基表 ``standard_clauses``（见 :data:`app.schema_sql.STANDARD_CLAUSES_DDL`）；
2. 导入校验（六项必填 + 版权护栏 + 唯一约束，strict 整批失败）；
3. 精确条文号查询（标准号 + 版本 + 条款号）；
4. 未命中走 LIKE 降级（不依赖 FTS5，10 万条约 14–16 ms）。

边界
----
* 只存摘录，不存全文：``excerpt`` ≤500 字，``excerpt_type`` 禁止 ``full``；
* ``license_note`` / ``applicability`` 必填；``source_url`` 与 ``page_ref`` 至少一项非空，
  ``source_kind=internal`` 时 ``page_ref`` 必填；
* ``confirm_license`` 是导入边界参数（本阶段不建批次表，故不落库）；
* 条款库是**全局数据**，不按 project_id / scheme_id 归属，不登记级联清理；
* 检索永远不返回 ``compliant``；空库 / 无命中都只能表达「无法判定」。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.services.standards_registry import (
    find_abolished_codes,
    is_known_base_number,
    is_known_standard,
    normalize_standard_code,
    strip_standard_year,
)

#: 允许的摘录类型；``full`` / ``full_text`` 明确不在其中（版权护栏）。
EXCERPT_TYPES = ("summary", "key_sentence", "data_point")

#: LIKE 降级时最多使用的查询片段数（防止 SQL 过长）。
_MAX_LIKE_TOKENS = 8

#: LIKE 降级默认返回条数。
_DEFAULT_LIMIT = 10

_WS_RE = re.compile(r"[\s\u3000]+")
_CLAUSE_RE = re.compile(r"^第?\s*(\d+(?:\.\d+)+)\s*(?:条|款|节)?$")
_APPENDIX_RE = re.compile(r"^附\s*录\s*([A-Za-z])\s*[.．]\s*(\d+(?:\.\d+)*)$")
_LIKE_SPLIT_RE = re.compile(r"[\s,，;；、。]+")


class ClauseValidationError(ValueError):
    """导入校验失败（整批拒绝）；``errors`` 逐行给出字段与原因。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"条款导入校验失败：{len(errors)} 项")


@dataclass(frozen=True)
class ClauseRef:
    """检索输出契约：生成侧只拿摘录与引用元数据，不拿全文。"""

    clause_uid: str
    standard_code: str
    standard_name: str
    version: str
    clause_no: str
    excerpt: str
    applicability: str
    verification_status: str
    license_note: str
    source_url: str
    page_ref: str
    source_kind: str
    match_type: str


@dataclass(frozen=True)
class ClauseSearchResult:
    """三态检索结果；``status`` 非 ``ok`` 时不得据此认定「无问题」。"""

    status: str
    hits: list[ClauseRef] = field(default_factory=list)
    degraded: bool = False
    message: str = ""


def normalize_clause_no(clause_no: str) -> tuple[str, str]:
    """条款号归一化，返回 ``(归一值, clause_kind)``。

    ``第5.2.1条`` / ``5.2.1`` / `` 第 5.2.1 条 `` → ``5.2.1``；
    附录 ``附录A.0.1`` → ``A.0.1`` 且 ``clause_kind='appendix'``（E9 实测主正则不覆盖附录）。
    """
    raw = unicodedata.normalize("NFKC", str(clause_no or "")).strip()
    if not raw:
        return "", "clause"
    appendix = _APPENDIX_RE.match(_WS_RE.sub("", raw))
    if appendix:
        return f"{appendix.group(1).upper()}.{appendix.group(2)}", "appendix"
    compact = _WS_RE.sub("", raw)
    matched = _CLAUSE_RE.match(compact)
    if matched:
        return matched.group(1), "clause"
    return compact, "clause"


def _excerpt_hash(excerpt: str) -> str:
    """摘录去空白后取 sha256（用于跨批次去重/审计，不做 NFKC 以免破坏单位）。"""
    normalized = re.sub(r"\s+", "", excerpt or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _as_mandatory(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value else 0
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "是", "强制", "强制性"):
        return 1
    if text in ("0", "false", "no", "否", ""):
        return 0
    raise ValueError("mandatory 取值非法（只接受 0/1/是/否/true/false）")


def _validate_row(row: dict, index: int) -> dict:
    """校验单行并返回可直接入库的规范化字段；失败抛 ``ClauseValidationError``。"""
    errors: list[dict] = []

    def reject(field_name: str, message: str) -> None:
        errors.append({"row": index, "field": field_name, "message": message})

    standard_code = str(row.get("standard_code") or "").strip()
    version = str(row.get("version") or "").strip()
    clause_no = str(row.get("clause_no") or "").strip()
    excerpt = str(row.get("excerpt") or "")
    applicability = str(row.get("applicability") or "").strip()
    license_note = str(row.get("license_note") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    page_ref = str(row.get("page_ref") or "").strip()
    source_kind = str(row.get("source_kind") or "public").strip().lower()
    excerpt_type = str(row.get("excerpt_type") or "summary").strip().lower()
    verification_status = str(row.get("verification_status") or "unverified").strip().lower()
    status = str(row.get("status") or "active").strip().lower()

    if not standard_code:
        reject("standard_code", "缺少标准号")
    if not version:
        reject("version", "缺少版本号")
    if not clause_no:
        reject("clause_no", "缺少条款号")
    if not excerpt.strip():
        reject("excerpt", "摘录不能为空")
    elif len(excerpt) > 500:
        reject("excerpt", f"摘录超长（{len(excerpt)} 字，上限 500 字）")
    if not applicability:
        reject("applicability", "缺少适用条件")
    if not license_note:
        reject("license_note", "缺少许可说明")
    if not source_url and not page_ref:
        reject("source_url", "缺少来源：source_url 与 page_ref 至少一项非空")
    if source_kind not in ("public", "internal"):
        reject("source_kind", f"来源类型非法：{source_kind}")
    elif source_kind == "internal" and not page_ref:
        reject("page_ref", "source_kind=internal 时 page_ref 必填")
    if excerpt_type not in EXCERPT_TYPES:
        if excerpt_type in ("full", "full_text"):
            reject("excerpt_type", "禁止导入全文（excerpt_type 不得为 full）")
        else:
            reject("excerpt_type", f"摘录类型非法：{excerpt_type}")
    if verification_status not in ("unverified", "verified", "doubtful"):
        reject("verification_status", f"核实状态非法：{verification_status}")
    if status not in ("active", "superseded", "abolished", "draft"):
        reject("status", f"条款状态非法：{status}")

    if errors:
        raise ClauseValidationError(errors)

    code_norm = normalize_standard_code(standard_code)
    if not code_norm:
        reject("standard_code", "标准号归一化后为空")
    elif find_abolished_codes(standard_code):
        reject("standard_code", f"标准已废止：{standard_code}")
    elif not (is_known_standard(standard_code) or is_known_base_number(standard_code)):
        reject("standard_code", f"标准未收录：{standard_code}")

    try:
        mandatory = _as_mandatory(row.get("mandatory", 0))
    except ValueError as exc:
        reject("mandatory", str(exc))
        mandatory = 0

    if errors:
        raise ClauseValidationError(errors)

    clause_norm, clause_kind = normalize_clause_no(clause_no)
    if not clause_norm:
        reject("clause_no", "条款号归一化后为空")
        raise ClauseValidationError(errors)

    return {
        "clause_uid": str(row.get("clause_uid") or "").strip() or uuid.uuid4().hex,
        "standard_code": standard_code,
        "standard_code_norm": code_norm,
        "standard_base_norm": strip_standard_year(standard_code),
        "standard_name": str(row.get("standard_name") or "").strip(),
        "version": version,
        "clause_no": clause_no,
        "clause_no_norm": clause_norm,
        "clause_kind": clause_kind,
        "chapter": str(row.get("chapter") or "").strip(),
        "clause_title": str(row.get("clause_title") or "").strip(),
        "excerpt": excerpt,
        "excerpt_type": excerpt_type,
        "excerpt_hash": _excerpt_hash(excerpt),
        "mandatory": mandatory,
        "applicability": applicability,
        "license_note": license_note,
        "source_url": source_url,
        "page_ref": page_ref,
        "source_kind": source_kind,
        "verification_status": verification_status,
        "status": status,
    }


def validate_clauses(rows: Iterable[dict]) -> list[dict]:
    """整批校验；任一必填/格式错误或 ``clause_uid`` 批内重复 → 整批拒绝。"""
    items = list(rows or [])
    errors: list[dict] = []
    validated: list[dict] = []
    seen_uids: dict[str, int] = {}
    for index, row in enumerate(items, start=1):
        try:
            item = _validate_row(row, index)
        except ClauseValidationError as exc:
            errors.extend(exc.errors)
            continue
        uid = item["clause_uid"]
        if uid in seen_uids:
            errors.append(
                {
                    "row": index,
                    "field": "clause_uid",
                    "message": f"clause_uid 与第 {seen_uids[uid]} 行重复：{uid}",
                }
            )
            continue
        seen_uids[uid] = index
        validated.append(item)
    if errors:
        raise ClauseValidationError(errors)
    return validated


_INSERT_SQL = (
    "INSERT INTO standard_clauses ("
    " clause_uid, standard_code, standard_code_norm, standard_base_norm, standard_name,"
    " version, clause_no, clause_no_norm, clause_kind, chapter, clause_title,"
    " excerpt, excerpt_type, excerpt_hash, mandatory, applicability, license_note,"
    " source_url, page_ref, source_kind, verification_status, status, batch_id"
    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


async def import_clauses(
    db,
    rows: Iterable[dict],
    *,
    confirm_license: bool,
    batch_id: str = "",
) -> dict:
    """strict 整批导入：先全量校验，再单事务写入；任一失败整批不落库。"""
    if not confirm_license:
        raise ClauseValidationError(
            [{"row": 0, "field": "confirm_license", "message": "未确认版权许可（confirm_license=true 必填）"}]
        )
    items = validate_clauses(rows)
    batch = batch_id or f"B{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    await db.execute("SAVEPOINT clause_import")
    try:
        for item in items:
            await db.execute(
                _INSERT_SQL,
                (
                    item["clause_uid"],
                    item["standard_code"],
                    item["standard_code_norm"],
                    item["standard_base_norm"],
                    item["standard_name"],
                    item["version"],
                    item["clause_no"],
                    item["clause_no_norm"],
                    item["clause_kind"],
                    item["chapter"],
                    item["clause_title"],
                    item["excerpt"],
                    item["excerpt_type"],
                    item["excerpt_hash"],
                    item["mandatory"],
                    item["applicability"],
                    item["license_note"],
                    item["source_url"],
                    item["page_ref"],
                    item["source_kind"],
                    item["verification_status"],
                    item["status"],
                    batch,
                ),
            )
        await db.execute("RELEASE SAVEPOINT clause_import")
        await db.commit()
    except sqlite3.IntegrityError as exc:
        await db.execute("ROLLBACK TO SAVEPOINT clause_import")
        await db.execute("RELEASE SAVEPOINT clause_import")
        raise ClauseValidationError([{"row": 0, "field": "db", "message": str(exc)}]) from exc
    return {"batch_id": batch, "total": len(items), "accepted": len(items), "rejected": 0}


_SELECT_COLUMNS = (
    "clause_uid, standard_code, standard_name, version, clause_no, excerpt,"
    " applicability, verification_status, license_note, source_url, page_ref, source_kind"
)


def _to_ref(row: dict, match_type: str) -> ClauseRef:
    return ClauseRef(
        clause_uid=str(row.get("clause_uid") or ""),
        standard_code=str(row.get("standard_code") or ""),
        standard_name=str(row.get("standard_name") or ""),
        version=str(row.get("version") or ""),
        clause_no=str(row.get("clause_no") or ""),
        excerpt=str(row.get("excerpt") or ""),
        applicability=str(row.get("applicability") or ""),
        verification_status=str(row.get("verification_status") or "unverified"),
        license_note=str(row.get("license_note") or ""),
        source_url=str(row.get("source_url") or ""),
        page_ref=str(row.get("page_ref") or ""),
        source_kind=str(row.get("source_kind") or "public"),
        match_type=match_type,
    )


def _escape_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_tokens(query: str) -> list[str]:
    tokens = [t.strip() for t in _LIKE_SPLIT_RE.split(str(query or "")) if t.strip()]
    return tokens[:_MAX_LIKE_TOKENS]


async def search_clauses(
    db,
    *,
    standard_code: str = "",
    version: str = "",
    clause_no: str = "",
    query: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> ClauseSearchResult:
    """先精确条文号命中；未命中走 LIKE 降级；空库/无命中均不冒充「无问题」。"""
    cur = await db.execute("SELECT COUNT(*) AS c FROM standard_clauses WHERE status='active'")
    corpus = int((await cur.fetchone())[0])
    if corpus == 0:
        return ClauseSearchResult(
            status="no_corpus",
            message="无法判定：条款库未收录",
        )

    code_norm = normalize_standard_code(standard_code)
    clause_norm, _ = normalize_clause_no(clause_no) if clause_no else ("", "clause")

    if code_norm and version.strip() and clause_norm:
        cur = await db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM standard_clauses"
            " WHERE status='active' AND standard_code_norm=? AND version=? AND clause_no_norm=?"
            " LIMIT ?",
            (code_norm, version.strip(), clause_norm, max(1, int(limit))),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            return ClauseSearchResult(
                status="ok",
                hits=[_to_ref(r, "exact") for r in rows],
                degraded=False,
                message="精确条文号命中",
            )

    tokens = _like_tokens(query)
    if not tokens:
        return ClauseSearchResult(
            status="no_match",
            message="未检索到相关条款，不能作为合规依据",
        )

    sql = f"SELECT {_SELECT_COLUMNS} FROM standard_clauses WHERE status='active'"
    params: list = []
    if code_norm:
        sql += " AND standard_code_norm=?"
        params.append(code_norm)
    if version.strip():
        sql += " AND version=?"
        params.append(version.strip())
    sql += " AND (" + " OR ".join("excerpt LIKE ? ESCAPE '\\'" for _ in tokens) + ")"
    params.extend(f"%{_escape_like(t)}%" for t in tokens)
    sql += " ORDER BY standard_code, version, clause_no_norm LIMIT ?"
    params.append(max(1, int(limit)))

    cur = await db.execute(sql, tuple(params))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return ClauseSearchResult(
            status="no_match",
            degraded=True,
            message="未检索到相关条款，不能作为合规依据",
        )
    return ClauseSearchResult(
        status="ok",
        hits=[_to_ref(r, "like") for r in rows],
        degraded=True,
        message="降级检索（LIKE），结果可能不完整",
    )
