# -*- coding: utf-8 -*-
"""R59 · 架构图 L3 叶子层竖排护栏（2026-10-10）。

来源：《架构图 L3 叶子层文字垂直显示优化方案》。文档给的是 HTML/React 版实现，
本仓架构图实际由 **PIL 渲染器**（`mermaid_architecture.py`）出 PNG，前端与 DOCX
都消费同一张位图 —— 故落地形态是「竖排节点 + 逐字竖排绘制」，而非 CSS。

覆盖（对应文档增强清单）：
  A 自动判断节点数（>6 切竖排）+ 尺寸配置化 + 运维回退开关
  B 几何：画布收窄、行不重叠、横排几何逐值不变（回退零风险）、模式不改结构
  C 竖排绘制：文字不越界、超长收敛省略号、连线仍精准对齐、highlight 仍琥珀
  D 契约：加法式字段、校验/渲染同口径、显式锁定版式
  E 源文件字节体检（R57/R58 行尾判据）+ 前后端符号 parity 锁
"""
import ast
import math
import os
import py_compile
from pathlib import Path

import app.services.ai.mermaid_architecture as ma
import pytest
from app.services.ai.mermaid_common import (
    _draw_text_vertical_center,
    _image_font,
    _text_height,
    _text_width,
)
from app.services.chart_validators import validate_architecture_tree
from PIL import Image, ImageDraw

_HERE = Path(__file__).resolve().parent
_BE = _HERE.parent
_EDGE_RGB = (140, 140, 140)   # #8C8C8C 连线灰
_AMBER_RGB = (255, 192, 0)    # #FFC000 highlight 琥珀橙


# ------------------------------------------------------------------ 造树工具
def _deep_leaf_tree(leaf_count: int, group_size: int = 3) -> dict:
    """造「根 → 若二级 → 叶子」的树，叶子总数恰为 leaf_count（全部落在 depth=2）。"""
    groups, remaining, gi = [], leaf_count, 0
    while remaining > 0:
        k = min(group_size, remaining)
        groups.append({
            "label": f"部门{gi}",
            "children": [{"label": f"组{gi}{j}"} for j in range(k)],
        })
        remaining -= k
        gi += 1
    return {"label": "质量管控体系", "children": groups}


def _mixed_depth_tree() -> dict:
    """深度混合树：depth=1 有直属叶子、depth=2 既有叶子又有内部节点、depth=3 有叶子。

    专治两类 A/B 盲区：
      · 只造「叶子全在同一层」的树，去掉 depth 限定（M2）也不会改变任何结果；
      · 只造「竖排行是最底层」的树，层高退回固定值（M3）也不会真的重叠。
    """
    return {"label": "质量管控体系", "children": [
        {"label": "直属专员"},  # depth=1 叶子：永远不得竖排
        {"label": "部门A", "children": [{"label": f"A{i}组"} for i in range(4)]},
        {"label": "部门B", "children": [{"label": f"B{i}组"} for i in range(4)]},
        {"label": "部门C", "children": [
            {"label": "C分部", "children": [{"label": "C1组"}, {"label": "C2组"}]},
        ]},
    ]}


def _by_depth(nodes):
    out: dict[int, list[dict]] = {}
    for n in nodes:
        out.setdefault(n["depth"], []).append(n)
    return out


