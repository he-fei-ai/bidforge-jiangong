"""R179 Phase C（2026-11-06）：统一缺陷账本 + 建议权/执行权分离。

锁定点
------
1. 幂等同步：同一缺陷跨轮次只收敛一行；只读引用 preflight_runs / consistency_audit，
   同步与执行都不回写源表。
2. 禁改白名单：安全数值 / 计算书结论 / 审批主体 / 强制性条文四类，apply 必须 409
   且不落任何写入；只能走 decision（人的裁决权）。
3. 检查器异常必须落 verdict=undetermined，且 summary.passed 只统计 verdict=yes。
4. before_text / after_text 全文保存，并排 diff 不截断。
5. 开关默认关闭；关闭时全部入口 404。
6. 新表纳入项目/方案级联清理；老库 _migrate 路径幂等且不动 user_version。
"""

from __future__ import annotations

import json
import uuid

import aiosqlite
import pytest
from app.config import Settings, settings
from app.db import _migrate
from app.routers import defects as defects_router
from app.routers.projects import delete_project
from app.services import defect_ledger as ledger
from app.services.fix_policy import PROTECTED_CATEGORIES, classify_fix_policy
from fastapi import HTTPException


def _uid() -> str:
    return uuid.uuid4().hex


@pytest.fixture
async def ctx(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "defect_ledger_enabled", True, raising=False)
    pid, sid, sec_id = _uid(), _uid(), _uid()
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "项目"))
    await db_conn.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "方案", "目录已确认"),
    )
    await db_conn.execute(
        "INSERT INTO sections(id,scheme_id,project_id,title,content,word_budget)" " VALUES(?,?,?,?,?,?)",
        (sec_id, sid, pid, "工程概况", "原文第一行\n原文第二行", 1200),
    )
    await db_conn.commit()
    yield db_conn, pid, sid, sec_id


async def _insert_preflight(db, sid: str, pid: str, findings: list) -> str:
    run_id = _uid()
    await db.execute(
        "INSERT INTO preflight_runs(id, scheme_id, project_id, findings, created_at)"
        " VALUES(?,?,?,?,datetime('now','localtime'))",
        (run_id, sid, pid, json.dumps(findings, ensure_ascii=False)),
    )
    await db.commit()
    return run_id


async def _count(db, table: str) -> int:
    cur = await db.execute(f"SELECT COUNT(*) AS c FROM {table}")
    return int((await cur.fetchone())[0])


# ---------------------------------------------------------------------------
# 1. 幂等同步 + 只读引用
# ---------------------------------------------------------------------------


async def test_sync_is_idempotent_and_engine_error_is_undetermined(ctx):
    db, pid, sid, sec_id = ctx
    findings = [
        {
            "rule_id": "DLV-06",
            "dimension": "deliverability",
            "severity": "medium",
            "title": "存在口语化表述",
            "detail": "正文包含口语词",
            "section_id": sec_id,
            "section_title": "工程概况",
            "suggestion": "改为书面语",
        },
        {
            "rule_id": "",
            "dimension": "",
            "severity": "low",
            "title": "检查项 check_xxx 执行异常（该项未参与本次预检）",
            "detail": "RuntimeError: boom",
            "section_id": "",
            "suggestion": "重跑一键总检",
            "engine_error": True,
        },
    ]
    run_id = await _insert_preflight(db, sid, pid, findings)
    before_run = await _count(db, "preflight_runs")

    first = await ledger.sync_scheme(db, sid, include_preflight=True, include_consistency=False)
    second = await ledger.sync_scheme(db, sid, include_preflight=True, include_consistency=False)
    assert first["sources"][0]["created"] == 2
    assert second["sources"][0]["created"] == 0
    assert second["sources"][0]["updated"] == 2
    assert await _count(db, "scheme_defects") == 2
    assert await _count(db, "preflight_runs") == before_run

    rows = await ledger.list_defects(db, sid)
    engine = next(r for r in rows if r["title"].startswith("检查项"))
    normal = next(r for r in rows if r["rule_id"] == "DLV-06")
    assert engine["verdict"] == ledger.VERDICT_UNDETERMINED
    assert all(r["source_run_id"] == run_id for r in rows)
    assert all(r["source_table"] == "preflight_runs" for r in rows)
    assert normal["before_text"] == "原文第一行\n原文第二行"

    summary = await ledger.ledger_summary(db, sid)
    assert summary["passed"] == 0
    assert summary["undetermined"] == 1
    assert summary["pending"] == 1


