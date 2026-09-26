# -*- coding: utf-8 -*-
"""正文生成进度增强（2026-09-15 / 2026-09-16 二次增强）回归测试。

覆盖本次增强的全部纯逻辑与协议契约：

  1. `_weighted_progress` / `_section_partial` —— 按阶段加权的平滑总进度
     （核心目标：消除「单章 AI 调用期间进度条长时间静止」）
  2. `_fmt_duration` —— 时长文案
  3. `_snapshot_stats` —— 运行统计快照（已耗时 / ETA / 平均单章耗时 / 累计字数 /
     进行中章节）；ETA 口径 = 章节阶段有效进度外推（2026-09-16 修正）
  3b. `_calibrate_stage_expect` —— 阶段耗时在线校准（EMA）与 `_monotonic_progress`
     进度不可回退护栏（2026-09-16 新增）
  4. `update_progress` —— DB 落库节流 + 终态强制落库 + 广播实时性
  5. `with_heartbeat` —— stats_provider 注入 ping 事件（长调用期间唯一实时通道）
  6. `_await_with_stats` —— AI 调用期间的周期性统计推送（异常 / 取消语义不变）

设计约束（改这里之前先读）：
  - 进度必须**单调不减**：阶段基准 base 是累积值，跨阶段不得回退；
  - 进度**不得超过** `_SECTION_PHASE_MAX`（章节阶段上限，其余留给一致性检查）；
  - 阶段耗时校准只在**阶段切换点**写入（否则同阶段内 expect 变化会让进度回退）；
  - 统计与进度只服务于展示，任何异常都**不得**影响生成主流程。
"""
import asyncio
import json
import time

import pytest

from app.routers.sse_handlers import (
    _SECTION_PHASE_MAX,
    _STAGE_EXPECT_MAX,
    _STAGE_EXPECT_MIN,
    _STAGE_FILL_MAX,
    _STAGE_MODEL,
    _await_with_stats,
    _calibrate_stage_expect,
    _fmt_duration,
    _monotonic_progress,
    _section_partial,
    _snapshot_stats,
    _weighted_progress,
)
from app.services.ai import task_registry as tr
from app.services.ai.sse_utils import with_heartbeat


# ---------------------------------------------------------------- 1. 加权进度

def test_progress_zero_when_no_total():
    assert _weighted_progress({"total": 0}) == 0.0


def test_progress_from_done_only():
    """10 章完成 2 章、无进行中 → 2/10 × 0.95。"""
    p = _weighted_progress({"total": 10, "done": 2, "running": {}})
    assert p == pytest.approx(0.19, abs=1e-6)


def test_stage_baseline_is_monotonic():
    """阶段基准必须单调递增（否则进度条会倒退）。"""
    now = time.monotonic()
    seq = [
        _weighted_progress({"total": 10, "done": 2,
                            "running": {"a": {"stage": s, "started_at": now}}})
        for s in ("context", "draft", "continue", "persist")
    ]
    assert seq == sorted(seq)
    assert len(set(seq)) == 4


def test_progress_advances_within_draft_stage():
    """耗时最长的 draft 阶段内，进度条也要持续前进（本次增强的核心诉求）。"""
    now = time.monotonic()
    p0 = _weighted_progress({"total": 10, "done": 2,
                             "running": {"a": {"stage": "draft", "started_at": now}}})
    p1 = _weighted_progress({"total": 10, "done": 2,
                             "running": {"a": {"stage": "draft", "started_at": now - 45}}})
    p2 = _weighted_progress({"total": 10, "done": 2,
                             "running": {"a": {"stage": "draft", "started_at": now - 900}}})
    assert p0 < p1 < p2, f"进度未前进: {p0} {p1} {p2}"


def test_draft_fill_capped_below_continue_baseline():
    """draft 阶段的时间填充必须封顶，否则会越过下一阶段基准造成跳变。"""
    now = time.monotonic()
    p_full = _weighted_progress({"total": 10, "done": 2,
                                 "running": {"a": {"stage": "draft", "started_at": now - 9999}}})
    p_cont = _weighted_progress({"total": 10, "done": 2,
                                 "running": {"a": {"stage": "continue", "started_at": now}}})
    assert p_full < p_cont
    expect = (2 + 0.05 + 0.55 * _STAGE_FILL_MAX) / 10 * _SECTION_PHASE_MAX
    assert p_full == pytest.approx(expect, abs=1e-4)


