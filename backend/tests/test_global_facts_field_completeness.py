"""全局事实「信息调用完整性」回归测试（2026-09-23）。

覆盖 list_facts 刷新路径补齐提取期已落库、SSE 已下发的扩展字段：
value_unit / evidence_kind / page_ref / zone_type / norm_group / chunk_hash。
此前 list_facts 仅回传部分字段，前端刷新后丢失页码溯源/证据类型/计量单位/语义区，
造成「流式有、刷新无」的字段漂移（信息调用不完整）。
"""
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import pytest
from app.db import get_conn, init_db


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-fields.sqlite"
    await init_db()
    db = await get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid, uploads


async def _insert_fact(db, sid, pid, **extra):
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, title, content, category, "
        "value_unit, evidence_kind, page_ref, zone_type, norm_group, chunk_hash, "
        "fact_type, is_resolved) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "项目总工期", "- **项目总工期**: 365 天", "schedule",
         extra.get("value_unit", "天"),
         extra.get("evidence_kind", "table"),
         extra.get("page_ref", 12),
         extra.get("zone_type", "schedule_zone"),
         extra.get("norm_group", "schedule"),
         extra.get("chunk_hash", "abc123"),
         "schedule", 1))
    await db.commit()
    return fid


async def test_list_facts_returns_all_extraction_fields(ctx):
    """list_facts 刷新路径必须回传提取期落库的全部溯源/单位扩展字段。"""
    db, pid, sid, _ = ctx
    await _insert_fact(db, sid, pid)

    res = await gf.list_facts(scheme_id=sid, project_id="", db=db)
    assert res["stats"]["total"] == 1
    item = res["groups"][0]["items"][0]

    # 信息调用完整性：刷新不应丢失这些字段
    assert item["value_unit"] == "天"
    assert item["evidence_kind"] == "table"
    assert item["page_ref"] == 12
    assert item["zone_type"] == "schedule_zone"
    assert item["norm_group"] == "schedule"
    assert item["chunk_hash"] == "abc123"
    assert item["fact_type"] == "schedule"


async def test_list_facts_missing_columns_default_empty(ctx):
    """对未写入扩展字段（旧数据/手工行）的行，缺失字段应兜底为空而非报错。"""
    db, pid, sid, _ = ctx
    fid = uuid.uuid4().hex
    # 仅写核心列，扩展列留默认
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, title, content, category, is_resolved) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "项目经理", "- **项目经理**: 张三", "personnel", 1))
    await db.commit()

    res = await gf.list_facts(scheme_id=sid, project_id="", db=db)
    item = res["groups"][0]["items"][0]
    assert item["value_unit"] == ""
    assert item["evidence_kind"] == ""
    assert item["page_ref"] == ""
    assert item["zone_type"] == ""
    assert item["norm_group"] == ""
    assert item["chunk_hash"] == ""
