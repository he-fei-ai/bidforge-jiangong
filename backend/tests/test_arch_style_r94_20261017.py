# -*- coding: utf-8 -*-
"""R94 · 架构图统一样张风格 v2 护栏（2026-10-17）

用户诉求：所有类型架构图按提供样张生成和渲染。样张取证（artifacts/r94/probe_style2.py
对 2000px 原图逐行/列像素扫描）：
  根=#1E3A5F · 一级=#2B579A · 二级=#4472C4 · highlight=#FBBF24（深藏青粗体字）
  节点=圆角 + 深色描边（填充×0.6）+ 右下柔和投影 #D5D9DF
  连线=#64748B 蓝灰直角折线 + 子端小下箭头；标题=#1E3A5F 粗体

覆盖：
  A 样式常量单一事实源（取证色逐字锁）
  B 像素级行为：描边/投影/箭头/标题色/highlight 琥珀金（真实渲染）
  C 四类子类型（organization/wbs/system/infrastructure）共用同一渲染路径与样张
  D 提示词与源码静态锁（旧色字面量不得回流；highlight 措辞与渲染器口径一致）
"""

import app.services.ai.mermaid_architecture as ma
from PIL import Image

_SCALE = 2.5
_TITLE_H = 56

# 样张取证色（与探针输出逐字一致；本文件独立复写一份，防渲染器常量被顺手改糊）
RGB_ROOT = (30, 58, 95)  # #1E3A5F
RGB_L1 = (43, 87, 154)  # #2B579A
RGB_L2 = (68, 114, 196)  # #4472C4
RGB_AMBER = (251, 191, 36)  # #FBBF24
RGB_EDGE = (100, 116, 139)  # #64748B
RGB_SHADOW = (213, 217, 223)  # #D5D9DF


def _render(tree: dict) -> Image.Image:
    bio = ma._render_architecture_image_v2(tree)
    assert bio is not None
    if hasattr(bio, "seek"):
        bio.seek(0)
    return Image.open(bio).convert("RGB")


def _root_and_child(tree: dict):
    nodes, _w, _h = ma._layout_architecture_tree(tree)
    root = next(n for n in nodes if n["depth"] == 0)
    child = next(n for n in nodes if n["depth"] == 1)
    return nodes, root, child


def _box_px(n: dict):
    x0 = int(n["x"] * _SCALE)
    y0 = int(n["y"] * _SCALE) + _TITLE_H
    return x0, y0, x0 + int(n["w"] * _SCALE), y0 + int(n["h"] * _SCALE)


def _count_in_box(img: Image.Image, n: dict, rgb: tuple, inset: int = 8) -> int:
    x0, y0, x1, y1 = _box_px(n)
    px = img.load()
    return sum(1 for y in range(y0 + inset, y1 - inset) for x in range(x0 + inset, x1 - inset) if px[x, y] == rgb)


# ---------------------------------------------------------------- A 常量事实源


class TestStyleConstants:
    def test_depth_palette_matches_sample_evidence(self):
        assert ma.ARCH_DEPTH_COLORS == [
            ("#1E3A5F", "#FFFFFF"),
            ("#2B579A", "#FFFFFF"),
            ("#4472C4", "#FFFFFF"),
            ("#5B9BD5", "#FFFFFF"),
            ("#A9C6E8", "#1E3A5F"),
        ]

    def test_highlight_edge_shadow_title_constants(self):
        assert ma.ARCH_HIGHLIGHT_FILL == "#FBBF24"
        assert ma.ARCH_HIGHLIGHT_TEXT == "#1E3A5F"
        assert ma.ARCH_EDGE_COLOR == "#64748B"
        assert ma.ARCH_SHADOW_COLOR == "#D5D9DF"
        assert ma.ARCH_TITLE_COLOR == "#1E3A5F"

    def test_darken_formula(self):
        assert ma._darken("#1E3A5F", 0.6) == "#122239"
        # 描边必须严格深于填充（逐通道），否则样张"深色描边"判据失真
        for fill, _txt in ma.ARCH_DEPTH_COLORS:
            stroke = ma._darken(fill, ma.ARCH_STROKE_FACTOR)
            assert all(
                int(stroke[i : i + 2], 16) < int(fill[i : i + 2], 16) for i in (1, 3, 5) if int(fill[i : i + 2], 16) > 0
            )


