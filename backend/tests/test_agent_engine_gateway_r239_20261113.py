"""agent_engine 网关护栏（R239 · omp 主路径 + 显式降级链，2026-11-13）。

⚠️ **R240 重锚说明（意图保留、强度不减）**：本文件原按 R237「单轨无兜底」写断言
   （``fallback is None`` / 失败即 ``paused`` / SSE ``fallback=None``）。R239 用户裁决
   已撤销「单轨无回退」，改为 **omp 恒为主路径 + ENGINE_ORDER 显式降级链**，
   主路径失败时显式标注 ``degraded``，绝不静默改道。因此：

   * 「失败 ⇒ 挂起 + 落盘 + resume 恢复」意图用 ``allow_fallback=False`` **原样保留**；
   * 「失败 ⇒ 显式降级成功」作为 R239 新语义**单独补用例**（不删旧意图）；
   * 配置 / SSE 断言重锚为「降级链可见、绝不静默」，比原单值断言更强；
   * 降级档一律注入离线替身，消除原用例经 ``platform_executor`` 偶发联网的隐患。

覆盖并行会话新增的 ``gateway.py`` / ``__init__.py`` 可达性：
* ``public_config()`` 脱敏配置快照（供诊断端点 / 前端 Feature Flag 消费）；
* ``OmpAgentGateway`` 挂起记录落盘 / ``list_paused`` / ``get_paused`` / ``resume``；
* ``sse_event`` 终态事件（挂起 / 完成 / 降级，降级显式可见）。

全部离线：MockOmpExecutor 替代真 omp 进程，降级档注入离线替身，
零子进程、零网络、零业务库访问。测试文件纯 LF。
"""

from __future__ import annotations

import asyncio

import pytest
from app.services.ai.agent_engine import (
    MockOmpExecutor,
    OmpAgentGateway,
    public_config,
)
from app.services.ai.agent_engine.contracts import (
    AgentResult,
    AgentTask,
)
from app.services.ai.omp_engine_router import ENGINE_ORDER


def _gateway(tmp_path, *, executor=None, fallback_executors=None) -> OmpAgentGateway:
    """隔离状态目录的网关实例；默认注入离线降级替身，杜绝真实网络调用。"""

    async def offline_fallback(task, workspace):
        return '{"offline": "降级档替身产物"}'

    return OmpAgentGateway(
        state_dir=str(tmp_path / "agent_omp_paused"),
        executor=executor,
        fallback_executors=fallback_executors
        or {"local": offline_fallback, "main": offline_fallback},
    )


def _task(task_id: str = "t1", instruction: str = "写目录") -> AgentTask:
    return AgentTask(task_id=task_id, instruction=instruction, task_type="outline")


# ===================== public_config 脱敏快照 =====================


def test_public_config_shape_and_sanitization():
    """配置快照脱敏；降级链由 ENGINE_ORDER 单一事实源决定且**显式可见**。"""
    cfg = public_config()
    assert cfg["engine"] == "omp"
    assert isinstance(cfg["enabled"], bool)
    # R239 重锚：原断言「单轨 fallback is None」已过时。
    # 锁意图而非锁实现：主路径恒为 omp、降级档顺序来自单一事实源、
    # 主路径不重复出现在降级档、降级链非空且显式开启（绝不静默无兜底）。
    assert cfg["primary"] == "omp"
    assert cfg["engine_order"] == list(ENGINE_ORDER)
    assert cfg["primary"] not in cfg["fallback"]
    assert cfg["fallback"] == [e for e in ENGINE_ORDER if e != cfg["primary"]]
    assert cfg["fallback"] and cfg["fallback_enabled"] is True
    assert isinstance(cfg["omp_binary_path"], str)
    assert 0 < cfg["context_threshold_ratio"] <= 1
    assert cfg["max_retries"] >= 0
    assert cfg["task_timeout"] > 0
    assert isinstance(cfg["provider_auth_configured"], bool)
    # 脱敏：只回报「是否配置」，绝不回传凭据路径 / 内容。
    assert "provider_auth" not in cfg


# ===================== list_paused / get_paused =====================


