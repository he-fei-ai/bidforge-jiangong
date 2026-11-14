"""Agent 引擎统一契约（**单轨**：omp = oh-my-pi 唯一执行器）。

R237（2026-11-13 · 用户裁决「彻底废弃原有 Pi Agent，单轨全面替换为 omp」）
把本模块从「pi / omp / hybrid 双轨 + 失败回退」改为**单轨**：

* :class:`AgentTask` / :class:`AgentResult` —— 业务层与底层执行器之间的数据契约；
* :class:`IAgentExecutor` —— 唯一执行器接口（``async execute(task) -> AgentResult``）；
* :class:`AgentEngineConfig` / :func:`load_agent_engine_config` —— 配置加载与合法性归一
  （**全部参数来自配置文件**：omp 路径 / 重试次数 / 退避基数 / 压缩阈值 / 授权 / Feature Flag）。

⚠️ **无兜底设计**（用户要求）：

* 执行器**不向业务层抛异常**，失败一律收敛为 ``AgentResult(ok=False, paused=True)``；
* **不保留任何旧引擎回退** —— 失败 ⇒ 指数退避重试 ⇒ 重试耗尽或遇致命错误
  （授权失败 / 未安装 / 未配模型）⇒ **任务挂起（paused）并保留现场**，
  由人工介入恢复；**绝不静默改走别的引擎**。

⚠️ 设计边界（与既有 omp 编排层的关系）：既有 ``omp_workflow`` 的 ``executor_fn``
契约 ``(spec, prompt, workspace) -> str`` 与业务层 Prompt / 四阶段流水线
（plan→execute→verify→fix）**保持现状不动**；本模块只提供更上层的
「单轨执行器 + 失败挂起」语义，由 :class:`OmpAgentGateway`
以无损适配暴露给既有编排层。

**分层边界（防「同一判据两份副本」）**：
* ``omp_workflow.platform_executor`` —— 路径 B 的**平台执行原语**：用平台 AI 链
  当「本地 LLM」跑同一套 plan→execute→verify→fix，**不依赖 omp 进程**；
  本地档无文件工具语义，CONTEXT/PLAN 内联进 prompt。
* ``agent_engine.OmpAgentExecutor`` —— **单轨 omp 执行器**：封装 omp 进程启动、
  Provider 授权注入、流式输出解析、上下文压缩、指数退避重试，**调用真 omp 进程**。
* 二者**互补而非重复**：platform_executor 处「本地编排档（不依赖 omp 进程）」，
  OmpAgentExecutor 处「真 omp 档」；失败时均按单轨语义挂起（paused），**绝不**
  改走其它引擎。

⚠️ 分层约束：services 层禁 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("ai_provider.agent_engine")

#: 单轨引擎名（**唯一**）：oh-my-pi。
ENGINE_OMP = "omp"

#: 上下文压缩阈值缺省（字）：超过则先压缩再送入单帧。
DEFAULT_COMPACTION_THRESHOLD = 120_000
#: 指数退避基数（秒）。
DEFAULT_RETRY_BACKOFF_BASE = 1.5
#: 重试上限缺省（不含首次执行）。
DEFAULT_MAX_RETRIES = 2
#: 上下文阈值比率缺省（对齐既有 ``omp_threshold`` 口径）。
DEFAULT_RATIO = 0.7
#: 单任务超时缺省（秒）。
DEFAULT_TASK_TIMEOUT = 300

#: 环境变量名前缀（``Settings`` 字段同名大写；热更新时优先读实时环境）。
_ENV_PREFIX = "AGENT_ENGINE_"
#: 非法配置告警去重（避免每个任务刷一次 WARN）。
_WARNED: set[str] = set()


# ===================== 数据契约 =====================
@dataclass
class AgentTask:
    """一次委派给底层引擎的任务（业务层 → 执行器）。

    ``task_type`` 取 ``outline / content / audit / generic`` 等；
    ``context_chars / context_limit`` 供阈值路由使用
    （未知窗口填 0 = 不做阈值判定，绝不臆造）。
    """

    task_type: str = "generic"
    task_id: str = ""
    prompt: str = ""
    instruction: str = ""
    output_file: str = ""
    workspace: str = ""
    scheme_id: str = ""
    context_chars: int = 0
    context_limit: int = 0
    timeout: float = 0.0  # 0 = 使用配置中的默认超时
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """执行器回包（执行器 → 业务层）。

    状态口径（**单轨 · 无兜底**）：

    * ``completed`` —— omp 成功产出；
    * ``paused``    —— omp **不可用 / 执行失败**：已按配置重试，仍失败或遇致命错误
      ⇒ **任务挂起**（``paused=True``），``manual_review`` 给出人工介入片段，
      ``scene_preserved=True`` 表示**现场已保留**（工作区未清理，供人工排查/恢复）。

    ⚠️ **不再有 ``degraded``**：双轨与旧引擎回退已按用户裁决彻底移除。
    """

    ok: bool = False
    task_id: str = ""
    task_type: str = "generic"
    engine: str = ENGINE_OMP
    requested_engine: str = ENGINE_OMP
    status: str = "paused"      # completed / paused
    payload: str = ""
    paused: bool = False
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    reason: str = ""
    elapsed_ms: int = 0
    manual_review: str = ""     # 人工介入片段（挂起时给出）
    scene_preserved: bool = False  # 现场（工作区）是否已保留
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为纯 dict（审计日志 / 诊断端点可直接使用）。"""
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "engine": self.engine,
            "requested_engine": self.requested_engine,
            "status": self.status,
            "paused": self.paused,
            "payload": self.payload,
            "attempts": self.attempts,
            "errors": list(self.errors),
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "manual_review": self.manual_review,
            "scene_preserved": self.scene_preserved,
            "meta": dict(self.meta),
        }


