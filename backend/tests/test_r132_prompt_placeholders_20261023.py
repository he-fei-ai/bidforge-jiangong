# -*- coding: utf-8 -*-
"""R132-B 提示词占位符未解析专项 —— 防回潮回归测试（2026-10-23）

立项：R132-B（P0），见 docs/遗留问题单独立项计划_R131_20261023.md §二。

本测试把修复前口径（R132-B 之前：仅豁免 {SHARED_*} / JSON 示例误报 /
整行独占可选区块，未登记动态白名单）下「全模板 × 全占位符」
的未解析集合**冻结为字面基线清单**（BASELINE_UNRESOLVED，162 条 key::token，
冻结日期 2026-10-23），然后用修复后口径重扫现状，断言：

  ① 现状 ⊆ 基线 —— 零新增（任何人新加模板占位符/改动提示词文案导致
     未解析集合扩大都会被拦下）；
  ② 本轮修复的 token 已从清单移除 —— BASELINE 与现状的差集必须**恰好**
     等于修复清单 FIXED_CONTEXT（18 条 bid_item::__CONTEXT__，
     经 DYNAMIC_PLACEHOLDER_ALLOWLIST 白名单登记豁免），且清单更短；
  ③ 告警链行为不回潮（豁免 token 永不进告警、真缺业务变量仍告警、
     fail-soft 降级不阻断 —— 详见各用例注释）。

⚠️ 若未来**合法**改动模板占位符或新增白名单登记项，必须经评审同步更新
   本文件的基线清单（这正是"冻结"的意义：改动必须显式可见）。
"""
from __future__ import annotations

import ast
import inspect
import logging
import pathlib
import re

import pytest
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    DYNAMIC_PLACEHOLDER_ALLOWLIST,
    _is_false_positive,
    _is_optional_block_var,
    extract_variables,
    is_dynamic_placeholder,
    register_lazy_prompts,
    render_prompt,
    validate_prompt_variables,
)

REGISTRY_LOGGER = "app.services.ai.prompts._registry"

PROMPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "prompts"

