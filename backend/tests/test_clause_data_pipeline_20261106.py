"""R179 · 条款库数据准备链路（2026-11-06）。

验收线
------
1. 模板只有表头 + 一行字段说明，**零示例条文**；``SAMPLE_ONLY`` 行一律拒绝入库；
2. 批量导入逐行返回成功 / 失败 / 跳过 + 原因 + 行号，``dry_run`` 默认 true；
3. 批内重复、库内重复、同 ``clause_uid`` 内容冲突（报冲突不覆盖）；
4. 任一行失败 → 整批不提交，并在报告中说明为什么不用「部分成功」；
5. 端到端：导入夹具条款 → ``clause_store.search_clauses`` →
   ``section_router.search_for_section`` → 数值锁按条款分组拦截。
"""

from __future__ import annotations

import csv
import inspect
import io
import json

import pytest
from app.config import settings
from app.routers import standards_admin
from app.services import clause_import, clause_store, section_router
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _clause(**overrides) -> dict:
    row = {
        "clause_uid": "c-5-2-1",
        "standard_code": "GB 50007-2011",
        "standard_name": "建筑地基基础设计规范",
        "version": "2011",
        "clause_no": "5.2.1",
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
        "status": "active",
    }
    row.update(overrides)
    return row


def _csv_with(rows: list[dict]) -> str:
    """在模板（表头 + 说明行）之后追加数据行，保留真实物理行号。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(clause_import.TEMPLATE_COLUMNS),
        lineterminator="\r\n",
    )
    for row in rows:
        writer.writerow(row)
    return clause_import.build_template_csv() + buffer.getvalue()


async def _count(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM standard_clauses")
    return int((await cursor.fetchone())[0])


# ---------------------------------------------------------------------------
# 1. 模板
# ---------------------------------------------------------------------------


def test_csv_template_is_header_plus_one_field_guide_row_only():
    text = clause_import.build_template_csv()
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].split(",") == list(clause_import.TEMPLATE_COLUMNS)
    assert clause_import.GUIDE_ROW_MARKER in lines[1]
    # 零示例条文：模板内不得出现任何测试用条文，也不得出现 SAMPLE_ONLY 之外的示例。
    assert "基础底面压力" not in text
    assert "SAMPLE_ONLY" not in text


def test_template_covers_six_mandatory_and_source_fields():
    columns = set(clause_import.TEMPLATE_COLUMNS)
    for required in ("standard_code", "version", "clause_no", "excerpt", "applicability", "license_note"):
        assert required in columns
    for extra in ("source_kind", "page_ref", "source_url", "standard_name", "clause_title"):
        assert extra in columns


def test_json_template_has_empty_clauses_and_one_field_guide_row():
    payload = clause_import.build_template_json()
    assert payload["columns"] == list(clause_import.TEMPLATE_COLUMNS)
    assert payload["clauses"] == []
    assert len(payload["field_guide"]) == 1
    assert payload["field_guide"][0]["clause_uid"] == clause_import.GUIDE_ROW_MARKER
    assert "基础底面压力" not in str(payload)


async def test_template_round_trip_only_skips_guide_row(db_conn):
    report = await clause_import.import_csv(
        db_conn, clause_import.build_template_csv(), confirm_license=False, dry_run=True
    )
    assert report.total == 1
    assert report.skipped == 1
    assert report.failed == 0
    assert report.outcomes[0].reason == "field_guide"
    assert await _count(db_conn) == 0


# ---------------------------------------------------------------------------
# 2. SAMPLE_ONLY 拒绝
# ---------------------------------------------------------------------------


async def test_sample_only_row_is_rejected_and_never_imported(db_conn):
    row = _clause(
        clause_uid="sample-uid",
        excerpt="SAMPLE_ONLY 这是示例条文，禁止入库。",
    )
    report = await clause_import.batch_import(db_conn, [row], confirm_license=True, dry_run=False)
    assert report.failed == 1
    assert report.committed is False
    assert report.failures_by_reason == {"sample_only": 1}
    assert await _count(db_conn) == 0


async def test_sample_only_marker_is_case_insensitive(db_conn):
    row = _clause(clause_uid="sample-lower", excerpt="sample_only 示例")
    report = await clause_import.batch_import(db_conn, [row], confirm_license=True, dry_run=False)
    assert report.failures_by_reason == {"sample_only": 1}


async def test_sample_only_in_json_payload_is_rejected(db_conn):
    payload = {"clauses": [_clause(clause_uid="json-sample", excerpt="SAMPLE_ONLY 示例")]}
    report = await clause_import.import_json(db_conn, payload, confirm_license=True, dry_run=False)
    assert report.failed == 1
    assert await _count(db_conn) == 0


# ---------------------------------------------------------------------------
# 3. 批量导入：dry_run 默认、逐行行号、报告分组
# ---------------------------------------------------------------------------


def test_dry_run_defaults_to_true():
    assert inspect.signature(clause_import.batch_import).parameters["dry_run"].default is True
    assert standards_admin.BatchImportIn().dry_run is True


async def test_dry_run_never_writes(db_conn):
    report = await clause_import.batch_import(db_conn, [_clause()], confirm_license=False, dry_run=True)
    assert report.accepted == 1
    assert report.committed is False
    assert report.inserted == 0
    assert "dry_run=true" in report.message
    assert await _count(db_conn) == 0


async def test_per_row_results_carry_physical_line_numbers(db_conn):
    valid = _clause(clause_uid="ok-1", clause_no="5.2.1")
    invalid = _clause(clause_uid="bad-1", clause_no="5.2.2", version="")
    text = _csv_with([valid, invalid])
    report = await clause_import.import_csv(db_conn, text, dry_run=True)

    assert report.total == 3
    assert report.accepted == 1
    assert report.failed == 1
    assert report.skipped == 1
    assert report.failure_rows == {"invalid_version": [4]}
    assert [item.row for item in report.outcomes] == [2, 3, 4]
    assert report.outcomes[2].field == "version"


async def test_report_groups_failures_by_reason(db_conn):
    rows = [
        _clause(clause_uid="v1", clause_no="5.2.1", version=""),
        _clause(clause_uid="v2", clause_no="5.2.2", version=""),
        _clause(clause_uid="a1", clause_no="5.2.3", applicability=""),
    ]
    report = await clause_import.batch_import(db_conn, rows, dry_run=True)
    assert report.total == 3
    assert report.failed == 3
    assert report.failures_by_reason == {"invalid_version": 2, "invalid_applicability": 1}
    assert report.failure_rows["invalid_version"] == [1, 2]
    assert report.failure_rows["invalid_applicability"] == [3]


async def test_multi_error_row_uses_first_error_for_grouping(db_conn):
    row = _clause(clause_uid="multi", clause_no="5.2.1", version="", applicability="")
    report = await clause_import.batch_import(db_conn, [row], dry_run=True)
    assert report.failures_by_reason == {"invalid_version": 1}
    assert {item["field"] for item in report.outcomes[0].errors} == {"version", "applicability"}


# ---------------------------------------------------------------------------
# 4. 重复与冲突
# ---------------------------------------------------------------------------


async def test_duplicate_clause_uid_within_batch(db_conn):
    rows = [
        _clause(clause_uid="dup", clause_no="5.2.1"),
        _clause(clause_uid="dup", clause_no="5.2.2"),
    ]
    report = await clause_import.batch_import(db_conn, rows, dry_run=True)
    assert report.accepted == 1
    assert report.failed == 1
    assert report.failure_rows == {"duplicate_in_batch": [2]}


async def test_duplicate_natural_key_within_batch(db_conn):
    rows = [
        _clause(clause_uid="a", clause_no="5.2.1"),
        _clause(clause_uid="b", clause_no="5.2.1"),
    ]
    report = await clause_import.batch_import(db_conn, rows, dry_run=True)
    assert report.accepted == 1
    assert report.failed == 1
    assert report.failure_rows == {"duplicate_in_batch": [2]}


async def test_identical_library_row_is_skipped_not_conflict(db_conn):
    row = _clause(clause_uid="same", clause_no="5.2.1")
    first = await clause_import.batch_import(db_conn, [row], confirm_license=True, dry_run=False)
    assert first.committed is True and first.inserted == 1

    second = await clause_import.batch_import(db_conn, [row], confirm_license=True, dry_run=False)
    assert second.skipped == 1
    assert second.committed is False
    assert second.skips_by_reason == {"duplicate_in_db": 1}
    assert await _count(db_conn) == 1


async def test_same_uid_different_content_is_conflict_not_overwrite(db_conn):
    original = _clause(clause_uid="conflict", clause_no="5.2.1")
    await clause_import.batch_import(db_conn, [original], confirm_license=True, dry_run=False)

    changed = _clause(clause_uid="conflict", clause_no="5.2.1", excerpt="改动后的摘录，不得覆盖。")
    report = await clause_import.batch_import(db_conn, [changed], confirm_license=True, dry_run=False)
    assert report.failed == 1
    assert report.committed is False
    assert report.failure_rows == {"conflict_in_db": [1]}

    cursor = await db_conn.execute("SELECT excerpt FROM standard_clauses WHERE clause_uid='conflict'")
    assert (await cursor.fetchone())[0] == original["excerpt"]


async def test_same_natural_key_with_different_uid_is_conflict(db_conn):
    await clause_import.batch_import(
        db_conn, [_clause(clause_uid="first", clause_no="5.2.1")], confirm_license=True, dry_run=False
    )
    report = await clause_import.batch_import(
        db_conn, [_clause(clause_uid="second", clause_no="5.2.1")], confirm_license=True, dry_run=False
    )
    assert report.failed == 1
    assert report.failure_rows == {"conflict_in_db": [1]}
    assert await _count(db_conn) == 1


# ---------------------------------------------------------------------------
# 5. 全成或全不成 + 许可确认
# ---------------------------------------------------------------------------


async def test_any_failure_rejects_whole_batch(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    rows = [
        _clause(clause_uid="good", clause_no="5.2.1"),
        _clause(clause_uid="bad", clause_no="5.2.2", source_url="", page_ref=""),
    ]
    report = await clause_import.batch_import(db_conn, rows, confirm_license=True, dry_run=False)
    assert report.accepted == 1
    assert report.failed == 1
    assert report.committed is False
    assert report.inserted == 0
    assert await _count(db_conn) == 0
    assert "整批未提交" in report.message
    assert "无法区分" in report.message
    assert "整批重导" in report.message


async def test_license_required_only_when_committing(db_conn):
    # dry_run 不需要许可确认（只预检）。
    preview = await clause_import.batch_import(db_conn, [_clause()], confirm_license=False, dry_run=True)
    assert preview.accepted == 1

    with pytest.raises(clause_store.ClauseValidationError) as excinfo:
        await clause_import.batch_import(db_conn, [_clause()], confirm_license=False, dry_run=False)
    assert excinfo.value.errors[0]["field"] == "confirm_license"


# ---------------------------------------------------------------------------
# 6. 路由：404 门禁 / 模板下载 / 批量导入
# ---------------------------------------------------------------------------


async def test_new_endpoints_return_404_when_disabled(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", False)
    with pytest.raises(HTTPException) as template_exc:
        await standards_admin.clause_template(file_format="csv")
    assert template_exc.value.status_code == 404
    with pytest.raises(HTTPException) as import_exc:
        await standards_admin.batch_import_clauses(standards_admin.BatchImportIn(clauses=[_clause()]), db=db_conn)
    assert import_exc.value.status_code == 404


async def test_template_endpoint_returns_csv_with_bom_and_field_guide(monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    response = await standards_admin.clause_template(file_format="csv")
    body = response.body.decode("utf-8")
    assert body.startswith("\ufeff")
    assert clause_import.GUIDE_ROW_MARKER in body
    assert "SAMPLE_ONLY" not in body
    assert response.media_type == "text/csv; charset=utf-8"


async def test_batch_import_endpoint_accepts_csv_text(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    payload = standards_admin.BatchImportIn(csv_text=_csv_with([_clause()]), dry_run=True)
    report = await standards_admin.batch_import_clauses(payload, db=db_conn)
    assert report["total"] == 2
    assert report["accepted"] == 1
    assert report["skipped"] == 1
    assert report["dry_run"] is True
    assert report["committed"] is False
    assert await _count(db_conn) == 0


async def test_batch_import_endpoint_accepts_clauses_json_and_commits(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    payload = standards_admin.BatchImportIn(clauses=[_clause()], dry_run=False, confirm_license=True)
    report = await standards_admin.batch_import_clauses(payload, db=db_conn)
    assert report["total"] == 1
    assert report["committed"] is True
    assert report["inserted"] == 1
    assert await _count(db_conn) == 1


async def test_template_endpoint_returns_json_template(monkeypatch):
    monkeypatch.setattr(settings, "clause_library_enabled", True)
    response = await standards_admin.clause_template(file_format="json")
    payload = json.loads(response.body)
    assert payload["columns"] == list(clause_import.TEMPLATE_COLUMNS)
    assert payload["clauses"] == []
    assert payload["field_guide"][0]["clause_uid"] == clause_import.GUIDE_ROW_MARKER


# ---------------------------------------------------------------------------
# 7. 端到端：导入 → 检索 → 分区检索 → 数值锁分组
# ---------------------------------------------------------------------------

_E2E_CLAUSE_A = {
    "clause_uid": "e2e-a",
    "standard_code": "GB 50202-2018",
    "standard_name": "建筑地基基础工程施工质量验收标准",
    "version": "2018",
    "clause_no": "第5.2.1条",
    "excerpt": "基础底面压力应符合设计要求，立杆间距不应大于 1.2m。",
    "excerpt_type": "summary",
    "mandatory": 1,
    "applicability": "天然地基",
    "license_note": "仅摘录要点，禁止全文转载",
    "source_url": "https://example.com/gb50202/5.2.1",
    "source_kind": "public",
}

_E2E_CLAUSE_B = {
    "clause_uid": "e2e-b",
    "standard_code": "GB 50202-2018",
    "standard_name": "建筑地基基础工程施工质量验收标准",
    "version": "2018",
    "clause_no": "第5.2.2条",
    "excerpt": "基础底面压力应符合设计要求，剪刀撑间距不应大于 6m。",
    "excerpt_type": "summary",
    "mandatory": 0,
    "applicability": "天然地基",
    "license_note": "仅摘录要点，禁止全文转载",
    "source_url": "https://example.com/gb50202/5.2.2",
    "source_kind": "public",
}


async def test_e2e_import_search_section_and_numeric_lock_grouping(db_conn):
    report = await clause_import.batch_import(
        db_conn, [_E2E_CLAUSE_A, _E2E_CLAUSE_B], confirm_license=True, dry_run=False
    )
    assert report.committed is True and report.inserted == 2

    found = await clause_store.search_clauses(db_conn, query="基础底面压力")
    assert found.status == "ok" and found.degraded is True
    assert {hit.clause_uid for hit in found.hits} == {"e2e-a", "e2e-b"}

    section = await section_router.search_for_section(db_conn, "基础底面压力", category_ids=("foundation_pit",))
    assert section.status == "ok" and len(section.hits) == 2

    route = section_router.SectionRoute(
        title="基础底面压力", content_type="standard", strategy="rag", fact_status="resolved"
    )
    scoped = section_router.allowed_numbers_for(route, section.hits)
    assert [group.uid for group in scoped.groups] == ["e2e-a", "e2e-b"]

    assert section_router.verify_standard_text("依据第5.2.1条，立杆间距 1.2m。", scoped).ok
    assert section_router.verify_standard_text("依据第5.2.2条，剪刀撑间距 6m。", scoped).ok

    blocked = section_router.verify_standard_text("依据第5.2.1条，剪刀撑间距 6m。", scoped)
    assert blocked.status == "blocked"
    assert [item["raw"] for item in blocked.violations] == ["6m"]

    undetermined = section_router.verify_standard_text("剪刀撑间距 6m。", scoped)
    assert undetermined.status == "undetermined"
    assert [item["raw"] for item in undetermined.undetermined] == ["6m"]
