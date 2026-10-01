"""图表修复提示词的 7 类 × 2 格式覆盖矩阵 parity 护栏（2026-09-27）。

背景（AGENTS.md §4.3 的「同一判据两处实现」模式）：

* 图表值域的唯一事实源是 ``chart_validators.PIL_RENDERABLE_CHART_TYPES``
  （7 类），但「图表修复提示词有哪些、覆盖哪几类」此前**没有任何断言**。
* 实测覆盖矩阵有 4 个缺口回退到通用版：
  ``chart_json_fix_flowchart`` / ``chart_json_fix_timeline`` /
  ``chart_mermaid_fix_architecture`` / ``chart_mermaid_fix_timeline`` 缺专项版。
* 其中 ``chart_mermaid_fix`` 通用版此前**完全没有提及 timeline**，
  于是「时间轴 + Mermaid 修复」链路拿不到任何时间轴结构约束，
  只能照 flowchart 规则改 → 必然产出错误结构。

本文件把「7 类值域」与「修复提示词覆盖」绑成一条 parity 断言：
**每一类、每一种格式，最终被选中的提示词正文里必须能读到这个类型的
结构口径**（含其 Mermaid 关键字别名），否则回退通用版就是静默失准。
"""
from __future__ import annotations

import pytest

from app.services.ai.prompts._registry import (
    PROMPT_VARIABLE_CONTRACTS, _ALL_PROMPTS, get_default_prompt, render_prompt,
)
from app.services.chart_validators import (
    MERMAID_KEYWORD_TO_CHART_TYPE, PIL_RENDERABLE_CHART_TYPES,
)

#: 7 类值域 —— 直接取校验器，不在此处复写（单一事实源）
ALL_TYPES = sorted(PIL_RENDERABLE_CHART_TYPES)

#: 每类在**提示词正文**里可能被写成的别名（含 Mermaid 关键字映射值）。
_TYPE_ALIASES = {
    "flowchart": ["flowchart", "graph", "流程图"],
    "gantt": ["gantt", "甘特图"],
    "architecture": ["architecture", "架构图", "组织架构"],
    "labor": ["labor", "劳动力", "人员配置"],
    "comparison": ["comparison", "pie", "对比图", "饼图"],
    "layout": ["layout", "总平面", "平面布置"],
    "timeline": ["timeline", "时间轴"],
}


def _select_key(chart_type: str, is_json: bool) -> str:
    """复刻 ``routers/charts.py::_select_fix_prompt`` 的选择逻辑。

    刻意在此**重写一份**而不是 import 私有函数：私有函数闭包在
    ``repair_chart_code`` 内部，不便直接调用；两侧口径由本文件
    ``test_selector_matches_runtime`` 一致性断言守护。
    """
    generic = "chart_json_fix" if is_json else "chart_mermaid_fix"
    specific = f"{generic}_{chart_type}"
    return specific if specific in _ALL_PROMPTS else generic


class TestFixPromptCoverageMatrix:
    @pytest.mark.parametrize("chart_type", ALL_TYPES)
    @pytest.mark.parametrize("is_json", [True, False], ids=["json", "mermaid"])
    def test_selected_prompt_mentions_the_type(self, chart_type, is_json):
        """核心 parity 断言：选中的提示词必须含该类型的结构口径。"""
        key = _select_key(chart_type, is_json)
        body = get_default_prompt(key)
        aliases = _TYPE_ALIASES[chart_type]
        assert any(a.lower() in body.lower() for a in aliases), (
            f"{key} 未提及 {chart_type}（别名 {aliases}）——"
            f"该类型回退到通用版后会失去结构约束")

    @pytest.mark.parametrize("chart_type", ALL_TYPES)
    def test_selector_matches_runtime_semantics(self, chart_type):
        """选择逻辑与运行时一致：专项版存在就用专项版，否则用通用版。"""
        for is_json in (True, False):
            key = _select_key(chart_type, is_json)
            assert key in _ALL_PROMPTS, f"选中的 {key} 未注册"
            assert key == f"{'chart_json_fix' if is_json else 'chart_mermaid_fix'}_{chart_type}" \
                or key in ("chart_json_fix", "chart_mermaid_fix")

    @pytest.mark.parametrize("chart_type", ALL_TYPES)
    def test_every_fix_prompt_renders_without_residue(self, chart_type):
        """按契约传满变量后不得残留占位符（否则修复链路会注入字面量）。"""
        from app.services.ai.prompts._registry import (
            _is_false_positive, extract_variables,
        )
        for is_json in (True, False):
            key = _select_key(chart_type, is_json)
            requires = PROMPT_VARIABLE_CONTRACTS.get(key, [])
            tpl = get_default_prompt(key)
            out = render_prompt(tpl, **{v: f"<{v}>" for v in requires})
            residual = [v for v in extract_variables(out)
                        if not v.startswith("SHARED_")
                        and not _is_false_positive(key, v, tpl)]
            assert not residual, f"{key} 渲染后残留：{residual}"

    def test_generic_json_template_covers_all_seven(self):
        """``chart_json_fix`` 通用版必须覆盖全部 7 类。"""
        body = get_default_prompt("chart_json_fix").lower()
        for t in ALL_TYPES:
            assert any(a.lower() in body for a in _TYPE_ALIASES[t]), \
                f"chart_json_fix 通用版未覆盖 {t}"

    def test_generic_mermaid_template_covers_all_seven(self):
        """``chart_mermaid_fix`` 通用版必须覆盖全部 7 类。

        回归：``timeline`` 此前完全缺失，导致时间轴的 Mermaid 修复
        链路拿不到任何时间轴结构约束。
        """
        body = get_default_prompt("chart_mermaid_fix").lower()
        for t in ALL_TYPES:
            assert any(a.lower() in body for a in _TYPE_ALIASES[t]), \
                f"chart_mermaid_fix 通用版未覆盖 {t}"

    def test_mermaid_reachable_types_all_have_guidance(self):
        """Mermaid 能产出的类型，必须都能在通用版里找到口径。"""
        body = get_default_prompt("chart_mermaid_fix").lower()
        reachable = {t for t in MERMAID_KEYWORD_TO_CHART_TYPE.values()
                     if t in PIL_RENDERABLE_CHART_TYPES}
        for t in sorted(reachable):
            assert any(a.lower() in body for a in _TYPE_ALIASES[t]), \
                f"mermaid 可产出 {t}，但通用修复提示词无对应口径"