#: 修复前口径下的「全模板 × 全占位符」未解析集合冻结基线
#: （R132-B 之前口径：{SHARED_*} / JSON 示例误报 / 整行可选区块已豁免，
#:  但动态白名单尚未落地 —— 故 bid_item 的 __CONTEXT__ 在列）。
#: 共 162 条，格式 "模板key::占位符token"，冻结于 2026-10-23。
BASELINE_UNRESOLVED = frozenset({
    "ILLUSTRATION_PROMPT_OPTIMIZE::section_title",
    "ILLUSTRATION_PROMPT_OPTIMIZE::style",
    "bid_item_calcAndDrawings::CONTEXT",
    "bid_item_compilationBasis::CONTEXT",
    "bid_item_constructionDeployment::CONTEXT",
    "bid_item_constructionProcess::CONTEXT",
    "bid_item_constructionTechnique::CONTEXT",
    "bid_item_deploymentSchedule::CONTEXT",
    "bid_item_emergencyResponse::CONTEXT",
    "bid_item_engineeringMethods::CONTEXT",
    "bid_item_equipmentManagement::CONTEXT",
    "bid_item_materialManagement::CONTEXT",
    "bid_item_overviewParams::CONTEXT",
    "bid_item_projectBasicInfo::CONTEXT",
    "bid_item_qualityAcceptance::CONTEXT",
    "bid_item_resourceAllocation::CONTEXT",
    "bid_item_safetyMeasures::CONTEXT",
    "bid_item_schemeBasicInfo::CONTEXT",
    "bid_item_siteConditions::CONTEXT",
    "bid_item_workInterfaceDivision::CONTEXT",
    "chart_json_fix::chart_type",
    "chart_json_fix::error",
    "chart_json_fix::scheme_type",
    "chart_json_fix_architecture::error",
    "chart_json_fix_architecture::scheme_type",
    "chart_json_fix_comparison::error",
    "chart_json_fix_comparison::scheme_type",
    "chart_json_fix_gantt::error",
    "chart_json_fix_gantt::scheme_type",
    "chart_json_fix_labor::error",
    "chart_json_fix_labor::scheme_type",
    "chart_json_fix_layout::error",
    "chart_json_fix_layout::scheme_type",
    "chart_mermaid_fix::error",
    "chart_mermaid_fix::scheme_type",
    "chart_mermaid_fix_comparison::error",
    "chart_mermaid_fix_comparison::scheme_type",
    "chart_mermaid_fix_flowchart::error",
    "chart_mermaid_fix_flowchart::scheme_type",
    "chart_mermaid_fix_gantt::error",
    "chart_mermaid_fix_gantt::scheme_type",
    "compliance_check_system::checklist",
    "compliance_check_system::content",
    "compliance_check_system::scheme_name",
    "compliance_check_system::scheme_type",
    "consistency_audit_system::content",
    "consistency_audit_system::facts",
    "consistency_audit_system::scheme_name",
    "consistency_audit_system::scheme_type",
    "consistency_fullbook_user::section_count",
    "consistency_repair_edits_user::section_id",
    "consistency_repair_edits_user::section_title",
    "consistency_repair_user::section_id",
    "consistency_repair_user::section_title",
    "consistency_scan_batch_user::section_count",
    "consistency_scan_user::section_id",
    "consistency_scan_user::section_title",
    "content_continue_system::scheme_name",
    "content_continue_system::scheme_type",
    "content_fact_autofix_user::generation_standard",
    "content_fact_autofix_user::section_id",
    "content_fact_autofix_user::section_title",
    "content_generation_system::scheme_name",
    "content_generation_system::scheme_type",
    "content_generation_system::section_number",
    "content_plan_user::parent_chain",
    "content_plan_user::project_brief",
    "content_plan_user::scheme_name",
    "content_plan_user::scheme_type",
    "content_plan_user::section_description",
    "content_plan_user::section_number",
    "content_plan_user::section_title",
    "content_plan_user::sibling_lines",
    "content_plan_user::word_budget",
    "content_shrink_system::current_words",
    "content_shrink_system::max_rounds",
    "content_shrink_system::round_no",
    "content_shrink_system::scheme_name",
    "content_shrink_system::scheme_type",
    "content_shrink_system::section_title",
    "content_shrink_system::target_words",
    "expert_review_system::attachments",
    "expert_review_system::check_items",
    "expert_review_system::outline_tree",
    "expert_review_system::scheme_name",
    "expert_review_system::scheme_type",
    "facts_extract_system::chunk_index",
    "facts_extract_system::heading",
    "facts_extract_system::priority_hint",
    "facts_extract_system::priority_weight",
    "facts_extract_system::zone_type",
    "facts_json_fix_system::target_description",
    "json_schema_fix_system::invalid_content",
    "json_schema_fix_system::issues",
    "json_schema_fix_system::target_description",
    "outline_adjust_system::scheme_name",
    "outline_adjust_system::scheme_type",
    "outline_feedback_system::project_brief",
    "outline_feedback_system::project_facts",
    "outline_feedback_system::scheme_name",
    "outline_feedback_system::scheme_type",
    "outline_json_fix_system::invalid_content",
    "outline_json_fix_system::issues",
    "outline_json_fix_system::target_description",
    "outline_level1_system::construction_scope",
    "outline_level1_system::project_brief",
    "outline_level1_system::project_facts",
    "outline_level1_system::reference_outline",
    "outline_level1_system::scheme_name",
    "outline_level1_system::scheme_type",
    "outline_level1_system::standards_text",
    "outline_patch_system::chapter_titles",
    "outline_patch_system::project_brief",
    "outline_patch_system::project_facts",
    "outline_patch_system::scheme_name",
    "outline_patch_system::scheme_type",
    "outline_review_system::construction_scope",
    "outline_review_system::is_dangerous",
    "outline_review_system::outline_json",
    "outline_review_system::project_facts",
    "outline_review_system::scheme_name",
    "outline_review_system::scheme_type",
    "outline_short_system::construction_scope",
    "outline_short_system::project_facts",
    "outline_short_system::scheme_name",
    "outline_short_system::scheme_type",
    "outline_short_system::standards_text",
    "outline_sublevel_batch_system::chapter_count",
    "outline_sublevel_batch_system::construction_scope",
    "outline_sublevel_batch_system::other_outline",
    "outline_sublevel_batch_system::prior_chapters",
    "outline_sublevel_batch_system::project_brief",
    "outline_sublevel_batch_system::project_facts",
    "outline_sublevel_batch_system::requirements",
    "outline_sublevel_batch_system::scheme_name",
    "outline_sublevel_batch_system::scheme_type",
    "outline_sublevel_batch_system::standards_text",
    "outline_sublevel_system::chapter_desc",
    "outline_sublevel_system::chapter_id",
    "outline_sublevel_system::chapter_title",
    "outline_sublevel_system::construction_scope",
    "outline_sublevel_system::other_outline",
    "outline_sublevel_system::prior_chapters",
    "outline_sublevel_system::project_brief",
    "outline_sublevel_system::project_facts",
    "outline_sublevel_system::requirements",
    "outline_sublevel_system::scheme_name",
    "outline_sublevel_system::scheme_type",
    "outline_sublevel_system::standards_text",
    "outline_title_fix_system::construction_scope",
    "outline_title_fix_system::project_brief",
    "outline_title_fix_system::project_facts",
    "outline_title_fix_system::requirements",
    "outline_title_fix_system::scheme_name",
    "outline_title_fix_system::scheme_type",
    "outline_title_fix_system::standards_text",
    "review_autofix_user::rule_id",
    "review_autofix_user::rule_title",
    "review_autofix_user::scheme_name",
    "review_autofix_user::scheme_type",
    "review_autofix_user::section_id",
    "review_autofix_user::section_title",
})

