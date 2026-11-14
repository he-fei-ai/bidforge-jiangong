"""R236 · omp 引擎层收口护栏（2026-11-13）。

覆盖附件方案《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》的三项遗留落地：

* **§5.4 阈值路由**：按「上下文窗口 × 任务比率」决定走 ``chat``（本仓 ``main``）/
  ``omp`` / ``local`` —— 只做**引擎层**判定，**不动主链字数判据**；
* **§5.6 降级链补档**：``omp → local（路径 B 本地编排）→ main（≡ chat）`` 三档；
  并落地路径 B 的**平台侧执行器**（不依赖 omp 进程，用平台 AI 链跑同一套
  plan→execute→verify→fix）；
* **§2.2 G-2 输入指纹缓存 + §5.3 持久任务**：``omp_task_store`` 接进编排器
  （命中即零 AI 调用复用产物，完成后回写）；
* **§5.12 诊断补面**：Session 快照 + 错误归一（分类复用 ``classify_error``）。

全程**不拉起真实 omp 二进制**、不访问业务库、不产生外部副作用。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.services.ai.omp_diagnostics import serialize_error, session_snapshot
from app.services.ai.omp_engine_router import ENGINE_ORDER, select_engine
from app.services.ai.omp_task_store import OmpTaskStore
from app.services.ai.omp_threshold import (
    DEFAULT_THRESHOLD_RATIOS,
    get_agent_threshold,
    select_engine_for_messages,
)
from app.services.ai.omp_workflow import (
    BatchRequest,
    OmpBatchOrchestrator,
    OmpTaskSpec,
    platform_executor,
)

# ===================== A. 阈值路由 =====================


def test_default_ratios_match_attachment():
    """附件 §5.4：正文生成 0.7 / 一致性修复 0.85 / 编排 0.8。"""
    assert DEFAULT_THRESHOLD_RATIOS["content"] == 0.7
    assert DEFAULT_THRESHOLD_RATIOS["audit"] == 0.85
    assert DEFAULT_THRESHOLD_RATIOS["planning"] == 0.8


def test_get_agent_threshold_scales_by_ratio():
    assert get_agent_threshold(context_limit=100_000, task_type="content") == 70_000
    assert get_agent_threshold(context_limit=100_000, task_type="audit") == 85_000
    assert get_agent_threshold(context_limit=100_000, task_type="unknown") == 80_000


def test_get_agent_threshold_unknown_window_is_zero():
    assert get_agent_threshold(context_limit=0, task_type="content") == 0
    assert get_agent_threshold(context_limit=-5, task_type="content") == 0


def test_content_ratio_follows_platform_config(monkeypatch):
    """content 比率**同源**读平台配置（并行轮次 R237 的 agent_engine_context_threshold_ratio）。

    目的：避免「配置一份、常量一份」的双份副本漂移 —— 配置改了阈值必须跟着变。
    """
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "agent_engine_context_threshold_ratio", 0.5, raising=False)
    assert get_agent_threshold(context_limit=100_000, task_type="content") == 50_000
    # 显式传入 ratios 时以传入值为准（测试与调用方可覆盖）
    assert (
        get_agent_threshold(
            context_limit=100_000, task_type="content", ratios={"content": 0.7}
        )
        == 70_000
    )
    # 非法配置 ⇒ 回落默认表（不臆造）
    monkeypatch.setattr(cfg.settings, "agent_engine_context_threshold_ratio", 1.7, raising=False)
    assert get_agent_threshold(context_limit=100_000, task_type="content") == 70_000


@pytest.mark.parametrize("bad", [1.5, 0, -0.2, "abc", None])
def test_invalid_ratio_falls_back_to_default(bad):
    """非法比率 ⇒ 回落 0.8（不臆造阈值）。"""
    got = get_agent_threshold(context_limit=100_000, task_type="content", ratios={"content": bad})
    assert got == 80_000


def test_select_engine_under_threshold_goes_main():
    out = select_engine_for_messages(
        task_type="content", context_chars=1000, context_limit=100_000
    )
    assert out["engine"] == "main"
    assert out["threshold"] == 70_000
    assert "未超阈值" in out["reason"]


def test_select_engine_over_threshold_goes_omp():
    out = select_engine_for_messages(
        task_type="content", context_chars=90_000, context_limit=100_000
    )
    assert out["engine"] == "omp"
    assert out["reason"]


def test_select_engine_over_threshold_falls_to_local_then_main():
    local = select_engine_for_messages(
        task_type="content",
        context_chars=90_000,
        context_limit=100_000,
        omp_available=False,
    )
    assert local["engine"] == "local"
    worst = select_engine_for_messages(
        task_type="content",
        context_chars=90_000,
        context_limit=100_000,
        omp_available=False,
        local_available=False,
    )
    assert worst["engine"] == "main"


def test_select_engine_unknown_window_does_not_guess():
    out = select_engine_for_messages(
        task_type="content", context_chars=999_999, context_limit=0, omp_available=True
    )
    assert out["engine"] == "main"
    assert out["threshold"] == 0


def test_select_engine_explicit_prefer_wins():
    out = select_engine_for_messages(
        task_type="content",
        context_chars=1,
        context_limit=100_000,
        prefer="local",
    )
    assert out["engine"] == "local"
    assert "显式" in out["reason"]


def test_threshold_engine_names_match_router_order():
    """阈值路由的档位名必须落在引擎路由的降级链词汇内（禁止第三套词汇）。"""
    assert ENGINE_ORDER == ("omp", "local", "main")
    for prefer in ("", "omp", "local", "main"):
        out = select_engine_for_messages(
            task_type="content", context_chars=1, context_limit=100_000, prefer=prefer
        )
        assert out["engine"] in ENGINE_ORDER


# ===================== B. 本地编排档执行器（路径 B） =====================


def _outline_spec() -> OmpTaskSpec:
    from app.services.ai.omp_agent_roles import OUTLINE_ROLE

    return OmpTaskSpec(
        task_id="t1",
        instruction="写一份目录",
        output_file="OUTLINE.json",
        metadata={"role": OUTLINE_ROLE, "system_prompt": "额外要求：只写 JSON"},
    )


def test_platform_executor_writes_artifact_and_returns_text(tmp_path):
    spec = _outline_spec()
    ws = str(tmp_path)

    async def fake_chat(messages, **kwargs):
        fake_chat.captured = messages
        fake_chat.kwargs = kwargs
        return '{"sections": []}'

    out = asyncio.run(platform_executor(spec, "写 OUTLINE.json", ws, chat_fn=fake_chat))
    assert out == '{"sections": []}'
    assert (tmp_path / "OUTLINE.json").read_text(encoding="utf-8") == '{"sections": []}'
    # 角色 system_prompt + 自定义 override 叠加在场
    system = fake_chat.captured[0]["content"]
    assert "工作区红线" in system
    assert "只写 JSON" in system
    # 场景打标（否则场景模型路由静默失效）
    assert fake_chat.kwargs.get("scene") == "omp_local_executor"


def test_platform_executor_inlines_context_and_plan(tmp_path):
    """本地档**没有文件工具语义** ⇒ CONTEXT/PLAN 必须内联进 prompt。"""
    spec = _outline_spec()
    ws = str(tmp_path)
    (tmp_path / "CONTEXT.md").write_text("公共上下文XYZ", encoding="utf-8")
    (tmp_path / "PLAN.md").write_text("计划ABC", encoding="utf-8")

    async def fake_chat(messages, **kwargs):
        fake_chat.user = messages[-1]["content"]
        return "ok"

    asyncio.run(platform_executor(spec, "任务说明", ws, chat_fn=fake_chat))
    assert "公共上下文XYZ" in fake_chat.user
    assert "计划ABC" in fake_chat.user


def test_platform_executor_rejects_path_escape(tmp_path):
    spec = OmpTaskSpec(
        task_id="t2", instruction="x", output_file="../evil.json", metadata={}
    )

    async def fake_chat(messages, **kwargs):
        return "payload"

    with pytest.raises(ValueError):
        asyncio.run(platform_executor(spec, "x", str(tmp_path), chat_fn=fake_chat))
    assert not (tmp_path.parent / "evil.json").exists()


def test_local_executor_scene_is_registered():
    from app.services.ai.provider_factory import KNOWN_SCENES

    assert "omp_local_executor" in KNOWN_SCENES
    assert KNOWN_SCENES["omp_local_executor"].strip()


# ===================== C. 输入指纹缓存接线 =====================


def _spec() -> OmpTaskSpec:
    return OmpTaskSpec(
        task_id="t1", instruction="写正文", output_file="sections/a.md", metadata={}
    )


def _orchestrator(store, calls):
    async def executor(spec, prompt, workspace):
        calls.append(spec.task_id)
        return "产物内容"

    def verify(spec, payload):
        return []

    return OmpBatchOrchestrator(
        stage="content",
        executor_fn=executor,
        verify_fn=verify,
        store=store,
        scheme_id="scheme-1",
    )


def test_cache_miss_executes_then_second_run_hits_cache(tmp_path):
    store = OmpTaskStore(str(tmp_path / "omp_tasks.json"))
    calls: list[str] = []
    orch = _orchestrator(store, calls)
    request = BatchRequest(context="上下文", tasks=[_spec()])

    first = asyncio.run(orch.run_batch(request))
    assert first.results[0].status == "completed"
    assert first.results[0].phase == "done"
    assert calls == ["t1"]

    second = asyncio.run(orch.run_batch(request))
    assert second.results[0].phase == "cache_hit"
    assert second.results[0].attempts == 0
    assert second.results[0].payload == "产物内容"
    assert calls == ["t1"], "命中缓存不得再调用 AI"


def test_cache_state_persisted_for_resume(tmp_path):
    """状态持久化 + 断点语义：completed 的任务不可续跑；不同输入不得误命中。"""
    from app.services.ai.omp_task_store import cache_key as _cache_key

    store = OmpTaskStore(str(tmp_path / "omp_tasks.json"))
    calls: list[str] = []
    orch = _orchestrator(store, calls)
    asyncio.run(orch.run_batch(BatchRequest(context="上下文", tasks=[_spec()])))

    raw = json.loads((tmp_path / "omp_tasks.json").read_text(encoding="utf-8"))
    assert raw, "编排状态必须落盘（断点续跑的前提）"
    states = list(raw.values())
    assert any(st.get("status") == "completed" for st in states)
    assert any((st.get("artifact") or {}).get("payload") == "产物内容" for st in states)

    # 不同输入不得误命中
    assert store.cached_artifact(_cache_key(scheme_id="scheme-1", stage="content", inputs="别的输入")) is None
    # completed 状态不可续跑（is_resumable 排除终态）
    done_key = next(k for k, v in raw.items() if v.get("status") == "completed")
    assert store.is_resumable(done_key) is False


def test_without_store_behavior_unchanged(tmp_path):
    """未接 store ⇒ 完全不做缓存（每次都执行，行为与引入前一致）。"""
    calls: list[str] = []
    orch = _orchestrator(None, calls)
    request = BatchRequest(context="上下文", tasks=[_spec()])
    first = asyncio.run(orch.run_batch(request))
    second = asyncio.run(orch.run_batch(request))
    assert calls == ["t1", "t1"]
    assert first.results[0].phase == "done"
    assert second.results[0].phase == "done"


# ===================== D. 降级链三档 =====================


def test_engine_order_has_three_tiers():
    assert ENGINE_ORDER == ("omp", "local", "main")


def test_select_engine_local_explicit():
    c = select_engine(task_type="outline", prefer="local", omp_available=True)
    assert c.engine == "local" and c.degraded is False


def test_select_engine_omp_unavailable_degrades_to_local():
    c = select_engine(task_type="outline", prefer="omp", omp_available=False)
    assert c.engine == "local" and c.degraded is True
    worst = select_engine(
        task_type="outline", prefer="omp", omp_available=False, local_available=False
    )
    assert worst.engine == "main" and worst.degraded is True


def test_auto_behavior_unchanged():
    """auto 不自动落到 local（平台主链强于单次对话）⇒ 默认行为不变。"""
    assert select_engine(task_type="outline", prefer="auto", omp_available=True).engine == "omp"
    assert select_engine(task_type="outline", prefer="auto", omp_available=False).engine == "main"


# ===================== E. 诊断补面 =====================


def test_session_snapshot_shape_and_no_secret():
    snap = session_snapshot()
    for key in (
        "enabled",
        "binary",
        "binary_available",
        "model",
        "tools",
        "workspace_root",
        "rpc_timeout",
        "task_timeout",
        "keep_workspace",
        "workspace_isolated",
    ):
        assert key in snap
    dumped = json.dumps(snap, ensure_ascii=False)
    assert "api_key" not in dumped and "secret" not in dumped.lower()


def test_serialize_error_classifies_with_action():
    out = serialize_error(RuntimeError("HTTP 429 rate limited"))
    assert out["type"] == "RuntimeError"
    assert out["category"] == "rate_limited"
    assert out["action"]
    assert out["message"].startswith("HTTP 429")


def test_serialize_error_accepts_plain_string():
    out = serialize_error("boom")
    assert out["type"] == "Error"
    assert out["message"] == "boom"
    assert out["category"] in ("unknown", "json_invalid", "tool_failed")


# ===================== F. 端点契约 =====================


@pytest.mark.asyncio
async def test_diagnostics_endpoint_exposes_session():
    from app.routers import omp_ops

    out = await omp_ops.omp_diagnostics()
    assert "session" in out
    assert "binary_available" in out["session"]
