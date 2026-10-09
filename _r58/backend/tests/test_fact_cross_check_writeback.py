"""全局事实交叉校验回归测试（2026-09-23）。

P2：bid_analysis_items（AI 解析项目）与 global_facts（全局事实）两套真值源
    同名取值冲突应被检出并回写 has_conflict。
P7：run_cross_check 的程序交叉校验冲突应**回写 global_facts.has_conflict**
    （旧实现只写 doc_validation_reports，标记不刷新）。
"""
import json
import uuid

import app.db as _appdb
import app.services.doc_pipeline.pipeline as pl
import pytest
from app.db import get_conn, init_db


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "xcheck.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid


async def test_p2_cross_source_conflict_flags_global_fact(ctx):
    """同名事实在 bid_analysis 与 global_facts 取值不一致 → 检出并标记。"""
    db, pid = ctx
    await db.execute(
        "INSERT INTO bid_analysis_items "
        "(id, project_id, item_id, label, content, status, output_type, required) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, "duration", "项目总工期", "365天",
         "success", "text", 1))
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, group_id, title, content, category, is_resolved, has_conflict) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fid, pid, "g1", "项目总工期", "- **项目总工期**: 400天",
         "schedule", 1, 0))
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")

    assert report["cross_source_conflicts"], "应检出双源同名取值冲突"
    assert report["cross_source_conflicts"][0]["rule_id"] == "XV-SRC-DUP"
    assert fid in report["flagged_fact_ids"]
    cur = await db.execute(
        "SELECT has_conflict FROM global_facts WHERE id=?", (fid,))
    assert dict(await cur.fetchone())["has_conflict"] == 1


async def test_p2_no_false_positive_when_values_match(ctx):
    """同名且取值一致时不误报冲突。"""
    db, pid = ctx
    await db.execute(
        "INSERT INTO bid_analysis_items "
        "(id, project_id, item_id, label, content, status, output_type, required) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, "duration", "项目总工期", "365天",
         "success", "text", 1))
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, group_id, title, content, category, is_resolved, has_conflict) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, "g1", "项目总工期", "- **项目总工期**: 365天",
         "schedule", 1, 0))
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")
    assert report["cross_source_conflicts"] == []


async def test_p7_within_pool_conflict_writeback(ctx):
    """P7：池内材料/设计参数冲突（C30 vs C35）回写 has_conflict=1。"""
    db, pid = ctx
    fa = uuid.uuid4().hex
    fb = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, group_id, title, content, category, is_resolved, "
        " has_conflict, conflict_keys) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (fa, pid, "g1", "混凝土强度等级", "- **混凝土强度等级**: C30", "material",
         1, 1,
         json.dumps([{"value": "C35", "source": "设计文件", "confidence": 1.0}])))
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, group_id, title, content, category, is_resolved, has_conflict) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fb, pid, "g2", "混凝土强度等级", "- **混凝土强度等级**: C35",
         "design_param", 1, 0))
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")

    # fb 原本 has_conflict=0，应被回写为 1（P7 修复点）
    assert fb in report["flagged_fact_ids"]
    cur = await db.execute(
        "SELECT has_conflict FROM global_facts WHERE id=?", (fb,))
    assert dict(await cur.fetchone())["has_conflict"] == 1