# ==========================================================================
# A · 自动切换判定（纯函数，不依赖渲染）
# ==========================================================================
class TestVerticalModeDecision:

    def test_threshold_boundary_is_strictly_greater_than_six(self):
        """文档 §5：≤6 横排、>6 竖排。6 是边界，必须仍为横排。

        ⚠️ R60 起 `resolve_leaf_vertical_mode` 有**两条**独立判据（数量 + 可读性），
        6 叶小树会被「可读性升级」切成竖排（横排画布 ≈1540px，纸面仅 1.87mm）。
        本例锁的是**数量规则本身**，故显式关掉另一条判据 —— 不是把断言改松，
        而是把两条规则拆开各测各的（升级行为由 R60 护栏组单独锁）。
        """
        assert ma.count_vertical_leaf_candidates(_deep_leaf_tree(6)) == 6
        assert ma.resolve_leaf_vertical_mode(_deep_leaf_tree(6), auto_vertical=False) is False
        assert ma.resolve_leaf_vertical_mode(_deep_leaf_tree(7), auto_vertical=False) is True

    def test_shallow_tree_never_goes_vertical(self):
        """根下直接挂 12 个叶子（depth=1）不参与竖排：二级节点保持横排标题。

        两种模式都要锁：自动判定下「不该切」是一半，显式 `vertical=True` 时
        「depth=1 仍不得竖排」是另一半（A/B M2 首轮就是只测了前一半而跑绿）。
        """
        tree = {"label": "根", "children": [{"label": f"部{i}"} for i in range(12)]}
        assert ma.count_vertical_leaf_candidates(tree) == 0
        assert ma.resolve_leaf_vertical_mode(tree) is False
        for forced in (None, True, False):
            nodes, _w, _h = ma._layout_architecture_tree(tree, vertical=forced)
            assert all(not n["vertical"] for n in nodes), forced

    def test_candidate_count_equals_actual_vertical_nodes(self):
        """计数口径必须与真正出图的竖排节点数**完全相等**，且**不含** depth=1 直属叶子 ——
        否则「计数说 9 个、实画 6 个」这类分叉不会被任何用例抓到。"""
        for tree, expected in ((_deep_leaf_tree(9), 9), (_mixed_depth_tree(), 10)):
            nodes, _w, _h = ma._layout_architecture_tree(tree)  # 自动判定
            assert ma.count_vertical_leaf_candidates(tree) == expected
            assert sum(1 for n in nodes if n["vertical"]) == expected
            assert all(n["depth"] >= ma.LEAF_VERTICAL_MIN_DEPTH
                       for n in nodes if n["vertical"]), "depth=1 叶子被误判为竖排"

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("on", True),
        ("0", False), ("false", False), ("NO", False), ("off", False), ("关闭", False),
    ])
    def test_env_flag_truth_table(self, raw, expected):
        os.environ["R59_PROBE_FLAG"] = raw
        try:
            assert ma._env_flag("R59_PROBE_FLAG", True) is expected
        finally:
            os.environ.pop("R59_PROBE_FLAG", None)

    def test_env_flag_absent_uses_default(self):
        os.environ.pop("R59_PROBE_FLAG", None)
        assert ma._env_flag("R59_PROBE_FLAG", True) is True
        assert ma._env_flag("R59_PROBE_FLAG", False) is False

    def test_ops_switch_off_restores_legacy_horizontal(self, monkeypatch):
        """ARCH_LEAF_VERTICAL=0 → 逐字回退旧横排（运维回退通道必须真的通）。"""
        monkeypatch.setenv(ma.ENV_VERTICAL_SWITCH, "0")
        tree = _deep_leaf_tree(11)
        assert ma.leaf_vertical_enabled() is False
        assert ma.resolve_leaf_vertical_mode(tree) is False
        nodes, _w, _h = ma._layout_architecture_tree(tree)
        assert all((n["w"], n["h"]) == (ma.NODE_W, ma.NODE_H) for n in nodes)

    def test_threshold_env_override_and_illegal_fallback(self, monkeypatch):
        tree = _deep_leaf_tree(3)
        assert ma.resolve_leaf_vertical_mode(tree) is False      # 默认阈值 6
        monkeypatch.setenv(ma.ENV_VERTICAL_THRESHOLD, "2")
        assert ma.vertical_leaf_threshold() == 2
        assert ma.resolve_leaf_vertical_mode(tree) is True
        for bad in ("abc", "-3", ""):
            monkeypatch.setenv(ma.ENV_VERTICAL_THRESHOLD, bad)
            assert ma.vertical_leaf_threshold() == ma.LEAF_VERTICAL_THRESHOLD, bad


