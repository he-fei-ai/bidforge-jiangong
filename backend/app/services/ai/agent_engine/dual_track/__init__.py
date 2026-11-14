"""Agent 引擎双轨层（R240 · 路径 B 注入执行器 POC）。

**加法式演进，业务层零改动**：

* 本包是**独立新增**的双轨路由层，与既有 ``agent_engine`` 的 R239 网关
  （``omp 主路径 + local/main 降级链``）互不干扰；既有业务端点、Prompt、
  四阶段流水线全部保持现状；
* 用户裁决的架构决策在此落地：``pi`` / ``omp`` / ``hybrid`` 三态路由、
  **默认 pi**、omp 失败**自动回退 Pi**、按项目/任务 ID 稳定哈希灰度；
* 路径 B：``agent_engine.omp_executor_impl=mock`` 即可把新轨整体注入
  :class:`MockOmpExecutor`，离线跑通三条业务链路。

快速用法（业务层唯一入口）::

    from app.services.ai.agent_engine.dual_track import AgentRouter, AgentTask

    result = await AgentRouter().executeTask(AgentTask(task_type="outline", prompt=...))
    # result 恒为 AgentResult；失败已自动回退 Pi，meta.degraded 标注是否降级

⚠️ 与父包同名类的区分：本包 :class:`OmpAgentExecutor` 是**双轨适配层**
   （读取配置 + 外层硬超时 + 结果归一化），真 omp 进程逻辑仍复用
   ``agent_engine.executors.OmpAgentExecutor``；两者不重复实现。
"""

# 业务数据契约直接复用父包，避免第二份模型（业务无感知的前提）。
from app.services.ai.agent_engine.contracts import AgentResult, AgentTask

from .config import (
    ENGINE_MOCK,
    ENGINE_OMP,
    ENGINE_PI,
    IMPL_MOCK,
    IMPL_REAL,
    MODE_HYBRID,
    MODE_OMP,
    MODE_PI,
    DualTrackConfig,
    load_dual_track_config,
    make_config,
    public_config,
)
from .contracts import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    IAgentExecutor,
    failed_result,
    normalize_result,
)
from .executors import MockOmpExecutor, OmpAgentExecutor, PiAgentExecutor
from .readiness import known_env_keys, readiness, unknown_env_keys
from .router import AgentRouter, stable_bucket

__all__ = [
    "ENGINE_MOCK",
    "ENGINE_OMP",
    "ENGINE_PI",
    "IMPL_MOCK",
    "IMPL_REAL",
    "MODE_HYBRID",
    "MODE_OMP",
    "MODE_PI",
    "AgentResult",
    "AgentRouter",
    "AgentTask",
    "DualTrackConfig",
    "IAgentExecutor",
    "MockOmpExecutor",
    "OmpAgentExecutor",
    "PiAgentExecutor",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "failed_result",
    "load_dual_track_config",
    "make_config",
    "known_env_keys",
    "normalize_result",
    "public_config",
    "readiness",
    "stable_bucket",
    "unknown_env_keys",
]
