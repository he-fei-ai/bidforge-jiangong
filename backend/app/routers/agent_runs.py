"""编排状态机路由（R179 Phase G，2026-11-07）。

开关 ``settings.agent_runs_enabled`` 默认关闭：关闭时所有入口首行 404，
对既有目录/正文/导出链路零影响（本模块只读写自己的两张新表与审计表）。

状态机语义与迁移白名单见 ``app.services.agent.state_machine``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db
from app.services.agent import state_machine as sm

router = APIRouter(prefix="/api/v1", tags=["agent-runs"])


def _require_enabled() -> None:
    if not settings.agent_runs_enabled:
        raise HTTPException(404, "Not Found")


class RunCreate(BaseModel):
    project_id: str = ""
    scheme_id: str = ""
    run_type: str = Field(default="full_pipeline", max_length=64)
    stages: list[str] | None = None
    context: dict[str, Any] | None = None


class StageStart(BaseModel):
    input_hash: str = Field(default="", max_length=128)


class StageComplete(BaseModel):
    status: str
    artifact: Any = None
    error: str = ""


@router.post("/agent/runs")
async def create_agent_run(body: RunCreate, db=Depends(get_db)):
    _require_enabled()
    try:
        return await sm.create_run(
            db,
            project_id=body.project_id,
            scheme_id=body.scheme_id,
            run_type=body.run_type,
            stages=body.stages,
            context=body.context,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/agent/runs")
async def list_agent_runs(scheme_id: str = "", status: str = "", limit: int = 20, db=Depends(get_db)):
    _require_enabled()
    try:
        items = await sm.list_runs(db, scheme_id=scheme_id, status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"items": items, "total": len(items)}


@router.get("/agent/runs/{run_id}")
async def get_agent_run(run_id: str, db=Depends(get_db)):
    _require_enabled()
    data = await sm.get_run(db, run_id)
    if not data:
        raise HTTPException(404, "run 不存在")
    return data


@router.post("/agent/runs/{run_id}/start")
async def start_agent_run(run_id: str, db=Depends(get_db)):
    _require_enabled()
    try:
        return await sm.start_run(db, run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except sm.RunStateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/agent/runs/{run_id}/stages/{stage}/start")
async def start_agent_stage(run_id: str, stage: str, body: StageStart | None = None, db=Depends(get_db)):
    _require_enabled()
    body = body or StageStart()
    try:
        return await sm.start_stage(db, run_id, stage, input_hash=body.input_hash)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except sm.StageTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/agent/runs/{run_id}/stages/{stage}/complete")
async def complete_agent_stage(run_id: str, stage: str, body: StageComplete, db=Depends(get_db)):
    _require_enabled()
    try:
        return await sm.complete_stage(db, run_id, stage, status=body.status, artifact=body.artifact, error=body.error)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (sm.StageTransitionError, sm.StageArtifactConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/agent/runs/{run_id}/resume")
async def resume_agent_run(run_id: str, db=Depends(get_db)):
    """强杀/重启后从最后完成阶段续跑；返回下一个待执行阶段。"""
    _require_enabled()
    try:
        return await sm.resume_run(db, run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
