# -*- coding: utf-8 -*-
"""R160 图表全链路文档核对收口护栏（2026-11-01）。

来源：桌面四份《商业级图文并茂》设计文档（流程图/架构图/统计图/其它图表）×
本仓图表管线逐项核对。核对结论：绝大多数主张已被既有形态覆盖（三围栏管线、
统一样张、三层去重、图题链、三轨渲染、甘特闭环、labor 归一校验、架构防拥挤…），
本轮只修三处真实缺口：
- FIX-1 触发关键词映射表补全（文档一级触发的 13 个缺口词，含机械/材料资源面）；
- FIX-2 图文呼应三件套补全（图后呼应 + 图文名称与正文一致 + 架构图黄金插入位）；
- FIX-3 旧预编排判型死代码簇删除（chart_validators 412 行，防「同一语义两条
  入口」漂移与按文档重复接线）。

裁定不做（留痕）：AI_Chart HTML 注释标记兼容（第二格式违背唯一实现纪律）、
预编排（R58 刻意裁定：读陈旧结论）、管理章节程序侧硬阻断（流程文档与架构
文档对同一批管理章节给出相反指令，现有提示词调和形态正确）、架构图图文
一致性强制覆盖（误报风险）、统计图峰值位置硬校验（语义启发式，与「校验降级
告警」演化方向相反）、RACI/鱼骨/雷达渲染器族（白名单 7 类既有裁定）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.routers._chart_pipeline import build_inline_chart_plan
from app.services import chart_validators as cv
from app.services.ai.prompts._registry import get_default_prompt

# ===========================================================================
# A 组 · FIX-1 触发关键词映射表补全（判型归属行级锚）
# ===========================================================================

NEW_WORDS_BY_TYPE = {
    "gantt": ["关键线路"],
    "flowchart": ["审批流程", "应急响应流程", "拆除顺序", "材料进场检验"],
    "architecture": ["应急组织机构", "应急救援体系", "岗位职责", "项目班子"],
    "labor": ["劳动力动态曲线"],
    "comparison": ["机械设备配置", "材料需求", "物资需求"],
    "layout": ["施工分区"],
}


def _table_row(prompt: str, chart_type: str) -> str:
    rows = [ln for ln in prompt.splitlines()
            if ln.strip().startswith(f"| {chart_type} |")]
    assert len(rows) == 1, f"{chart_type} 映射表行应恰 1 行，实得 {len(rows)}"
    return rows[0]


class TestTriggerKeywordCompletion:
    @pytest.mark.parametrize("chart_type,words", sorted(NEW_WORDS_BY_TYPE.items()))
    def test_new_keywords_in_own_type_row(self, chart_type: str, words: list[str]):
        prompt = get_default_prompt("content_generation_system")
        row = _table_row(prompt, chart_type)
        for kw in words:
            assert kw in row, f"触发词「{kw}」应落在 {chart_type} 行"

    def test_emergency_semantic_isolation(self):
        """「应急响应」是流程属性入 flowchart、「应急组织」是层级属性入 architecture，
        不得互相串行（判型语义隔离）。"""
        prompt = get_default_prompt("content_generation_system")
        fc = _table_row(prompt, "flowchart")
        ar = _table_row(prompt, "architecture")
        assert "应急响应" in fc and "应急响应" not in ar
        assert "应急组织" in ar and "应急组织" not in fc

    def test_resource_words_only_in_comparison(self):
        """机械/材料资源词入 comparison（数量对比），不得进 labor（工种人数矩阵）。"""
        prompt = get_default_prompt("content_generation_system")
        cmp_row = _table_row(prompt, "comparison")
        lb = _table_row(prompt, "labor")
        assert "机械设备" in cmp_row and "机械设备" not in lb
        assert "物资需求" in cmp_row and "物资需求" not in lb

    def test_existing_keywords_untouched(self):
        """既有触发词回归锚（R58 时代口径不回退）。"""
        prompt = get_default_prompt("content_generation_system")
        fc = _table_row(prompt, "flowchart")
        for kw in ("施工工序", "工艺流程", "检验流程", "验收程序"):
            assert kw in fc
        ar = _table_row(prompt, "architecture")
        for kw in ("组织机构", "管理体系", "任务分解"):
            assert kw in ar


# ===========================================================================
# B 组 · FIX-2 图文呼应三件套补全
# ===========================================================================

class TestChartEchoCompletion:
    def test_post_chart_echo_required(self):
        """图后必须紧跟呼应说明（如上图所示……），不得图后直接进入下一话题。"""
        prompt = get_default_prompt("content_generation_system")
        assert "呼应说明" in prompt
        assert "如上图所示" in prompt

    def test_chart_names_match_body_and_arch_position(self):
        """图文名称与正文一致 + 架构图黄金插入位（管理目标后、岗位职责罗列前）。"""
        prompt = get_default_prompt("content_generation_system")
        assert "图中一套名称" in prompt
        assert "不得作为本章第一段" in prompt
        assert "逐岗位职责罗列之前" in prompt

    def test_three_piece_complete(self):
        """三件套齐备：引导语（既有）+ 图后呼应（本轮）+ 名称一致（本轮）。"""
        prompt = get_default_prompt("content_generation_system")
        assert "引导语" in prompt
        assert "呼应说明" in prompt
        assert "图中一套名称" in prompt


# ===========================================================================
# C 组 · FIX-3 旧预编排判型死代码簇绝迹
# ===========================================================================

DEAD_SYMBOLS = (
    "TRIGGER_KEYWORDS",
    "SENTENCE_PATTERNS",
    "INDUSTRY_TERMS",
    "get_industry_terms_by_category",
    "trigger_match",
    "detect_chart_type_from_text",
    "extract_matched_keywords",
    "detect_chart_with_metadata",
)


class TestDeadTriggerClusterRemoved:
    def test_symbols_absent_from_chart_validators(self):
        src = Path(cv.__file__).read_text(encoding="utf-8")
        for sym in DEAD_SYMBOLS:
            assert sym not in src, f"死符号 {sym} 仍残留在 chart_validators.py"

    def test_symbols_absent_from_app_tree(self):
        """全 app/ 扫描（app/ 不含 tests 目录，测试文件自身的断言引用不受影响）。"""
        import app as app_module

        app_root = Path(app_module.__file__).parent
        offenders: list[str] = []
        for p in sorted(app_root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for sym in DEAD_SYMBOLS:
                if sym in text:
                    offenders.append(f"{p.relative_to(app_root)}:{sym}")
        assert offenders == [], f"死符号残留: {offenders}"

    def test_live_surface_intact(self):
        """存活面冒烟：归一/校验行为与删除前一致（删除只许摘死枝，不许伤活干）。"""
        ok, errs = cv.validate_labor_data(
            {"phases": ["准备", "主体"], "categories": ["普工"],
             "data": [[5], [20]]})
        assert ok and errs == []
        ok, errs = cv.validate_architecture_tree(
            {"root": {"label": "项目经理部", "children": [{"label": "技术部"}]}})
        assert ok and errs == []
        ok, errs = cv.validate_gantt_plan(
            {"tasks": [{"id": "t1", "name": "施工准备", "start": 1, "days": 3}]})
        assert ok and errs == []
        assert cv.PIL_RENDERABLE_CHART_TYPES == frozenset({
            "flowchart", "gantt", "architecture", "labor", "comparison",
            "layout", "timeline"})

    def test_inline_chart_pipeline_still_registers(self):
        """管线冒烟：围栏登记与纯计算 plan 不受 FIX-3 影响。"""
        content = ("施工工序如下图所示：\n\n```mermaid\nflowchart TD\n"
                   '  A["土方开挖"] --> B["基坑支护"]\n'
                   '  B --> C["基坑回填"]\n```\n\n呼应。\n')
        new_content, rows = build_inline_chart_plan(
            "S1", "SEC1", content, enforce_limits=False)
        assert len(rows) == 1 and rows[0][3] == "flowchart"


# ===========================================================================
# D 组 · 配额提示回归冒烟（本轮未触碰，防误伤）
# ===========================================================================

class TestQuotaHintRegression:
    def test_quota_hint_semantics_unchanged(self):
        from app.routers._chart_pipeline import (
            _CHART_PER_SECTION_LIMIT,
            _CHART_SCHEME_TYPE_DEFAULT_LIMIT,
            build_chart_quota_hint,
        )
        hint = build_chart_quota_hint({"flowchart": 3, "gantt": 1})
        assert "已达全方案上限" in hint
        assert "剩余 2" in hint
        assert f"本章最多 {_CHART_PER_SECTION_LIMIT} 个图表块" in hint
        assert _CHART_SCHEME_TYPE_DEFAULT_LIMIT == 3

    def test_min_words_threshold_unchanged(self):
        from app.routers._chart_pipeline import _CHART_MIN_WORDS
        assert _CHART_MIN_WORDS == 200
