"""跨模块数据传递：doc_pipeline.run_cross_check 回写 global_facts.has_conflict（2026-09-26）

覆盖：
- 跨源冲突（bid_analysis_items 与 global_facts 同名取值不一致）必须回写
  global_facts.has_conflict=1（跨模块数据链：解析项 → 全局事实矛盾标记）。
- ✅ 修复回归：run_cross_check 须先清零本项目 has_conflict 再按当前结果置 1，
  否则已消解的冲突会永久残留 has_conflict=1，导致前端矛盾徽标误报、下游
  (has_conflict=0) 注入门槛误杀事实。
"""
import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.services.doc_pipeline.pipeline import run_cross_check


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.execute("DELETE FROM global_facts WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM bid_analysis_items WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM doc_validation_reports WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _seed_fact(db, pid, sid, fid, title, value):
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, title, content, "
        "category, has_conflict, is_resolved) VALUES (?,?,?,?,?,?,?,?)",
        (fid, pid, sid, title, f"- **{title}**: {value}", "other", 0, 1))


async def _seed_bid(db, pid, bid_id, label, value):
    await db.execute(
        "INSERT INTO bid_analysis_items (id, project_id, item_id, label, content, "
        "status) VALUES (?,?,?,?,?,?)",
        (bid_id, pid, bid_id, label, f"{label}: {value}", "success"))


async def _gf_conflict(db, fid):
    cur = await db.execute(
        "SELECT has_conflict FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    return int(row[0]) if row else None


class TestCrossSourceConflictWritesHasConflict:
    async def test_conflict_flags_global_fact(self, ctx):
        db, pid, sid = ctx
        fid = uuid.uuid4().hex
        # 全局事实记为 C30，解析项记为 C35 → 同名取值冲突
        await _seed_fact(db, pid, sid, fid, "混凝土强度等级", "C30")
        await _seed_bid(db, pid, "bid_concrete", "混凝土强度等级", "C35")
        await db.commit()

        report = await run_cross_check(db, project_id=pid, doc_id="")
        assert fid in report["flagged_fact_ids"]
        assert await _gf_conflict(db, fid) == 1

    async def test_stale_flag_cleared_after_resolved(self, ctx):
        db, pid, sid = ctx
        fid = uuid.uuid4().hex
        await _seed_fact(db, pid, sid, fid, "混凝土强度等级", "C30")
        await _seed_bid(db, pid, "bid_concrete", "混凝土强度等级", "C35")
        await db.commit()
        await run_cross_check(db, project_id=pid, doc_id="")
        assert await _gf_conflict(db, fid) == 1  # 先确认被标记

        # 解析项取值被改一致（冲突消解）→ 再次交叉校验须清零
        await db.execute(
            "UPDATE bid_analysis_items SET content=? WHERE id=?",
            ("混凝土强度等级: C30", "bid_concrete"))
        await db.commit()
        report = await run_cross_check(db, project_id=pid, doc_id="")
        assert fid not in report["flagged_fact_ids"]
        assert await _gf_conflict(db, fid) == 0

    async def test_no_conflict_when_values_match(self, ctx):
        db, pid, sid = ctx
        fid = uuid.uuid4().hex
        await _seed_fact(db, pid, sid, fid, "混凝土强度等级", "C30")
        await _seed_bid(db, pid, "bid_concrete", "混凝土强度等级", "C30")
        await db.commit()
        report = await run_cross_check(db, project_id=pid, doc_id="")
        assert fid not in report["flagged_fact_ids"]
        assert await _gf_conflict(db, fid) == 0
