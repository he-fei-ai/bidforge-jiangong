# -*- coding: utf-8 -*-
"""正文生成链路性能改造的不变量回归测试（配套《正文生成模块性能瓶颈分析与优化方案_20260917.md》）。

锁定以下优化的正确性，防止后续回归：
- P1-2 `max_tokens_for_budget`：输出上限与目标字数成正比且有下限保护；
- P0-2 `AdaptiveConcurrencyController`：**慢响应不再降并发**（反向棘轮修复）、
  429/高失败率才降、降幅受目标档位约束；
- P0-1 `_order_candidates`：死配置剔除、主配置低成功率后置、保底不空；
- P0-1 对冲请求：错峰启动、先成功者胜、慢候选被取消；全失败时抛错；
- P0-3 `_candidate_timeout`：主候选用完整预算、降级候选套用上限；
- P0-4 `http_pool`：同 (base_url, proxy) 复用同一 client 实例；
- P1-3 `build_inline_chart_plan` / `apply_inline_chart_plan`：纯计算与写库分离
  （计算阶段不触碰 DB），且沿用既有 DELETE→INSERT 事务契约。
"""
import asyncio
import json
import time
import uuid

import pytest

import app.services.ai.provider_factory as pf
from app.config import settings
from app.services.ai import http_pool
from app.services.ai.workflows_base import AdaptiveConcurrencyController
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET, MAX_TOKENS_FLOOR, max_tokens_for_budget,
)
from app.routers._chart_pipeline import (
    apply_inline_chart_plan, build_inline_chart_plan,
)

VALID_FLOWCHART = (
    'flowchart TD\n    A["开始"] --> B["施工"]\n'
    '    B --> C{"验收合格?"}\n    C --> D["结束"]'
)


# ---------------------------------------------------------------------------
# P1-2 输出 token 上限折算
# ---------------------------------------------------------------------------

class TestMaxTokensForBudget:

    def test_scales_with_budget(self):
        assert max_tokens_for_budget(4000) > max_tokens_for_budget(2000)
        assert max_tokens_for_budget(2000) > max_tokens_for_budget(1000)

    def test_floor_protects_short_sections(self):
        assert max_tokens_for_budget(10) == MAX_TOKENS_FLOOR
        assert max_tokens_for_budget(0) >= MAX_TOKENS_FLOOR
        assert max_tokens_for_budget(None) >= MAX_TOKENS_FLOOR

    def test_follows_char_gap_for_continuation(self):
        """续写按"剩余待补字数"折算：缺口小 → 上限小（但不低于下限）。"""
        big = max_tokens_for_budget(3000, chars=2000)
        small = max_tokens_for_budget(3000, chars=200)
        assert big > small >= MAX_TOKENS_FLOOR

    def test_default_budget_is_sane(self):
        # 默认预算 1500 字的输出上限应足以覆盖 1.2X（1800 字）而不至于失控
        v = max_tokens_for_budget(DEFAULT_WORD_BUDGET)
        assert MAX_TOKENS_FLOOR <= v <= 4096


# ---------------------------------------------------------------------------
# P0-2 自适应并发控制器（反向棘轮修复）
# ---------------------------------------------------------------------------

class TestAdaptiveConcurrencyNoRatchet:

    def _saturate(self, ctl, duration, status=200):
        for _ in range(ctl.WINDOW_SIZE):
            ctl.record(duration, status=status)
        ctl.adjust_concurrency()

    def test_slow_success_does_not_downgrade(self):
        """核心回归：响应慢但成功率高 → 并发**保持不变**（旧实现每次 -1 直到 1）。"""
        ctl = AdaptiveConcurrencyController(initial=4)
        for _ in range(10):
            self._saturate(ctl, duration=60.0, status=200)
        assert ctl.current == 4, "慢响应不得触发降并发（服务端慢≠本地过载）"

    def test_429_downgrades(self):
        ctl = AdaptiveConcurrencyController(initial=4)
        for _ in range(ctl.WINDOW_SIZE):
            ctl.record(5.0, status=429)
        ctl.adjust_concurrency()
        assert ctl.current == 3

    def test_high_failure_rate_downgrades_but_stops_at_floor(self):
        """降幅受目标档位约束：不会一路掉到 1。"""
        ctl = AdaptiveConcurrencyController(initial=4)
        for _ in range(10):
            for _ in range(ctl.WINDOW_SIZE):
                ctl.record(5.0, status=500)
            ctl.adjust_concurrency()
        floor = max(ctl.min_c, ctl.target - ctl.MAX_DOWNGRADE_FROM_TARGET)
        assert ctl.current == floor, "降并发必须被目标档位下限夹住"

    def test_fast_healthy_recovers_up_to_target(self):
        ctl = AdaptiveConcurrencyController(initial=2)
        ctl.set_concurrency(4)
        ctl.current = 2
        ctl._sem.set_value(2)
        for _ in range(10):
            self._saturate(ctl, duration=2.0, status=200)
        assert ctl.current == 4, "快且健康时应回升到目标档位"

    def test_never_exceeds_target(self):
        ctl = AdaptiveConcurrencyController(initial=3)
        for _ in range(20):
            self._saturate(ctl, duration=1.0, status=200)
        assert ctl.current == 3