def test_progress_capped_at_section_phase_max():
    """章节全部完成 → 上限 0.95（其余区间留给全文一致性检查）。"""
    assert _weighted_progress({"total": 3, "done": 3, "running": {}}) == pytest.approx(
        _SECTION_PHASE_MAX, abs=1e-9)


def test_progress_never_exceeds_cap():
    """进行中章节数多于总数时也不得越界。"""
    now = time.monotonic()
    p = _weighted_progress({"total": 1, "done": 0,
                            "running": {str(i): {"stage": "persist", "started_at": now}
                                        for i in range(5)}})
    assert p <= _SECTION_PHASE_MAX


def test_stage_model_shape():
    """span 合计 1.0，且 base+span 逐阶段连续（无重叠 / 空洞）。"""
    assert sum(m["span"] for m in _STAGE_MODEL.values()) == pytest.approx(1.0, abs=1e-9)
    for a, b in (("context", "draft"), ("draft", "continue"), ("continue", "persist")):
        assert _STAGE_MODEL[a]["base"] + _STAGE_MODEL[a]["span"] == pytest.approx(
            _STAGE_MODEL[b]["base"], abs=1e-9)
    # persist 阶段的 base+span 必须是 1.0（本章完成）
    assert (_STAGE_MODEL["persist"]["base"] + _STAGE_MODEL["persist"]["span"]
            == pytest.approx(1.0, abs=1e-9))


# ---------------------------------------------------------------- 2. 时长文案

@pytest.mark.parametrize("ms,expect", [
    (None, "0秒"),
    (0, "0秒"),
    (45000, "45秒"),
    (150000, "2分30秒"),
    (180000, "3分钟"),
    (3660000, "1小时1分"),
])
def test_fmt_duration(ms, expect):
    assert _fmt_duration(ms) == expect


# ---------------------------------------------------------------- 3. 统计快照

def _make_prog(elapsed=200.0, section_elapsed=50.0):
    return {
        "started_at": time.monotonic() - elapsed,
        "total": 10, "done": 2, "failed": 1, "words": 3500,
        "running": {"a": {"title": "施工部署", "index": 3, "stage": "draft",
                          "stage_label": "AI 生成中",
                          "started_at": time.monotonic() - section_elapsed}},
        "concurrency": 3, "phase": "sections", "phase_label": "生成正文",
    }


def test_snapshot_basic_fields():
    s = _snapshot_stats(_make_prog())
    assert 199000 <= s["elapsed_ms"] <= 202000
    assert (s["done"], s["total"], s["failed"], s["words"]) == (2, 10, 1, 3500)
    assert s["concurrency"] == 3
    assert s["phase_label"] == "生成正文"


def test_snapshot_running_entries():
    s = _snapshot_stats(_make_prog())
    assert len(s["running"]) == 1
    r = s["running"][0]
    assert r["title"] == "施工部署"
    assert r["stage"] == "draft"
    assert r["stage_label"] == "AI 生成中"
    assert 49000 <= r["elapsed_ms"] <= 52000


def test_snapshot_eta_counts_inflight_partial_work():
    """ETA = 章节阶段累计耗时 / 有效进度 × 剩余量。

    有效进度 = 已处理章节数 + Σ进行中章节在本章内的完成比例（与进度条同源）。
    旧口径 = 任务总耗时 / 已处理章数 × 剩余章数：把"3 章正在跑、已跑 50s"
    当成零产出，开局 ETA 系统性高估。
    """
    prog = _make_prog(elapsed=200.0, section_elapsed=50.0)
    s = _snapshot_stats(prog)
    effective = 2 + _section_partial(prog["running"]["a"], time.monotonic())
    expect = int(200000 / effective * (10 - effective))
    assert s["eta_ms"] == pytest.approx(expect, rel=1e-3)
    # 进行中章节已投入的 50s 必须体现在分母里 → 明显低于旧口径的 800s
    assert s["eta_ms"] < 800000


