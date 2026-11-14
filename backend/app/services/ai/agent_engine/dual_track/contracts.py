"""双轨 Agent 引擎 · 统一契约（R240 · 路径 B 注入执行器 POC）。

* :class:`IAgentExecutor` —— 业务层与底层执行器之间的唯一接口；
* :func:`normalize_result` —— 把执行器任意返回（含 omp 畸形结构）归一化为
  :class:`AgentResult`，**白名单之外的内容绝不进入业务层数据模型**。

⚠️ 数据契约直接复用 ``agent_engine.contracts.AgentTask / AgentResult``，
   不新造第二份业务模型 —— 这是「业务层无感知」的前提。
⚠️ 方案以 TypeScript 伪代码给出的 ``executeTask(task): Promise<AgentResult>``，
   在本仓 Python/FastAPI 技术栈中等价落地为 ``async def executeTask(task) -> AgentResult``。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

# 复用既有业务数据模型（单一事实源），禁止另建一份 AgentTask/AgentResult。
from app.services.ai.agent_engine.contracts import AgentResult, AgentTask

from .config import ENGINE_MOCK, ENGINE_OMP, ENGINE_PI

logger = logging.getLogger("ai_provider.agent_engine.dual_track")

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"

__all__ = [
    "ENGINE_MOCK",
    "ENGINE_OMP",
    "ENGINE_PI",
    "AgentResult",
    "AgentTask",
    "IAgentExecutor",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "failed_result",
    "normalize_result",
]


@runtime_checkable
class IAgentExecutor(Protocol):
    """底层引擎统一执行器接口。

    契约要点：

    * ``name`` —— 执行器标识（``pi`` / ``omp`` / ``mock_omp``）；
    * ``executeTask`` —— 异步执行，**不向业务层抛异常**，失败收敛为
      ``AgentResult(ok=False)``；
    * 实现类同时提供 ``execute`` 别名，兼容 R237/R239 既有调用方。
    """

    name: str

    async def executeTask(self, task: AgentTask) -> AgentResult:
        """执行任务并返回归一化结果（失败不抛异常）。"""
        ...


#: 允许进入 AgentResult 顶层的字段白名单（防 omp 异常结构污染业务数据模型）。
_RESULT_FIELDS = frozenset({f for f in AgentResult.__dataclass_fields__})
_STR_FIELDS = frozenset(
    {"task_id", "task_type", "engine", "requested_engine", "status", "payload", "reason", "manual_review"}
)
_BOOL_FIELDS = frozenset({"ok", "paused", "scene_preserved"})
_INT_FIELDS = frozenset({"attempts", "elapsed_ms"})


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # 异常结构（dict / list / 对象）只允许变成可诊断的短文本，绝不塞进 payload。
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_as_str(item) for item in value]
    if value is None:
        return []
    return [_as_str(value)]


def _as_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _as_bool(value: Any) -> bool:
    """布尔归一：字符串 "false" / "0" 不能被 bool() 误判为 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return bool(value)


def _coerce_field(name: str, value: Any) -> Any:
    """按业务数据模型的字段类型做强制归一（非法类型一律收敛，不改写模型定义）。"""
    if name in _STR_FIELDS:
        return _as_str(value)
    if name in _BOOL_FIELDS:
        return _as_bool(value)
    if name in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if name == "errors":
        return _as_str_list(value)
    if name == "meta":
        return _as_meta(value)
    return value


def _sanitize(result: AgentResult) -> AgentResult:
    """净化既有 AgentResult：字段类型被畸形结构污染时按白名单重铸。"""
    clean = {
        name: _coerce_field(name, getattr(result, name, None))
        for name in _RESULT_FIELDS
    }
    return AgentResult(**clean)


def failed_result(
    task: AgentTask,
    engine: str,
    *,
    errors: list[str] | None = None,
    reason: str = "",
    attempts: int = 1,
    meta: dict[str, Any] | None = None,
) -> AgentResult:
    """构造失败结果（统一状态口径，供各执行器 / 路由层复用）。"""
    return AgentResult(
        ok=False,
        task_id=task.task_id,
        task_type=task.task_type,
        engine=engine,
        requested_engine=engine,
        status=STATUS_FAILED,
        paused=False,
        attempts=attempts,
        errors=list(errors or []),
        reason=reason,
        meta=dict(meta or {}),
    )


def normalize_result(value: Any, *, task: AgentTask, engine: str) -> AgentResult:
    """把执行器的任意返回归一化为业务层 ``AgentResult``。

    铁律（对应「捕获 omp 返回的异常结构，确保不会污染业务层的数据模型」）：

    * 只有 :data:`_RESULT_FIELDS` 白名单字段可进入 ``AgentResult`` 顶层；
    * 畸形 dict 的未知键**只记录键名**到 ``meta["omp_unknown_fields"]``，
      键对应的值一律丢弃，绝不带入业务模型；
    * 非 dict / 非 str / None / 异常对象一律收敛为失败结果，**不抛异常**。

    返回结果的 ``task_id`` / ``task_type`` 强制以入参任务为准，
    防止 omp 回包篡改业务身份。
    """
    try:
        if isinstance(value, AgentResult):
            result = _sanitize(value)
        elif isinstance(value, dict):
            known: dict[str, Any] = {}
            unknown: list[str] = []
            for key, item in value.items():
                name = str(key)
                if name in _RESULT_FIELDS:
                    known[name] = item
                else:
                    unknown.append(name)
            clean = {
                name: _coerce_field(name, known.get(name))
                for name in _RESULT_FIELDS
            }
            clean.pop("task_id", None)
            clean.pop("task_type", None)
            result = AgentResult(**clean)
            if not result.status:
                result.status = STATUS_COMPLETED if result.ok else STATUS_FAILED
            if unknown:
                meta = dict(result.meta)
                # 只留键名 + 个数，绝不留值；供排障定位 omp 协议漂移。
                meta["omp_unknown_fields"] = sorted(unknown)
                meta["omp_unknown_field_count"] = len(unknown)
                result.meta = meta
                logger.warning(
                    "agent_engine.dual_track: omp 返回含未知字段 %s，已隔离到 meta（未进入业务模型）",
                    ", ".join(sorted(unknown)[:5]),
                )
        elif isinstance(value, str):
            result = AgentResult(
                ok=bool(value),
                status=STATUS_COMPLETED if value else STATUS_FAILED,
                payload=value,
                errors=[] if value else ["执行器返回空产物"],
            )
        else:
            result = failed_result(
                task,
                engine,
                errors=[f"执行器返回非契约类型：{type(value).__name__}"],
                reason="omp 返回结构无法解析为 AgentResult",
            )
    except Exception as exc:  # noqa: BLE001 — 归一化本身也不得向业务层抛异常
        logger.warning("agent_engine.dual_track: 结果归一化异常（已收敛）：%s", exc)
        result = failed_result(
            task,
            engine,
            errors=[f"结果归一化失败：{type(exc).__name__}: {exc}"],
            reason="omp 返回结构无法解析为 AgentResult",
        )

    # 业务身份 / 引擎身份由路由层裁定，禁止回包覆写。
    result.task_id = task.task_id
    result.task_type = task.task_type
    if not result.engine:
        result.engine = engine
    if not result.requested_engine:
        result.requested_engine = engine
    if result.status not in (STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED):
        # 畸形 status（如数字 777）按状态枚举重铸，不把脏值留给业务层。
        result.status = STATUS_COMPLETED if result.ok else STATUS_FAILED
    if not result.ok and result.status == STATUS_COMPLETED:
        result.status = STATUS_FAILED
    return result
