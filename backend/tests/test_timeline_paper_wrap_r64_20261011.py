# -*- coding: utf-8 -*-
"""R64 · 时间轴按纸面毫米折行 + 全图种纸面毫米可观测性（2026-10-11）。

来源：R60 §5 / R62 §8 未落地**第 4 项**（AGENTS.md 的 R62 块把它列作 ⑥；两套编号里
  第 ⑤ 条分别是「预算内不自动拆」「节点档仍 <2.4mm」，都不是本条）——
「五个纸面字号低于下限的 PIL 渲染器」里时间轴最严重（画布宽随事件数**线性膨胀且
无上界**：24 事件印到纸上名称档 0.38mm / 最小档 0.28mm），而全仓只有架构图对
「印到纸上多大字」有感知。本轮两片：
  ① 公共层（`mermaid_common`）落**唯一**的纸面下限读数与毫米播报出口；五个 PIL
     渲染器各接一行 —— 纯观测，不改像素、不改返回值。
  ② 时间轴**按毫米折行**：行数由闭式算出（宽上限 16cm 与高上限 22cm 交叉 ⇒ 存在
     最优行数，正是 R62 实测那条「先升后降」曲线），不是拍「每行最多 6 个」。

判据分五组：A 公共层口径 / B 折行决策纯函数 / C 渲染接线与逐字回退 /
D 全图种可观测性 / E 源文件字节体检。
"""
from __future__ import annotations

import ast
import inspect
import logging
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.services.ai import mermaid_common as mc
from app.services.ai import mermaid_comparison as mcmp
from app.services.ai import mermaid_gantt as mg
from app.services.ai import mermaid_labor as mlb
from app.services.ai import mermaid_layout as mly
from app.services.ai import mermaid_renderer as mr
from app.services.ai import mermaid_timeline as mt

