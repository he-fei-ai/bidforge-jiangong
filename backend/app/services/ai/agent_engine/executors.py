"""Agent 引擎执行器实现（**单轨**：omp = oh-my-pi 唯一执行器）。

R237（2026-11-13 · 用户裁决「彻底废弃原有 Pi Agent，单轨全面替换为 omp」）：

* ❌ **已移除** ``PiAgentExecutor`（旧版 Pi Agent 执行器 / 平台主链路兜底）——
   双轨与旧引擎回退机制**彻底废弃**；
* ✅ :class:`OmpAgentExecutor` —— **唯一**执行器：封装 omp 进程启动、Provider 授权
  注入、**流式输出解析**（NDJSON 帧级回调）、**上下文压缩**、**指数退避重试**；
* ✅ :class:`MockOmpExecutor` —— 不启动真实 omp 的离线替身（供契约测试与全链路测试）。

⚠️ **无兜底设计**：执行器**不向业务层抛异常**；失败 ⇒ 重试 ⇒ 仍失败或遇致命错误
（授权失败 / 未安装 / 未配模型）⇒ 返回 ``AgentResult(ok=False, paused=True)``
并**保留现场**（工作区不清理）+ 给出 ``manual_review`` 人工介入片段。

⚠️ 业务层 Prompt 与四阶段流水线（plan→execute→verify→fix）**保持不变**；
   本模块只替换「底层执行器」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Callable

from app.services.ai.agent_engine.contracts import (
    AgentAuthError,
    AgentEngineConfig,
    AgentEngineError,
    AgentExecutionError,
    AgentResult,
    AgentTask,
    AgentTimeoutError,
    AgentUnavailableError,
    load_agent_engine_config,
)

logger = logging.getLogger("ai_provider.agent_engine")

#: 人工介入片段最大长度（挂起时随结果回传，供前端展示）。
_MANUAL_SNIPPET_CHARS = 500


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _classify(exc: BaseException) -> str:
    """把执行异常归为可诊断类别（不改写异常本身，仅用于结果标注）。"""
    if isinstance(exc, AgentEngineError):
        return exc.kind
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "超时" in text or "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "授权" in text or "auth" in lowered or "api key" in lowered:
        return "auth"
    if "not found" in lowered or "不存在" in text or "未安装" in text:
        return "unavailable"
    return "execution"


def _merge_system_prompt(task: AgentTask) -> str | None:
    """角色 system prompt 与自定义指令**叠加**（自定义指令不得冲掉硬约束）。"""
    role = (task.metadata or {}).get("role")
    base = str(getattr(role, "system_prompt", "") or "")
    override = str((task.metadata or {}).get("system_prompt") or "").strip()
    merged = "\n\n".join(p for p in (base, override) if p)
    return merged or None


class OmpAgentExecutor:
    """omp 单轨执行器：进程启动 + 授权注入 + 流式解析 + 上下文压缩 + 重试 / 挂起。

    ⚠️ **失败即挂起**（无兜底）：重试耗尽或遇致命错误 ⇒ ``AgentResult(paused=True)``
    且**保留现场**（工作区不删除），由人工介入恢复。
    """

    name = "omp"

    def __init__(
        self,
        *,
        config: AgentEngineConfig | None = None,
        session_factory: Callable[..., Any] | None = None,
        retry_backoff: float | None = None,
        compaction_threshold: int | None = None,
        keep_workspace_on_pause: bool = True,
    ):
        self._config_override = config
        self._session_factory = session_factory
        self._retry_backoff_override = retry_backoff
        self._compaction_override = compaction_threshold
        # 挂起时**保留现场**（人工排查 / 恢复的输入）——「保留现场」是用户明确要求。
        self._keep_workspace_on_pause = bool(keep_workspace_on_pause)

    # ---------- 配置与探测 ----------
    def _config(self) -> AgentEngineConfig:
        """每次执行时读取配置（动态读取，支持热更新环境变量）。"""
        return self._config_override or load_agent_engine_config()

    @staticmethod
    def _provider_env(path: str) -> dict[str, str]:
        """读取 Provider 授权凭据并转为待注入环境变量。

        约定：文件为 JSON 对象（环境变量名 → 值）。**任何日志都不打印值**。
        路径为空 ⇒ 不注入（沿用 omp 自身的 models.yml 授权）。
        """
        path = str(path or "").strip()
        if not path:
            return {}
        if not os.path.isfile(path):
            raise AgentAuthError(f"Provider 授权凭据不存在：{path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise AgentAuthError(f"Provider 授权凭据不可读或非合法 JSON：{exc}") from exc
        if not isinstance(data, dict) or not data:
            raise AgentAuthError("Provider 授权凭据为空或不是 JSON 对象")
        return {str(k): str(v) for k, v in data.items() if str(k).strip()}

    def _make_session(
        self,
        *,
        binary: str,
        workspace: str,
        model: str,
        tools: str,
        system_prompt: str | None,
        env: dict[str, str],
    ) -> Any:
        if self._session_factory is not None:
            return self._session_factory(
                binary=binary,
                model=model,
                workspace=workspace,
                tools=tools,
                rpc_timeout=self._timeout,
                system_prompt=system_prompt,
                env=dict(env),
            )
        from app.services.ai.omp_rpc import OmpRpcSession

        return OmpRpcSession(
            binary,
            model,
            "",
            workspace=workspace,
            tools=tools,
            rpc_timeout=self._timeout,
            system_prompt=system_prompt,
            env=dict(env),
        )

    # ---------- 上下文压缩 ----------
    def _compact(self, task: AgentTask, workspace: str, threshold: int) -> dict[str, Any]:
        """上下文超阈值 ⇒ 先压缩再送入单帧（全文写工作区）。

        返回 ``{"applied": bool, "chars": int, "digest": str}``。
        """
        text = task.prompt or task.instruction or ""
        if threshold <= 0 or len(text) <= threshold:
            return {"applied": False, "chars": len(text), "digest": ""}
        from app.services.ai.omp_workflow import _compact_text, _write_file

        compacted, applied, digest = _compact_text(text, threshold)
        if applied:
            try:
                _write_file(workspace, "CONTEXT.md", compacted)
            except Exception as exc:  # noqa: BLE001 — 压缩写盘失败不阻断（仍有摘要）
                logger.warning("agent_engine: 压缩上下文写盘失败（忽略）：%s", exc)
        return {"applied": applied, "chars": len(text), "digest": digest}

    # ---------- 单次执行 ----------
    async def _run_once(self, task: AgentTask, workspace: str) -> tuple[str, dict[str, Any]]:
        """跑一次 omp（返回产物与元信息）；失败抛 ``AgentEngineError`` 子类。"""
        from app.services.ai.omp_rpc import binary_exists
        from app.services.ai.omp_workflow import _read_output

        cfg = self._config()
        if not cfg.enabled:
            raise AgentUnavailableError(
                "omp 执行器未启用（agent_engine_enabled=False）；单轨模式下不回退其它引擎"
            )
        self._timeout = max(1, int(task.timeout or cfg.task_timeout))
        self._env = self._provider_env(cfg.provider_auth)

        if not binary_exists(cfg.omp_binary_path):
            raise AgentUnavailableError(f"omp 二进制不存在或不可执行：{cfg.omp_binary_path}")

        model = ""
        tools = ""
        try:
            from app.config import settings

            model = str(getattr(settings, "omp_task_model", "") or "").strip()
            tools = str(
                getattr(settings, "omp_task_tools", "write,read,edit,grep,glob") or ""
            ).strip()
        except Exception:  # noqa: BLE001
            model, tools = "", ""
        if not model:
            raise AgentUnavailableError("未配置 omp_task_model（omp 无法调用底层 LLM）")

        threshold = (
            self._compaction_override
            if self._compaction_override is not None
            else cfg.compaction_threshold
        )
        compaction = self._compact(task, workspace, int(threshold or 0))

        session = self._make_session(
            binary=cfg.omp_binary_path,
            workspace=workspace,
            model=model,
            tools=tools,
            system_prompt=_merge_system_prompt(task),
            env=self._env,
        )
        frames: list[dict[str, Any]] = []

        def _on_frame(frame: dict[str, Any]) -> None:
            """流式帧收集（仅统计与留存类型，不落敏感内容）。"""
            if isinstance(frame, dict):
                frames.append({"type": str(frame.get("type") or "")})

        try:
            async with asyncio.timeout(self._timeout):
                await session.start()
                text = await session.run_prompt(
                    task.prompt or task.instruction, on_frame=_on_frame
                )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise AgentTimeoutError(f"omp 执行超过 {self._timeout}s") from exc
        finally:
            await session.stop(cleanup_workspace=False)

        payload = ""
        if task.output_file:
            payload = _read_output(workspace, task.output_file)
        if not payload:
            payload = text if isinstance(text, str) else str(text or "")
        if not payload:
            raise AgentExecutionError("omp 执行完成但产物为空（既无输出文件也无助手文本）")
        return payload, {
            "frames": len(frames),
            "frame_types": sorted({f["type"] for f in frames if f.get("type")}),
            "compaction": compaction,
            "auth_injected": bool(self._env),
        }

    async def execute(self, task: AgentTask) -> AgentResult:
        """带重试的 omp 执行；失败**不抛异常**，统一收敛为 ``paused`` 结果。"""
        started = time.monotonic()
        cfg = self._config()
        errors: list[str] = []
        attempts = 0
        last_kind = "execution"
        last_message = ""
        workspace = task.workspace
        created_ws = ""
        if not workspace:
            created_ws = tempfile.mkdtemp(prefix="agent_omp_")
            workspace = created_ws

        backoff_base = (
            self._retry_backoff_override
            if self._retry_backoff_override is not None
            else cfg.retry_backoff_base
        )

        try:
            for attempt in range(cfg.max_retries + 1):
                attempts += 1
                try:
                    payload, meta = await self._run_once(task, workspace)
                    return AgentResult(
                        ok=True,
                        task_id=task.task_id,
                        task_type=task.task_type,
                        engine=self.name,
                        requested_engine=self.name,
                        status="completed",
                        payload=payload,
                        attempts=attempts,
                        errors=errors,
                        elapsed_ms=_elapsed_ms(started),
                        meta=meta,
                    )
                except AgentEngineError as exc:
                    last_kind, last_message = exc.kind, str(exc)
                    errors.append(f"[{exc.kind}] {exc}")
                    retryable = exc.retryable
                except Exception as exc:  # noqa: BLE001 — 进程崩溃 / 协议异常同样收敛
                    last_kind = _classify(exc)
                    last_message = str(exc)
                    errors.append(f"[{last_kind}] {exc}")
                    retryable = True

                if not retryable:
                    # 致命错误（授权失败 / 未安装 / 未配模型）⇒ 立即挂起，不浪费重试。
                    break
                if attempt >= cfg.max_retries:
                    break
                # 指数退避：base * 2**attempt
                await asyncio.sleep(max(0.0, float(backoff_base)) * (2**attempt))

            logger.warning(
                "agent_engine: omp 执行失败（kind=%s, attempts=%d）⇒ 任务挂起：%s",
                last_kind,
                attempts,
                last_message,
            )
            return AgentResult(
                ok=False,
                task_id=task.task_id,
                task_type=task.task_type,
                engine=self.name,
                requested_engine=self.name,
                status="paused",
                paused=True,
                attempts=attempts,
                errors=errors,
                reason=f"omp 执行失败（{last_kind}）⇒ 任务挂起，待人工介入",
                elapsed_ms=_elapsed_ms(started),
                manual_review=(last_message or "；".join(errors[-3:]))[:_MANUAL_SNIPPET_CHARS],
                scene_preserved=True,
                meta={"workspace": workspace, "kind": last_kind},
            )
        finally:
            if created_ws and not self._keep_workspace_on_pause:
                import shutil

                shutil.rmtree(created_ws, ignore_errors=True)


class MockOmpExecutor:
    """Mock omp 执行器：不拉起真实进程，按预置 JSON 响应模拟 omp 产物。

    供契约测试 / 全链路测试离线使用（不消耗真实额度、不产生子进程）。
    """

    name = "mock_omp"

    def __init__(
        self,
        *,
        response: str | None = None,
        response_file: str | None = None,
        fail_times: int = 0,
        fail_kind: str = "execution",
        frames: list[dict[str, Any]] | None = None,
    ):
        self._response = response
        self._response_file = response_file
        self._fail_times = int(fail_times or 0)
        self._fail_kind = str(fail_kind or "execution")
        self._frames = list(frames or [])
        self.calls = 0

    def _text(self) -> str:
        if self._response is not None:
            return self._response
        if self._response_file:
            try:
                with open(self._response_file, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError as exc:
                logger.warning("agent_engine: Mock 响应文件不可读（%s），按空响应处理", exc)
        return ""

    async def execute(self, task: AgentTask) -> AgentResult:
        """模拟执行：可配置「前 N 次失败」以验证重试与挂起路径。"""
        started = time.monotonic()
        self.calls += 1
        if self.calls <= self._fail_times:
            exc = (
                AgentAuthError("模拟授权失败")
                if self._fail_kind == "auth"
                else AgentExecutionError("模拟 omp 进程崩溃")
            )
            return AgentResult(
                ok=False,
                task_id=task.task_id,
                task_type=task.task_type,
                engine=self.name,
                requested_engine=self.name,
                status="paused",
                paused=True,
                attempts=self.calls,
                errors=[f"[{exc.kind}] {exc}"],
                reason=f"omp 执行失败（{exc.kind}）⇒ 任务挂起，待人工介入",
                elapsed_ms=_elapsed_ms(started),
                manual_review=str(exc)[:_MANUAL_SNIPPET_CHARS],
                scene_preserved=True,
            )
        text = self._text()
        return AgentResult(
            ok=True,
            task_id=task.task_id,
            task_type=task.task_type,
            engine=self.name,
            requested_engine=self.name,
            status="completed",
            payload=text,
            attempts=self.calls,
            elapsed_ms=_elapsed_ms(started),
            meta={"frames": len(self._frames), "mock": True},
        )
