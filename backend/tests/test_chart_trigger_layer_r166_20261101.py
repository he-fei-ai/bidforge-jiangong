# -*- coding: utf-8 -*-
"""R166 · 触发词库 / 本地模板兜底 / 来源标记 护栏（2026-11-01）。

引入来源：外部「招投标方案平台」图表生成机制（词库 + 句式信号 + 行业术语 +
置信度元数据 + 本地兜底 + source 标记），按本仓「正文一体配图」架构重构为
**纯函数判定层**（不做独立编排器、不做预编排、不硬阻断）。

本护栏锁定四件事：
1. 提示词「触发关键词映射表」与程序词库**同源**（单一口径，不可能漂移）；
2. 判定语义（gantt 标题→gantt、管理章节排除、字数不足排除、置信度阈值）；
3. 7 类本地模板**放行 ⇔ 必出图**（全部通过管线同一批校验器）；
4. 来源标记 ``source``：``ai`` 逐字节零回归、``local`` 才写入键，且管线透传。
"""
from __future__ import annotations

import json

import pytest
from app.routers._chart_pipeline import build_inline_chart_plan
from app.services.chart_local_templates import (
    LOCAL_TEMPLATE_DISCLAIMER,
    SUPPORTED_LOCAL_TYPES,
    build_local_chart_fence,
    build_local_chart_payload,
    maybe_append_local_chart,
)
from app.services.chart_payload import build_chart_envelope, extract_chart_payload
from app.services.chart_triggers import (
    CHART_SENTENCE_SIGNALS,
    CHART_TRIGGER_LEXICON,
    CHART_TRIGGER_PRIORITY,
    SCHEME_DOMAIN_TERMS,
    analyze_section_chart_need,
    build_section_chart_hint,
    build_trigger_table_markdown,
)
from app.services.chart_validators import (
    validate_architecture_tree,
    validate_comparison_data,
    validate_flowchart_data,
    validate_gantt_plan,
    validate_labor_data,
    validate_layout_data,
    validate_timeline_data,
)

_VALIDATORS = {
    "flowchart": validate_flowchart_data,
    "gantt": validate_gantt_plan,
    "architecture": validate_architecture_tree,
    "labor": validate_labor_data,
    "comparison": validate_comparison_data,
    "layout": validate_layout_data,
    "timeline": validate_timeline_data,
}


# ===========================================================================
# 1. 提示词表格与程序词库同源（单一口径）
# ===========================================================================


class TestPromptTableParity:
    def test_default_prompt_contains_generated_table(self):
        """出厂提示词的映射表必须**逐字**由 build_trigger_table_markdown 生成。"""
        from app.services.ai.prompts._registry import get_default_prompt

        prompt = get_default_prompt("content_generation_system")
        assert build_trigger_table_markdown() in prompt, (
            "提示词映射表与程序词库已分叉（应统一由 chart_triggers 生成）")

    def test_every_type_row_carries_its_lexicon(self):
        from app.services.ai.prompts._registry import get_default_prompt

        prompt = get_default_prompt("content_generation_system")
        for ct in CHART_TRIGGER_PRIORITY:
            rows = [ln for ln in prompt.splitlines()
                    if ln.strip().startswith(f"| {ct} |")]
            assert len(rows) == 1, f"{ct} 映射表行应恰 1 行，实得 {len(rows)}"
            for kw in CHART_TRIGGER_LEXICON[ct]:
                assert kw in rows[0], f"词库词「{kw}」未进 {ct} 行"

    def test_semantic_isolation_words_stay_in_own_row(self):
        """跨类型易混词必须只落在语义归属行（防止提示词误导判型）。"""
        from app.services.ai.prompts._registry import get_default_prompt

        prompt = get_default_prompt("content_generation_system")
        rows = {ct: next(ln for ln in prompt.splitlines()
                         if ln.strip().startswith(f"| {ct} |"))
                for ct in CHART_TRIGGER_PRIORITY}
        assert "应急响应" in rows["flowchart"] and "应急响应" not in rows["architecture"]
        assert "应急组织" in rows["architecture"] and "应急组织" not in rows["flowchart"]
        assert "机械设备" in rows["comparison"] and "机械设备" not in rows["labor"]
        assert "物资需求" in rows["comparison"] and "物资需求" not in rows["labor"]

    def test_priority_covers_all_seven_types(self):
        assert set(CHART_TRIGGER_PRIORITY) == set(SUPPORTED_LOCAL_TYPES)
        assert set(CHART_TRIGGER_LEXICON) == set(CHART_TRIGGER_PRIORITY)