#: 本轮（R132-B，2026-10-23）修复而从基线移除的条目：18 条 bid_item 的
#: __CONTEXT__ —— 由 DYNAMIC_PLACEHOLDER_ALLOWLIST 白名单登记豁免
#: （bid_analysis_service.build_item 用 .replace 注入，非渲染入参）。
FIXED_CONTEXT = frozenset({
    "bid_item_calcAndDrawings::CONTEXT",
    "bid_item_compilationBasis::CONTEXT",
    "bid_item_constructionDeployment::CONTEXT",
    "bid_item_constructionProcess::CONTEXT",
    "bid_item_constructionTechnique::CONTEXT",
    "bid_item_deploymentSchedule::CONTEXT",
    "bid_item_emergencyResponse::CONTEXT",
    "bid_item_engineeringMethods::CONTEXT",
    "bid_item_equipmentManagement::CONTEXT",
    "bid_item_materialManagement::CONTEXT",
    "bid_item_overviewParams::CONTEXT",
    "bid_item_projectBasicInfo::CONTEXT",
    "bid_item_qualityAcceptance::CONTEXT",
    "bid_item_resourceAllocation::CONTEXT",
    "bid_item_safetyMeasures::CONTEXT",
    "bid_item_schemeBasicInfo::CONTEXT",
    "bid_item_siteConditions::CONTEXT",
    "bid_item_workInterfaceDivision::CONTEXT",
})


class _WarningCapture:
    """捕获指定 logger 消息的轻量上下文管理器（与既有审计测试同款做法）。"""

    def __init__(self, logger_name: str):
        self.name = logger_name
        self.records: list[str] = []

    def __enter__(self):
        lg = logging.getLogger(self.name)
        self._lg, self._level = lg, lg.level
        recs = self.records

        def _emit(rec):
            recs.append(rec.getMessage())

        self._h = logging.Handler()
        self._h.emit = _emit
        lg.addHandler(self._h)
        lg.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        self._lg.removeHandler(self._h)
        self._lg.setLevel(self._level)
        return False


@pytest.fixture(scope="module")
def registered():
    """确保全部模板（含惰性注册的投标分析域 bid_item_*）已入注册表。

    惰性注册钩子在包 import 期可能因既有循环导入告警失败一次
    （失败后幂等锁不置位，读注册表路径会自动重试），故显式重试到
    bid_item_* 出现为止，保证静态扫描覆盖完整。
    """
    for _ in range(3):
        register_lazy_prompts()
        if "bid_item_projectBasicInfo" in _ALL_PROMPTS:
            break
    assert "bid_item_projectBasicInfo" in _ALL_PROMPTS, (
        "投标分析域提示词未注册（惰性注册失败），静态扫描覆盖不完整")
    assert len(_ALL_PROMPTS) >= 76, (
        f"注册模板数 {len(_ALL_PROMPTS)} 低于冻结时的 76，扫描覆盖不完整")
    return _ALL_PROMPTS


def _scan_current(templates) -> frozenset[str]:
    """修复后口径的「全模板 × 全占位符」未解析集合静态扫描。

    判据链与 render_prompt 告警路径逐条对齐（单一事实源）：
    动态占位（{SHARED_*} / 白名单）→ JSON 示例误报 → 整行可选区块。
    """
    out: set[str] = set()
    for key, meta in templates.items():
        tpl = meta.get("default_content") or meta.get("content") or ""
        for tok in extract_variables(tpl):
            if is_dynamic_placeholder(tok):
                continue  # R132-B：SHARED_* 运行期解析 + 白名单登记项
            if _is_false_positive("", tok, tpl):
                continue  # JSON 示例误报（{max: 200} 等）
            if _is_optional_block_var(tok, tpl):
                continue  # 整行独占可选区块，未传即整行丢弃
            out.add(f"{key}::{tok}")
    return frozenset(out)