# ==========================================================================
# B · 几何
# ==========================================================================
class TestLayoutGeometry:

    def test_horizontal_geometry_is_byte_identical_to_legacy_formula(self):
        """横排回退路径的**逐值锁**：y 仍等于旧内联公式 `depth*(52+48)+60`，
        宽高仍为 220×52。竖排改造不得顺手挪动旧版式的任何一个像素。"""
        nodes, w, h = ma._layout_architecture_tree(_deep_leaf_tree(4))
        assert (ma.NODE_H, ma.V_GAP, ma.TOP_PAD) == (52, 48, 60)
        for n in nodes:
            assert n["y"] == n["depth"] * (ma.NODE_H + ma.V_GAP) + ma.TOP_PAD
            assert (n["w"], n["h"]) == (ma.NODE_W, ma.NODE_H)
            assert n["vertical"] is False
        assert w == max(n["x"] + n["w"] for n in nodes) + 80
        assert h == max(n["y"] + n["h"] for n in nodes) + 40

    def test_vertical_canvas_narrows_and_heightens_as_documented(self):
        """文档 §4.1：宽度 -54%、高度 +47%。宽度取保守下界 40%
        （字号/字体度量不参与布局，跨机器只有宽度是稳定判据）。"""
        tree = _deep_leaf_tree(11)
        _nh, wh, hh = ma._layout_architecture_tree(tree, vertical=False)
        _nv, wv, hv = ma._layout_architecture_tree(tree, vertical=True)
        shrink = 1 - wv / wh
        assert shrink > 0.40, f"宽度仅收缩 {shrink:.1%}"
        assert hv > hh, f"竖排后画布高度必须变大（{hv} <= {hh}）"

    def test_vertical_rows_do_not_overlap(self):
        """竖排叶子远高于其它层 —— 若 y 仍按「固定层高 × depth」推进，下一层会
        直接压进竖条里。本用例是 `_assign_rows_y` 存在的唯一理由。

        ⚠️ 必须用**深度混合树**：竖排行若不是最底层，固定层高才不会露馅
        （A/B M3 首轮就是在单层树上跑绿的）。
        """
        for tree in (_deep_leaf_tree(11), _mixed_depth_tree()):
            nodes, _w, _h = ma._layout_architecture_tree(tree, vertical=True)
            rows = _by_depth(nodes)
            for depth in sorted(rows)[:-1]:
                bottom = max(n["y"] + n["h"] for n in rows[depth])
                top = min(n["y"] for n in rows[depth + 1])
                assert bottom < top, f"depth {depth} 底边 {bottom} 压进 depth {depth + 1} 顶边 {top}"

    def test_vertical_only_touches_deep_leaves(self):
        for tree in (_deep_leaf_tree(9), _mixed_depth_tree()):
            nodes, _w, _h = ma._layout_architecture_tree(tree, vertical=True)
            for n in nodes:
                expect = (not n["has_children"]) and n["depth"] >= ma.LEAF_VERTICAL_MIN_DEPTH
                assert n["vertical"] is expect, n

    def test_sibling_stride_is_exact_in_both_modes(self):
        """兄弟步距 = 节点宽 + 层间距，两种模式各只有一个值。

        这条是 `_gap_after()` 的唯一判别：把竖排间距改回 H_GAP（或把返回值
        里外多算一个 gap）时，画布宽度仍会「看起来对」，只有逐项步距会红。
        """
        tree = _deep_leaf_tree(11)
        hn, _hw, _hh = ma._layout_architecture_tree(tree, vertical=False)
        vn, _vw, _vh = ma._layout_architecture_tree(tree, vertical=True)

        def strides(nodes):
            """同一父节点下的相邻兄弟步距（跨组的步距会被父盒外扩污染，不算）。"""
            groups: dict[str, list[float]] = {}
            for n in nodes:
                if not n["has_children"] and n["depth"] == 2:
                    groups.setdefault(n["parent_id"], []).append(n["x"])
            out = set()
            for xs in groups.values():
                xs.sort()
                out |= {round(b - a, 6) for a, b in zip(xs, xs[1:])}
            return out

        assert strides(hn) == {float(ma.NODE_W + ma.H_GAP)}
        assert strides(vn) == {float(ma.LEAF_VERTICAL_W + ma.LEAF_VERTICAL_GAP)}
        assert (ma.LEAF_VERTICAL_W + ma.LEAF_VERTICAL_GAP) < (ma.NODE_W + ma.H_GAP)

    def test_leftmost_node_is_normalized_to_zero(self):
        """归一化出口：最左节点 x 必须为 0（画布宽度 = 最右边界 + 留白的前提）。"""
        nodes, w, _h = ma._layout_architecture_tree(_deep_leaf_tree(11))
        assert min(n["x"] for n in nodes) == 0
        assert w == int(max(n["x"] + n["w"] for n in nodes)) + 80

    def test_mode_does_not_add_drop_or_reparent_nodes(self):
        """版式只改尺寸，**不改结构**：节点数、ID、父子关系、层深在两种模式下必须一致。"""
        tree = _deep_leaf_tree(9)
        hn, _hw, _hh = ma._layout_architecture_tree(tree, vertical=False)
        vn, _vw, _vh = ma._layout_architecture_tree(tree, vertical=True)

        def key(nodes):
            return sorted((n["id"], n["depth"], n["parent_id"], n["has_children"]) for n in nodes)

        assert key(hn) == key(vn)
        assert [n["id"] for n in hn] == [n["id"] for n in vn]

    def test_cycle_and_overdeep_trees_still_layout(self):
        """环 / 超深数据按叶子截断（旧护栏的口径不能被竖排改造打破）。

        环 root↔child 的实际产出是 3 条记录：root（内部）→ child（叶子）→
        再次遇到 root 时命中 `id(node) in visited` → 按叶子截断。
        判据是「不 RecursionError 且能出图」，节点条数只是把截断形态钉死。
        """
        root = {"label": "根"}
        child = {"label": "子", "children": [root]}
        root["children"] = [child]
        nodes, w, h = ma._layout_architecture_tree(root)
        assert len(nodes) == 3 and w > 0 and h > 0
        assert sum(1 for n in nodes if n["has_children"]) == 2
        assert ma._render_architecture_image_v2(root) is not None


