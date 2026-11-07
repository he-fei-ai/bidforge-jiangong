"""R179 Phase G（2026-11-07）：编排状态机 + 留痕 / 断点续跑。

锁定点
------
1. 强杀后续跑：进程内不存状态；残留 running 的 stage 退回 pending，已 succeeded
   的 stage 及其产物 hash 原样保留。
2. stage 产物 hash 幂等：规范 JSON（键序无关）取 sha256；重复完成且产物一致
   是 no-op（不重复计数、不重复写审计），产物不一致必须冲突。
3. 每次成功迁移写一条 ai_audit_logs（scene=agent_orchestration），明细可回溯到
   run_id / stage / from / to。
4. undetermined 与 failed 并列独立终态，续跑不得把它改写为成功。
5. 非法迁移（顺序错、状态错、越级启动）一律拒绝且不落任何写入。
6. 开关默认关闭；关闭时全部入口 404。
"""

from __future__ import annotations

import aiosqlite
import pytest
from app.config import Settings, settings
from app.routers import agent_runs as router_mod
from app.services.agent import state_machine as sm
from fastapi import HTTPException


async def _audit_rows(db, action: str = "") -> list:
    clause = " AND action=?" if action else ""
    params = (action,) if action else ()
    cur = await db.execute(
        f"SELECT action, error, attempts_json FROM ai_audit_logs WHERE scene=?{clause}",
        (sm._AUDIT_SCENE, *params),
    )
    return list(await cur.fetchall())


async def _audit_count(db, action: str = "") -> int:
    return len(await _audit_rows(db, action))


def _stage_by_name(snapshot: dict, name: str) -> dict:
    return next(s for s in snapshot["stages"] if s["stage"] == name)


# ---------------------------------------------------------------------------
# 0. 开关与惰性建表
# ---------------------------------------------------------------------------


def test_switch_defaults_to_false():
    assert Settings().agent_runs_enabled is False


async def test_router_returns_404_when_disabled(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "agent_runs_enabled", False, raising=False)
    with pytest.raises(HTTPException) as create_exc:
        await router_mod.create_agent_run(router_mod.RunCreate(stages=["ingest"]), db=db_conn)
    assert create_exc.value.status_code == 404
    with pytest.raises(HTTPException) as list_exc:
        await router_mod.list_agent_runs(db=db_conn)
    assert list_exc.value.status_code == 404


async def test_schema_is_created_lazily_on_bare_db():
    """agent 两张表不依赖 _migrate 顺序：裸库首次 ensure 即建好。

    注：ai_audit_logs 由 SCHEMA_SQL 提供（本模块不重复其 DDL），故此处只验建表。
    """
    conn = await aiosqlite.connect(":memory:")
    try:
        await sm.ensure_schema(conn)
        await sm.ensure_schema(conn)  # 幂等
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in await cur.fetchall()}
        assert {"agent_runs", "agent_stage_runs"} <= names
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 1. 正常迁移 + 审计留痕
# ---------------------------------------------------------------------------


async def test_run_lifecycle_writes_one_audit_per_transition(db_conn):
    snap = await sm.create_run(db_conn, scheme_id="S1", stages=["ingest", "outline"])
    assert snap["run"]["status"] == "pending"
    assert snap["run"]["current_stage"] == "ingest"
    assert [s["stage"] for s in snap["stages"]] == ["ingest", "outline"]

    await sm.start_run(db_conn, snap["run"]["id"])
    await sm.start_stage(db_conn, snap["run"]["id"], "ingest", input_hash="in-1")
    mid = await sm.complete_stage(db_conn, snap["run"]["id"], "ingest", status="succeeded", artifact={"n": 1})
    assert mid["run"]["status"] == "running"
    assert mid["next_stage"] == "outline"

    await sm.start_stage(db_conn, snap["run"]["id"], "outline", input_hash="in-2")
    done = await sm.complete_stage(db_conn, snap["run"]["id"], "outline", status="succeeded", artifact={"n": 2})
    assert done["run"]["status"] == "succeeded"
    assert done["run"]["current_stage"] == ""
    assert done["next_stage"] is None

    # create + run start + 2×(stage start / stage succeeded) + run succeeded
    assert await _audit_count(db_conn) == 7
    assert await _audit_count(db_conn, "agent_run_create") == 1
    assert await _audit_count(db_conn, "agent_run_start") == 1
    assert await _audit_count(db_conn, "agent_stage_start") == 2
    assert await _audit_count(db_conn, "agent_stage_succeeded") == 2
    assert await _audit_count(db_conn, "agent_run_succeeded") == 1

    row = (await _audit_rows(db_conn, "agent_stage_succeeded"))[0]
    assert row["attempts_json"]
    assert snap["run"]["id"] in row["attempts_json"]