def test_snapshot_eta_excludes_prepare_phase():
    """准备阶段（章节树加载 + 字数分配 AI，最长 180s）不计入 ETA。"""
    now = time.monotonic()
    prog = {
        "started_at": now - 300,           # 任务已跑 300s（其中准备阶段占 250s）
        "sections_started_at": now - 50,   # 章节生成阶段只跑了 50s
        "total": 10, "done": 1, "failed": 0, "words": 0,
        "running": {}, "concurrency": 3,
        "phase": "sections", "phase_label": "生成正文",
    }
    s = _snapshot_stats(prog)
    # 50s / 1 章 × 9 章 = 450s（旧口径会用 300s/1 章 × 9 = 2700s）
    assert 440000 <= s["eta_ms"] <= 460000


def test_snapshot_deducts_paused_ms():
    """✅ B1（2026-09-20）：elapsed_ms 必须扣除暂停时长（含进行中的暂停段）。

    协作式暂停不打断在飞 AI 调用，暂停期间 stats 仍推送；不扣除的话进度卡
    「已耗时」持续增长，与活动中心（get_task_elapsed_ms）口径相反。
    """
    prog = _make_prog(elapsed=200.0, section_elapsed=50.0)
    base = _snapshot_stats(prog)
    s = _snapshot_stats(prog, paused_ms=120_000.0)
    # 200s - 120s = 80s（±2s 时钟容差）
    assert 78000 <= s["elapsed_ms"] <= 82000
    assert s["elapsed_ms"] < base["elapsed_ms"]


def test_snapshot_paused_ms_deducted_from_eta_section_elapsed():
    """ETA 同步受益于暂停扣除：_make_prog 未记录 sections_started_at 时，
    ETA 回退口径 = 任务耗时（已扣暂停）→ 章节阶段口径同理（B1 注释）。"""
    prog = _make_prog(elapsed=200.0, section_elapsed=50.0)
    s = _snapshot_stats(prog, paused_ms=30_000.0)
    effective = 2 + _section_partial(prog["running"]["a"], time.monotonic())
    # 回退口径：章节阶段耗时 = elapsed_ms = 200s - 30s = 170s
    expect = int(170000 / effective * (10 - effective))
    assert s["eta_ms"] == pytest.approx(expect, rel=1e-3)


def test_snapshot_paused_ms_deducted_from_eta_with_section_start():
    """显式记录 sections_started_at 时，ETA 按章节阶段耗时扣除暂停。"""
    now = time.monotonic()
    prog = {
        "started_at": now - 300,
        "sections_started_at": now - 50,
        "total": 10, "done": 1, "failed": 0, "words": 0,
        "running": {}, "concurrency": 3,
        "phase": "sections", "phase_label": "生成正文",
    }
    s = _snapshot_stats(prog, paused_ms=10_000.0)
    # 章节阶段有效耗时 = 50s - 10s = 40s → 40s / 1 章 × 9 章 = 360s
    assert 350000 <= s["eta_ms"] <= 370000


def test_snapshot_paused_ms_clamped_non_negative():
    """脏输入防御：扣除后不得为负（paused_ms 大于总耗时按 0 计）。"""
    prog = _make_prog(elapsed=10.0, section_elapsed=1.0)
    s = _snapshot_stats(prog, paused_ms=999_999.0)
    assert s["elapsed_ms"] == 0


def test_snapshot_paused_ms_default_zero_backcompat():
    """缺省 paused_ms=0：既有调用点（单测 / 其他模块）行为不变。"""
    prog = _make_prog()
    assert _snapshot_stats(prog)["elapsed_ms"] == _snapshot_stats(prog, paused_ms=0)["elapsed_ms"]


def test_snapshot_eta_none_without_sample():
    assert _snapshot_stats({"total": 5, "done": 0, "running": {}})["eta_ms"] is None


