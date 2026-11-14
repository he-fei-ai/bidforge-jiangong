"""双轨 Agent 引擎 · 三个执行器实现（R240 · 路径 B 注入执行器 POC）。

* :class:`PiAgentExecutor` —— 旧轨，**复用现有平台主链**，业务层 Prompt 零改动；
* :class:`OmpAgentExecutor` —— 新轨，适配/复用现有 omp 进程执行器（进程拉起、
  环境变量注入、授权校验、NDJSON 流式解析、上下文压缩、重试、超时）；
* :class:`MockOmpExecutor` —— 路径 B 注入替身，不拉起真实 omp，读取预置 JSON
  响应并模拟流式输出，供三条业务链路 POC 与故障注入验证。

⚠️ 三者均实现 :class:`IAgentExecutor.executeTask`，且提供 ``execute`` 别名以
   兼容 R237/R239 既有调用方；**任何失败都不向业务层抛异常**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from app.services.ai.agent_engine.contracts import AgentResult, AgentTask

from .config import (
    ENGINE_MOCK,
    ENGINE_OMP,
    ENGINE_PI,
    DualTrackConfig,
    load_dual_track_config,
)
from .contracts import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    failed_result,
    normalize_result,
)

logger = logging.getLogger("ai_provider.agent_engine.dual_track")

ChatFn = Callable[..., Awaitable[str]]

#: 离线 POC 内置样例产物（业务示例数据，不是配置阈值）。
#: 三条链路分别产出可解析 JSON，便于验证「上下文交接」是否无损。
_SAMPLE_RESPONSES: dict[str, str] = {
    "outline": json.dumps(
        {"chapters": [{"id": "1", "title": "工程概况"}, {"id": "2", "title": "施工部署"}], "version": "v1"},
        ensure_ascii=False,
    ),
    "content": json.dumps(
        {"sections": [{"id": "1", "text": "工程概况正文"}], "version": "v1"},
        ensure_ascii=False,
    ),
    "audit": json.dumps({"passed": True, "issues": [], "fixed": []}, ensure_ascii=False),
    "generic": json.dumps({"result": "mock 默认产物"}, ensure_ascii=False),
}

#: 模拟 omp NDJSON 流式帧（仅结构，不含真实模型内容）。
_SAMPLE_FRAMES: list[dict[str, Any]] = [
    {"type": "session_start"},
    {"type": "assistant_delta", "delta": "…"},
    {"type": "tool_call", "name": "write"},
    {"type": "tool_result", "name": "write"},
    {"type": "prompt_result"},
]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


class _ExecutorBase:
    """执行器基类：提供 ``execute`` 别名，兼容既有 ``IAgentExecutor`` 调用方。"""

    name = "base"

    async def executeTask(self, task: AgentTask) -> AgentResult:
        raise NotImplementedError

    async def execute(self, task: AgentTask) -> AgentResult:
        """R237/R239 兼容入口，语义与 ``executeTask`` 完全一致。"""
        return await self.executeTask(task)


# ===================== 旧轨：Pi =====================
class PiAgentExecutor(_ExecutorBase):
    """旧轨执行器：复用现有平台主链 ``provider_factory.chat_with_fallback``。

    ⚠️ **绝对不迁移 / 不重写业务层 Prompt**：这里仅把 ``AgentTask.prompt``
       原样组装为 messages，不增删、不改写任何提示词与流程逻辑；
       ``scene`` 复用已登记的 ``omp_local_executor``，不新增场景登记。
    """

    name = ENGINE_PI

    def __init__(
        self,
        *,
        chat_fn: ChatFn | None = None,
        scene: str = "omp_local_executor",
        system_prompt: str = "",
    ) -> None:
        # chat_fn 仅供离线测试注入，生产恒走平台主链。
        self._chat_fn = chat_fn
        self._scene = scene
        self._system_prompt = system_prompt
        #: 记录收到的任务快照，供「业务 Prompt 改造前后一致」回归断言。
        self.calls: list[AgentTask] = []

    async def _chat(self, messages: list[dict[str, str]]) -> str:
        if self._chat_fn is not None:
            text = await self._chat_fn(messages)
        else:
            from app.services.ai.provider_factory import chat_with_fallback

            text = await chat_with_fallback(messages, scene=self._scene)
        return text if isinstance(text, str) else str(text or "")

    async def executeTask(self, task: AgentTask) -> AgentResult:
        started = time.monotonic()
        self.calls.append(task)
        messages: list[dict[str, str]] = []
        system_text = self._system_prompt or str((task.metadata or {}).get("system_prompt") or "")
        if system_text:
            messages.append({"role": "system", "content": system_text})
        # 业务层 Prompt 原样透传（唯一入口，禁止在此加工）。
        messages.append({"role": "user", "content": task.prompt or task.instruction})
        try:
            payload = await self._chat(messages)
        except Exception as exc:  # noqa: BLE001 — 失败收敛，不向业务层抛
            logger.warning("agent_engine.dual_track: Pi 轨执行失败（task_id=%s）：%s", task.task_id, exc)
            return failed_result(
                task,
                self.name,
                errors=[f"[pi] {type(exc).__name__}: {exc}"],
                reason="Pi 轨执行失败",
                meta={"elapsed_ms": _elapsed_ms(started)},
            )
        return AgentResult(
            ok=bool(payload),
            task_id=task.task_id,
            task_type=task.task_type,
            engine=self.name,
            requested_engine=self.name,
            status=STATUS_COMPLETED if payload else STATUS_FAILED,
            payload=payload,
            attempts=1,
            errors=[] if payload else ["[pi] 主链返回空产物"],
            elapsed_ms=_elapsed_ms(started),
            meta={"scene": self._scene},
        )


# ===================== 新轨：omp =====================
class OmpAgentExecutor(_ExecutorBase):
    """omp 新轨执行器（适配层）。

    复用 ``agent_engine.executors.OmpAgentExecutor`` 的进程拉起、环境变量注入、
    Provider 授权校验、NDJSON 流式解析、上下文压缩、指数退避重试。本层负责：

    * 从配置读取 ``max_retries`` / ``task_timeout`` / ``context_threshold_ratio``
      / ``omp_binary_path`` / ``provider_auth``（**禁止硬编码**）并写入 meta 供观测；
    * **外层硬超时**：防止 omp 子进程僵死拖垮整条业务链路；
    * 把 omp 返回的任意结构经 :func:`normalize_result` 归一化，不污染业务模型。

    ⚠️ 重试循环只在底层执行器实现一次（判据单一事实源）；本层**不叠加第二份
       重试**，否则 ``max_retries`` 会被平方放大。
    """

    name = ENGINE_OMP

    def __init__(
        self,
        *,
        inner: Any | None = None,
        config: DualTrackConfig | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._inner = inner
        self._config_override = config
        self._session_factory = session_factory
        self.calls: list[AgentTask] = []

    def _config(self) -> DualTrackConfig:
        """每次执行读取配置（动态读取，支持热更新）。"""
        return self._config_override or load_dual_track_config()

    @staticmethod
    def _default_inner(session_factory: Callable[..., Any] | None = None) -> Any:
        from app.services.ai.agent_engine.executors import OmpAgentExecutor as _RealOmp

        return _RealOmp(session_factory=session_factory) if session_factory else _RealOmp()

    async def executeTask(self, task: AgentTask) -> AgentResult:
        started = time.monotonic()
        self.calls.append(task)
        cfg = self._config()
        inner = self._inner or self._default_inner(self._session_factory)
        timeout = max(1, int(cfg.task_timeout))
        try:
            raw = await asyncio.wait_for(inner.execute(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "agent_engine.dual_track: omp 外层硬超时 %ss（task_id=%s）", timeout, task.task_id
            )
            return failed_result(
                task,
                self.name,
                errors=[f"[timeout] omp 执行超过 {timeout}s"],
                reason="omp 执行超时",
                meta={"kind": "timeout", "max_retries": cfg.max_retries, "elapsed_ms": _elapsed_ms(started)},
            )
        except Exception as exc:  # noqa: BLE001 — 进程崩溃 / 协议异常统一收敛
            logger.warning("agent_engine.dual_track: omp 执行异常（task_id=%s）：%s", task.task_id, exc)
            return failed_result(
                task,
                self.name,
                errors=[f"[execution] {type(exc).__name__}: {exc}"],
                reason="omp 进程崩溃或协议异常",
                meta={"kind": "execution", "max_retries": cfg.max_retries, "elapsed_ms": _elapsed_ms(started)},
            )

        result = normalize_result(raw, task=task, engine=self.name)
        # 全部来自配置中心，写入 meta 便于验收时核对「阈值确实来自配置」。
        result.meta.update(
            {
                "max_retries": cfg.max_retries,
                "task_timeout": cfg.task_timeout,
                "context_threshold_ratio": cfg.context_threshold_ratio,
                "compaction_threshold": cfg.compaction_threshold,
                "retry_backoff_base": cfg.retry_backoff_base,
                "omp_binary_path": cfg.omp_binary_path,
                "provider_auth_configured": bool(cfg.provider_auth),
            }
        )
        return result


# ===================== 路径 B：Mock =====================
class MockOmpExecutor(_ExecutorBase):
    """Mock omp 执行器（路径 B：注入执行器 POC）。

    * **不启动真实 omp 进程**、不消耗模型额度、零网络；
    * 读取预置 JSON 响应（``responses`` / ``response_file`` / 配置中的
      ``mock_response_path``），按 ``task_type`` 分发三条业务链路产物；
    * 模拟 NDJSON 流式输出帧（``frames`` + ``on_frame`` 回调）；
    * 支持故障注入：``fail_times``（前 N 次失败）、``fail_kind``（未安装 /
      未授权 / 超时 / 崩溃）、``malformed_times``（返回 omp 畸形结构，
      用于验证结果归一化不污染业务数据模型）。

    ``malformed`` 会故意返回非 ``AgentResult`` 的 dict —— 这是**故障注入专用**，
    生产路径永远不会启用；由 :class:`AgentRouter` 统一归一化。
    """

    name = ENGINE_MOCK

    def __init__(
        self,
        *,
        responses: dict[str, str] | str | None = None,
        response_file: str = "",
        frames: list[dict[str, Any]] | None = None,
        fail_times: int = 0,
        fail_kind: str = "execution",
        malformed_times: int = 0,
        stream_delay: float = 0.0,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
        config: DualTrackConfig | None = None,
    ) -> None:
        self._responses = responses
        self._response_file = response_file
        self._frames = list(frames) if frames is not None else list(_SAMPLE_FRAMES)
        self._fail_times = int(fail_times or 0)
        self._fail_kind = str(fail_kind or "execution")
        self._malformed_times = int(malformed_times or 0)
        self._stream_delay = float(stream_delay or 0.0)
        self._on_frame = on_frame
        self._config_override = config
        self.calls: list[AgentTask] = []
        self.stream_frames: list[dict[str, Any]] = []
        self._seq = 0

    def _effective_response_file(self) -> str:
        if self._response_file:
            return self._response_file
        cfg = self._config_override or load_dual_track_config()
        return str(cfg.mock_response_path or "")

    def _load_responses(self) -> tuple[dict[str, str] | str, list[dict[str, Any]]]:
        """读取预置响应：优先级 构造入参 → 配置/指定 JSON 文件 → 内置样例。"""
        if self._responses is not None:
            return self._responses, self._frames
        path = self._effective_response_file()
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError) as exc:
                logger.warning("agent_engine.dual_track: Mock 响应文件不可读（%s），回落内置样例", exc)
            else:
                if isinstance(data, dict):
                    mapping = data.get("responses", data)
                    file_frames = data.get("frames")
                    if isinstance(file_frames, list):
                        self._frames = [f for f in file_frames if isinstance(f, dict)]
                    return mapping, self._frames
                return str(data), self._frames
        return dict(_SAMPLE_RESPONSES), self._frames

    @staticmethod
    def _pick(mapping: dict[str, str] | str, task_type: str) -> str:
        if isinstance(mapping, str):
            return mapping
        if task_type in mapping:
            return str(mapping[task_type])
        for fallback_key in ("generic", "default"):
            if fallback_key in mapping:
                return str(mapping[fallback_key])
        return next(iter(mapping.values()), "") if mapping else ""

    def _failure(self, task: AgentTask, kind: str) -> AgentResult:
        messages = {
            "unavailable": "[unavailable] 模拟 omp 未安装 / 二进制不存在",
            "auth": "[auth] 模拟 Provider 未授权 / 凭据不可读",
            "timeout": "[timeout] 模拟 omp 执行超时",
            "execution": "[execution] 模拟 omp 进程崩溃",
            "crash": "[execution] 模拟 omp 进程崩溃",
        }
        return failed_result(
            task,
            self.name,
            errors=[messages.get(kind, messages["execution"])],
            reason=f"模拟 omp 失败（{kind}）",
            attempts=1,
            meta={"kind": kind, "mock": True},
        )

    @staticmethod
    def _malformed(task: AgentTask) -> dict[str, Any]:
        """模拟 omp 协议漂移：字段类型错乱 + 未知嵌套结构。"""
        return {
            "ok": True,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "payload": ["畸形：payload 不该是列表"],
            "status": 777,
            "attempts": "两次",
            "weird_nested": {"secret": object(), "unexpected": [1, 2, 3]},
            "omp_internal_trace": "不应进入业务模型",
        }

    async def executeTask(self, task: AgentTask) -> AgentResult:
        started = time.monotonic()
        self.calls.append(task)
        self._seq += 1

        if self._seq <= self._fail_times:
            return self._failure(task, self._fail_kind)
        if self._seq <= self._fail_times + self._malformed_times:
            # 故意返回异常结构：由 AgentRouter.normalize_result 兜住。
            return self._malformed(task)  # type: ignore[return-value]

        mapping, frames = self._load_responses()
        payload = self._pick(mapping, task.task_type)
        frame_types: list[str] = []
        for frame in frames:
            if self._stream_delay > 0:
                await asyncio.sleep(self._stream_delay)
            self.stream_frames.append(frame)
            if callable(self._on_frame):
                self._on_frame(frame)
            frame_type = str(frame.get("type") or "")
            if frame_type:
                frame_types.append(frame_type)

        return AgentResult(
            ok=bool(payload),
            task_id=task.task_id,
            task_type=task.task_type,
            engine=self.name,
            requested_engine=self.name,
            status=STATUS_COMPLETED if payload else STATUS_FAILED,
            payload=payload,
            attempts=1,
            errors=[] if payload else ["[mock_omp] 预置响应为空"],
            elapsed_ms=_elapsed_ms(started),
            meta={
                "mock": True,
                "frames": len(frames),
                "frame_types": sorted(set(frame_types)),
                "streamed": True,
                "response_source": "file" if self._effective_response_file() else "preset",
            },
        )