async def test_list_and_get_runs(db_conn):
    a = await sm.create_run(db_conn, scheme_id="S1", stages=["ingest"])
    await sm.create_run(db_conn, scheme_id="S2", stages=["ingest"])
    rows = await sm.list_runs(db_conn, scheme_id="S1")
    assert [r["id"] for r in rows] == [a["run"]["id"]]
    assert await sm.get_run(db_conn, "missing") is None
    assert await sm.get_run(db_conn, a["run"]["id"]) is not None


# ---------------------------------------------------------------------------
# 2. 非法迁移拒绝矩阵（拒绝时零写入）
# ---------------------------------------------------------------------------


async def test_illegal_transitions_are_rejected_without_writes(db_conn):
    snap = await sm.create_run(db_conn, stages=["ingest", "outline"])
    run_id = snap["run"]["id"]

    # pending stage 不能直接完成
    with pytest.raises(sm.StageTransitionError):
        await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={})
    # run 未启动不能启动 stage
    with pytest.raises(sm.StageTransitionError):
        await sm.start_stage(db_conn, run_id, "ingest")

    await sm.start_run(db_conn, run_id)
    # 越级启动
    with pytest.raises(sm.StageTransitionError):
        await sm.start_stage(db_conn, run_id, "outline")
    # 重复启动 run
    with pytest.raises(sm.RunStateError):
        await sm.start_run(db_conn, run_id)

    await sm.start_stage(db_conn, run_id, "ingest", input_hash="h")
    # 重复启动 stage
    with pytest.raises(sm.StageTransitionError):
        await sm.start_stage(db_conn, run_id, "ingest")
    # 非法完成状态
    with pytest.raises(ValueError):
        await sm.complete_stage(db_conn, run_id, "ingest", status="bogus")

    audit_before = await _audit_count(db_conn)
    await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={"v": 1})
    audit_after = await _audit_count(db_conn)
    assert audit_after == audit_before + 1  # 仅 stage 迁移；run 仍是 running（无 run 级迁移审计）

    # 已完成 stage 用不同产物重放 → 冲突
    with pytest.raises(sm.StageArtifactConflict):
        await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={"v": 2})
    assert await _audit_count(db_conn) == audit_after


# ---------------------------------------------------------------------------
# 3. 强杀后续跑（断点续跑）
# ---------------------------------------------------------------------------


async def test_resume_after_hard_kill_keeps_completed_stage_and_recovers_running(db_conn):
    snap = await sm.create_run(db_conn, stages=["ingest", "outline", "content"])
    run_id = snap["run"]["id"]
    await sm.start_run(db_conn, run_id)
    await sm.start_stage(db_conn, run_id, "ingest", input_hash="in-1")
    await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={"facts": 3})
    await sm.start_stage(db_conn, run_id, "outline", input_hash="in-2")  # 模拟此刻进程被强杀

    before = await sm.get_run(db_conn, run_id)
    ingest_hash = _stage_by_name(before, "ingest")["output_hash"]
    assert ingest_hash

    resumed = await sm.resume_run(db_conn, run_id)
    assert resumed["terminal"] is False
    assert resumed["next_stage"] == "outline"
    ingest = _stage_by_name(resumed, "ingest")
    assert ingest["status"] == "succeeded"
    assert ingest["output_hash"] == ingest_hash  # 已完成阶段产物 hash 不被重算
    outline = _stage_by_name(resumed, "outline")
    assert outline["status"] == "pending"
    assert outline["error"] == "interrupted"
    assert await _audit_count(db_conn, "agent_stage_recover") == 1

    # 幂等：再次续跑不产生新的恢复审计
    count_before = await _audit_count(db_conn)
    again = await sm.resume_run(db_conn, run_id)
    assert again["next_stage"] == "outline"
    assert await _audit_count(db_conn) == count_before

    # 续跑后重跑被中断阶段：attempts=2，完成后 run 继续前进
    restarted = await sm.start_stage(db_conn, run_id, "outline", input_hash="in-2")
    assert _stage_by_name(restarted, "outline")["attempts"] == 2
    await sm.complete_stage(db_conn, run_id, "outline", status="succeeded", artifact={"outline": 1})
    final = await sm.resume_run(db_conn, run_id)
    assert final["next_stage"] == "content"
    assert final["run"]["status"] == "running"