def test_snapshot_eta_minimum_sample_guard():
    """首个统计点（耗时≈0）按 1s 下限估算，不得给出 0 或天量 ETA。"""
    now = time.monotonic()
    prog = {"started_at": now, "sections_started_at": now,
            "total": 4, "done": 0, "running": {}, "concurrency": 2}
    s = _snapshot_stats(prog)
    assert s["eta_ms"] is None, "无任何已完成章节且无进行中章节 → 不预估"


def test_snapshot_avg_section_ms():
    prog = _make_prog()
    assert _snapshot_stats(prog)["avg_section_ms"] is None
    prog["section_ms"] = [10.0, 20.0, 30.01]  # 秒（与 _prog["section_ms"] 口径一致）
    # 20.003s → 20003ms（代码保留毫秒精度，断言按容差比较）
    assert _snapshot_stats(prog)["avg_section_ms"] == pytest.approx(20000, abs=50)


def test_snapshot_progress_same_as_weighted():
    prog = _make_prog()
    s = _snapshot_stats(prog)
    assert s["progress"] == pytest.approx(_weighted_progress(prog), abs=1e-3)


def test_snapshot_never_raises_on_dirty_input():
    """统计只服务展示，脏数据必须返回空字典而不是抛异常。"""
    assert _snapshot_stats({"started_at": "bad", "total": 1}) == {}


# ---------------------------------------------- 3b. 阶段耗时校准 / 进度护栏

def test_calibrate_stage_expect_ema():
    """实测耗时按 EMA 融入预期耗时（0.7 旧 + 0.3 新）。"""
    prog: dict = {"stage_expect": {}}
    _calibrate_stage_expect(prog, "draft", 30.0)
    assert prog["stage_expect"]["draft"] == pytest.approx(0.7 * 90 + 0.3 * 30)
    # 第二次校准基于上一次结果，而不是出厂值
    prev = prog["stage_expect"]["draft"]
    _calibrate_stage_expect(prog, "draft", 130.0)
    assert prog["stage_expect"]["draft"] == pytest.approx(0.7 * prev + 0.3 * 130)


def test_calibrate_stage_expect_clamped():
    prog: dict = {"stage_expect": {}}
    _calibrate_stage_expect(prog, "draft", 100000.0)
    assert prog["stage_expect"]["draft"] == _STAGE_EXPECT_MAX
    prog2: dict = {"stage_expect": {"draft": 6.0}}
    _calibrate_stage_expect(prog2, "draft", 0.001)
    assert prog2["stage_expect"]["draft"] == _STAGE_EXPECT_MIN


def test_calibrate_stage_expect_ignores_bad_input():
    """空阶段 / 非正耗时 / 未知阶段一律 no-op（不得污染进度模型）。"""
    prog: dict = {"stage_expect": {}}
    _calibrate_stage_expect(prog, "", 10.0)
    _calibrate_stage_expect(prog, "draft", 0.0)
    _calibrate_stage_expect(prog, "draft", -5.0)
    _calibrate_stage_expect(prog, "unknown-stage", 10.0)
    assert prog["stage_expect"] == {}


def test_section_partial_uses_calibrated_expect():
    """校准后的预期耗时决定填充速率：预期越短，同一时刻填充越多。"""
    now = time.monotonic()
    rec = {"stage": "draft", "started_at": now - 45}
    base = _section_partial(rec, now, None)
    faster = _section_partial(rec, now, {"draft": 45.0})
    slower = _section_partial(rec, now, {"draft": 180.0})
    assert faster > base > slower
    # 无论怎么校准都不得越过该阶段上限（否则会越过下一阶段基准造成跳变）
    cap = _STAGE_MODEL["draft"]["base"] + _STAGE_MODEL["draft"]["span"]
    assert faster <= cap and base <= cap


def test_section_partial_survives_dirty_expect_override():
    """校准映射里的脏值不得抛异常（统计/进度异常会拖垮生成主流程）。"""
    now = time.monotonic()
    rec = {"stage": "draft", "started_at": now - 45}
    assert _section_partial(rec, now, {"draft": "abc"}) == pytest.approx(
        _section_partial(rec, now, None))