# ---------------------------------------------------------------------------
# P0-1 候选排序：死配置剔除 + 主配置后置
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_reliability():
    pf._provider_reliability.clear()
    yield
    pf._provider_reliability.clear()


class TestOrderCandidates:

    def _cands(self):
        return [
            {"provider_name": "primary", "api_key": "k", "_is_primary": True},
            {"provider_name": "backup", "api_key": "k", "_is_primary": False},
        ]

    def test_dead_config_dropped(self, clean_reliability):
        pf._provider_reliability["primary"] = {"ok": 30, "fail": 0}
        pf._provider_reliability["backup"] = {"ok": 0, "fail": 20}   # 0% 成功率
        out = pf._order_candidates(self._cands())
        assert [c["provider_name"] for c in out] == ["primary"]

    def test_low_rate_primary_demoted_to_tail(self, clean_reliability):
        pf._provider_reliability["primary"] = {"ok": 4, "fail": 6}   # 40%
        pf._provider_reliability["backup"] = {"ok": 19, "fail": 1}
        out = pf._order_candidates(self._cands())
        assert [c["provider_name"] for c in out] == ["backup", "primary"]
        assert out[-1]["_is_primary"] is True

    def test_healthy_primary_stays_first(self, clean_reliability):
        pf._provider_reliability["primary"] = {"ok": 18, "fail": 2}  # 90%
        pf._provider_reliability["backup"] = {"ok": 10, "fail": 0}
        out = pf._order_candidates(self._cands())
        assert out[0]["provider_name"] == "primary"

    def test_insufficient_samples_never_judged(self, clean_reliability):
        """样本不足（<5）时不做任何判定（避免冷启动误杀）。"""
        pf._provider_reliability["primary"] = {"ok": 0, "fail": 3}
        out = pf._order_candidates(self._cands())
        assert [c["provider_name"] for c in out] == ["primary", "backup"]

    def test_all_dead_keeps_at_least_one(self, clean_reliability):
        pf._provider_reliability["primary"] = {"ok": 0, "fail": 20}
        pf._provider_reliability["backup"] = {"ok": 0, "fail": 20}
        out = pf._order_candidates(self._cands())
        assert out, "全部判定为死配置时必须保底保留候选，不能返回空"


# ---------------------------------------------------------------------------
# P0-3 每候选独立超时
# ---------------------------------------------------------------------------

class TestCandidateTimeout:

    def test_primary_uses_full_budget(self):
        c = {"_is_primary": True, "timeout": 60}
        assert pf._candidate_timeout(c, 300) == 300

    def test_fallback_capped(self):
        c = {"_is_primary": False, "timeout": 60}
        assert pf._candidate_timeout(c, 300) == settings.ai_fallback_attempt_timeout

    def test_explicit_short_timeout_wins(self):
        c = {"_is_primary": False, "timeout": 60}
        assert pf._candidate_timeout(c, 30) == 30

    def test_config_timeout_used_when_no_override(self):
        assert pf._candidate_timeout({"_is_primary": True, "timeout": 45}, None) == 45
        assert pf._candidate_timeout({"_is_primary": True}, None) == 60


# ---------------------------------------------------------------------------
# P0-1 对冲请求（hedged request）
# ---------------------------------------------------------------------------

class _FakeProvider:
    """可控时延/结果的假 Provider。"""

    def __init__(self, delay: float, result: str = "", exc: Exception | None = None):
        self.delay = delay
        self.result = result
        self.exc = exc
        self.model = "fake-model"
        self.last_usage = {}

    async def chat(self, messages, temperature=None, json_mode=False,
                   max_tokens=None, **_kw) -> str:
        await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def isolated_fallback(monkeypatch):
    """隔离 chat_with_fallback 的外部依赖：不读 DB、不写审计、无历史可靠性/熔断污染。"""
    async def _noop_flush():
        return None

    monkeypatch.setattr(pf, "_flush_audit_buffer", _noop_flush)
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()
    yield monkeypatch
    pf._audit_buffer.clear()
    pf._provider_reliability.clear()
    pf.circuit_breaker._per_provider.clear()