# ===========================================================================
# 2. 判定语义（纯函数）
# ===========================================================================


class TestAnalyzeSectionChartNeed:
    def test_gantt_title_judged_as_gantt(self):
        res = analyze_section_chart_need(title="施工总进度计划")
        assert res["needed"] is True
        assert res["chart_type"] == "gantt"
        assert res["confidence"] >= 0.70
        assert res["source"] == "local"

    def test_management_section_excluded(self):
        """背景/管理类章节即使标题含图表词也必须排除（与提示词排除场景同口径）。"""
        res = analyze_section_chart_need(title="项目概况与编制依据")
        assert res["excluded"] is True
        assert res["needed"] is False
        assert "项目概况" in res["excluded_by"]

    def test_short_section_excluded(self):
        res = analyze_section_chart_need(
            title="施工总进度计划", content="文字" * 50, word_count=100)
        assert res["needed"] is False
        assert "< 200" in res["reason"]

    def test_low_confidence_not_needed(self):
        """只有正文弱命中时不得判定 needed（避免程序侧越界替 AI 决策）。"""
        res = analyze_section_chart_need(
            title="本章说明", content="施工进度相关的一般性描述。",
            word_count=500, min_confidence=0.95)
        assert res["needed"] is False

    def test_used_counts_skips_exhausted_type(self):
        res = analyze_section_chart_need(
            title="施工总进度计划", used_counts={"gantt": 3})
        assert res["needed"] is False

    def test_hint_keeps_advisory_wording(self):
        """软提示必须明确「仅供参考、最终由 AI 判定」（R58 不做预编排）。"""
        hint = build_section_chart_hint(title="施工总进度计划")
        assert hint, "命中词库时应产出软提示"
        assert "仅供参考" in hint
        assert "自主判定" in hint

    def test_hint_empty_when_no_hit(self):
        assert build_section_chart_hint(title="项目概况") == ""
        assert build_section_chart_hint(title="无关章节") == ""

    def test_hint_respects_exhausted_quota(self):
        """加固：某类型已达全案上限时，软提示不得再推荐它（否则建议画了又被删）。"""
        assert build_section_chart_hint(
            title="施工总进度计划", used_counts={"gantt": 3}) == ""
        hint = build_section_chart_hint(
            title="施工总进度计划", used_counts={"gantt": 1})
        assert hint and "甘特图" in hint

    def test_sentence_signals_all_types_known(self):
        for _pattern, ct in CHART_SENTENCE_SIGNALS:
            assert ct in CHART_TRIGGER_PRIORITY

    def test_domain_terms_all_types_known(self):
        for terms in SCHEME_DOMAIN_TERMS.values():
            for ct in terms:
                assert ct in CHART_TRIGGER_PRIORITY


# ===========================================================================
# 3. 本地模板兜底：7 类全部「放行 ⇔ 必出图」
# ===========================================================================