def test_progress_monotonic_under_stage_calibration():
    """模拟真实链路（切阶段时校准上一阶段耗时）：进度必须单调不减。"""
    now = time.monotonic()
    prog: dict = {"total": 10, "done": 2, "running": {}, "stage_expect": {}}
    rec = {"stage": "context", "started_at": now, "stage_started_at": now}
    prog["running"]["a"] = rec
    seq = []
    for stage, elapsed in (("context", 4.0), ("draft", 30.0),
                           ("continue", 15.0), ("persist", 3.0)):
        _calibrate_stage_expect(prog, rec["stage"], elapsed)   # 与 _on_stage 同一调用点
        rec["stage"], rec["stage_started_at"] = stage, now
        rec["started_at"] = now - elapsed
        seq.append(_weighted_progress(prog))
    assert seq == sorted(seq), f"进度回退: {seq}"


@pytest.mark.parametrize("prev,new,expect", [
    (0.0, 0.3, 0.3),
    (0.5, 0.2, 0.5),          # 新值更小 → 取历史最大值（不可回退）
    (0.5, 0.5, 0.5),
    (-1.0, -2.0, 0.0),        # 负数一律抬到 0
])
def test_monotonic_progress_guard(prev, new, expect):
    assert _monotonic_progress(prev, new) == pytest.approx(expect)


def test_monotonic_progress_dirty_input():
    assert _monotonic_progress("bad", None) == 0.0
    assert _monotonic_progress(0.5, "bad") == pytest.approx(0.5)


# ------------------------------------------------- 4. update_progress 节流

class _FakeConn:
    def __init__(self):
        self.writes = 0

    async def execute(self, *a, **k):
        self.writes += 1

    async def commit(self):
        pass


async def _run_throttle_scenario(monkeypatch):
    conn = _FakeConn()
    sent = []

    async def _fake_get_conn():
        return conn

    async def _fake_broadcast(tid, payload):
        sent.append(payload)

    monkeypatch.setattr(tr, "get_conn", _fake_get_conn)
    monkeypatch.setattr(tr, "broadcast", _fake_broadcast)

    tid = "t-throttle"
    tr._tasks[tid] = {"progress": 0.0, "stats": {}, "started_at": 0.0,
                      "_db_progress_at": 0.0, "_db_progress_val": -1.0}
    try:
        await tr.update_progress(tid, 0.10, "m1")          # 首次 → 落库
        n1 = conn.writes
        await tr.update_progress(tid, 0.101, "m2")         # 微增量 + 短间隔 → 跳过
        n2 = conn.writes
        await tr.update_progress(tid, 0.30, "m3")          # 增量够大 → 落库
        n3 = conn.writes
        await tr.update_progress(tid, 0.3001, "done", event="completed")  # 终态 → 强制
        n4 = conn.writes
    finally:
        tr._tasks.pop(tid, None)
    return n1, n2, n3, n4, len(sent)


async def test_update_progress_throttles_db_but_broadcasts_always(monkeypatch):
    n1, n2, n3, n4, broadcasts = await _run_throttle_scenario(monkeypatch)
    assert n1 == 1, "首次进度应落库"
    assert n2 == 1, "微增量 + 短间隔应被节流（不落库）"
    assert n3 == 2, "增量够大应恢复落库"
    assert n4 == 3, "终态事件必须强制落库"
    assert broadcasts == 4, "广播必须始终实时（节流只影响 DB 落库）"


async def test_update_progress_never_raises_on_db_failure(monkeypatch):
    """DB 落库失败不得中断生成主流程（内存态与广播仍继续）。"""
    sent = []

    async def _boom_get_conn():
        raise RuntimeError("db down")

    async def _fake_broadcast(tid, payload):
        sent.append(payload)

    monkeypatch.setattr(tr, "get_conn", _boom_get_conn)
    monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
    tid = "t-db-fail"
    tr._tasks[tid] = {"progress": 0.0, "stats": {}, "started_at": 0.0,
                      "_db_progress_at": 0.0, "_db_progress_val": -1.0}
    try:
        await tr.update_progress(tid, 0.5, "m")
    finally:
        tr._tasks.pop(tid, None)
    assert len(sent) == 1, "落库失败仍应广播"


