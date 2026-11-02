# -*- coding: utf-8 -*-
"""R164 · 图表模块修复护栏（2026-11-01）。

本次修复 / 优化三处，全部由**实测探针**先复现后修复（探针输出原文见
``artifacts/`` 下的一次性脚本，不随仓交付）：

1. **图题残字**（``services/content_blocks.py``）
   ``LEAD_IN_HINT_RE`` 备选顺序原为 ``…|见下图|…``，``re.sub`` 命中的是
   ``详见下图``里的子串 ``见下图``，剥完还剩一个悬空的「详」⇒ 引导语图题
   被写成 ``各项内容详``（成稿即「图 3-1 各项内容详」）。
   现备选「长词在前」（``详见下图`` 最前）+ ``_LEAD_IN_TAIL_RE`` 兜底剥除
   悬空连接字（详/如/见/所/下/示/后）。
   ⚠️ 检测语义（``search`` + ``$``）必须与原来逐例等价 —— 否则「删图表时
   回收引导语」的判据会跟着变（一处删、一处留 ⇒ 新的悬空引用）。

2. **图题残留前导标点**（``routers/export.py::_strip_leading_figure_number``）
   ``FIG_TITLE_PREFIX_RE`` 只吃到「图 3-1」，源文冒号形态
   ``图 3-1：施工工艺流程`` 剥完前缀剩一个悬空冒号，成稿图题变成
   「图 2-1 ：施工工艺流程」。现前缀后紧跟的标点/空白一并剥除。
   判据仍是契约模块那一份（不在本文件复制第二份图号正则）。

3. **纸面主文字不足下限**（``services/ai/mermaid_gantt.py`` /
   ``services/ai/mermaid_labor.py``）
   纸面字号 ≈ 字号 × 160 / 画布逻辑宽（docx 按 16cm 宽等比插入，scale/DPI
   在闭式中被约掉），与条目数无关。旧值：甘特图 20px / 1900px → 1.68mm、
   劳动力图 13px / 1172px → 1.75mm，均低于全仓下限 2.4mm
   （``mermaid_common.PAPER_FONT_MIN_MM``）。现两者都收窄画布 + 抬字号。
   产物尺寸/外观已变 ⇒ ``_RENDERER_VERSION`` 必须 7→8，否则历史方案会继续
   从缓存拿旧图（修复在生产上等于没生效）。

本文件用**运行时捕获**（patch ``paper_floor_note`` 拿真实画布/字号）而不是
抄一份字号常量表做断言 —— 渲染器日后改几何，断言仍按同一公式重新判定。
"""
from __future__ import annotations

import json
import logging

import pytest
from app.services import content_blocks as cb
from app.services.ai import mermaid_common as ma
from app.services.ai import mermaid_gantt as gantt
from app.services.ai import mermaid_labor as labor
from PIL import Image

# ===========================================================================
# 1 · 引导语图题残字
# ===========================================================================

FENCE = "```mermaid\nflowchart TD\n  A-->B\n```"


def _chart_title(content: str) -> str | None:
    blocks = cb._parse_content_blocks(content)
    charts = [b for b in blocks if b.get("type") == "chart"]
    return charts[0].get("title") if charts else None


class TestLeadInCaptionResidue:
    """引导语图题：整词剥离，不得留悬空单字。"""

    def test_long_form_hint_strips_whole_word(self):
        """``各项内容详见下图：`` ⇒ 图题 ``各项内容``（修复前实测 ``各项内容详``）。"""
        assert _chart_title(f"各项内容详见下图：\n\n{FENCE}\n") == "各项内容"

    def test_short_form_hint_unchanged(self):
        """既有形态不得回退：``施工工艺流程如下图所示：`` ⇒ ``施工工艺流程``。"""
        assert _chart_title(f"施工工艺流程如下图所示：\n\n{FENCE}\n") == "施工工艺流程"

    def test_plain_hint_unchanged(self):
        assert _chart_title(f"施工工艺流程详见下图：\n\n{FENCE}\n") == "施工工艺流程"

    @pytest.mark.parametrize("line", [
        "各项内容详见下图：",
        "各项内容详见下图",
        "各项内容见下图：",
        "各项内容如下图所示：",
        "各项内容如下图：",
        "各项内容如图：",
        "各项内容图示：",
    ])
    def test_detector_matches_all_hint_forms(self, line):
        """检测判据（用于「删图表同步删引导语」）覆盖全部既有写法。

        备选重排只应影响 ``re.sub`` 的**替换长度**，不应影响 ``search`` 的命中
        结论 —— 这里逐例锁定。
        """
        assert cb.LEAD_IN_HINT_RE.search(line), line
        assert cb.ORPHAN_LEAD_IN_RE.search(line), line

    def test_detector_rejects_non_hint(self):
        """普通段落（不含图表提示词）绝不能被判成引导语 —— 否则会误删正文。"""
        for line in ["施工工艺流程共分三个阶段。", "配图如下：", "本章内容如下："]:
            assert not cb.LEAD_IN_HINT_RE.search(line), line


