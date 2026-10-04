"""增量提取与「清除全部事实」回归测试（2026-09-17）。

覆盖：
- 管线：completed_chunks 命中的段跳过 AI 调用；全跳过短路；失败段不记进度
- persist_extraction：跳过段的事实行不被误删；新行带 chunk_hash 落库
- save_extracted_chunks：残留指纹清理
- POST /global-facts/clear：清空事实 + 重置提取进度
"""
import io
import json
import uuid

import app.db as _appdb
import app.routers.global_facts as gf
import app.services.facts_extractor as fe
import pytest
from app.db import get_conn, init_db
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    _chunk_hash,
    load_completed_chunks,
    persist_extraction,
    run_extraction_pipeline,
    save_extracted_chunks,
)
from fastapi import UploadFile


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-incremental.sqlite"
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
    yield db, pid, sid
    await db.close()


def _big_text(n_chars: int, tag: str) -> str:
    return f"# {tag}\n" + (f"{tag}关键参数说明。" * (n_chars // 9 + 1))[:n_chars]


# ---------------------------------------------------------------------------
# 管线增量跳过
# ---------------------------------------------------------------------------

async def test_pipeline_skips_completed_chunks(monkeypatch):
    text = "\n\n".join([_big_text(5000, "第一章"), _big_text(5000, "第二章")])
    chunks = fe.split_into_chunks(text)
    assert len(chunks) >= 2
    completed = {_chunk_hash(c.text) for c in chunks}

    calls: list[str] = []

    async def _fake_extract(**kwargs):
        calls.append(kwargs.get("text", "")[:10])
        return [FactItem(name="工期", value="300天", key="total_duration")]

    monkeypatch.setattr(fe, "extract_from_single_chunk", _fake_extract)

    result = await run_extraction_pipeline(text, completed_chunks=completed)
    assert calls == []                       # 全部段已提取 → 零 AI 调用
    assert result.all_skipped is True
    assert result.skipped_chunks == len(chunks)
    assert result.chunk_hashes_ok == completed


async def test_pipeline_reruns_only_missing_and_failed(monkeypatch):
    text = "\n\n".join([_big_text(5000, "第一章"), _big_text(5000, "第二章")])
    chunks = fe.split_into_chunks(text)
    # 只完成第一段
    completed = {_chunk_hash(chunks[0].text)}

    async def _fake_extract(**kwargs):
        return [FactItem(name="工期", value="300天", key="total_duration")]

    monkeypatch.setattr(fe, "extract_from_single_chunk", _fake_extract)
    result = await run_extraction_pipeline(text, completed_chunks=completed)
    assert result.all_skipped is False
    assert result.skipped_chunks == 1
    assert _chunk_hash(chunks[0].text) in result.chunk_hashes_ok
    assert _chunk_hash(chunks[1].text) in result.chunk_hashes_ok
    assert result.total_items >= 1


async def test_pipeline_failed_chunk_not_marked_completed(monkeypatch):
    text = _big_text(3000, "第一章")
    chunks = fe.split_into_chunks(text)
    completed = set()

    async def _fail_extract(**kwargs):
        kwargs["error_out"]["error"] = "HTTP 429"
        return []

    monkeypatch.setattr(fe, "extract_from_single_chunk", _fail_extract)
    result = await run_extraction_pipeline(text, completed_chunks=completed)
    assert result.chunk_hashes_ok == set()   # 失败段不得计入已完成（下次重试）
    assert not result.all_skipped


async def test_truncated_chunk_fingerprint_not_evicted(monkeypatch, ctx):
    """BUG 回归：因优先级截断被挤出窗口的历史段，其指纹不得被当残留清理。

    场景：文档增长后段数超过 max_chunks，低优先段被截断丢弃。旧实现把
    chunk_hashes_all 设为截断后保留段 → save 的残留清理会误删被截断段
    的指纹（而其事实仍在库），下次该段重回窗口又要全量重抽。
    """
    db, pid, sid = ctx

    def _fake_split(text, *a, **kw):
        return [
            fe.Chunk(text="高优先A机械表", priority_weight=1.5,
                     zone_type="machinery_stat"),
            fe.Chunk(text="高优先B做法表", priority_weight=1.4,
                     zone_type="construction_practice_zone"),
            fe.Chunk(text="低优先C封面", priority_weight=0.2,
                     zone_type="general"),
        ]

    async def _fake_extract(**kwargs):
        return [FactItem(name="工期", value="300天", key="total_duration")]

    monkeypatch.setattr(fe, "split_into_chunks", _fake_split)
    monkeypatch.setattr(fe, "extract_from_single_chunk", _fake_extract)

    low_hash = _chunk_hash("低优先C封面")
    # 上次低优先段 C 已成功提取（指纹已入库），本次因截断被挤出窗口
    await save_extracted_chunks(db, pid, sid, {low_hash}, {low_hash})
    assert low_hash in await load_completed_chunks(db, pid, sid)

    # max_chunks=1 → 只保留最高权重段 A，B/C 被截断
    result = await run_extraction_pipeline("x" * 200, max_chunks=1,
                                           completed_chunks={low_hash})
    # ✅ 关键断言：chunk_hashes_all 覆盖文档全部段（含被截断的 C）
    assert low_hash in result.chunk_hashes_all

    # 按 SSE 口径用本次结果刷新进度：C 的指纹不应被残留清理掉
    await save_extracted_chunks(db, pid, sid,
                                result.chunk_hashes_ok, result.chunk_hashes_all)
    assert low_hash in await load_completed_chunks(db, pid, sid)


# ---------------------------------------------------------------------------
# persist_extraction：跳过段事实保留
# ---------------------------------------------------------------------------

def _fact_row(db, pid, sid, *, name, key, chunk_hash, category="schedule",
              resolved=0):
    return db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "chunk_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, sid, "g1", "工期安排", name,
         f"- **{name}**: v", category,
         json.dumps([{"file": "doc.docx", "quote": ""}]),
         0, 1.0, resolved, 0, "", key, chunk_hash))


