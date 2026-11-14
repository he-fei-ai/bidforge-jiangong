"""Agent 引擎路由（R239 · omp 主路径 + 降级路径 + 可双轨）。

把 ``agent_engine`` 接成生产可达链路，并落实「前端通过 SSE 抛出明确异常信息，
提供人工介入恢复入口」：

* ``GET  /api/v1/omp/agent/config`` —— 只读脱敏配置 + 全局 Feature Flag + 降级链；
* ``POST /api/v1/omp/agent/execute`` —— 主路径执行；失败按链降级，全链失败返回 ``paused``；
* ``POST /api/v1/omp/agent/execute-stream`` —— SSE；挂起推 ``agent_paused`` 事件；
* ``GET  /api/v1/omp/agent/paused`` / ``GET /paused/{task_id}`` —— 挂起现场查询；
* ``POST /api/v1/omp/agent/paused/{task_id}/resume`` —— 人工介入恢复。

⚠️ **可双轨**：请求体 ``prefer`` 可取 ``omp|local|main|auto`` 直接选轨；
  ``allow_fallback=false`` 则关闭本次降级（主路径失败即挂起）。
⚠️ 降级**显式标注**：结果加法式追加 ``engine_used`` / ``degraded`` /
  ``engine_reason``（复用 ``omp_engine_router.annotate_result`` 单一事实源）。
⚠️ 分层约束：判据全在 ``app.services.ai.agent_engine``，routers 只做参数编排。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.services.ai.agent_engine import AgentTask, get_gateway, public_config

router = APIRouter(prefix="/api/v1/omp/agent", tags=["omp-agent"])

_TASK_FIELDS = frozenset(AgentTask.__dataclass_fields__)


def _task_from_body(body: dict[str, Any]) -> AgentTask:
    data = body if isinstance(body, dict) else {}
    return AgentTask(**{k: v for k, v in data.items() if k in _TASK_FIELDS})


def _route_options(body: dict[str, Any]) -> dict[str, Any]:
    """抽出双轨偏好与降级开关（不进 AgentTask，避免污染任务契约）。"""
    data = body if isinstance(body, dict) else {}
    raw = data.get("allow_fallback")
    allow: bool | None = None
    if raw is not None:
        allow = str(raw).strip().lower() not in ("false", "0", "no", "off")
    return {"prefer": str(data.get("prefer") or "").strip().lower(), "allow_fallback": allow}


def _annotated(result: Any) -> dict[str, Any]:
    """把引擎选择标注进结果（加法式，复用 R236 单一事实源）。"""
    from app.services.ai.omp_engine_router import EngineChoice, annotate_result

    payload = result.to_dict()
    meta = dict(result.meta or {})
    degraded = bool(meta.get("degraded"))
    return annotate_result(
        payload,
        EngineChoice(
            engine=str(meta.get("engine_used") or result.engine),
            degraded=degraded,
            reason=str(meta.get("engine_reason") or ""),
        ),
    )


def _sse(event: dict[str, Any]) -> str:
    """与既有 SSE 端点同口径：``data: {json}`` 文本帧。"""
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


@router.get("/config")
async def config_get():
    """只读脱敏配置（含全局 Feature Flag 与降级链；永不回传凭据内容）。"""
    return public_config()


@router.post("/execute")
async def execute(body: dict[str, Any] | None = None):
    """omp 主路径 → 降级链；全链失败不抛 HTTP 异常，返回 ``paused`` + 恢复入口。

    刻意不用 409/502：挂起是**任务状态**而非网关错误，前端需拿到
    ``errors`` / ``manual_review`` / ``resume_url`` 才能渲染人工介入入口。
    """
    body = body or {}
    task = _task_from_body(body)
    result = await get_gateway().execute(task, **_route_options(body))
    return _annotated(result)


@router.post("/execute-stream")
async def execute_stream(body: dict[str, Any] | None = None):
    """SSE：降级推 ``degraded=true``，全链失败推 ``agent_paused`` 明确异常。"""
    body = body or {}
    task = _task_from_body(body)
    options = _route_options(body)
    gateway = get_gateway()

    async def _gen():
        yield _sse({"event": "agent_started", "engine": "omp", **public_config()})
        result = await gateway.execute(task, **options)
        yield _sse(gateway.sse_event(task, result))

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/paused")
async def paused_list():
    """挂起任务清单（人工介入看板数据源）。"""
    return {"items": get_gateway().list_paused()}


@router.get("/paused/{task_id}")
async def paused_get(task_id: str):
    record = get_gateway().get_paused(task_id)
    if record is None:
        raise HTTPException(404, f"挂起任务不存在：{task_id}")
    return record


@router.post("/paused/{task_id}/resume")
async def paused_resume(task_id: str, body: dict[str, Any] | None = None):
    """人工介入恢复：重建任务后重跑 omp 主路径 + 降级链。"""
    instruction = str((body or {}).get("instruction") or "")
    result = await get_gateway().resume(task_id, instruction=instruction)
    return _annotated(result)
