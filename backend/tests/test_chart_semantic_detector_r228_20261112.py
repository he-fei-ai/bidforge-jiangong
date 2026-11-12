"""R228（2026-11-12）· 图表生成与渲染模块 · 程序侧语义检测器收口护栏。

**背景**：`chart_validators` 的 `detect_chart_type_from_text` / `detect_chart_with_metadata`
/ `extract_matched_keywords` / `trigger_match` 是 v3.1 规范「触发条件关键词库」的残留
（2026-09-14 移除手动图表编排管线后**全仓零调用点、零测试覆盖**）。本轮裁定：

  P1 · 修复「零分误判」：`detect_chart_with_metadata` 在 `top_score == 0`（无任何
       关键词/句式命中）时仍返回 `results[0][0]` —— 按类型名升序排序后恒为
       `architecture`。探针实测：`安全教育培训`/`项目概况与建设目标`/`主要施工方案比选`
       等排除/未命中场景全部误判为架构图（score=0）。修复为「零分 → top_chart=None」，
       与 `input_coverage` 的「宁漏报不误报」哲学一致。

本文件锁定下列不变式（护栏锁意图、不锁实现）：
  A 零分 → top_chart is None（不返回首键 architecture）
  B 正分场景仍正确判定（gantt / architecture / labor / layout）
  C detect_chart_type_from_text 全零分 = 无匹配（列表所有分数为 0）
  D 排除场景（背景/目标/方针叙述类）→ top_chart is None
"""

from __future__ import annotations

import json

import pytest
from app.routers import _chart_pipeline as CP
from app.services import chart_validators as CV

# ===========================================================================
# A · 零分 → 不匹配（修复点：旧实现返回首键 architecture）
# ===========================================================================


def test_zero_score_returns_none():
    """无任何命中（纯背景叙述）→ top_chart 必须为 None，不得返回首键。"""
    meta = CV.detect_chart_with_metadata("安全教育培训与文明施工管理")
    assert meta["top_score"] == 0, "排除场景应零分"
    assert meta["top_chart"] is None, "零分必须判不匹配（旧实现误判 architecture）"


def test_zero_score_general_project():
    """项目概况/建设目标（明确排除场景）→ 不匹配。"""
    meta = CV.detect_chart_with_metadata("项目概况与建设目标")
    assert meta["top_chart"] is None


def test_zero_score_comparison_only_text():
    """「方案比选」若当前关键词/句式零命中 → 不得落到 architecture。"""
    meta = CV.detect_chart_with_metadata("主要施工方案比选")
    # 修复后：未命中即 None；若命中 comparison 则 score>0 且类型正确
    if meta["top_score"] == 0:
        assert meta["top_chart"] is None
    else:
        assert meta["top_chart"] == "comparison"


# ===========================================================================
# B · 正分场景仍正确判定
# ===========================================================================


def test_positive_gantt_still_detected():
    meta = CV.detect_chart_with_metadata("施工进度计划与工期安排")
    assert meta["top_score"] >= 1
    assert meta["top_chart"] == "gantt"


def test_positive_architecture_still_detected():
    meta = CV.detect_chart_with_metadata("项目管理组织机构及岗位职责")
    assert meta["top_score"] >= 1
    assert meta["top_chart"] == "architecture"


def test_positive_labor_still_detected():
    meta = CV.detect_chart_with_metadata("劳动力配置计划")
    assert meta["top_score"] >= 1
    assert meta["top_chart"] == "labor"


def test_positive_layout_still_detected():
    meta = CV.detect_chart_with_metadata("施工总平面布置")
    assert meta["top_score"] >= 1
    assert meta["top_chart"] == "layout"


# ===========================================================================
# C · detect_chart_type_from_text 全零分 = 无匹配
# ===========================================================================


def test_type_list_zero_score_all_zero():
    """列表形式：无命中时所有分数为 0（调用方据此判不匹配）。"""
    results = CV.detect_chart_type_from_text("项目概况与建设目标")
    assert results, "应返回全部类型（含 0 分）"
    assert all(score == 0 for _, score in results), "无命中时不得有任何正分"


def test_type_list_positive_score():
    results = CV.detect_chart_type_from_text("施工进度计划与工期安排")
    top_type, top_score = results[0]
    assert top_score >= 1
    assert top_type == "gantt"


# ===========================================================================
# D · 排除场景矩阵
# ===========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "项目概况与建设目标",           # 背景叙述
        "质量方针与管理目标",            # 方针叙述
        "安全教育培训",                  # 纯管理性
        "文明施工管理措施",              # 管理性（无工序/数据）
        "绿色施工与环境保护",            # 无量化图表数据
    ],
)
def test_exclusion_scenarios_no_match(text):
    """排除场景（背景/目标/方针/纯管理）→ 零分或 None，绝不误判为 architecture。"""
    meta = CV.detect_chart_with_metadata(text)
    assert meta["top_chart"] is None, f"{text!r} 应判不匹配，实际 {meta['top_chart']}"


