# -*- coding: utf-8 -*-
"""未闭合图表围栏「三侧口径收口」护栏（2026-10-03 · 未闭合=不是图）

覆盖本轮修复的三个缺陷：
  FIX-A 登记侧 `_scan_chart_fences_full` 此前只跳 truncated 不跳 eof ——
        EOF 截断残片被登记进 chart_predictions（幽灵登记：清单里有、成稿里
        没有、绕过每章≤1/同类型≤3 配图上限）；导出解析侧 chart-json /
        ai_image 分支此前**不检查闭合状态**，合法 JSON 残片照样渲染占号，
        违背 read_fenced_block 文档承诺「eof/truncated 走未闭合降级」。
        现三侧统一：未闭合（eof/truncated）一律不是图。
  FIX-B 解析期跳过的图表围栏（未闭合 / 类型不可识别 / JSON 不可渲染）
        同步回收孤儿引导语（"…如下图所示："），与 export._pop_orphan_lead_in、
        _chart_pipeline._drop_dangling_lead_in 同口径，消除"见下图却无图"。
  防御 `_apply_chart_fence_edits` 对 eof 序号原样保留 —— 即使调用方误传
        序号，也不得借修复之名给 EOF 残片补写闭合围栏（洗白成合法图表）。

口径对照（同一份正文，登记数 == 导出可渲染 chart 块数）由 TestThreeSideParity 锁定。
"""
import ast

import pytest

from app.routers._chart_pipeline import (
    _apply_chart_fence_edits,
    _scan_chart_fences_full,
    iter_inline_chart_fences,
)
from app.services.content_blocks import _parse_content_blocks


# ---------------------------------------------------------------------------
# 测试素材：三类图表围栏的「闭合」与「EOF 未闭合」形态
# ---------------------------------------------------------------------------
MERMAID_OK = "flowchart TD\n  A[开工] --> B[验收]"
CHART_JSON_OK = '{"type": "labor", "title": "劳动力配置", "phases": [], "categories": []}'
AI_IMAGE_OK = '{"prompt": "基坑剖面示意图", "title": "基坑剖面"}'

LEAD_IN = "施工工艺流程如下图所示："  # 典型孤儿引导语（≤60 字、以引导词收尾）


def _mermaid_eof():
    """mermaid 围栏直到文档末尾都没有闭合行（max_tokens 截断的真实形态）。"""
    return f"{LEAD_IN}\n```mermaid\nflowchart TD\n  A[开工] --> B{{"


def _chart_json_eof():
    """chart-json 残片本身是**完整合法 JSON**，只缺闭合围栏行。

    旧导出解析只看「JSON 可解析 + 类型白名单」→ 这种块照样产出渲染占用图号，
    而登记侧修复后不认它 → 本用例正是新旧行为差异的分界样本。
    """
    return f"{LEAD_IN}\n```chart-json\n{CHART_JSON_OK}"


def _ai_image_eof():
    return f"{LEAD_IN}\n```ai_image\n{AI_IMAGE_OK}"


def _chart_types_in_parse(content: str) -> list[str]:
    return [b["chart_type"] for b in _parse_content_blocks(content)
            if b.get("type") == "chart"]


# ---------------------------------------------------------------------------
# FIX-A · 登记侧：eof 与 truncated 同样不提取
# ---------------------------------------------------------------------------
class TestRegistrationSkipsEof:

    @pytest.mark.parametrize("factory", [_mermaid_eof, _chart_json_eof, _ai_image_eof])
    def test_eof_fragment_not_registered(self, factory):
        assert _scan_chart_fences_full(factory()) == []

    def test_closed_blocks_still_registered(self):
        content = f"{LEAD_IN}\n```mermaid\n{MERMAID_OK}\n```"
        assert [ct for ct, _c, _o in _scan_chart_fences_full(content)] == ["flowchart"]

    def test_truncated_still_skipped(self):
        """truncated（超长且窗口内无干净闭合）跳过是既有行为，收口不得放宽。"""
        big = "%% pad\n" * 1100  # 超 500 上限且前视窗口（再 500 行）内无闭合 → truncated
        content = f"```mermaid\nflowchart TD\n{big}```"
        states = [st for _l, _c, st, _o in iter_inline_chart_fences(content)]
        assert "truncated" in states
        assert _scan_chart_fences_full(content) == []


