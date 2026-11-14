"""单轨 Agent 引擎层（唯一执行器：oh-my-pi / omp）。

R237 落盘契约与执行器，R238 补「可达性」：此前 ``agent_engine`` 目录只有
``contracts.py`` / ``executors.py`` 两个模块且 ``backend/app`` 内零生产引用，
属于本仓反复出现的「服务层就绪 != 功能可达」。本包把它们显式组装为一条
单轨链路：业务层 -> :class:`OmpAgentGateway` -> :class:`OmpAgentExecutor`
-> omp 子进程，失败即挂起并保留现场，**无任何旧引擎回退分支**。
"""

from .contracts import (
    ENGINE_OMP,
    AgentAuthError,
    AgentEngineConfig,
    AgentEngineError,
    AgentExecutionError,
    AgentResult,
    AgentTask,
    AgentTimeoutError,
    AgentUnavailableError,
    IAgentExecutor,
    load_agent_engine_config,
)
from .executors import MockOmpExecutor, OmpAgentExecutor
from .gateway import OmpAgentGateway, get_gateway, public_config

__all__ = [
    "ENGINE_OMP",
    "AgentAuthError",
    "AgentEngineConfig",
    "AgentEngineError",
    "AgentExecutionError",
    "AgentResult",
    "AgentTask",
    "AgentTimeoutError",
    "AgentUnavailableError",
    "IAgentExecutor",
    "MockOmpExecutor",
    "OmpAgentExecutor",
    "OmpAgentGateway",
    "get_gateway",
    "load_agent_engine_config",
    "public_config",
]