def _setup_candidates(monkeypatch, primary, fallbacks, *, hedge_delay=0.05,
                      hedge_enabled=True):
    monkeypatch.setattr(settings, "ai_hedge_enabled", hedge_enabled)
    monkeypatch.setattr(settings, "ai_hedge_delay_seconds", hedge_delay)

    async def _cfg():
        return {"provider_name": primary[0].get("name", "primary"),
                "api_key": "k", "base_url": "https://x", "model": "m",
                "max_tokens": 4096, "temperature": 0.7, "timeout": 60}

    async def _chain():
        return [{"provider_name": p.get("name", f"fb{i}"), "api_key": "k",
                 "base_url": "https://x", "model": "m", "max_tokens": 4096,
                 "temperature": 0.7, "timeout": 60}
                for i, p in enumerate(fallbacks)]

    monkeypatch.setattr(pf, "_load_active_config", _cfg)
    monkeypatch.setattr(pf, "_fallback_chain", _chain)
    # 主配置的 key 解析不读 DB（否则 api_key 为空会被当"无 Key"跳过）
    monkeypatch.setattr(pf, "_primary_api_key", lambda cfg: "k")

    by_name = {}
    for item in [*primary, *fallbacks]:
        by_name[item.get("name", "")] = item
    monkeypatch.setattr(pf, "_build_provider",
                        lambda pname, *a, **kw: by_name[pname]["provider"])
    return by_name


def test_hedge_slow_primary_loses_to_fast_fallback(isolated_fallback):
    """主候选慢 → 对冲启动备选，先成功者胜（并把慢候选取消）。"""
    monkeypatch = isolated_fallback
    _setup_candidates(
        monkeypatch,
        primary=[{"name": "slow", "provider": _FakeProvider(1.5, "SLOW-RESULT")}],
        fallbacks=[{"name": "fast", "provider": _FakeProvider(0.02, "FAST-RESULT")}],
        hedge_delay=0.05)
    t0 = time.monotonic()
    out = asyncio.run(pf.chat_with_fallback([{"role": "user", "content": "hi"}]))
    elapsed = time.monotonic() - t0
    assert out == "FAST-RESULT"
    assert elapsed < 0.8, "对冲应在慢候选完成前返回"


def test_no_hedge_when_disabled(isolated_fallback):
    """关闭对冲 → 严格按候选顺序串行（慢候选先返回即胜）。"""
    monkeypatch = isolated_fallback
    _setup_candidates(
        monkeypatch,
        primary=[{"name": "slow", "provider": _FakeProvider(0.05, "SLOW-RESULT")}],
        fallbacks=[{"name": "fast", "provider": _FakeProvider(0.0, "FAST-RESULT")}],
        hedge_enabled=False)
    out = asyncio.run(pf.chat_with_fallback([{"role": "user", "content": "hi"}]))
    assert out == "SLOW-RESULT"


def test_single_candidate_no_hedge_path(isolated_fallback):
    monkeypatch = isolated_fallback
    _setup_candidates(
        monkeypatch,
        primary=[{"name": "only", "provider": _FakeProvider(0.0, "ONLY")}],
        fallbacks=[])
    out = asyncio.run(pf.chat_with_fallback([{"role": "user", "content": "hi"}]))
    assert out == "ONLY"


