"""全局事实：清空指纹重置 + SSE 字段一致性 + 章节键漂移护栏（2026-09-26）

覆盖：
- clear_all_facts 必须按「方案级 + 项目共享级」双口径清理增量提取指纹，
  否则重提取会跳过已哈希段落、新资料永不进入（旧实现仅按 (project_id, scheme_id)
  且要求 project_id 非空，导致无 project 绑定 / 项目共享级残留）。
- format_for_frontend（SSE 下发路径）须与 list_facts（刷新路径）同构，补齐
  scope / is_stale，避免任何直接消费 SSE 数据的客户端丢失「项目共享 / 过期」徽标。
- facts_classification.CHAPTER_ORDER 的九大章节键是前后端章节标题的唯一事实源，
  须与前端 FACT_CHAPTER_TITLES 键严格一致（漂移护栏）。
"""
import asyncio
import uuid

import pytest

import app.db as _appdb
from app.db import init_db, get_conn
from app.routers import global_facts as gf
from app.services import facts_classification as fc
import app.services.facts_extractor as fe
from app.services.facts_extractor import FactItem, FactGroup, ExtractionResult

# 前后端约定的九大章节键（与前端 FACT_CHAPTER_TITLES / 建办质〔2018〕31号 对齐）
CANONICAL_CHAPTER_KEYS = [
    "overview", "basis", "plan", "technique", "safety",
    "personnel", "acceptance", "emergency", "calc_drawings",
]


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
    await db.execute("DELETE FROM facts_extracted_chunks WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM facts_extracted_chunks WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM global_facts WHERE scheme_id=?", (sid,))
    await db.execute("DELETE FROM schemes WHERE id=?", (sid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()


async def _count_chunks(db, **kw):
    conds, params = [], []
    for k, v in kw.items():
        conds.append(f"{k}=?")
        params.append(v)
    sql = "SELECT count(*) FROM facts_extracted_chunks" + (
        " WHERE " + " AND ".join(conds) if conds else "")
    cur = await db.execute(sql, params)
    return int((await cur.fetchone())[0] or 0)


class TestClearAllFactsChunkReset:
    async def test_scheme_scoped_chunk_cleared(self, ctx):
        db, pid, sid = ctx
        await db.execute(
            "INSERT INTO facts_extracted_chunks(project_id,scheme_id,chunk_hash) "
            "VALUES(?,?,?)", (pid, sid, "h1"))
        await db.commit()
        assert await _count_chunks(db, scheme_id=sid) == 1
        await gf.clear_all_facts({"scheme_id": sid}, db)
        assert await _count_chunks(db, scheme_id=sid) == 0

    async def test_project_shared_chunk_cleared(self, ctx):
        db, pid, sid = ctx
        # 项目共享级指纹（scheme_id=''）也应被清理，与 facts 删除口径对齐
        # （旧实现仅按 (project_id, scheme_id) 删除，scheme_id='' 的共享级指纹残留，
        # 导致项目级重新提取跳过已哈希段落）。
        await db.execute(
            "INSERT INTO facts_extracted_chunks(project_id,scheme_id,chunk_hash) "
            "VALUES(?,?,?)", (pid, "", "h2"))
        await db.commit()
        assert await _count_chunks(db, project_id=pid, scheme_id="") == 1
        await gf.clear_all_facts({"scheme_id": sid}, db)
        assert await _count_chunks(db, project_id=pid, scheme_id="") == 0


class TestFormatForFrontendScopeStale:
    def test_scope_and_is_stale_present(self):
        grp = FactGroup(title="人员角色", category="personnel", items=[
            FactItem(name="项目经理", value="张伟", key="pm",
                     category="personnel", confidence=0.9)])
        res = ExtractionResult(groups=[grp], total_items=1)
        out = fe.format_for_frontend(res)
        item = out["groups"][0]["items"][0]
        assert item["scope"] == "scheme", "SSE 提取事实恒为方案级"
        assert item["is_stale"] is False, "刚提取的事实非过期"
        # 与 list_facts 刷新路径关键字段同构
        for key in ("chapter", "fact_attr", "source_kind", "is_shared",
                    "is_simulated", "has_conflict", "confidence"):
            assert key in item


class TestChapterKeyDriftGuard:
    def test_chapter_order_canonical(self):
        assert fc.CHAPTER_ORDER == CANONICAL_CHAPTER_KEYS, (
            "九大章节键是前后端章节标题唯一事实源，须与前端 FACT_CHAPTER_TITLES 严格一致")
        for k in fc.CHAPTER_ORDER:
            assert fc.CHAPTER_TITLES.get(k), f"章节键 {k} 缺少中文标题"


class TestBatchResolveScopeGuard:
    async def test_fact_ids_without_scheme_id_rejected(self, ctx):
        db, pid, sid = ctx
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await gf.batch_resolve({"fact_ids": ["x", "y"]}, db)
        assert exc.value.status_code == 400

    async def test_resolve_safe_fact_with_scheme_id(self, ctx):
        db, pid, sid = ctx
        fid = uuid.uuid4().hex
        # 普通事实（非模拟 / 无矛盾 / 非安全关键 / 未过期）
        await db.execute(
            "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
            "group_title, title, content, category, source_ref, is_simulated, "
            "confidence, is_resolved, has_conflict) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, pid, sid, "g", "其他事实", "工程名称", "- **工程名称**: 某项目",
             "other", "[]", 0, 1.0, 0, 0))
        await db.commit()
        res = await gf.batch_resolve({"scheme_id": sid, "fact_ids": [fid]}, db)
        assert res["changed"] == 1
        cur = await db.execute(
            "SELECT is_resolved FROM global_facts WHERE id=?", (fid,))
        assert int((await cur.fetchone())[0]) == 1