class TestLocalTemplates:
    @pytest.mark.parametrize("chart_type", sorted(SUPPORTED_LOCAL_TYPES))
    def test_payload_passes_same_validator_as_pipeline(self, chart_type):
        payload = build_local_chart_payload(chart_type)
        assert payload is not None
        assert payload["source"] == "local"
        ok, errs = _VALIDATORS[chart_type](payload)
        assert ok and errs == [], f"{chart_type} 模板未过校验器: {errs}"

    @pytest.mark.parametrize("chart_type", sorted(SUPPORTED_LOCAL_TYPES))
    def test_fence_is_chart_json_with_disclaimer(self, chart_type):
        fence, payload = build_local_chart_fence(chart_type)
        assert fence.startswith("\n\n")
        assert "```chart-json" in fence and fence.rstrip().endswith("```")
        assert LOCAL_TEMPLATE_DISCLAIMER in fence
        assert json.loads(fence.split("```chart-json")[1].split("```")[0]) == payload

    def test_unsupported_type_returns_none(self):
        assert build_local_chart_payload("ai_image") is None
        assert build_local_chart_fence("ai_image") == ("", None)

    def test_append_only_when_no_fence(self):
        body = "施工总进度计划相关说明。" * 20
        content, meta = maybe_append_local_chart(
            body, section_title="施工总进度计划", word_count=400)
        assert meta and meta["chart_type"] == "gantt"
        assert "```chart-json" in content and LOCAL_TEMPLATE_DISCLAIMER in content

        with_fence = body + "\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
        content2, meta2 = maybe_append_local_chart(
            with_fence, section_title="施工总进度计划", word_count=400)
        assert meta2 is None and content2 == with_fence

    def test_no_append_for_excluded_section(self):
        content, meta = maybe_append_local_chart(
            "项目概况说明。" * 30, section_title="项目概况", word_count=400)
        assert meta is None and "```chart-json" not in content


# ===========================================================================
# 4. source 标记：ai 零回归 / local 写键 / 管线透传
# ===========================================================================

class TestSourceMarker:
    def test_ai_envelope_is_byte_identical_to_legacy(self):
        """默认 source="ai" 时信封必须与本项引入前逐字节一致（无 source 键）。"""
        data = {"type": "comparison", "items": [{"label": "A", "value": 1}]}
        legacy = build_chart_envelope(data=data, title="正文同步生成")
        with_src = build_chart_envelope(data=data, title="正文同步生成", source="ai")
        assert legacy == with_src
        assert "source" not in json.loads(legacy)

    def test_local_envelope_carries_source_key(self):
        data = build_local_chart_payload("comparison")
        raw = build_chart_envelope(data=data, title="正文同步生成", source="local")
        assert json.loads(raw)["source"] == "local"
        assert json.loads(extract_chart_payload(raw))["source"] == "local"

    def test_pipeline_passes_through_local_source(self):
        body = "施工总进度计划相关说明。" * 40
        new_content, rows = build_inline_chart_plan(
            "S", "SEC", body, enforce_limits=False,
            enable_local_fallback=True, local_fallback_confidence=0.70,
            section_title="施工总进度计划")
        assert len(rows) == 1, "兜底图应被登记（放行 ⇔ 必出图）"
        _id, _sid, _pid, ct, _title, _prio, _status, payload = rows[0]
        assert ct == "gantt"
        assert json.loads(payload)["source"] == "local"
        assert "```chart-json" in new_content

    def test_pipeline_fallback_disabled_is_zero_regression(self):
        body = "施工总进度计划相关说明。" * 40
        new_content, rows = build_inline_chart_plan(
            "S", "SEC", body, enforce_limits=False,
            section_title="施工总进度计划")
        assert rows == []
        assert new_content == body

    def test_pipeline_does_not_double_add_when_ai_already_drew(self):
        body = ("施工总进度计划相关说明。" * 40
                + "\n\n进度计划如下：\n\n```mermaid\nflowchart TD\n"
                '  A["土方开挖"] --> B["主体结构"]\n'
                '  B --> C["竣工验收"]\n```\n')
        new_content, rows = build_inline_chart_plan(
            "S", "SEC", body, enforce_limits=False,
            enable_local_fallback=True, section_title="施工总进度计划")
        assert len(rows) == 1
        assert json.loads(rows[0][7]).get("source") != "local", (
            "AI 已配图时不得再追加兜底图")


# ===========================================================================
# 5. 接线 / 默认值护栏（配置开关、SSE 提示协同、清单来源读口）
# ===========================================================================


