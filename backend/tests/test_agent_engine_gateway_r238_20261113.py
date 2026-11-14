"""R239 · **omp 主路径 + 降级路径 + 可双轨**契约与全链路护栏。

用户裁决（2026-11-13）：「以 can1357/oh-my-pi (omp) 作为底层 Agent 引擎，
为主路径，其它为降级路径，可双轨」⇒ 重锚 R238「单轨无回退」护栏为
「主路径优先 + 显式降级 + 全链失败才挂起」，意图改变但强度不减。

* A 契约面：主路径恒 omp；降级顺序来自 ``omp_engine_router.ENGINE_ORDER``
  单一事实源（不造第三套档位）；配置全部来自配置文件且凭据脱敏；
  全局 Feature Flag 关闭 ⇒ 挂起。
* B 主路径 / 降级 / 双轨：omp 成功不降级；omp 失败显式标注 degraded 降级 local；
  ``allow_fallback=false`` 关闭降级；``prefer=main`` 显式走另一轨；
  **模拟 omp 崩溃 + 降级档全失败** ⇒ 任务挂起 + 现场保护。
* C 全链路 HTTP 级：execute / execute-stream / paused / resume 端点契约。

零真实 omp 进程、零外部网络、零业务库访问（降级执行体全部注入替身）。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from app.services.ai.agent_engine import (
    AgentEngineConfig,
    AgentResult,
    AgentTask,
    IAgentExecutor,
    MockOmpExecutor,
    OmpAgentExecutor,
    OmpAgentGateway,
    public_config,
)
from app.services.ai.agent_engine.gateway import engine_policy
from fastapi import FastAPI


def _task(**kw) -> AgentTask:
    base = dict(task_id="t_r239", task_type="outline", prompt="写目录")
    base.update(kw)
    return AgentTask(**base)


def _all_fallbacks_fail() -> dict:
    async def fail(task, workspace):
        raise RuntimeError("降级档不可用（模拟）")

    return {"local": fail, "main": fail}


def _fallback_succeeds(engine: str, text: str, calls: list[str]) -> dict:
    async def ok(task, workspace):
        calls.append(engine)
        return text

    async def fail(task, workspace):
        calls.append("main")
        raise RuntimeError("主链不可用（模拟）")

    return {"local": ok, "main": fail} if engine == "local" else {"local": fail, "main": ok}


# ===================== A. 契约面 =====================
def test_gateway_primary_is_omp_and_reachable(tmp_path):
    """主路径恒为 omp，且网关确实持有唯一主路径执行器（可达性护栏）。"""
    gw = OmpAgentGateway(state_dir=str(tmp_path))
    assert isinstance(gw.executor, IAgentExecutor)
    assert gw.executor.name == "omp" and gw.name == "omp"
    policy = engine_policy()
    assert policy["primary"] == "omp"
    assert policy["fallback"] == ["local", "main"]
    assert policy["fallback_enabled"] is True


def test_fallback_uses_engine_order_single_source():
    """降级顺序必须复用 omp_engine_router.ENGINE_ORDER（不另造第三套档位）。"""
    import ast
    import inspect

    from app.services.ai.agent_engine import gateway as gw_mod
    from app.services.ai.omp_engine_router import ENGINE_ORDER

    assert engine_policy()["engine_order"] == list(ENGINE_ORDER)
    tree = ast.parse(inspect.getsource(gw_mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    # 必须引用判据模块；不得回流旧双轨路由类。
    assert any("omp_engine_router" in n for n in names)
    assert "PiAgentExecutor" not in names and "AgentRouter" not in names


def test_public_config_is_complete_redacted_and_exposes_chain():
    cfg = public_config()
    for key in ("enabled", "primary", "fallback", "fallback_enabled", "engine_order",
                "omp_binary_path", "max_retries", "retry_backoff_base",
                "compaction_threshold", "context_threshold_ratio", "task_timeout"):
        assert key in cfg, key
    assert cfg["engine"] == "omp" and cfg["primary"] == "omp"
    assert "provider_auth" not in cfg
    assert isinstance(cfg["provider_auth_configured"], bool)


def test_config_can_disable_fallback(monkeypatch):
    """降级开关与降级档裁剪均从配置读取。"""
    import app.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "agent_engine_fallback_enabled", False, raising=False)
    assert engine_policy()["fallback"] == []
    monkeypatch.setattr(cfg_mod.settings, "agent_engine_fallback_enabled", True, raising=False)
    monkeypatch.setattr(cfg_mod.settings, "agent_engine_fallback_order", "main", raising=False)
    assert engine_policy()["fallback"] == ["main"]
    monkeypatch.setattr(cfg_mod.settings, "agent_engine_fallback_order", "bogus", raising=False)
    assert engine_policy()["fallback"] == []


def test_feature_flag_off_pauses_immediately(tmp_path, monkeypatch):
    """全局 Feature Flag 关闭 ⇒ 整层不可用，不进降级链，直接挂起。"""
    import app.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "agent_engine_enabled", False, raising=False)
    calls: list[str] = []
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="ok"),
        fallback_executors=_fallback_succeeds("local", "x", calls),
        state_dir=str(tmp_path),
    )
    res = asyncio.run(gw.execute(_task()))
    assert res.paused is True and res.status == "paused"
    assert any("未启用" in e for e in res.errors)
    assert calls == []                       # 没有偷偷降级
    assert gw.sse_event(_task(), res)["fallback_chain"] == []


# ===================== B. 主路径 / 降级 / 双轨 =====================
def test_primary_success_does_not_degrade(tmp_path):
    calls: list[str] = []
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="primary-ok"),
        fallback_executors=_fallback_succeeds("local", "fallback", calls),
        state_dir=str(tmp_path),
    )
    res = asyncio.run(gw.execute(_task()))
    assert res.ok is True and res.payload == "primary-ok"
    assert not res.meta.get("degraded")
    assert calls == []                       # omp 成功 ⇒ 降级档零调用


def test_primary_failure_degrades_to_local_with_explicit_mark(tmp_path):
    """omp 失败 ⇒ 降级 local，且 degraded / engine_used / 主路径错误全部显式可见。"""
    calls: list[str] = []
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="never", fail_times=99),
        fallback_executors=_fallback_succeeds("local", "fallback-ok", calls),
        state_dir=str(tmp_path),
    )
    res = asyncio.run(gw.execute(_task()))
    assert res.ok is True and res.payload == "fallback-ok"
    assert res.engine == "local" and res.requested_engine == "omp"
    assert res.meta["degraded"] is True
    assert res.meta["engine_used"] == "local"
    assert res.meta["fallback_chain"] == ["local"]
    assert res.meta["primary_errors"] and "omp" in json.dumps(res.meta, ensure_ascii=False)
    assert calls == ["local"]
    event = gw.sse_event(_task(), res)
    assert event["event"] == "agent_completed"
    assert event["degraded"] is True and event["engine_used"] == "local"


def test_explicit_dual_track_prefer_main(tmp_path):
    """可双轨：显式 prefer=main 直接走主链路，omp 零调用且不算降级。"""
    calls: list[str] = []
    primary = MockOmpExecutor(response="unused")
    gw = OmpAgentGateway(
        executor=primary,
        fallback_executors=_fallback_succeeds("main", "main-ok", calls),
        state_dir=str(tmp_path),
    )
    res = asyncio.run(gw.execute(_task(), prefer="main"))
    assert res.ok is True and res.payload == "main-ok"
    assert primary.calls == 0
    assert res.engine == "main"
    assert not res.meta.get("degraded")      # 显式选轨 ≠ 故障降级


def test_fallback_can_be_disabled_per_request(tmp_path):
    calls: list[str] = []
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="never", fail_times=99),
        fallback_executors=_fallback_succeeds("local", "x", calls),
        state_dir=str(tmp_path),
    )
    res = asyncio.run(gw.execute(_task(), allow_fallback=False))
    assert res.paused is True
    assert res.meta["fallback_chain"] == [] and calls == []
    assert gw.get_paused("t_r239") is not None


def test_omp_crash_all_fallbacks_fail_pauses_and_preserves_scene(tmp_path, monkeypatch):
    """模拟 omp 崩溃：重试耗尽 + 降级链全失败 ⇒ 挂起 + 现场保留。"""
    import app.config as cfg_mod
    from app.services.ai import omp_rpc
    from app.services.ai.agent_engine.contracts import AgentExecutionError

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CONTEXT.md").write_text("现场输入", encoding="utf-8")

    cfg = AgentEngineConfig(
        enabled=True, omp_binary_path=str(tmp_path / "omp"),
        max_retries=1, retry_backoff_base=0.0, compaction_threshold=0,
    )
    (tmp_path / "omp").write_text("fake", encoding="ascii")
    monkeypatch.setattr(cfg_mod.settings, "omp_task_model", "test-model", raising=False)
    monkeypatch.setattr(omp_rpc, "binary_exists", lambda b: True)

    class CrashingSession:
        def __init__(self, *args, **kwargs):
            return None

        async def start(self):
            return None

        async def run_prompt(self, text, on_frame=None):
            raise AgentExecutionError("omp 进程崩溃（模拟）")

        async def stop(self, cleanup_workspace=True):
            return None

    gw = OmpAgentGateway(
        executor=OmpAgentExecutor(config=cfg, session_factory=CrashingSession),
        fallback_executors=_all_fallbacks_fail(),
        state_dir=str(tmp_path / "state"),
    )
    res = asyncio.run(gw.execute(_task(workspace=str(workspace))))

    assert res.ok is False and res.paused is True and res.status == "paused"
    # 网关层 attempts = 总执行次数：主路径 2 次（首次 + 1 次指数退避重试）
    # + local / main 两档降级各 1 次；主路径重试次数由 R237 执行器层护栏单独锁定。
    assert res.attempts == 4
    assert res.scene_preserved is True
    assert any("崩溃" in e for e in res.errors)
    assert res.meta["fallback_chain"] == ["local", "main"]
    assert res.meta["fallback_exhausted"] is True
    # 现场保护：工作区未被清理，挂起记录落盘供重启后恢复。
    assert (workspace / "CONTEXT.md").read_text(encoding="utf-8") == "现场输入"
    record = gw.get_paused("t_r239")
    assert record is not None and record["result"]["paused"] is True

    event = gw.sse_event(_task(), res)
    assert event["event"] == "agent_paused"
    assert event["error"]["details"] and event["manual_review"]
    assert event["scene_preserved"] is True and event["fallback_exhausted"] is True
    assert event["resume_url"].endswith("/paused/t_r239/resume")


def test_resume_after_crash_reruns_primary(tmp_path):
    """人工介入恢复：重跑 omp 主路径，成功后清理挂起记录。"""
    mock = MockOmpExecutor(response='{"ok": true}', fail_times=1)
    gw = OmpAgentGateway(
        executor=mock, fallback_executors=_all_fallbacks_fail(), state_dir=str(tmp_path)
    )
    first = asyncio.run(gw.execute(_task()))
    assert first.paused is True and gw.get_paused("t_r239") is not None
    resumed = asyncio.run(gw.resume("t_r239", instruction="请改用更细的章节粒度"))
    assert resumed.ok is True and resumed.payload == '{"ok": true}'
    assert not resumed.meta.get("degraded")
    assert gw.get_paused("t_r239") is None
    assert mock.calls == 2


def test_resume_missing_record_pauses_explicitly(tmp_path):
    gw = OmpAgentGateway(executor=MockOmpExecutor(response="x"), state_dir=str(tmp_path))
    res = asyncio.run(gw.resume("not-exist"))
    assert res.paused is True and "挂起记录不存在" in res.reason


def test_agent_result_has_no_degraded_dataclass_field():
    """降级标注放 meta，不新增状态/字段（R237 状态口径 completed|paused 不变）。"""
    assert "degraded" not in AgentResult.__dataclass_fields__
    assert "degraded" not in AgentResult.to_dict(AgentResult())


# ===================== C. 全链路 HTTP 级 =====================
def _app_with_gateway(monkeypatch, gw: OmpAgentGateway) -> FastAPI:
    from app.routers import omp_agent

    monkeypatch.setattr(omp_agent, "get_gateway", lambda: gw)
    app = FastAPI()
    app.include_router(omp_agent.router)
    return app


@pytest.mark.asyncio
async def test_http_execute_degraded_annotation(tmp_path, monkeypatch):
    calls: list[str] = []
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="never", fail_times=99),
        fallback_executors=_fallback_succeeds("local", "fallback-ok", calls),
        state_dir=str(tmp_path),
    )
    app = _app_with_gateway(monkeypatch, gw)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        conf = (await c.get("/api/v1/omp/agent/config")).json()
        assert conf["primary"] == "omp" and conf["fallback"] == ["local", "main"]

        resp = await c.post("/api/v1/omp/agent/execute", json={
            "task_id": "http_1", "task_type": "outline", "prompt": "写目录",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True and data["payload"] == "fallback-ok"
        # annotate_result 加法式标注，前端可显式看到「已降级」。
        assert data["degraded"] is True and data["engine_used"] == "local"


@pytest.mark.asyncio
async def test_http_pause_and_resume(tmp_path, monkeypatch):
    mock = MockOmpExecutor(response="done", fail_times=1)
    gw = OmpAgentGateway(
        executor=mock, fallback_executors=_all_fallbacks_fail(), state_dir=str(tmp_path)
    )
    app = _app_with_gateway(monkeypatch, gw)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        paused = await c.post("/api/v1/omp/agent/execute", json={
            "task_id": "http_2", "prompt": "写目录",
        })
        assert paused.status_code == 200 and paused.json()["paused"] is True
        rec = await c.get("/api/v1/omp/agent/paused/http_2")
        assert rec.status_code == 200 and rec.json()["result"]["paused"] is True
        resumed = await c.post("/api/v1/omp/agent/paused/http_2/resume",
                               json={"instruction": "人工补充"})
        assert resumed.status_code == 200 and resumed.json()["payload"] == "done"


@pytest.mark.asyncio
async def test_http_sse_emit_explicit_paused_event(tmp_path, monkeypatch):
    """SSE：全链失败推 agent_paused，含明确异常与人工介入恢复入口。"""
    gw = OmpAgentGateway(
        executor=MockOmpExecutor(response="x", fail_times=99),
        fallback_executors=_all_fallbacks_fail(),
        state_dir=str(tmp_path),
    )
    app = _app_with_gateway(monkeypatch, gw)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/api/v1/omp/agent/execute-stream",
                            json={"task_id": "sse_1", "prompt": "写目录"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    frames = [json.loads(line[6:]) for line in resp.text.splitlines()
              if line.startswith("data: ")]
    assert frames[0]["event"] == "agent_started"
    final = frames[-1]
    assert final["event"] == "agent_paused"
    assert final["error"]["message"] and final["manual_review"]
    assert final["scene_preserved"] is True and final["fallback_exhausted"] is True
    assert final["fallback_chain"] == ["local", "main"]
    assert final["resume_url"].endswith("/paused/sse_1/resume")


def test_sections_dual_track_gate_is_restored():
    """可双轨裁决：omp 旁路入口不得再被单轨 422 闸门挡死。"""
    from pathlib import Path

    import app.routers.sections as sec_mod

    src = Path(sec_mod.__file__).read_text(encoding="utf-8")
    assert "单轨模式下 engine 仅支持" not in src
    assert src.count("resolve_engine(") >= 2  # outline / workflow 两处双轨分流可达
    assert "delegate_to" in src               # main 档显式委托平台主链路
