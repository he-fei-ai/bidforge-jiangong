# -*- coding: utf-8 -*-
"""R60 · 架构图纸面可读性下限护栏（2026-10-10）。

来源：R59 §9 遗留项①「竖排不是万能解 —— 16 叶/4 组竖排后纸面字号仍仅约 1.31mm，
真正解法是限制单张架构图规模」。本轮**不删内容**，改为按实测机理收口：

    纸面字号(mm) = 图内字号(px) × scale × 2.54 / DPI × fit × 10
    宽度受限时 fit = 16cm ÷ 纸面宽 ⇒ **scale 与 DPI 全部约掉**
    ⇒ 只有「字号 ÷ 画布宽」这一个比值决定纸上多大

覆盖：
  A 纸面字号模型（与 `routers/export.py:_fit_image_cm` 真实返回值交叉核对）
  B 字号自适应的三条不变量（永不改小 / 不越上限 / 关闭态逐字回退）
  C 竖排「放大不得新增省略号」（用真实字体度量端到端判定，不用近似公式自证）
  D 可读性驱动的竖排升级（与 R59 数量规则相互独立、可分别关闭）
  E 渲染接线与可观测性（warning 只在"字号封顶仍不达标"时出现）
  F 跨层常量 parity + 源文件字节体检（R57/R58 行尾判据）
  G 纸面字号不足 × 规模超预算**合并为一条**告警（纯函数判据 + 渲染器单告警出口）
"""
import ast
import io
import py_compile
from pathlib import Path

import app.services.ai.mermaid_architecture as ma
import pytest
from app.services.ai.mermaid_common import (
    _draw_text_vertical_center,
    _image_font,
    _vertical_text_capacity,
)
from PIL import Image, ImageDraw