# ===========================================================================
# ① 现状 ⊆ 冻结基线：零新增（防"新增未解析占位符"回潮）
# ===========================================================================
def test_current_unresolved_subset_of_baseline(registered):
    """现状未解析集合必须是冻结基线的子集 —— 任何新增条目即失败。"""
    current = _scan_current(registered)
    new_added = sorted(current - BASELINE_UNRESOLVED)
    assert not new_added, (
        "R132-B 防回潮：出现基线之外的新增未解析占位符（需评审并显式更新"
        f"基线清单）: {new_added}")


# ===========================================================================
# ② 本轮修复的 token 已从清单移除：差集恰为修复清单，且清单更短
# ===========================================================================
def test_fixed_context_tokens_removed(registered):
    """差集必须恰好等于 18 条 bid_item::__CONTEXT__，且现状严格更短。"""
    current = _scan_current(registered)
    fixed = BASELINE_UNRESOLVED - current
    assert fixed == FIXED_CONTEXT, (
        "R132-B：基线与现状的差集必须恰为本轮修复的 CONTEXT 条目 —— "
        f"多移除/少移除均异常。多移除={sorted(fixed - FIXED_CONTEXT)} "
        f"少移除={sorted(FIXED_CONTEXT - fixed)}")
    ctx_left = sorted(p for p in current if p.endswith("::CONTEXT"))
    assert not ctx_left, f"CONTEXT 已登记白名单豁免，不应再入现状: {ctx_left}"
    assert len(current) < len(BASELINE_UNRESOLVED), (
        f"清单应比冻结前更短: 现状 {len(current)} vs 基线 "
        f"{len(BASELINE_UNRESOLVED)}")


# ===========================================================================
# ③ 告警链行为不回潮：豁免 token 永不进告警；真缺业务变量仍必须告警
#     （后者是既有契约 test_real_missing_variable_still_reported 钉住的
#      「无实参 + 真缺 → 告警」口径，R132-B 不加门控与其互斥，见
#      _registry.render_prompt 内 2026-10-23 注释）
# ===========================================================================
def test_exempted_tokens_never_flagged_in_no_arg_scan(registered):
    """全模板无实参扫描：告警 token 只能是「真未解析」集合成员。

    已豁免项（{SHARED_*} / CONTEXT 白名单 / JSON 示例 / 整行可选区块）
    永不得出现在任何未解析告警里；同时真缺 token 仍须照常暴露
    （口径不被误杀——全模板扫描的既有用途）。
    """
    with _WarningCapture(REGISTRY_LOGGER) as cap:
        for meta in registered.values():
            tpl = meta.get("default_content") or meta.get("content") or ""
            render_prompt(tpl)
    prefix = "Prompt template has unresolved placeholders:"
    flagged: set[str] = set()
    for msg in cap.records:
        if prefix not in msg:
            continue
        try:
            parsed = ast.literal_eval(msg.split(prefix, 1)[1].strip())
        except (ValueError, SyntaxError):
            continue  # 非列表形态的告警文本不参与 token 级比对
        if isinstance(parsed, list):
            flagged.update(str(t) for t in parsed)
    assert flagged, "全模板扫描应照常暴露真未解析 token（既有告警口径未被误杀）"
    allowed = {p.split("::", 1)[1] for p in _scan_current(registered)}
    bad = sorted(flagged - allowed)
    assert not bad, f"已豁免 token 出现在未解析告警中（防回潮）: {bad}"


def test_partial_args_still_warns_for_real_missing():
    """部分传参时缺失变量仍须告警（真缺信号不得被任何过滤吞掉）。"""
    tpl = "方案：{scheme_name} 类型：{scheme_type} 章节：{section_number}"
    with _WarningCapture(REGISTRY_LOGGER) as cap:
        render_prompt(tpl, scheme_name="某项目")
    hits = [m for m in cap.records
            if "unresolved placeholders" in m and "scheme_type" in m]
    assert hits, "部分传参缺失的 scheme_type/section_number 应触发告警"


def test_context_exempt_in_real_bid_item_template(registered):
    """真实 bid_item 模板：__CONTEXT__ 命中白名单，有传参也不告警。"""
    meta = registered["bid_item_projectBasicInfo"]
    tpl = meta.get("default_content") or meta.get("content") or ""
    with _WarningCapture(REGISTRY_LOGGER) as cap:
        render_prompt(tpl, dummy_flag="1")  # 非空 kwargs 进入告警判定
    hits = [m for m in cap.records if "unresolved placeholders" in m]
    assert not hits, f"CONTEXT 属白名单动态占位，不应告警: {hits}"