def test_all_candidates_fail_raises(isolated_fallback):
    monkeypatch = isolated_fallback
    _setup_candidates(
        monkeypatch,
        primary=[{"name": "p", "provider": _FakeProvider(0.0, exc=RuntimeError("boom-p"))}],
        fallbacks=[{"name": "f", "provider": _FakeProvider(0.0, exc=RuntimeError("boom-f"))}],
        hedge_delay=0.05)
    with pytest.raises(RuntimeError, match="所有 AI 提供商调用失败"):
        asyncio.run(pf.chat_with_fallback([{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# P0-4 共享 HTTP 连接池
# ---------------------------------------------------------------------------

class TestHttpPoolReuse:

    @pytest.mark.asyncio
    async def test_same_key_reuses_client(self):
        http_pool.reset_clients_for_test()
        try:
            c1 = http_pool.get_async_client("https://api.example.com/v1")
            c2 = http_pool.get_async_client("https://api.example.com/v1/")
            assert c1 is c2, "同 (base_url, proxy) 必须复用同一 client"
        finally:
            await http_pool.aclose_all_clients()
            http_pool.reset_clients_for_test()

    @pytest.mark.asyncio
    async def test_proxy_isolates_pool(self):
        http_pool.reset_clients_for_test()
        try:
            c1 = http_pool.get_async_client("https://api.example.com/v1")
            c2 = http_pool.get_async_client("https://api.example.com/v1",
                                           "http://127.0.0.1:7890")
            assert c1 is not c2, "不同代理必须使用不同连接池"
        finally:
            await http_pool.aclose_all_clients()
            http_pool.reset_clients_for_test()


# ---------------------------------------------------------------------------
# P1-3 图表计划/写入分离（计算不触碰 DB）
# ---------------------------------------------------------------------------

class _RecordingDb:
    def __init__(self):
        self.rows = []

    async def execute(self, sql, params=None):
        self.rows.append((sql, tuple(params or ())))
        return None


class TestInlineChartPlanSplit:

    def test_plan_is_pure_and_returns_rows(self):
        """build_inline_chart_plan 不接收 db —— 结构上保证计算不触碰数据库。"""
        content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
        new_content, rows = build_inline_chart_plan(
            "scheme-1", "sec-1", content, enforce_limits=True,
            scheme_type_counts={})
        assert new_content == content
        assert len(rows) == 1
        # rows 为 INSERT 参数元组：8 项（含 1 个字面量列 needed=1）
        assert len(rows[0]) == 8
        assert rows[0][1] == "sec-1" and rows[0][3] == "flowchart"
        # ✅ B1 回归：正文同步生成的图表完成态统一为 "generated"
        assert rows[0][6] == "generated"
        payload = json.loads(rows[0][7])
        assert payload["mermaid_code"] == VALID_FLOWCHART

    def test_plan_no_charts_returns_empty_rows(self):
        new_content, rows = build_inline_chart_plan(
            "scheme-1", "sec-2", "纯文本，无图表。")
        assert rows == []
        assert new_content == "纯文本，无图表。"

    def test_apply_writes_delete_then_inserts(self):
        db = _RecordingDb()
        content = "```mermaid\n" + VALID_FLOWCHART + "\n```"
        _new, rows = build_inline_chart_plan("scheme-1", "sec-3", content)
        asyncio.run(apply_inline_chart_plan(db, "sec-3", rows))
        assert db.rows[0][0] == "DELETE FROM chart_predictions WHERE section_id=?"
        assert db.rows[0][1] == ("sec-3",)
        inserts = [r for r in db.rows if r[0].startswith("INSERT")]
        assert len(inserts) == 1

    def test_apply_deletes_even_without_rows(self):
        """无图表也必须清理历史登记（防僵尸图）。"""
        db = _RecordingDb()
        asyncio.run(apply_inline_chart_plan(db, "sec-4", []))
        assert db.rows == [
            ("DELETE FROM chart_predictions WHERE section_id=?", ("sec-4",))]


async def test_apply_enforces_scheme_wide_type_limit(tmp_path):
    """B2 回归：写锁内重查全方案同类型已登记数，超额的图必须被跳过登记。

    模拟「先写满 3 个 gantt（默认上限），再为另一章节申请 3 个 gantt」——
    第二次 apply 在事务内复核到 live=3，应全部超额跳过，最终不超发。
    """
    import app.db as _appdb
    from app.db import get_conn, init_db
    _appdb.DB_PATH = tmp_path / "chart-limit.sqlite"
    await init_db()
    db = await get_conn()
    sid = "scheme-limit"
    await db.execute(
        "INSERT INTO projects(id,name) VALUES(?,?)", ("p", "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, "p", "s"))
    await db.commit()
    rows_a = [(str(uuid.uuid4()), "secA", sid, "gantt", "t", 5, "generated", "{}")
              for _ in range(3)]
    await apply_inline_chart_plan(db, "secA", rows_a)
    rows_b = [(str(uuid.uuid4()), "secB", sid, "gantt", "t", 5, "generated", "{}")
              for _ in range(3)]
    await apply_inline_chart_plan(db, "secB", rows_b)
    cur = await db.execute(
        "SELECT count(*) FROM chart_predictions WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())[0] == 3  # 不超过全方案同类型上限
    await db.close()

    def test_limits_trim_extra_block_in_plan(self):
        """per-chapter 上限在纯计算阶段生效（第 2 块被裁出正文）。"""
        second = "flowchart LR\n    C --> D"
        content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n正文\n"
                   "```mermaid\n" + second + "\n```")
        new_content, rows = build_inline_chart_plan(
            "scheme-1", "sec-5", content, enforce_limits=True,
            scheme_type_counts={})
        assert second not in new_content
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 链路常量回归锁
# ---------------------------------------------------------------------------

def test_continue_rounds_capped():
    """P1-1：续写轮数上限收敛为 2（旧实现 4 轮 × 每轮重试 → 单章最多 9 次调用）。"""
    from app.routers.sse_handlers import CONTENT_CONTINUE_MAX_ROUNDS
    assert CONTENT_CONTINUE_MAX_ROUNDS == 2


def test_fallback_chain_capped_by_setting():
    """P0-3：降级链上限来自配置（默认 3）。"""
    assert settings.ai_fallback_chain_max == 3