_BE = Path(__file__).resolve().parent.parent
_TITLE_H = 56                              # 渲染器内标题带高度
_MM = ma.PAPER_FONT_MIN_MM                 # 纯函数用例统一取常量口径（不隐式读环境）
_ARCH_SRC = (_BE / "app" / "services" / "ai" / "mermaid_architecture.py").read_text(encoding="utf-8")
_COMMON_SRC = (_BE / "app" / "services" / "ai" / "mermaid_common.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 造树工具
def _tree(n_groups: int, per_group: int, extra_depth1: int = 0,
          label_len: int = 5) -> dict:
    """造「根 → 二级组 → 叶子」的树；`extra_depth1` 追加 depth=1 的直属叶子。"""
    kids = []
    for gi in range(n_groups):
        kids.append({
            "label": f"第{gi}施工组",
            "children": [{"label": "作" * label_len + f"{gi}{j}",
                          "highlight": (gi == 0 and j == 0)} for j in range(per_group)],
        })
    for i in range(extra_depth1):
        kids.append({"label": f"总工办直属专员{i}"})
    return {"label": "项目经理部", "children": kids}


def _rendered_png(tree):
    bio = ma._render_architecture_image_v2({"root": tree})
    assert bio is not None
    with Image.open(io.BytesIO(bio.getvalue())) as im:
        return im.size, im.info.get("dpi")


def _drawn_text(text: str, font_px: int) -> str:
    """用**与渲染器完全一致**的 scale/box/padding 真画一次，回传实际绘出的文字。"""
    scale = ma.ARCH_RENDER_SCALE
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    box_w = int((ma.LEAF_VERTICAL_W - 2 * ma.LEAF_VERTICAL_PAD_X) * scale)
    box_h = int((ma.LEAF_VERTICAL_H - 2 * ma.LEAF_VERTICAL_PAD_Y) * scale)
    gap = int(ma.LEAF_VERTICAL_CHAR_GAP * scale)
    font = _image_font(int(font_px * scale), bold=True)
    return _draw_text_vertical_center(draw, (0, 0, box_w, box_h), text, font,
                                      "#000000", char_gap=gap)


def _names_used(src: str, symbol: str) -> int:
    """AST 里对 `symbol` 的**真实引用**次数（Load），不含注释/文档字符串。

    刻意不用 `src.count(symbol)`：R58 已学到「`in`/count 粗判在文档字符串提到
    该符号时就给出假数」，本文件的 docstring 就写了 `_vertical_text_capacity`。
    """
    tree = ast.parse(src)
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol and isinstance(node.ctx, ast.Load):
            hits += 1
        elif isinstance(node, ast.ImportFrom) and any(a.name == symbol for a in node.names):
            hits += 1
    return hits


# ==========================================================================
# A · 纸面字号模型（必须与真实导出函数对得上，不能只是推演）
# ==========================================================================
class TestPaperFontModel:

    def test_model_agrees_with_real_export_fit(self):
        """`paper_font_mm` vs `export._fit_image_cm` 对真实 PNG 的返回值 —— 独立重算。"""
        from app.routers.export import _fit_image_cm, _read_image_dpi

        tree = _tree(4, 4)
        (pw, ph), dpi_meta = _rendered_png(tree)
        dpi = _read_image_dpi(dpi_meta)
        assert dpi > 1, "渲染器未写出 DPI 元数据，模型口径失去依据"
        w_cm, _h_cm = _fit_image_cm(pw, ph, dpi)
        nodes, cw, ch = ma._layout_architecture_tree(tree)
        leaf_font = ma.resolve_leaf_vertical_font_px(
            cw, [n["label"] for n in nodes if n.get("vertical")],
            min_mm=_MM, canvas_h=ch, title_h=_TITLE_H)
        # 纸上毫米 = PNG 内字号毫米 × 实际缩放比（缩放比由真实 _fit_image_cm 反推）
        px_to_mm = ma.ARCH_RENDER_SCALE * 2.54 / dpi * 10.0
        expect = leaf_font * px_to_mm * (w_cm / (pw * 2.54 / dpi))
        assert abs(expect - ma.paper_font_mm(leaf_font, cw, ch, _TITLE_H)) < 0.01 * expect

    def test_supersample_and_dpi_cancel_when_width_binds(self):
        """宽度绑定时闭式解退化为 160×字号/画布宽（超采样与 DPI 全部约掉）。"""
        for font in (15, 20, 28):
            for cw in (1200, 1832, 2720):
                assert abs(ma.paper_font_mm(font, cw) - 160.0 * font / cw) < 1e-9

    def test_narrow_chart_is_inserted_at_natural_size(self):
        """画布窄到 16cm 以内时 fit=1：纸面字号按 DPI 自然尺寸，不再随宽度反比变化。"""
        natural = ma.paper_font_mm(15, 544)          # 544 设计px → PNG 1360px @300dpi → 11.5cm
        assert abs(natural - 15 * ma.ARCH_RENDER_SCALE * 2.54 / ma.ARCH_OUTPUT_DPI * 10) < 1e-9
        assert natural < 160.0 * 15 / 544, "窄图被误按 16cm 拉伸，纸面字号会虚高"

    def test_height_cap_binds_for_very_tall_canvas(self):
        """超高图受 22cm 约束：同一宽度下图越高纸面越小（模型必须计入）。"""
        assert ma.paper_font_mm(15, 800, 2000, _TITLE_H) < ma.paper_font_mm(15, 800)

    def test_model_never_raises_on_garbage(self):
        """纯函数绝不抛：畸形树（空/0 宽/负值）只能让它回退，不能炸渲染链。"""
        assert ma.paper_font_mm(0, 0) == 0.0
        assert ma.paper_font_mm(15, 0) == 0.0
        assert ma.paper_font_mm(-1, 1000) == 0.0
        assert ma.paper_font_mm(15, 1000, -5) > 0


# ==========================================================================
# B · 字号自适应的三条不变量
# ==========================================================================
class TestFontAdaptationInvariants:

    def test_min_mm_is_a_required_argument(self):
        """`min_mm` 必须必填 —— 有默认值时它只能取常量，忘传的调用点会对运维杀开关
        静默失明（本轮就是被这条逼出了真缺陷）。用 TypeError 把契约钉死。"""
        with pytest.raises(TypeError):
            ma.resolve_paper_font_px(1832, base=15, ceiling=18)
        with pytest.raises(TypeError):
            ma.resolve_leaf_vertical_font_px(1832, ["作业面"])

    def test_never_shrinks_below_base(self):
        """任何画布宽度下都**不会比旧固定字号更小** —— 本轮零回归面的根源。"""
        for cw in range(300, 6000, 137):
            assert ma.resolve_paper_font_px(cw, base=15, ceiling=18, min_mm=_MM,
                                            canvas_h=352, title_h=_TITLE_H) >= 15
            assert ma.resolve_leaf_vertical_font_px(cw, ["作业面"], min_mm=_MM) >= ma.LEAF_VERTICAL_FONT_SIZE

    def test_never_exceeds_ceiling(self):
        for cw in (1000, 3000, 9000, 30000):
            assert ma.resolve_paper_font_px(cw, base=15, ceiling=18, min_mm=_MM,
                                            canvas_h=352, title_h=_TITLE_H) <= 18
            assert ma.resolve_leaf_vertical_font_px(cw, ["短标签"], min_mm=_MM) <= ma.LEAF_VERTICAL_FONT_MAX

    def test_monotonic_in_canvas_width(self):
        """画布越宽要求字号越大（否则大图仍按小图字号出，等于没修）。"""
        seq = [ma.resolve_paper_font_px(cw, base=15, ceiling=26, min_mm=_MM,
                                        canvas_h=352, title_h=_TITLE_H)
               for cw in range(800, 5000, 100)]
        assert all(b >= a for a, b in zip(seq, seq[1:])), seq

    def test_target_is_met_when_ceiling_allows(self):
        """只要上限够，纸面就必须 ≥ 目标下限（本功能的验收口径）。"""
        for cw in (900, 1200, 1600, 2000):
            font = ma.resolve_paper_font_px(cw, base=15, ceiling=40, min_mm=_MM,
                                            canvas_h=352, title_h=_TITLE_H)
            assert ma.paper_font_mm(font, cw, 352, _TITLE_H) >= ma.PAPER_FONT_MIN_MM - 1e-6

    def test_disabled_env_returns_exact_base(self, monkeypatch):
        """环境置 0 → `paper_font_min_mm()` 返回 0 → 字号逐字回退 R59 固定值。"""
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "0")
        assert ma.paper_font_min_mm() == 0.0
        assert ma.resolve_paper_font_px(4000, base=15, ceiling=18,
                                        min_mm=ma.paper_font_min_mm()) == 15
        assert ma.resolve_leaf_vertical_font_px(
            4000, ["x" * 6], min_mm=ma.paper_font_min_mm()) == ma.LEAF_VERTICAL_FONT_SIZE
        assert ma.auto_vertical_enabled() is False

    def test_negative_env_also_disables(self, monkeypatch):
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "-1")
        assert ma.paper_font_min_mm() == 0.0

    def test_garbage_env_falls_back_to_default_not_off(self, monkeypatch):
        """打错一个字母不该让交付文档里的图突然变小：非法值回落默认，**不是关闭**。"""
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "two-point-four")
        assert ma.paper_font_min_mm() == ma.PAPER_FONT_MIN_MM

    def test_explicit_tuning_env_is_honoured(self, monkeypatch):
        """运维调高目标 → 同一画布要求更大字号（证明环境真的接进了链路）。"""
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "3.5")
        assert ma.paper_font_min_mm() == pytest.approx(3.5)
        raised = ma.resolve_paper_font_px(1832, base=15, ceiling=40,
                                          min_mm=ma.paper_font_min_mm(),
                                          canvas_h=352, title_h=_TITLE_H)
        default = ma.resolve_paper_font_px(1832, base=15, ceiling=40, min_mm=_MM,
                                           canvas_h=352, title_h=_TITLE_H)
        assert raised > default, (raised, default)