# ==========================================================================
# C · 竖排绘制
# ==========================================================================
def _ink_outside(img: Image.Image, box) -> int:
    """box 之外出现非白像素的个数（越界判据）。"""
    x1, y1, x2, y2 = (int(v) for v in box)
    px = img.load()
    bad = 0
    for y in range(img.height):
        for x in range(img.width):
            if x1 <= x < x2 and y1 <= y < y2:
                continue
            if px[x, y] != (255, 255, 255):
                bad += 1
    return bad


class TestVerticalTextDrawing:

    def _draw_in_box(self, text: str, box_w: int = 90, box_h: int = 208, char_gap: int = 10):
        font = _image_font(37, bold=True)   # 15 设计像素 × 2.5 超采样
        img = Image.new("RGB", (300, 400), "white")
        draw = ImageDraw.Draw(img)
        box = (100, 50, 100 + box_w, 50 + box_h)
        _draw_text_vertical_center(draw, box, text, font, "#1F3864", char_gap=char_gap)
        return img, box, font

    def test_font_can_measure_cjk_or_test_is_skipped(self):
        """判别力前置：字体量不出汉字宽度时，下面的「有墨」断言会假绿，
        必须显式 skip 而不是静默通过。"""
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        if _text_width(draw, "国", _image_font(20, bold=True)) < 4:
            pytest.skip("本机无能量出汉字的中文字体")

    def test_glyphs_stay_inside_box(self):
        img, box, _f = self._draw_in_box("项目经理（第一责任人）")
        assert _ink_outside(img, box) == 0

    def test_long_label_collapses_to_ellipsis_instead_of_overflowing(self):
        """超长标签（文档 §2.2 的 word-break 场景）宁可省略，也不许画出边框。"""
        img, box, _f = self._draw_in_box("质量" * 120)
        assert _ink_outside(img, box) == 0
        px = img.load()
        inside = sum(1 for y in range(box[1], box[3]) for x in range(box[0], box[2])
                     if px[x, y] != (255, 255, 255))
        assert inside > 200, "竖排区域完全没有墨迹 —— 判据已失去判别力"

    def test_capacity_math_is_a_pure_function_of_box(self):
        """列容量 = 可用高 / (字高+字距)，列数 = 可用宽 / 列步进。
        锁住「容量随 box 单调」这条隐含契约（box 变高 → 每列装得更多）。"""
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = _image_font(20, bold=True)
        char_h = max(12, _text_height(draw, "国", font))
        per_col_short = (60 + 4) // (char_h + 4)
        per_col_tall = (200 + 4) // (char_h + 4)
        assert per_col_tall > per_col_short >= 1
        assert math.ceil(per_col_tall / max(1, per_col_short)) >= 2

    def test_rendered_vertical_bar_has_text_and_connected_edge(self):
        """端到端像素判据：竖条内部有文字墨迹，且节点顶边中点正上方是连线灰
        （文档 §6「连线与节点对齐 100% 精准」）。"""
        tree = _deep_leaf_tree(11)
        nodes, w, h = ma._layout_architecture_tree(tree)
        leaves = [n for n in nodes if n["vertical"]]
        bio = ma._render_architecture_image_v2(tree)
        assert bio is not None
        with Image.open(bio) as im:
            img = im.convert("RGB")
            assert img.size == (int(w * 2.5), int((h + 56) * 2.5))
            px = img.load()
            for leaf in leaves[:3]:
                x0 = int(leaf["x"] * 2.5) + 4
                x1 = int((leaf["x"] + leaf["w"]) * 2.5) - 4
                y0 = int(leaf["y"] * 2.5) + 56 + 4
                y1 = int((leaf["y"] + leaf["h"]) * 2.5) + 56 - 4
                ink = sum(1 for y in range(y0, y1, 2) for x in range(x0, x1, 2)
                          if px[x, y] != _leaf_fill_rgb(leaf))
                assert ink > 50, f"竖排节点 {leaf['label']} 内无文字墨迹"
                cx = int(leaf["x"] * 2.5) + int(leaf["w"] * 2.5) // 2
                top = int(leaf["y"] * 2.5) + 56
                above = px[cx, top - 6]
                assert abs(above[0] - _EDGE_RGB[0]) <= 8 and abs(above[1] - _EDGE_RGB[1]) <= 8 \
                    and abs(above[2] - _EDGE_RGB[2]) <= 8, \
                    f"顶边中点上方不是连线色：{above}"

    def test_highlight_leaf_keeps_amber_in_vertical_mode(self):
        tree = _deep_leaf_tree(9)
        tree["children"][0]["children"][0]["highlight"] = True
        bio = ma._render_architecture_image_v2(tree)
        with Image.open(bio) as im:
            img = im.convert("RGB")
            found = any(c == _AMBER_RGB for _n, c in (img.getcolors(maxcolors=1 << 24) or []))
            assert found, "竖排模式下 highlight 琥珀橙丢失"


