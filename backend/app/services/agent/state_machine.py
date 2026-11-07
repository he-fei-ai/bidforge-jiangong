"""编排状态机（R179 Phase G，2026-11-07）。

设计约束
--------
1. **数据库是唯一事实源**：不持有进程内运行状态；进程被强杀后，新实例直接
   读 ``agent_runs`` / ``agent_stage_runs`` 续跑，不需要内存补偿。
2. **迁移确定性**：只允许白名单迁移（见 ``_RUN_TRANSITIONS`` / stage 迁移表），
   非法迁移一律抛错且不落任何写入（不留半条审计）。
3. **产物 hash 幂等**：``artifact_hash`` 对规范 JSON（键排序、紧凑分隔符）
   取 sha256；同一 stage 重复完成且产物一致时是 no-op，不重复计数、不重复
   写审计；产物不一致必须报冲突，绝不静默覆盖。
4. **每次成功迁移写一条 ``ai_audit_logs``**（``scene=agent_orchestration``）：
   迁移明细放 ``attempts_json``（JSON 数组，单元素），失败原因放 ``error``。
5. **undetermined 是独立终态**：与 failed 并列，续跑不会把它改成成功，也不会
   把「无法判定」伪装成「已通过」。

建表方式
--------
``ensure_schema`` 采用幂等惰性初始化（``CREATE TABLE IF NOT EXISTS``，先查
``sqlite_master`` 命中即跳过）：不依赖 ``_migrate`` 的执行顺序，服务首次调用
即就绪，老库/测试库/内存库行为一致。
"""

from __future__ import annotations

import hashlib
import json
import uuid

#: 默认阶段计划（与 L1–L5 流水线对齐）；调用方可在建 run 时自定义顺序。
DEFAULT_STAGES: tuple[str, ...] = ("ingest", "facts", "outline", "content", "review", "export")

RUN_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "undetermined"})
STAGE_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "undetermined"})
_TERMINAL = frozenset({"succeeded", "failed", "undetermined"})
_AUDIT_SCENE = "agent_orchestration"
#: 本模块写入 ai_audit_logs 的 action 前缀。
#: 收敛护栏（tests/test_audit_budget_config_and_action_enum_20261104.py）静态读取本常量，
#: 并校验本文件内所有 ``_audit(...)`` 的 action 均带该前缀。
_AUDIT_ACTION_PREFIX = "agent_"

_AGENT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT DEFAULT '',
    scheme_id TEXT DEFAULT '',
    run_type TEXT NOT NULL DEFAULT 'full_pipeline',
    status TEXT NOT NULL DEFAULT 'pending',
    current_stage TEXT DEFAULT '',
    stages_json TEXT DEFAULT '[]',
    context_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_scheme ON agent_runs(scheme_id, status);