# ==========================================================================
# C · 竖排放大不得新增省略号（真实字体度量端到端判定）
# ==========================================================================
class TestNoNewEllipsisUnderAdaptation:

    def test_capacity_formula_is_shared_with_the_drawer(self):
        """自适应估算与真正绘制必须共用同一函数 —— 两套口径就是假绿。"""
        assert _COMMON_SRC.count("def _vertical_text_capacity(") == 1
        assert _names_used(_COMMON_SRC, "_vertical_text_capacity") == 1   # 绘制侧调用
        assert _names_used(_ARCH_SRC, "_vertical_text_capacity") == 2     # 导入 + 自适应调用
        # 列容量算式只能有一处（第二处就是复制粘贴出来的分叉）
        assert _COMMON_SRC.count("per_col = max(1,") == 1
        assert _COMMON_SRC.count("max_cols = max(1,") == 1

    @pytest.mark.parametrize("n_groups,per_group,label_len", [
        (1, 2, 5), (3, 2, 5), (4, 4, 5), (4, 2, 12), (4, 4, 18),
        (4, 4, 24), (4, 4, 40), (6, 5, 9),
    ])
    def test_every_leaf_is_drawn_in_full_at_resolved_font(self, n_groups, per_group, label_len):
        """**核心护栏**：按自适应字号真画一遍，回传文字必须等于原标签（无 `…`）。

        刻意不用 `resolve_leaf_vertical_font_px` 内部的近似公式自证 —— 它与真实
        字体度量差 1px 就可能多/少一列，那样护栏会永远绿而实际漏字。
        """
        tree = _tree(n_groups, per_group, label_len=label_len)
        nodes, cw, ch = ma._layout_architecture_tree(tree, vertical=True)
        labels = [n["label"] for n in nodes if n.get("vertical")]
        assert labels, "该形态没有竖排节点，用例失效"
        font = ma.resolve_leaf_vertical_font_px(cw, labels, min_mm=_MM,
                                                canvas_h=ch, title_h=_TITLE_H)
        for lb in labels:
            assert _drawn_text(lb, font) == lb.strip(), (
                f"字号 {font}px 下标签被省略号收口：{lb!r}（画布 {cw}px）")

    def test_long_labels_pin_the_font_back_down(self):
        """同一画布下，长标签必须把字号压回小值（宁可小一点也要看得全）。"""
        short = ma.resolve_leaf_vertical_font_px(1832, ["作业面00"], min_mm=_MM,
                                                 canvas_h=532, title_h=_TITLE_H)
        long = ma.resolve_leaf_vertical_font_px(1832, ["作" * 30], min_mm=_MM,
                                                canvas_h=532, title_h=_TITLE_H)
        assert long < short
        assert long >= ma.LEAF_VERTICAL_FONT_SIZE

    def test_capacity_shrinks_as_font_grows(self):
        """字号与容量反向 —— 这条物理事实正是 C 组存在的全部理由。"""
        box_w = ma.LEAF_VERTICAL_W - 2 * ma.LEAF_VERTICAL_PAD_X
        box_h = ma.LEAF_VERTICAL_H - 2 * ma.LEAF_VERTICAL_PAD_Y
        cap_small = _vertical_text_capacity(box_w, box_h, 15, ma.LEAF_VERTICAL_CHAR_GAP)[2]
        cap_big = _vertical_text_capacity(box_w, box_h, 28, ma.LEAF_VERTICAL_CHAR_GAP)[2]
        assert cap_big < cap_small
        assert cap_big >= 12, "上限字号下容量过小，说明 LEAF_VERTICAL_FONT_MAX 定得太高"

    def test_empty_labels_never_raise(self):
        assert (ma.resolve_leaf_vertical_font_px(1832, [], min_mm=_MM)
                == ma.resolve_leaf_vertical_font_px(1832, [""], min_mm=_MM))
        assert _drawn_text("   ", 20) == ""


