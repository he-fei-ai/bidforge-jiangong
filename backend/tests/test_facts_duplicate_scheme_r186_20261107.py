"""R186（2026-11-07）：方案复制不得把源方案的历史冲突扩散进副本。

判据（一句话）：**复制后新方案的注入结果，不应因为源方案的脏状态而改变。**

- 干净源复制 = 恒等变换（旧实现与新实现结果一致，证明问题不在复制字段范围）；
- 历史脏源（同一 ``fact_key`` 两个取值都 ``is_resolved=1/has_conflict=0``）复制后，
  副本必须按**自身作用域**（私有行 + 同项目共享行）重跑唯一性判定，后出现的
  异值行被标记 ``has_conflict=1/is_resolved=0`` 并写入冲突候选；
- 重算只**新增阻塞**，不清除任何既有标记，不改任何溯源列
  （``source_ref`` / ``chunk_hash`` / ``page_ref`` / ``group_id`` / ``fact_key``）。

变异验证：删掉 ``duplicate_scheme`` 中 ``_recompute_inherited_fact_conflicts``
调用后，本文件 ``test_dirty_source...`` / ``test_shared_layer...`` 必红。
"""

import json
import uuid

import app.db as _appdb
import app.routers.schemes as _schemes
import pytest
from app.db import get_conn, init_db
from app.services.facts_extractor import FACT_SCOPE_PREDICATE, get_facts_inject_where, normalize_key


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-r186.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


async def _seed_fact(
    db,
    pid: str,
    sid: str,
    name: str,
    value: str,
    *,
    key: str | None = None,
    source_ref: str = "",
    chunk_hash: str = "",
    page_ref=None,
    is_simulated: int = 0,
    confidence: float = 1.0,
    is_resolved: int = 1,
    has_conflict: int = 0,
    conflict_keys: str = "",
) -> str:
    """按生产口径落一行事实（``content`` 用 ``- **名称**: 取值`` 形态）。"""
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts ("
        "id, project_id, scheme_id, group_id, group_title, title, content, category,"
        " source_ref, is_simulated, confidence, is_resolved, has_conflict, conflict_keys,"
        " fact_key, chunk_hash, value_unit, fact_type, evidence_kind, page_ref, zone_type,"
        " is_safety_critical, norm_group, value_kind, chapter, fact_attr, source_kind,"
        " is_shared, is_stale"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            uuid.uuid4().hex,
            "人员",
            name,
            f"- **{name}**: {value}",
            "personnel",
            source_ref,
            is_simulated,
            confidence,
            is_resolved,
            has_conflict,
            conflict_keys,
            key if key is not None else normalize_key(name),
            chunk_hash,
            "",
            "",
            "",
            page_ref,
            "",
            0,
            "",
            "",
            "",
            "",
            "",
            0,
            0,
        ),
    )
    return fid


async def _facts(db, sid: str) -> list[dict]:
    cur = await db.execute("SELECT * FROM global_facts WHERE scheme_id=? ORDER BY rowid", (sid,))
    return [dict(r) for r in await cur.fetchall()]