def test_list_paused_empty_initially(tmp_path):
    """新网关下挂起列表恒为空（零消费点收口后的基线断言）。"""
    gw = _gateway(tmp_path)
    assert gw.list_paused() == []
    assert gw.get_paused("nonexistent") is None


# ===================== 挂起记录落盘 + resume =====================


def test_persist_paused_then_resume_recovers(tmp_path):
    """显式关闭降级时：首次失败 ⇒ paused + 落盘；resume 重跑 ⇒ completed。

    ⚠️ R240 重锚：R239 默认允许降级，故用 ``allow_fallback=False`` 把本用例
       原本要验证的「挂起 / 现场保留 / 人工恢复」语义**逐字保留**，
       同时避免经 platform_executor 触发真实网络调用。
    """
    gw = _gateway(tmp_path)
    # fail_times=1：第 1 次崩溃，第 2 次成功，模拟「重试后恢复」
    mock = MockOmpExecutor(response='{"sections": []}', fail_times=1, fail_kind="execution")
    gw._executor = mock  # 注入 mock（生产恒为 OmpAgentExecutor，测试离线用 mock）

    task = _task("t-resume-1")
    result = asyncio.run(gw.execute(task, allow_fallback=False))
    assert result.paused is True
    assert result.status == "paused"
    assert result.attempts == 1  # mock 单次调用即失败，首轮即挂起

    # 挂起记录落盘
    paused = gw.list_paused()
    assert len(paused) == 1
    assert paused[0]["task_id"] == "t-resume-1"
    assert paused[0]["engine"] == "omp"
    # R239 重锚：显式关闭降级 ⇒ 降级链为空且未耗尽（不是「无兜底」）。
    assert paused[0]["result"]["meta"]["fallback_chain"] == []
    assert paused[0]["result"]["meta"]["fallback_exhausted"] is False
    got = gw.get_paused("t-resume-1")
    assert got is not None
    assert got["result"]["paused"] is True

    # resume：从挂起记录重建任务，仍交给同一 omp 执行器重跑（绝不改走别的引擎）
    resumed = asyncio.run(gw.resume("t-resume-1"))
    assert resumed.ok is True
    assert resumed.status == "completed"
    assert resumed.payload == '{"sections": []}'
    assert resumed.attempts == 2  # resume 又跑了一次（mock 第 2 次成功）

    # resume 后挂起记录应被清理
    assert gw.get_paused("t-resume-1") is None
    assert gw.list_paused() == []


def test_omp_failure_degrades_explicitly(tmp_path, caplog):
    """R239 新语义：主路径失败 ⇒ 显式降级成功，业务产物不中断且绝不静默。"""
    degraded_payload = '{"offline": "降级档替身产物"}'

    async def offline_local(task, workspace):
        return degraded_payload

    mock = MockOmpExecutor(response='{"sections": []}', fail_times=1, fail_kind="execution")
    gw = _gateway(tmp_path, executor=mock, fallback_executors={"local": offline_local})

    with caplog.at_level("WARNING", logger="ai_provider.agent_engine"):
        result = asyncio.run(gw.execute(_task("t-degrade-1")))

    assert result.ok is True and result.status == "completed"
    assert result.requested_engine == "omp"                 # 主路径身份不可被覆盖
    assert result.engine in list(ENGINE_ORDER)[1:]          # 落降级档，绝不落回 omp
    assert result.payload == degraded_payload               # 业务产物不中断
    assert result.meta["degraded"] is True                  # 显式标注，绝不静默
    assert result.meta["engine_used"] == result.engine
    assert result.meta["fallback_chain"]                    # 降级链留痕
    assert result.meta["primary_errors"]                    # 主路径失败原因保留
    assert "降级" in result.meta["engine_reason"]
    assert "显式降级" in caplog.text
    assert gw.list_paused() == []                           # 降级成功不挂起、不落盘


def test_resume_unknown_task_returns_paused_with_reason(tmp_path):
    """resume 不存在的任务 ⇒ paused + 明确 reason + 现场保留标记。"""
    gw = _gateway(tmp_path)
    result = asyncio.run(gw.resume("does-not-exist"))
    assert result.ok is False
    assert result.paused is True
    assert result.scene_preserved is True
    assert "不存在" in result.reason or "不可读" in result.reason