CREATE TABLE IF NOT EXISTS agent_stage_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    input_hash TEXT DEFAULT '',
    output_hash TEXT DEFAULT '',
    artifact_json TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    started_at TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (run_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_agent_stage_runs_run ON agent_stage_runs(run_id, seq);
"""


class AgentStateError(RuntimeError):
    """编排状态机错误基类。"""


class RunStateError(AgentStateError):
    """run 级迁移非法（如非 pending 启动）。"""


class StageTransitionError(AgentStateError):
    """stage 级迁移非法（顺序、状态、所属 run 状态不符合前置条件）。"""


class StageArtifactConflict(AgentStateError):
    """同一输入下的产物 hash 与已记录值不一致（幂等性被破坏）。"""


def canonical_json(value) -> str:
    """规范 JSON：键排序 + 紧凑分隔符，保证同内容不同键序得到同一字节串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def artifact_hash(artifact) -> str:
    """stage 产物哈希（对规范 JSON 取 sha256）；``None`` 视为空对象。"""
    payload = {} if artifact is None else artifact
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


async def ensure_schema(db) -> None:
    """幂等建表：``sqlite_master`` 命中即跳过，避免每次调用重放 DDL。"""
    cur = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'")
    if await cur.fetchone() is None:
        await db.executescript(_AGENT_SCHEMA_DDL)


async def _audit(
    db,
    action: str,
    *,
    run_id: str,
    stage: str = "",
    from_status: str = "",
    to_status: str = "",
    input_hash: str = "",
    output_hash: str = "",
    error: str = "",
) -> None:
    """写一条迁移审计（与状态更新同事务，调用方负责 commit）。

    ⚠️ 写的是**任务/阶段状态迁移审计**，不是 AI 调用审计：不填 provider/model、
    不计 token 与耗时，``attempt_count`` 恒为 1。``success`` 列恒为 1 仅表示
    「迁移动作已落库」，**不表示调用成功** —— 不得据此统计成功率（真实的迁移
    结果看 ``attempts_json`` 里的 ``to`` 与 ``error`` 列）。
    """
    detail = [
        {
            "run_id": run_id,
            "stage": stage,
            "from": from_status,
            "to": to_status,
            "input_hash": input_hash,
            "output_hash": output_hash,
        }
    ]
    await db.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action, success, error, scene,"
        " attempt_count, attempts_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "", "", action, 1, error, _AUDIT_SCENE, 1, canonical_json(detail)),
    )


async def _require_run(db, run_id: str):
    cur = await db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    row = await cur.fetchone()
    if not row:
        raise LookupError(f"run 不存在: {run_id}")
    return row


async def _require_stage(db, run_id: str, stage: str):
    cur = await db.execute("SELECT * FROM agent_stage_runs WHERE run_id=? AND stage=?", (run_id, stage))
    row = await cur.fetchone()
    if not row:
        raise LookupError(f"stage 不存在: {run_id}/{stage}")
    return row


async def _load_stages(db, run_id: str):
    cur = await db.execute("SELECT * FROM agent_stage_runs WHERE run_id=? ORDER BY seq", (run_id,))
    return await cur.fetchall()


def _run_dict(row) -> dict:
    out = dict(row)
    out["stages"] = json.loads(out.pop("stages_json", "") or "[]")
    out["context"] = json.loads(out.pop("context_json", "") or "{}")
    return out


def _stage_dict(row) -> dict:
    out = dict(row)
    raw = out.pop("artifact_json", "") or ""
    out["artifact"] = json.loads(raw) if raw else None
    return out


async def _snapshot(db, run_id: str, **extra) -> dict:
    run = await _require_run(db, run_id)
    stages = await _load_stages(db, run_id)
    data = {"run": _run_dict(run), "stages": [_stage_dict(s) for s in stages]}
    data.update(extra)
    return data


async def create_run(
    db,
    *,
    project_id: str = "",
    scheme_id: str = "",
    run_type: str = "full_pipeline",
    stages: list[str] | tuple[str, ...] | None = None,
    context: dict | None = None,
) -> dict:
    """建 run 并预置全部 stage 行为 pending（stage id 由 run_id+stage 确定性生成）。"""
    await ensure_schema(db)
    plan = [str(s).strip() for s in (stages if stages is not None else DEFAULT_STAGES)]
    if not plan or any(not s for s in plan):
        raise ValueError("stage 计划不能为空且不能含空名")
    if len(set(plan)) != len(plan):
        raise ValueError("stage 计划不允许重名")

    run_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO agent_runs (id, project_id, scheme_id, run_type, status, current_stage,"
        " stages_json, context_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            project_id,
            scheme_id,
            run_type,
            "pending",
            plan[0],
            canonical_json(plan),
            canonical_json(context or {}),
        ),
    )
    for seq, stage in enumerate(plan):
        await db.execute(
            "INSERT INTO agent_stage_runs (id, run_id, stage, seq, status) VALUES (?,?,?,?,?)",
            (f"{run_id}:{stage}", run_id, stage, seq, "pending"),
        )
    await _audit(db, "agent_run_create", run_id=run_id, to_status="pending")
    await db.commit()
    return await _snapshot(db, run_id)


