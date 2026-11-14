"""Agent 引擎网关（R239）：**omp 主路径 + 降级路径 + 可双轨**。

用户裁决（2026-11-13）：「以 can1357/oh-my-pi (omp) 作为底层 Agent 引擎，
为主路径，其它为降级路径，可双轨」。本模块据此把 R237 的执行器接成一条
**可达**的主路径 + 降级链：

* 主路径恒为 omp（``OmpAgentExecutor``：进程启动 / 授权注入 / NDJSON 流式解析 /
  上下文压缩 / 指数退避重试）；
* 降级路径复用 **R236 判据单一事实源** ``omp_engine_router.ENGINE_ORDER``
  = ``("omp", "local", "main")``，**不另造第三套档位词汇**：
  - ``local`` → ``omp_workflow.platform_executor``（平台 AI 链跑同一套编排，
    不依赖 omp 进程，CONTEXT/PLAN 内联；已登记 ``scene="omp_local_executor"``）；
  - ``main``  → ``provider_factory.chat_with_fallback``（平台主链直出，
    业务层 Prompt 与流程不变）；
* 「可双轨」：请求可显式 ``prefer=omp|local|main|auto`` 直接选轨；
  ``agent_engine_fallback_enabled=False`` 则收回到「主路径失败即挂起」。

⚠️ 降级**必须显式标注**：``meta.degraded`` / ``meta.engine_used`` /
  ``meta.engine_reason`` / ``meta.primary_errors``，绝不静默改走别的引擎；
  **全链失败**才挂起并保留现场（工作区 + 挂起记录）。
⚠️ 业务层 Prompt 与四阶段流水线保持不变；本层只决定「由谁执行」。
⚠️ 分层约束：services 层零业务库、零网络、零 routers 反向依赖。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from .contracts import AgentResult, AgentTask, IAgentExecutor, load_agent_engine_config
from .executors import OmpAgentExecutor, _merge_system_prompt

logger = logging.getLogger("ai_provider.agent_engine")

#: 挂起记录根目录（重启后仍可恢复；不含密钥，凭据只在 omp 子进程 env 中）。
_PAUSED_DIRNAME = "agent_omp_paused"
#: 恢复端点路径模板（前端据此渲染「人工介入恢复」按钮）。
_RESUME_PATH = "/api/v1/omp/agent/paused/{task_id}/resume"

FallbackFn = Callable[[AgentTask, str], Awaitable[str]]


def _safe_task_id(task_id: str) -> str:
    """复用 ``omp_workflow.safe_task_id`` 单一事实源（防路径穿越）。"""
    from app.services.ai.omp_workflow import safe_task_id

    return safe_task_id(task_id)


def _json_default(value: Any) -> str:
    """metadata 里可能有角色对象等不可序列化值，落盘时降级为类型标记。"""
    return f"<{type(value).__name__}>"


def _setting(name: str, default: Any) -> Any:
    """按「实时环境变量 → settings → default」读引擎配置（与 R237 同款热更新口径）。"""
    raw = os.environ.get("AGENT_ENGINE_" + name.upper())
    if raw is not None and str(raw).strip():
        return raw
    try:
        from app.config import settings

        return getattr(settings, "agent_engine_" + name, default)
    except Exception:  # noqa: BLE001 — 配置模块不可用时不阻断引擎
        return default


def engine_policy() -> dict[str, Any]:
    """主路径 / 降级链 / 全局开关（**全部来自配置文件**）。

    ⚠️ **顺序以 ``ENGINE_ORDER`` 单一事实源为准**，配置只裁剪「启用哪些降级档」，
       避免第二份顺序判据（本仓反复复发的「判据两份副本」反模式）。
    """
    from app.services.ai.omp_engine_router import ENGINE_ORDER

    primary = str(_setting("primary", "omp") or "omp").strip().lower()
    if primary not in ENGINE_ORDER:
        primary = "omp"
    enabled = str(_setting("fallback_enabled", True)).strip().lower() not in (
        "false", "0", "no", "off", "",
    )
    raw_order = str(_setting("fallback_order", "") or "").strip()
    if not enabled:
        allowed: list[str] = []
    elif raw_order:
        allowed = [x.strip().lower() for x in raw_order.split(",") if x.strip()]
        allowed = [x for x in allowed if x in ENGINE_ORDER and x != primary]
    else:
        allowed = [e for e in ENGINE_ORDER if e != primary]
    return {
        "primary": primary,
        "fallback_enabled": enabled,
        "fallback": allowed,
        "engine_order": list(ENGINE_ORDER),
    }


# ---------- 降级执行体（复用既有实现，不新造 Prompt / 编排） ----------
async def _fallback_local(task: AgentTask, workspace: str) -> str:
    """路径 B：平台 AI 链跑同一套编排（不依赖 omp 进程）。"""
    from app.services.ai.omp_workflow import OmpTaskSpec, _read_output, platform_executor

    spec = OmpTaskSpec(
        task_id=task.task_id,
        instruction=task.instruction or task.prompt,
        output_file=task.output_file,
        metadata=dict(task.metadata or {}),
    )
    text = await platform_executor(spec, task.prompt or task.instruction, workspace)
    if task.output_file:
        try:
            text = _read_output(workspace, task.output_file) or text
        except (OSError, ValueError):
            pass
    return text or ""


async def _fallback_main(task: AgentTask, workspace: str) -> str:
    """平台主链直出：仅换底层执行器，业务层 Prompt 与流程不变。

    ``scene`` 复用已登记的 ``omp_local_executor``（平台 AI 链替代 omp 的统一场景），
    不新增场景登记 ⇒ 不触发 KNOWN_SCENES 双向漂移护栏。
    """
    from app.services.ai.provider_factory import chat_with_fallback

    messages: list[dict[str, str]] = []
    system_prompt = _merge_system_prompt(task)
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": task.prompt or task.instruction})
    text = await chat_with_fallback(messages, scene="omp_local_executor")
    return text if isinstance(text, str) else str(text or "")


DEFAULT_FALLBACKS: dict[str, FallbackFn] = {
    "local": _fallback_local,
    "main": _fallback_main,
}


class OmpAgentGateway:
    """omp 主路径网关；降级链与双轨偏好均可配置、可注入。"""

    name = "omp"

    def __init__(
        self,
        executor: IAgentExecutor | None = None,
        *,
        state_dir: str = "",
        fallback_executors: dict[str, FallbackFn] | None = None,
    ) -> None:
        # 主路径执行器；注入仅用于离线测试（MockOmpExecutor）。
        self._executor: IAgentExecutor = executor or OmpAgentExecutor()
        self._fallback_executors: dict[str, FallbackFn] = {
            **DEFAULT_FALLBACKS,
            **(fallback_executors or {}),
        }
        self._state_dir = Path(
            state_dir or os.path.join(tempfile.gettempdir(), _PAUSED_DIRNAME)
        )
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def executor(self) -> IAgentExecutor:
        return self._executor

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def _record_path(self, task_id: str) -> Path:
        return self._state_dir / f"{_safe_task_id(task_id)}.json"

    @staticmethod
    def _resume_url(task_id: str) -> str:
        return _RESUME_PATH.format(task_id=_safe_task_id(task_id))

    def _persist(self, task: AgentTask, result: AgentResult) -> dict[str, Any]:
        """挂起记录落盘 = 现场索引；工作区本体由执行器保留，绝不删除。"""
        record: dict[str, Any] = {
            "task_id": task.task_id,
            "paused_at": time.time(),
            "engine": self.name,
            "task": asdict(task),
            "result": result.to_dict(),
            "resume_url": self._resume_url(task.task_id),
        }
        try:
            self._record_path(task.task_id).write_text(
                json.dumps(record, ensure_ascii=False, default=_json_default),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("agent_engine: 挂起记录落盘失败（工作区现场仍在）：%s", exc)
        return record

    def _clear(self, task_id: str) -> None:
        try:
            self._record_path(task_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("agent_engine: 清理挂起记录失败：%s", exc)

    @staticmethod
    def _resolve_chain(task: AgentTask, prefer: str, allow_fallback: bool | None) -> list[str]:
        """选轨 + 降级链（判据单一事实源 ``omp_engine_router``）。"""
        from app.services.ai.omp_engine_router import ENGINE_ORDER, select_engine

        policy = engine_policy()
        pref = str(prefer or "").strip().lower()
        if pref in ENGINE_ORDER:
            first = pref                       # 可双轨：显式指定任意轨
        elif pref == "auto":
            first = select_engine(task_type=task.task_type, prefer="auto").engine
        else:
            first = policy["primary"]          # 缺省：omp 主路径
        fallback = list(policy["fallback"])
        if allow_fallback is False or not policy["fallback_enabled"]:
            fallback = []
        idx = ENGINE_ORDER.index(first) if first in ENGINE_ORDER else 0
        # 只降级到 ENGINE_ORDER 中该轨之后的档；相对顺序由单一事实源决定。
        return [first] + [e for e in ENGINE_ORDER[idx + 1:] if e in fallback]

    async def _run_fallback(self, engine: str, task: AgentTask, workspace: str) -> AgentResult:
        fn = self._fallback_executors.get(engine)
        if fn is None:
            return AgentResult(
                ok=False, task_id=task.task_id, task_type=task.task_type,
                engine=engine, status="paused", paused=True, attempts=1,
                errors=[f"[{engine}] 未配置降级执行体"],
            )
        try:
            text = await fn(task, workspace)
        except Exception as exc:  # noqa: BLE001 — 降级档异常同样收敛，不向业务层抛
            return AgentResult(
                ok=False, task_id=task.task_id, task_type=task.task_type,
                engine=engine, status="paused", paused=True, attempts=1,
                errors=[f"[{engine}] {type(exc).__name__}: {exc}"],
            )
        if not text:
            return AgentResult(
                ok=False, task_id=task.task_id, task_type=task.task_type,
                engine=engine, status="paused", paused=True, attempts=1,
                errors=[f"[{engine}] 降级执行返回空产物"],
            )
        return AgentResult(
            ok=True, task_id=task.task_id, task_type=task.task_type,
            engine=engine, status="completed", payload=text, attempts=1,
        )

    async def execute(
        self,
        task: AgentTask,
        *,
        prefer: str = "",
        allow_fallback: bool | None = None,
    ) -> AgentResult:
        """omp 主路径 → 降级链 → 全链失败才挂起（不向业务层抛异常）。"""
        if not task.task_id:
            task.task_id = "t_" + uuid.uuid4().hex[:16]
        cfg = load_agent_engine_config()
        # 全局 Feature Flag 关闭 ⇒ 整层不可用：不进降级链，直接挂起。
        if not cfg.enabled:
            return self._paused_result(
                task, chain=[], errors=["[unavailable] omp 引擎层未启用（agent_engine_enabled=False）"],
                attempts=0, workspace="", policy=engine_policy(), last_kind="unavailable",
            )

        chain = self._resolve_chain(task, prefer, allow_fallback)
        primary = chain[0]
        workspace = str(task.workspace or "")
        errors: list[str] = []
        attempts = 0
        last_kind = "unknown"
        for idx, engine in enumerate(chain):
            if engine == "omp":
                res = await self._executor.execute(task)
                workspace = workspace or str((res.meta or {}).get("workspace") or "")
                last_kind = str((res.meta or {}).get("kind") or "unknown")
                attempts += res.attempts
            else:
                res = await self._run_fallback(engine, task, workspace)
                attempts += res.attempts
            errors.extend(res.errors)
            if not res.ok:
                continue
            if idx > 0:
                # ⚠️ 降级必须显式标注，绝不静默改走别的引擎。
                res.engine = engine
                res.requested_engine = primary
                res.status = "completed"
                res.meta.update({
                    "degraded": True,
                    "engine_used": engine,
                    "requested_engine": primary,
                    "engine_reason": f"omp 主路径失败，降级至 {engine}",
                    "fallback_chain": chain[1:idx + 1],
                    "primary_errors": errors[:-len(res.errors)] if res.errors else errors[:],
                })
                logger.warning(
                    "agent_engine: omp 主路径失败 ⇒ 显式降级 %s（原因：%s）",
                    engine, "；".join(errors[:3]),
                )
            self._clear(task.task_id)
            return res

        return self._paused_result(
            task, chain=chain, errors=errors, attempts=attempts,
            workspace=workspace, policy=engine_policy(), last_kind=last_kind,
        )

    def _paused_result(
        self,
        task: AgentTask,
        *,
        chain: list[str],
        errors: list[str],
        attempts: int,
        workspace: str,
        policy: dict[str, Any],
        last_kind: str,
    ) -> AgentResult:
        """全链失败：挂起 + 保留现场 + 人工介入片段。"""
        result = AgentResult(
            ok=False,
            task_id=task.task_id,
            task_type=task.task_type,
            engine=policy["primary"],
            requested_engine=policy["primary"],
            status="paused",
            paused=True,
            attempts=attempts,
            errors=errors,
            reason="omp 主路径与降级链均失败 ⇒ 任务挂起，待人工介入",
            manual_review=(errors[-1] if errors else "omp 执行失败")[:500],
            scene_preserved=True,
            meta={
                "workspace": workspace,
                "kind": last_kind,
                "engine_used": policy["primary"],
                "fallback_chain": chain[1:],
                "fallback_exhausted": bool(chain[1:]),
                "resume_url": self._resume_url(task.task_id),
            },
        )
        self._persist(task, result)
        return result

    def get_paused(self, task_id: str) -> dict[str, Any] | None:
        path = self._record_path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list_paused(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self._state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict) and result.get("paused"):
                items.append(data)
        return items

    async def resume(self, task_id: str, *, instruction: str = "") -> AgentResult:
        """人工介入恢复：重建任务后**重跑整条主路径 + 降级链**。"""
        record = self.get_paused(task_id)
        if record is None:
            return AgentResult(
                ok=False, task_id=task_id, status="paused", paused=True,
                reason=f"挂起记录不存在或不可读：{task_id}",
                manual_review="请检查任务 ID，或在保留的 omp 工作区中人工恢复现场",
                scene_preserved=True,
            )
        raw = record.get("task") or {}
        fields = AgentTask.__dataclass_fields__
        task = AgentTask(**{k: v for k, v in raw.items() if k in fields})
        if instruction:
            task.instruction = "\n\n".join(
                p for p in (task.instruction, f"[人工介入补充指令] {instruction}") if p
            )
        task.metadata = dict(task.metadata or {})
        task.metadata["resumed_from"] = task_id
        return await self.execute(task)

    @staticmethod
    def sse_event(task: AgentTask, result: AgentResult) -> dict[str, Any]:
        """SSE 终态事件：降级显式可见；挂起时抛明确异常 + 人工介入恢复入口。"""
        meta = dict(result.meta or {})
        event: dict[str, Any] = {
            "event": "agent_paused" if result.paused else "agent_completed",
            "task_id": result.task_id or task.task_id,
            "task_type": result.task_type or task.task_type,
            "engine": result.engine,
            "engine_used": meta.get("engine_used") or result.engine,
            "requested_engine": result.requested_engine,
            "degraded": bool(meta.get("degraded")),
            "status": result.status,
            "attempts": result.attempts,
            "elapsed_ms": result.elapsed_ms,
            "fallback_chain": list(meta.get("fallback_chain") or []),
        }
        if result.paused:
            event["error"] = {
                "kind": str(meta.get("kind") or "unknown"),
                "message": result.reason or "omp 主路径与降级链均失败 ⇒ 任务已挂起",
                "details": list(result.errors),
            }
            event["manual_review"] = result.manual_review
            event["scene_preserved"] = result.scene_preserved
            event["workspace"] = str(meta.get("workspace") or "")
            event["resume_url"] = str(meta.get("resume_url") or _RESUME_PATH.format(
                task_id=_safe_task_id(result.task_id or task.task_id)
            ))
            event["fallback_exhausted"] = bool(meta.get("fallback_exhausted"))
        else:
            event["payload"] = result.payload
            event["engine_reason"] = str(meta.get("engine_reason") or "")
            event["meta"] = meta
        return event


_gateway: OmpAgentGateway | None = None


def get_gateway() -> OmpAgentGateway:
    """进程级网关（懒加载；配置按任务动态读取，不在网关内缓存）。"""
    global _gateway
    if _gateway is None:
        _gateway = OmpAgentGateway()
    return _gateway


def public_config() -> dict[str, Any]:
    """只读、脱敏的引擎配置快照（供诊断端点与前端 Feature Flag 消费）。"""
    cfg = load_agent_engine_config()
    policy = engine_policy()
    return {
        "engine": "omp",
        "enabled": cfg.enabled,
        "primary": policy["primary"],
        "fallback": list(policy["fallback"]),
        "fallback_enabled": policy["fallback_enabled"],
        "engine_order": list(policy["engine_order"]),
        "omp_binary_path": cfg.omp_binary_path,
        "context_threshold_ratio": cfg.context_threshold_ratio,
        "max_retries": cfg.max_retries,
        "retry_backoff_base": cfg.retry_backoff_base,
        "compaction_threshold": cfg.compaction_threshold,
        "task_timeout": cfg.task_timeout,
        # 只回报「是否配置」，绝不回传凭据路径 / 内容。
        "provider_auth_configured": bool(cfg.provider_auth),
    }