# ---------------------------------------------------------------------------
# 4. stage 产物 hash 幂等
# ---------------------------------------------------------------------------


async def test_stage_artifact_hash_is_canonical_and_replay_is_noop(db_conn):
    assert sm.artifact_hash({"b": 2, "a": 1}) == sm.artifact_hash({"a": 1, "b": 2})
    assert sm.artifact_hash({"a": 1}) != sm.artifact_hash({"a": 2})

    snap = await sm.create_run(db_conn, stages=["ingest"])
    run_id = snap["run"]["id"]
    await sm.start_run(db_conn, run_id)
    await sm.start_stage(db_conn, run_id, "ingest", input_hash="same-input")
    first = await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={"a": 1, "b": 2})
    stage = _stage_by_name(first, "ingest")
    assert first["reused"] is False
    hash_before, attempts_before = stage["output_hash"], stage["attempts"]
    audit_before = await _audit_count(db_conn)

    # 同一产物（键序不同）重放 → no-op，不重复计数、不写审计
    replay = await sm.complete_stage(db_conn, run_id, "ingest", status="succeeded", artifact={"b": 2, "a": 1})
    assert replay["reused"] is True
    replay_stage = _stage_by_name(replay, "ingest")
    assert replay_stage["output_hash"] == hash_before
    assert replay_stage["attempts"] == attempts_before
    assert await _audit_count(db_conn) == audit_before

    # 跨 run 同输入同产物 → 同一 hash（确定性）
    snap2 = await sm.create_run(db_conn, stages=["ingest"])
    run2 = snap2["run"]["id"]
    await sm.start_run(db_conn, run2)
    await sm.start_stage(db_conn, run2, "ingest", input_hash="same-input")
    second = await sm.complete_stage(db_conn, run2, "ingest", status="succeeded", artifact={"a": 1, "b": 2})
    assert _stage_by_name(second, "ingest")["output_hash"] == hash_before


# ---------------------------------------------------------------------------
# 5. undetermined 独立终态
# ---------------------------------------------------------------------------


async def test_undetermined_is_independent_terminal_state(db_conn):
    snap = await sm.create_run(db_conn, stages=["ingest", "outline"])
    run_id = snap["run"]["id"]
    await sm.start_run(db_conn, run_id)
    await sm.start_stage(db_conn, run_id, "ingest", input_hash="in")
    done = await sm.complete_stage(db_conn, run_id, "ingest", status="undetermined", error="语料缺失，无法判定")
    assert done["run"]["status"] == "undetermined"
    assert done["run"]["status"] != "failed"
    assert _stage_by_name(done, "ingest")["error"] == "语料缺失，无法判定"
    assert await _audit_count(db_conn, "agent_run_undetermined") == 1
    assert await _audit_count(db_conn, "agent_run_failed") == 0

    # 终态续跑：不改状态、不写审计
    audit_before = await _audit_count(db_conn)
    resumed = await sm.resume_run(db_conn, run_id)
    assert resumed["terminal"] is True
    assert resumed["run"]["status"] == "undetermined"
    assert resumed["next_stage"] is None
    assert await _audit_count(db_conn) == audit_before

    # 终态 run 不允许再启动后续 stage
    with pytest.raises(sm.StageTransitionError):
        await sm.start_stage(db_conn, run_id, "outline")


# ---------------------------------------------------------------------------
# 6. 路由层（开关打开）
# ---------------------------------------------------------------------------


async def test_router_create_get_resume_when_enabled(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "agent_runs_enabled", True, raising=False)
    created = await router_mod.create_agent_run(
        router_mod.RunCreate(project_id="P1", scheme_id="S1", stages=["ingest"]), db=db_conn
    )
    run_id = created["run"]["id"]
    detail = await router_mod.get_agent_run(run_id, db=db_conn)
    assert detail["run"]["status"] == "pending"
    assert detail["run"]["project_id"] == "P1"

    await router_mod.start_agent_run(run_id, db=db_conn)
    await router_mod.start_agent_stage(run_id, "ingest", router_mod.StageStart(input_hash="h"), db=db_conn)
    completed = await router_mod.complete_agent_stage(
        run_id, "ingest", router_mod.StageComplete(status="succeeded", artifact={"ok": True}), db=db_conn
    )
    assert completed["run"]["status"] == "succeeded"

    listed = await router_mod.list_agent_runs(scheme_id="S1", db=db_conn)
    assert listed["total"] == 1

    with pytest.raises(HTTPException) as missing:
        await router_mod.get_agent_run("nope", db=db_conn)
    assert missing.value.status_code == 404