@runtime_checkable
class IAgentExecutor(Protocol):
    """底层引擎执行器统一接口（异步，与既有 omp 编排层同事件循环）。

    契约要点：

    * **不向业务层抛异常** —— 失败收敛为 ``AgentResult(ok=False, paused=True)``；
    * 失败必须**保留现场**（不清理工作区），并给出 ``manual_review`` 片段。
    """

    name: str

    async def execute(self, task: AgentTask) -> AgentResult:
        """执行任务；失败返回 ``AgentResult(ok=False, paused=True)``，不向业务层抛异常。"""
        ...


# ===================== 异常分类 =====================
class AgentEngineError(RuntimeError):
    """执行器内部错误基类。"""

    kind = "unknown"
    retryable = False


class AgentUnavailableError(AgentEngineError):
    """omp 未安装 / 模型未配置（不可通过重试恢复）⇒ 立即挂起。"""

    kind = "unavailable"
    retryable = False


class AgentAuthError(AgentEngineError):
    """Provider 授权凭据缺失或不可读（不可通过重试恢复）⇒ 立即挂起。"""

    kind = "auth"
    retryable = False


class AgentTimeoutError(AgentEngineError):
    """执行超时（可重试）。"""

    kind = "timeout"
    retryable = True


class AgentExecutionError(AgentEngineError):
    """进程崩溃 / 空产物 / 协议错误（可重试）。"""

    kind = "execution"
    retryable = True


# ===================== 配置加载 =====================
@dataclass(frozen=True)
class AgentEngineConfig:
    """引擎层配置快照（全部来自配置中心，禁止在代码中硬编码具体阈值）。"""

    enabled: bool = True               # 全局 Feature Flag（关闭即不提供 omp 执行器）
    omp_binary_path: str = "omp"
    context_threshold_ratio: float = DEFAULT_RATIO
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE
    compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD
    provider_auth: str = ""
    task_timeout: int = DEFAULT_TASK_TIMEOUT


