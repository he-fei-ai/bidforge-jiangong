"""双轨 Agent 引擎 · 路由分发与默认回退（R240）。

:class:`AgentRouter` 按 ``agent_engine.mode`` 决定调用哪个执行器：

============  ==========================================================
mode          行为
============  ==========================================================
``pi``        旧轨（默认）：与改造前行为一致
``omp``       新轨：oh-my-pi；可经 ``omp_executor_impl=mock`` 注入路径 B
``hybrid``    灰度：按 ``scheme_id`` / ``task_id`` 的**稳定哈希取模**分流
============  ==========================================================

四条铁律：

* **可开关**：``enabled=False`` 强制全量走 ``pi``；
* **可回退**：omp 失败（未安装 / 未授权 / 进程崩溃 / 超时 / 异常结构）自动
  降级到 :class:`PiAgentExecutor`，并打 WARN 日志与 ``meta.degraded``；
* **可灰度**：百分比来自配置，同一项目/任务 ID 恒定落同一轨（不抖动）；
* **业务无感知**：业务层只调用 ``executeTask(task)``，收到的永远是
  归一化后的 ``AgentResult``，异常结构不进入业务数据模型。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.services.ai.agent_engine.contracts import AgentResult, AgentTask

from .config import (
    ENGINE_MOCK,
    ENGINE_OMP,
    ENGINE_PI,
    IMPL_MOCK,
    MODE_HYBRID,
    MODE_OMP,
    MODE_PI,
    DualTrackConfig,
    load_dual_track_config,
)
from .contracts import STATUS_COMPLETED, STATUS_FAILED, failed_result, normalize_result
from .executors import MockOmpExecutor, OmpAgentExecutor, PiAgentExecutor

logger = logging.getLogger("ai_provider.agent_engine.dual_track")

#: 灰度桶数量（百分比口径）；与配置中的百分比配套，不是业务阈值。
_GRAY_BUCKETS = 100


def stable_bucket(key: str, buckets: int = _GRAY_BUCKETS) -> int:
    """稳定哈希取模：跨进程 / 跨重启结果恒定。

    ⚠️ 不用内置 ``hash()`` —— Python 字符串哈希带随机盐，进程重启后同一
       项目会落到不同轨，灰度就失去意义。改用 blake2b 定长摘要。
    """
    digest = hashlib.blake2b(str(key or "").encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % max(1, int(buckets))


class AgentRouter:
    """双轨路由器（支持依赖注入，路径 B 可把 omp 轨整体替换为 Mock）。"""

    name = "router"

    def __init__(
        self,
        *,
        config: DualTrackConfig | None = None,
        pi_executor: Any | None = None,
        omp_executor: Any | None = None,
        mock_executor: Any | None = None,
    ) -> None:
        self._config_override = config
        self._pi = pi_executor or PiAgentExecutor()
        self._omp = omp_executor or OmpAgentExecutor()
        self._mock = mock_executor or MockOmpExecutor()
        #: 路由决策留痕（可观测 / 验收断言）。
        self.decisions: list[dict[str, Any]] = []

    def _config(self) -> DualTrackConfig:
        """每次任务动态读取配置（热更新点）；注入 config 仅供离线测试。"""
        return self._config_override or load_dual_track_config()

    @staticmethod
    def _gray_key(task: AgentTask, cfg: DualTrackConfig) -> str:
        """按配置选定哈希键；项目/方案 ID 优先，缺省回落任务 ID。"""
        key = str(getattr(task, cfg.gray_key, "") or "")
        return key or str(task.task_id or "")

    def _new_track(self, cfg: DualTrackConfig) -> str:
        """新轨引擎名：路径 B 注入开关打开时返回 Mock。"""
        return ENGINE_MOCK if cfg.omp_executor_impl == IMPL_MOCK else ENGINE_OMP

    def select(self, task: AgentTask, cfg: DualTrackConfig | None = None) -> str:
        """纯函数式选轨（不产生副作用，便于单元测试与灰度预演）。"""
        conf = cfg or self._config()
        if not conf.enabled:
            return ENGINE_PI
        new_track = self._new_track(conf)
        if conf.mode == MODE_PI:
            return ENGINE_PI
        if conf.mode == MODE_OMP:
            return new_track
        if conf.mode == MODE_HYBRID:
            bucket = stable_bucket(self._gray_key(task, conf), _GRAY_BUCKETS)
            return new_track if bucket < conf.gray_percent else ENGINE_PI
        # load_dual_track_config 已把非法 mode 归一为 pi，此处为防御性兜底。
        return ENGINE_PI

    def route_plan(self, task: AgentTask, cfg: DualTrackConfig | None = None) -> dict[str, Any]:
        """诊断快照：本次任务会走哪一轨、灰度桶值是多少（不执行任务）。"""
        conf = cfg or self._config()
        key = self._gray_key(task, conf)
        return {
            "mode": conf.mode,
            "enabled": conf.enabled,
            "requested_engine": self.select(task, conf),
            "fallback_enabled": conf.fallback_enabled,
            "gray_key": conf.gray_key,
            "gray_percent": conf.gray_percent,
            "hash_key": key,
            "bucket": stable_bucket(key),
            "omp_executor_impl": conf.omp_executor_impl,
        }

    def _executor_for(self, engine: str) -> Any:
        if engine == ENGINE_OMP:
            return self._omp
        if engine == ENGINE_MOCK:
            return self._mock
        return self._pi

    async def executeTask(self, task: AgentTask) -> AgentResult:
        """路由执行 + 默认回退（业务层唯一入口，永不抛异常）。"""
        cfg = self._config()
        requested = self.select(task, cfg)
        plan = self.route_plan(task, cfg)
        self.decisions.append({"task_id": task.task_id, "task_type": task.task_type, **plan})

        executor = self._executor_for(requested)
        try:
            raw = await executor.executeTask(task)
        except Exception as exc:  # noqa: BLE001 — 执行器抛异常也纳入统一回退
            logger.warning(
                "agent_engine.dual_track: 执行器 %s 抛出未收敛异常（task_id=%s）：%s",
                requested,
                task.task_id,
                exc,
            )
            raw = failed_result(
                task,
                requested,
                errors=[f"[{requested}] {type(exc).__name__}: {exc}"],
                reason="执行器异常",
            )

        # 归一化：omp 畸形结构在此被隔离，绝不污染业务数据模型。
        result = normalize_result(raw, task=task, engine=requested)
        result.requested_engine = requested
        result.meta.setdefault("mode", cfg.mode)
        result.meta.setdefault("route", requested)
        if result.ok:
            return result

        # ===== 默认回退机制 =====
        if requested == ENGINE_PI or not cfg.fallback_enabled:
            logger.warning(
                "agent_engine.dual_track: 任务失败且无可回退路径（mode=%s, engine=%s, task_id=%s）",
                cfg.mode,
                requested,
                task.task_id,
            )
            return result

        logger.warning(
            "agent_engine.dual_track: %s 执行失败（task_id=%s, task_type=%s, errors=%s）"
            " ⇒ 自动回退 Pi 轨（默认回退机制）",
            requested,
            task.task_id,
            task.task_type,
            "；".join(result.errors[:3]),
        )
        try:
            pi_raw = await self._pi.executeTask(task)
        except Exception as exc:  # noqa: BLE001
            pi_raw = failed_result(
                task,
                ENGINE_PI,
                errors=[f"[pi] {type(exc).__name__}: {exc}"],
                reason="Pi 轨回退执行异常",
            )
        fallback = normalize_result(pi_raw, task=task, engine=ENGINE_PI)
        fallback.requested_engine = requested
        fallback.meta.update(
            {
                "degraded": True,
                "engine_used": ENGINE_PI,
                "requested_engine": requested,
                "fallback_reason": f"{requested} 执行失败，自动回退 Pi 轨",
                "primary_errors": list(result.errors),
                "mode": cfg.mode,
                "route": requested,
            }
        )
        if fallback.ok and fallback.status != STATUS_COMPLETED:
            fallback.status = STATUS_COMPLETED
        if not fallback.ok and fallback.status == STATUS_COMPLETED:
            fallback.status = STATUS_FAILED
        return fallback

    async def execute(self, task: AgentTask) -> AgentResult:
        """R237/R239 兼容入口，语义与 ``executeTask`` 完全一致。"""
        return await self.executeTask(task)
