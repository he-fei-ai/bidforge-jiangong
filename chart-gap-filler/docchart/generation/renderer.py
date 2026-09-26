# -*- coding: utf-8 -*-
"""图表渲染器（Matplotlib 后端）。

- 构建器注册表 BUILDERS：chart_type -> 绘制函数，插件化扩展点
  （如需 Mermaid/Graphviz 后端，注册同名类型或新类型即可）；
- 统一处理中文字体、主题、尺寸、DPI、PNG/SVG 输出。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")  # 无界面环境下渲染
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch  # noqa: E402

from ..config import Config  # noqa: E402
from ..models import ChartSpec  # noqa: E402

logger = logging.getLogger("docchart.render")

BUILDERS: dict[str, Callable[[ChartSpec, Config], "plt.Figure"]] = {}


def builder(name: str):
    """注册图表构建器的装饰器。"""
    def deco(fn):
        BUILDERS[name] = fn
        return fn
    return deco


def _setup_style(cfg: Config) -> None:
    fonts = cfg.get("render", "fonts", ["Microsoft YaHei", "SimHei"])
    plt.rcParams["font.sans-serif"] = list(fonts)
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 11


def _new_fig(cfg: Config, flow: bool = False):
    size = cfg.get("render", "flow_size", [8, 6]) if flow else cfg.get("render", "figure_size", [8, 4.5])
    fig, ax = plt.subplots(figsize=(size[0], size[1]))
    return fig, ax


def _finish(fig, ax, spec: ChartSpec, cfg: Config, out_path: Path) -> Path:
    if spec.title:
        ax.set_title(spec.title, fontsize=13, pad=12)
    if spec.note:
        fig.text(0.5, 0.015, spec.note, ha="center", fontsize=8, color="#888888")
    fig.tight_layout(rect=(0, 0.04 if spec.note else 0, 1, 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=int(cfg.get("render", "dpi", 150)),
                facecolor="white")
    plt.close(fig)
    logger.info("已渲染 %s -> %s", spec.chart_type, out_path)
    return out_path


# ---------------------------------------------------------------------------
# 统计图
# ---------------------------------------------------------------------------

@builder("bar")
def _bar(spec: ChartSpec, cfg: Config):
    fig, ax = _new_fig(cfg)
    data = spec.data
    cats = data.categories
    n_series = len(data.numeric_series())
    width = 0.8 / max(n_series, 1)
    for i, s in enumerate(data.numeric_series()):
        xs = [j + i * width - 0.4 + width / 2 for j in range(len(cats))]
        ax.bar(xs, s.values, width=width, label=s.name)
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels([c if len(c) <= 10 else c[:9] + "…" for c in cats],
                       rotation=30 if any(len(c) > 5 for c in cats) else 0,
                       ha="right" if any(len(c) > 5 for c in cats) else "center")
    if data.x_label:
        ax.set_xlabel(data.x_label)
    if n_series > 1:
        ax.legend(fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.35)
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


@builder("line")
def _line(spec: ChartSpec, cfg: Config):
    fig, ax = _new_fig(cfg)
    data = spec.data
    cats = data.categories
    for s in data.numeric_series():
        ax.plot(range(len(cats)), s.values, marker="o", label=s.name)
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels([c if len(c) <= 10 else c[:9] + "…" for c in cats],
                       rotation=30 if any(len(c) > 5 for c in cats) else 0, ha="right")
    if data.x_label:
        ax.set_xlabel(data.x_label)
    ax.legend(fontsize=9)
    ax.grid(linestyle="--", alpha=0.35)
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


@builder("pie")
def _pie(spec: ChartSpec, cfg: Config):
    fig, ax = _new_fig(cfg)
    data = spec.data
    s = data.numeric_series()[0]
    vals = [max(v, 0) for v in s.values]
    palette = cfg.get("render", "palette", None)
    wedges, _, autotexts = ax.pie(
        vals, labels=[c if len(c) <= 12 else c[:11] + "…" for c in data.categories],
        autopct="%1.1f%%", startangle=90, colors=palette, textprops={"fontsize": 9})
    ax.axis("equal")
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


@builder("scatter")
def _scatter(spec: ChartSpec, cfg: Config):
    fig, ax = _new_fig(cfg)
    data = spec.data
    ns = data.numeric_series()
    if len(ns) >= 2:
        ax.scatter(ns[0].values, ns[1].values, s=40)
        ax.set_xlabel(ns[0].name)
        ax.set_ylabel(ns[1].name)
    elif len(ns) == 1:
        ax.scatter(range(len(data.categories)), ns[0].values, s=40)
        ax.set_xlabel(data.x_label or "序号")
        ax.set_ylabel(ns[0].name)
    ax.grid(linestyle="--", alpha=0.35)
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


@builder("timeline")
def _timeline(spec: ChartSpec, cfg: Config):
    """类目为节点的简易时间线（等距里程碑）。"""
    fig, ax = _new_fig(cfg)
    data = spec.data
    marks = data.categories if data else []
    n = max(len(marks), 1)
    xs = list(range(n))
    ax.hlines(0, -0.5, n - 0.5, color="#4E79A7", lw=2)
    for x, c in zip(xs, marks):
        ax.plot(x, 0, "o", ms=8, color="#E15759")
        va = "bottom" if x % 2 == 0 else "top"
        ax.annotate(c if len(c) <= 14 else c[:13] + "…", (x, 0), xytext=(0, 14 if va == "bottom" else -18),
                    textcoords="offset points", ha="center", va=va, fontsize=9)
    ax.set_ylim(-1, 1)
    ax.set_xlim(-0.8, n - 0.2)
    ax.axis("off")
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


# ---------------------------------------------------------------------------
# 结构图（数据不足时的示意路径）
# ---------------------------------------------------------------------------

@builder("flow")
def _flow(spec: ChartSpec, cfg: Config):
    """竖向流程图：圆角框 + 箭头。"""
    steps = spec.steps or []
    n = len(steps)
    fig, ax = _new_fig(cfg, flow=True)
    if n == 0:
        ax.axis("off")
        return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)
    box_h, gap_h = 0.11, 0.06
    total = n * box_h + (n - 1) * gap_h
    y0 = 0.5 + total / 2
    centers = []
    for i in range(n):
        y = y0 - i * (box_h + gap_h) - box_h / 2
        centers.append((0.5, y))
    palette = cfg.get("render", "palette", ["#4E79A7"])
    for i, ((cx, cy), label) in enumerate(zip(centers, steps)):
        box = FancyBboxPatch((cx - 0.28, cy - box_h / 2), 0.56, box_h,
                             boxstyle="round,pad=0.012,rounding_size=0.02",
                             linewidth=1.2, edgecolor=palette[i % len(palette)],
                             facecolor="#EAF1F8")
        ax.add_patch(box)
        text = label if len(label) <= 22 else label[:21] + "…"
        ax.text(cx, cy, text, ha="center", va="center", fontsize=10)
        if i < n - 1:
            ax.add_patch(FancyArrowPatch((cx, cy - box_h / 2), (cx, cy - box_h / 2 - gap_h),
                                         arrowstyle="-|>", mutation_scale=14, color="#555555"))
    ax.set_xlim(0, 1)
    ax.set_ylim(centers[-1][1] - box_h, y0 + 0.05)
    ax.axis("off")
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


@builder("relation")
def _relation(spec: ChartSpec, cfg: Config):
    """中心辐射关系图：首步骤为中心节点，其余环绕。"""
    steps = spec.steps or []
    n = len(steps)
    fig, ax = _new_fig(cfg, flow=True)
    if n == 0:
        ax.axis("off")
        return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)
    center = steps[0]
    others = steps[1:9]
    r = 0.34
    palette = cfg.get("render", "palette", ["#4E79A7"])
    ax.add_patch(FancyBboxPatch((0.5 - 0.16, 0.5 - 0.06), 0.32, 0.12,
                                boxstyle="round,pad=0.01,rounding_size=0.03",
                                edgecolor=palette[0], facecolor="#D6E4F0", lw=1.5))
    ax.text(0.5, 0.5, center if len(center) <= 14 else center[:13] + "…",
            ha="center", va="center", fontsize=11, weight="bold")
    m = max(len(others), 1)
    for i, label in enumerate(others):
        ang = 2 * math.pi * i / m - math.pi / 2
        x, y = 0.5 + r * math.cos(ang), 0.5 + r * math.sin(ang) * 0.85
        ax.add_patch(FancyArrowPatch((0.5, 0.5), (x, y), arrowstyle="-", color="#999999", lw=1))
        ax.add_patch(FancyBboxPatch((x - 0.13, y - 0.05), 0.26, 0.10,
                                    boxstyle="round,pad=0.008,rounding_size=0.025",
                                    edgecolor=palette[(i + 1) % len(palette)],
                                    facecolor="#F4F8FC"))
        ax.text(x, y, label if len(label) <= 12 else label[:11] + "…",
                ha="center", va="center", fontsize=9)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax, lambda p: _finish(fig, ax, spec, cfg, p)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def render(spec: ChartSpec, cfg: Config, out_path: str | Path) -> Path:
    """按类型分发渲染，输出 PNG/SVG（扩展名由 out_path 决定）。"""
    _setup_style(cfg)
    fn = BUILDERS.get(spec.chart_type)
    if fn is None:
        raise ValueError(f"未注册的图表类型: {spec.chart_type}（可用: {', '.join(sorted(BUILDERS))}）")
    fig, ax, finish = fn(spec, cfg)
    return finish(Path(out_path))