async def get_run(db, run_id: str) -> dict | None:
    await ensure_schema(db)
    cur = await db.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,))
    if await cur.fetchone() is None:
        return None
    return await _snapshot(db, run_id)


async def list_runs(db, *, scheme_id: str = "", status: str = "", limit: int = 20) -> list[dict]:
    await ensure_schema(db)
    where, params = [], []
    if scheme_id:
        where.append("scheme_id=?")
        params.append(scheme_id)
    if status:
        if status not in RUN_STATUSES:
            raise ValueError(f"未知 run 状态: {status}")
        where.append("status=?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 20), 100))
    cur = await db.execute(
        f"SELECT * FROM agent_runs{clause} ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit)
    )
    return [_run_dict(r) for r in await cur.fetchall()]


async def start_run(db, run_id: str) -> dict:
    """run: pending → running。"""
    await ensure_schema(db)
    run = await _require_run(db, run_id)
    if run["status"] != "pending":
        raise RunStateError(f"run 只能从 pending 启动（当前 {run['status']}）")
    await db.execute(
        "UPDATE agent_runs SET status='running', updated_at=datetime('now','localtime') WHERE id=?",
        (run_id,),
    )
    await _audit(db, "agent_run_start", run_id=run_id, from_status="pending", to_status="running")
    await db.commit()
    return await _snapshot(db, run_id)


async def start_stage(db, run_id: str, stage: str, *, input_hash: str = "") -> dict:
    """stage: pending → running；必须严格按计划的 ``current_stage`` 顺序推进。"""
    await ensure_schema(db)
    run = await _require_run(db, run_id)
    row = await _require_stage(db, run_id, stage)
    if run["status"] != "running":
        raise StageTransitionError(f"run 未处于 running（当前 {run['status']}），禁止启动 stage")
    if row["status"] != "pending":
        raise StageTransitionError(f"stage {stage} 只能从 pending 启动（当前 {row['status']}）")
    if run["current_stage"] != stage:
        raise StageTransitionError(f"必须按计划顺序推进：当前应为 {run['current_stage']}，请求 {stage}")

    await db.execute(
        "UPDATE agent_stage_runs SET status='running', input_hash=?, attempts=attempts+1,"
        " started_at=datetime('now','localtime'), error='', updated_at=datetime('now','localtime')"
        " WHERE id=?",
        (input_hash or "", f"{run_id}:{stage}"),
    )
    await _audit(
        db,
        "agent_stage_start",
        run_id=run_id,
        stage=stage,
        from_status="pending",
        to_status="running",
        input_hash=input_hash or "",
    )
    await db.commit()
    return await _snapshot(db, run_id)


async def complete_stage(
    db,
    run_id: str,
    stage: str,
    *,
    status: str,
    artifact=None,
    error: str = "",
) -> dict:
    """stage: running → succeeded / failed / undetermined，并同步推进 run 状态。

    ``succeeded`` 时 ``artifact`` 为产物快照；重复完成同一 stage 且产物 hash
    一致时返回 ``reused=True`` 的 no-op（不写审计、不重复计数）。
    """
    await ensure_schema(db)
    if status not in _TERMINAL:
        raise ValueError(f"完成状态只能是 {sorted(_TERMINAL)}，收到 {status!r}")

    run = await _require_run(db, run_id)
    row = await _require_stage(db, run_id, stage)

    if row["status"] == "succeeded":
        new_hash = artifact_hash(artifact) if status == "succeeded" else ""
        if status == "succeeded" and new_hash == row["output_hash"]:
            return await _snapshot(db, run_id, reused=True, next_stage=None)
        raise StageArtifactConflict(
            f"stage {stage} 已完成，重复完成必须产物 hash 一致："
            f"已存 {row['output_hash'] or '(空)'}，本次 {new_hash or '(空)'}"
        )
    if row["status"] != "running":
        raise StageTransitionError(f"stage {stage} 只能从 running 完成（当前 {row['status']}）")

    artifact_json = canonical_json(artifact) if status == "succeeded" and artifact is not None else ""
    output_hash = artifact_hash(artifact) if status == "succeeded" else ""
    await db.execute(
        "UPDATE agent_stage_runs SET status=?, output_hash=?, artifact_json=?, error=?,"
        " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
        (status, output_hash, artifact_json, error or "", f"{run_id}:{stage}"),
    )

    old_run_status = run["status"]
    next_stage: str | None = None
    if status == "failed":
        new_run_status = "failed"
    elif status == "undetermined":
        new_run_status = "undetermined"
    else:
        stages = await _load_stages(db, run_id)
        pending = [s for s in stages if s["status"] != "succeeded"]
        if pending:
            new_run_status = "running"
            next_stage = pending[0]["stage"]
        else:
            new_run_status = "succeeded"

    current_stage = next_stage or ""
    await db.execute(
        "UPDATE agent_runs SET status=?, current_stage=?, updated_at=datetime('now','localtime') WHERE id=?",
        (new_run_status, current_stage, run_id),
    )
    await _audit(
        db,
        f"agent_stage_{status}",
        run_id=run_id,
        stage=stage,
        from_status="running",
        to_status=status,
        output_hash=output_hash,
        error=error or "",
    )
    if new_run_status != old_run_status:
        await _audit(
            db,
            f"agent_run_{new_run_status}",
            run_id=run_id,
            from_status=old_run_status,
            to_status=new_run_status,
            error=error or "",
        )
    await db.commit()
    return await _snapshot(db, run_id, reused=False, next_stage=next_stage)


async def resume_run(db, run_id: str) -> dict:
    """断点续跑：恢复被强杀时遗留的 running 阶段，并返回下一个待执行阶段。

    - terminal run（含 undetermined）不改变任何状态，``terminal=True``；
    - 残留 ``running`` 的 stage 退回 ``pending`` 并记 ``error='interrupted'``；
    - ``pending`` 的 run 顺带提升为 ``running``；
    - 已 succeeded 的 stage 原样保留（产物 hash 不被重算覆盖）。
    """
    await ensure_schema(db)
    run = await _require_run(db, run_id)
    stages = await _load_stages(db, run_id)

    if run["status"] in _TERMINAL:
        return await _snapshot(db, run_id, terminal=True, next_stage=None)

    changed = False
    if run["status"] == "pending":
        await db.execute(
            "UPDATE agent_runs SET status='running', updated_at=datetime('now','localtime') WHERE id=?", (run_id,)
        )
        await _audit(db, "agent_run_resume", run_id=run_id, from_status="pending", to_status="running")
        changed = True

    for row in stages:
        if row["status"] == "running":
            await db.execute(
                "UPDATE agent_stage_runs SET status='pending', error='interrupted',"
                " updated_at=datetime('now','localtime') WHERE id=?",
                (row["id"],),
            )
            await _audit(
                db,
                "agent_stage_recover",
                run_id=run_id,
                stage=row["stage"],
                from_status="running",
                to_status="pending",
                input_hash=row["input_hash"] or "",
                error="interrupted",
            )
            changed = True

    pending = [s for s in stages if s["status"] != "succeeded"]
    next_stage = pending[0]["stage"] if pending else None
    if next_stage is None:
        await db.execute(
            "UPDATE agent_runs SET status='succeeded', current_stage='',"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (run_id,),
        )
        await _audit(db, "agent_run_succeeded", run_id=run_id, from_status=run["status"], to_status="succeeded")
        changed = True
    elif run["current_stage"] != next_stage:
        await db.execute(
            "UPDATE agent_runs SET current_stage=?, updated_at=datetime('now','localtime') WHERE id=?",
            (next_stage, run_id),
        )
        changed = True

    if changed:
        await db.commit()
    return await _snapshot(db, run_id, terminal=False, next_stage=next_stage)