# ===========================================================================
# 2 · 图题残留前导标点
# ===========================================================================

class TestStripLeadingFigureNumber:

    @staticmethod
    def _strip(text: str) -> str:
        from app.routers.export import _strip_leading_figure_number
        return _strip_leading_figure_number(text)

    def test_colon_form_no_residual_punct(self):
        """``图 3-1：施工工艺流程`` —— 修复前实测 ``：施工工艺流程``。"""
        assert self._strip("图 3-1：施工工艺流程") == "施工工艺流程"

    def test_space_form_unchanged(self):
        assert self._strip("图 3-1 施工工艺流程") == "施工工艺流程"
        assert self._strip("图3-1 施工工艺流程") == "施工工艺流程"

    def test_half_width_colon(self):
        assert self._strip("图 3-1: 施工工艺流程") == "施工工艺流程"

    def test_idempotent(self):
        """重复剥离必须稳定（导出链路在 alt / title 两处都会调）。"""
        once = self._strip("图 3-1：施工工艺流程")
        assert self._strip(once) == once

    def test_no_number_kept_verbatim(self):
        # ⚠️ R165 起 ``图 A-1 x`` 属**应剥**形态（宽口径 FIG_TITLE_ANY_PREFIX_RE），
        #    已移出本用例；这里只锁「不含图号前缀的文本逐字保留」。
        for text in ["施工工艺流程", "图纸会审流程", ""]:
            assert self._strip(text) == text

    def test_contract_source_of_truth(self):
        """判据只允许来自契约模块：本函数不得再持有第二份图号正则。"""
        from app.routers import export as export_mod
        from app.services.content_format_contract import FIG_TITLE_PREFIX_RE
        assert export_mod.FIG_TITLE_PREFIX_RE is FIG_TITLE_PREFIX_RE


# ===========================================================================
# 3 · 纸面主文字下限（甘特图 / 劳动力图）
# ===========================================================================

_GANTT_PLAN = {
    "type": "gantt",
    "title": "施工进度计划",
    "duration": 90,
    "tasks": [
        {"id": "T1", "name": "施工准备", "start": 1, "end": 10},
        {"id": "T2", "name": "基坑开挖", "start": 11, "end": 30, "dependencies": ["T1"]},
        {"id": "T3", "name": "主体结构施工", "start": 31, "end": 60, "dependencies": ["T2"]},
        {"id": "T4", "name": "机电安装", "start": 45, "end": 75, "dependencies": ["T3"]},
        {"id": "T5", "name": "装饰装修", "start": 61, "end": 85, "dependencies": ["T4"]},
        {"id": "T6", "name": "竣工验收", "start": 86, "end": 90, "dependencies": ["T5"]},
    ],
}

_LABOR_DATA = {
    "type": "labor",
    "title": "劳动力投入及工种分布图",
    "phases": ["准备期", "基础期", "主体期", "装饰期", "收尾期"],
    "categories": ["钢筋工", "木工", "混凝土工", "电工", "焊工"],
    "data": [
        [10, 30, 40, 60, 90],
        [5, 20, 35, 50, 70],
        [5, 25, 40, 55, 60],
        [2, 10, 20, 30, 40],
        [0, 5, 10, 12, 8],
    ],
}


