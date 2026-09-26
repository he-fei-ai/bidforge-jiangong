# -*- coding: utf-8 -*-
"""图表类型推荐：按数据形态与缺口意图选定最终渲染类型。

规则优先级：
1. 数据形态（时间序 > 百分比构成 > 多系列对比 > 单系列比较）；
2. 检测阶段的意图（flow/relation/timeline 等结构性图形优先）；
3. 数据缺失时降级为示意结构图（流程/层级）并标记。
"""

from __future__ import annotations

import re

from ..models import ChartData, ChartGap, ChartSpec

_TIME_RE = re.compile(r"\d{1,2}月|\d{4}[年/-]|第[一二三四五六七八九十\d]+(?:期|阶段|层|天)|周[一二三四五六日]|\d+~\d+m|±0\.00")


def _looks_temporal(categories: list[str]) -> bool:
    hit = sum(1 for c in categories if _TIME_RE.search(c or ""))
    return bool(categories) and hit / len(categories) >= 0.5


def _is_pct(data: ChartData) -> bool:
    """数值疑似占比：单系列且总和接近 100。"""
    ns = data.numeric_series()
    if len(ns) != 1 or not ns[0].values:
        return False
    total = sum(ns[0].values)
    return len(ns[0].values) <= 8 and 85 <= total <= 115


def recommend_type(data: ChartData | None, hint: str) -> str:
    """结合数据形态与检测提示给出类型。"""
    structural = {"flow", "relation", "timeline"}
    if data is None:
        return hint if hint in structural else "flow"
    shape = "bar"
    if _looks_temporal(data.categories) and hint in ("", "line", "bar", "timeline"):
        shape = "line" if len(data.categories) >= 3 else "timeline"
    elif _is_pct(data) and hint in ("", "pie", "bar"):
        shape = "pie"
    elif len(data.numeric_series()) >= 2:
        shape = "bar"
    elif hint in ("line", "pie", "scatter") and len(data.categories) >= 2:
        shape = hint
    else:
        shape = "bar"
    # 检测器明确给出结构性意图且数据牵强时，尊重意图
    if hint in structural and (not data.numeric_series() or not data.complete):
        return hint
    return shape


def build_spec(gap: ChartGap, data: ChartData | None, doc_title: str) -> ChartSpec | None:
    """缺口 -> 完整渲染配置；返回 None 表示无法生成（由上层记录原因）。"""
    ctype = recommend_type(data, gap.suggested_type)
    if data is None and not gap.steps:
        return None
    if data is not None and not data.numeric_series() and not gap.steps:
        return None
    title = gap.caption.replace("（AI 补全）", "").strip() or doc_title
    if data is not None and not title:
        title = data.title or "数据可视化"
    # 结构性图（流程/关系）在数据缺失或有步骤时优先走 steps 路径
    if ctype in ("flow", "relation"):
        # 无文本步骤时退而用分类标签拼结构（系列名只是表头，作为步骤是无意义内容）；
        # 少于 2 个节点无法成图，宁可跳过不产半成品
        steps = gap.steps or list(data.categories if data else [])
        if len(steps) < 2:
            return None
        return ChartSpec(chart_type=ctype, title=title, steps=steps[:10],
                         data=data if gap.suggested_type not in ("flow", "relation") else None,
                         note="" if steps == gap.steps else "结构据文本推断，供核对")
    if data is None:
        return None
    return ChartSpec(chart_type=ctype, title=title, data=data,
                     note="" if data.complete else "部分数据待补充，图中以 0 占位显示")
