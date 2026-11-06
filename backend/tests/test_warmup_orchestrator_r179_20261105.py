"""R179 U5（2026-11-05）分组预热编排 · 回归护栏（方案 §2.7 用例 1~14）。

全部使用假 runner + 假时钟，禁止真实 sleep；断言调用顺序 / 在飞并发 /
失败语义 / 配置自检 / 既有默认值未被改动。
"""

import asyncio
from types import SimpleNamespace

import pytest
from app.config import settings as real_settings
from app.services.ai import warmup as w


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """记录调用顺序与在飞并发。"""

    def __init__(self, clock=None, behavior=None):
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.clock = clock
        self.behavior = behavior or (lambda item: {"success": True})

    async def runner(self, item):
        self.calls.append(item["id"])
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self.clock is not None:
            self.clock.now += 1.0
        await asyncio.sleep(0)
        try:
            res = self.behavior(item)
        finally:
            self.inflight -= 1
        if isinstance(res, BaseException):
            raise res
        return res


def _items(n: int) -> list[dict]:
    return [{"id": f"i{i}"} for i in range(n)]


def _plan(n: int, concurrency: int = 2, delay_ms: int | None = None) -> w.WarmupPlan:
    return w.WarmupPlan(key="content", items=_items(n), concurrency=concurrency, delay_ms=delay_ms)