async def test_persist_keeps_skipped_chunk_rows(ctx):
    db, pid, sid = ctx
    # 上一轮提取的行：一段已完成（hash=AAA，增量跳过）、一段未完成（hash=BBB，
    # 本轮重跑）。两行均属本次结果覆盖的分类，用于验证「跳过段保留 / 重跑段刷新」
    await _fact_row(db, pid, sid, name="总工期", key="total_duration",
                    chunk_hash="AAA", category="schedule")
    await _fact_row(db, pid, sid, name="机械数量", key="excavator_count",
                    chunk_hash="BBB", category="machinery")
    await db.commit()

    # 本轮只重跑了 hash=BBB 的段（AAA 段被增量跳过）
    item = FactItem(name="挖掘机型号", value="SY215C", key="excavator_model",
                    category="machinery", source="doc.docx", chunk_hash="BBB")
    result = ExtractionResult()
    result.groups = [FactGroup(title="机械设备", category="machinery",
                               items=[item])]
    result.total_items = 1
    result.chunk_hashes_all = {"BBB"}
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute(
        "SELECT title, chunk_hash FROM global_facts WHERE scheme_id=?", (sid,))
    rows = {(r["title"], r["chunk_hash"]) for r in await cur.fetchall()}
    # 跳过段（AAA）的旧事实必须原样保留，不得被误删
    assert ("总工期", "AAA") in rows
    # 重跑段的旧行被刷新，新行带 chunk_hash 落库
    assert ("挖掘机型号", "BBB") in rows
    assert not any(t == "机械数量" for t, _ in rows)


async def test_persist_deletes_stale_empty_hash_rows(ctx):
    db, pid, sid = ctx
    await _fact_row(db, pid, sid, name="总工期", key="total_duration",
                    chunk_hash="")
    await db.commit()
    item = FactItem(name="总工期", value="300天", key="total_duration",
                    category="schedule", source="doc.docx", chunk_hash="CCC")
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule",
                               items=[item])]
    result.total_items = 1
    result.chunk_hashes_all = {"CCC"}
    await persist_extraction(result, db, pid, sid)
    cur = await db.execute(
        "SELECT title, chunk_hash FROM global_facts WHERE scheme_id=?", (sid,))
    rows = [(r["title"], r["chunk_hash"]) for r in await cur.fetchall()]
    assert rows == [("总工期", "CCC")]