# ==========================================================================
# D · 可读性驱动的竖排升级（与 R59 数量规则相互独立）
# ==========================================================================
class TestReadabilityEscalation:

    def test_small_chart_stays_horizontal(self):
        """4 叶/2 组横排画布 1032px，字号到上限时纸面 2.79mm ≥ 目标 → 不升级。"""
        tree = _tree(2, 2)
        assert ma.paper_font_mm(ma.NODE_FONT_MAX, ma.horizontal_canvas_width(tree)) >= ma.PAPER_FONT_MIN_MM
        assert ma.resolve_leaf_vertical_mode(tree) is False

    def test_wide_but_few_leaves_escalates(self):
        """R59 漏掉的形态：叶子数没超阈值，但横排已经印不清 → 必须升级。

        6 叶 + 1 直属 = 13 节点，横排画布 1764px，字号顶格也只有 2.11mm。
        """
        tree = _tree(3, 2, extra_depth1=1)
        candidates = ma.count_vertical_leaf_candidates(tree)
        assert candidates <= ma.vertical_leaf_threshold(), "本例要锁的是数量规则之外的分支"
        assert ma.resolve_leaf_vertical_mode(tree, auto_vertical=False) is False
        assert ma.resolve_leaf_vertical_mode(tree) is True

    def test_escalation_only_switches_mode_not_geometry(self):
        """升级只是"换了个版式判定结果"，几何必须与显式 `vertical=True` 逐值相同。"""
        tree = _tree(3, 2, extra_depth1=1)
        auto, wa, ha = ma._layout_architecture_tree(tree)
        forced, wf, hf = ma._layout_architecture_tree(tree, vertical=True)
        assert (wa, ha) == (wf, hf)
        assert [(n["x"], n["y"], n["w"], n["h"], n["vertical"]) for n in auto] == \
               [(n["x"], n["y"], n["w"], n["h"], n["vertical"]) for n in forced]

    def test_tree_without_candidates_never_escalates(self):
        """depth=1 的直属叶子不参与竖排（R59 §3.2）：无候选时升级必须空转，
        否则日志会谎称"竖排"而几何一点没变。"""
        tree = {"label": "根", "children": [{"label": f"部{i}"} for i in range(12)]}
        assert ma.count_vertical_leaf_candidates(tree) == 0
        assert ma.resolve_leaf_vertical_mode(tree) is False

    def test_count_rule_still_wins_when_escalation_is_off(self, monkeypatch):
        monkeypatch.delenv(ma.ENV_PAPER_FONT_MIN_MM, raising=False)
        tree = _tree(4, 3)   # 12 个可竖排叶子 > 6
        assert ma.resolve_leaf_vertical_mode(tree, auto_vertical=False) is True
        assert ma.resolve_leaf_vertical_mode(tree, auto_vertical=True) is True

    def test_master_switch_short_circuits_everything(self, monkeypatch):
        """`ARCH_LEAF_VERTICAL=0` 必须压过本轮两条新判据（运维只记一个总开关）。"""
        monkeypatch.setenv(ma.ENV_VERTICAL_SWITCH, "0")
        assert ma.resolve_leaf_vertical_mode(_tree(6, 4)) is False
        assert ma.resolve_leaf_vertical_mode(_tree(3, 2, extra_depth1=1)) is False

    def test_escalation_respects_disabled_floor_env(self, monkeypatch):
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "0")
        assert ma.resolve_leaf_vertical_mode(_tree(3, 2, extra_depth1=1)) is False

    def test_escalation_can_be_disabled_independently(self, monkeypatch):
        """只关升级、保留字号自适应：运维想维持 R59 的「≤6 恒横排」观感时有退路。"""
        monkeypatch.setenv(ma.ENV_AUTO_VERTICAL, "0")
        assert ma.auto_vertical_enabled() is False
        assert ma.resolve_leaf_vertical_mode(_tree(3, 2, extra_depth1=1)) is False
        assert ma.paper_font_min_mm() == ma.PAPER_FONT_MIN_MM

    def test_horizontal_canvas_width_matches_layout(self):
        """升级判据量的是横排版式，必须与 `_layout_architecture_tree(vertical=False)` 同值。"""
        tree = _tree(3, 2, extra_depth1=1)
        _n, w, _h = ma._layout_architecture_tree(tree, vertical=False)
        assert ma.horizontal_canvas_width(tree) == w