async def test_consistency_sync_reads_source_only(ctx):
    db, pid, sid, sec_id = ctx
    audit_id = _uid()
    issues = [
        {
            "conflict_type": "numeric",
            "topic": "基坑深度",
            "value": "5",
            "text": "深度 6m",
            "section_occurrences": [{"section_id": sec_id, "section_title": "工程概况", "text": "深度 6m"}],
        }
    ]
    await db.execute(
        "INSERT INTO consistency_audit(id, project_id, scheme_id, issues)" " VALUES(?,?,?,?)",
        (audit_id, pid, sid, json.dumps(issues, ensure_ascii=False)),
    )
    await db.commit()
    source_before = await db.execute("SELECT issues FROM consistency_audit WHERE id=?", (audit_id,))
    source_before = dict(await source_before.fetchone())

    out = await ledger.sync_scheme(db, sid, include_preflight=False, include_consistency=True)
    assert out["sources"][0]["created"] == 1
    rows = await ledger.list_defects(db, sid)
    assert rows[0]["source_table"] == "consistency_audit"
    assert rows[0]["source_run_id"] == audit_id
    assert rows[0]["rule_id"] == "CON-07"
    assert rows[0]["before_text"] == "原文第一行\n原文第二行"

    source_after = await db.execute("SELECT issues FROM consistency_audit WHERE id=?", (audit_id,))
    assert dict(await source_after.fetchone()) == source_before


# ---------------------------------------------------------------------------
# 2. 四类禁改白名单：apply 409 且零写入；decide accept 才可执行
# ---------------------------------------------------------------------------

_PROTECTED_CASES = [
    ("safety_value", "安全系数取值偏低", "SAF-08"),
    ("calculation_conclusion", "计算书结论有误", "TRC-01"),
    ("approval_subject", "审批主体不明确", "DLV-09"),
    ("mandatory_clause", "未引用强制性条文", "STD-02"),
]


def test_protected_categories_cover_four_required_kinds():
    assert set(PROTECTED_CATEGORIES) == {
        "safety_value",
        "calculation_conclusion",
        "approval_subject",
        "mandatory_clause",
    }
    for _, title, rule_id in _PROTECTED_CASES:
        policy = classify_fix_policy(rule_id=rule_id, title=title)
        assert policy["policy"] == "protected"


@pytest.mark.parametrize("category,title,rule_id", _PROTECTED_CASES)
async def test_protected_apply_returns_409_without_any_write(ctx, category, title, rule_id):
    db, pid, sid, sec_id = ctx
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title=title,
        rule_id=rule_id,
        section_id=sec_id,
        before_text="原文第一行\n原文第二行",
        after_text="改写后的全文",
    )
    assert row["fix_policy"] == "protected"
    status_before = row["status"]
    decisions_before = await _count(db, "fix_decisions")

    with pytest.raises(HTTPException) as exc:
        await defects_router.apply_defect_fix(row["id"], db=db)
    assert exc.value.status_code == 409
    assert category in exc.value.detail["categories"]

    cur = await db.execute("SELECT content FROM sections WHERE id=?", (sec_id,))
    assert dict(await cur.fetchone())["content"] == "原文第一行\n原文第二行"
    assert await _count(db, "fix_decisions") == decisions_before
    after = await ledger.get_defect(db, row["id"])
    assert after["status"] == status_before
    assert after["after_text"] == "改写后的全文"


async def test_protected_can_only_be_executed_by_decision(ctx):
    db, pid, sid, sec_id = ctx
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="安全系数取值偏低",
        rule_id="SAF-08",
        section_id=sec_id,
        before_text="原文第一行\n原文第二行",
        after_text="安全系数调整为 1.25",
    )
    with pytest.raises(ledger.ProtectedDefectError):
        await ledger.apply_fix(db, row["id"])

    out = await ledger.decide_fix(db, row["id"], decision="accept", decided_by="项目总工", reason="人工确认")
    assert out["section_updated"] is True
    cur = await db.execute("SELECT content FROM sections WHERE id=?", (sec_id,))
    assert dict(await cur.fetchone())["content"] == "安全系数调整为 1.25"
    dec = await db.execute("SELECT decision, applied FROM fix_decisions WHERE defect_id=?", (row["id"],))
    d = dict(await dec.fetchone())
    assert d["decision"] == "accept" and d["applied"] == 1


# ---------------------------------------------------------------------------
# 3. 自由缺陷：apply 写入全文；diff 全文不截断
# ---------------------------------------------------------------------------


async def test_free_apply_writes_full_text(ctx):
    db, pid, sid, sec_id = ctx
    after = "\n".join(f"第{i}行：" + "正文内容" * 40 for i in range(30))
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="正文存在口语化表述",
        rule_id="DLV-06",
        section_id=sec_id,
        before_text="原文第一行\n原文第二行",
        after_text=after,
    )
    assert row["fix_policy"] == "free"
    out = await ledger.apply_fix(db, row["id"], applied_by="editor")
    assert out["section_updated"] is True

    cur = await db.execute("SELECT content, word_count FROM sections WHERE id=?", (sec_id,))
    saved = dict(await cur.fetchone())
    assert saved["content"] == after
    assert len(saved["content"]) == len(after)

    dec = await db.execute("SELECT before_text, after_text, applied FROM fix_decisions WHERE defect_id=?", (row["id"],))
    d = dict(await dec.fetchone())
    assert d["before_text"] == "原文第一行\n原文第二行"
    assert d["after_text"] == after
    assert d["applied"] == 1