# ===========================================================================
# E · 接线：生成后语义交叉校验（build_inline_chart_plan · 观测不干预）
# ===========================================================================

_LABOR = json.dumps(
    {
        "type": "labor",
        "title": "劳动力配置计划",
        "fig_no": "",
        "phases": ["施工准备", "基础工程", "主体工程", "装饰装修", "竣工验收"],
        "categories": ["普工", "钢筋工", "木工", "混凝土工", "电工", "管理人员"],
        "data": [[15, 5, 3, 2, 1, 6], [25, 20, 15, 10, 2, 8], [35, 30, 25, 20, 5, 12],
                 [20, 10, 12, 8, 10, 10], [10, 2, 2, 2, 5, 6]],
    },
    ensure_ascii=False,
)

_FLOW = "flowchart LR\nA[\"基层清理\"] --> B[\"测量放线\"]\nB --> C[\"一体板粘贴及锚固\"]\n"

_SCHEDULE_TEXT = (
    "施工进度计划与工期安排\n"
    "本工程总工期 180 天，划分为施工准备、基础工程、主体工程、装饰装修、竣工验收五个阶段，"
    "关键线路为地基基础→主体结构→装饰装修。\n"
    "劳动力配置如下图所示：\n"
)

_PROCESS_TEXT = (
    "外墙保温装饰一体板施工工艺流程\n"
    "本工程采用外墙保温装饰一体板工艺，施工工序为基层清理、测量放线、一体板粘贴及锚固、质量检查验收。\n"
    "施工工艺流程如下图所示：\n"
)


def test_semantic_context_extracts_lead_in():
    """_chart_semantic_context 提取图表块前 3 个非空正文段（含引导语）。"""
    content = _SCHEDULE_TEXT + "```chart-json\n" + _LABOR + "\n```\n"
    ctx = CP._chart_semantic_context(content, 0)
    assert "施工进度计划与工期安排" in ctx
    assert "劳动力配置如下图所示" in ctx  # 引导语被提取
    assert "```" not in ctx  # 不含围栏本身


def test_semantic_crosscheck_mismatch_observes():
    """进度章节（应 gantt）塞 labor 图 → 观测 detected=gantt，且块**不被删**。"""
    content = _SCHEDULE_TEXT + "```chart-json\n" + _LABOR + "\n```\n"
    notes: list = []
    fixed, rows = CP.build_inline_chart_plan(
        "scheme-a", "section-a", content, semantic_notes_out=notes
    )
    assert len(rows) == 1, "语义不匹配不得删块（观测不干预）"
    assert notes, "应产生语义交叉校验观测"
    assert notes[0]["type"] == "labor"
    assert notes[0]["detected"] == "gantt"
    assert notes[0]["score"] >= 1
    assert fixed == content, "正文不得被改写"


def test_semantic_crosscheck_match_no_note():
    """工艺章节配 flowchart（匹配）→ 无观测。"""
    content = _PROCESS_TEXT + "```mermaid\n" + _FLOW + "```\n"
    notes: list = []
    fixed, rows = CP.build_inline_chart_plan(
        "scheme-b", "section-b", content, semantic_notes_out=notes
    )
    assert len(rows) == 1
    assert not notes, "匹配场景不应产生观测"


def test_semantic_crosscheck_default_none_noop():
    """缺省 semantic_notes_out=None → 零行为变化（与旧实现逐字等价）。"""
    content = _SCHEDULE_TEXT + "```chart-json\n" + _LABOR + "\n```\n"
    _none_fixed, none_rows = CP.build_inline_chart_plan("scheme-a", "section-a", content)
    notes: list = []
    _obs_fixed, obs_rows = CP.build_inline_chart_plan(
        "scheme-a", "section-a", content, semantic_notes_out=notes
    )
    # rows 首列是随机 UUID，比较其余字段（登记内容必须逐字一致）
    assert [r[1:] for r in none_rows] == [r[1:] for r in obs_rows], (
        "传入观测列表不得改变登记结果"
    )
    assert _none_fixed == _obs_fixed


def test_semantic_context_ordinal_aligns():
    """多图表块时 ordinal 正确对齐（第 2 个块取其自身上下文）。"""
    content = (
        "施工进度计划与工期安排\n"
        "总工期 180 天，五个阶段。\n"
        "进度计划如下图所示：\n"
        "```chart-json\n" + _LABOR + "\n```\n"
        "劳动力配置计划\n"
        "各工种人数如下。\n"
        "```chart-json\n" + _LABOR + "\n```\n"
    )
    # 第 2 个块（ordinal=1）的上下文应含「劳动力配置计划」
    ctx1 = CP._chart_semantic_context(content, 1)
    assert "劳动力配置计划" in ctx1
    assert "进度计划如下图所示" not in ctx1
