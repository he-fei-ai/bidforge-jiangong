"""全局事实模块缺陷护栏（2026-10-03）。

P1-1：run_cross_check 不得静默清掉「结构化冲突候选但处于 XV 规则盲区」
      （普通文本值，非材料等级/流程序列/机械时序）的 has_conflict 标记。
P1-2：范围值兼容（不低于 C30 ↔ C35）经程序判定消解时，标记应正确清零。
P1-3：方案复制必须携带扩展列（page_ref/chapter 等溯源与四维标注），
      旧实现仅复制 16 列，溯源信息永久丢失。
"""
import json
import uuid

import pytest

import app.db as _appdb
from app.db import get_conn, init_db
import app.services.doc_pipeline.pipeline as pl
from app.routers.schemes import duplicate_scheme


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts_preserve.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    yield db, pid


async def _insert_fact(db, pid, **kw):
    fid = kw.pop("id", uuid.uuid4().hex)
    cols = ["id", "project_id", "group_id", "title", "content", "category",
            "is_resolved", "has_conflict"]
    vals = [fid, pid, kw.pop("group_id", "g1"), kw.pop("title", "t"),
            kw.pop("content", "c"), kw.pop("category", "other"),
            kw.pop("is_resolved", 1), kw.pop("has_conflict", 0)]
    for k, v in kw.items():
        cols.append(k)
        vals.append(v)
    ph = ",".join("?" * len(cols))
    db2 = db
    await db2.execute(
        f"INSERT INTO global_facts ({','.join(cols)}) VALUES ({ph})", vals)
    return fid


async def test_blind_text_conflict_is_preserved(ctx):
    """普通文本值的结构化冲突候选（XV 盲区）标记不得被清零。"""
    db, pid = ctx
    fid = await _insert_fact(
        db, pid, title="项目经理", content="- **项目经理**: 张三",
        category="personnel", has_conflict=1,
        conflict_keys=json.dumps(
            [{"value": "李四", "source": "例会纪要", "confidence": 0.9}]))
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")

    cur = await db.execute(
        "SELECT has_conflict, conflict_keys FROM global_facts WHERE id=?", (fid,))
    row = dict(await cur.fetchone())
    assert row["has_conflict"] == 1, "XV 盲区的普通文本冲突标记被静默清除"
    assert json.loads(row["conflict_keys"])[0]["value"] == "李四"
    assert fid in report["flagged_fact_ids"]


async def test_range_compatible_conflict_is_cleared(ctx):
    """范围值兼容（C35 满足「不低于 C30」）经程序判定消解 → 标记清零。"""
    db, pid = ctx
    fid = await _insert_fact(
        db, pid, title="混凝土强度等级",
        content="- **混凝土强度等级**: 不低于C30",
        category="material", has_conflict=1,
        conflict_keys=json.dumps(
            [{"value": "C35", "source": "设计文件", "confidence": 1.0}]))
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")

    cur = await db.execute(
        "SELECT has_conflict, conflict_keys FROM global_facts WHERE id=?", (fid,))
    row = dict(await cur.fetchone())
    assert row["has_conflict"] == 0
    assert fid not in report["flagged_fact_ids"]


async def test_flag_without_candidate_is_kept_failclosed(ctx):
    """无结构化候选的初始标记，XV 无证据消解时按契约保守保留。"""
    db, pid = ctx
    fid = await _insert_fact(
        db, pid, title="总工期", content="- **总工期**: 450天",
        category="schedule", has_conflict=1, conflict_keys="")
    await db.commit()

    report = await pl.run_cross_check(db, project_id=pid, doc_id="")

    cur = await db.execute(
        "SELECT has_conflict FROM global_facts WHERE id=?", (fid,))
    assert dict(await cur.fetchone())["has_conflict"] == 1
    assert fid in report["flagged_fact_ids"]


async def test_duplicate_scheme_carries_extended_columns(ctx):
    """方案复制必须携带 page_ref/chapter 等扩展列。"""
    db, pid = ctx
    sid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type, profession, status,"
        " word_budget, outline_source, config_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, pid, "源方案", "脚手架", "土建", "草稿", 10000, "ai", "{}"))
    await _insert_fact(
        db, pid, scheme_id=sid, group_id="g1", title="基坑深度",
        content="- **基坑深度**: 5m", category="basic",
        page_ref="P12", chapter="overview", fact_attr="quantitative",
        source_kind="survey", is_shared=1, evidence_kind="图纸",
        value_unit="m", is_safety_critical=1, norm_group="ng",
        fact_type="number", zone_type="A区")
    await db.commit()

    resp = await duplicate_scheme(project_id=pid, scheme_id=sid, db=db)
    new_sid = resp["id"]

    cur = await db.execute(
        "SELECT * FROM global_facts WHERE scheme_id=?", (new_sid,))
    row = dict(await cur.fetchone())
    assert row["page_ref"] == "P12", "page_ref 溯源信息丢失"
    assert row["chapter"] == "overview"
    assert row["fact_attr"] == "quantitative"
    assert row["source_kind"] == "survey"
    assert row["is_shared"] == 1
    assert row["evidence_kind"] == "图纸"
    assert row["value_unit"] == "m"
    assert row["is_safety_critical"] == 1
    assert row["norm_group"] == "ng"
    assert row["fact_type"] == "number"
    assert row["zone_type"] == "A区"


async def test_duplicate_scheme_group_id_remapped(ctx):
    """复制后分组 id 必须重映射，不复用原方案 group_id。"""
    db, pid = ctx
    sid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes (id, project_id, name, type, profession, status,"
        " word_budget, outline_source, config_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, pid, "源方案", "脚手架", "土建", "草稿", 10000, "ai", "{}"))
    await _insert_fact(
        db, pid, scheme_id=sid, group_id="ORIGINAL_GROUP",
        title="f", content="c")
    await db.commit()

    resp = await duplicate_scheme(project_id=pid, scheme_id=sid, db=db)

    cur = await db.execute(
        "SELECT group_id FROM global_facts WHERE scheme_id=?", (resp["id"],))
    new_group = (await cur.fetchone())[0]
    assert new_group != "ORIGINAL_GROUP"