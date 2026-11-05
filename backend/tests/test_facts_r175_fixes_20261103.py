"""R175（2026-11-03）全局事实模块修复回归。

覆盖：
- BUG-01：部分段失败时，失败段旧事实必须保留（删除范围 = run ∩ ok）
- BUG-02：segment_failed 不得记完成；失败段下次可重试 + 连续失败指数退避
- BUG-03：交叉校验异常 → 取消 auto_resolve 自动裁决（全部转人工）
- BUG-04：cross_conflicts 落库，刷新（重新读取）后仍可查到
- BUG-05：/chapters totals 键名对齐 + covered_fields 形态统一
- BUG-12：空白值簇不再 IndexError
"""

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
    load_cross_conflicts,
    merge_and_deduplicate,
    persist_extraction,
    record_chunk_failures,
    retry_delay_seconds,
    run_extraction_pipeline,
)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "facts-r175.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


def _fact_row(db, pid, sid, *, name, key, chunk_hash, category="schedule"):
    return db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "chunk_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uuid.uuid4().hex,
            pid,
            sid,
            "g1",
            "工期安排",
            name,
            f"- **{name}**: v",
            category,
            json.dumps([{"file": "doc.docx", "quote": ""}]),
            0,
            1.0,
            0,
            0,
            "",
            key,
            chunk_hash,
        ),
    )