# ==========================================================================
# E · 渲染接线与可观测性
# ==========================================================================
class TestRendererWiringAndObservability:

    def test_renderer_uses_the_shared_scale_constant(self):
        """`scale = 2.5` 字面量不得残留：模型与渲染器不同源时纸面换算就是空谈。"""
        assert "scale = ARCH_RENDER_SCALE" in _ARCH_SRC
        assert "scale = 2.5" not in _ARCH_SRC

    def test_renderer_reads_the_env_through_the_function(self):
        """渲染器必须经 `paper_font_min_mm()` 取阈值（直接取常量 = 杀开关失效）。"""
        assert "min_mm = paper_font_min_mm()" in _ARCH_SRC
        assert "min_mm=PAPER_FONT_MIN_MM" not in _ARCH_SRC

    def test_escalated_chart_renders_narrower_than_disabled(self, monkeypatch):
        tree = _tree(3, 2, extra_depth1=1)
        on_size, _dpi = _rendered_png(tree)
        monkeypatch.setenv(ma.ENV_PAPER_FONT_MIN_MM, "0")
        off_size, _ = _rendered_png(tree)
        assert on_size[0] < off_size[0], (on_size, off_size)
        assert on_size[1] > off_size[1], (on_size, off_size)

    def test_wide_chart_gets_bigger_glyphs_than_r59(self):
        """16 叶/4 组：字号从固定 15 抬到上限，纸上字号应显著提升（R59 遗留项①）。"""
        tree = _tree(4, 4)
        nodes, cw, ch = ma._layout_architecture_tree(tree)
        font = ma.resolve_leaf_vertical_font_px(
            cw, [n["label"] for n in nodes if n.get("vertical")],
            min_mm=_MM, canvas_h=ch, title_h=_TITLE_H)
        assert font > ma.LEAF_VERTICAL_FONT_SIZE
        assert ma.paper_font_mm(font, cw, ch, _TITLE_H) >= ma.PAPER_FONT_MIN_MM

    def test_warning_fires_only_for_unfixable_charts(self, caplog):
        """普通图零噪音；只有「字号封顶仍不达标」才 warning（这才是需要拆图的信号）。"""
        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_architecture"):
            caplog.clear()
            ma._render_architecture_image_v2({"root": _tree(4, 4)})       # 16 叶：能救
            assert [r for r in caplog.records if "纸面字号" in r.getMessage()] == []
            caplog.clear()
            ma._render_architecture_image_v2({"root": _tree(8, 6)})       # 48 叶：救不回来
            msgs = [r.getMessage() for r in caplog.records]
            assert any("纸面字号" in m and "拆分" in m for m in msgs), msgs

    def test_completion_log_carries_paper_millimetres(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="app.services.ai.mermaid_architecture"):
            caplog.clear()
            ma._render_architecture_image_v2({"root": _tree(4, 4)})
            joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "纸面≈" in joined and "mm" in joined, joined
        assert "字号(标题/节点/叶子)" in joined

    def test_render_still_produces_valid_png_for_extremes(self):
        """畸形/极端数据不得因为新增字号逻辑而炸。"""
        for tree in (_tree(1, 1, label_len=60), _tree(10, 6), {"label": "光杆"}):
            bio = ma._render_architecture_image_v2({"root": tree})
            assert bio is not None and len(bio.getvalue()) > 1000


