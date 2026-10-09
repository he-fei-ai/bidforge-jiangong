# -*- coding: utf-8 -*-
"""R49（2026-10-06 · 图表模块遗留收口）单测。

本轮三件事，全部**加法式**（零新依赖、零新配置项、零数据迁移）：

1. **口径漂移消除（改文案不改行为）**：提示词原写「每章合计最多 1 个图表块」，
   而代码按 **per-section_id（叶子小节）** 执行（一个一级章挂 N 个二级小节可各
   出 1 张）。实测生产路径已传 `enforce_limits=True`，行为本身正确 —— 漂移只在
   文档。现把提示词与代码注释统一到「本节」口径，零行为变化。
   ⚠️ 刻意**不**改为一级章计数：那需解析 sections.parent_id 树根祖先，而
   `build_inline_chart_plan` 是无 DB 访问的纯计算（锁外执行），且收紧会删掉
   当前合法的图表（属行为变更）。
2. **图表产出闭环**：新增 `count_inline_charts` 作**计数唯一出口**
   （`has_inline_charts` 是布尔，`extract_inline_charts` 按类型去重，都不能当计数）。
   `chart_count` 落 `last_generation_report` 并随 `section_done` 实时下发，与
   `charts_dropped_count` 配对即得「AI 提名 ≈ 保留 + 删除」的完整口径。
3. **修 R48 遗留缺陷**：`_record_chart_drop` 写入的键是 `"type"`，而 sse_handlers
   的删图日志用 `d.get("chart_type", "?")` 读取 → 图表类型**恒打印 "?"**。现收敛为
   唯一读出口 `chart_drop_label`，读侧与写侧同源。

⚠️ **未落地（记录以免重复排查）**：「本应配图但 AI 零产出」的检测**不做** ——
它需要一份「标题→配图相关性」判据，而该判据目前**只存在于提示词散文里**，抽成
代码常量就是本仓反复踩的「同一判据多副本」反模式（R48 引导语第 3 份副本同族）。
需要它时应先在提示词层收敛出唯一来源，再考虑落代码。
"""
import re

import pytest

import app.routers._chart_pipeline as CP
import app.routers.sse_handlers as SH
import app.services.ai.prompts.content as CONTENT_PROMPTS
from app.routers._chart_pipeline import (
    _CHART_PER_SECTION_LIMIT,
    chart_drop_label,
    count_inline_charts,
    extract_inline_charts,
    has_inline_charts,
)

# =========================================================================
# A. count_inline_charts —— 图表计数的唯一出口
# =========================================================================

MERMAID = "```mermaid\nflowchart TD\n    A --> B\n```"
CHART_JSON = '```chart-json\n{"type": "gantt", "title": "进度"}\n```'
AI_IMAGE = '```ai_image\n{"prompt": "剖面图", "title": "剖面"}\n```'
MERMAID_TRUNCATED = "```mermaid\nflowchart TD\n    A --> B"  # 未闭合


class TestCountInlineCharts:

    @pytest.mark.parametrize("content,expected", [
        ("", 0),
        ("纯文本，没有图表", 0),
        (MERMAID, 1),
        (CHART_JSON, 1),
        (AI_IMAGE, 1),
        # 同类型多个块也逐块计数（extract_inline_charts 会去重成 1，故不可当计数用）
        (MERMAID + "\n\n正文\n\n" + MERMAID, 2),
        # 不同族块同时存在
        (MERMAID + "\n\n正文\n\n" + CHART_JSON, 2),
    ])
    def test_counts_each_closed_block(self, content, expected):
        assert count_inline_charts(content) == expected

    def test_unclosed_fences_not_counted(self):
        # 「未闭合 = 不是图」是登记/导出/改写/判定/计数五处统一口径。
        assert count_inline_charts(MERMAID_TRUNCATED) == 0
        assert has_inline_charts(MERMAID_TRUNCATED) is False

    def test_zero_over_empty_text(self):
        # fail-soft：空串返回 0，不抛异常。
        assert count_inline_charts("") == 0

    def test_counts_three_family_types(self):
        content = MERMAID + "\n\n正文\n\n" + CHART_JSON + "\n\n正文\n\n" + AI_IMAGE
        assert count_inline_charts(content) == 3