_AI_DIR = Path(inspect.getsourcefile(mc)).resolve().parent
_ENV_NAMES = ("TIMELINE_AUTO_WRAP", "TIMELINE_MAX_ROWS_PER_FIGURE",
              "CHART_PAPER_MIN_MM", "ARCH_LEAF_PAPER_MIN_MM")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「环境干净」开始：本轮新增的开关不得被上一用例残留影响。"""
    for k in _ENV_NAMES:
        monkeypatch.delenv(k, raising=False)


def _f(px: int):
    """字体替身：公共层口径只经 `font_px` 读 `.size` ⇒ 不必造真 PIL 字体。

    ⚠️ 不能直接把整数当字体传：`font_px(12) == 0` 会让 `paper_floor_note` 走
    「无字号 ⇒ 静默返回 0.0」的分支，凡断言「没有 warning」的用例都会**假绿**。
    """
    return type("_FontStub", (), {"size": px})()


def _ms(n: int, *, long_name: bool = False) -> dict:
    """造 n 个里程碑（默认短名称 ⇒ 不触发省略号，才能单独审「折行不丢字」）。"""
    tpl = "很长的里程碑名称用于验证显示效果%d" if long_name else "里程碑%d"
    return {"title": "关键里程碑",
            "milestones": [{"name": tpl % (i + 1), "date": f"第{i * 7 + 1}天",
                            "desc": "说明", "group": f"阶段{i % 3}"} for i in range(n)]}


def _png(stream) -> tuple[int, int]:
    with Image.open(stream) as im:
        return im.size


class _DrawSpy:
    """包一层 ImageDraw：把绘制调用**原样**转发给真画笔，同时记录调用参数。

    为什么要 spy 而不是只看 PNG 尺寸：折行的三条契约（每行一条轴 / 单卡宽度不变 /
    序号连续不丢字）都无法从外接尺寸反推出来 —— 尺寸相同而画错了照样绿。
    """

    def __init__(self, img):
        self._real = ImageDraw.Draw(img)
        self.lines: list[tuple] = []
        self.cards: list[tuple] = []
        self.texts: list[str] = []

    def line(self, *a, **k):
        self.lines.append(a[0] if a else ())
        return self._real.line(*a, **k)

    def rounded_rectangle(self, *a, **k):
        self.cards.append(a[0] if a else ())
        return self._real.rounded_rectangle(*a, **k)

    def text(self, *a, **k):
        if len(a) > 1 and isinstance(a[1], str):
            self.texts.append(a[1])
        return self._real.text(*a, **k)

    def __getattr__(self, name):                      # 其余原语直接透传
        return getattr(self._real, name)


@pytest.fixture
def draw_spy(monkeypatch):
    """把 `mermaid_timeline` 里的 `ImageDraw.Draw` 换成记录器（只影响时间轴模块）。"""
    rec: list[_DrawSpy] = []

    class _ModuleShim:
        @staticmethod
        def Draw(img):
            s = _DrawSpy(img)
            rec.append(s)
            return s

        # 万一别处用到 PIL 符号，透传以免打断渲染
        Image = Image

    monkeypatch.setattr(mt, "ImageDraw", _ModuleShim)
    assert rec == []
    return lambda: rec[0]


# ==========================================================================
# A · 公共层：纸面下限读数与毫米播报（单一事实源）
# ==========================================================================
class TestPaperFloorLayer:

    def test_floor_default_is_24mm(self):
        assert mc.paper_floor_mm() == pytest.approx(mc.PAPER_FONT_MIN_MM) == 2.4

    def test_floor_generic_env_wins(self, monkeypatch):
        monkeypatch.setenv("CHART_PAPER_MIN_MM", "3.0")
        monkeypatch.setenv("ARCH_LEAF_PAPER_MIN_MM", "2.8")
        assert mc.paper_floor_mm() == pytest.approx(3.0)

    def test_floor_arch_env_still_honoured(self, monkeypatch):
        """兼容既有旋钮：只设架构图那把名字也要认（全局调档不该逼人改两处）。"""
        monkeypatch.setenv("ARCH_LEAF_PAPER_MIN_MM", "2.8")
        assert mc.paper_floor_mm() == pytest.approx(2.8)

    def test_floor_blank_and_invalid_fall_back(self, monkeypatch):
        for bad in ("", "   ", "abc", "2.4x"):
            monkeypatch.setenv("CHART_PAPER_MIN_MM", bad)
            assert mc.paper_floor_mm() == pytest.approx(mc.PAPER_FONT_MIN_MM), bad

    def test_floor_zero_or_negative_disables_warning(self, monkeypatch):
        """0 / 负数 ⇒ 0.0 = 关闭告警（毫米仍照常统计），与「非法回落默认」不同。"""
        for v in ("0", "-1"):
            monkeypatch.setenv("CHART_PAPER_MIN_MM", v)
            assert mc.paper_floor_mm() == 0.0, v

    def test_font_px_reads_real_font_size(self):
        f = mc._image_font(37, bold=True)
        assert mc.font_px(f) == 37

    def test_font_px_never_raises(self):
        class _NoSize:
            pass
        assert mc.font_px(None) == 0
        assert mc.font_px(object()) == 0
        assert mc.font_px(_NoSize()) == 0
        assert mc.font_px("not a font") == 0

    def test_note_returns_worst_mm(self):
        got = mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800)
        assert got == pytest.approx(round(mc.paper_font_mm(12, 2000, 800), 2))
        assert 0 < got < mc.paper_floor_mm()

    def test_note_logs_info_with_both_calibers(self, caplog):
        with caplog.at_level(logging.INFO):
            mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800, primary=[_f(30)])
        txt = " | ".join(r.getMessage() for r in caplog.records)
        assert "[纸面可读性]" in txt and "最差字号" in txt and "主文字" in txt

    def test_note_warning_keys_on_primary_not_worst(self, caplog):
        """小字档（序号/说明）低于下限而主文字达标 ⇒ **不**告警（否则每张图永久红灯）。
        
        ⚠️ 必须同时断言 info **在位**：只断言「没有 warning」时，入参被 `font_px`
        吞成 0（函数整个静默）也能通过 —— 那是假绿，不是本条要证的口径分组。
        """
        with caplog.at_level(logging.INFO):
            mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800, primary=[_f(30)])
        infos = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO and "纸面可读性" in r.getMessage()]
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "纸面可读性" in r.getMessage()]
        assert len(infos) == 1, "info 必须照常播报最差档"
        assert warns == [], f"主文字达标却告警：{[r.getMessage() for r in warns]}"
        assert mc.paper_font_mm(12, 2000, 800) < mc.paper_floor_mm()  # 前提：小字档确实低于下限

    def test_note_warning_fires_when_primary_below_floor(self, caplog):
        with caplog.at_level(logging.WARNING):
            mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800,
                              primary=[_f(12)], suggest="（试）")
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(msgs) == 1
        assert "主文字印到纸上" in msgs[0] and "（试）" in msgs[0]

    def test_note_without_primary_judges_all_fonts(self, caplog):
        with caplog.at_level(logging.WARNING):
            mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800)
        assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1

    def test_note_floor_zero_silences_warning_only(self, caplog, monkeypatch):
        monkeypatch.setenv("CHART_PAPER_MIN_MM", "0")
        with caplog.at_level(logging.INFO):
            mc.paper_floor_note("测试", [_f(12)], 2000, 800)
        assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 0
        assert sum(1 for r in caplog.records if "[纸面可读性]" in r.getMessage()) == 1

    def test_note_empty_inputs_are_silent(self, caplog):
        with caplog.at_level(logging.INFO):
            assert mc.paper_floor_note("测试", [], 2000, 800) == 0.0
            assert mc.paper_floor_note("测试", [_f(12)], 0, 800) == 0.0
            assert mc.paper_floor_note("测试", [None], 2000, 800) == 0.0
            assert mc.paper_floor_note("测试", [_f(0)], 2000, 800) == 0.0
        assert caplog.records == []

    def test_note_never_propagates_logger_failure(self, monkeypatch):
        """播报是**附属**能力：日志器炸了也不能连累出图（渲染路径没有 try 兜这一层）。"""
        class _Boom:
            def info(self, *a, **k):
                raise RuntimeError("boom")

            def warning(self, *a, **k):
                raise RuntimeError("boom")

        assert mc.paper_floor_note("测试", [_f(30), _f(12)], 2000, 800, log=_Boom()) > 0

    def test_architecture_does_not_use_generic_note(self):
        """架构图走自己的 `compose_readability_warning`：两边都接会打两条告警。"""
        src = (_AI_DIR / "mermaid_architecture.py").read_text(encoding="utf-8")
        assert "paper_floor_note" not in src


class TestFontPxReadsStubOnly:

    def test_font_px_reads_size_attribute(self):
        class _F:
            size = 37
        assert mc.font_px(_F()) == 37

    def test_font_px_on_real_font_is_bounded(self):
        """真字体拿不到 `.size` 时降级为 0（播报会跳过该档），但绝不抛。"""
        assert mc.font_px(mc._image_font(37, bold=True)) in (0, 37)


# ==========================================================================
# B · 折行决策：行数由闭式算出（宽 16cm / 高 22cm 两个上限交叉 ⇒ 存在最优）
# ==========================================================================
class TestWrapPlanner:

    def test_small_counts_stay_single_row(self):
        """n≤4：折行收益不到 20% 门槛 ⇒ 保持原形态（改动必须有理由）。"""
        for n in (1, 2, 3, 4):
            assert mt.plan_timeline_rows(n) == (1, n), n

    @pytest.mark.parametrize("n,expected", [
        (5, (2, 3)), (6, (2, 3)), (8, (3, 3)), (9, (3, 3)),
        (12, (4, 3)), (16, (6, 3)), (24, (6, 4)), (30, (8, 4)),
    ])
    def test_known_decision_table(self, n, expected):
        assert mt.plan_timeline_rows(n) == expected

    def test_choice_is_global_maximum(self):
        """planner 选到的必须是**穷举**所有行数里的最大毫米（不是第一个还行的一般）。"""
        n = 24
        best = max(_mm_at(n, r) for r in range(1, 13))
        rows, cards = mt.plan_timeline_rows(n, gain=1.0)
        assert _mm_at(n, rows) == pytest.approx(best), f"选了 {rows} 行，最优是别的"

    def test_gain_gate_is_real(self):
        """门槛真的参与决策：gain=1.0 会折，gain=99 一定不折。"""
        assert mt.plan_timeline_rows(5, gain=1.0)[0] > 1
        assert mt.plan_timeline_rows(24, gain=99.0) == (1, 24)

    def test_single_row_reason_covers_every_cause(self, monkeypatch):
        """`_single_row_reason` 四条分支各说各话（纯函数层，先于端到端把口径钉死）。

        这个函数**只产文案**，所以它绝不能被布局层反向依赖；分支判据与
        `plan_timeline_rows` 用的谓词**同一把**（`_timeline_auto_wrap_enabled` /
        `_timeline_max_rows`），否则文案与决策又会各说各话。
        """
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", "0")
        assert "TIMELINE_AUTO_WRAP=0" in mt._single_row_reason(8)
        monkeypatch.delenv("TIMELINE_AUTO_WRAP")
        monkeypatch.setenv("TIMELINE_MAX_ROWS_PER_FIGURE", "1")
        assert "TIMELINE_MAX_ROWS_PER_FIGURE" in mt._single_row_reason(8)
        monkeypatch.delenv("TIMELINE_MAX_ROWS_PER_FIGURE")
        assert "增益" in mt._single_row_reason(4)          # 算过而不值
        assert "无从折行" in mt._single_row_reason(1)       # 单事件没有「折不折」可言
        for bad in (None, "abc", 0, -2):
            assert isinstance(mt._single_row_reason(bad), str)      # 绝不抛

    def test_max_rows_cap_is_respected(self):
        assert mt.plan_timeline_rows(24, max_rows=1) == (1, 24)
        rows, cards = mt.plan_timeline_rows(24, max_rows=2)
        assert (rows, cards) == (2, 12)
        assert mt.plan_timeline_rows(24, max_rows=0) == (1, 24)   # 非法上限 ⇒ 不折

    def test_max_rows_env(self, monkeypatch):
        monkeypatch.setenv("TIMELINE_MAX_ROWS_PER_FIGURE", "2")
        assert mt.plan_timeline_rows(24)[0] == 2
        monkeypatch.setenv("TIMELINE_MAX_ROWS_PER_FIGURE", "abc")
        assert mt._timeline_max_rows() == mt._MAX_ROWS_DEFAULT

    @pytest.mark.parametrize("off", ["0", "false", "FALSE", "no", "off", " No "])
    def test_switch_falsy_forms(self, monkeypatch, off):
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", off)
        assert mt.plan_timeline_rows(16) == (1, 16), off

    @pytest.mark.parametrize("on", ["1", "true", "", "  "])
    def test_switch_default_on(self, monkeypatch, on):
        """缺开；未识别的值一律按「没关」处理（与 ARCH_AUTO_SPLIT 同语义）。"""
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", on)
        assert mt.plan_timeline_rows(16)[0] > 1

    @pytest.mark.parametrize("bad", [0, -3, None, "abc", 1.0 / 0 if False else 2.5])
    def test_never_raises_on_garbage_input(self, bad):
        rows, cards = mt.plan_timeline_rows(bad)
        assert rows >= 1 and cards >= 0

    def test_disabled_flag_short_circuits(self):
        assert mt.plan_timeline_rows(24, enabled=False) == (1, 24)

    def test_canvas_formula_equals_legacy_single_row(self):
        """`_timeline_canvas_design(n, 1)` 必须逐项等于改造前的宽度/高度表达式。"""
        for n in (1, 2, 4, 6, 8, 16, 24, 30):
            w, h = mt._timeline_canvas_design(n, 1)
            assert w == max(960, 80 + n * 230 + (n - 1) * 24), n
            assert h == 60 + 80 + 80 + 60 + 50 == 330, n

    def test_each_extra_row_adds_pitch(self):
        base = mt._timeline_canvas_design(3, 1)[1]
        for rows in (2, 3, 7, 12):
            assert mt._timeline_canvas_design(3, rows)[1] == base + (rows - 1) * 170

    def test_wrapping_actually_buys_millimetres(self):
        """本轮的立身之本：折行后日期档毫米**必须**高于单行，且不是压线赢。"""
        for n in (8, 16, 24, 30):
            rows, cards = mt.plan_timeline_rows(n)
            w0, h0 = mt._timeline_canvas_design(n, 1)
            w1, h1 = mt._timeline_canvas_design(cards, rows)
            a = mc.paper_font_mm(12, w0, h0)
            b = mc.paper_font_mm(12, w1, h1)
            assert b > a * 1.2, f"n={n}: {a:.2f} → {b:.2f}"

    def test_height_cap_eventually_wins(self):
        """行太多会被 22cm 高上限反噬 ⇒ 曲线先升后降，planner 不会无限折。"""
        w, h = mt._timeline_canvas_design(3, 12)
        assert mc.paper_height_binds(w, h), "12 行时应由高上限决定缩放"
        assert mc.paper_font_mm(12, w, h) < mc.paper_font_mm(12, *_timeline_at(3, 4))


def _mm_at(n: int, rows: int) -> float:
    cards = -(-n // rows)
    w, h = mt._timeline_canvas_design(cards, rows)
    return mc.paper_font_mm(mt._DATE_FONT_DESIGN, w, h)


def _timeline_at(cards: int, rows: int) -> tuple[int, int]:
    return mt._timeline_canvas_design(cards, rows)


# ==========================================================================
# C · 渲染接线：折行真的画出来了，且「关掉开关 = 逐字回退」
# ==========================================================================
class TestWrapRenderWiring:

    def test_switch_off_equals_forced_single_row(self, monkeypatch):
        """两条「回到旧形态」的路径必须产出**同一张 PNG**：env 关 vs 决策恒返回 (1, n)。"""
        payload = _ms(16)
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", "0")
        a = mt._render_timeline_image_v2(payload).getvalue()
        monkeypatch.delenv("TIMELINE_AUTO_WRAP")
        monkeypatch.setattr(mt, "plan_timeline_rows", lambda n, **kw: (1, n))
        b = mt._render_timeline_image_v2(payload).getvalue()
        assert a == b and len(a) > 1000

    def test_single_row_geometry_unchanged_by_the_patch(self, monkeypatch):
        """关掉开关后，n=16 的画布宽必须仍是改造前公式的 4120×2.5 = 10300。"""
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", "0")
        w, h = _png(mt._render_timeline_image_v2(_ms(16)))
        assert (w, h) == (10300, 825)

    def test_wrapped_canvas_geometry_is_exactly_as_planned(self):
        """n=16 ⇒ 6 行 × 每行 3 卡：宽被 960 地板托住 = 2400，高 = (330+5×170)×2.5。"""
        rows, cards = mt.plan_timeline_rows(16)
        w, h = _png(mt._render_timeline_image_v2(_ms(16)))
        assert (rows, cards) == (6, 3)
        assert (w, h) == (2400, (330 + 5 * 170) * 5 // 2)
        assert h == 825 + (rows - 1) * 425

    def test_planner_model_matches_the_delivered_png(self):
        """决策用的闭式尺寸必须**逐像素**等于真正交付的那张 PNG（A/B M2 撞出的盲区）。

        渲染层的 `total_w/total_h` 与 `_timeline_canvas_design()` 是同一个几何的**两份写法**：
        前者产像素、后者产决策。任一方被改动而另一方没跟上时，planner 优化的就是一张
        并不存在的图 —— 像素没错、日志里的毫米全是错的，而这类偏差**只有这条判据能看见**
        （沿 R62 §3.5：不变量必须由真实交付尺寸成立，不能靠预估自证）。
        """
        scale = 2.5
        for n in (1, 6, 7, 16, 24, 30):
            rows, cards = mt.plan_timeline_rows(n)
            dw, dh = mt._timeline_canvas_design(cards, rows)
            w, h = _png(mt._render_timeline_image_v2(_ms(n)))
            expect = (int(dw * scale), int(dh * scale))
            assert (w, h) == expect, f"n={n} 模型 {dw}x{dh}×2.5={expect} 与产物 {(w, h)} 分叉"

    def test_one_axis_line_per_row(self, draw_spy):
        """每行**一条**时间轴：按 y 聚成行带后，带数 == 行数、带间距恒 425px。

        不能直接数「不同 y 的个数」：每行除了主轴还有一条下偏几像素的阴影线，
        实测水平线是 2×行数（n=16 ⇒ 12 个 y）。聚类半径取 20px，明显小于行距 425、
        又明显大于阴影偏移，不依赖那个偏移量的具体数值。
        """
        mt._render_timeline_image_v2(_ms(16))
        spy = draw_spy()
        ys = sorted({int(p[0][1]) for p in spy.lines
                     if len(p) == 2 and p[0][1] == p[1][1]})
        assert ys, "没有画任何水平线"
        bands = [[ys[0]]]
        for y in ys[1:]:
            (bands[-1].append(y) if y - bands[-1][-1] <= 20 else bands.append([y]))
        rows, _ = mt.plan_timeline_rows(16)
        assert len(bands) == rows, f"应有 {rows} 条时间轴（行带），实际 {bands}"
        tops = [b[0] for b in bands]
        pitch = [b - a for a, b in zip(tops, tops[1:])]
        assert set(pitch) == {425}, f"行距必须恒等于 170×2.5：{pitch}"
        assert all(len(b) == 2 for b in bands), f"每行应只有主线+阴影线两条：{bands}"

    def test_card_width_survives_wrapping(self, draw_spy):
        """折行**只改行数**：卡片宽恒 550 = 230×2.5 − 10×2.5，且一张卡都不许丢。

        R61 把 min_node_w 180→230 是为了长名称不截断；本轮折行只允许动**行数**。
        每次画卡是「阴影 + 本体」两个圆角矩形 ⇒ 矩形数 == 2×事件数，缺一个就是丢卡。
        """
        mt._render_timeline_image_v2(_ms(16))
        spy = draw_spy()
        widths = {abs(p[2] - p[0]) for p in spy.cards if len(p) == 4}
        assert widths == {550}, f"卡片宽必须恰为 230×scale 内缩后 550：{widths}"
        assert len(spy.cards) == 2 * 16, f"16 卡应画 32 个矩形，实际 {len(spy.cards)}"

    def test_all_serials_present_and_no_new_ellipsis(self, draw_spy):
        """折行不得重排/丢字：序号 1..16 全在，且不因折行新产生省略号。"""
        mt._render_timeline_image_v2(_ms(16))
        spy = draw_spy()
        for i in range(1, 17):
            assert str(i) in spy.texts, f"序号 {i} 丢失"
        assert not any("…" in t for t in spy.texts), spy.texts

    def test_last_row_may_be_partial_never_empty(self):
        """n=7 → 3 行 × 每行 3 卡 ⇒ 每行分布 [3,3,1]：末行不满但**不为空**。"""
        rows, cards = mt.plan_timeline_rows(7)
        per_row = [min(cards, 7 - r * cards) for r in range(rows)]
        assert per_row == [3, 3, 1] and min(per_row) >= 1

    def test_single_milestone_still_renders(self):
        """n=1 时旧代码靠 `if n == 1` 跳过除零；改造后由 `per_row[0] == 1` 承担同一职责。"""
        w, h = _png(mt._render_timeline_image_v2(_ms(1)))
        assert (w, h) == (2400, 825)

    def test_long_names_do_not_get_truncated_by_wrapping(self, draw_spy):
        """R61 的「加宽防截断」在折行形态下同样成立（长名称仍完整下发）。"""
        mt._render_timeline_image_v2(_ms(6, long_name=True))
        spy = draw_spy()
        joined = "".join(spy.texts)
        assert "很长的里程碑名称" in joined

    def test_one_card_per_row_does_not_divide_by_zero(self, draw_spy, monkeypatch):
        """决策被钉成「4 行 × 每行 1 卡」时仍须出图：`span / (c - 1)` 的除零护栏真走过。

        其余折行用例每行都 ≥2 卡 ⇒ 三元式的 `else 0.0` 分支与 `per_row` 的末行
        归一从来不是**必要条件**，改坏了也没人红。这里刻意用 monkeypatch 造形态，
        审的是布局层而不是决策层（决策由 B 组负责）。
        """
        monkeypatch.setattr(mt, "plan_timeline_rows", lambda n, **kw: (4, 1))
        w, h = _png(mt._render_timeline_image_v2(_ms(4)))
        assert (w, h) == (2400, 825 + 3 * 425), (w, h)
        spy = draw_spy()
        ys = sorted({int(p[0][1]) for p in spy.lines
                     if len(p) == 2 and p[0][1] == p[1][1]})
        bands = [[ys[0]]]
        for y in ys[1:]:
            (bands[-1].append(y) if y - bands[-1][-1] <= 20 else bands.append([y]))
        assert len(bands) == 4, f"4 行 ⇒ 4 条独立时间轴：{bands}"
        assert len(spy.cards) == 2 * 4, f"4 卡应画 8 个矩形：{len(spy.cards)}"

    def test_renderer_version_bumped(self):
        """改的是**产物尺寸**：不升版本号，历史方案会继续从缓存拿旧图 = 修复从未生效。
        R64 升到 4；后续轮次（如 R65 流程图统一样张）会继续上调，故此处只锁「≥4」。"""
        assert mr._RENDERER_VERSION >= 4
        assert inspect.getsource(mr).count("_RENDERER_VERSION = ") == 1


# ==========================================================================
# D · 全图种可观测性：五类 PIL 渲染器都要把毫米写进日志
# ==========================================================================
_MIN_PAYLOADS = {
    "mermaid_timeline.py": (mt, "_render_timeline_image_v2",
                            _ms(6), "时间轴"),
    "mermaid_layout.py": (mly, "_render_layout_image_v2",
                          {"zones": [{"name": "钢筋加工区", "category": "生产区"},
                                     {"name": "生活区", "category": "生活区"}]}, "布局图"),
    "mermaid_comparison.py": (mcmp, "_render_comparison_image_v2",
                              {"rows": [{"label": "方案A", "value": 30},
                                        {"label": "方案B", "value": 70}]}, "对比图"),
    "mermaid_gantt.py": (mg, "_render_gantt_image_v2",
                         {"tasks": [{"name": "基础施工", "start": "2026-03-01", "duration": "30d"},
                                    {"name": "主体封顶", "start": "2026-04-01", "duration": "45d"}]},
                         "甘特图"),
    # 劳动力图只认 phases/categories/data 三件套（counts 形态归一器不接受 → None）。
    "mermaid_labor.py": (mlb, "_render_labor_image_v2",
                         {"phases": ["基础", "主体", "装修"],
                          "categories": [{"name": "木工"}, {"name": "钢筋工"}],
                          "data": [[10, 15], [20, 25], [8, 10]]},
                         "劳动力图"),
}


class TestAllRenderersReport:

    @pytest.mark.parametrize("mod_name", sorted(_MIN_PAYLOADS))
    def test_renderer_emits_floor_note(self, mod_name, caplog):
        mod, fn_name, payload, label = _MIN_PAYLOADS[mod_name]
        fn = getattr(mod, fn_name)
        with caplog.at_level(logging.INFO):
            out = fn(payload)
        assert out is not None, f"{fn_name} 渲染失败（本轮改动打断了出图）"
        msgs = [r.getMessage() for r in caplog.records if "[纸面可读性]" in r.getMessage()]
        assert any(label in m for m in msgs), f"{mod_name} 没有播报：{msgs}"

    @pytest.mark.parametrize("mod_name", sorted(_MIN_PAYLOADS))
    def test_at_most_one_warning_per_chart(self, mod_name, caplog):
        """播报不得刷屏：一张图最多一条 warning。"""
        mod, fn_name, payload, _ = _MIN_PAYLOADS[mod_name]
        with caplog.at_level(logging.WARNING):
            getattr(mod, fn_name)(payload)
        assert sum(1 for r in caplog.records
                   if "[纸面可读性]" in r.getMessage()) <= 1

    def test_reported_mm_matches_the_delivered_png(self, caplog):
        """播报必须**自洽**：日志里的 px 与 mm，要用**真实交付 PNG 的尺寸**复算得出。

        为什么不是「断言出现某个预期毫米值」：info 行打印的是最差档与主文字档两个数，
        由内部字号表决定，从外面猜字号等于把口径重新抄一份（`font_px` 的存在正是为了
        不抄表）。复算判据把「播报口径 ← 真实像素 ← 真实交付尺寸」三者钉在一起。
        """
        with caplog.at_level(logging.INFO):
            stream = mt._render_timeline_image_v2(_ms(16))
        w, h = _png(stream)
        msg = next(r.getMessage() for r in caplog.records if "[纸面可读性]" in r.getMessage())
        assert f"画布 {w}x{h}px" in msg, msg
        m = re.search(r"最差字号 (\d+)px → 纸面 ([\d.]+)mm", msg)
        assert m, f"info 行缺少 px→mm 口径：{msg}"
        worst_px, worst_mm = int(m.group(1)), float(m.group(2))
        assert worst_px > 0 and worst_mm > 0
        assert worst_mm == pytest.approx(round(mc.paper_font_mm(worst_px, w, h), 2), abs=0.01), msg
        m2 = re.search(r"主文字 → ([\d.]+)mm", msg)
        assert m2, f"info 行缺少主文字口径：{msg}"
        assert float(m2.group(1)) >= worst_mm - 1e-9, "主文字档不可能优于最差档"

    def test_timeline_primary_wiring_prevents_permanent_red(self, caplog):
        """端到端锁「时间轴真的传了 `primary`」：小字档低于下限**不该**让每张图永久红灯。

        A 组锁的是公共层口径**逻辑**；本例锁的是**接线**（摘掉 `primary=[font_name]`
        时 A 组全绿、只有这里会红）。毫米数从日志自身取出后用真实 PNG 尺寸复算 ⇒
        不抄字号表（`font_px` 的存在就是为了不抄表）。
        """
        with caplog.at_level(logging.INFO):
            stream = mt._render_timeline_image_v2(_ms(16))
        w, h = _png(stream)
        info = next(r.getMessage() for r in caplog.records
                    if r.levelno == logging.INFO and "[纸面可读性]" in r.getMessage())
        worst_px, worst_mm = re.search(r"最差字号 (\d+)px → 纸面 ([\d.]+)mm", info).groups()
        main_mm = re.search(r"主文字 → ([\d.]+)mm", info).group(1)
        assert float(worst_mm) == pytest.approx(
            round(mc.paper_font_mm(int(worst_px), w, h), 2), abs=0.01), info
        floor = mc.paper_floor_mm()
        gap = f"最差 {worst_mm} / 主文字 {main_mm} / 下限 {floor}"
        assert float(worst_mm) < floor, f"前提不成立（{gap}）：本例是空跑"
        assert float(main_mm) >= floor, f"前提不成立（{gap}）：本例是空跑"
        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert warns == [], f"主文字已达标仍在告警：{warns}"

    def test_single_row_warning_explains_why_not_wrapped(self, caplog):
        """单行且低于下限（成因=收益不够）⇒ 告警要交代门槛，不许声称「单行最优形态」。

        这条锁的是**文案与事实一致**：同一行里程序既说主文字低于下限、又说单行是
        「最优形态」，运维读到就会以为程序已无能为力（实际是门槛主动放弃了 12% 收益）。
        n=4 是实测的门槛外侧形态（2.21mm < 2.40mm 且 plan=(1, 4)）。
        """
        assert mt.plan_timeline_rows(4) == (1, 4), "前提：n=4 门槛外，保持单行"
        with caplog.at_level(logging.WARNING):
            mt._render_timeline_image_v2(_ms(4))
        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert len(warns) == 1, f"前提：n=4 主文字确实低于下限（实际告警 {len(warns)} 条）"
        assert "增益" in warns[0] and "20%" in warns[0], warns[0]
        assert "最优形态" not in warns[0], warns[0]
        # 三条成因不许写串：这一例是「算过而不值」，另两种成因的词都不该出现
        assert "TIMELINE_AUTO_WRAP" not in warns[0], warns[0]
        assert "TIMELINE_MAX_ROWS_PER_FIGURE" not in warns[0], warns[0]

    def test_disabled_switch_is_reported_as_the_reason(self, monkeypatch, caplog):
        """**开关关掉**导致的单行，不许说成「折行不值」（成因分叉的第二条）。

        n=8 开着开关会折 3 行（收益 2.19 倍），关掉后才是单行 5220x825。本轮取证表
        `all_renderers_report.md` 里这条谎就是程序自己打出来的：同一句话既说主文字
        低于下限、又怪门槛，运维会去调 `_WRAP_GAIN` —— 调它对本例完全不起作用。
        """
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", "0")
        assert mt.plan_timeline_rows(8) == (1, 8), "前提：关掉开关后保持单行"
        with caplog.at_level(logging.WARNING):
            mt._render_timeline_image_v2(_ms(8))
        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert len(warns) == 1, f"前提：n=8 单行主文字低于下限（实际告警 {len(warns)} 条）"
        assert "TIMELINE_AUTO_WRAP" in warns[0], warns[0]
        assert "增益" not in warns[0] and "20%" not in warns[0], warns[0]

    def test_max_rows_cap_is_reported_as_the_reason(self, monkeypatch, caplog):
        """行数上限被钉成 1 ⇒ 第三条成因，报出自己的 env 名（三成因互不写串）。

        开关是开着的、折行也确实「值」（n=8 收益 2.19 倍），是 `max_rows` 把它按住的：
        这时怪门槛或怪开关都错，只有 env 名能把人带到真正要改的那把旋钮上。
        """
        monkeypatch.setenv("TIMELINE_MAX_ROWS_PER_FIGURE", "1")
        assert mt.plan_timeline_rows(8) == (1, 8), "前提：上限被钉为 1 ⇒ 单行"
        with caplog.at_level(logging.WARNING):
            mt._render_timeline_image_v2(_ms(8))
        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert len(warns) == 1, f"前提：n=8 单行主文字低于下限（实际告警 {len(warns)} 条）"
        assert "TIMELINE_MAX_ROWS_PER_FIGURE" in warns[0], warns[0]
        assert "TIMELINE_AUTO_WRAP" not in warns[0], warns[0]

    def test_all_five_renderers_wired_exactly_once(self):
        """AST 级：五个渲染器各调用 `paper_floor_note` **恰一次**（多处=双告警风险）。"""
        for mod_name in _MIN_PAYLOADS:
            tree = ast.parse((_AI_DIR / mod_name).read_text(encoding="utf-8"))
            n = sum(1 for x in ast.walk(tree)
                    if isinstance(x, ast.Call)
                    and getattr(x.func, "id", "") == "paper_floor_note")
            assert n == 1, f"{mod_name} 调用 {n} 次（要求恰 1 次）"

    def test_architecture_not_wired(self):
        """架构图不接通用播报（它有自己的预算告警），否则一条图两条红灯。"""
        assert "paper_floor_note" not in (_AI_DIR / "mermaid_architecture.py").read_text(encoding="utf-8")

    def test_common_layer_is_the_only_floor_source(self):
        """下限默认值只允许在公共层与架构图各一份**同名常量**，不许第三份。"""
        hits = []
        for p in _AI_DIR.glob("*.py"):
            if "PAPER_FONT_MIN_MM = " in p.read_text(encoding="utf-8"):
                hits.append(p.name)
        assert sorted(hits) == ["mermaid_architecture.py", "mermaid_common.py"], hits


# ==========================================================================
# E · 源文件字节体检（CRLF 仓库的 .py 一律字节级改动，改完立即体检）
# ==========================================================================
_TOUCHED = ["mermaid_common.py", "mermaid_timeline.py", "mermaid_layout.py",
            "mermaid_comparison.py", "mermaid_gantt.py", "mermaid_labor.py",
            "mermaid_renderer.py", "mermaid_architecture.py"]

# 本轮同样改过的**测试**文件（补丁④升级 R61 语义）：CRLF 仓库里 .py 被文本工具
# 展开行尾的事故已经两轮，只体检生产文件等于留洞。
_TOUCHED_TESTS = ["test_r61_autosplit_and_label_widen_20261010.py",
                  "test_timeline_paper_wrap_r64_20261011.py"]
_TESTS_DIR = _AI_DIR.parents[2] / "tests"


class TestR64ByteIntegrity:

    @pytest.mark.parametrize("name", _TOUCHED)
    def test_line_endings_and_no_corruption(self, name):
        raw = (_AI_DIR / name).read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0, name
        assert raw.count(b"\n") == crlf, f"{name} 有裸 LF（被工具展开过）"
        assert raw.count(b"\r") == crlf, f"{name} 有孤立 CR（\\r\\r\\n 污染）"
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, f"{name} 有 U+FFFD（编码丢失）"
        assert b"\x00" not in raw and b"\x07" not in raw and b"\x0c" not in raw

    @pytest.mark.parametrize("name", _TOUCHED)
    def test_sources_still_compile(self, name):
        src = (_AI_DIR / name).read_text(encoding="utf-8")
        compile(src, name, "exec")

    @pytest.mark.parametrize("name", _TOUCHED_TESTS)
    def test_touched_test_files_are_clean(self, name):
        """改过的测试文件也要字节体检（R57/R58 两轮都栽在行尾上）。"""
        raw = (_TESTS_DIR / name).read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0, name
        assert raw.count(b"\n") == crlf, f"{name} 有裸 LF"
        assert raw.count(b"\r") == crlf, f"{name} 有孤立 CR"
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, name
        compile(raw.decode("utf-8"), name, "exec")

    def test_card_width_has_a_single_source(self):
        """卡片宽度在时间轴里只许出现一次字面量（多处 ⇒ 折行改造容易只改一处）。"""
        src = (_AI_DIR / "mermaid_timeline.py").read_text(encoding="utf-8")
        assert src.count("int(230 * scale)") == 1, "min_node_w 必须只有一个来源"

    def test_no_vertical_text_primitives(self):
        """沿 R61：时间轴/布局/对比三图的修复必须是「非竖排」路线。"""
        for name in ("mermaid_timeline.py", "mermaid_layout.py", "mermaid_comparison.py"):
            assert "_draw_text_vertical_center" not in (_AI_DIR / name).read_text(encoding="utf-8")