# ------------------------------------------------------------------ B 像素级行为


class TestRenderedStyle:
    tree = {
        "label": "项目经理",
        "children": [
            {"label": "技术副经理", "children": [{"label": "技术部", "highlight": True}, {"label": "质量部"}]},
            {"label": "生产副经理", "children": [{"label": "施工部"}]},
        ],
    }

    def test_root_and_levels_use_sample_fills(self):
        nodes, root, child = _root_and_child(self.tree)
        img = _render(self.tree)
        assert _count_in_box(img, root, RGB_ROOT) > 500, "根节点未渲染为样张深藏青"
        assert _count_in_box(img, child, RGB_L1) > 500, "一级节点未渲染为样张 #2B579A"
        leaf = next(n for n in nodes if n["depth"] == 2 and not n.get("highlight"))
        assert _count_in_box(img, leaf, RGB_L2) > 500, "二级节点未渲染为样张 #4472C4"

    def test_highlight_node_amber_with_navy_text(self):
        nodes, _root, _child = _root_and_child(self.tree)
        img = _render(self.tree)
        hl = next(n for n in nodes if n.get("highlight"))
        assert _count_in_box(img, hl, RGB_AMBER) > 300, "highlight 未渲染为样张琥珀金"

    def test_node_has_dark_stroke_on_border(self):
        """样张节点带深色描边：根框左缘中段（避开圆角）应为填充×0.6 的描边色。"""
        _nodes, root, _child = _root_and_child(self.tree)
        img = _render(self.tree)
        x0, y0, _x1, y1 = _box_px(root)
        stroke = ma._darken("#1E3A5F", ma.ARCH_STROKE_FACTOR)
        stroke_rgb = tuple(int(stroke[i : i + 2], 16) for i in (1, 3, 5))
        mid_y = (y0 + y1) // 2
        band = [img.load()[x0 + k, mid_y] for k in range(0, 6)]
        assert stroke_rgb in band, f"根框左缘无样张描边（期望 {stroke_rgb}，实测 {band}）"

    def test_soft_shadow_below_right_of_node(self):
        """样张节点右下有柔和投影：根框底缘下方薄带、避开中列连线处为投影色。"""
        _nodes, root, _child = _root_and_child(self.tree)
        img = _render(self.tree)
        x0, _y0, x1, y1 = _box_px(root)
        px = img.load()
        found = sum(1 for x in range(x0 + 10, x1, 4) if px[x, y1 + 6] == RGB_SHADOW)
        assert found > 10, "根框底缘下方缺少样张投影带 #D5D9DF"

    def test_child_edge_ends_in_down_arrow(self):
        """样张连线子端有朝下小箭头：顶边上方 20px 处，连线色像素的横向
        连续宽度必须显著大于线宽本身（箭头展开），摘掉 polygon 仅剩线宽恒红。"""
        _nodes, _root, child = _root_and_child(self.tree)
        img = _render(self.tree)
        x0, y0, x1, _y1 = _box_px(child)
        px = img.load()
        cx = (x0 + x1) // 2
        y = y0 - 20  # 箭头底边附近（箭头高 int(9*2.5)=22），展开宽度≈箭宽而非线宽
        run = [x for x in range(cx - 30, cx + 30) if all(abs(px[x, y][i] - RGB_EDGE[i]) <= 8 for i in range(3))]
        width = (max(run) - min(run) + 1) if run else 0
        line_w = int(2 * _SCALE) + 2  # 线宽 int(2*2.5)=5，容差 2
        assert width >= line_w + 4, f"子端无样张下箭头（顶边上方 6px 连线色宽度={width}，仅线宽水平）"

    def test_title_painted_sample_navy(self):
        """左上角标题为样张深藏青粗体（取根框上方、标题带区域内 navy 文字像素）。"""
        img = _render(self.tree)
        px = img.load()
        cnt = sum(1 for y in range(0, 140) for x in range(0, 600) if px[x, y] == RGB_ROOT)
        assert cnt > 100, "标题未渲染为样张深藏青 #1E3A5F"


# ------------------------------------------------- C 全子类型共用同一渲染路径与样张