# ==========================================================================
# G · 合并告警（纸面字号不足 × 规模超预算 → 恰一条）
# ==========================================================================
_OK_BUDGET = (False, 4, 8)
_OK_CAPS = (24, 60)


def _warn(canvas_w, eff_px, eff_mm, min_mm, cand, budget=_OK_BUDGET, caps=_OK_CAPS):
    """按生产调用形态取合并告警文本（纯函数，不碰渲染路径）。"""
    return ma.compose_readability_warning(canvas_w, eff_px, eff_mm, min_mm, cand, budget, caps)


class TestMergedReadabilityWarning:

    def test_fine_chart_produces_no_text(self):
        assert _warn(1000, 18, 2.88, _MM, 0) == ""

    def test_unreadable_alone_keeps_mm_diagnosis_and_advice(self):
        msg = _warn(3050, 28, 1.31, _MM, 16)
        assert "纸面字号仅 1.31mm" in msg and "目标 ≥2.4mm" in msg, msg
        assert "画布 3050px" in msg and "可竖排叶子 16 个" in msg
        assert "拆分" in msg, "只说问题不说对策的告警等于没告警"
        assert "超出规模预算" not in msg, "未超预算却报预算 = 假故障"

    def test_over_budget_alone_keeps_foreign_substrings(self):
        """规模守卫护栏锁的是**字面子串**，合并后必须在位（跨轮契约不得被本轮顺手改掉）。"""
        msg = _warn(600, 15, 4.00, _MM, 0, budget=(True, 30, 61), caps=(5, 60))
        assert "架构图超出规模预算" in msg, msg
        assert "建议拆分" in msg, msg
        assert "纸面字号仅" not in msg, "印得清却报字号 = 假故障"

    def test_both_conditions_collapse_into_one_sentence(self):
        msg = _warn(3050, 28, 1.31, _MM, 48, budget=(True, 48, 55), caps=(24, 60))
        assert "纸面字号仅" in msg and "架构图超出规模预算" in msg, msg
        assert "同时" in msg, "两条诊断需读成一因一果，不是并列的两个故障"

    def test_split_suggestion_rounds_up(self):
        msg = _warn(600, 15, 4.00, _MM, 0, budget=(True, 30, 31), caps=(5, 60))
        assert "约 6 张子图" in msg, msg

    def test_split_suggestion_never_below_one(self):
        msg = _warn(600, 15, 4.00, _MM, 0, budget=(True, 1, 99), caps=(24, 60))
        assert "约 1 张子图" in msg, msg

    def test_zero_max_leaves_does_not_raise(self):
        """上限被配成 0 时不许把渲染路径炸掉（本仓红线：告警永不抛）。"""
        msg = _warn(600, 15, 4.00, _MM, 0, budget=(True, 30, 31), caps=(0, 0))
        assert "超出规模预算" in msg

    def test_min_mm_zero_silences_only_the_font_branch(self):
        msg = _warn(3050, 28, 1.31, 0.0, 48, budget=(True, 48, 55), caps=(24, 60))
        assert "纸面字号仅" not in msg and "超出规模预算" in msg, msg

    def test_message_declares_data_is_not_dropped(self):
        assert "本图仍按完整数据渲染" in _warn(3050, 28, 1.31, _MM, 48)