def _leaf_fill_rgb(leaf: dict) -> tuple:
    depth_idx = min(leaf["depth"], 4)
    palette = ["#1F3864", "#2E5FA3", "#4472C4", "#5B9BD5", "#A9C6E8"]
    hexc = palette[depth_idx]
    return tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))


# ==========================================================================
# D · 契约（加法式扩展，旧调用方零改动）
# ==========================================================================
class TestContract:

    def test_layout_returns_three_tuple_with_legacy_keys_plus_vertical(self):
        nodes, w, h = ma._layout_architecture_tree(_deep_leaf_tree(8))
        assert isinstance(w, int) and isinstance(h, int)
        required = {"id", "label", "x", "y", "w", "h", "depth",
                    "has_children", "parent_id", "highlight", "vertical"}
        assert all(required <= set(n) for n in nodes)

    def test_explicit_mode_lock_overrides_env(self, monkeypatch):
        monkeypatch.setenv(ma.ENV_VERTICAL_SWITCH, "0")
        tree = _deep_leaf_tree(11)
        nodes, _w, _h = ma._layout_architecture_tree(tree, vertical=True)
        assert sum(1 for n in nodes if n["vertical"]) == 11

    def test_validator_and_renderer_agree_on_11_leaf_payload(self):
        """校验侧与渲染侧同口径（本仓反复踩的「渲染器能画、校验器判非法→删块」）。"""
        payload = {"type": "architecture", "root": _deep_leaf_tree(11)}
        assert validate_architecture_tree(payload)[0] is True
        bio = ma._render_architecture_image_v2(payload)
        assert bio is not None and len(bio.getvalue()) > 1000

    def test_renderer_reexports_the_new_helper(self):
        from app.services.ai import mermaid_renderer as mr
        assert mr._draw_text_vertical_center is _draw_text_vertical_center


