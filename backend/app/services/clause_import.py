"""条款库数据准备链路（R179 · 2026-11-06）。

在 Phase E 的 :mod:`app.services.clause_store` 之上补齐「数据准备」层：

1. 导入模板：CSV 只放表头 + 一行字段说明，JSON 只放列定义与空 ``clauses``；
2. 批量导入：逐行校验，返回成功 / 失败 / 跳过 + 原因 + 行号，``dry_run`` 默认 true；
3. 重复与冲突：批内重复、库内重复、同 ``clause_uid`` 内容冲突（报冲突不覆盖）；
4. 全成或全不成：任一行失败整批不提交（理由见 :func:`_reject_message`）；
5. 导入报告：总数 / 成功 / 失败 / 跳过，失败按原因分组。

边界
----
* 不新增依赖：CSV 用标准库 ``csv``，JSON 用标准库 ``json``；
* 不复制校验与写入逻辑：单行校验复用 ``clause_store.validate_clauses``，
  落库复用 ``clause_store.import_clauses``（唯一写入实现）；
* 模板零条文：不放任何示例条文；若数据里出现 ``SAMPLE_ONLY`` 标记，该行
  一律拒绝入库；
* 条款库仍是全局数据，不按 project_id / scheme_id 归属。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import datetime

from app.services import clause_store

#: 模板列（顺序即 CSV 表头顺序）。
TEMPLATE_COLUMNS: tuple[str, ...] = (
    "clause_uid",
    "standard_code",
    "standard_name",
    "version",
    "clause_no",
    "chapter",
    "clause_title",
    "excerpt",
    "excerpt_type",
    "mandatory",
    "applicability",
    "license_note",
    "source_url",
    "page_ref",
    "source_kind",
    "verification_status",
    "status",
)

#: 模板字段说明行的 ``clause_uid`` 标记：说明行不是条文，导入时记为「跳过」。
GUIDE_ROW_MARKER = "__FIELD_GUIDE__"

#: 示例行标记：任何单元格命中即拒绝整行入库（防止把示例条文当真条文）。
SAMPLE_ONLY_MARKER = "SAMPLE_ONLY"

#: 字段说明（与 :data:`TEMPLATE_COLUMNS` 一一对应；刻意不写任何条文原文）。
_FIELD_GUIDE: dict[str, str] = {
    "clause_uid": "可选；留空自动生成；库内唯一",
    "standard_code": "必填；标准号，须已在现行标准库中收录",
    "standard_name": "建议填写；标准中文名称",
    "version": "必填；标准版本（年号或版本标识）",
    "clause_no": "必填；条款号（如 5.2.1 或 第5.2.1条）",
    "chapter": "可选；所属章",
    "clause_title": "可选；条款标题",
    "excerpt": "必填；仅摘录要点，1–500 字，禁止全文",
    "excerpt_type": "可选；summary / key_sentence / data_point，禁止 full",
    "mandatory": "可选；0/1，是否为强制性条文",
    "applicability": "必填；适用条件",
    "license_note": "必填；许可说明（仅摘录授权口径）",
    "source_url": "来源之一；公开来源链接（与 page_ref 至少一项非空）",
    "page_ref": "来源之一；页码或内部资料位置（source_kind=internal 时必填）",
    "source_kind": "可选；public / internal",
    "verification_status": "可选；unverified / verified / doubtful",
    "status": "可选；active / superseded / abolished / draft",
}

#: 内容一致性比对字段（摘录按去空白哈希比对）。
_COMPARE_FIELDS: tuple[str, ...] = (
    "standard_code",
    "standard_name",
    "version",
    "clause_no",
    "chapter",
    "clause_title",
    "excerpt_type",
    "mandatory",
    "applicability",
    "license_note",
    "source_url",
    "page_ref",
    "source_kind",
    "verification_status",
    "status",
)

_FETCH_BY_UID = (
    "SELECT clause_uid, standard_code, standard_name, version, clause_no, chapter,"
    " clause_title, excerpt_hash, excerpt_type, mandatory, applicability, license_note,"
    " source_url, page_ref, source_kind, verification_status, status"
    " FROM standard_clauses WHERE clause_uid = ? LIMIT 1"
)

_FETCH_UID_BY_KEY = (
    "SELECT clause_uid FROM standard_clauses"
    " WHERE standard_code_norm = ? AND version = ? AND clause_no_norm = ? LIMIT 1"
)


@dataclass
class ParsedRow:
    """解析后的一行：``data`` / ``error`` / ``skip`` 三者恰有一项生效。"""

    row: int
    data: dict | None = None
    error: str = ""
    message: str = ""
    skip: str = ""


@dataclass
class RowOutcome:
    """单行处理结果；``status`` ∈ accepted / failed / skipped。"""

    row: int
    clause_uid: str
    status: str
    reason: str
    message: str
    field: str = ""
    errors: list[dict] = dc_field(default_factory=list)


@dataclass
class ImportReport:
    """导入报告：总数 / 成功 / 失败 / 跳过 + 按原因分组 + 逐行结果。

    ``accepted`` 指通过全部校验的行数；是否真正落库由 ``committed`` /
    ``inserted`` 表示 —— ``dry_run`` 或整批被拒时 ``accepted > 0`` 但
    ``inserted = 0``，报告 ``message`` 会显式说明。
    """

    dry_run: bool
    committed: bool
    total: int
    accepted: int
    failed: int
    skipped: int
    inserted: int
    batch_id: str
    message: str
    failures_by_reason: dict[str, int] = dc_field(default_factory=dict)
    failure_rows: dict[str, list[int]] = dc_field(default_factory=dict)
    skips_by_reason: dict[str, int] = dc_field(default_factory=dict)
    outcomes: list[RowOutcome] = dc_field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "committed": self.committed,
            "total": self.total,
            "accepted": self.accepted,
            "failed": self.failed,
            "skipped": self.skipped,
            "inserted": self.inserted,
            "batch_id": self.batch_id,
            "message": self.message,
            "failures_by_reason": dict(self.failures_by_reason),
            "failure_rows": dict(self.failure_rows),
            "skips_by_reason": dict(self.skips_by_reason),
            "outcomes": [asdict(item) for item in self.outcomes],
        }


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def build_template_csv() -> str:
    """CSV 模板：表头 + 一行字段说明；不含任何示例条文。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(TEMPLATE_COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    writer.writerow({**_FIELD_GUIDE, "clause_uid": GUIDE_ROW_MARKER})
    return buffer.getvalue()


def build_template_json() -> dict:
    """JSON 模板：列定义 + 一行字段说明 + 空 ``clauses``。"""
    return {
        "columns": list(TEMPLATE_COLUMNS),
        "field_guide": [{**_FIELD_GUIDE, "clause_uid": GUIDE_ROW_MARKER}],
        "clauses": [],
    }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_csv_text(text: str) -> list[ParsedRow]:
    """解析 CSV 文本，保留物理行号（表头为第 1 行）。

    表头缺列属于**整批前置错误**（无法确定字段映射），直接抛
    ``ClauseValidationError``；行内列数不符、空行则作为逐行结果返回。
    """
    raw = str(text or "").lstrip("\ufeff")
    if not raw.strip():
        raise clause_store.ClauseValidationError([{"row": 1, "field": "csv", "message": "CSV 内容为空"}])

    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration as exc:  # pragma: no cover - 空内容已在上方拦截
        raise clause_store.ClauseValidationError([{"row": 1, "field": "csv", "message": "CSV 缺少表头"}]) from exc

    normalized = [str(name).strip().lower() for name in header]
    missing = [name for name in TEMPLATE_COLUMNS if name not in normalized]
    if missing:
        raise clause_store.ClauseValidationError(
            [{"row": 1, "field": "header", "message": f"CSV 缺少必需列：{'、'.join(missing)}"}]
        )

    entries: list[ParsedRow] = []
    for cells in reader:
        line = reader.line_num
        if not any(str(cell).strip() for cell in cells):
            entries.append(ParsedRow(row=line, skip="blank_row", message="空行，已跳过"))
            continue
        if len(cells) != len(normalized):
            entries.append(
                ParsedRow(
                    row=line,
                    error="column_count",
                    message=f"列数不符：表头 {len(normalized)} 列，本行 {len(cells)} 列",
                )
            )
            continue
        data = {name: cells[index].strip() for index, name in enumerate(normalized) if name in TEMPLATE_COLUMNS}
        entries.append(ParsedRow(row=line, data=data))
    return entries


def parse_json_payload(payload: str | list | dict) -> list[ParsedRow]:
    """解析 JSON（字符串 / 数组 / 含 ``clauses`` 的对象），行号用数组序号（从 1 开始）。"""
    obj = payload
    if isinstance(payload, str):
        text = payload.lstrip("\ufeff")
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise clause_store.ClauseValidationError(
                [{"row": exc.lineno, "field": "json", "message": f"JSON 解析失败：{exc.msg}"}]
            ) from exc

    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and isinstance(obj.get("clauses"), list):
        items = obj["clauses"]
    else:
        raise clause_store.ClauseValidationError(
            [{"row": 0, "field": "json", "message": "JSON 顶层须为数组，或含 clauses 数组的对象"}]
        )

    entries: list[ParsedRow] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            entries.append(ParsedRow(row=index, error="not_an_object", message="条目不是对象"))
            continue
        entries.append(ParsedRow(row=index, data={str(key).strip().lower(): value for key, value in item.items()}))
    return entries


def _coerce_entries(entries: Iterable) -> list[ParsedRow]:
    """把 dict 直接调用方 / ParsedRow 统一为 ParsedRow 列表。"""
    result: list[ParsedRow] = []
    for index, item in enumerate(entries or (), start=1):
        if isinstance(item, ParsedRow):
            result.append(item)
        elif isinstance(item, dict):
            result.append(ParsedRow(row=index, data=item))
        else:
            result.append(ParsedRow(row=index, error="not_an_object", message="条目不是对象"))
    return result


# ---------------------------------------------------------------------------
# 批量导入
# ---------------------------------------------------------------------------


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def _contains_sample_marker(data: dict) -> bool:
    return any(SAMPLE_ONLY_MARKER in str(value).upper() for value in data.values())


def _same_content(existing: dict, item: dict) -> bool:
    if _text(existing.get("excerpt_hash")) != _text(item.get("excerpt_hash")):
        return False
    return all(_text(existing.get(name)) == _text(item.get(name)) for name in _COMPARE_FIELDS)


def _diff_fields(existing: dict, item: dict) -> list[str]:
    fields: list[str] = []
    if _text(existing.get("excerpt_hash")) != _text(item.get("excerpt_hash")):
        fields.append("excerpt")
    for name in _COMPARE_FIELDS:
        if _text(existing.get(name)) != _text(item.get(name)):
            fields.append(name)
    return fields


async def _fetch_existing_by_uid(db, uid: str) -> dict | None:
    cursor = await db.execute(_FETCH_BY_UID, (uid,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _fetch_uid_by_key(db, key: tuple[str, str, str]) -> str:
    cursor = await db.execute(_FETCH_UID_BY_KEY, key)
    row = await cursor.fetchone()
    return _text(row[0]) if row else ""


def _reject_message(failed: int, total: int) -> str:
    """整批拒绝时写清「为什么不用部分成功」。"""
    return (
        f"整批未提交（全成或全不成）：本批 {total} 行中有 {failed} 行失败。"
        "条款库以「标准号 + 版本 + 条款号」作唯一键，且检索结果会直接进入生成正文；"
        "若只提交通过的行，库中会出现「同源批次只进一半」的状态，检索会漏掉未提交的条款"
        "却仍返回 ok —— 调用方看到的是「没有该条款」，与「该条款不存在」无法区分，"
        "比整批失败更危险；同时部分提交没有可续传的失败行游标，重跑会撞唯一键。"
        "故本批全部回滚，修正失败行后整批重导。"
    )


async def batch_import(
    db,
    entries: Iterable,
    *,
    confirm_license: bool = False,
    dry_run: bool = True,
    batch_id: str = "",
) -> ImportReport:
    """批量导入：逐行校验 + 重复/冲突判定 + 全成或全不成 + 分组报告。

    ``dry_run`` 默认 true：只预检、不写库；``dry_run=False`` 且
    ``confirm_license=True`` 时才真正提交。
    """
    rows = _coerce_entries(entries)
    batch = batch_id or f"B{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    outcomes: list[RowOutcome] = []
    accepted_items: list[dict] = []
    seen_uid: dict[str, int] = {}
    seen_key: dict[tuple[str, str, str], int] = {}

    for entry in rows:
        if entry.skip:
            outcomes.append(
                RowOutcome(row=entry.row, clause_uid="", status="skipped", reason=entry.skip, message=entry.message)
            )
            continue
        if entry.error:
            outcomes.append(
                RowOutcome(row=entry.row, clause_uid="", status="failed", reason=entry.error, message=entry.message)
            )
            continue

        data = entry.data or {}
        if _contains_sample_marker(data):
            outcomes.append(
                RowOutcome(
                    row=entry.row,
                    clause_uid=_text(data.get("clause_uid")),
                    status="failed",
                    reason="sample_only",
                    message=f"检测到 {SAMPLE_ONLY_MARKER} 标记：示例行不参与入库",
                    field="excerpt",
                )
            )
            continue
        if _text(data.get("clause_uid")) == GUIDE_ROW_MARKER:
            outcomes.append(
                RowOutcome(
                    row=entry.row,
                    clause_uid=GUIDE_ROW_MARKER,
                    status="skipped",
                    reason="field_guide",
                    message="模板字段说明行，不参与入库",
                )
            )
            continue

        try:
            item = clause_store.validate_clauses([data])[0]
        except clause_store.ClauseValidationError as exc:
            first = exc.errors[0]
            outcomes.append(
                RowOutcome(
                    row=entry.row,
                    clause_uid=_text(data.get("clause_uid")),
                    status="failed",
                    reason=f"invalid_{first['field']}",
                    message=first["message"],
                    field=first["field"],
                    errors=[{**err, "row": entry.row} for err in exc.errors],
                )
            )
            continue

        uid = item["clause_uid"]
        key = (item["standard_code_norm"], item["version"], item["clause_no_norm"])
        if uid in seen_uid:
            outcomes.append(
                RowOutcome(
                    row=entry.row,
                    clause_uid=uid,
                    status="failed",
                    reason="duplicate_in_batch",
                    message=f"clause_uid 与第 {seen_uid[uid]} 行重复：{uid}",
                    field="clause_uid",
                )
            )
            continue
        if key in seen_key:
            outcomes.append(
                RowOutcome(
                    row=entry.row,
                    clause_uid=uid,
                    status="failed",
                    reason="duplicate_in_batch",
                    message=(
                        f"标准号 + 版本 + 条款号与第 {seen_key[key]} 行重复："
                        f"{item['standard_code']} / {item['version']} / {item['clause_no']}"
                    ),
                    field="clause_no",
                )
            )
            continue

        try:
            existing = await _fetch_existing_by_uid(db, uid)
            if existing:
                if _same_content(existing, item):
                    outcomes.append(
                        RowOutcome(
                            row=entry.row,
                            clause_uid=uid,
                            status="skipped",
                            reason="duplicate_in_db",
                            message="库内已存在且内容一致，跳过（不覆盖）",
                        )
                    )
                    continue
                fields = _diff_fields(existing, item)
                outcomes.append(
                    RowOutcome(
                        row=entry.row,
                        clause_uid=uid,
                        status="failed",
                        reason="conflict_in_db",
                        message=f"clause_uid 已存在但内容不同（差异字段：{'、'.join(fields)}），不得覆盖",
                        field=fields[0] if fields else "clause_uid",
                    )
                )
                continue
            other_uid = await _fetch_uid_by_key(db, key)
            if other_uid:
                outcomes.append(
                    RowOutcome(
                        row=entry.row,
                        clause_uid=uid,
                        status="failed",
                        reason="conflict_in_db",
                        message=(
                            f"标准号 + 版本 + 条款号已存在（clause_uid={other_uid}），"
                            "同一自然键不允许挂两个 clause_uid，不得覆盖"
                        ),
                        field="clause_no",
                    )
                )
                continue
        except sqlite3.Error as exc:
            raise clause_store.ClauseValidationError(
                [{"row": 0, "field": "db", "message": f"条款库不可用：{exc}"}]
            ) from exc

        seen_uid[uid] = entry.row
        seen_key[key] = entry.row
        accepted_items.append(item)
        outcomes.append(
            RowOutcome(
                row=entry.row,
                clause_uid=uid,
                status="accepted",
                reason="ok",
                message="校验通过（dry_run，未入库）" if dry_run else "校验通过",
            )
        )

    failed = sum(1 for item in outcomes if item.status == "failed")
    skipped = sum(1 for item in outcomes if item.status == "skipped")
    accepted = sum(1 for item in outcomes if item.status == "accepted")

    committed = False
    inserted = 0
    message = ""

    if dry_run:
        if failed:
            message = "dry_run=true：仅预检，未写入条款库。" + _reject_message(failed, len(rows))
        else:
            message = "dry_run=true：仅预检，未写入条款库。全部通过，可用于正式导入。"
    elif failed:
        message = _reject_message(failed, len(rows))
    elif not confirm_license:
        raise clause_store.ClauseValidationError(
            [{"row": 0, "field": "confirm_license", "message": "未确认版权许可（confirm_license=true 必填）"}]
        )
    elif not accepted_items:
        message = "本批无新增条目（全部已存在或跳过），未写入条款库。"
    else:
        try:
            await clause_store.import_clauses(
                db,
                accepted_items,
                confirm_license=True,
                batch_id=batch,
            )
        except clause_store.ClauseValidationError as exc:
            # 并发/竞态下的唯一键冲突：import_clauses 已整批回滚。
            for item in outcomes:
                if item.status == "accepted":
                    item.status = "failed"
                    item.reason = "db_integrity"
                    item.message = str(exc)
            failed = sum(1 for item in outcomes if item.status == "failed")
            accepted = sum(1 for item in outcomes if item.status == "accepted")
            message = "入库失败，整批已回滚：" + str(exc)
        else:
            committed = True
            inserted = accepted
            message = f"整批提交成功，写入 {inserted} 条。"

    failures_by_reason = Counter(item.reason for item in outcomes if item.status == "failed")
    skips_by_reason = Counter(item.reason for item in outcomes if item.status == "skipped")
    failure_rows: dict[str, list[int]] = {}
    for item in outcomes:
        if item.status == "failed":
            failure_rows.setdefault(item.reason, []).append(item.row)

    return ImportReport(
        dry_run=dry_run,
        committed=committed,
        total=len(rows),
        accepted=accepted,
        failed=failed,
        skipped=skipped,
        inserted=inserted,
        batch_id=batch,
        message=message,
        failures_by_reason=dict(failures_by_reason),
        failure_rows=failure_rows,
        skips_by_reason=dict(skips_by_reason),
        outcomes=outcomes,
    )


async def import_csv(
    db,
    text: str,
    *,
    confirm_license: bool = False,
    dry_run: bool = True,
    batch_id: str = "",
) -> ImportReport:
    """CSV 便捷入口：解析（保留物理行号）后走同一 ``batch_import``。"""
    return await batch_import(
        db,
        parse_csv_text(text),
        confirm_license=confirm_license,
        dry_run=dry_run,
        batch_id=batch_id,
    )


async def import_json(
    db,
    payload: str | list | dict,
    *,
    confirm_license: bool = False,
    dry_run: bool = True,
    batch_id: str = "",
) -> ImportReport:
    """JSON 便捷入口：解析（数组序号作行号）后走同一 ``batch_import``。"""
    return await batch_import(
        db,
        parse_json_payload(payload),
        confirm_license=confirm_license,
        dry_run=dry_run,
        batch_id=batch_id,
    )