class TestHasInlineChartsParity:

    """`has_inline_charts(c) ⟺ count_inline_charts(c) > 0` 必须恒成立。

    R49 前两个出口各自遍历 `iter_inline_chart_fences`，存在分叉风险；
    现 `has_inline_charts` 是 `count_inline_charts` 的薄包装，语义必然一致。
    """

    @pytest.mark.parametrize("content", [
        "", "纯文本", MERMAID, CHART_JSON, AI_IMAGE,
        MERMAID_TRUNCATED,
        MERMAID + "\n\n" + MERMAID_TRUNCATED,
        MERMAID + "\n\n正文\n\n" + CHART_JSON,
    ])
    def test_bool_and_count_agree(self, content):
        assert has_inline_charts(content) is (count_inline_charts(content) > 0)

    def test_count_is_not_deduped_unlike_extract(self):
        # extract_inline_charts 按 chart_type 去重（登记清单口径），
        # count_inline_charts 不去重（产出计数口径）—— 两者用途不同，不得混用。
        content = MERMAID + "\n\n正文\n\n" + MERMAID
        assert len(extract_inline_charts(content)) == 1
        assert count_inline_charts(content) == 2


# =========================================================================
# B. chart_drop_label —— 修 R48 遗留缺陷（日志图表类型恒为 "?"）
# =========================================================================

class TestChartDropLabel:

    def test_record_shape_from_record_chart_drop(self):
        # 真实写入形态：键名是 "type"（不是 chart_type）。
        dropped = []
        CP._record_chart_drop(dropped, "flowchart", "per_section_limit")
        assert dropped[0] == {"type": "flowchart", "reason": "per_section_limit"}
        assert chart_drop_label(dropped[0]) == "flowchart/per_section_limit"

    def test_legacy_chart_type_key_also_works(self):
        assert chart_drop_label(
            {"chart_type": "gantt", "reason": "invalid_json"}) == "gantt/invalid_json"

    def test_type_key_takes_precedence(self):
        # 两个键都在时优先 "type"（与写入侧一致），不得被兜底键覆盖。
        assert chart_drop_label(
            {"type": "mermaid", "chart_type": "layout", "reason": "x"}) == "mermaid/x"

    @pytest.mark.parametrize("drop,expected", [
        (None, "?/?"),
        ({}, "?/?"),
        ({"type": "", "reason": ""}, "?/?"),
        ({"type": "   ", "reason": "  "}, "?/?"),
        ({"type": "gantt"}, "gantt/?"),
        ({"reason": "insert_failed"}, "?/insert_failed"),
        ({"type": None, "reason": None}, "?/?"),
    ])
    def test_missing_or_blank_values_fallback_to_question_mark(self, drop, expected):
        assert chart_drop_label(drop) == expected

    def test_all_drops_are_renderable(self):
        # 全枚举冒烟：任何真实删图记录都能渲出「类型/理由」，不丢信息、不抛异常。
        for reason in ("per_section_limit", "scheme_type_limit", "validation_failed",
                       "missing_prompt", "empty_envelope", "invalid_json",
                       "insert_failed", "insert_none"):
            label = chart_drop_label({"type": "labor", "reason": reason})
            assert label.startswith("labor/")
            assert label.endswith(reason)


# =========================================================================
# C. 提示词口径：「本节」而非「每章」（零行为变化的文案修正）
# =========================================================================

class TestPromptWordingParity:

    """提示词的配图限额单位必须是「本节」，与代码的 per-section_id 计数同口径。

    漂移后果：提示词说「每章 1 个」而代码按叶子小节执行 —— 一个一级章挂 N 个二级
    小节时，程序允许 N 张而提示词暗示 1 张。文档与行为不一致本身就是缺陷
    （R48 记录的「引导语判据第 3 份副本」同族）。

    判据锚点说明：图表围栏规则写在 `content_generation_system` 的注册文本里
    （`_reg(..., _wrap_fuzzy_fill(<triple-quoted literal>))`），运行时会被
    `_wrap_fuzzy_fill` 拼接，模块级常量扫不到。故直接读源码文本 —— 那正是被
    `_reg` 注册、最终下发给模型的那份文本（与 R48 对 sse_handlers 的静态锁同法）。
    """

    _SRC = open(CONTENT_PROMPTS.__file__, encoding="utf-8").read()

    def test_per_section_limit_says_benjie_not_meizhang(self):
        src = self._SRC
        assert "本节合计最多 1 个图表块" in src, (
            "提示词的配图限额单位必须写「本节」（= 代码的 per-section_id）")
        # 「每章」是漂移形态，不得回流。
        assert "每章合计最多 1 个图表块" not in src
        assert "每章 1 块" not in src

    def test_type_diversity_rule_also_uses_benjie(self):
        src = self._SRC
        assert "同一节一旦选择某类型" in src
        assert "同一章节一旦选择某类型" not in src

    def test_prompt_unit_matches_code_unit(self):
        # 口径锁：提示词写「本节」、代码按 section_id 计数、限额为 1。三者必须一致。
        assert _CHART_PER_SECTION_LIMIT == 1
        assert "本节合计最多 1 个图表块" in self._SRC
        assert "每个 section_id" in open(CP.__file__, encoding="utf-8").read()