# ===========================================================================
# ④ fail-soft 硬约束：占位符解析/检测异常不得阻断 AI 调用
# ===========================================================================
def test_render_failsoft_returns_template_on_internal_error(monkeypatch):
    """替换环节异常 → 降级为原文返回并告警，不得向上抛。"""
    import app.services.ai.prompts._registry as reg

    def _boom(*a, **k):
        raise RuntimeError("模拟渲染异常")

    monkeypatch.setattr(reg, "_render_by_line", _boom)
    with _WarningCapture(REGISTRY_LOGGER) as cap:
        out = reg.render_prompt("原文 {scheme_name}", scheme_name="X")
    assert out == "原文 {scheme_name}", "fail-soft 必须降级为原文返回"
    assert any("降级为原文返回" in m for m in cap.records), (
        "降级必须留下告警痕迹")


def test_unresolved_detection_failsoft_keeps_render(monkeypatch):
    """未解析检测环节异常 → 只告警忽略，渲染结果照常返回。"""
    import app.services.ai.prompts._registry as reg

    def _boom(_tpl):
        raise RuntimeError("模拟检测异常")

    monkeypatch.setattr(reg, "extract_variables", _boom)
    with _WarningCapture(REGISTRY_LOGGER) as cap:
        out = reg.render_prompt("值 {scheme_name}", scheme_name="X")
    assert out == "值 X", "检测异常不得影响已渲染结果"
    assert any("未解析占位符检测异常" in m for m in cap.records), (
        "检测异常必须留下告警痕迹")


# ===========================================================================
# ⑤ 校验侧动态豁免：空参校验不再把 CONTEXT 报成缺失
# ===========================================================================
def test_validate_exempts_dynamic_context(registered):
    """bid_item_* 空参校验 missing=[]；非动态业务变量仍如实报缺失。"""
    assert validate_prompt_variables("bid_item_projectBasicInfo") == []
    missing = validate_prompt_variables("content_generation_system")
    assert "scheme_name" in missing, "真实业务缺参不得被豁免吞掉"
    assert "CONTEXT" not in missing, "CONTEXT 已登记白名单，不得报缺失"


# ===========================================================================
# ⑥ 白名单唯一登记处 + 单一判据（本仓头号反模式：同一判据多处分叉）
# ===========================================================================
def test_dynamic_allowlist_is_single_registry():
    """白名单唯一登记处：当前仅 CONTEXT；判据函数覆盖 SHARED 与白名单。"""
    assert isinstance(DYNAMIC_PLACEHOLDER_ALLOWLIST, frozenset)
    assert DYNAMIC_PLACEHOLDER_ALLOWLIST == frozenset({"CONTEXT"}), (
        "R132-B：新增动态占位必须登记到 DYNAMIC_PLACEHOLDER_ALLOWLIST 并"
        "同步更新本测试基线（禁止散落硬编码）")
    assert is_dynamic_placeholder("CONTEXT") is True
    assert is_dynamic_placeholder("SHARED_OUTPUT_SPEC") is True
    assert is_dynamic_placeholder("scheme_name") is False


def test_warning_paths_share_single_judgment():
    """四处告警/校验消费方必须调用 is_dynamic_placeholder，禁止内联副本。"""
    import app.services.ai.prompts._registry as reg
    from app.services.ai.prompts import _cache as cache_mod

    consumers = [
        reg.render_prompt,
        reg.validate_prompt_variables,
        cache_mod.get_prompt,
        cache_mod.get_prompt_with_validation,
    ]
    for fn in consumers:
        src = inspect.getsource(fn)
        assert "is_dynamic_placeholder" in src, (
            f"{fn.__qualname__} 未使用统一判据 is_dynamic_placeholder")
        assert 'startswith("SHARED_")' not in src, (
            f"{fn.__qualname__} 存在散落的 SHARED_ 内联判据副本"
            "（反模式：同一判据多处分叉）")


def test_no_scattered_context_equality_hardcoding():
    """prompts 包内禁止出现 `== "CONTEXT"` 式散落硬编码判据。"""
    offenders = []
    for f in sorted(PROMPTS_DIR.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        if re.search(r"==\s*['\"]CONTEXT['\"]", src):
            offenders.append(f.name)
    assert not offenders, (
        "R132-B：动态占位豁免只能走 DYNAMIC_PLACEHOLDER_ALLOWLIST 唯一"
        f"登记处，禁止散落判据副本: {offenders}")
