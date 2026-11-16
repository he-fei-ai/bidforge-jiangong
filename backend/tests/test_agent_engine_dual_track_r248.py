"""
Agent 引擎双轨层（R248 · 路径 B 注入执行器 POC）契约测试。

覆盖：
1. 配置动态加载与环境变量覆盖；
2. 路由器按 mode / gray 选执行器（pi / omp / hybrid 稳定哈希）；
3. MockOmpExecutor 预置响应与内置样例；
4. OmpAgentExecutor 失败重试 + 默认回退 Pi；禁用回退时抛异常；
5. 路由器端到端执行（engine_used / degraded / reason 标注）。

运行方式（在 backend/ 目录下）：
    python -m pytest tests/test_agent_engine_dual_track_r248.py -q
"""

import json
import os
import tempfile
from unittest import mock

import pytest
from app.services.ai.agent_executor import (
    AgentEngineConfig,
    AgentResult,
    AgentRouter,
    AgentTask,
    IAgentExecutor,
    MockOmpExecutor,
    OmpAgentExecutor,
    PiAgentExecutor,
    build_default_router,
)


# --------------------------------------------------------------------------- #
# 辅助伪执行器（用于 Pi / 路由测试，不触碰真 omp 子进程）                   #
# --------------------------------------------------------------------------- #
class _FakePiExecutor(IAgentExecutor):
    def __init__(self, text: str = "pi-result", fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls = 0

    async def execute_task(self, task: AgentTask) -> AgentResult:
        self.calls += 1
        if self.fail:
            return AgentResult(engine_used="pi", degraded=True, reason="pi 注入失败", text="")
        return AgentResult(engine_used="pi", degraded=False, text=self.text, metadata={"ok": True})


class _FakeOmpExecutor(IAgentExecutor):
    def __init__(self, text: str = "omp-result", fail: bool = False, times_to_fail: int = 0):
        self.text = text
        self.fail = fail
        self.times_to_fail = times_to_fail
        self.calls = 0

    async def execute_task(self, task: AgentTask) -> AgentResult:
        self.calls += 1
        if self.calls <= self.times_to_fail:
            return AgentResult(engine_used="omp", degraded=True, reason="omp 注入失败", text="")
        return AgentResult(engine_used="omp", degraded=False, text=self.text, metadata={"ok": True})


# --------------------------------------------------------------------------- #
# 1. 配置动态加载                                                             #
# --------------------------------------------------------------------------- #
def test_config_load_defaults():
    cfg = AgentEngineConfig.load()
    assert cfg.mode == "pi"
    assert cfg.enabled is True
    assert cfg.fallback_enabled is True
    assert cfg.max_retries == 2
    assert float(cfg.context_threshold_ratio) == 0.7
    assert cfg.omp_executor_impl == "real"
    assert cfg.gray_percent == 0


def test_config_load_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_ENGINE_MODE", "omp")
    monkeypatch.setenv("AGENT_ENGINE_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("AGENT_ENGINE_MAX_RETRIES", "5")
    monkeypatch.setenv("AGENT_ENGINE_GRAY_PERCENT", "30")
    monkeypatch.setenv("AGENT_ENGINE_OMP_EXECUTOR_IMPL", "mock")
    cfg = AgentEngineConfig.load()
    assert cfg.mode == "omp"
    assert cfg.fallback_enabled is False
    assert cfg.max_retries == 5
    assert cfg.gray_percent == 30
    assert cfg.omp_executor_impl == "mock"
# --------------------------------------------------------------------------- #
# 2. 路由器按 mode 选执行器                                                   #
# --------------------------------------------------------------------------- #
async def test_router_pi_mode_selects_pi():
    pi = _FakePiExecutor("pi-text")
    omp = _FakeOmpExecutor("omp-text")
    router = AgentRouter(pi_executor=pi, omp_executor=omp, config=AgentEngineConfig(mode="pi"))
    task = AgentTask(task_type="content", scheme_id="s1", prompt="hello")
    res = await router.execute(task)
    assert res.engine_used == "pi"
    assert res.degraded is False
    assert res.text == "pi-text"
    assert pi.calls == 1
    assert omp.calls == 0


async def test_router_omp_mode_selects_omp_fallback_to_pi_when_binary_missing():
    """mode=omp，但二进制不存在；fallback 启用（缺省）应回退 pi 并标注 degraded。

    契约校正（R249）：``max_retries`` 的唯一来源是 ``AgentEngineConfig``，
    执行器构造函数不接受该参数（模块架构决策第 4 条「阈值/重试均从配置读取」），
    故重试次数经 config 传入，而不是给执行器加一个会覆盖配置的旋钮。
    """
    pi = _FakePiExecutor("pi-text")
    cfg = AgentEngineConfig(
        mode="omp", omp_binary_path="/absolutely/not/exist/omp_xyz",
        fallback_enabled=True, max_retries=1,
    )
    omp = OmpAgentExecutor(config=cfg, fallback_executor=pi)
    router = AgentRouter(pi_executor=pi, omp_executor=omp, config=cfg)
    task = AgentTask(task_type="outline", scheme_id="s2", prompt="big outline")
    res = await router.execute(task)
    assert res.engine_used == "omp->pi", res
    assert res.degraded is True
    assert res.text == "pi-text"
    assert res.metadata.get("fallback") is True
    assert pi.calls == 1


async def test_router_omp_mode_no_fallback_raises():
    cfg = AgentEngineConfig(
        mode="omp", omp_binary_path="/absolutely/not/exist/omp_xyz",
        fallback_enabled=False, max_retries=1,
    )
    omp = OmpAgentExecutor(config=cfg, fallback_executor=None)
    router = AgentRouter(pi_executor=_FakePiExecutor(), omp_executor=omp, config=cfg)
    task = AgentTask(task_type="content", scheme_id="s3", prompt="x")
    with pytest.raises(RuntimeError):
        await router.execute(task)


# --------------------------------------------------------------------------- #
# 3. hybrid 灰度：稳定哈希，同一任务恒定落同一轨                             #
# --------------------------------------------------------------------------- #
async def test_hybrid_gray_zero_is_pi():
    pi = _FakePiExecutor("pi")
    omp = _FakeOmpExecutor("omp")
    router = AgentRouter(pi_executor=pi, omp_executor=omp, config=AgentEngineConfig(mode="hybrid", gray_percent=0))
    for i in range(10):
        task = AgentTask(task_type="content", scheme_id=f"s{i}", prompt="p")
        res = await router.execute(task)
        assert res.engine_used == "pi", f"gray=0 时不应走 omp: {res.engine_used}"
    assert omp.calls == 0


async def test_hybrid_gray_hundred_is_omp():
    pi = _FakePiExecutor("pi")
    omp = _FakeOmpExecutor("omp")
    router = AgentRouter(pi_executor=pi, omp_executor=omp, config=AgentEngineConfig(mode="hybrid", gray_percent=100))
    for i in range(5):
        task = AgentTask(task_type="content", scheme_id=f"s{i}", prompt="p")
        res = await router.execute(task)
        assert res.engine_used == "omp", f"gray=100 时应全量 omp: {res.engine_used}"
    assert pi.calls == 0


async def test_hybrid_gray_stable_hashing():
    """同一 (scheme_id, task_type) 必须恒定落同一轨；不同 scheme 可分散。"""
    pi = _FakePiExecutor("pi")
    omp = _FakeOmpExecutor("omp")
    router = AgentRouter(pi_executor=pi, omp_executor=omp, config=AgentEngineConfig(mode="hybrid", gray_percent=50))
    results = {}
    for i in range(40):
        task = AgentTask(task_type="content", scheme_id=f"scheme-{i % 8}", prompt="p")
        res = await router.execute(task)
        results.setdefault(f"scheme-{i % 8}", set()).add(res.engine_used)
    # 每个 scheme_id 的结果集应只包含一种引擎（稳定性）
    for sid, engines in results.items():
        assert len(engines) == 1, f"{sid} 出现不稳定路由: {engines}"

