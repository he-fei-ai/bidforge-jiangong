"""全局事实 · 引入参考软件（OpenBidKit 易标）能力的护栏

本轮引入的参考软件能力与其在本仓的落点
----------------------------------------
===============================  ==========================================
参考软件（globalFactsTask.cjs）  本仓落点
===============================  ==========================================
``normalizeFactId``             ``facts_patches.normalize_fact_id``
``ensureUniqueId``              ``facts_patches.ensure_unique_id``
``valueToMarkdown``             ``facts_patches.value_to_markdown``
``buildMissingFactRule``        ``facts_patches.build_missing_value_rule``
``buildGlobalFactsCompletenessRules``
                                ``facts_patches.build_completeness_rules``
``normalizeGlobalFactsPatchResponse``
                                ``facts_patches.normalize_patches_response``
``validateGlobalFactsPatchResponse``
                                ``facts_patches.validate_patches_response``
``mergeGlobalFactPatches``      ``facts_patches.merge_fact_patches``
``batchRenderedItems``          ``facts_patches.batch_rendered_items``
``waitAllOrThrow``              ``facts_patches.wait_all_or_throw``
``getGlobalFactsSegmentLimit``  ``facts_patches.get_segment_limit``
``HAZARD_THRESHOLDS`` 脚手架闭区间修正
                                ``scheme_classification.HAZARD_THRESHOLDS``

护栏分三组：与参考软件逐条对齐的语义断言、危大阈值闭区间、静态护栏。
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import re
from pathlib import Path

import pytest
from app.services import facts_patches as fp
from app.services import scheme_classification as sc
from app.services.facts_patches import (
    FactPatch,
    apply_patch_mode,
    batch_rendered_items,
    build_completeness_rules,
    build_missing_value_rule,
    ensure_unique_id,
    get_segment_limit,
    merge_fact_patches,
    normalize_fact_id,
    normalize_missing_value_mode,
    normalize_patches_response,
    validate_patches_response,
    value_to_markdown,
    wait_all_or_throw,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"


# =========================================================================
# 一、标识符与内容归一化（参考 normalizeFactId / ensureUniqueId /
#    valueToMarkdown）
# =========================================================================
class TestIdAndValueNormalization:
    def test_normalize_fact_id_keeps_ascii(self):
        """参考 :127-134：非 ASCII 清洗后仍保留 a-z0-9_-。"""
        assert normalize_fact_id("Project_Team-01") == "project_team-01"
        assert normalize_fact_id("  ABC  ") == "abc"

    def test_normalize_fact_id_pure_chinese_falls_back_to_index(self):
        """纯中文 id 清洗后为空 → 必须落到 fact_00N 兜底（不得返回空串）。

        参考 ``validateGlobalFactsResponse`` 要求 id 必填，空 id 会让整轮
        结果被拒；本仓的 fact_key 同样不允许为空。
        """
        assert normalize_fact_id("项目组", 0) == "fact_001"
        assert normalize_fact_id("项目组", 4) == "fact_005"
        assert normalize_fact_id("", 11) == "fact_012"

    def test_normalize_fact_id_never_empty(self):
        for raw in ("", "   ", "中文", "!!!", None, "___"):
            assert normalize_fact_id(raw, 0).strip()

    def test_ensure_unique_id_appends_suffix(self):
        """参考 :136-144：冲突时依次追加 _2 / _3。"""
        used: set = set()
        assert ensure_unique_id("a", used) == "a"
        assert ensure_unique_id("a", used) == "a_2"
        assert ensure_unique_id("a", used) == "a_3"
        assert used == {"a", "a_2", "a_3"}

    def test_value_to_markdown_dict_never_object_repr(self):
        """dict 必须转成 Markdown 行；绝不能出现 ``[object Object]``。"""
        out = value_to_markdown({"项目经理": "张伟", "安全员": "2"})
        assert "[object Object]" not in out
        assert "**项目经理**：张伟" in out
        assert "**安全员**：2" in out

    def test_value_to_markdown_list_variants(self):
        """list 内的 str → ``- x``；list 内的 dict → ``- **name**：value``。"""
        assert value_to_markdown(["- a", "- b"]) == "- - a\n- - b"
        got = value_to_markdown([{"name": "工期", "value": "450 日历天"}])
        assert "**工期**：450 日历天" in got

    def test_value_to_markdown_scalars_and_none(self):
        assert value_to_markdown(None) == ""
        assert value_to_markdown(3.5) == "3.5"
        assert value_to_markdown("  x  ") == "x"

    def test_value_to_markdown_never_raises(self):
        """永不抛异常（对齐参考实现「任意形状都能吃」）。"""
        for raw in (None, 0, 1.5, True, [], {}, {1: 2}, [[[1]]], object()):
            assert isinstance(value_to_markdown(raw), str)


# =========================================================================
# 二、缺值模式（参考 buildMissingFactRule / buildGlobalFactsCompletenessRules）
# =========================================================================
class TestMissingValueMode:
    def test_normalize_mode_value_domain(self):
        assert normalize_missing_value_mode("omit") == "omit"
        assert normalize_missing_value_mode("PLACEHOLDER") == "placeholder"
        # 非法值 fail-closed 到 fabricate（与本仓 sse_handlers 同口径）
        assert normalize_missing_value_mode("nope") == "fabricate"
        assert normalize_missing_value_mode(None) == "fabricate"
        assert normalize_missing_value_mode("") == "fabricate"

    def test_fabricate_mode_has_no_completeness_rules(self):
        """参考 :47：fabricate 模式返回空串（保持默认行为逐字一致）。"""
        assert build_completeness_rules("fabricate") == ""

    def test_omit_and_placeholder_rules_are_distinct(self):
        """两种模式的措辞必须逐字不同——这是契约的一部分。

        混用会让下游无法区分「用户明确留白（【待填写】）」与
        「资料确实没有（笼统承诺）」，正文侧标注随之失真。
        """
        omit = build_missing_value_rule("omit")
        ph = build_missing_value_rule("placeholder")
        assert omit != ph
        assert "【待填写】" in ph and "【待填写】" not in omit
        assert "笼统承诺" in omit and "笼统承诺" not in ph

    def test_completeness_rules_both_nonempty_for_non_default_modes(self):
        for mode in ("omit", "placeholder"):
            rules = build_completeness_rules(mode)
            assert rules.strip()
            assert "事实补全规则" in rules
            # 两种模式都必须强制保留工期类变量（参考 :32/:44）
            assert "工期" in rules

    def test_completeness_rules_mode_label_distinct(self):
        assert "别招欠模式" in build_completeness_rules("omit")
        assert "放着我来模式" in build_completeness_rules("placeholder")


# =========================================================================
# 三、补丁归一化 / 校验（参考 normalize/validateGlobalFactsPatchResponse）
# =========================================================================
class TestPatchNormalizeValidate:
    def test_normalize_drops_empty_content(self):
        """参考 :229：``if (!content) return null`` —— 空补丁必须被丢弃。"""
        got = normalize_patches_response(
            {"patches": [{"target_group_id": "g1", "content": "A"}, {"content": "   "}]})
        assert len(got["patches"]) == 1

    def test_normalize_accepts_reference_key_aliases(self):
        """参考 :214-224 的多键回退在本仓同样成立。"""
        for key in ("patches", "supplements", "additions", "items"):
            got = normalize_patches_response({key: [{"content": "A"}]})
            assert len(got["patches"]) == 1, key

    def test_normalize_unwraps_result_envelope(self):
        got = normalize_patches_response({"result": {"patches": [{"content": "A"}]}})
        assert len(got["patches"]) == 1

    def test_normalize_mode_whitelist_defaults_to_append(self):
        """参考 :230-231：非法 mode 归一为 append。"""
        got = normalize_patches_response(
            {"patches": [{"content": "A", "mode": "delete-all"},
                         {"content": "B", "mode": "REPLACE"}]})
        assert got["patches"][0].mode == "append"
        assert got["patches"][1].mode == "replace"

    def test_normalize_garbage_input_is_empty_not_crash(self):
        for raw in (None, 42, "text", [], {}):
            assert normalize_patches_response(raw)["patches"] == []

    def test_create_flag_distinguishes_patch_vs_new(self):
        got = normalize_patches_response({"patches": [
            {"target_group_id": "g1", "content": "A"},
            {"title": "新项", "content": "B"},
        ]})
        assert got["patches"][0].create is False
        assert got["patches"][1].create is True

    def test_target_group_id_alias_property(self):
        """兼容别名（跨语言比对参考软件字段名）。"""
        p = FactPatch(content="A", target_fact_id="g1")
        assert p.target_group_id == "g1"
        assert p.to_dict()["target_fact_id"] == "g1"

    def test_validate_accepts_valid(self):
        validate_patches_response(normalize_patches_response(
            {"patches": [{"content": "A"}]}))

    def test_validate_fails_closed_on_missing_patches(self):
        for bad in ({}, {"patches": None}, {"patches": "x"}, None, "x"):
            with pytest.raises(ValueError):
                validate_patches_response(bad)

    def test_validate_fails_closed_on_empty_content(self):
        with pytest.raises(ValueError):
            validate_patches_response({"patches": [FactPatch(content="  ")]})
        with pytest.raises(ValueError):
            validate_patches_response({"patches": ["not-a-patch"]})



# =========================================================================
# 四、补丁合并（参考 mergeGlobalFactPatches）
# =========================================================================
class TestMergePatches:
    def test_apply_patch_mode_three_modes(self):
        assert apply_patch_mode("base", "new", "append") == "base\n\nnew"
        assert apply_patch_mode("base", "new", "prepend") == "new\n\nbase"
        assert apply_patch_mode("base", "new", "replace") == "new"
        # 空基值：append/prepend 都不应产生前导空行
        assert apply_patch_mode("", "new", "append") == "new"
        assert apply_patch_mode("", "new", "prepend") == "new"

    def test_merge_by_id(self):
        groups = [{"id": "g1", "title": "T", "content": "base"}]
        out = merge_fact_patches(groups, [FactPatch(content="A", target_fact_id="g1")])
        assert out[0]["content"] == "base\n\nA"
        assert len(out) == 1

    def test_merge_falls_back_to_title(self):
        """参考 :258-261：id 未命中时按 title 定位。"""
        groups = [{"id": "g1", "title": "工期变量", "content": "base"}]
        out = merge_fact_patches(groups, [FactPatch(content="A", title="工期变量")])
        assert out[0]["content"] == "base\n\nA"
        assert len(out) == 1

    def test_merge_unmatched_creates_new_group(self):
        groups = [{"id": "g1", "title": "T", "content": "base"}]
        out = merge_fact_patches(groups, [FactPatch(
            content="A", new_fact_id="g2", title="新项")])
        assert len(out) == 2
        assert out[1] == {"id": "g2", "title": "新项", "content": "A"}

    def test_merge_new_group_id_never_empty(self):
        out = merge_fact_patches([], [FactPatch(content="A", title="中文标题")])
        assert out[0]["id"].strip()

    def test_merge_is_pure_does_not_mutate_input(self):
        groups = [{"id": "g1", "title": "T", "content": "base"}]
        snapshot = [dict(g) for g in groups]
        merge_fact_patches(groups, [FactPatch(content="A", target_fact_id="g1")])
        assert groups == snapshot

    def test_merge_skips_invalid_patches(self):
        groups = [{"id": "g1", "title": "T", "content": "base"}]
        out = merge_fact_patches(groups, [
            "junk", None, FactPatch(content="   "),
            FactPatch(content="ok", target_fact_id="g1")])
        assert out[0]["content"] == "base\n\nok"
        assert len(out) == 1

    def test_merge_empty_inputs(self):
        assert merge_fact_patches([], []) == []


# =========================================================================
# 五、分批与并发等待（参考 batchRenderedItems / waitAllOrThrow）
# =========================================================================
class TestBatchAndWait:
    def test_batch_respects_limit(self):
        assert batch_rendered_items([1, 2, 3, 4], lambda x: "x" * 10, 25) == [[1, 2], [3, 4]]

    def test_batch_no_loss_no_duplication(self):
        items = list(range(20))
        flat = [i for b in batch_rendered_items(items, lambda x: "x" * 7, 20) for i in b]
        assert flat == items

    def test_batch_non_positive_limit_single_batch(self):
        assert batch_rendered_items([1, 2, 3], lambda x: "x", 0) == [[1, 2, 3]]
        assert batch_rendered_items([1, 2, 3], lambda x: "x", -5) == [[1, 2, 3]]

    def test_batch_empty(self):
        assert batch_rendered_items([], lambda x: "x", 100) == []

    def test_batch_render_failure_does_not_abort(self):
        def boom(_):
            raise RuntimeError("render failed")
        assert batch_rendered_items([1, 2], boom, 10) == [[1, 2]]

    def test_wait_all_or_throw_returns_results(self):
        async def ok(v):
            return v
        assert asyncio.run(wait_all_or_throw([ok(1), ok(2)])) == [1, 2]

    def test_wait_all_or_throw_empty(self):
        assert asyncio.run(wait_all_or_throw([])) == []

    def test_wait_all_or_throw_raises_first_failure(self):
        """合并阶段失败没有降级语义 → 必须抛出，不得静默返回残缺结果。"""
        async def ok():
            return 1

        async def bad():
            raise ValueError("merge failed")

        with pytest.raises(ValueError, match="merge failed"):
            asyncio.run(wait_all_or_throw([ok(), bad()]))


# =========================================================================
# 六、上下文预算分段（参考 getGlobalFactsSegmentLimit）
# =========================================================================
class TestSegmentLimit:
    def test_ratio_and_floor_constants_match_reference(self):
        assert fp.DEFAULT_CONTEXT_LENGTH_LIMIT == 400_000
        assert fp.GLOBAL_FACTS_CONTEXT_LIMIT_RATIO == 0.8
        assert fp.MIN_GLOBAL_FACTS_SEGMENT_CHARS == 1_000

    def test_segment_limit_subtracts_fixed_messages(self):
        empty = get_segment_limit(400_000, [])
        with_fixed = get_segment_limit(400_000, [{"role": "user", "content": "x" * 1000}])
        assert empty == int(400_000 * 0.8)
        assert with_fixed < empty

    def test_segment_limit_has_lower_bound(self):
        """固定消息超长时必须回落到下限 1000，不得返回负数。"""
        huge = get_segment_limit(400_000, [{"role": "user", "content": "x" * 10_000_000}])
        assert huge == fp.MIN_GLOBAL_FACTS_SEGMENT_CHARS

    @pytest.mark.parametrize("bad", [None, 0, -1, "abc", float("nan")])
    def test_invalid_context_limit_falls_back(self, bad):
        assert get_segment_limit(bad, []) == int(
            fp.DEFAULT_CONTEXT_LENGTH_LIMIT * fp.GLOBAL_FACTS_CONTEXT_LIMIT_RATIO)

    def test_measure_messages_counts_role_overhead(self):
        assert fp.measure_messages_length(
            [{"role": "user", "content": "abc"}]) == len("user") + 3 + 64



# =========================================================================
# 七、FactItem 适配器（补丁落到扁平事实行）
# =========================================================================
class TestFactItemAdapter:
    def _items(self):
        from app.services.facts_extractor import FactItem
        return [FactItem(name="工期", value="450日历天", key="total_duration",
                         source="orig", confidence=0.9, is_simulated=True)]

    def test_patch_updates_value_in_place_preserving_metadata(self):
        """核心不变式：只改 value，溯源 / 置信度 / 模拟值标记全部保留。"""
        items = self._items()
        out = fp.apply_patches_to_fact_items(
            items, [FactPatch(content="追加：保质保量", target_fact_id="total_duration")])
        assert out[0].value == "450日历天\n\n追加：保质保量"
        assert out[0].source == "orig"
        assert out[0].confidence == 0.9
        assert out[0].is_simulated is True

    def test_patch_by_title_fallback(self):
        items = self._items()
        out = fp.apply_patches_to_fact_items(
            items, [FactPatch(content="X", title="工期")])
        assert out[0].value == "450日历天\n\nX"
        assert len(out) == 1

    def test_new_patch_item_annotated_and_not_simulated(self):
        items = self._items()
        out = fp.apply_patches_to_fact_items(
            items, [FactPatch(content="- 基坑深度：6.5m", new_fact_id="foundation_depth")])
        assert len(out) == 2
        new = out[1]
        assert new.is_simulated is False
        assert new.source == "patch"
        # 四维标注必须补齐，否则新行在章节视图里「消失」
        assert new.chapter
        assert new.fact_attr
        assert new.source_kind

    def test_replace_mode_clears_stale_conflict_values(self):
        """replace 后旧 conflict_values 可能已含被替换值，留着会误导裁决。"""
        items = self._items()
        items[0].has_conflict = True
        items[0].conflict_values = [{"value": "旧值", "source": "s"}]
        out = fp.apply_patches_to_fact_items(
            items, [FactPatch(content="新值", target_fact_id="total_duration",
                              mode="replace")])
        assert out[0].value == "新值"
        assert out[0].has_conflict is False
        assert out[0].conflict_values == []

    def test_append_mode_keeps_conflict_flag(self):
        items = self._items()
        items[0].has_conflict = True
        items[0].conflict_values = [{"value": "旧值", "source": "s"}]
        out = fp.apply_patches_to_fact_items(
            items, [FactPatch(content="追加", target_fact_id="total_duration",
                              mode="append")])
        assert out[0].has_conflict is True

    def test_empty_items_returns_empty(self):
        assert fp.apply_patches_to_fact_items([], [FactPatch(content="A")]) == []

    def test_derive_title_from_content(self):
        assert fp.derive_title_from_content("- 项目经理：张伟", 0) == "项目经理"
        assert fp.derive_title_from_content("纯文本", 2)
        assert fp.derive_title_from_content("", 1) == "补充事实项2"



# =========================================================================
# 八、危大阈值闭区间（含用户点名的 7 组参数）
# =========================================================================
class TestHazardThresholdClosure:
    @pytest.mark.parametrize("key,params,expect", [
        # 开挖深度（≥3m / ≥5m）
        ("fp_support_drain", {"depth": 3.0}, True),
        ("fp_support_drain", {"depth": 2.9}, False),
        ("fp_support_drain", {"depth": 5.0}, True),
        ("fp_earthwork", {"depth": 3.0}, True),
        # 支撑高度（≥5m / ≥8m）—— 需给全 fw_support 的四个参数，
        # 否则缺参会走 fail-closed 保守分支（恒判危大），见下方专项用例
        ("fw_support", {"height": 5.0, "span": 10.0,
                        "total_load": 10.0, "line_load": 15.0}, True),
        ("fw_support", {"height": 8.0, "span": 18.0,
                        "total_load": 15.0, "line_load": 20.0}, True),
        # 跨度（≥10m / ≥18m）
        ("fw_support", {"span": 10.0}, True),
        ("fw_support", {"span": 18.0}, True),
        # 施工总荷载（≥10 / ≥15 kN/m²）
        ("fw_support", {"total_load": 10.0}, True),
        ("fw_support", {"total_load": 15.0}, True),
        # 集中线荷载（≥15 / ≥20 kN/m）
        ("fw_support", {"line_load": 15.0}, True),
        ("fw_support", {"line_load": 20.0}, True),
        # 起吊重量（≥10kN / ≥100kN）
        ("ho_lift", {"single_weight": 10.0}, True),
        ("ho_lift", {"single_weight": 100.0}, True),
        # 脚手架搭设高度（≥24m / ≥50m）—— 本轮修复项
        ("sc_ground", {"height": 24.0}, True),
        ("sc_ground", {"height": 23.9}, False),
        ("sc_ground", {"height": 50.0}, True),
    ])
    def test_threshold_boundaries_are_inclusive(self, key, params, expect):
        """建办质〔2018〕31号 附件的「及以上」= 闭区间，临界值必须命中。

        ⚠️ 参数必须**给全**再断言闭区间：`evaluate_hazard_level` 对缺参
        采取 fail-closed（缺参 → 保守判危大，见其 docstring），只给
        ``height`` 而不给 span/荷载时 ``missing_params`` 非空，判定恒为
        危大，闭区间差异会被这一保守分支掩盖。
        """
        assert sc.evaluate_hazard_level(key, params)["is_hazardous"] is expect

    def test_template_support_closure_with_all_params(self):
        """模板支撑体系：高度 ≥5 危大 / ≥8 超规模（span/荷载同时给全）。"""
        full = {"height": 4.9, "span": 10.0, "total_load": 10.0, "line_load": 15.0}
        assert sc.evaluate_hazard_level("fw_support", full)["is_hazardous"] is True
        below = {"height": 4.9, "span": 9.9, "total_load": 9.9, "line_load": 14.9}
        got = sc.evaluate_hazard_level("fw_support", below)
        assert got["is_hazardous"] is False
        assert got["missing_params"] == []

    def test_missing_params_is_fail_closed_conservative(self):
        """缺参必须保守判危大（宁可多提示专家论证，不可漏判）。"""
        got = sc.evaluate_hazard_level("fw_support", {"height": 1.0})
        assert got["missing_params"] == ["span", "total_load", "line_load"]
        assert got["is_hazardous"] is True

    def test_scaffold_height_closure_with_own_param(self):
        """脚手架搭设高度 ≥24 危大 / ≥50 超规模（本轮 P0 修复项）。"""
        assert sc.evaluate_hazard_level("sc_ground", {"height": 24.0})["is_hazardous"] is True
        assert sc.evaluate_hazard_level("sc_ground", {"height": 23.9})["is_hazardous"] is False
        assert sc.evaluate_hazard_level("sc_ground", {"height": 50.0})["is_oversize"] is True
        assert sc.evaluate_hazard_level("sc_ground", {"height": 49.9})["is_oversize"] is False

    def test_oversize_boundaries(self):
        assert sc.evaluate_hazard_level("fw_support", {"height": 8.0})["is_oversize"] is True
        assert sc.evaluate_hazard_level("sc_ground", {"height": 50.0})["is_oversize"] is True
        assert sc.evaluate_hazard_level("sc_ground", {"height": 49.9})["is_oversize"] is False

    def test_no_strict_greater_conditions_remain(self):
        """静态护栏：部文附件全部是「及以上」，不得再出现严格 `>`。

        本仓 2026-09-24 已修基坑(3/5)与高大模板(8/18/15/20)，本轮补齐脚手架
        (24/50)——这是最后一处，也是唯一漏判 24m 临界值的那一处。
        """
        offenders = [
            (key, cond)
            for key, rule in sc.HAZARD_THRESHOLDS.items()
            for cond in (rule.get("hazard_when") or []) + (rule.get("oversize_when") or [])
            if cond[1] == ">"
        ]
        assert offenders == [], f"残留严格大于阈值（部文口径为闭区间）：{offenders}"

    def test_sc_ground_is_exactly_24_and_50(self):
        rule = sc.HAZARD_THRESHOLDS["sc_ground"]
        assert rule["hazard_when"] == [("height", ">=", 24)]
        assert rule["oversize_when"] == [("height", ">=", 50)]



# =========================================================================
# 九、静态护栏
# =========================================================================
class TestStaticGuards:
    def test_module_exports_all_reference_parity_functions(self):
        for name in ("normalize_fact_id", "ensure_unique_id", "value_to_markdown",
                     "build_missing_value_rule", "build_completeness_rules",
                     "normalize_patches_response", "validate_patches_response",
                     "merge_fact_patches", "batch_rendered_items",
                     "wait_all_or_throw", "get_segment_limit",
                     "apply_patches_to_fact_items"):
            assert callable(getattr(fp, name)), name

    def test_patch_modes_match_reference_whitelist(self):
        assert fp.PATCH_MODES == ("append", "prepend", "replace")
        assert fp.DEFAULT_PATCH_MODE == "append"

    def test_missing_value_modes_match_reference(self):
        assert fp.MISSING_VALUE_MODES == ("fabricate", "omit", "placeholder")

    def test_missing_value_modes_match_sse_handler_value_domain(self):
        """``sse_handlers.generate_facts`` 必须走 facts_patches 的单一出口。

        2026-09-30 第十三轮把该处原本的字面量值域
        （``not in ("fabricate","omit","placeholder")``）收敛为调用
        ``normalize_missing_value_mode``。本护栏随之从「断言字面量存在」
        升级为「断言单一出口被调用」——原断言在收敛后必然失败（它锁的正是
        要被消除的分叉），而升级后的断言能防住「又写回一份字面量」的回归。
        """
        src = (APP_DIR / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert "normalize_missing_value_mode" in src, \
            "sse_handlers.generate_facts 未走 facts_patches 单一出口"
        assert '("fabricate", "omit", "placeholder")' not in src, \
            "sse_handlers 重新写回了缺值模式值域字面量（分叉回归）"

    def test_fact_item_adapter_does_not_rebuild_items(self):
        """静态护栏：适配器只能改 value，不得出现 ``FactItem(`` 重建调用。

        重建会冲掉 source/confidence/is_simulated/chapter 等不变式标注
        ——这正是本仓 `/global-facts/adjust` 明确拒绝让 AI 重写整库的原因。
        """
        tree = ast.parse(inspect.getsource(fp.apply_patches_to_fact_items))
        calls = [n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "FactItem" not in calls

    def test_patch_item_construction_is_isolated_in_helper(self):
        """``FactItem(`` 只允许出现在 make_fact_item_from_patch 一处。"""
        tree = ast.parse(inspect.getsource(fp))
        fn_with_call = {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "FactItem"
                    for c in ast.walk(n))
        }
        assert fn_with_call == {"make_fact_item_from_patch"}

    def test_completeness_rules_single_source(self):
        """「事实补全规则」只能有一处实现（避免四处各写一份再分叉）。"""
        hits = []
        for py in (APP_DIR / "services").rglob("*.py"):
            if py.name == "facts_patches.py":
                continue
            if "事实补全规则（" in py.read_text(encoding="utf-8"):
                hits.append(py.name)
        assert hits == [], f"「事实补全规则」在多处重复实现：{hits}"

    def test_omission_wording_single_source(self):
        """「笼统承诺」措辞只能有一处定义。"""
        hits = []
        for py in (APP_DIR / "services").rglob("*.py"):
            if py.name == "facts_patches.py":
                continue
            if "不涉及具体时间、地点、人员、业绩、证书、规格型号" in py.read_text(
                    encoding="utf-8"):
                hits.append(py.name)
        assert hits == [], f"「笼统承诺」措辞在多处重复定义：{hits}"

    def test_no_circular_import_with_facts_extractor(self):
        """facts_extractor 引用 facts_patches 时不得形成循环导入。

        校验方式：两个模块互相 import 后仍能各自正常 import，且
        ``facts_patches`` 内延迟 import 的 FactItem 解析正常。
        """
        from app.services import facts_extractor as fe
        assert fe.FactItem is not None
        assert fp.make_fact_item_from_patch(FactPatch(content="X", title="T")) is not None

    def test_danger_param_names_all_classified_to_a_chapter(self):
        """危大判定参数的**中文规范名**必须全部有九大章节归属。

        ``DANGER_PARAM_RULES`` 是危大阈值参数的唯一事实源；其关键词对应的
        事实名若落空串，则该事实在九大章节视图里不显示、也不计入
        ``chapter_field_completeness`` 的任一章覆盖率——用户会看到
        「工程概况 0 条事实」却不知数据已提取（2026-09-30 修复项）。

        ⚠️ 只校验中文关键词：该表同时收录 ``foundation_depth`` 等**英文归一
        化键**，而九大章节规则是纯中文关键词匹配（英文键走
        ``CATEGORY_TO_CHAPTER`` / ``FACT_TYPE_TO_CHAPTER`` 两层判据）。
        对英文键强求中文章节归属会把「各层判据各司其职」误判为缺陷。
        """
        from app.services.facts_classification import DANGER_PARAM_RULES, classify_chapter_from_text
        unclassified = [
            kw for keywords, _param in DANGER_PARAM_RULES
            for kw in keywords
            if kw and not kw.isascii() and not classify_chapter_from_text(kw, "")
        ]
        assert unclassified == [], f"危大参数名未被九大章节规则覆盖：{unclassified}"

    def test_canonical_danger_fact_names_are_classified(self):
        """本轮修复的规范名逐一断言已归入第一章工程概况。"""
        from app.services.facts_classification import classify_chapter_from_text
        for name in ("基坑深度", "坑深", "支撑高度", "支撑架高度", "架体高度",
                     "跨度", "跨距", "施工总荷载", "总荷载", "集中线荷载",
                     "线荷载", "单件起吊重量", "起吊重量", "边坡高度",
                     "安装高度", "承载力"):
            assert classify_chapter_from_text(name, "") == "overview", name

    def test_danger_facts_counted_in_chapter_coverage(self):
        """危大参数必须计入 ``chapter_field_completeness`` 的工程概况章。"""
        from app.services.facts_classification import chapter_field_completeness
        rows = [{"name": "基坑深度", "value": "6.5m"},
                {"name": "支撑高度", "value": "9m"},
                {"name": "施工总荷载", "value": "16kN/m²"}]
        got = chapter_field_completeness(rows)
        overview = got["chapters"]["overview"]
        assert overview["fact_count"] == 3
        assert overview["has_facts"] is True
        assert got["chapters"]["calc_drawings"]["fact_count"] == 0