# ---------------------------------------------------------------------------
# FIX-A · 导出解析侧：三家族 eof 残片一律不产出（不渲染、不占图号）
# ---------------------------------------------------------------------------
class TestExportParseSkipsEof:

    def test_mermaid_eof_no_chart(self):
        assert _chart_types_in_parse(_mermaid_eof()) == []

    def test_chart_json_eof_no_chart_even_when_json_is_valid(self):
        """核心回归：JSON 完整可解析 ≠ 可以渲染 —— 未闭合就是未闭合。"""
        assert _chart_types_in_parse(_chart_json_eof()) == []

    def test_ai_image_eof_no_block(self):
        assert not any(b["type"] == "ai_image"
                       for b in _parse_content_blocks(_ai_image_eof()))

    def test_closed_chart_json_still_parsed(self):
        content = f"{LEAD_IN}\n```chart-json\n{CHART_JSON_OK}\n```"
        blocks = _parse_content_blocks(content)
        charts = [b for b in blocks if b.get("type") == "chart"]
        assert len(charts) == 1
        assert charts[0]["chart_type"] == "labor"
        # 闭合块的引导语**不得**被回收（它是合法图题候选）
        assert any(b.get("type") == "paragraph" and b.get("text") == LEAD_IN
                   for b in blocks)

    def test_recovered_long_but_closed_still_parsed_both_sides(self):
        """「超长但闭合」（recovered）仍视为闭合：登记与导出两侧照旧放行。"""
        pad = "".join(f"  n{k}[节点{k}] ;\n" for k in range(600))
        code = f"flowchart TD\n{pad}"
        content = f"```mermaid\n{code}\n```"
        assert [ct for ct, _c, _o in _scan_chart_fences_full(content)] == ["flowchart"]
        assert _chart_types_in_parse(content) == ["flowchart"]


# ---------------------------------------------------------------------------
# FIX-B · 解析期跳过 → 孤儿引导语回收（与渲染期 _pop_orphan_lead_in 同口径）
# ---------------------------------------------------------------------------
class TestLeadInReclaimedOnParseSkip:

    def test_mermaid_unknown_type_pops_lead_in(self):
        """闭合但首关键字不在映射表 → 跳过渲染（登记侧同口径不登记），引导语一并回收。"""
        content = f"{LEAD_IN}\n```mermaid\nunknownDiagram TD\n  A --> B\n```"
        blocks = _parse_content_blocks(content)
        assert _chart_types_in_parse(content) == []
        assert not any(b.get("type") == "paragraph" and b.get("text") == LEAD_IN
                       for b in blocks)

    def test_chart_json_unrenderable_pops_lead_in(self):
        """闭合但 JSON 不可解析 → 跳过，引导语回收（旧行为会留下悬空"见下图"）。"""
        content = f"{LEAD_IN}\n```chart-json\n{{不是合法 JSON\n```"
        blocks = _parse_content_blocks(content)
        assert _chart_types_in_parse(content) == []
        assert not any(b.get("type") == "paragraph" and b.get("text") == LEAD_IN
                       for b in blocks)

    def test_eof_skip_pops_lead_in(self):
        for factory in (_mermaid_eof, _chart_json_eof, _ai_image_eof):
            blocks = _parse_content_blocks(factory())
            assert not any(b.get("type") == "paragraph" and b.get("text") == LEAD_IN
                           for b in blocks), factory.__name__

    def test_long_sentence_before_skipped_fence_not_popped(self):
        """超过 60 字的正文长句不是引导语，绝不回收（判据与 _lead_in_title 同源）。"""
        long_text = "为确保本次深基坑开挖支护施工全过程的安全与质量，并兼顾工期约束，" \
                    "项目部依据设计图纸与现场踏勘结果，对施工工艺流程进行了反复论证，最终确定如下图所示："
        assert len(long_text) > 60
        content = f"{long_text}\n```mermaid\nflowchart TD\n  A{{"
        blocks = _parse_content_blocks(content)
        assert any(b.get("type") == "paragraph" for b in blocks)

    def test_heading_before_skipped_fence_not_popped(self):
        """前一块是标题（结构元素）时不回收其后任何内容。"""
        content = f"# 第三章 施工工艺\n```mermaid\nflowchart TD\n  A{{"
        blocks = _parse_content_blocks(content)
        assert any(b.get("type") == "heading" for b in blocks)

    def test_body_after_unclosed_fence_survives(self):
        """未闭合围栏**之后**的正文还原不受回收影响（v13 承诺不回归）。"""
        # 无闭合围栏行 → state=eof；残片中首个句读行起归还给段落解析
        content = (f"{LEAD_IN}\n```chart-json\n{{\"type\": \"labor\",\n"
                   f"本章节其余说明文字照常成段。\n后续段落也是正文。\n")
        blocks = _parse_content_blocks(content)
        texts = " ".join(str(b.get("text", "")) for b in blocks)
        assert "后续段落也是正文" in texts
        assert _chart_types_in_parse(content) == []
        assert not any(b.get("type") == "paragraph" and b.get("text") == LEAD_IN
                       for b in blocks)


