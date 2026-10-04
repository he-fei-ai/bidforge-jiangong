"""全局事实路由：resolve_fact 对不存在的 id 返回 404。"""
import pytest
from app.routers.global_facts import resolve_fact
from fastapi import HTTPException


async def test_resolve_nonexistent_fact_raises_404(db_conn):
    with pytest.raises(HTTPException) as ei:
        await resolve_fact("not-exists-id", db=db_conn)
    assert ei.value.status_code == 404


async def test_resolve_existing_fact_marks_resolved(db_conn):
    await db_conn.execute(
        "INSERT INTO projects (id, name) VALUES (?,?)", ("p1", "项目"))
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "方案"))
    await db_conn.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence,"
        " is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("f1", "p1", "s1", "g1", "工期", "项目总工期", "- **项目总工期**: 120天",
         "schedule", "", 0, 0.9, 0, 0, "", "total_duration"))
    await db_conn.commit()

    result = await resolve_fact("f1", db=db_conn)
    assert result == {"ok": True}

    cur = await db_conn.execute(
        "SELECT is_resolved FROM global_facts WHERE id=?", ("f1",))
    row = await cur.fetchone()
    assert row["is_resolved"] == 1


# ---------------------------------------------------------------------------
# resolve_conflict：模拟值闸门随裁决结果重算（2026-09-21 回归锁）
#   旧实现只写 content / has_conflict / is_resolved，is_simulated 原样保留 →
#   编造值被裁决为文档实值后行上仍带模拟值标记，stats.simulated 永不归零，
#   前端「全部就绪」入口因此永不出现。
# ---------------------------------------------------------------------------

import json
import uuid

import app.routers.global_facts as gf


async def _seed_conflicting(db, *, value="12.5m", candidates, simulated=1):
    """插入一条带矛盾候选的事实，返回行 id。"""
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, "p1", "s1", "g1", "技术参数", "基坑深度",
         gf._build_fact_content("基坑深度", value, bool(simulated)),
         "tech_param", "", simulated, 0.6, 0, 1,
         json.dumps(candidates, ensure_ascii=False), "depth"))
    await db.commit()
    return fid


async def _row_of(db, fid):
    cur = await db.execute(
        "SELECT title, content, is_simulated, is_resolved, has_conflict, "
        "conflict_keys, confidence FROM global_facts WHERE id=?", (fid,))
    return dict(await cur.fetchone())


async def test_conflict_resolved_to_real_value_drops_simulated(db_conn):
    """编造值 → 裁决为文档实值：is_simulated 必须归 0（闸门不再残留）。"""
    fid = await _seed_conflicting(
        db_conn, value="12.5m",
        candidates=[{"value": "18.0m", "source": "地质报告.pdf",
                     "confidence": 0.95, "is_simulated": False}])
    r = await _row_of(db_conn, fid)
    assert r["is_simulated"] == 1  # 初始状态：模拟值待确认

    out = await gf.resolve_conflict(fid, {"value": "18.0m"}, db=db_conn)
    assert out == {"ok": True}

    r = await _row_of(db_conn, fid)
    assert r["is_simulated"] == 0, "裁决为真实取值后不得残留模拟值标记"
    assert "模拟值" not in r["content"]
    assert r["has_conflict"] == 0 and r["conflict_keys"] == ""
    assert r["is_resolved"] == 1 and r["confidence"] == 1.0


async def test_conflict_resolved_to_simulated_candidate_keeps_gate(db_conn):
    """裁决为另一个模拟值候选：闸门必须继续生效（不得放行编造值）。"""
    fid = await _seed_conflicting(
        db_conn, value="12.5m",
        candidates=[{"value": "18.0m", "source": "估算",
                     "confidence": 0.3, "is_simulated": True}])
    await gf.resolve_conflict(fid, {"value": "18.0m"}, db=db_conn)
    r = await _row_of(db_conn, fid)
    assert r["is_simulated"] == 1, "候选本身是模拟值时闸门必须保留"
    assert "模拟值" in r["content"]
    assert r["has_conflict"] == 0, "矛盾标记仍应清除"


async def test_conflict_keep_current_value_preserves_simulated(db_conn):
    """「保留当前值」= 值未变 → 只清矛盾，模拟值语义保持原样。"""
    fid = await _seed_conflicting(db_conn, value="12.5m",
                                  candidates=[{"value": "18.0m"}])
    await gf.resolve_conflict(fid, {"value": "12.5m"}, db=db_conn)
    r = await _row_of(db_conn, fid)
    assert r["is_simulated"] == 1, "值未变时不应改写模拟值语义"
    assert "模拟值" in r["content"]
    assert r["has_conflict"] == 0


async def test_conflict_legacy_candidates_without_flag_treat_as_real(db_conn):
    """旧数据候选无 is_simulated 字段 → 视为真实证据，闸门归 0。"""
    fid = await _seed_conflicting(
        db_conn, value="12.5m",
        candidates=[{"value": "18.0m", "source": "地质报告.pdf", "confidence": 0.95}])
    await gf.resolve_conflict(fid, {"value": "18.0m"}, db=db_conn)
    r = await _row_of(db_conn, fid)
    assert r["is_simulated"] == 0, "无标记的旧候选按真实证据处理"


async def test_conflict_resolved_value_marker_stripped(db_conn):
    """传入值自带模拟值标记 → 归一化为纯值（标记由 is_simulated 列承载）。"""
    from app.services.facts_extractor import SIMULATED_MARKER
    fid = await _seed_conflicting(
        db_conn, value="12.5m",
        candidates=[{"value": "18.0m", "is_simulated": False}])
    await gf.resolve_conflict(fid, {"value": f"18.0m{SIMULATED_MARKER}"}, db=db_conn)
    r = await _row_of(db_conn, fid)
    assert r["content"] == "- **基坑深度**: 18.0m"
    assert r["is_simulated"] == 0


async def test_conflict_empty_value_raises_400(db_conn):
    with pytest.raises(HTTPException) as ei:
        await gf.resolve_conflict("f1", {"value": "   "}, db=db_conn)
    assert ei.value.status_code == 400


async def test_conflict_nonexistent_raises_404(db_conn):
    with pytest.raises(HTTPException) as ei:
        await gf.resolve_conflict("not-exists-id", {"value": "x"}, db=db_conn)
    assert ei.value.status_code == 404