async def test_diff_keeps_full_before_after(ctx):
    db, pid, sid, sec_id = ctx
    before = "\n".join(f"原第{i}行" for i in range(40))
    after = "\n".join(f"新第{i}行" for i in range(45))
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="正文存在口语化表述",
        rule_id="DLV-06",
        section_id=sec_id,
        before_text=before,
        after_text=after,
    )
    diff = await defects_router.get_defect_diff(row["id"], db=db)
    assert diff["before_text"] == before
    assert diff["after_text"] == after
    assert diff["before_chars"] == len(before)
    assert diff["after_chars"] == len(after)
    assert diff["truncated"] is False
    assert len(diff["rows"]) >= 40


# ---------------------------------------------------------------------------
# 4. 检查器异常 / 未注册 → undetermined，且不计入 passed
# ---------------------------------------------------------------------------


async def test_checker_exception_becomes_undetermined_and_not_passed(ctx):
    db, pid, sid, sec_id = ctx
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="正文存在口语化表述",
        rule_id="DLV-06",
        section_id=sec_id,
        before_text="原文",
        after_text="改写",
    )

    async def boom(_defect):
        raise RuntimeError("checker blew up")

    updated = await ledger.verify_defect(db, row["id"], checker=boom)
    assert updated["verdict"] == ledger.VERDICT_UNDETERMINED
    assert "checker blew up" in updated["verdict_reason"]

    summary = await ledger.ledger_summary(db, sid)
    assert summary["passed"] == 0, "undetermined 绝不能被计入通过"
    assert summary["undetermined"] == 1


async def test_unregistered_checker_is_undetermined(ctx):
    db, pid, sid, sec_id = ctx
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="未注册检查器的缺陷",
        rule_id="ZZZ-99",
    )
    updated = await ledger.verify_defect(db, row["id"])
    assert updated["verdict"] == ledger.VERDICT_UNDETERMINED
    assert "未注册检查器" in updated["verdict_reason"]


# ---------------------------------------------------------------------------
# 5. 开关默认关闭 + 关闭时全部入口 404
# ---------------------------------------------------------------------------


def test_switch_default_off():
    assert Settings.model_fields["defect_ledger_enabled"].default is False


@pytest.mark.parametrize(
    "handler,kwargs",
    [
        (defects_router.list_scheme_defects, {"scheme_id": "s"}),
        (defects_router.scheme_defect_summary, {"scheme_id": "s"}),
        (defects_router.get_defect_detail, {"defect_id": "d"}),
    ],
)
async def test_disabled_routes_404(db_conn, monkeypatch, handler, kwargs):
    monkeypatch.setattr(settings, "defect_ledger_enabled", False, raising=False)
    with pytest.raises(HTTPException) as exc:
        await handler(db=db_conn, **kwargs)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 6. 项目级联 + 老库迁移
# ---------------------------------------------------------------------------


async def test_project_delete_cascades_ledger_tables(ctx):
    db, pid, sid, sec_id = ctx
    row, _ = await ledger.upsert_defect(
        db,
        scheme_id=sid,
        project_id=pid,
        title="正文存在口语化表述",
        rule_id="DLV-06",
        after_text="改写",
    )
    await ledger.decide_fix(db, row["id"], decision="defer", decided_by="tester")
    assert await _count(db, "scheme_defects") == 1
    assert await _count(db, "fix_decisions") == 1

    await delete_project(pid, db=db)
    assert await _count(db, "scheme_defects") == 0
    assert await _count(db, "fix_decisions") == 0


async def test_old_db_upgrade_is_idempotent_and_keeps_user_version(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    cur = await conn.execute("PRAGMA user_version")
    before = (await cur.fetchone())[0]

    await _migrate(conn)
    await _migrate(conn)  # 第二次必须同样通过（全 IF NOT EXISTS）

    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN" " ('scheme_defects','fix_decisions')"
    )
    names = {r[0] for r in await cur.fetchall()}
    assert names == {"scheme_defects", "fix_decisions"}
    cur = await conn.execute("PRAGMA index_list(scheme_defects)")
    indexes = {r[1] for r in await cur.fetchall()}
    assert "uq_scheme_defects_fingerprint" in indexes
    cur = await conn.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == before, "user_version 只读语义不得改变"
    await conn.close()


def test_ledger_ddl_only_adds_new_tables():
    from app.schema_sql import DEFECT_LEDGER_DDL

    assert "CREATE TABLE IF NOT EXISTS scheme_defects" in DEFECT_LEDGER_DDL
    assert "CREATE TABLE IF NOT EXISTS fix_decisions" in DEFECT_LEDGER_DDL
    assert "ALTER TABLE" not in DEFECT_LEDGER_DDL.upper()
    assert "DROP TABLE" not in DEFECT_LEDGER_DDL.upper()
