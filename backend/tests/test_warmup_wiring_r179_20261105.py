"""R179 U5 接线层用例（2026-11-05）。

锁定三件事：
1. 开关关闭（``stage_concurrency`` 返回 0）→ 走现状分支：不调用 ``run_grouped``、
   不读取 ``stage_delay_ms``、并发仍取 ``_item_concurrency()``、等待仍是一次
   ``PROMPT_CACHE_WARMUP_DELAY_MS``；
2. 开关打开 → 整组交给 ``warmup.run_grouped``（代表先行 + 阶段并发）；
3. 开关打开但阶段并发=1 → 自检报 ``enabled_but_no_gain`` 且等价现状
   （``applied=False`` / ``degraded_reason=no_gain``）。

等价性证明口径：用 ``ba.asyncio`` 替身记录每次 sleep 的秒数，用
``_run_single_item`` 替身记录执行顺序与最大在途数，逐项对比两条分支。
"""

import asyncio

import pytest
from app.routers import bid_analysis as ba
from app.services.ai import warmup as w


class _AsyncioSpy:
    """ba 模块内 asyncio 的替身：只替换 sleep，其余透传（记录等待次数）。"""

    def __init__(self):
        self.sleeps = []
        self.Semaphore = asyncio.Semaphore
        self.gather = asyncio.gather

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


class _Cfg:
    force_rerun = False

    def __init__(self, items):
        self._items = items

    async def get_task_items_async(self, db, project_id):  # noqa: D401 - 测试替身
        return list(self._items)


def _items():
    return [
        {"item_id": "projectBasicInfo", "output_type": "json"},
        {"item_id": "projectOverview", "output_type": "markdown"},
        {"item_id": "scopeOfWork", "output_type": "markdown"},
    ]


def _install(monkeypatch, *, delay_ms=0, stage_conc=0):
    """安装同步入口的全部替身，返回可观测记录。"""
    state = {"now": 0, "max_inflight": 0, "order": []}
    spy = _AsyncioSpy()
    calls = {"grouped": [], "delay_stage": []}

    async def _noop(*_a, **_k):
        return None

    async def _empty(*_a, **_k):
        return []

    async def _hint(*_a, **_k):
        return ""

    def _evidence(*_a, **_k):
        return ""

    async def _single(*args, **kwargs):
        item = args[3] if len(args) > 3 else kwargs["item"]
        state["now"] += 1
        state["max_inflight"] = max(state["max_inflight"], state["now"])
        state["order"].append(item["item_id"])
        try:
            await asyncio.sleep(0)
            return {"content": "ok"}
        finally:
            state["now"] -= 1

    monkeypatch.setattr(ba, "asyncio", spy)
    monkeypatch.setattr(ba, "PROMPT_CACHE_WARMUP_DELAY_MS", 0)
    monkeypatch.setattr(ba, "_resolve_classification_hint", _hint)
    monkeypatch.setattr(ba, "_split_tender_text", lambda text: [text])
    monkeypatch.setattr(ba, "_update_item_status", _noop)
    monkeypatch.setattr(ba, "_reset_items_for_rerun", _noop)
    monkeypatch.setattr(ba, "_get_missing_required", _empty)
    monkeypatch.setattr(ba, "_safe_invalidate_downstream", _noop)
    monkeypatch.setattr(ba, "_safe_evidence", _evidence)
    monkeypatch.setattr(ba, "_run_single_item", _single)

    monkeypatch.setattr(w, "stage_concurrency", lambda stage, settings_obj=None: stage_conc)

    def _delay(stage, settings_obj=None):
        calls["delay_stage"].append(stage)
        return delay_ms

    monkeypatch.setattr(w, "stage_delay_ms", _delay)

    real_grouped = w.run_grouped

    async def _spy_grouped(plans, runner, **kwargs):
        calls["grouped"].append(
            {
                "plans": [
                    {
                        "key": p.key,
                        "items": [it["item_id"] for it in p.items],
                        "concurrency": p.concurrency,
                        "representative_index": p.representative_index,
                        "delay_ms": p.delay_ms,
                    }
                    for p in plans
                ],
                "enabled": kwargs.get("enabled"),
                "default_delay_ms": kwargs.get("default_delay_ms"),
            }
        )
        return await real_grouped(plans, runner, **kwargs)

    monkeypatch.setattr(w, "run_grouped", _spy_grouped)
    return state, calls, spy