# ===================== sse_event 终态事件 =====================


def test_sse_event_paused_shape(tmp_path):
    """挂起终态事件：降级语义显式可见，error / manual_review / resume_url 在场。"""
    gw = _gateway(tmp_path)
    task = _task("t-sse-1")
    result = AgentResult(
        ok=False,
        task_id="t-sse-1",
        task_type="outline",
        engine="omp",
        requested_engine="omp",
        status="paused",
        paused=True,
        attempts=2,
        errors=["[execution] omp 进程崩溃（模拟）"],
        reason="omp 执行失败（execution）⇒ 任务挂起，待人工介入",
        elapsed_ms=123,
        manual_review="omp 进程崩溃（模拟）",
        scene_preserved=True,
        meta={"kind": "execution", "workspace": str(tmp_path / "ws")},
    )

    event = gw.sse_event(task, result)
    assert event["event"] == "agent_paused"
    assert event["task_id"] == "t-sse-1"
    assert event["engine"] == "omp"
    # R239 重锚：不再有 fallback 键；改用「显式可见」字段，强度不减。
    assert event["degraded"] is False
    assert event["fallback_chain"] == []
    assert event["engine_used"] == "omp"
    assert event["requested_engine"] == "omp"
    assert event["fallback_exhausted"] is False
    assert "error" in event
    assert event["error"]["kind"] == "execution"
    assert event["error"]["message"] == "omp 执行失败（execution）⇒ 任务挂起，待人工介入"
    assert event["manual_review"] == "omp 进程崩溃（模拟）"
    assert event["scene_preserved"] is True
    assert event["resume_url"].endswith(f"/api/v1/omp/agent/paused/{task.task_id}/resume")


def test_sse_event_completed_shape(tmp_path):
    """完成终态事件：未降级时 degraded=False、降级链为空，payload/meta 下发。"""
    gw = _gateway(tmp_path)
    task = _task("t-sse-2")
    result = AgentResult(
        ok=True,
        task_id="t-sse-2",
        task_type="outline",
        engine="omp",
        requested_engine="omp",
        status="completed",
        payload='{"ok": true}',
        attempts=1,
        elapsed_ms=45,
        meta={"mock": True},
    )

    event = gw.sse_event(task, result)
    assert event["event"] == "agent_completed"
    assert event["degraded"] is False
    assert event["fallback_chain"] == []
    assert event["engine_used"] == "omp"
    assert event["requested_engine"] == "omp"
    assert event["engine_reason"] == ""
    assert event["payload"] == '{"ok": true}'
    assert event["meta"].get("mock") is True


def test_sse_event_degraded_shape(tmp_path):
    """降级成功事件：前端必须能显式识别降级，不得误判为 omp 主路径成功。"""
    gw = _gateway(tmp_path)
    task = _task("t-sse-degraded")
    result = AgentResult(
        ok=True,
        task_id=task.task_id,
        task_type="outline",
        engine="local",
        requested_engine="omp",
        status="completed",
        payload='{"ok": true}',
        attempts=2,
        meta={
            "degraded": True,
            "engine_used": "local",
            "engine_reason": "omp 主路径失败，降级至 local",
            "fallback_chain": ["local"],
            "primary_errors": ["[execution] omp 进程崩溃（模拟）"],
        },
    )

    event = gw.sse_event(task, result)
    assert event["event"] == "agent_completed"
    assert event["degraded"] is True
    assert event["engine_used"] == "local"
    assert event["requested_engine"] == "omp"
    assert event["fallback_chain"] == ["local"]
    assert "降级" in event["engine_reason"]
    assert event["payload"] == '{"ok": true}'


# ===================== executor 默认保留现场 =====================


def test_executor_keeps_workspace_on_pause_by_default():
    """OmpAgentExecutor 默认 keep_workspace_on_pause=True（挂起不清工作区，供人工排查）。"""
    from app.services.ai.agent_engine.executors import OmpAgentExecutor

    ex = OmpAgentExecutor()
    assert ex._keep_workspace_on_pause is True