async def _injectable(db, sid: str, pid: str) -> list[dict]:
    cur = await db.execute(
        "SELECT title, content, fact_key FROM global_facts WHERE "
        + FACT_SCOPE_PREDICATE
        + " AND "
        + get_facts_inject_where(),
        (sid, pid),
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_dirty_source_copy_does_not_inherit_dual_injectable(ctx):
    """历史脏源：两行同名异值都可注入 → 副本只能留一个可注入值。"""
    db, pid, sid = ctx
    await _seed_fact(
        db,
        pid,
        sid,
        "项目经理",
        "张三",
        key="project_manager",
        source_ref='[{"file":"a.pdf","quote":"张三"}]',
        chunk_hash="hash-a",
    )
    await _seed_fact(
        db,
        pid,
        sid,
        "项目经理",
        "李四",
        key="project_manager",
        source_ref='[{"file":"b.pdf","quote":"李四"}]',
        chunk_hash="hash-b",
    )
    await db.commit()
    assert len(await _injectable(db, sid, pid)) == 2, "前置条件：源方案自身已脏"

    res = await _schemes.duplicate_scheme(db=db, project_id=pid, scheme_id=sid)
    assert res["conflicts_marked"] == 1, res
    new_id = res["id"]

    rows = await _facts(db, new_id)
    assert len(rows) == 2, "复制不得丢行"
    assert len(await _injectable(db, new_id, pid)) == 1, "副本不得同时注入两个取值"

    kept = next(r for r in rows if not r["has_conflict"])
    marked = next(r for r in rows if r["has_conflict"])
    assert kept["chunk_hash"] == "hash-a", "先出现者保留可注入资格且溯源列不变"
    assert marked["chunk_hash"] == "hash-b", "被标记行溯源列不得被重算洗掉"
    assert marked["source_ref"].startswith("[{"), "溯源列必须保留复制值"
    assert marked["is_resolved"] == 0
    candidates = json.loads(marked["conflict_keys"])
    assert [c["value"] for c in candidates] == ["张三"]
    assert f"复制自方案 {sid}" in candidates[0]["source"]

    # 源方案不受影响：重算只作用于副本作用域
    assert len(await _injectable(db, sid, pid)) == 2


async def test_clean_source_copy_is_faithful(ctx):
    """干净源复制 = 恒等变换（证明问题不在复制字段范围，而在脏状态扩散）。"""
    db, pid, sid = ctx
    await _seed_fact(db, pid, sid, "项目经理", "张三", key="project_manager")
    await _seed_fact(db, pid, sid, "安全员", "王五", key="safety_officer")
    await db.commit()

    before = [(r["title"], r["content"], r["has_conflict"], r["is_resolved"]) for r in await _facts(db, sid)]
    res = await _schemes.duplicate_scheme(db=db, project_id=pid, scheme_id=sid)
    assert res["conflicts_marked"] == 0
    after = [(r["title"], r["content"], r["has_conflict"], r["is_resolved"]) for r in await _facts(db, res["id"])]
    assert after == before
    assert [r["content"] for r in await _injectable(db, res["id"], pid)] == [
        r["content"] for r in await _injectable(db, sid, pid)
    ]


async def test_same_key_same_value_is_redundant_not_conflict(ctx):
    """同键同值只是冗余（T-01 明确放行），复制后不得被误标矛盾。"""
    db, pid, sid = ctx
    await _seed_fact(db, pid, sid, "项目经理", "张三", key="project_manager")
    await _seed_fact(db, pid, sid, "项目经理", "张三", key="project_manager")
    await db.commit()

    res = await _schemes.duplicate_scheme(db=db, project_id=pid, scheme_id=sid)
    assert res["conflicts_marked"] == 0
    rows = await _facts(db, res["id"])
    assert len(rows) == 2
    assert all(not r["has_conflict"] and r["is_resolved"] == 1 for r in rows)
    assert len(await _injectable(db, res["id"], pid)) == 2


async def test_shared_layer_conflict_marks_copied_row(ctx):
    """副本私有行与同项目共享行同名异值 → 与写闸门同作用域，副本行必须被阻塞。"""
    db, pid, sid = ctx
    shared_id = await _seed_fact(db, pid, "", "项目经理", "张三", key="project_manager")
    private_id = await _seed_fact(db, pid, sid, "项目经理", "李四", key="project_manager")
    await db.commit()
    assert shared_id != private_id

    res = await _schemes.duplicate_scheme(db=db, project_id=pid, scheme_id=sid)
    assert res["conflicts_marked"] == 1
    rows = await _facts(db, res["id"])
    assert len(rows) == 1, "共享层不随方案复制（作用域不变）"
    assert rows[0]["has_conflict"] == 1 and rows[0]["is_resolved"] == 0
    candidates = json.loads(rows[0]["conflict_keys"])
    assert [c["value"] for c in candidates] == ["张三"]
    assert candidates[0]["source"] == "项目共享事实"
    assert [r["content"] for r in await _injectable(db, res["id"], pid)] == ["- **项目经理**: 张三"]
    # 共享行本身不被改写
    cur = await db.execute("SELECT has_conflict, is_resolved FROM global_facts WHERE id=?", (shared_id,))
    shared = dict(await cur.fetchone())
    assert shared == {"has_conflict": 0, "is_resolved": 1}


async def test_non_injectable_states_are_preserved(ctx):
    """模拟值 / 待确认 / 已矛盾 / 过期：复制保留原状态，重算不反向清除标记。"""
    db, pid, sid = ctx
    await _seed_fact(
        db,
        pid,
        sid,
        "基坑深度",
        "12.5m",
        key="pit_depth",
        is_simulated=1,
        is_resolved=0,
        confidence=0.3,
    )
    await _seed_fact(
        db,
        pid,
        sid,
        "总工期",
        "450天",
        key="total_duration",
        is_resolved=0,
    )
    await _seed_fact(
        db,
        pid,
        sid,
        "混凝土强度",
        "C30",
        key="concrete_grade",
        has_conflict=1,
        is_resolved=0,
        conflict_keys=json.dumps([{"value": "C35", "source": "旧资料", "confidence": 1.0}]),
    )
    await _seed_fact(db, pid, sid, "项目经理", "张三", key="project_manager")
    await db.commit()

    res = await _schemes.duplicate_scheme(db=db, project_id=pid, scheme_id=sid)
    assert res["conflicts_marked"] == 0
    rows = {r["fact_key"]: r for r in await _facts(db, res["id"])}
    assert rows["pit_depth"]["is_simulated"] == 1 and rows["pit_depth"]["is_resolved"] == 0
    assert rows["total_duration"]["is_resolved"] == 0
    assert rows["concrete_grade"]["has_conflict"] == 1 and rows["concrete_grade"]["is_resolved"] == 0
    assert json.loads(rows["concrete_grade"]["conflict_keys"]) == [
        {"value": "C35", "source": "旧资料", "confidence": 1.0}
    ]
    assert rows["project_manager"]["is_resolved"] == 1