def _big_text(n_chars: int, tag: str) -> str:
    return f"# {tag}\n" + (f"{tag}关键参数说明。" * (n_chars // 9 + 1))[:n_chars]


# ---------------------------------------------------------------------------
# BUG-01：删除范围 = run ∩ ok（失败段旧事实保留）
# ---------------------------------------------------------------------------


async def test_persist_partial_failure_keeps_failed_segment_rows(ctx):
    db, pid, sid = ctx
    # 三段旧事实（同分类，确保旧实现一定会命中删除条件）
    await _fact_row(db, pid, sid, name="总工期", key="total_duration", chunk_hash="AAA")
    await _fact_row(db, pid, sid, name="机械数量", key="machinery_count", chunk_hash="BBB")
    await _fact_row(db, pid, sid, name="质量标准", key="quality_standard", chunk_hash="CCC")
    await db.commit()

    items = [
        FactItem(
            name="开工日期",
            value="2026-03-01",
            key="start_date",
            category="schedule",
            source="doc.docx",
            chunk_hash="AAA",
        ),
        FactItem(
            name="质量标准",
            value="合格",
            key="quality_standard",
            category="schedule",
            source="doc.docx",
            chunk_hash="CCC",
        ),
    ]
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule", items=items)]
    result.total_items = len(items)
    result.chunk_hashes_all = {"AAA", "BBB", "CCC"}
    result.chunk_hashes_run = {"AAA", "BBB", "CCC"}
    # BBB 段本轮失败（报错）→ 其旧事实不得被删除
    result.chunk_hashes_ok = {"AAA", "CCC"}
    result.chunk_hashes_failed = {"BBB"}
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute("SELECT title, chunk_hash FROM global_facts WHERE scheme_id=?", (sid,))
    rows = {(r["title"], r["chunk_hash"]) for r in await cur.fetchall()}
    assert ("机械数量", "BBB") in rows  # 失败段旧事实保留
    assert ("总工期", "AAA") not in rows  # 成功段旧事实被刷新
    assert ("开工日期", "AAA") in rows  # 新事实仅来自成功段
    assert ("质量标准", "CCC") in rows
    assert not any(h == "BBB" and t != "机械数量" for t, h in rows)


# ---------------------------------------------------------------------------
# BUG-02：segment_failed 不记完成 + 失败退避
# ---------------------------------------------------------------------------


async def test_declared_failed_segment_not_marked_complete(monkeypatch, ctx):
    db, pid, sid = ctx
    text = _big_text(3000, "第一章")
    chunks = fe.split_into_chunks(text)
    h = _chunk_hash(chunks[0].text)

    async def _declared(**kwargs):
        kwargs["error_out"]["segment_failed"] = True
        return []

    monkeypatch.setattr(fe, "extract_from_single_chunk", _declared)
    result = await run_extraction_pipeline(text, completed_chunks=set())
    assert h not in result.chunk_hashes_ok  # 不得写入完成表
    assert h in result.chunk_hashes_failed

    await record_chunk_failures(db, pid, sid, failed_hashes={h})
    # 第 1 次失败 → next_retry_at 无延迟 → 下一次运行立即可重试
    assert h not in await load_completed_chunks(db, pid, sid)
    cur = await db.execute(
        "SELECT fail_count FROM facts_chunk_failures " "WHERE project_id=? AND scheme_id=? AND chunk_hash=?",
        (pid, sid, h),
    )
    assert (await cur.fetchone())[0] == 1


async def test_deferred_failed_chunk_not_marked_complete(monkeypatch):
    """退避窗口内的失败段参与跳过，但绝不能写回完成表（BUG-02 回归防线）。"""
    text = _big_text(3000, "第一章")
    h = _chunk_hash(fe.split_into_chunks(text)[0].text)
    called = {"n": 0}

    async def _fake(**kwargs):
        called["n"] += 1
        return []

    monkeypatch.setattr(fe, "extract_from_single_chunk", _fake)
    result = await run_extraction_pipeline(text, completed_chunks=fe.CompletedChunkSet({h}, deferred={h}))
    assert called["n"] == 0  # 退避窗口内 → 跳过不调用 AI
    assert h not in result.chunk_hashes_ok  # 但不得记为已完成
    assert result.all_skipped is True


async def test_retry_backoff_curve_and_deferred_window(ctx):
    db, pid, sid = ctx
    assert retry_delay_seconds(1) == 0
    assert retry_delay_seconds(2) == fe.RETRY_BACKOFF_BASE_SECONDS
    assert retry_delay_seconds(3) == fe.RETRY_BACKOFF_BASE_SECONDS * 2
    assert retry_delay_seconds(10) == fe.RETRY_BACKOFF_MAX_SECONDS

    h = "H1"
    await record_chunk_failures(db, pid, sid, failed_hashes={h})
    await record_chunk_failures(db, pid, sid, failed_hashes={h})  # 连续第 2 次
    # 退避窗口内 → 本轮暂跳过（但不在完成表，到期后会自动重新提取）
    assert h in await load_completed_chunks(db, pid, sid)

    # 模拟时间到期：把 next_retry_at 置为过去 → 重新可提取
    await db.execute(
        "UPDATE facts_chunk_failures SET next_retry_at=? " "WHERE project_id=? AND scheme_id=? AND chunk_hash=?",
        ("2020-01-01 00:00:00", pid, sid, h),
    )
    await db.commit()
    assert h not in await load_completed_chunks(db, pid, sid)

    # 成功段清理失败记录
    await record_chunk_failures(db, pid, sid, ok_hashes={h})
    cur = await db.execute(
        "SELECT COUNT(*) FROM facts_chunk_failures " "WHERE project_id=? AND scheme_id=? AND chunk_hash=?",
        (pid, sid, h),
    )
    assert (await cur.fetchone())[0] == 0


# ---------------- R175 续（2026-11-04）：all_skipped 两种来源区分 ----------------


def _all_skipped_result(skipped: int, ok_hashes: set) -> ExtractionResult:
    result = ExtractionResult()
    result.all_skipped = True
    result.skipped_chunks = skipped
    result.chunk_hashes_ok = set(ok_hashes)
    return result


def test_all_skipped_incremental_reports_no_new_content():
    """真·增量跳过：确有历史成果 → completed，并明确「本次无新增内容」。"""
    from app.routers.sse_handlers import _facts_all_skipped_payload

    status, event, msg, extra = _facts_all_skipped_payload(_all_skipped_result(5, {"h1", "h2", "h3", "h4", "h5"}))
    assert status == "completed" and event == "completed"
    assert "本次无新增内容" in msg
    assert "失败重试退避" not in msg
    assert extra == {"skipped": 5, "deferred": 0, "skipped_source": "incremental"}


def test_all_skipped_deferred_backoff_not_counted_as_success():
    """全部段在失败退避窗口（此前从未成功）→ 必须 failed，不得计入成功。"""
    from app.routers.sse_handlers import _facts_all_skipped_payload

    status, event, msg, extra = _facts_all_skipped_payload(_all_skipped_result(5, set()))
    assert status == "failed" and event == "error"
    assert "失败重试退避窗口" in msg
    assert "暂无已提取成果" in msg
    assert "本次无新增内容" not in msg
    assert extra == {"skipped": 5, "deferred": 5, "skipped_source": "deferred_backoff"}


def test_all_skipped_mixed_reports_incremental_plus_deferred_count():
    """部分真完成 + 部分退避：仍算增量跳过，但必须回传被退避的段数。"""
    from app.routers.sse_handlers import _facts_all_skipped_payload

    status, event, msg, extra = _facts_all_skipped_payload(_all_skipped_result(5, {"h1", "h2", "h3"}))
    assert status == "completed" and event == "completed"
    assert "本次无新增内容" in msg
    assert "2 段处于失败重试退避窗口" in msg
    assert extra == {"skipped": 5, "deferred": 2, "skipped_source": "incremental"}


# ---------------------------------------------------------------------------
# BUG-03：交叉校验异常 → 关闭自动裁决
# ---------------------------------------------------------------------------


async def test_cross_validation_error_disables_auto_resolve(monkeypatch, ctx):
    db, pid, sid = ctx
    monkeypatch.setattr(fe.settings, "auto_resolve_extracted_facts", True)

    def _boom(_items):
        raise RuntimeError("validator boom")

    monkeypatch.setattr(fe, "run_cross_validations", _boom)

    async def _fake_extract(**kwargs):
        return [FactItem(name="总工期", value="300天", key="total_duration")]

    monkeypatch.setattr(fe, "extract_from_single_chunk", _fake_extract)
    result = await run_extraction_pipeline(_big_text(3000, "第一章"), completed_chunks=set())
    assert result.cross_validation_failed is True
    assert any("交叉校验异常" in w for w in result.warnings)

    await persist_extraction(result, db, pid, sid)
    cur = await db.execute(
        "SELECT is_resolved FROM global_facts WHERE scheme_id=? AND fact_key=?", (sid, "total_duration")
    )
    assert (await cur.fetchone())[0] == 0  # 自动裁决被取消 → 待人工审核


# ---------------------------------------------------------------------------
# BUG-04：冲突记录落库 + 刷新可查
# ---------------------------------------------------------------------------

_CONFLICT = {
    "rule_id": "XV-SAME-NAME",
    "severity": "high",
    "conflict_type": "value_mismatch",
    "side_a": {"name": "混凝土强度", "value": "C30", "source": "a.docx", "confidence": 0.9},
    "side_b": {"name": "混凝土强度", "value": "C35", "source": "b.docx", "confidence": 0.8},
    "resolution_hint": "请核对设计说明",
    "auto_resolvable": False,
}


async def test_cross_conflicts_persisted_and_reloadable(ctx):
    db, pid, sid = ctx
    item = FactItem(
        name="混凝土强度", value="C30", key="concrete_strength", category="schedule", source="a.docx", chunk_hash="H1"
    )
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule", items=[item])]
    result.total_items = 1
    result.chunk_hashes_all = {"H1"}
    result.chunk_hashes_ok = {"H1"}
    result.cross_conflicts = [dict(_CONFLICT)]
    await persist_extraction(result, db, pid, sid)

    # 「刷新」= 丢弃内存态，仅靠 DB 重新读取
    stored = await load_cross_conflicts(db, pid, sid)
    assert len(stored) == 1
    assert stored[0]["rule_id"] == "XV-SAME-NAME"
    assert stored[0]["fact_name"] == "混凝土强度"
    assert stored[0]["side_a"]["value"] == "C30"
    assert stored[0]["side_b"]["source"] == "b.docx"

    # 路由可读（刷新后仍可追溯）
    resp = await gf.list_cross_conflicts(scheme_id=sid, project_id=pid, db=db)
    assert resp["total"] == 1

    # 下一轮同事实名无冲突 → 旧冲突记录被清除（冲突已解除）
    result2 = ExtractionResult()
    result2.groups = [
        FactGroup(
            title="工期安排",
            category="schedule",
            items=[
                FactItem(
                    name="混凝土强度",
                    value="C35",
                    key="concrete_strength",
                    category="schedule",
                    source="b.docx",
                    chunk_hash="H1",
                )
            ],
        )
    ]
    result2.total_items = 1
    result2.chunk_hashes_all = {"H1"}
    result2.chunk_hashes_ok = {"H1"}
    await persist_extraction(result2, db, pid, sid)
    assert await load_cross_conflicts(db, pid, sid) == []


async def test_cross_validation_error_keeps_previous_conflicts(ctx):
    db, pid, sid = ctx
    item = FactItem(
        name="混凝土强度", value="C30", key="concrete_strength", category="schedule", source="a.docx", chunk_hash="H1"
    )
    r1 = ExtractionResult()
    r1.groups = [FactGroup(title="工期安排", category="schedule", items=[item])]
    r1.total_items = 1
    r1.chunk_hashes_all = {"H1"}
    r1.chunk_hashes_ok = {"H1"}
    r1.cross_conflicts = [dict(_CONFLICT)]
    await persist_extraction(r1, db, pid, sid)

    # 本轮校验异常：不得用「空结果」覆盖已知冲突
    r2 = ExtractionResult()
    r2.groups = [FactGroup(title="工期安排", category="schedule", items=[item])]
    r2.total_items = 1
    r2.chunk_hashes_all = {"H1"}
    r2.chunk_hashes_ok = {"H1"}
    r2.cross_validation_failed = True
    await persist_extraction(r2, db, pid, sid)
    assert len(await load_cross_conflicts(db, pid, sid)) == 1


# ---------------------------------------------------------------------------
# BUG-05：/chapters totals 键名 + covered_fields 形态
# ---------------------------------------------------------------------------


async def test_chapters_totals_contract_keys(ctx):
    db, pid, sid = ctx
    await _fact_row(db, pid, sid, name="总工期", key="total_duration", chunk_hash="H1")
    await db.commit()
    resp = await gf.list_facts_by_chapters(scheme_id=sid, project_id=pid, db=db)
    totals = resp["totals"]
    # types/facts.ts: FactChaptersResponse.totals = {facts, fields, covered, ...}
    assert "fields" in totals and "covered" in totals
    assert totals["fields"] == totals["total_fields"]
    assert totals["covered"] == totals["covered_fields"]

    from app.services.facts_classification import nine_chapter_summary

    summary = nine_chapter_summary([])
    for ch in summary["chapters"]:
        # 章条目键集合不变（R89 零回归），仅 covered_fields 形态统一为清单
        assert isinstance(ch["covered_fields"], list)
    assert isinstance(summary["totals"]["covered_fields"], int)


# ---------------------------------------------------------------------------
# BUG-12：空白值簇
# ---------------------------------------------------------------------------


def test_blank_value_cluster_does_not_index_error():
    items = [
        FactItem(name="备注", value="   ", key="remark"),
        FactItem(name="备注", value="\n", key="remark"),
    ]
    out = merge_and_deduplicate(items)
    assert out == []