@pytest.mark.asyncio
async def test_disabled_branch_is_verbatim(monkeypatch):
    """开关关闭：不调编排器、不读阶段等待、并发=1、仅一次现状等待。"""
    state, calls, spy = _install(monkeypatch, stage_conc=0)
    items = _items()
    cfg = _Cfg(items)

    result = await ba._run_bid_analysis_sync(object(), "pid", "sid", cfg, "正文", task_id="")

    assert calls["grouped"] == [], "开关关闭不得调用 run_grouped"
    assert calls["delay_stage"] == [], "开关关闭不得读取 stage_delay_ms"
    assert state["order"] == ["projectBasicInfo", "projectOverview", "scopeOfWork"]
    assert state["max_inflight"] == 1, "开关关闭必须保持项级串行（默认 1）"
    assert spy.sleeps == [0.0], "开关关闭只允许既有的一次预热等待"
    assert result["ok"] is True
    assert result["warmup"]["enabled"] is False
    assert result["warmup"]["equivalent"] == "verbatim"
    assert result["warmup"]["degraded_reason"] == "disabled"


@pytest.mark.asyncio
async def test_enabled_branch_delegates_to_run_grouped(monkeypatch):
    """开关打开：整组（含代表）交给 run_grouped，阶段并发生效。"""
    state, calls, spy = _install(monkeypatch, stage_conc=2)
    items = _items()
    cfg = _Cfg(items)

    result = await ba._run_bid_analysis_sync(object(), "pid", "sid", cfg, "正文", task_id="")

    assert len(calls["grouped"]) == 1, "开关打开必须走 run_grouped 一次"
    spec = calls["grouped"][0]
    assert spec["enabled"] is True
    assert spec["plans"][0]["key"] == "facts"
    assert spec["plans"][0]["items"] == [it["item_id"] for it in items]
    assert spec["plans"][0]["concurrency"] == 2
    assert spec["plans"][0]["representative_index"] == 0
    assert calls["delay_stage"] == ["facts"]
    assert state["order"][0] == "projectBasicInfo", "代表项必须先行"
    assert state["max_inflight"] <= 2
    assert result["ok"] is True
    assert result["warmup"]["enabled"] is True
    assert result["warmup"]["applied"] is True
    assert result["warmup"]["concurrency"] == 2


@pytest.mark.asyncio
async def test_enabled_but_concurrency_one_is_equivalent_no_gain(monkeypatch, caplog):
    """开关开但阶段并发=1：自检告警，实际调度与现状等价（不等待、不并行）。"""
    state, calls, spy = _install(monkeypatch, stage_conc=1)
    cfg = _Cfg(_items())

    result = await ba._run_bid_analysis_sync(object(), "pid", "sid", cfg, "正文", task_id="")

    assert len(calls["grouped"]) == 1, "开关开也必须过 run_grouped 统一入口"
    assert state["order"] == ["projectBasicInfo", "projectOverview", "scopeOfWork"]
    assert state["max_inflight"] == 1
    assert spy.sleeps == [], "并发=1 时不得白等预热延迟"
    assert result["warmup"]["applied"] is False
    assert result["warmup"]["degraded_reason"] == "no_gain"
    assert result["warmup"]["equivalent"] == "verbatim"

    stub = type(
        "S",
        (),
        {
            "ai_warmup_enabled": True,
            "ai_warmup_stages": "",
            "ai_warmup_stage_concurrency": 1,
            "ai_warmup_stage_concurrency_map": "",
            "max_concurrency": 5,
            "ai_warmup_delay_ms": 0,
        },
    )()
    report = w.config_selfcheck(stub)
    assert report["status"] == "enabled_but_no_gain"
    w.log_config_selfcheck(stub)
    assert any("AI_WARMUP_CONFIG" in r.message for r in caplog.records if r.levelname == "WARNING")


@pytest.mark.asyncio
async def test_sse_disabled_and_enabled_branch(monkeypatch):
    """SSE 入口同口径：关闭不调编排器；打开交给 run_grouped。"""
    state, calls, spy = _install(monkeypatch, stage_conc=0)
    cfg = _Cfg(_items())

    async def _cb(*_a, **_k):
        return None

    await ba._run_bid_analysis_with_progress(object(), "pid", "sid", cfg, "正文", _cb, task_id="")
    assert calls["grouped"] == [], "SSE 关闭分支不得调用 run_grouped"
    assert spy.sleeps == [0.0]

    state2, calls2, spy2 = _install(monkeypatch, stage_conc=2)
    await ba._run_bid_analysis_with_progress(object(), "pid", "sid", _Cfg(_items()), "正文", _cb, task_id="")
    assert len(calls2["grouped"]) == 1, "SSE 开启分支必须走 run_grouped"
    assert calls2["grouped"][0]["plans"][0]["concurrency"] == 2
    assert state2["order"][0] == "projectBasicInfo"