class TestSingleWarningSite:
    """合并的**唯一可验证形态**是一张图只落一条 warning ⇒ 判据落在日志条数上。"""

    def test_oversized_chart_warns_exactly_once(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_architecture"):
            caplog.clear()
            assert ma._render_architecture_image_v2({"root": _tree(8, 6)}) is not None
            warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1, f"同一张图落了 {len(warns)} 条告警：{warns}"
        assert "纸面字号仅" in warns[0] and "超出规模预算" in warns[0], warns[0]

    def test_normal_chart_warns_zero(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_architecture"):
            caplog.clear()
            assert ma._render_architecture_image_v2({"root": _tree(2, 2)}) is not None
            assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_renderer_body_contains_exactly_one_warning_call(self):
        """AST 级：渲染函数体内 `logger.warning` 恰 1 处（退回双告警形态会变红）。"""
        fn = next(n for n in ast.parse(_ARCH_SRC).body
                  if isinstance(n, ast.FunctionDef) and n.name == "_render_architecture_image_v2")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "warning"
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
        assert len(calls) == 1, [ast.unparse(c) for c in calls]


# ==========================================================================
# F · 跨层常量 parity + 源文件字节体检
# ==========================================================================
_TOUCHED = [
    _BE / "app" / "services" / "ai" / "mermaid_architecture.py",
    _BE / "app" / "services" / "ai" / "mermaid_common.py",
    _BE / "app" / "services" / "ai" / "mermaid_renderer.py",
    _BE / "app" / "routers" / "export.py",
    Path(__file__).resolve(),
]


class TestCrossLayerAndBytes:

    def test_paper_geometry_constants_match_export_authority(self):
        """栏宽/高度上限的权威在导出侧；服务层抄一份就必须被锁住。"""
        from app.routers import export

        assert ma.PAPER_COLUMN_WIDTH_CM == export._CHART_MAX_WIDTH_CM
        assert ma.PAPER_MAX_HEIGHT_CM == export._CHART_MAX_HEIGHT_CM
        _size, dpi = _rendered_png(_tree(2, 2))
        assert abs(float(dpi[0]) - ma.ARCH_OUTPUT_DPI) < 0.5, "渲染器写出的 DPI 与模型不同源"

    def test_renderer_reexports_capacity_helper(self):
        from app.services.ai import mermaid_renderer as mr
        assert mr._vertical_text_capacity is _vertical_text_capacity

    @pytest.mark.parametrize("path", _TOUCHED, ids=lambda p: p.name)
    def test_no_line_ending_or_control_damage(self, path):
        raw = path.read_bytes()
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{path.name} 含孤立 CR（\\r\\r\\n 污染）"
        assert raw.count(b"\n") == crlf, f"{path.name} 含裸 LF（整文件被改成 LF）"
        assert crlf > 0, f"{path.name} 行尾形态异常（CRLF=0）"
        for ctrl in (b"\x00", b"\x07", b"\x0c"):
            assert raw.count(ctrl) == 0, f"{path.name} 含控制字节 {ctrl!r}"
        assert "\ufffd" not in raw.decode("utf-8"), f"{path.name} 含 U+FFFD"
        py_compile.compile(str(path), doraise=True)