# ---------------------------------------------------------------------------
# 防御 · 改写侧不得借修复之名给 EOF 残片补闭合围栏
# ---------------------------------------------------------------------------
class TestEditsNeverWhitewashEof:

    def test_apply_edits_keeps_eof_fragment_untouched(self):
        content = _mermaid_eof()
        # 即使调用方误传序号 0（修复/删除），eof 块也必须原样回写
        assert _apply_chart_fence_edits(content, {0: "flowchart TD\n  A --> B"}) == content
        assert _apply_chart_fence_edits(content, {0: None}) == content

    def test_apply_edits_still_rewrites_closed_block(self):
        content = f"```mermaid\n{MERMAID_OK}\n```"
        out = _apply_chart_fence_edits(content, {0: "flowchart TD\n  A --> Z"})
        assert "A --> Z" in out and out.count("```") == 2


# ---------------------------------------------------------------------------
# 三侧 parity · 同一份正文：登记类型集 == 导出可渲染 chart 类型集
# ---------------------------------------------------------------------------
class TestThreeSideParity:

    @pytest.mark.parametrize("content_factory,expected", [
        (_mermaid_eof, []),
        (_chart_json_eof, []),
        (_ai_image_eof, []),
    ], ids=["mermaid-eof", "chart-json-eof", "ai_image-eof"])
    def test_unclosed_both_sides_disagree_on_nothing(self, content_factory, expected):
        registered = [ct for ct, _c, _o in _scan_chart_fences_full(content_factory())]
        parsed = _chart_types_in_parse(content_factory())
        assert registered == parsed == expected

    def test_registered_chart_always_parseable_by_export(self):
        """不变量：凡被登记的（非 ai_image）块，导出解析必须能产出对应 chart 块
        —— 反之亦然，杜绝任何一侧单独放行造成的幽灵图/漏渲染。"""
        samples = [
            f"```mermaid\n{MERMAID_OK}\n```",
            f"```chart-json\n{CHART_JSON_OK}\n```",
            _mermaid_eof(),
            _chart_json_eof(),
            f"```mermaid\nunknownDiagram TD\n  A --> B\n```",
        ]
        for content in samples:
            registered = {ct for ct, _c, _o in _scan_chart_fences_full(content)}
            registered.discard("ai_image")  # ai_image 导出走独立分支，不参与 chart parity
            parsed = set(_chart_types_in_parse(content))
            assert registered == parsed, content


# ---------------------------------------------------------------------------
# 静态锁 · 收口不得被静默摘除（行为用例的判据若被改回，这里先于人工发现）
# ---------------------------------------------------------------------------
class TestStaticGuards:

    @staticmethod
    def _source(rel_path: str) -> str:
        import pathlib
        import app
        base = pathlib.Path(str(app.__file__)).parent
        return (base / rel_path).read_text(encoding="utf-8")

    def test_pipeline_scan_skips_truncated_and_eof(self):
        src = self._source("routers/_chart_pipeline.py")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_scan_chart_fences_full")
        cmp_src = [ast.unparse(n) for n in ast.walk(fn)
                   if isinstance(n, ast.Compare)]
        assert any("truncated" in s and "eof" in s for s in cmp_src), \
            "_scan_chart_fences_full 必须同时跳过 truncated 与 eof（未闭合=不是图）"

    def test_parse_content_blocks_checks_fence_closed_in_all_chart_branches(self):
        src = self._source("services/content_blocks.py")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_parse_content_blocks")
        body_src = ast.unparse(fn)
        # 1 处通用守卫（告警+正文还原）+ 三个图表家族分支各自的闭合守卫
        assert body_src.count("if not _fence_closed") >= 4, \
            "mermaid / chart-json / ai_image 三分支的闭合检查一个都不能少"

    def test_exporter_version_bumped_for_layout_change(self):
        """引导语回收/未闭合口径属版式变化，必须烤进导出缓存版本号失效旧缓存。"""
        src = self._source("routers/export.py")
        m = ast.parse(src)
        val = next(
            node.value.value for node in ast.walk(m)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name) and t.id == "_EXPORTER_VERSION"
        )
        assert int(val) >= 21, "v21：未闭合围栏口径收口必须使旧导出缓存整体失效"
