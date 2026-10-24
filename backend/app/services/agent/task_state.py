"""P4 — Agent 任务持久化：持久 task_key 的 create / resume。

移植来源（易标语义）：
- ``persistent_task = { task_key, mode: 'create' | 'resume' }``
  （outlineGenerationTaskV2.cjs 的 ``runTask`` 入参）；
- ``checkpointPersistentTask({status, phase, agent_connection,
  prompted_stage, compaction_pending, ...})``（piRuntimeService.cjs 的
  阶段推进断点写入）。

本仓映射（不建新表、不改 task_registry 既有行为、纯加法）：
- **task_key 是逻辑身份**：稳定业务键，存入 ``task_registry.checkpoint_json``；
  task_registry 行 id 是单次运行的注册 id（uuid4），一行 = 一次运行；
- **断点字段**沿用易标原字段名逐字写入 checkpoint_json（``kind``
  ``= "outline_agent"`` 与其他成果 checkpoint 区分，避免 sse_checkpoint
  的 kind 校验互相串台）；
- **create** = ``register_task`` 注册新行（复用本仓暂停/停止/进度/互斥能力）；
- **resume** = 按 task_key 读回最近断点 → 重新注册一行继续跑；旧行若仍
  running 会被 register_task 的防僵尸逻辑标记 failed 并触发停止，
  结构上不会出现同 scheme 同类型双跑；
- checkpoint 合并写经 ``app.db.retry_db_op`` 幂等重试（与 task_registry
  终态写同级别的瞬态锁容忍）。

红线：
1. ``kind`` / ``task_key`` 是身份字段，checkpoint 合并时**不可被覆盖**；
2. resume 必须保留断点中的 ``stage_index`` / ``workflow_stage`` /
   ``prompted_stage`` / ``phase``，只把运行时状态（status /
   agent_connection）复位为运行中；
3. 所有 DB 读写失败不吞异常语义之外的业务错误：查询无果返回 ``None``，
   对不存在的任务做断点写入/ resume 抛 ``ValueError``（编程错误应显式暴露）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.db import get_conn, get_read_conn, release_read_conn, retry_db_op
from app.services.ai.task_registry import register_task

#: 本类断点在 checkpoint_json 里的 kind（与 outline_result/content_result
#: 等既有 kind 互不覆盖，sse_checkpoint._load_task_checkpoint 的 kind 校验
#: 会天然忽略 agent 断点）。
AGENT_CHECKPOINT_KIND = "outline_agent"

#: Agent 任务在 task_registry 的 task_type：直接复用目录生成既有类型，
#: 使「同 scheme 同类型防僵尸 + 跨类型互斥」自动生效，不引入新类型词表。
DEFAULT_TASK_TYPE = "outline_generation"

#: 易标 checkpointPersistentTask 的断点字段（逐字保留，勿改名）。
AGENT_CHECKPOINT_FIELDS: tuple[str, ...] = (
    "status",
    "phase",
    "agent_connection",
    "prompted_stage",
    "compaction_pending",
    "stage_index",
    "workflow_stage",
)

#: 合并写时不可覆盖的身份字段。
_PROTECTED_FIELDS: frozenset[str] = frozenset({"kind", "task_key"})


def build_agent_checkpoint(
    task_key: str,
    *,
    phase: str = "",
    stage_index: int = 0,
    workflow_stage: str = "",
    prompted_stage: str = "",
    compaction_pending: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 create/resume 使用的初始断点（易标字段名逐字）。

    ``status`` / ``agent_connection`` 初始化为运行中 —— 与 register_task
    插入行时的 ``status='running'`` 对齐（checkpoint 内的 status 是易标
    语义的业务状态，与 task_registry 行状态是两套词表，互不改写）。
    """
    if not isinstance(task_key, str) or not task_key.strip():
        raise ValueError("task_key 必须是非空字符串")
    checkpoint: dict[str, Any] = {
        "kind": AGENT_CHECKPOINT_KIND,
        "task_key": task_key,
        "status": "running",
        "phase": str(phase or ""),
        "agent_connection": "running",
        "prompted_stage": str(prompted_stage or ""),
        "compaction_pending": compaction_pending,
        "stage_index": int(stage_index or 0),
        "workflow_stage": str(workflow_stage or ""),
    }
    if extra:
        for key, value in extra.items():
            if key in _PROTECTED_FIELDS:
                continue  # 身份字段不可由 extra 改写
            checkpoint[key] = value
    return checkpoint