def _warn_once(key: str, message: str, *args: Any) -> None:
    """同一条非法配置只告警一次，避免按任务刷日志。"""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _raw(name: str) -> Any:
    """按「实时环境变量 → settings → None」顺序读取原始配置。

    先读 ``os.environ``：部署侧改环境变量后，新任务即可读到新值（动态读取）。
    """
    env_key = _ENV_PREFIX + name.upper()
    if env_key in os.environ and str(os.environ.get(env_key) or "").strip() != "":
        return os.environ.get(env_key)
    try:
        from app.config import settings

        return getattr(settings, f"agent_engine_{name}", None)
    except Exception:  # noqa: BLE001 — 配置模块不可用时回落默认值（不阻断引擎）
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_agent_engine_config() -> AgentEngineConfig:
    """加载并归一化引擎配置（每次任务调度时调用 ⇒ 动态读取，无进程级缓存）。

    * ``enabled``：全局 Feature Flag（默认开；关闭 ⇒ 引擎层整体不可用）；
    * ``context_threshold_ratio``：必须落在 ``(0, 1]``，否则回落 0.7；
    * ``max_retries``：负数视为非法回落 2，上限 10（防误配成无限重试打穿额度）；
    * ``retry_backoff_base``：非正数回落 1.5；
    * ``compaction_threshold``：非正数回落 12 万字；
    * ``omp_binary_path``：新键为空 ⇒ 回落既有 ``omp_binary_path``（向后兼容）。
    """
    enabled_raw = _raw("enabled")
    enabled = True if enabled_raw is None else bool(enabled_raw)

    raw_ratio = _raw("context_threshold_ratio")
    ratio = _as_float(raw_ratio, DEFAULT_RATIO)
    if not (0 < ratio <= 1):
        _warn_once(
            "ratio",
            "agent_engine.context_threshold_ratio=%r 非法（需 (0,1]），已回落 %s",
            raw_ratio,
            DEFAULT_RATIO,
        )
        ratio = DEFAULT_RATIO

    raw_retries = _raw("max_retries")
    retries = _as_int(raw_retries, DEFAULT_MAX_RETRIES)
    if retries < 0:
        _warn_once(
            "retries",
            "agent_engine.max_retries=%r 非法（需 >=0），已回落 %s",
            raw_retries,
            DEFAULT_MAX_RETRIES,
        )
        retries = DEFAULT_MAX_RETRIES
    retries = min(retries, 10)

    raw_backoff = _raw("retry_backoff_base")
    backoff = _as_float(raw_backoff, DEFAULT_RETRY_BACKOFF_BASE)
    if backoff <= 0:
        _warn_once(
            "backoff",
            "agent_engine.retry_backoff_base=%r 非法（需 >0），已回落 %s",
            raw_backoff,
            DEFAULT_RETRY_BACKOFF_BASE,
        )
        backoff = DEFAULT_RETRY_BACKOFF_BASE

    raw_threshold = _raw("compaction_threshold")
    threshold = _as_int(raw_threshold, DEFAULT_COMPACTION_THRESHOLD)
    if threshold <= 0:
        _warn_once(
            "threshold",
            "agent_engine.compaction_threshold=%r 非法（需 >0），已回落 %s",
            raw_threshold,
            DEFAULT_COMPACTION_THRESHOLD,
        )
        threshold = DEFAULT_COMPACTION_THRESHOLD

    binary = str(_raw("omp_binary_path") or "").strip()
    if not binary:
        try:
            from app.config import settings

            binary = str(getattr(settings, "omp_binary_path", "") or "omp").strip() or "omp"
        except Exception:  # noqa: BLE001
            binary = "omp"

    try:
        from app.config import settings

        timeout = _as_int(getattr(settings, "omp_task_timeout", DEFAULT_TASK_TIMEOUT), DEFAULT_TASK_TIMEOUT)
    except Exception:  # noqa: BLE001
        timeout = DEFAULT_TASK_TIMEOUT

    return AgentEngineConfig(
        enabled=enabled,
        omp_binary_path=binary,
        context_threshold_ratio=ratio,
        max_retries=retries,
        retry_backoff_base=backoff,
        compaction_threshold=threshold,
        provider_auth=str(_raw("provider_auth") or "").strip(),
        task_timeout=timeout if timeout > 0 else DEFAULT_TASK_TIMEOUT,
    )
