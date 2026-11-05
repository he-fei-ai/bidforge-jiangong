"""AI 调用侧三项缺陷修复回归（2026-11-05）

覆盖：
A. 对冲策略
   - 自适应阈值：P90 / P50×1.5 / min-max 夹紧 / 样本不足回落 / 关闭自适应；
   - 对冲上限：同一逻辑调用最多 1 个副本，落败请求被取消；
   - 冷却期：命中配额冷却的候选不会被对冲拉起；
   - 429：尊重 Retry-After，且重复 429 不进入指数放大。
B. 推理模型输出预算
   - 推理模型识别（名称 / 实测吞噬）；
   - 截断重试预算一次性抬到 floor，而不是只翻倍。
C. 统计口径
   - 逻辑调用 vs 尝试计数拆分（降级链多尝试只算一次业务调用）；
   - 每日统计只统计 action='chat'（跳过行不再稀释成功率）；
   - 平均耗时只取成功真实调用。
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from app.routers import system as sys_mod
from app.routers.ai_config import usage as usage_mod
from app.services.ai import provider_factory as pf

CANDIDATES = [
    {"provider_name": "p1", "model": "m1", "config_id": "cfg1", "base_url": "u1"},
    {"provider_name": "p2", "model": "m1", "config_id": "cfg2", "base_url": "u2"},
    {"provider_name": "p3", "model": "m1", "config_id": "cfg3", "base_url": "u3"},
]


def _fresh_live(monkeypatch) -> dict:
    """隔离进程级 _ai_live（既有测试用增量断言，这里用全新字典更直观）。"""
    live = {
        "total": 0,
        "failed": 0,
        "in_flight": 0,
        "last_provider": "",
        "last_model": "",
        "last_ok": True,
        "last_duration": 0.0,
        "last_at": 0.0,
        "cancelled": 0,
        "fast_fail": 0,
        "calls": 0,
        "call_failed": 0,
        "call_cancelled": 0,
    }
    monkeypatch.setattr(pf, "_ai_live", live)
    return live


def _patch_pipeline(monkeypatch, attempt, candidates=None):
    cands = candidates or CANDIDATES

    async def _resolve(_ai_config, _scene):
        return {}

    async def _build(_cfg):
        return [dict(c) for c in cands]

    async def _gate(usable, _scene):
        return usable, None

    monkeypatch.setattr(pf, "_resolve_chat_config", _resolve)
    monkeypatch.setattr(pf, "_build_candidates", _build)
    monkeypatch.setattr(pf, "_gate_candidates", _gate)
    monkeypatch.setattr(pf, "_attempt_candidate", attempt)


def _patch_hedge(monkeypatch, *, delay: float, max_copies: int = 1, adaptive: bool = False):
    monkeypatch.setattr(pf.settings, "ai_hedge_enabled", True)
    monkeypatch.setattr(pf.settings, "ai_hedge_delay_seconds", delay)
    monkeypatch.setattr(pf.settings, "ai_hedge_adaptive_enabled", adaptive)
    monkeypatch.setattr(pf.settings, "ai_hedge_max_copies", max_copies)


def _record_attempt(live: dict, ok: bool) -> None:
    """模拟真实 _attempt_candidate 的尝试口径记账。"""
    with pf._ai_live_lock:
        live["total"] = int(live["total"]) + 1
        live["in_flight"] = int(live["in_flight"]) + 1
    pf._ai_live_record("p", "m", ok, 0.01)


# ---------------------------------------------------------------------------
# A. 自适应阈值
# ---------------------------------------------------------------------------
def test_adaptive_hedge_delay_uses_high_percentile_not_median():
    key = pf._reliability_key("p1", "m1", "cfg1", "u1")
    for v in (20, 22, 24, 25, 26, 27, 28, 30, 32, 35, 40, 45):
        pf._note_provider_latency(key, float(v))

    delay = pf._adaptive_hedge_delay(key, fixed_delay=45.0, min_samples=12)
    # P50≈26.5 / P90≈41.5 → max(P90, P50×1.5)=41.5，明显高于实测 P50(25.83)
    assert 40.0 <= delay <= 43.0, delay
    assert delay > 25.83

    # 样本不足 → 冷启动固定值（避免冷启动又回到低阈值）
    assert pf._adaptive_hedge_delay(key, fixed_delay=45.0, min_samples=20) == 45.0
    # 关闭自适应 → 固定值
    assert pf._adaptive_hedge_delay(key, fixed_delay=45.0, adaptive=False) == 45.0
    # 显式 0 → 立即竞速（保留旧语义）
    assert pf._adaptive_hedge_delay(key, fixed_delay=0.0) == 0.0


def test_adaptive_hedge_delay_clamped_to_min_and_max():
    key = pf._reliability_key("p1", "m1", "cfg1", "u1")
    pf.reset_provider_latency()
    for _ in range(12):
        pf._note_provider_latency(key, 10.0)
    # p90=10 / p50×1.5=15 → 下限 30s
    assert pf._adaptive_hedge_delay(key, fixed_delay=45.0) == 30.0

    pf.reset_provider_latency()
    for _ in range(12):
        pf._note_provider_latency(key, 300.0)
    # 长尾（实测 p90 181s）也不允许无限推迟 → 上限 90s
    assert pf._adaptive_hedge_delay(key, fixed_delay=45.0) == 90.0


# ---------------------------------------------------------------------------
# A. 对冲上限 + 落败取消
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hedge_limited_to_one_copy_and_loser_cancelled(monkeypatch):
    _fresh_live(monkeypatch)
    _patch_hedge(monkeypatch, delay=0.05, max_copies=1)
    started: list[str] = []
    cancelled: list[str] = []

    async def attempt(c, _messages, **_kwargs):
        started.append(c["config_id"])
        try:
            await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            cancelled.append(c["config_id"])
            raise
        return f"ok-{c['config_id']}", None

    _patch_pipeline(monkeypatch, attempt)

    text = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
    assert text == "ok-cfg1"
    # 旧实现会继续拉起 cfg3（候选链 3 → 2 个副本）；修复后只允许 1 个副本
    assert started == ["cfg1", "cfg2"], started
    assert cancelled == ["cfg2"], cancelled


@pytest.mark.asyncio
async def test_hedge_skips_candidate_in_quota_cooldown(monkeypatch):
    _fresh_live(monkeypatch)
    _patch_hedge(monkeypatch, delay=0.05, max_copies=1)
    # p2 刚命中 429 → 冷却期内不得再被拉起
    pf._note_quota_failure("p2", RuntimeError("HTTP 429: rate limited"))
    started: list[str] = []

    async def attempt(c, _messages, **_kwargs):
        started.append(c["config_id"])
        if c["config_id"] == "cfg1":
            await asyncio.sleep(0.35)
        return f"ok-{c['config_id']}", None

    _patch_pipeline(monkeypatch, attempt)

    text = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
    assert text == "ok-cfg3"
    assert "cfg2" not in started, "冷却期内的候选不得被对冲拉起"
    assert started == ["cfg1", "cfg3"], started


# ---------------------------------------------------------------------------
# A. 429 / Retry-After
# ---------------------------------------------------------------------------
def test_429_respects_retry_after_without_exponential_amplification():
    err = RuntimeError("HTTP 429: rate limited（Retry-After: 300s）")
    assert pf._extract_retry_after(err) == 300.0
    assert pf._quota_cooldown_seconds(err) == 300.0

    pf._note_quota_failure("p429", err)
    remain_1 = pf._quota_cool_until["p429"] - time.time()
    assert 295.0 <= remain_1 <= 301.0, remain_1

    # 重复 429：仍是 Retry-After 值（不翻倍成 600）
    pf._note_quota_failure("p429", err)
    remain_2 = pf._quota_cool_until["p429"] - time.time()
    assert 295.0 <= remain_2 <= 301.0, remain_2

    # 无 Retry-After 的 429：用默认冷却，不做指数放大
    plain = RuntimeError("HTTP 429: too many requests")
    assert pf._quota_cooldown_seconds(plain) == pf.QUOTA_FAIL_COOLDOWN

    # 响应头形态
    resp = SimpleNamespace(headers={"retry-after": "45"})
    assert pf._extract_retry_after(SimpleNamespace(response=resp)) == 45.0

    # 异常大的 Retry-After 被上限钳制
    assert pf._quota_cooldown_seconds(RuntimeError("HTTP 429（Retry-After: 9999s）")) == pf.QUOTA_COOLDOWN_MAX


# ---------------------------------------------------------------------------
# B. 推理模型输出预算
# ---------------------------------------------------------------------------
def test_reasoning_model_detection_and_retry_budget(monkeypatch):
    monkeypatch.setattr(pf.settings, "ai_reasoning_effort", "")
    assert pf._is_reasoning_candidate({"model": "deepseek-r1", "provider_name": "x"})
    assert pf._is_reasoning_candidate({"model": "o3-mini", "provider_name": "x"})
    assert not pf._is_reasoning_candidate({"model": "gpt-4o", "provider_name": "x"})

    # custom 这类任意名称：本会话发生过思考吞噬后也应预扩预算
    pf._note_thinking_suspect("custom")
    assert pf._is_reasoning_candidate({"model": "custom", "provider_name": "custom"})

    # 1536 → 8192（不是只翻倍到 3072）
    assert pf._reasoning_retry_tokens(1536, 16384, 8192) == 8192
    assert pf._reasoning_retry_tokens(8192, 16384, 8192) == 16384
    # 用户把上限压到 4096 时仍尊重上限
    assert pf._reasoning_retry_tokens(1536, 4096, 8192) == 4096


# ---------------------------------------------------------------------------
# C. 统计口径：逻辑调用 vs 尝试
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_logical_call_counted_once_across_fallback_attempts(monkeypatch):
    live = _fresh_live(monkeypatch)

    async def attempt(c, _messages, **_kwargs):
        if c["config_id"] == "cfg1":
            _record_attempt(live, False)
            return "", RuntimeError("boom")
        _record_attempt(live, True)
        return "ok", None

    _patch_pipeline(monkeypatch, attempt, candidates=CANDIDATES[:2])

    text = await pf.chat_with_fallback([{"role": "user", "content": "hi"}])
    assert text == "ok"
    # 业务调用只计一次；尝试层面是 2 次（1 失败 + 1 成功）
    assert live["calls"] == 1
    assert live["call_failed"] == 0
    assert live["total"] == 2
    assert live["failed"] == 1

    async def attempt_all_fail(c, _messages, **_kwargs):
        _record_attempt(live, False)
        return "", RuntimeError("boom")

    _patch_pipeline(monkeypatch, attempt_all_fail, candidates=CANDIDATES[:2])
    with pytest.raises(RuntimeError):
        await pf.chat_with_fallback([{"role": "user", "content": "hi"}])

    # 第二次业务调用最终失败：calls +1 / call_failed +1；尝试 +2 / 失败 +2
    assert live["calls"] == 2
    assert live["call_failed"] == 1
    assert live["total"] == 4
    assert live["failed"] == 3


@pytest.mark.asyncio
async def test_stats_daily_excludes_non_chat_rows_and_avg_is_success_only(db_conn, monkeypatch):
    """每日统计只算 action='chat'；活动面板平均耗时只取成功真实调用。"""
    await db_conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action, duration, success)"
        " VALUES ('e1', 'p1', 'm1', 'chat', 22.3, 1)"
    )
    await db_conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action, duration, success)"
        " VALUES ('e2', 'p1', 'm1', 'chat', 0.2, 0)"
    )
    await db_conn.execute(
        "INSERT INTO ai_audit_logs (id, provider_name, model, action, duration, success)"
        " VALUES ('e3', 'p1', 'm1', 'circuit_skipped', 0, 0)"
    )
    await db_conn.commit()

    stats = await usage_mod.ai_stats(days=1, db=db_conn)
    assert stats["summary"]["total"] == 2, stats["summary"]
    assert stats["summary"]["failed_count"] == 1, stats["summary"]
    assert stats["summary"]["avg_duration"] == pytest.approx(22.3, rel=1e-3)

    today = next(row for row in stats["daily"] if row["calls"] == 2)
    assert today["success_count"] == 1
    assert today["failed_count"] == 1, "跳过行不得计入今日失败"

    async def fake_read_conn():
        return db_conn

    async def fake_release(_conn):
        return None

    monkeypatch.setattr(sys_mod, "get_read_conn", fake_read_conn)
    monkeypatch.setattr(sys_mod, "release_read_conn", fake_release)
    data = await sys_mod.activity(limit=5)

    ai = data["ai"]
    assert ai["calls_today"] == 2
    assert ai["real_calls_today"] == 2
    assert ai["skipped_today"] == 1
    assert ai["failed_today"] == 1
    assert ai["avg_duration"] == pytest.approx(22.3, rel=1e-3)
    assert ai["avg_duration_all"] == pytest.approx((22.3 + 0.2) / 2, rel=1e-3)