# =========================================================================
# D. sse_handlers 接线（跨文件静态锁，防静默摘除）
# =========================================================================

_SSE_SRC = open(SH.__file__, encoding="utf-8").read()
_PIPELINE_SRC = open(CP.__file__, encoding="utf-8").read()


def test_sse_imports_the_chart_helpers():
    assert "count_inline_charts," in _SSE_SRC
    assert "chart_drop_label," in _SSE_SRC


def test_report_always_sets_chart_count():
    assert 'report["chart_count"]' in _SSE_SRC, (
        "chart_count 未写入 report —— 「留下了几张图」将再次不可见")


def test_section_done_carries_chart_yield():
    """`section_done` 事件必须实时下发三键 —— 用户不必等整批结束翻报告。"""
    i = _SSE_SRC.index('"event": "section_done"')
    block = _SSE_SRC[i:i + 3000]
    for key in ('"chart_count"', '"charts_dropped_count"', '"charts_dropped"'):
        assert key in block, (
            f"section_done 事件未携带 {key} —— 配图产出闭环将再次只在生成结束后可见")


def test_report_json_redumped_outside_the_drop_branch():
    """R49 起 `report_json` 的重算必须**在 `if _chart_dropped:` 之外**。

    R48 只在「有删图」时重算（chart_count 出现前没问题）；R49 新增恒有值的
    `chart_count` 后，若沿用条件重算它同样永远不会落库（加键但没生效）。
    """
    i = _SSE_SRC.index('report["chart_count"]')
    after = _SSE_SRC[i:]
    m = re.search(r"\n\s*report_json = json\.dumps\(report, ensure_ascii=False\)", after)
    assert m, "chart_count 写入 report 后未重新 json.dumps"
    snippet = after[:m.start()]
    drop_if = snippet.rfind("if _chart_dropped:")
    if drop_if != -1:
        inner = snippet[drop_if:]
        # 重算语句前的所有 report 赋值都必须不是更深缩进的（即不在 if 分支内）
        assert not re.search(r"\n\s{12,}report\[(\"chart_count\")\]", inner), (
            "report_json 重算被放回 `if _chart_dropped:` 内部 —— chart_count "
            "在无删图时不会落库")


def test_log_uses_chart_drop_label_not_bare_get():
    """删图日志必须走模块级 `chart_drop_label`，不得回退到裸 `d.get('chart_type')`。

    裸取会因键名不匹配（写入侧是 "type"）而恒打印 "?"。
    两个方向都要锁：① 裸 get 形态不得回流；② 不得写成模块内不存在的
    `_chart_drop_label`（带下划线会被解析成局部变量 → NameError）。
    """
    assert "d.get('chart_type'" not in _SSE_SRC, (
        "删图日志回退为裸 d.get('chart_type') —— 图表类型将再次恒打印 '?'")
    assert "_chart_drop_label(" not in _SSE_SRC, (
        "删图日志写成模块内不存在的 _chart_drop_label —— 运行期 NameError")
    assert "chart_drop_label(d)" in _SSE_SRC


def test_pipeline_has_single_drop_reader():
    # 读侧（chart_drop_label）与写侧（_record_chart_drop）必须共存于同一模块。
    assert "def chart_drop_label(" in _PIPELINE_SRC
    assert 'dropped_out.append({"type": chart_type, "reason": reason})' in _PIPELINE_SRC