def _cfg(**kw) -> SimpleNamespace:
    base = {
        "ai_warmup_enabled": False,
        "ai_warmup_delay_ms": 5000,
        "ai_warmup_stage_concurrency": 2,
        "ai_warmup_stage_concurrency_map": "",
        "ai_warmup_stages": "outline,content,facts,consistency_scan",
        "ai_warmup_fallback_serial": True,
        "ai_warmup_rep_retry_on_parse": True,
        "ai_warmup_stage_delay_ms": "",
        "max_concurrency": 5,
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def _run(plan, rec, clock, *, enabled=True, delay_ms=5000, fallback_serial=True, rep_retry=True):
    return await w.run_grouped(
        [plan],
        rec.runner,
        enabled=enabled,
        default_delay_ms=delay_ms,
        fallback_serial=fallback_serial,
        rep_retry_on_parse=rep_retry,
        sleep_fn=clock.sleep,
        clock_fn=clock.clock,
    )


# ------------------------------------------------------------- 1/2. 代表先行
async def test_case01_representative_goes_first():
    clock, rec = FakeClock(), Recorder()
    out = await _run(_plan(4), rec, clock)
    assert rec.calls[0] == "i0"
    assert out[0]["warmup"]["representative_ok"] is True
    assert out[0]["warmup"]["applied"] is True


async def test_case02_sleep_only_after_success_with_remaining():
    clock, rec = FakeClock(), Recorder()
    out = await _run(_plan(4), rec, clock)
    assert clock.sleeps == [5.0]
    assert out[0]["warmup"]["wait_ms"] == 5000

    # 单项：不 sleep、不进入预热分支
    clock2, rec2 = FakeClock(), Recorder()
    out2 = await _run(_plan(1), rec2, clock2)
    assert clock2.sleeps == []
    assert out2[0]["warmup"]["applied"] is False


# --------------------------------------------------------------- 3. 换代表一次
async def test_case03_parse_failed_switches_representative_once_then_serial():
    def behavior(item):
        return {"success": True, "parse_failed": item["id"] in ("i0", "i1")}

    clock, rec = FakeClock(), Recorder(behavior=behavior)
    out = await _run(_plan(5), rec, clock)
    assert rec.calls[:2] == ["i0", "i1"]
    assert rec.max_inflight == 1, "两次代表都失败 → 剩余必须串行"
    assert out[0]["warmup"]["representative_class"] == "parse_failed->parse_failed"
    assert clock.sleeps == [], "换代表后仍失败 → 不放开发等待"


# ------------------------------------------------------------------- 4. 429
async def test_case04_rate_limit_degrades_serial_without_retrying_rep():
    def behavior(item):
        if item["id"] == "i0":
            return {"success": False, "error_class": "rate_limit"}
        return {"success": True}

    clock, rec = FakeClock(), Recorder(behavior=behavior)
    out = await _run(_plan(4), rec, clock)
    assert rec.calls.count("i0") == 1, "429 不换代表、不重复调用代表"
    assert rec.max_inflight == 1
    assert out[0]["warmup"]["degraded_reason"] == "rate_limit"
    assert clock.sleeps == []


# --------------------------------------------------------------- 5. 整组放弃
async def test_case05_non_retryable_abandons_group():
    def behavior(item):
        if item["id"] == "i0":
            raise RuntimeError("HTTP 401 Unauthorized: invalid api key")
        return {"success": True}

    clock, rec = FakeClock(), Recorder(behavior=behavior)
    out = await _run(_plan(4), rec, clock)
    assert rec.calls == ["i0"], "认证类错误 → 剩余项零调用"
    assert out[0]["warmup"]["degraded_reason"] == "auth"
    assert all(r == w.SKIPPED for r in out[0]["results"][1:])


# ----------------------------------------------------------------- 6. 并发上限
async def test_case06_concurrency_never_exceeds_limit():
    clock, rec = FakeClock(), Recorder()
    await _run(_plan(6, concurrency=2), rec, clock)
    assert rec.max_inflight <= 2


async def test_case06b_stage_concurrency_clamped_by_max_concurrency():
    cfg = _cfg(ai_warmup_enabled=True, ai_warmup_stage_concurrency=99)
    assert w.stage_concurrency("content", cfg) == 5


# ------------------------------------------------------------ 7. 部分失败聚合
async def test_case07_partial_failure_identical_to_serial():
    def behavior(item):
        return {"success": item["id"] != "i3"}

    clock, rec = FakeClock(), Recorder(behavior=behavior)
    on = await _run(_plan(5), rec, clock)
    clock2, rec2 = FakeClock(), Recorder(behavior=behavior)
    off = await _run(_plan(5), rec2, clock2, enabled=False)
    assert [r for r in on[0]["results"]] == [r for r in off[0]["results"]]
    assert on[0]["warmup"]["success"] == off[0]["warmup"]["success"]


# ------------------------------------------------------------- 8/9. 关闭等价
async def test_case08_disabled_is_verbatim_serial_and_no_wait():
    # 关闭时并发上限由调用方现状决定（stage_concurrency 返回 0 → 用原值）；
    # 这里显式给 1，证明「串行顺序 + 零等待 + 不接管」与现状逐字一致。
    clock, rec = FakeClock(), Recorder()
    out = await _run(_plan(5, concurrency=1), rec, clock, enabled=False)
    assert rec.calls == ["i0", "i1", "i2", "i3", "i4"]
    assert rec.max_inflight == 1
    assert clock.sleeps == []
    assert out[0]["warmup"]["applied"] is False
    assert out[0]["warmup"]["degraded_reason"] == "disabled"


async def test_case09_enabled_but_stage_concurrency_one_equals_disabled():
    clock, rec = FakeClock(), Recorder()
    out = await _run(_plan(5, concurrency=1), rec, clock, enabled=True)
    assert rec.calls == ["i0", "i1", "i2", "i3", "i4"]
    assert clock.sleeps == []
    assert out[0]["warmup"]["applied"] is False
    assert out[0]["warmup"]["degraded_reason"] == "no_gain"


# ---------------------------------------------------------------------- 10. 停止
async def test_case10_stop_prevents_remaining_calls():
    state = {"stop": False}
    clock, rec = FakeClock(), Recorder()

    async def runner(item):
        rec.calls.append(item["id"])
        state["stop"] = True
        return {"success": True}

    out = await w.run_grouped(
        [_plan(4)],
        runner,
        enabled=True,
        default_delay_ms=5000,
        stop_check=lambda: state["stop"],
        sleep_fn=clock.sleep,
        clock_fn=clock.clock,
    )
    assert rec.calls == ["i0"]
    assert all(r == w.SKIPPED for r in out[0]["results"][1:])


# -------------------------------------------------------------- 11/12/13. 自检
def test_case11_selfcheck_disabled_verbatim():
    report = w.config_selfcheck(_cfg(ai_warmup_enabled=False))
    assert report["status"] == "disabled"
    assert report["equivalent"] == "verbatim"


def test_case12_selfcheck_enabled_but_no_gain(caplog):
    report = w.config_selfcheck(_cfg(ai_warmup_enabled=True, ai_warmup_stage_concurrency=1))
    assert report["status"] == "enabled_but_no_gain"
    assert report["equivalent"] == "verbatim"
    with caplog.at_level("WARNING"):
        w.log_config_selfcheck(_cfg(ai_warmup_enabled=True, ai_warmup_stage_concurrency=1))
    assert any("等于未开启" in r.message for r in caplog.records)


def test_case13_selfcheck_clamps_to_max_concurrency():
    report = w.config_selfcheck(_cfg(ai_warmup_enabled=True, ai_warmup_stage_concurrency=99))
    assert report["clamped"] is True
    assert report["effective_concurrency"] == 5
    assert w.stage_concurrency("content", _cfg(ai_warmup_enabled=True, ai_warmup_stage_concurrency=99)) == 5


# ------------------------------------------------------- 14. 既有默认值未改动
def test_case14_existing_defaults_untouched():
    assert real_settings.bid_analysis_item_concurrency == 1
    assert real_settings.max_concurrency == 5
    assert real_settings.ai_warmup_enabled is False
    assert real_settings.ai_warmup_delay_ms == 5000
    assert real_settings.ai_warmup_stage_concurrency == 2
    assert real_settings.ai_warmup_fallback_serial is True


def test_stage_concurrency_returns_zero_when_disabled():
    assert w.stage_concurrency("content", _cfg(ai_warmup_enabled=False)) == 0
    assert w.warmup_stage_enabled("content", _cfg(ai_warmup_enabled=False)) is False


def test_stage_delay_override():
    cfg = _cfg(ai_warmup_enabled=True, ai_warmup_delay_ms=5000, ai_warmup_stage_delay_ms='{"content":2000}')
    assert w.stage_delay_ms("content", cfg) == 2000
    assert w.stage_delay_ms("facts", cfg) == 5000


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"success": True}, "success"),
        ({"success": True, "parse_failed": True}, "parse_failed"),
        ({"success": False, "error_class": "timeout"}, "timeout"),
        ({"success": False, "error_class": "upstream_5xx"}, "upstream_5xx"),
    ],
)
def test_classify_result(result, expected):
    assert w.classify_result(result) == expected