async def test_persist_deletes_vanished_source_rows(ctx):
    """2026-09-28 回归：文档内容变化导致分段指纹漂移后的过期未确认事实清理。

    场景：旧文档的分段指纹 V1（对应一次提取落库的事实行）；重新解析/更新
    文档后，新文档分段变为 {SKIP, V2} —— 其中 V2 段本次重跑，SKIP 段被增量
    跳过。旧实现只删「空指纹 / 本次重跑指纹」：V1 行既不在 run 也不在 all，
    永远残留 → 与新的 V2 事实同 key 重复堆积（界面出现两条同名未确认事实）。
    新口径：指纹已不在当前文档**全量**分段集合 → 源头已消失 → 立即清除；
    而仍在 all 但被跳过的 SKIP 段事实必须保留（增量语义不回归）。
    """
    db, pid, sid = ctx
    await _fact_row(db, pid, sid, name="总工期", key="total_duration",
                    chunk_hash="V1")                     # 源头已消失 → 应清除
    await _fact_row(db, pid, sid, name="机械数量", key="machinery_count",
                    chunk_hash="SKIP", category="machinery")  # 被跳过 → 应保留
    await db.commit()

    item = FactItem(name="总工期", value="300天", key="total_duration",
                    category="schedule", source="doc.docx", chunk_hash="V2")
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule",
                               items=[item])]
    result.total_items = 1
    result.chunk_hashes_all = {"SKIP", "V2"}      # 当前文档全量分段（含跳过的 SKIP）
    result.chunk_hashes_run = {"V2"}              # 本次实际重跑的段
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute(
        "SELECT title, chunk_hash FROM global_facts WHERE scheme_id=?", (sid,))
    rows = {(r["title"], r["chunk_hash"]) for r in await cur.fetchall()}
    # V1 源已消失 → 清除；SKIP 被跳过 → 保留；V2 新事实 → 落库
    assert rows == {("机械数量", "SKIP"), ("总工期", "V2")}


# ---------------------------------------------------------------------------
# 进度表：写入与残留清理
# ---------------------------------------------------------------------------

async def test_save_extracted_chunks_prunes_stale(ctx):
    db, pid, sid = ctx
    await db.execute(
        "INSERT OR IGNORE INTO facts_extracted_chunks(project_id, chunk_hash) "
        "VALUES (?,?)", (pid, "OLD"))
    await db.commit()
    await save_extracted_chunks(db, pid, "", {"NEW1", "NEW2"}, {"NEW1", "NEW2"})
    assert await load_completed_chunks(db, pid) == {"NEW1", "NEW2"}
    # OLD 不在本次分段集合 → 残留被清理
    assert "OLD" not in await load_completed_chunks(db, pid)


async def test_load_completed_chunks_missing_project(ctx):
    db, pid, sid = ctx
    assert await load_completed_chunks(db, "no-such-project") == set()


async def test_completed_chunks_scoped_by_scheme(ctx):
    """BUG-1 回归：多方案共享项目时，各方案的提取进度必须相互独立。"""
    db, pid, sid = ctx
    sa, sb = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sa, pid, "A"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sb, pid, "B"))
    await db.commit()
    # 方案 A 标记一段完成
    await save_extracted_chunks(db, pid, sa, {"H1"}, {"H1"})
    # ✅ 方案 B 不能看到 A 的进度（否则会 all_skipped 早退、写入 0 条事实）
    assert await load_completed_chunks(db, pid, sb) == set()
    assert await load_completed_chunks(db, pid, sa) == {"H1"}
    # 为 B 记录进度不影响 A
    await save_extracted_chunks(db, pid, sb, {"H2"}, {"H2"})
    assert await load_completed_chunks(db, pid, sa) == {"H1"}
    assert await load_completed_chunks(db, pid, sb) == {"H2"}