def _capture_paper(monkeypatch, module, render):
    """渲染一次并捕获 ``paper_floor_note`` 的**真实**画布/字号入参。"""
    captured: dict = {}

    def fake_note(label, fonts, canvas_w, canvas_h=0, *, log=None,
                  suggest="", primary=None):
        captured.update(label=label, fonts=list(fonts), w=canvas_w, h=canvas_h,
                        primary=list(primary if primary is not None else fonts))
        return 0.0

    monkeypatch.setattr(module, "paper_floor_note", fake_note)
    stream = render()
    assert stream is not None
    captured["img_w"] = Image.open(stream).size[0]
    assert captured["w"] == captured["img_w"], "纸面播报必须用真实画布宽"
    return captured


def _paper_mm(captured: dict, font) -> float:
    return ma.paper_font_mm(ma.font_px(font), captured["w"], captured["h"])


class TestPaperFontFloor:

    def test_gantt_primary_font_meets_floor(self, monkeypatch):
        """甘特图主文字（任务行）≥ 2.4mm —— 修复前实测 1.68mm。"""
        cap = _capture_paper(
            monkeypatch, gantt,
            lambda: gantt._render_gantt_image_v2(json.loads(json.dumps(_GANTT_PLAN))),
        )
        primary_mm = min(_paper_mm(cap, f) for f in cap["primary"])
        assert primary_mm >= ma.PAPER_FONT_MIN_MM - 1e-6, (
            f"甘特图主文字纸面仅 {primary_mm:.2f}mm（画布 {cap['w']}px），"
            f"低于下限 {ma.PAPER_FONT_MIN_MM}mm"
        )

    def test_gantt_all_fonts_meet_floor(self, monkeypatch):
        """甘特图最小字档（图例/刻度）也必须达标（本次几何调整的目标之一）。"""
        cap = _capture_paper(
            monkeypatch, gantt,
            lambda: gantt._render_gantt_image_v2(json.loads(json.dumps(_GANTT_PLAN))),
        )
        worst_mm = min(_paper_mm(cap, f) for f in cap["fonts"])
        assert worst_mm >= ma.PAPER_FONT_MIN_MM - 1e-6, (
            f"甘特图最小字档纸面 {worst_mm:.2f}mm，低于下限 {ma.PAPER_FONT_MIN_MM}mm"
        )

    def test_labor_primary_font_meets_floor(self, monkeypatch):
        """劳动力图主文字（工种名/工期轴）≥ 2.4mm —— 修复前实测 1.75mm。"""
        cap = _capture_paper(
            monkeypatch, labor,
            lambda: labor._render_labor_image_v2(json.loads(json.dumps(_LABOR_DATA))),
        )
        primary_mm = min(_paper_mm(cap, f) for f in cap["primary"])
        assert primary_mm >= ma.PAPER_FONT_MIN_MM - 1e-6, (
            f"劳动力图主文字纸面仅 {primary_mm:.2f}mm（画布 {cap['w']}px），"
            f"低于下限 {ma.PAPER_FONT_MIN_MM}mm"
        )

    def test_render_emits_no_floor_warning(self, monkeypatch, caplog):
        """渲染完成后日志里不得再有「低于下限」告警（用户可见的红字来源）。"""
        monkeypatch.delenv("CHART_PAPER_MIN_MM", raising=False)
        monkeypatch.delenv("ARCH_LEAF_PAPER_MIN_MM", raising=False)
        caplog.set_level(logging.WARNING)
        gantt._render_gantt_image_v2(json.loads(json.dumps(_GANTT_PLAN)))
        labor._render_labor_image_v2(json.loads(json.dumps(_LABOR_DATA)))
        bad = [r.getMessage() for r in caplog.records if "低于下限" in r.getMessage()]
        assert bad == [], bad


# ===========================================================================
# 4 · 缓存版本号：几何/字号变了就必须让旧图失效
# ===========================================================================

class TestRendererVersionBump:

    def test_renderer_version_bumped_for_r164(self):
        """R164 改变了甘特图/劳动力图的产物尺寸 ⇒ 版本号必须 ≥ 8。

        版本号是内存 LRU 键的第一段，也是磁盘缓存 material 的后缀
        （``mermaid_renderer._render_cache_key`` / ``ChartCache``）—— 不升则
        历史方案继续命中旧图，本次修复在生产上等于没生效。
        """
        from app.services.ai.mermaid_renderer import _RENDERER_VERSION
        assert int(_RENDERER_VERSION) >= 8
