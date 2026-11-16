"""
Agent 引擎双轨层（R248 · 路径 B 注入执行器 POC）

架构决策（不可违背）：
1. 现有业务层 Prompt 与流程逻辑保持现状，绝对不迁移/重写；
2. 底层引擎以 oh-my-pi (omp) 为准，采用「双轨落地、引擎路由、默认回退、灰度上线」；
3. 所有改造满足「可开关、可回退、可灰度」；
4. 阈值/二进制路径/重试/授权均从配置读取，严禁在业务代码中硬编码；
5. 当前阶段通过「路径 B：注入执行器」进行 POC，实现契约定义与 Mock 执行器。

分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.config import settings
from app.services.ai.omp_engine_router import EngineChoice
from app.services.ai.omp_rpc import OmpRpcSession, binary_exists
from app.services.ai.omp_safety import assert_tool_allowlist

logger = logging.getLogger("ai_provider.agent_executor")
# ============================ 配置（动态读取，禁止硬编码） ============================

@dataclass(frozen=True)
class AgentEngineConfig:
    """双轨层唯一配置出口。

    每次任务均通过 :meth:`load` 动态读取：环境变量优先于 pydantic Settings，
    故支持热更新与运行时动态切换，无需重启后端。
    """

    mode: str = "pi"                      # pi | omp | hybrid
    enabled: bool = True
    primary: str = "omp"
    fallback_enabled: bool = True
    fallback_order: str = ""
    omp_binary_path: str = ""
    compaction_threshold: int = 120000
    context_threshold_ratio: float = 0.7
    retry_backoff_base: float = 1.5
    max_retries: int = 2
    provider_auth: str = ""
    gray_percent: int = 0                 # [0,100]，hybrid 灰度放量
    gray_key: str = "scheme_id"           # scheme_id | task_id
    omp_executor_impl: str = "real"       # real | mock（路径 B POC）
    mock_response_path: str = ""          # Mock 预置响应 JSON 路径

    @classmethod
    def load(cls) -> "AgentEngineConfig":
        """从 settings + 环境变量动态读取（环境变量优先，实时生效）。"""

        def env(name: str, default: Any) -> Any:
            v = os.environ.get(name)
            return default if (v is None or v == "") else v

        def env_bool(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            return str(v).strip().lower() in ("1", "true", "yes", "on")

        def env_int(name: str, default: int) -> int:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            try:
                return int(v)
            except ValueError:
                return default

        def env_float(name: str, default: float) -> float:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            try:
                return float(v)
            except ValueError:
                return default

        s = settings
        return cls(
            mode=str(env("AGENT_ENGINE_MODE", getattr(s, "agent_engine_mode", "pi") or "pi")).strip().lower(),
            enabled=env_bool("AGENT_ENGINE_ENABLED", bool(getattr(s, "agent_engine_enabled", True))),
            primary=str(env("AGENT_ENGINE_PRIMARY", getattr(s, "agent_engine_primary", "omp") or "omp")).strip().lower(),
            fallback_enabled=env_bool("AGENT_ENGINE_FALLBACK_ENABLED", bool(getattr(s, "agent_engine_fallback_enabled", True))),
            fallback_order=str(env("AGENT_ENGINE_FALLBACK_ORDER", getattr(s, "agent_engine_fallback_order", "") or "")).strip(),
            omp_binary_path=str(env("AGENT_ENGINE_OMP_BINARY_PATH", getattr(s, "agent_engine_omp_binary_path", "") or "")).strip(),
            compaction_threshold=env_int("AGENT_ENGINE_COMPACTION_THRESHOLD", int(getattr(s, "agent_engine_compaction_threshold", 120000))),
            context_threshold_ratio=env_float("AGENT_ENGINE_CONTEXT_THRESHOLD_RATIO", float(getattr(s, "agent_engine_context_threshold_ratio", 0.7))),
            retry_backoff_base=env_float("AGENT_ENGINE_RETRY_BACKOFF_BASE", float(getattr(s, "agent_engine_retry_backoff_base", 1.5))),
            max_retries=env_int("AGENT_ENGINE_MAX_RETRIES", int(getattr(s, "agent_engine_max_retries", 2))),
            provider_auth=str(env("AGENT_ENGINE_PROVIDER_AUTH", getattr(s, "agent_engine_provider_auth", "") or "")).strip(),
            gray_percent=env_int("AGENT_ENGINE_GRAY_PERCENT", int(getattr(s, "agent_engine_gray_percent", 0))),
            gray_key=str(env("AGENT_ENGINE_GRAY_KEY", getattr(s, "agent_engine_gray_key", "scheme_id") or "scheme_id")).strip().lower(),
            omp_executor_impl=str(env("AGENT_ENGINE_OMP_EXECUTOR_IMPL", getattr(s, "agent_engine_omp_executor_impl", "real") or "real")).strip().lower(),
            mock_response_path=str(env("AGENT_ENGINE_MOCK_RESPONSE_PATH", getattr(s, "agent_engine_mock_response_path", "") or "")).strip(),
        )

# ============================ 任务与结果契约 ============================

@dataclass
class AgentTask:
    """下发给引擎执行器的统一任务。

    不携带 FastAPI Request / Depends，由调用方在调用前完成框架绑定；
    执行器层只关心「做什么」与「上下文多大」。
    """

    task_type: str          # outline | content | audit | facts | full
    scheme_id: str
    prompt: str
    context_chars: int = 0
    context_limit: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def gray_identity(self) -> str:
        """灰度哈希的输入：按 config.gray_key 选 scheme_id 或 task_id。"""
        if self.extra.get("task_id") and self.extra.get("gray_key_hint") == "task_id":
            return str(self.extra["task_id"])
        return self.scheme_id or ""


@dataclass
class AgentResult:
    """执行器统一返回，加法式字段，不覆盖既有业务数据模型。"""

    engine_used: str        # pi | omp | mock | omp->pi
    degraded: bool = False
    reason: str = ""
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================ 统一执行器接口 ============================

class IAgentExecutor(ABC):
    """Agent 执行器抽象基类。

    业务层只依赖此接口，不感知底层是 pi / omp 还是 mock；
    新增引擎只需实现 execute_task，无需改动路由与业务。
    """

    @abstractmethod
    async def execute_task(self, task: AgentTask) -> AgentResult:
        """执行单次 Agent 任务，返回统一结果。"""
        raise NotImplementedError


# ============================ Pi 执行器（既有主链路适配器） ============================

class PiAgentExecutor(IAgentExecutor):
    """既有主链路（pi）的统一契约包装器。

    不改动任何业务 Prompt / 流程逻辑；把调用方已有的生成函数
    注入进来，按 task_type 分发。未绑定的任务类型返回 degraded=True
    而不抛异常，保证路由层可统一处理。
    """

    def __init__(
        self,
        *,
        outline_fn: Optional[Callable[[AgentTask], Any]] = None,
        content_fn: Optional[Callable[[AgentTask], Any]] = None,
        facts_fn: Optional[Callable[[AgentTask], Any]] = None,
    ):
        self._fn_by_type = {
            "outline": outline_fn,
            "content": content_fn,
            "facts": facts_fn,
        }

    def bind(self, task_type: str, fn: Callable[[AgentTask], Any]) -> "PiAgentExecutor":
        """注册/覆盖某任务类型的生成函数（链式调用）。"""
        self._fn_by_type[str(task_type).lower()] = fn
        return self

    async def execute_task(self, task: AgentTask) -> AgentResult:
        tt = str(task.task_type or "").lower()
        fn = self._fn_by_type.get(tt)
        if fn is None:
            return AgentResult(
                engine_used="pi",
                degraded=True,
                reason=f"pi 执行器未绑定 {tt or '(空)'} 任务类型的生成函数（需由调用方通过 bind() 注入）",
                text="",
                metadata=dict(task.extra),
            )
        try:
            result = await fn(task) if asyncio.iscoroutinefunction(fn) else fn(task)
        except Exception as exc:  # noqa: BLE001 — 业务层异常不得泄露进执行器层
            logger.exception("pi 执行器任务失败: type=%s scheme=%s", tt, task.scheme_id)
            return AgentResult(
                engine_used="pi",
                degraded=True,
                reason=f"pi 执行器异常: {exc}",
                text="",
                metadata=dict(task.extra),
            )
        return AgentResult(
            engine_used="pi",
            degraded=False,
            reason="pi 主链路执行成功",
            text=str(result),
            metadata=dict(task.extra),
        )

# ============================ omp 执行器（主路径 + 重试 + 回退） ============================

class OmpAgentExecutor(IAgentExecutor):
    """omp 主路径执行器。

    负责 omp 二进制进程拉起、授权环境注入、结果解析；按
    ``max_retries`` + 指数退避重试；超限后按 ``fallback_enabled`` 决定：
    启用则无缝降级到 PiAgentExecutor（WARN 日志），禁用则向上抛异常。
    """

    _DEFAULT_RPC_TIMEOUT = 120

    def __init__(
        self,
        *,
        config: AgentEngineConfig,
        fallback_executor: Optional[IAgentExecutor] = None,
        model: str = "openai/gpt-4o",
        api_key: str = "",
        tools: str = "",
        workspace_dir: str = "",
        rpc_timeout: Optional[int] = None,
    ):
        self._config = config
        self._fallback = fallback_executor
        self._model = model
        self._api_key = api_key
        self._tools = tools
        self._workspace_dir = workspace_dir
        self._rpc_timeout = rpc_timeout or int(getattr(settings, "omp_rpc_timeout", self._DEFAULT_RPC_TIMEOUT) or self._DEFAULT_RPC_TIMEOUT)

    # ----- 公开契约 -----

    async def execute_task(self, task: AgentTask) -> AgentResult:
        if not self._config.enabled:
            return AgentResult(
                engine_used="omp",
                degraded=True,
                reason="agent_engine 已显式关闭（agent_engine_enabled=False）",
                text="",
                metadata=dict(task.extra),
            )

        last_error: Optional[Exception] = None
        delay = max(0.05, float(self._config.retry_backoff_base))

        for attempt in range(1, self._config.max_retries + 1):
            try:
                text = await self._run_omp_once(task)
                return AgentResult(
                    engine_used="omp",
                    degraded=False,
                    reason=f"omp 第 {attempt} 次尝试成功",
                    text=text,
                    metadata={"attempts": attempt, **task.extra},
                )
            except Exception as exc:  # noqa: BLE001 — 未安装/未授权/崩溃/超时均走此处
                last_error = exc
                logger.warning(
                    "omp 第 %d/%d 次尝试失败（scheme=%s type=%s）: %s",
                    attempt, self._config.max_retries, task.scheme_id, task.task_type, exc,
                )
                if attempt < self._config.max_retries:
                    await asyncio.sleep(delay)
                    delay *= 2

        # 重试超限：默认回退到 pi（不可静默）
        if self._config.fallback_enabled and self._fallback is not None:
            logger.warning(
                "omp 重试超限（%d 次），已回退 Pi 执行器（scheme=%s type=%s 原因: %s）",
                self._config.max_retries, task.scheme_id, task.task_type, last_error,
            )
            res = await self._fallback.execute_task(task)
            res.engine_used = "omp->pi"
            res.degraded = True
            res.reason = f"omp 失败后回退 pi: {last_error}"
            res.metadata = {"fallback": True, "original_error": str(last_error), **task.extra}
            return res

        raise RuntimeError(f"omp 执行失败且未启用回退（最后错误: {last_error}）") from last_error

    # ----- 内部：单次 omp 调用 -----

    async def _run_omp_once(self, task: AgentTask) -> str:
        # 授权文件校验（配置了 provider_auth 才生效，缺失/不可读 ⇒ 未授权）
        if self._config.provider_auth and not self._auth_file_readable():
            raise RuntimeError(f"provider 授权文件不可读: {self._config.provider_auth}")

        binary = self._config.omp_binary_path or "omp"
        if not binary_exists(binary):
            raise RuntimeError(f"omp 二进制不存在或不可执行: {binary}")

        # 工作区隔离：优先用配置目录，否则系统临时目录下独立子目录（与业务数据/数据库隔离）
        ws = self._workspace_dir or tempfile.mkdtemp(prefix="omp_agent_")

        # 工具白名单 fail-closed（空白名单=放开全部内置工具，须拒绝启动）
        assert_tool_allowlist(self._tools or "")

        # 若 task.extra 中携带相对工作区路径，做越界校验（不改既有业务路径语义）
        rel = str(task.extra.get("workspace_rel_path") or "").strip()
        if rel:
            resolve_workspace_path(ws, rel)  # 越界直接抛 ValueError

        env = self._build_auth_env()
        session = OmpRpcSession(
            binary,
            self._model,
            self._api_key,
            workspace=ws,
            tools=self._tools,
            rpc_timeout=self._rpc_timeout,
            env=env,
        )
        await session.start()
        try:
            text = await session.run_prompt(task.prompt)
        finally:
            await session.stop(cleanup_workspace=not bool(self._workspace_dir))

        if not text or not text.strip():
            raise RuntimeError("omp 返回空内容（未产出有效文本）")
        return text

    def _auth_file_readable(self) -> bool:
        if not self._config.provider_auth:
            return True
        try:
            with open(self._config.provider_auth, "r", encoding="utf-8") as f:
                json.load(f)  # 仅做可读性/JSON 形态校验，内容不进日志
            return True
        except Exception:
            return False

    def _build_auth_env(self) -> dict[str, str]:
        """把授权凭据仅注入 omp 子进程环境，绝不写入日志。"""
        env = dict(os.environ)
        if self._config.provider_auth:
            try:
                with open(self._config.provider_auth, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                if isinstance(creds, dict):
                    for k, v in creds.items():
                        env[str(k)] = str(v)
            except Exception as exc:  # noqa: BLE001 — 可读性已在 _auth_file_readable 确认，此处兜底
                logger.debug("构造 omp 授权环境失败: %s", exc)
        return env

# ============================ Mock 执行器（路径 B POC） ============================

class MockOmpExecutor(IAgentExecutor):
    """路径 B Mock 执行器：不启动子进程、不消耗真实额度。

    读取预置 JSON 响应（{任务类型: 响应文本}）或内置样例响应；
    支持可选的轻量流式拼接（用于契约演示），适合离线 POC 与契约测试。
    """

    def __init__(
        self,
        *,
        responses: Optional[dict[str, str]] = None,
        response_path: Optional[str] = None,
        stream_delay: float = 0.05,
    ):
        self._responses: dict[str, str] = {}
        if response_path:
            try:
                with open(response_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._responses = {str(k): str(v) for k, v in loaded.items()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mock 响应文件读取失败，改用内置样例: %s", exc)
        self._responses.update({str(k): str(v) for k, v in (responses or {}).items()})
        self._stream_delay = max(0.0, float(stream_delay))

    async def execute_task(self, task: AgentTask) -> AgentResult:
        # 命中顺序：精确 {task_type}:{scheme_id} > {task_type} > 内置样例
        key = f"{task.task_type}:{task.scheme_id}"
        text = self._responses.get(key)
        if text is None:
            text = self._responses.get(task.task_type)
        if text is None:
            text = (
                f"[mock omp] 已收到 {task.task_type} 任务（方案={task.scheme_id or '-'}），"
                f"上下文约 {task.context_chars} 字；此为路径 B 模拟输出，未启动真实 omp 进程。\n"
                f"提示摘要：{(task.prompt or '')[:120]}"
            )
        # 轻量流式拼接（Mock 层无真实增量流，仅用于验证“分片→最终文本”契约）
        if self._stream_delay > 0 and len(text) > 0:
            pieces = [text[i:i + 40] for i in range(0, len(text), 40)]
            assembled = ""
            for piece in pieces:
                assembled += piece
                await asyncio.sleep(self._stream_delay)
        else:
            assembled = text
        return AgentResult(
            engine_used="mock",
            degraded=False,
            reason="路径 B Mock 执行器（未启动子进程，不消耗额度）",
            text=assembled,
            metadata={"mocked": True, "task_type": task.task_type, **task.extra},
        )


# ============================ 路由器 ============================

class AgentRouter:
    """双轨路由器：根据 mode / gray 决定使用哪个执行器，并标注 engine_used/degraded/reason。

    不直接执行任务，仅做路由决策；真正的执行（含 omp 回退）由所选执行器完成。
    """

    def __init__(
        self,
        *,
        pi_executor: IAgentExecutor,
        omp_executor: IAgentExecutor,
        config: Optional[AgentEngineConfig] = None,
    ):
        self._pi = pi_executor
        self._omp = omp_executor
        self._config = config or AgentEngineConfig.load()

    def _gray_route(self, task: AgentTask) -> bool:
        """hybrid 模式下按稳定哈希决定是否走 omp：同一 ID 恒定落同一轨。"""
        pct = max(0, min(100, int(self._config.gray_percent)))
        if pct <= 0:
            return False
        if pct >= 100:
            return True
        key_val = task.gray_identity()
        digest = hashlib.sha256(f"{key_val}:{task.task_type}".encode("utf-8")).hexdigest()
        return (int(digest, 16) % 100) < pct

    def select(self, task: AgentTask) -> tuple[IAgentExecutor, EngineChoice]:
        """返回（执行器，选择结论）；不执行任务，零副作用。"""
        mode = str(self._config.mode or "pi").lower()
        if mode == "omp":
            choice = EngineChoice(
                engine="omp", degraded=False,
                reason="模式=omp，强制走 omp 主路径",
            )
            return self._omp, choice
        if mode == "pi":
            choice = EngineChoice(
                engine="pi", degraded=False,
                reason="模式=pi，走既有主链路（改造前行为，零影响）",
            )
            return self._pi, choice
        # hybrid：按灰度比例分流
        if self._gray_route(task):
            choice = EngineChoice(
                engine="omp", degraded=False,
                reason=f"hybrid 灰度命中（{self._config.gray_percent}%），走 omp",
            )
            return self._omp, choice
        choice = EngineChoice(
            engine="pi", degraded=False,
            reason=f"hybrid 灰度未命中（{self._config.gray_percent}%），走 pi",
        )
        return self._pi, choice

    async def execute(self, task: AgentTask) -> AgentResult:
        """路由 + 执行（Omp 执行器内部自带重试与回退）。"""
        executor, choice = self.select(task)
        res = await executor.execute_task(task)
        # 统一标注，不覆盖执行器已填的更详细字段
        if not res.engine_used:
            res.engine_used = choice.engine
        if not res.reason:
            res.reason = choice.reason
        res.metadata = {**task.extra, **res.metadata}
        return res


def build_default_router() -> AgentRouter:
    """从配置构建默认路由器（生产接入入口）。

    Pi 执行器的具体生成函数由调用方在构建后通过 ``pi.bind(...)`` 注入；
    未注册的类型走 degraded 路径并给出清晰原因，不会静默失败。
    """
    config = AgentEngineConfig.load()
    pi = PiAgentExecutor()
    omp_impl = str(config.omp_executor_impl or "real").lower()
    if omp_impl == "mock":
        omp = MockOmpExecutor(response_path=config.mock_response_path or None)
    else:
        omp = OmpAgentExecutor(
            config=config,
            fallback_executor=pi,
            model=getattr(settings, "omp_task_model", "openai/gpt-4o"),
            api_key=getattr(settings, "omp_task_api_key", ""),
            tools=getattr(settings, "omp_task_tools", ""),
            workspace_dir=getattr(settings, "omp_workspace_dir", ""),
        )
    return AgentRouter(pi_executor=pi, omp_executor=omp, config=config)


__all__ = [
    "AgentEngineConfig",
    "AgentTask",
    "AgentResult",
    "IAgentExecutor",
    "PiAgentExecutor",
    "OmpAgentExecutor",
    "MockOmpExecutor",
    "AgentRouter",
    "build_default_router",
]