# ---------------------------------------------------- 5. 心跳注入 ping

async def test_heartbeat_injects_ping_and_keeps_data():
    async def _gen():
        yield "data: {\"event\":\"x\"}\n\n"
        await asyncio.sleep(0.5)
        yield "data: {\"event\":\"y\"}\n\n"

    calls = {"n": 0}

    def _provider():
        calls["n"] += 1
        return {"elapsed_ms": 1234, "running": []}

    out = [item async for item in with_heartbeat(_gen(), interval=0.1,
                                                 stats_provider=_provider)]
    pings = [json.loads(o[6:]) for o in out
             if o.startswith("data: ") and '"ping"' in o]
    hbs = [o for o in out if o.startswith(": heartbeat")]
    data = [o for o in out if o.startswith("data: ") and '"ping"' not in o]

    assert calls["n"] >= 2
    assert len(pings) >= 2
    assert len(hbs) >= 2, "心跳 comment 必须保留（连接活性）"
    assert len(data) == 2, "业务数据不得丢失或错序"
    assert pings[0]["event"] == "ping"
    assert pings[0]["elapsed_ms"] == 1234


async def test_heartbeat_survives_broken_stats_provider():
    """stats_provider 抛异常不得影响心跳与业务流。"""
    async def _gen():
        yield "data: {}\n\n"
        await asyncio.sleep(0.35)

    def _boom():
        raise RuntimeError("provider 炸了")

    out = [o async for o in with_heartbeat(_gen(), interval=0.1,
                                           stats_provider=_boom)]
    assert any(o.startswith(": heartbeat") for o in out)
    assert any(o.startswith("data: {}") for o in out)


async def test_heartbeat_without_provider_is_backward_compatible():
    """不传 stats_provider 时行为与旧版一致（仅心跳 comment）。"""
    async def _gen():
        yield "data: {\"a\":1}\n\n"
        await asyncio.sleep(0.25)

    out = [o async for o in with_heartbeat(_gen(), interval=0.1)]
    assert not any('"ping"' in o for o in out)
    assert any(o.startswith(": heartbeat") for o in out)


# ------------------------------------------- 6. _await_with_stats 语义

async def test_await_with_stats_fast_coro_no_push():
    pushed = {"n": 0}

    async def _push():
        pushed["n"] += 1

    async def _fast():
        await asyncio.sleep(0.01)
        return "ok"

    r = await _await_with_stats(_fast(), _push, interval=0.2)
    assert r == "ok"
    assert pushed["n"] == 0, "interval 内完成不应推送统计"


async def test_await_with_stats_pushes_periodically():
    pushed = {"n": 0}

    async def _push():
        pushed["n"] += 1

    async def _slow():
        await asyncio.sleep(0.45)
        return "slow-ok"

    r = await _await_with_stats(_slow(), _push, interval=0.1)
    assert r == "slow-ok"
    assert pushed["n"] >= 3, f"慢协程期间应周期性推送，实际 {pushed['n']} 次"


async def test_await_with_stats_propagates_exception():
    """AI 调用异常必须原样传播（调用方依赖它做重试 / 降级）。"""
    async def _push():
        pass

    class _Boom(Exception):
        pass

    async def _boom():
        await asyncio.sleep(0.01)
        raise _Boom("boom")

    with pytest.raises(_Boom):
        await _await_with_stats(_boom(), _push, interval=0.1)


async def test_await_with_stats_cancels_inner_on_cancel():
    """外层取消 → 内部协程同步取消，不留悬挂任务。"""
    async def _push():
        pass

    cancelled = {"inner": False}

    async def _hang():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["inner"] = True
            raise

    t = asyncio.ensure_future(_await_with_stats(_hang(), _push, interval=0.05))
    await asyncio.sleep(0.12)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert cancelled["inner"], "内部协程未被取消，存在悬挂任务"
