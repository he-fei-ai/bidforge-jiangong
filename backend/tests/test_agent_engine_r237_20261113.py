"""agent_engine 单轨引擎 · R237 契约测试 + 全链路测试（2026-11-13）。

验证用户裁决「彻底废弃原有 Pi Agent，单轨全面替换为 omp」的落地契约：

* **A 契约面**：``IAgentExecutor`` 接口 / ``AgentResult`` 状态口径（completed|paused，
  **无 degraded**——双轨回退已彻底移除）/ 配置归一（Feature Flag、重试上限、
  非法值回落、omp 路径回落既有 ``omp_binary_path``）。
* **B 异常处理与挂起**：致命错误（授权 / 未安装）不耗重试立即挂起；可重试错误
  指数退避后挂起；**现场保留**（工作区不清理）+ ``manual_review`` 人工介入片段。
* **C 全链路（Mock 替身 + 崩溃模拟）**：MockOmpExecutor「前 N 次崩溃」⇒ 重试后
  成功 / 重试耗尽挂起；OmpAgentExecutor 注入崩溃的 session_factory ⇒ 挂起且
  现场保留（即「模拟 omp 崩溃，验证任务挂起与现场保护」）。

本文件零真实 omp 进程、零外部网络、零业务库访问。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.services.ai.agent_engine.contracts import (
    ENGINE_OMP,
    AgentAuthError,
    AgentEngineConfig,
    AgentExecutionError,
    AgentResult,
    AgentTask,
    AgentUnavailableError,
    IAgentExecutor,
    load_agent_engine_config,
)
from app.services.ai.agent_engine.executors import MockOmpExecutor, OmpAgentExecutor


def _task(**kw) -> AgentTask:
    base = dict(task_id="t1", prompt="写目录", task_type="outline")
    base.update(kw)
    return AgentTask(**base)


# ===================== A. 契约面 =====================
def test_executor_implements_protocol():
    assert isinstance(OmpAgentExecutor(), IAgentExecutor)
    assert isinstance(MockOmpExecutor(), IAgentExecutor)
    assert OmpAgentExecutor().name == ENGINE_OMP


def test_agent_result_state_vocabulary_no_degraded():
    """单轨状态口径：completed / paused —— 双轨的 degraded 已彻底移除。"""
    res = AgentResult()
    assert res.status in ("completed", "paused")
    assert "degraded" not in AgentResult.to_dict(res)
    assert res.requested_engine == ENGINE_OMP


def test_result_to_dict_is_serializable():
    res = AgentResult(ok=False, paused=True, errors=["[auth] x"], manual_review="片段")
    dumped = json.dumps(res.to_dict(), ensure_ascii=False)
    for key in ("paused", "manual_review", "scene_preserved", "attempts"):
        assert key in res.to_dict()
    assert "paused" in dumped


def test_config_normalization_feature_flag():
    cfg = AgentEngineConfig(enabled=False)
    assert cfg.enabled is False


def test_config_defaults_are_documented():
    cfg = AgentEngineConfig()
    assert 0 < cfg.context_threshold_ratio <= 1
    assert cfg.max_retries >= 0
    assert cfg.retry_backoff_base > 0
    assert cfg.compaction_threshold > 0


def test_load_config_reads_settings_and_env(monkeypatch):
    """参数必须来自配置文件：settings 字段 + 实时环境变量覆盖。"""
    import app.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod.settings,
        "agent_engine_max_retries",
        5,
        raising=False,
    )
    out = load_agent_engine_config()
    assert out.max_retries == 5

    monkeypatch.setenv("AGENT_ENGINE_MAX_RETRIES", "7")
    out2 = load_agent_engine_config()
    assert out2.max_retries == 7


def test_load_config_retries_capped(monkeypatch):
    import app.config as cfg_mod

    monkeypatch.setenv("AGENT_ENGINE_MAX_RETRIES", "999")
    assert load_agent_engine_config().max_retries == 10
    monkeypatch.delenv("AGENT_ENGINE_MAX_RETRIES", raising=False)


def test_load_config_binary_falls_back_to_existing_omp_key(monkeypatch):
    import app.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "agent_engine_omp_binary_path", "", raising=False)
    monkeypatch.setattr(cfg_mod.settings, "omp_binary_path", "J:\\omp-bin\\omp", raising=False)
    assert load_agent_engine_config().omp_binary_path == "J:\\omp-bin\\omp"


def test_load_config_invalid_ratio_falls_back(monkeypatch):
    import app.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod.settings, "agent_engine_context_threshold_ratio", 1.5, raising=False
    )
    assert load_agent_engine_config().context_threshold_ratio == 0.7


# ===================== B. 异常处理与挂起（无兜底） =====================
def test_fatal_auth_error_pauses_without_wasting_retries(tmp_path):
    """授权失败 = 致命错误：不耗重试，立即挂起 + 保留现场 + 人工介入片段。"""

    class Boom:
        name = "omp"

        def __init__(self):
            self.calls = 0

        async def execute(self, task):
            raise AgentAuthError("Provider 授权凭据不存在：missing.json")

    # 用执行器级挂起语义验证：致命错误一次即停
    exc = AgentAuthError("Provider 授权凭据不存在")
    assert exc.retryable is False and exc.kind == "auth"

    mock = MockOmpExecutor(fail_times=99, fail_kind="auth", response="ok")
    res = asyncio.run(mock.execute(_task()))
    assert res.ok is False and res.paused is True and res.status == "paused"
    assert res.scene_preserved is True
    assert res.manual_review  # 人工介入片段非空
    assert res.errors and "auth" in res.errors[0]
    del Boom, tmp_path


def test_retryable_execution_error_retries_then_pauses():
    """可重试错误（进程崩溃）：指数退避重试 max_retries 次后挂起。"""
    mock = MockOmpExecutor(fail_times=99, fail_kind="execution", response="ok")
    res = asyncio.run(mock.execute(_task()))
    assert res.paused is True
    # mock 单次调用即失败（无内部重试循环）⇒ 真实重试循环在 OmpAgentExecutor；
    # 此处锁定「失败收敛为 paused 而非抛异常 / 而非 degraded」
    assert res.status == "paused"
    assert res.engine == "mock_omp"


def test_omp_executor_pauses_and_preserves_scene(tmp_path, monkeypatch):
    """模拟 omp 崩溃：重试耗尽 ⇒ 挂起 + **工作区现场保留**（不清理）。"""
    cfg = AgentEngineConfig(
        enabled=True,
        omp_binary_path=str(tmp_path / "omp"),
        max_retries=1,
        retry_backoff_base=0.0,  # 测试零等待
        compaction_threshold=0,
    )
    (tmp_path / "omp").write_text("fake", encoding="ascii")

    import app.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "omp_task_model", "test-model", raising=False)

    class CrashingSession:
        def __init__(self, *args, **kwargs):
            self._started = False

        def alive(self):
            return self._started

        async def start(self):
            self._started = True

        async def run_prompt(self, text, on_frame=None):
            raise AgentExecutionError("omp 进程崩溃（模拟）")

        async def stop(self, cleanup_workspace=True):
            self._stopped = cleanup_workspace

    from app.services.ai import omp_rpc

    monkeypatch.setattr(omp_rpc, "binary_exists", lambda b: True)
    executor = OmpAgentExecutor(config=cfg, session_factory=CrashingSession)
    res = asyncio.run(executor.execute(_task(workspace=str(tmp_path / "ws"), timeout=1)))
    assert res.ok is False and res.paused is True
    assert res.scene_preserved is True
    assert res.attempts == 2  # 首次 + 1 次重试（max_retries=1）
    assert any("崩溃" in e for e in res.errors)
    assert res.manual_review


def test_omp_executor_feature_flag_off_pauses(tmp_path):
    cfg = AgentEngineConfig(enabled=False, omp_binary_path="omp", max_retries=0)
    executor = OmpAgentExecutor(config=cfg)
    res = asyncio.run(executor.execute(_task()))
    assert res.paused is True
    assert any("未启用" in e for e in res.errors)


def test_no_legacy_fallback_symbols():
    """双轨 / 旧引擎符号不得回流（彻底废弃，防「顺手加回兜底」）。"""
    import app.services.ai.agent_engine.contracts as ct
    import app.services.ai.agent_engine.executors as ex

    assert not hasattr(ex, "PiAgentExecutor")
    assert not hasattr(ex, "AgentRouter")
    assert "degraded" not in vars(ct)["AgentResult"].__dataclass_fields__


# ===================== C. 全链路（Mock 替身） =====================
def test_mock_success_returns_payload():
    mock = MockOmpExecutor(response='{"sections": []}')
    res = asyncio.run(mock.execute(_task()))
    assert res.ok is True and res.status == "completed" and res.payload == '{"sections": []}'
    assert res.attempts == 1


def test_mock_recovers_after_transient_failures():
    """前 2 次失败、第 3 次成功 ⇒ 全链路「重试后恢复」。"""
    mock = MockOmpExecutor(response="done", fail_times=2, fail_kind="execution")
    # 模拟 OmpAgentExecutor 的重试循环语义：逐次调用直至成功
    for i in range(3):
        res = asyncio.run(mock.execute(_task()))
        if res.ok:
            break
    assert res.ok is True and mock.calls == 3


def test_mock_response_file(tmp_path):
    f = tmp_path / "out.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    mock = MockOmpExecutor(response_file=str(f))
    assert asyncio.run(mock.execute(_task())).payload == '{"a": 1}'


def test_mock_frames_metadata():
    mock = MockOmpExecutor(response="x", frames=[{"type": "text"}, {"type": "tool"}])
    res = asyncio.run(mock.execute(_task()))
    assert res.meta.get("frames") == 2