async def create_agent_task(
    task_key: str,
    *,
    project_id: str = "",
    scheme_id: str = "",
    conflict_types=(),
    phase: str = "",
    stage_index: int = 0,
    workflow_stage: str = "",
    prompted_stage: str = "",
    compaction_pending: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """create 模式：注册一次新的运行（易标 ``persistent_task.mode='create'``）。

    Returns:
        task_registry 行 id（单次运行的注册 id，非 task_key）。
    """
    checkpoint = build_agent_checkpoint(
        task_key, phase=phase, stage_index=stage_index,
        workflow_stage=workflow_stage, prompted_stage=prompted_stage,
        compaction_pending=compaction_pending, extra=extra)
    return await register_task(
        DEFAULT_TASK_TYPE, project_id, scheme_id,
        checkpoint=checkpoint, conflict_types=conflict_types)


async def _write_checkpoint(task_id: str, merged: dict[str, Any]) -> None:
    """单语句断点落库（幂等，供 retry_db_op 重放）。"""
    conn = await get_conn()
    await conn.execute(
        "UPDATE task_registry SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps(merged, ensure_ascii=False),
         datetime.now().isoformat(), task_id))
    await conn.commit()


async def checkpoint_agent_task(task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """合并写断点（易标 ``checkpointPersistentTask`` 的本仓对应）。

    - 读回现有 checkpoint_json → 合并 ``fields`` → 整体写回；
    - ``kind`` / ``task_key`` 身份字段被静默保护：请求里带了也不采纳；
    - 任务行不存在时抛 ``ValueError``（调用方拿错 id 是编程错误）；
    - 瞬态锁错误由 ``retry_db_op`` 退避重试，确定性错误原样上抛。
    """
    if not isinstance(fields, dict):
        raise ValueError("fields 必须是 dict")
    incoming = {k: v for k, v in fields.items() if k not in _PROTECTED_FIELDS}
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT checkpoint_json FROM task_registry WHERE id=?", (task_id,))
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"任务不存在：{task_id}")
    try:
        current = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    merged = {**current, **incoming}
    await retry_db_op(lambda: _write_checkpoint(task_id, merged))
    return merged


async def load_agent_checkpoint(task_key: str) -> dict[str, Any] | None:
    """按 task_key 读回最近一次运行的断点（无果返回 ``None``）。

    用 ``json_valid`` 守卫历史行里可能存在的空串/坏 JSON，
    避免 ``json_extract`` 抛 malformed JSON 污染查询。
    """
    if not isinstance(task_key, str) or not task_key.strip():
        return None
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT checkpoint_json FROM task_registry "
            "WHERE json_valid(checkpoint_json) "
            "AND json_extract(checkpoint_json, '$.kind')=? "
            "AND json_extract(checkpoint_json, '$.task_key')=? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (AGENT_CHECKPOINT_KIND, task_key))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def resume_agent_task(
    task_key: str,
    *,
    project_id: str = "",
    scheme_id: str = "",
    conflict_types=(),
    extra: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """resume 模式：读回断点并重新注册一行继续跑。

    - 断点保真：``stage_index`` / ``workflow_stage`` / ``prompted_stage`` /
      ``phase`` / ``compaction_pending`` 原样带入（易标续跑据此只发送
      「继续之前的任务」）；
    - 运行时状态复位：``status`` / ``agent_connection`` 置回 ``running``；
    - 旧行仍在 running 时由 register_task 的防僵尸逻辑收尾（标记 failed 并
      触发 stop），不会双跑。

    Returns:
        ``(新 task_registry 行 id, 合并后的断点)``。

    Raises:
        ValueError: task_key 无历史断点（无法 resume）。
    """
    old = await load_agent_checkpoint(task_key)
    if old is None:
        raise ValueError(f"task_key 无历史断点，无法 resume：{task_key}")
    carried = {k: v for k, v in old.items() if k not in _PROTECTED_FIELDS}
    carried["status"] = "running"
    carried["agent_connection"] = "running"
    if extra:
        for key, value in extra.items():
            if key in _PROTECTED_FIELDS:
                continue
            carried[key] = value
    checkpoint = build_agent_checkpoint(
        task_key,
        phase=carried.get("phase", ""),
        stage_index=carried.get("stage_index", 0) or 0,
        workflow_stage=carried.get("workflow_stage", ""),
        prompted_stage=carried.get("prompted_stage", ""),
        compaction_pending=carried.get("compaction_pending"),
        extra=carried,
    )
    new_id = await register_task(
        DEFAULT_TASK_TYPE, project_id, scheme_id,
        checkpoint=checkpoint, conflict_types=conflict_types)
    return new_id, checkpoint


__all__ = [
    "AGENT_CHECKPOINT_FIELDS",
    "AGENT_CHECKPOINT_KIND",
    "DEFAULT_TASK_TYPE",
    "build_agent_checkpoint",
    "checkpoint_agent_task",
    "create_agent_task",
    "load_agent_checkpoint",
    "resume_agent_task",
]
