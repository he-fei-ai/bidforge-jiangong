"""schemes 审核状态解耦 + 一致性扫描结果聚合进 overview 的不变量测试（2026-09-17）。

不变量：
1. /submit 只写 schemes.review_status，绝不触碰编译状态 status；
2. 数据迁移把历史被污染的英文审核态从 status 搬运到 review_status（幂等）；
3. overview 聚合最近一批一致性扫描中未解决（pending/failed）冲突，
   repaired/accepted/skipped 不计分。
"""
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.models import SchemeReviewIn
from app.routers.compliance import readiness_overview
from app.routers.review import review_summary, submit_scheme_review


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "review-decouple.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)",
        (sid, pid, "s", "目录已确认"))
    await db.commit()
    yield db, pid, sid
    # ✅ 关闭全局连接：避免 aiosqlite 非守护 worker 线程阻塞解释器退出
    await close_db()


async def test_submit_writes_review_status_not_compile_status(db_ctx):
    """/submit 推进审核态时，编译状态必须原样保留。"""
    db, _pid, sid = db_ctx
    await submit_scheme_review(
        sid, SchemeReviewIn(to_status="pending", reviewer="张工", comment=""), db)
    cur = await db.execute("SELECT status, review_status FROM schemes WHERE id=?", (sid,))
    row = await cur.fetchone()
    assert row["review_status"] == "pending"
    assert row["status"] == "目录已确认"  # 编译状态不被覆盖


async def test_summary_returns_both_statuses(db_ctx):
    db, _pid, sid = db_ctx
    await submit_scheme_review(
        sid, SchemeReviewIn(to_status="reviewing", reviewer="张工", comment=""), db)
    summary = await review_summary(sid, db)
    assert summary["review_status"] == "reviewing"
    assert summary["review_status_label"] == "审核中"
    assert summary["scheme_status"] == "目录已确认"


async def test_migration_moves_polluted_status(tmp_path, monkeypatch):
    """历史被英文审核态污染的 status 行：迁移搬运到 review_status 并还原编译态（幂等）。"""
    _appdb.DB_PATH = tmp_path / "mig.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,'approved')",
        (sid, pid, "s"))
    await db.commit()
    await init_db()  # 二次执行触发数据迁移
    cur = await db.execute("SELECT status, review_status FROM schemes WHERE id=?", (sid,))
    row = await cur.fetchone()
    assert row["review_status"] == "approved"
    assert row["status"] == "目录已确认"
    await init_db()  # 幂等：重复执行不二次改动
    cur = await db.execute("SELECT status, review_status FROM schemes WHERE id=?", (sid,))
    row = await cur.fetchone()
    assert row["review_status"] == "approved"
    assert row["status"] == "目录已确认"


def _insert_conflict(db, sid, scan_id, cid, status, severity="high",
                     created_at="2026-09-17 10:00:00"):
    return db.execute(
        "INSERT INTO consistency_conflicts (id, scheme_id, scan_id, conflict_type,"
        " severity, topic, occurrences, authoritative_value, authoritative_source,"
        " repair_instruction, reason, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, sid, scan_id, "numeric_conflict", severity, "工期",
         '["A章节:90天","B章节:60天"]', "90天", "全局事实",
         "以全局事实为准统一工期", "两处工期取值矛盾", status, created_at))


async def test_overview_counts_only_unresolved_scan_conflicts(db_ctx):
    """pending/failed 计分，repaired/accepted/skipped 不计分。"""
    db, _pid, sid = db_ctx
    scan_id = "scan-1"
    await _insert_conflict(db, sid, scan_id, "c1", "pending", severity="high")
    await _insert_conflict(db, sid, scan_id, "c2", "repaired")
    await _insert_conflict(db, sid, scan_id, "c3", "skipped")
    await db.commit()

    payload = await readiness_overview(sid, db)
    assert "consistency_scan" in payload["sources"]
    scan_findings = [f for f in payload["findings"]
                     if str(f.get("rule_id", "")).startswith("CON-SCAN-")]
    assert len(scan_findings) == 1
    f = scan_findings[0]
    assert f["rule_id"] == "CON-SCAN-1"
    assert f["dimension"] == "consistency"
    assert f["severity"] == "high"
    assert f["mode"] == "program"
    assert f["evidence"] == ["A章节:90天", "B章节:60天"]


async def test_overview_without_scan_has_no_scan_source(db_ctx):
    db, _pid, sid = db_ctx
    payload = await readiness_overview(sid, db)
    assert "consistency_scan" not in payload["sources"]


async def test_submit_rejects_stale_preflight(db_ctx):
    """BUG#1 回归：正文变更早于最近一次预检时，提交审核应被过期校验拦截。"""
    from fastapi import HTTPException
    db, _pid, sid = db_ctx
    # 预检结论（blocked=0，未阻断），时间早于正文变更
    await db.execute(
        "INSERT INTO preflight_runs (id, scheme_id, rule_version, total, grade, "
        "verdict, released, blocked, counts, dimensions, findings, stats, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "1.0.0", 0, "A", "pass", 1, 0,
         "{}", "[]", "[]", "{}", "2026-09-17 10:00:00"))
    # 章节正文在预检之后才生成（updated_at 更新）
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, level, status, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "第一章", 1, "generated", "2026-09-18T11:00:00"))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await submit_scheme_review(
            sid, SchemeReviewIn(to_status="approved", reviewer="张工", comment=""), db)
    assert exc.value.status_code == 422
    assert "过期" in exc.value.detail


async def test_submit_accepts_fresh_preflight(db_ctx):
    """预检晚于正文变更时，不应被过期校验拦截（仅校验 blocked）。"""
    db, _pid, sid = db_ctx
    await db.execute(
        "INSERT INTO preflight_runs (id, scheme_id, rule_version, total, grade, "
        "verdict, released, blocked, counts, dimensions, findings, stats, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "1.0.0", 0, "A", "pass", 1, 0,
         "{}", "[]", "[]", "{}", "2026-09-18T11:00:00"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, level, status, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "第一章", 1, "generated", "2026-09-17 10:00:00"))
    await db.commit()
    res = await submit_scheme_review(
        sid, SchemeReviewIn(to_status="approved", reviewer="张工", comment=""), db)
    assert res["ok"] is True
    assert res["to_status"] == "approved"