# ==========================================================================
# E · 源文件字节体检（R57/R58 行尾判据 + 符号 parity）
# ==========================================================================
_TOUCHED = [
    _BE / "app" / "services" / "ai" / "mermaid_architecture.py",
    _BE / "app" / "services" / "ai" / "mermaid_common.py",
    _BE / "app" / "services" / "ai" / "mermaid_renderer.py",
    Path(__file__).resolve(),
]


class TestSourceByteIntegrity:

    @pytest.mark.parametrize("path", _TOUCHED, ids=lambda p: p.name)
    def test_no_line_ending_or_control_damage(self, path, tmp_path):
        raw = path.read_bytes()
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{path.name} 含孤立 CR（\r\r\n 污染）"
        assert raw.count(b"\n") == crlf, f"{path.name} 含裸 LF（整文件被改成 LF）"
        assert crlf > 0, f"{path.name} 行尾形态异常（CRLF=0）"
        for ctrl in (b"\x00", b"\x07", b"\x0c"):
            assert raw.count(ctrl) == 0, f"{path.name} 含控制字节 {ctrl!r}"
        assert "\ufffd" not in raw.decode("utf-8"), f"{path.name} 含 U+FFFD"
        py_compile.compile(str(path), doraise=True)

    def test_helper_defined_once_imported_once_and_called(self):
        common = (_BE / "app" / "services" / "ai" / "mermaid_common.py").read_text(encoding="utf-8")
        arch = (_BE / "app" / "services" / "ai" / "mermaid_architecture.py").read_text(encoding="utf-8")
        renderer = (_BE / "app" / "services" / "ai" / "mermaid_renderer.py").read_text(encoding="utf-8")
        assert common.count("def _draw_text_vertical_center(") == 1
        assert renderer.count("_draw_text_vertical_center,") == 1
        # 架构图侧只允许「导入 1 次 + 调用 1 次」：多出来就是复制了第二份实现，
        # 少掉调用就是导出了死符号（两者都不会被其它用例抓到）。
        assert arch.count("_draw_text_vertical_center") == 2
        assert "vertical-rl" in arch

    def test_architecture_module_has_no_duplicate_top_level_defs(self):
        tree = ast.parse((_BE / "app" / "services" / "ai" / "mermaid_architecture.py")
                         .read_text(encoding="utf-8"))
        seen, dups = set(), []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    dups.append(node.name)
                seen.add(node.name)
        assert dups == []
