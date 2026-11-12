# -*- coding: utf-8 -*-
"""R220（2026-11-11）Token 用量埋点 + SAF-08 事实覆盖度商业化增强护栏。

承接 R219 报告 §7 登记两项：
① 访问/审计日志未落逐调用 ``prompt_tokens``/``completion_tokens`` ⇒ 真实
   Token 消耗基线无法建立（2658 行日志只采到 2 条 max_tokens 放大记录）；
② SAF-08 在 ``ctx.facts`` 为空时直接 ``return []``，与「有事实、核对通过」
   都表现为零条 finding ⇒ 用户把「根本没查」误读成「已核对无危大超阈值」。

商业化验收锚（本文件逐条锁死）：
- 单一事实源：用量归一 / total / hit_rate / 覆盖度各只有一个出口；
- 加法式：既有响应键与既有日志格式不减、不改；
- fail-soft：日志通道故障不得影响快照，埋点故障不得影响 AI 主流程；
- 可控与有界：开关关闭零开销、维度 key 有上限、快照为深拷贝；
- 可查询可追溯：结构化日志机器可解析、快照可 JSON 序列化、findings 带参与条数；
- 跨端一致：前端类型契约与三态渲染同步落地。
"""
from __future__ import annotations

import json
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
ROOT = pathlib.Path(__file__).resolve().parents[2]
FIVE_KEYS = {"prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "cache_hit_rate"}


@pytest.fixture(autouse=True)
def _isolate_snapshot():
    """进程内快照是全局态，逐例隔离，防跨用例串扰。"""
    from app.services.ai import usage_logging as U

    U.reset_usage_snapshot()
    yield
    U.reset_usage_snapshot()


def _facts_with_late_hazard() -> list[dict]:
    """30 条无危大关键词的长事实 + 末尾 1 条「基坑开挖深度 16m」（R219 同构）。"""
    return [{"name": "工程概况描述", "value": "现场情况说明" * 40} for _ in range(30)] + [
        {"name": "基坑开挖深度", "fact_key": "excavation_depth", "value": "16m", "value_unit": "m"}
    ]


# =====================================================================
# A 组 · Token 用量埋点
# =====================================================================
class TestUsageNormalization:
    def test_five_key_contract_and_basis(self):
        """归一出口结构恒定五键，total / hit_rate 与统计端点 SQL 同源。"""
        from app.services.ai import usage_logging as U

        f = U.normalize_usage_fields({"prompt_tokens": 1200, "completion_tokens": 800, "cached_tokens": 300})
        assert set(f) == FIVE_KEYS
        assert f["total_tokens"] == 2000
        assert f["cache_hit_rate"] == 25.0
        assert U.compute_total_tokens(1200, 800) == 2000
        assert U.compute_cache_hit_rate(1200, 300) == 25.0
        assert U.compute_cache_hit_rate(0, 300) is None, "分母为 0 不得伪造 0% 命中率"

    def test_degenerate_usage_never_raises(self):
        """脏 usage（None / 非 dict / 字符串 / 负数）一律归零，绝不抛异常。"""
        from app.services.ai import usage_logging as U

        for bad in (None, {}, "x", {"prompt_tokens": None}, {"prompt_tokens": "abc", "completion_tokens": -5}):
            f = U.normalize_usage_fields(bad)
            assert set(f) == FIVE_KEYS
            assert f["total_tokens"] >= 0
        assert U.compute_total_tokens(None, "abc") == 0

    def test_structured_log_is_machine_parseable_and_injection_safe(self):
        """key=value 单行、字段顺序固定、换行/空格/控制字符不得破坏解析。"""
        from app.services.ai import usage_logging as U

        f = U.normalize_usage_fields({"prompt_tokens": 1200, "completion_tokens": 800, "cached_tokens": 300})
        line = U.format_usage_log(
            f,
            provider="agnes\nINJECT=1",
            model="gpt x",
            scene="outline",
            duration_ms=1234,
            context="trace=abc123 pid=999\nEVIL=1",
        )
        assert line.startswith("ai_usage ")
        assert "\n" not in line and "\r" not in line
        pairs = dict(part.split("=", 1) for part in line.split()[1:])
        assert pairs["prompt_tokens"] == "1200"
        assert pairs["completion_tokens"] == "800"
        assert pairs["cached_tokens"] == "300"
        assert pairs["total_tokens"] == "2000"
        assert pairs["cache_hit_rate"] == "25.0"
        assert pairs["provider"].startswith("agnes")
        assert pairs["duration_ms"] == "1234"
        assert pairs["trace"] == "abc123"
        assert len(line.split()[1:5]) == 4, "数值字段顺序固定，采集侧可稳定解析"

    def test_log_never_contains_secrets_or_prompt_text(self):
        """商业化红线：埋点只带数值与 provider/model/scene，不落任何内容。"""
        from app.services.ai import usage_logging as U

        line = U.format_usage_log(
            U.normalize_usage_fields({"prompt_tokens": 1}), provider="agnes", model="m1", scene="s1"
        )
        assert "sk-" not in line
        assert "api_key" not in line
        assert line.count("=") <= 10


class TestUsageSnapshot:
    def test_aggregation_and_deep_copy(self):
        """逐调用累计到 totals / by_provider / by_scene，快照返回深拷贝。"""
        from app.services.ai import usage_logging as U

        U.log_usage_event(
            {"prompt_tokens": 1200, "completion_tokens": 800, "cached_tokens": 300},
            provider="agnes", scene="outline",
        )
        U.log_usage_event(
            {"prompt_tokens": 300, "completion_tokens": 100, "cached_tokens": 0},
            provider="sensetime", scene="content",
        )
        snap = U.get_usage_snapshot()
        assert snap["sample_count"] == 2
        assert snap["totals"]["total_tokens"] == 2400
        assert snap["totals_cache_hit_rate"] == 20.0
        assert snap["by_provider"]["agnes"]["calls"] == 1
        assert snap["by_scene"]["content"]["total_tokens"] == 400
        snap["totals"]["total_tokens"] = -1
        snap["by_provider"]["agnes"]["calls"] = 999
        again = U.get_usage_snapshot()
        assert again["totals"]["total_tokens"] == 2400
        assert again["by_provider"]["agnes"]["calls"] == 1

    def test_snapshot_is_json_serializable(self):
        """端点可查询：快照必须能直接进 JSON 响应，不可含锁/线程对象。"""
        from app.services.ai import usage_logging as U

        U.log_usage_event({"prompt_tokens": 10, "completion_tokens": 5}, provider="p", scene="s")
        assert json.loads(json.dumps(U.get_usage_snapshot(), ensure_ascii=False))["sample_count"] == 1

    def test_dimension_keys_are_bounded(self, monkeypatch):
        """长跑防内存膨胀：维度 key 超限归溢出桶，但调用次数不丢。"""
        from app.services.ai import usage_logging as U

        monkeypatch.setattr(U, "_max_keys", lambda: 2)
        for i in range(5):
            U.log_usage_event({"prompt_tokens": 1}, provider=f"p{i}", scene=f"s{i}")
        snap = U.get_usage_snapshot()
        assert snap["sample_count"] == 5
        assert len(snap["by_provider"]) <= 3
        assert U._OVERFLOW_KEY in snap["by_provider"]
        assert sum(v["calls"] for v in snap["by_provider"].values()) == 5
        assert snap["totals"]["prompt_tokens"] == 5, "溢出不得漏计总量"

    def test_disabled_switch_has_zero_overhead(self, monkeypatch):
        """开关关闭后日志与快照均不累计（零开销、可运维）。"""
        from app.services.ai import usage_logging as U

        monkeypatch.setattr(U, "usage_logging_enabled", lambda: False)
        U.log_usage_event({"prompt_tokens": 999}, provider="agnes", scene="outline")
        snap = U.get_usage_snapshot()
        assert snap["sample_count"] == 0
        assert snap["enabled"] is False
        assert snap["totals"]["total_tokens"] == 0

    def test_logger_failure_does_not_block_snapshot(self, monkeypatch):
        """fail-soft 双通道：结构化日志写失败仍要完成快照累计。"""
        from app.services.ai import usage_logging as U

        def _boom(*_a, **_k):
            raise RuntimeError("logger down")

        monkeypatch.setattr(U.logger, "info", _boom)
        U.log_usage_event({"prompt_tokens": 10, "completion_tokens": 5}, provider="agnes", scene="outline")
        assert U.get_usage_snapshot()["sample_count"] == 1

    def test_end_to_end_baseline_matches_per_call_sum(self):
        """贯通：逐次调用之和 == 快照总量（基线可复算）。"""
        from app.services.ai import usage_logging as U

        usages = [
            {"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 200},
            {"prompt_tokens": 300, "completion_tokens": 100},
            {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
        ]
        for i, u in enumerate(usages):
            U.log_usage_event(u, provider="agnes" if i % 2 == 0 else "sensetime", scene=f"s{i}")
        snap = U.get_usage_snapshot()
        assert snap["totals"]["total_tokens"] == sum(U.normalize_usage_fields(u)["total_tokens"] for u in usages)
        assert sum(v["calls"] for v in snap["by_provider"].values()) == len(usages)


class TestUsageSingleSourceOfTruth:
    def test_settings_expose_commercial_switches(self):
        """配置项可运维：默认开启 + 有界上限。"""
        from app.config import settings

        assert hasattr(settings, "ai_usage_log_enabled")
        assert hasattr(settings, "ai_usage_snapshot_max_keys")
        assert settings.ai_usage_log_enabled is True
        assert settings.ai_usage_snapshot_max_keys > 0

    def test_provider_factory_wires_success_path(self):
        """源码棘轮：埋点必须落在 extract_usage 之后的成功分支，不得漏接。"""
        src = (APP / "services/ai/provider_factory.py").read_text(encoding="utf-8")
        assert "from app.services.ai.usage_logging import log_usage_event as _log_ai_usage" in src
        assert "from app.utils.log_context import context_suffix as _context_suffix" in src
        i = src.index("u = extract_usage(provider)")
        window = src[i : i + 900]
        assert "_log_ai_usage(" in window
        assert "duration_ms=duration * 1000" in window
        assert "context=_context_suffix()" in window

    def test_cache_hit_rate_has_no_second_implementation(self):
        """源码棘轮：usage.py 两处命中率计算必须委托唯一出口，旧内联算式不得回流。"""
        src = (APP / "routers/ai_config/usage.py").read_text(encoding="utf-8")
        assert src.count("compute_cache_hit_rate(") == 2
        assert 'p["cache_hit_rate"] = compute_cache_hit_rate(' in src
        assert 'summary["cache_hit_rate"] = compute_cache_hit_rate(' in src
        assert 'p["cache_hit_rate"] = (' not in src
        assert 'summary["cache_hit_rate"] = (' not in src

    def test_prompt_metrics_endpoint_exposes_snapshot_additively(self):
        """端点加法式：today 与原有快照键不减，新增 ai_usage。"""
        src = (APP / "routers/prompts.py").read_text(encoding="utf-8")
        assert '_snap["today"] = _today_stats' in src
        assert '_snap["ai_usage"] = _get_usage_snapshot()' in src
        assert src.index('_snap["today"]') < src.index('_snap["ai_usage"]')


# =====================================================================
# B 组 · SAF-08 事实覆盖度
# =====================================================================
class TestFactCoverage:
    def test_two_states_and_participating_count(self):
        """未检查 / 已检查二态；脏数据不计入参与数；不臆造 partial。"""
        from app.services.preflight_engine import build_fact_coverage

        empty = build_fact_coverage([])
        assert empty["state"] == "unchecked"
        assert empty["participating"] == 0
        assert empty["reason"]
        assert empty["rules"] == ["SAF-08"]
        some = build_fact_coverage([{"name": "a"}, None, "bad", {"name": "b"}])
        assert some["state"] == "checked"
        assert some["participating"] == 2
        assert some["reason"] == ""
        assert "partial" not in (empty["state"], some["state"])

    def test_negative_control_zero_findings_is_ambiguous(self):
        """反证：无事实 / 有事实但阈值未命中，finding 完全一致 ⇒ 必须靠覆盖度区分。"""
        from app.services.preflight_engine import PreflightContext, build_fact_coverage, check_hazard_params

        not_checked = check_hazard_params(
            PreflightContext(scheme_name="某住宅楼施工组织设计", facts=[])
        )
        checked_no_issue = check_hazard_params(
            PreflightContext(
                scheme_name="某住宅楼施工组织设计",
                facts=[{"name": "建筑面积", "value": "5000平方米", "value_unit": "平方米"}],
            )
        )
        assert not_checked == [] and checked_no_issue == [], "反证前提：两侧都是零条 finding"
        assert not_checked == checked_no_issue
        assert build_fact_coverage([])["state"] != build_fact_coverage([{"name": "建筑面积"}])["state"]

    def test_finding_carries_participating_and_state(self):
        """逐条 finding 加法式回传参与事实条数，且 R219 的 SAF-08 修复不回退。"""
        from app.services.preflight_engine import PreflightContext, check_hazard_params

        facts = _facts_with_late_hazard()
        out = check_hazard_params(PreflightContext(scheme_name="某住宅楼施工组织设计", facts=facts))
        assert [f["rule_id"] for f in out] == ["SAF-08"]
        assert out[0]["facts_participating"] == len(facts)
        assert out[0]["facts_coverage_state"] == "checked"
        assert any("depth=16" in e for e in (out[0].get("evidence") or []))

    def test_both_total_check_exports_expose_coverage(self):
        """源码棘轮：/overview 与 /preflight 两个总检出口同口径，杜绝只改一处。"""
        src = (APP / "routers/compliance.py").read_text(encoding="utf-8")
        assert src.count('"fact_coverage": build_fact_coverage(ctx.facts)') == 2
        assert "build_fact_coverage," in src

    def test_existing_response_keys_untouched(self):
        """加法式验收：既有响应键一个都不能少。"""
        src = (APP / "routers/compliance.py").read_text(encoding="utf-8")
        for key in (
            '"partial_sources"', '"created_at"', '"sources"', '"findings"',
            '"total"', '"score"', '"verdict"', '"blocked"', '"rule_version"', '"dimensions"',
        ):
            assert key in src


# =====================================================================
# C 组 · 跨端契约贯通
# =====================================================================
class TestCrossEndContract:
    def test_frontend_type_and_render_parity(self):
        """前端类型契约 + 三态渲染必须同步落地（后端加法式字段才真正可见）。"""
        ts = (ROOT / "frontend/src/types/audit.ts").read_text(encoding="utf-8")
        assert "export interface FactCoverage" in ts
        assert "fact_coverage?: FactCoverage;" in ts
        assert "facts_participating?: number;" in ts
        assert 'facts_coverage_state?: "checked" | "unchecked";' in ts
        tsx = (ROOT / "frontend/src/components/review/ReadinessDashboard.tsx").read_text(encoding="utf-8")
        assert 'overview.fact_coverage?.state === "unchecked"' in tsx
        assert 'overview.fact_coverage?.state === "checked"' in tsx
        assert "参与核对事实" in tsx
        assert "不得据此判定方案非危大" in tsx