class TestWiringDefaults:
    def test_config_defaults_locked(self):
        """默认值锁定：提示开、兜底关、阈值 0.70 —— 任一项漂移都会改变线上行为。"""
        from app.config import Settings

        st = Settings()
        assert st.content_chart_trigger_hint_enabled is True
        assert st.chart_local_fallback_enabled is False
        assert st.chart_local_fallback_min_confidence == 0.70

    def test_pipeline_fallback_defaults_off(self):
        """默认参数必须保持「关闭」，否则非 SSE 调用方也会悄悄开始追加兜底图。"""
        import inspect

        sig = inspect.signature(build_inline_chart_plan)
        assert sig.parameters["enable_local_fallback"].default is False
        assert sig.parameters["local_fallback_confidence"].default == 0.70

    def test_sse_hint_reuses_quota_counts(self):
        """加固：两处软提示共用「一次查库」的唯一实现点，且各自受独立开关门控。"""
        import inspect

        from app.routers import sse_handlers

        src = inspect.getsource(sse_handlers)
        assert "build_section_chart_hints(" in src, (
            "SSE 未走统一装配函数 → 两处提示可能各自查库/拿不到同一份计数")
        assert "quota_hint_enabled=" in src, "配额提示缺少开关门控"
        assert "trigger_hint_enabled=" in src, "配图参考缺少独立开关门控"
        assert "used_counts=_chart_counts" not in src, (
            "旧实现残留：触发词提示在自己拼装，未复用统一计数")

    def test_charts_list_source_reader(self):
        """清单来源读口：local 才返回 local，AI/历史载荷一律 ai。"""
        from app.routers.charts import _payload_source

        local_raw = build_chart_envelope(
            data=build_local_chart_payload("comparison"), source="local")
        assert _payload_source(local_raw) == "local"
        assert _payload_source(build_chart_envelope(
            data={"type": "comparison", "items": [{"label": "A", "value": 1}]})) == "ai"
        assert _payload_source("flowchart TD\n  A --> B") == "ai"
        assert _payload_source(None) == "ai"


# ===========================================================================
# 6. 配额统计逻辑单元测试（R166 修复的真实缺陷）
#
#    缺陷：配额提示自己查一次库，触发词提示拿不到计数 → 某类型已达全案上限时
#    词库仍推荐该类型，AI 照提示画图必被限额删除。修复：一次查库、两处共用，
#    实现收敛到 _chart_pipeline.build_section_chart_hints（本节直接断言该契约）。
# ===========================================================================