async def test_persist_keeps_confirmed_fact_on_reextract(ctx):
    """BUG-3 回归：重新提取产生与已确认事实冲突的新值，不得静默打回待审核。"""
    db, pid, sid = ctx
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, group_title, title, content, "
        "category, source_ref, is_simulated, confidence, is_resolved, "
        "has_conflict, conflict_keys, fact_key, chunk_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "工期安排", "总工期",
         "- **总工期**: 300天", "schedule",
         json.dumps([{"file": "doc.docx", "quote": ""}]),
         0, 1.0, 1, 0, "", "total_duration", "OLD"))
    await db.commit()
    # 重新提取产生同 key 不同值
    item = FactItem(name="总工期", value="360天", key="total_duration",
                    category="schedule", source="doc.docx", chunk_hash="NEW")
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule", items=[item])]
    result.total_items = 1
    result.chunk_hashes_all = {"NEW"}
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute(
        "SELECT is_resolved, has_conflict, content, conflict_keys "
        "FROM global_facts WHERE scheme_id=? AND fact_key=?",
        (sid, "total_duration"))
    row = await cur.fetchone()
    # ✅ 用户已确认状态保留，不被静默打回「待审核 + 有冲突」
    assert row["is_resolved"] == 1
    assert row["has_conflict"] == 1
    stale = await (await db.execute(
        "SELECT is_stale FROM global_facts WHERE id=?", (fid,))).fetchone()
    assert stale["is_stale"] == 1
    # 用户原权威值保留，但新证据使其退出注入链路，等待人工裁决。
    assert "300天" in row["content"]
    # 新证据（360天）被记入候选供人工裁决
    assert "360天" in (row["conflict_keys"] or "")


# ---------------------------------------------------------------------------
# 清除全部事实
# ---------------------------------------------------------------------------

async def test_clear_all_facts_resets_everything(ctx):
    db, pid, sid = ctx
    await _fact_row(db, pid, sid, name="总工期", key="total_duration",
                    chunk_hash="AAA", resolved=1)
    await db.execute(
        "INSERT INTO facts_extracted_chunks(project_id, scheme_id, chunk_hash) VALUES (?,?,?)",
        (pid, sid, "AAA"))
    await db.commit()

    res = await gf.clear_all_facts({"scheme_id": sid}, db)
    assert res["ok"] is True and res["deleted"] == 1
    cur = await db.execute("SELECT count(*) FROM global_facts")
    assert (await cur.fetchone())[0] == 0
    # 提取进度同步重置（下次提取全量重跑）
    cur = await db.execute("SELECT count(*) FROM facts_extracted_chunks")
    assert (await cur.fetchone())[0] == 0


async def test_clear_all_facts_requires_scheme(ctx):
    db, pid, sid = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await gf.clear_all_facts({}, db)


async def test_batch_resolve_includes_simulated(ctx):
    """安全不变式（2026-09-21 起）：模拟值（is_simulated=1）不得被 batch_resolve 放行。

    背景：早期版本允许批量确认模拟值，被证实会把编造数据放行到正文/导出。
    现强制不变式 ``is_simulated=1 ⟹ is_resolved=0``：``batch_resolve`` 必须
    跳过模拟值，保持 ``is_resolved`` 不变；同时把跳过的 id 显式回传
    ``skipped_safety`` 供前端提示，防止用户误以为已确认。
    """
    db, pid, sid = ctx
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, group_title, title, content, "
        "category, source_ref, is_simulated, confidence, is_resolved, "
        "has_conflict, conflict_keys, fact_key, chunk_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "工程概况", "基坑深度",
         "- **基坑深度**: 【待填写】", "basic",
         json.dumps([{"file": "doc.docx", "quote": ""}]),
         1, 0.5, 0, 0, "", "basin_depth", "SIM"))
    await db.commit()
    res = await gf.batch_resolve({"fact_ids": [fid], "scheme_id": sid}, db)
    assert res["ok"] is True
    cur = await db.execute(
        "SELECT is_resolved, is_simulated FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    # 不变式：模拟值不得被批量确认放行
    assert row["is_resolved"] == 0
    assert row["is_simulated"] == 1
    # 前端能明确看到跳过原因
    assert any(s.get("id") == fid for s in res.get("skipped_safety", []))