class TestAllSubtypesShareStyle:
    """organization/wbs/system/infrastructure 四子类型只影响校验器节点数区间，
    渲染恒走 `_render_architecture_image_v2` 单一路径 ⇒ 样张必然逐类型生效。"""

    _TREES = {
        "organization": {
            "label": "项目经理部",
            "children": [
                {"label": "工程部", "children": [{"label": "施工员"}]},
                {"label": "安质部", "children": [{"label": "安全员"}, {"label": "质检员"}]},
            ],
        },
        "wbs": {
            "label": "单位工程",
            "children": [
                {"label": "地基基础", "children": [{"label": "桩基工程"}]},
                {"label": "主体结构", "children": [{"label": "混凝土工程"}]},
            ],
        },
        "system": {
            "label": "智慧工地平台",
            "children": [
                {"label": "感知层", "children": [{"label": "视频监测"}]},
                {"label": "应用层", "children": [{"label": "预警管理"}]},
            ],
        },
        "infrastructure": {
            "label": "现场临设",
            "children": [
                {"label": "供电系统", "children": [{"label": "配电箱"}]},
                {"label": "排水系统", "children": [{"label": "集水井"}]},
            ],
        },
    }

    def test_every_subtype_renders_sample_palette(self):
        from app.services.chart_validators import detect_architecture_subtype, validate_architecture_tree

        for expect_subtype, tree in self._TREES.items():
            ok, errors = validate_architecture_tree(tree, expect_subtype)
            assert ok, f"{expect_subtype} 校验失败：{errors}"
            nodes, _w, _h = ma._layout_architecture_tree(tree)
            img = _render(tree)
            root = next(n for n in nodes if n["depth"] == 0)
            l1 = next(n for n in nodes if n["depth"] == 1)
            assert _count_in_box(img, root, RGB_ROOT) > 300, f"{expect_subtype} 根样式漂移"
            assert _count_in_box(img, l1, RGB_L1) > 300, f"{expect_subtype} 一级样式漂移"
        # 子类型探测本身仍工作（样张统一不代表探测失效）
        assert (
            detect_architecture_subtype({"label": "项目组织机构", "children": [{"label": "部门"}]}, "项目组织机构")
            == "organization"
        )


# -------------------------------------------------------------- D 静态源码与提示词锁


class TestStaticLocks:
    def test_old_style_literals_removed_from_architecture_renderer(self):
        """旧样张字面量（#FFC000 琥珀橙 / #8C8C8C 灰连线 / #2E5FA3 旧一级 /
        #1F3864 旧根 / 扁平无描边注释）不得回流到架构图渲染器。"""
        import inspect

        src = inspect.getsource(ma)
        for dead in ('"#FFC000"', '"#8C8C8C"', '"#2E5FA3"', '"#1F3864"'):
            assert dead not in src, f"旧样式字面量回流：{dead}"
        assert "扁平无阴影无描边" not in src, "旧「扁平无阴影无描边」判据注释未随样张 v2 更新"

    def test_prompts_describe_amber_highlight(self):
        """生成/修复提示词与渲染器口径一致：highlight 描述为琥珀金（不再是橙色/琥珀橙）。"""
        from pathlib import Path

        be = Path(ma.__file__).resolve().parents[3]
        charts = (be / "app" / "services" / "ai" / "prompts" / "charts.py").read_text(encoding="utf-8")
        content = (be / "app" / "services" / "ai" / "prompts" / "content.py").read_text(encoding="utf-8")
        assert "琥珀金" in charts and "橙色高亮" not in charts
        assert "琥珀金" in content and "渲染为橙色高亮" not in content

    def test_render_path_is_single_for_all_architectures(self):
        """mermaid_renderer 的 architecture 分派必须且只指向 `_render_architecture_image_v2`
        （样张单一出口的结构性保证）。"""
        from pathlib import Path

        be = Path(ma.__file__).resolve().parent
        src = (be / "mermaid_renderer.py").read_text(encoding="utf-8")
        assert "_render_architecture_image_v2(" in src
        assert "_render_architecture_image(" not in src.replace(
            "_render_architecture_image_v2(", ""
        ), "存在旧版架构图渲染旁路"