class TestQuotaCountsSharedOnce:
    @staticmethod
    def _patch_loader(monkeypatch, result=None, *, raises: Exception | None = None):
        """替换计数查询，返回「调用记录」列表（每元素为 (scheme_id, section_id)）。"""
        from app.routers import _chart_pipeline as cp

        calls: list[tuple[str, str]] = []

        async def fake_loader(db, scheme_id, section_id):
            calls.append((scheme_id, section_id))
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(cp, "_load_scheme_type_counts", fake_loader)
        return cp, calls

    @pytest.mark.asyncio
    async def test_counts_loaded_exactly_once_and_passed_through(self, monkeypatch):
        """一次调用 → 只查一次库；scheme_id/section_id 原样透传。"""
        cp, calls = self._patch_loader(
            monkeypatch, {"gantt": 1, "flowchart": 2})

        counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "SCH", "SEC", word_budget=1200,
            section_title="施工总进度计划",
            quota_hint_enabled=True, trigger_hint_enabled=True)

        assert calls == [("SCH", "SEC")], "配额统计被重复查询"
        assert counts == {"gantt": 1, "flowchart": 2}
        assert quota and "剩余 2 个" in quota          # gantt 1/3 → 剩 2
        assert trigger and "甘特图" in trigger          # 未满 → 仍推荐

    @pytest.mark.asyncio
    async def test_exhausted_type_not_recommended(self, monkeypatch):
        """核心回归：gantt 已达全案上限 → 配额提示报满、词库不得再推荐 gantt。"""
        cp, calls = self._patch_loader(monkeypatch, {"gantt": 3})

        _counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划")

        assert len(calls) == 1
        assert "gantt：已达全方案上限（3/3）" in quota
        assert trigger == "", "已达上限的类型仍被推荐 → 必然「画了又被删」"

    @pytest.mark.asyncio
    async def test_both_switches_off_skips_query(self, monkeypatch):
        """两开关全关 → 不查库、不产出提示（关闭即完全零副作用）。"""
        cp, calls = self._patch_loader(monkeypatch, {"gantt": 3})

        counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划",
            quota_hint_enabled=False, trigger_hint_enabled=False)

        assert calls == [], "开关全关时仍在查库"
        assert counts == {} and quota == "" and trigger == ""

    @pytest.mark.asyncio
    async def test_quota_off_still_loads_once_for_trigger(self, monkeypatch):
        """只开配图参考 → 仍需一次计数（供其排除已满类型）。"""
        cp, calls = self._patch_loader(monkeypatch, {"gantt": 1})

        _counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划",
            quota_hint_enabled=False, trigger_hint_enabled=True)

        assert len(calls) == 1
        assert quota == "" and trigger and "甘特图" in trigger

    @pytest.mark.asyncio
    async def test_trigger_off_keeps_quota_hint(self, monkeypatch):
        """只开配额提示 → 提示照常、配图参考为空（独立开关互不影响）。"""
        cp, calls = self._patch_loader(monkeypatch, {"gantt": 0})

        _counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划",
            quota_hint_enabled=True, trigger_hint_enabled=False)

        assert len(calls) == 1
        assert quota and "gantt" in quota
        assert trigger == ""

    @pytest.mark.asyncio
    async def test_loader_failure_fail_soft(self, monkeypatch):
        """查库异常不得阻断生成：counts 置空，两处提示按空计数继续产出。"""
        cp, calls = self._patch_loader(
            monkeypatch, raises=RuntimeError("db down"))

        counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划")

        assert len(calls) == 1
        assert counts == {}
        assert quota and "gantt：剩余 3 个" in quota
        assert trigger and "甘特图" in trigger

    @pytest.mark.asyncio
    async def test_loader_returning_none_is_tolerated(self, monkeypatch):
        """计数查询返回 None（旧连接异常场景）→ 按空计数继续。"""
        cp, calls = self._patch_loader(monkeypatch, None)

        counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划")

        assert len(calls) == 1
        assert counts == {}
        assert quota and "gantt" in quota
        assert trigger and "甘特图" in trigger

    @pytest.mark.asyncio
    async def test_word_budget_short_section_advises_none(self, monkeypatch):
        """配额提示的字数闸门：目标字数 < 200 → 明确建议不配图。"""
        cp, _calls = self._patch_loader(monkeypatch, {})

        _counts, quota, _trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", word_budget=150,
            section_title="施工总进度计划")

        assert quota and "建议不配图" in quota

    @pytest.mark.asyncio
    async def test_loader_real_query_aggregates_and_excludes_current_section(self):
        """计数查询本身：按类型聚合，且 SQL 必须排除当前章节（避免自增重复计数）。"""
        from app.routers import _chart_pipeline as cp

        seen_sql: list[str] = []

        class _Cur:
            async def fetchall(self):
                return [{"chart_type": "gantt", "n": 2},
                        {"chart_type": "flowchart", "n": 1}]

        class _DB:
            async def execute(self, sql, params=()):
                seen_sql.append(sql)
                assert params == ("SCH", "SEC")
                return _Cur()

        assert await cp._load_scheme_type_counts(_DB(), "SCH", "SEC") == {
            "gantt": 2, "flowchart": 1}
        assert "section_id<>?" in seen_sql[0], (
            "计数未排除当前章节 → 本章既有图表会把自己算进配额，重复生成时误判")

    @pytest.mark.asyncio
    async def test_min_confidence_threshold_applies_to_trigger_only(self, monkeypatch):
        """阈值只影响配图参考；配额提示始终按真实计数产出。"""
        cp, calls = self._patch_loader(monkeypatch, {"gantt": 0})

        _counts, quota, trigger = await cp.build_section_chart_hints(
            object(), "S", "SEC", section_title="施工总进度计划",
            min_confidence=0.99)

        assert len(calls) == 1
        assert quota and "gantt" in quota
        assert trigger == "", "阈值 0.99 下仍产出了配图参考"
