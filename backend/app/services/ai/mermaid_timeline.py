"""Timeline 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import copy
import logging
import os
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _draw_text_center,
    _image_font,
    _image_to_stream,
    _render_hq,
    paper_floor_note,
    paper_font_mm,
)

logger = logging.getLogger(__name__)


# ============================================================
# ✅ R64：时间轴按「纸面毫米」折行（全仓最差的一张图：24 事件印到纸上名称档
#    0.38mm / 最小档 0.28mm；R60 单档口径记的 0.31mm 是同一张图、同一画布
#    15380x825 —— 本轮把播报拆成每一档，所以一个位置出现两个读数）
# ============================================================
#   机理（与 R60/R62 同一套闭式，已用真实 PNG + 真实 `routers/export._fit_image_cm` 核对）：
#     宽度受限时  纸面mm = 160 × 字号px ÷ 画布宽   （scale 与 DPI 在分子分母同现、约掉）
#     高度受限时  纸面mm = 220 × 字号px ÷ 画布高   （22cm 高上限的对称式）
#   旧实现只吃到第一条：画布宽 = 960 起 + 每事件 230 + 间隔 24，**随事件数线性膨胀
#   且无上界** ⇒ 纸面字号按 1/n 衰减，而日志对「印到纸上多大字」一字未提。
#   把一行折成 R 行，宽度按 ceil(n/R) 收窄、代价是高度每行 +170px ⇒ 两个上限交叉，
#   **最优行数存在且可算**（正是 R62 实测那条「先升后降」曲线）。所以行数是算出来的，
#   不是拍一个「每行最多 6 个」—— 拍出来的数字在 n=8 时过度折行、在 n=30 时又不够。
_ENV_AUTO_WRAP = "TIMELINE_AUTO_WRAP"           # 缺开；0/false/no/off 回退单行
_ENV_MAX_ROWS = "TIMELINE_MAX_ROWS_PER_FIGURE"  # 缺 12；0/非法 → 默认
_FALSY = ("0", "false", "no", "off")
_MAX_ROWS_DEFAULT = 12       # 再长就该拆成多张图，而不是继续在同一张图里折行
_WRAP_GAIN = 1.2             # 纸面毫米至少提升 20% 才肯改布局：微收益不值形态变化
_ROW_PITCH_DESIGN = 170      # 每多一行加的高度（设计 px）：上卡片 70 + 下卡片 70 + 30 呼吸
_DATE_FONT_DESIGN = 12       # 常驻最小字号（日期档）= 折行决策的**标尺字号**


def _timeline_auto_wrap_enabled() -> bool:
    """开关：缺开；只有显式 falsy 才回退单行（与 ARCH_AUTO_SPLIT 同一套语义）。"""
    return os.environ.get(_ENV_AUTO_WRAP, "").strip().lower() not in _FALSY


def _timeline_max_rows() -> int:
    """行数上限读数：空 / 非法 / ≤0 ⇒ 默认 12。绝不抛。"""
    try:
        v = int(os.environ.get(_ENV_MAX_ROWS, "") or 0)
    except ValueError:
        return _MAX_ROWS_DEFAULT
    return v if v > 0 else _MAX_ROWS_DEFAULT


def _timeline_canvas_design(cards_in_row, rows):
    """画布外接尺寸（**设计 px**，未乘 scale）。纯函数。

    `rows=1` 时与改造前的 `total_w / total_h` 表达式逐项等价：
      宽 = max(960, 左右 padding 80 + cards×230 + (cards-1)×24)
      高 = title 60 + padding×2 80 + node 80 + label 60 + desc 50 = 330
    """
    cards = max(1, int(cards_in_row))
    rows = max(1, int(rows))
    w = max(960, 80 + cards * 230 + (cards - 1) * 24)
    h = 330 + (rows - 1) * _ROW_PITCH_DESIGN
    return w, h


def plan_timeline_rows(n, *, max_rows=None, gain=_WRAP_GAIN, enabled=None):
    """折行决策：返回 `(行数, 每行卡片上限)`。纯函数、**绝不抛**、无副作用。

    目标函数 = **日期档字号**（常驻最小字号 `_DATE_FONT_DESIGN`）的纸面毫米，在
    `rows ∈ [1, min(max_rows, n)]` 上取最大；只有当最优值比单行**至少高 gain 倍**
    才真的折，否则返回 `(1, n)` —— 即改造前的形态，逐字回退。
    """
    try:
        total = int(n)
    except (TypeError, ValueError):
        return 1, 1
    if total <= 1:
        return 1, max(total, 1)
    if enabled is None:
        enabled = _timeline_auto_wrap_enabled()
    if not enabled:
        return 1, total
    if max_rows is None:
        max_rows = _timeline_max_rows()
    try:
        top = min(int(max_rows), total)
    except (TypeError, ValueError):
        top = total
    if top < 2:
        return 1, total

    def _mm(cards, rows):
        w, h = _timeline_canvas_design(cards, rows)
        return paper_font_mm(_DATE_FONT_DESIGN, w, h)

    single = _mm(total, 1)
    best_rows, best_cards, best_mm = 1, total, single
    for rows in range(2, top + 1):
        cards = -(-total // rows)          # ceil：每行卡片上限（末行可不满）
        if cards < 1:
            continue
        m = _mm(cards, rows)
        if m > best_mm + 1e-9:             # 严格更好才换 ⇒ 平局保留更少行（形态更保守）
            best_rows, best_cards, best_mm = rows, cards, m
    if best_rows > 1 and best_mm >= single * max(1.0, float(gain)):
        return best_rows, best_cards
    return 1, total


def _single_row_reason(n) -> str:
    """保持单行**到底为什么**：只供告警文案说实话，绝不参与任何布局决策。纯函数、绝不抛。

    单行有三种完全不同的成因，混成一句「折行不值」就是说谎：
      · 开关被关（`TIMELINE_AUTO_WRAP=0`）⇒ 折行根本没参与；
      · 行数上限被钉死（`TIMELINE_MAX_ROWS_PER_FIGURE < 2`）⇒ 同上；
      · 真的算过、收益不够（n=4 折 2 行只赢 12%，或折行根本不涨毫米）。
    """
    try:
        total = int(n)
    except (TypeError, ValueError):
        total = 0
    if total <= 1:
        return "仅 %d 个事件，无从折行" % total
    if not _timeline_auto_wrap_enabled():
        return "折行开关已关闭 %s=0" % _ENV_AUTO_WRAP
    if _timeline_max_rows() < 2:
        return "行数上限 %s 小于 2，折行未启用" % _ENV_MAX_ROWS
    return "%d 事件保持单行：折行未带来 ≥%.0f%% 的纸面增益" % (total, (_WRAP_GAIN - 1) * 100)


def _parse_mermaid_timeline(code: str) -> dict | None:
    """解析 Mermaid 原生 timeline 语法为 v2 渲染数据。

    支持：
      timeline
          title 关键里程碑
          第30天 : 基坑开挖完成
          2026-05-01 : 主体封顶 : （可选描述）
          阶段一
              2026-03-01 : 进场
              2026-03-10 : 验收
    """
    if not code:
        return None
    lines = [ln.strip() for ln in code.splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    if not lines or lines[0].lower() != "timeline":
        return None
    title = "关键里程碑时间线"
    milestones: list[dict] = []
    current_group = ""
    for raw in lines[1:]:
        if raw.lower().startswith("title"):
            t = raw[5:].strip()
            if t:
                title = t
            continue
        parts = [p.strip() for p in raw.split(":") if p.strip()]
        if not parts:
            continue
        if len(parts) == 1:
            # 无冒号的行视为阶段分组标题
            current_group = parts[0]
            continue
        date, name = parts[0], parts[1]
        desc = parts[2] if len(parts) > 2 else ""
        milestones.append({"name": name, "date": date, "desc": desc, "group": current_group})
    if not milestones:
        return None
    return {"title": title, "milestones": milestones}


@_render_hq
def _render_timeline_image_v2(data: dict, scale: float = 2.5) -> BytesIO | None:
    """商业级时间轴/里程碑图渲染器 v2

    适用于：项目关键里程碑、阶段时间节点、重大事件时间线。
    元素：
    - 横向时间轴
    - 里程碑标记（菱形）+ 名称 + 日期
    - 阶段分组（可选）

    Args:
        scale: 超采样缩放比例（默认 2.5，提供 2.5x 抗锯齿）
    """
    if not isinstance(data, dict):
        return None
    # ✅ 统一容错规则（2026-09-16）：与 chart_validators.normalize_timeline_data 共用同一套归一逻辑。
    #    （校验侧与渲染侧容错度必须一致，否则会出现"校验通过、渲染器返回 None → 导出红字占位"。）
    #    · 容器别名 milestones / events / points / **items**；
    #    · name 回落 label/title → 「里程碑N」；date 回落空串；
    #    · status 别名（done/进行中/未开始…）→ completed/in_progress/pending，无法识别的丢弃。
    #    历史缺陷：本渲染器只认 milestones/events/points，而校验器还认 items
    #    → `{"type":"timeline","items":[...]}` 校验放行、渲染返回 None，正文图表变红字占位。
    from app.services.chart_validators import normalize_timeline_data

    normalized = normalize_timeline_data(data)
    if normalized is None:
        return None
    # 归一结果只含 milestones；title/type 等字段从原载荷保留
    data = {**data, **normalized}
    milestones = data["milestones"]
    if not isinstance(milestones, list) or not milestones:
        return None

    # 规范化
    norm: list[dict] = []
    for i, m in enumerate(milestones):
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("title") or m.get("label") or f"里程碑{i+1}").strip()
        if not name:
            continue
        date = str(m.get("date") or m.get("time") or m.get("day") or "").strip()
        desc = str(m.get("description") or m.get("desc") or "").strip()
        group = str(m.get("group") or m.get("phase") or "").strip()
        norm.append({"name": name, "date": date, "desc": desc, "group": group})

    if not norm:
        return None
    n = len(norm)

    logger.info(
        "[PIL v2渲染] 时间轴渲染启动: 里程碑=%d 分组数=%d scale=%.1f",
        n,
        len(set(m["group"] for m in norm if m["group"])),
        scale,
    )

    # 尺寸（乘以 scale 超采样）
    padding = int(40 * scale)
    title_h = int(60 * scale)
    node_h = int(80 * scale)

    arrow_w = int(16 * scale)
    label_h = int(60 * scale)
    desc_max = int(50 * scale)

    # 横向布局：宽度按**每行**卡片数自适应（R64：行数由纸面毫米算出来）
    min_node_w = int(230 * scale)
    card_gap = int(24 * scale)
    rows, cards_per_row = plan_timeline_rows(n)
    # 每行实际卡片数（末行可不满）；max_cards 决定画布宽，占旧表达式里 `n` 的位置
    per_row = [min(cards_per_row, n - r * cards_per_row) for r in range(rows)]
    max_cards = max(per_row)
    total_w = max(int(960 * scale),
                  padding * 2 + max_cards * min_node_w + (max_cards - 1) * card_gap)
    # rows=1 时末项为 0 ⇒ 与改造前的 total_h 逐项相同（含 int() 截断口径）
    total_h = (title_h + padding * 2 + node_h + label_h + desc_max
               + (rows - 1) * int(_ROW_PITCH_DESIGN * scale))

    img = Image.new("RGB", (int(total_w), int(total_h)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_name = _image_font(int(15 * scale), bold=True)
    font_date = _image_font(int(12 * scale))
    font_desc = _image_font(int(11 * scale))
    font_axis = _image_font(int(12 * scale))

    title = str(data.get("title") or "项目关键里程碑时间线")
    draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # 计算每个节点的水平位置（R64：按**行**分桶，每行一条自己的时间轴）
    inner_left = padding + int(30 * scale)
    inner_right = total_w - padding - int(30 * scale)
    steps = [((inner_right - inner_left) / (c - 1)) if c > 1 else 0.0 for c in per_row]

    axis_y0 = title_h + padding + node_h / 2
    row_pitch = int(_ROW_PITCH_DESIGN * scale)

    ax_line_width = int(4 * scale)
    for _r in range(rows):
        axis_y = axis_y0 + _r * row_pitch
        # 绘制时间轴主线
        draw.line([(inner_left, axis_y), (inner_right, axis_y)], fill="#64748B", width=ax_line_width)
        # 末端箭头（每行都指向右：折行不改变「时间从左到右」的方向语义）
        draw.polygon(
            [
                (inner_right, axis_y),
                (inner_right - arrow_w, axis_y - int(10 * scale)),
                (inner_right - arrow_w, axis_y + int(10 * scale)),
            ],
            fill="#64748B",
        )
        # 时间轴阴影
        draw.line(
            [(inner_left, axis_y + int(2 * scale)), (inner_right, axis_y + int(2 * scale))],
            fill="#E5E7EB",
            width=ax_line_width,
        )

    # 阶段分组配色
    group_colors = [
        ("#2B579A", "#EAF2F8"),
        ("#16A34A", "#EAFBEA"),
        ("#D97706", "#FFF7E6"),
        ("#7C3AED", "#F3E8FF"),
        ("#DC2626", "#FEE2E2"),
    ]
    all_groups: list[str] = []
    for m in norm:
        if m["group"] and m["group"] not in all_groups:
            all_groups.append(m["group"])
    logger.info(
        "[PIL v2渲染] 时间轴绘制: 节点=%d 分组=%s 画布=%dx%dpx 时间步长=%.1f 行数=%d 每行≤%d",
        n,
        all_groups,
        total_w,
        total_h,
        steps[0] if n > 1 else 0,
        rows,
        cards_per_row,
    )

    # 绘制里程碑
    for i, m in enumerate(norm):
        _r = i // cards_per_row
        _j = i - _r * cards_per_row
        _cards_here = per_row[_r]
        axis_y = axis_y0 + _r * row_pitch
        cx = inner_left if _cards_here == 1 else inner_left + _j * steps[_r]

        # 节点位置（上下交错显示，**按行内**序号交错 ⇒ 单行时与改造前逐字一致）
        is_up = _j % 2 == 0
        box_y = axis_y - int(70 * scale) if is_up else axis_y + int(14 * scale)

        # 颜色
        group_idx = (
            all_groups.index(m["group"]) if m["group"] in all_groups else i % len(group_colors)
        )
        border_c, fill_c = group_colors[group_idx % len(group_colors)]

        # 连接线（从 box 到 axis）
        if is_up:
            line_y_top = box_y + int(56 * scale)
            line_y_bottom = axis_y - int(6 * scale)
        else:
            line_y_top = axis_y + int(6 * scale)
            line_y_bottom = box_y
        draw.line(
            [(cx, line_y_top), (cx, line_y_bottom)],
            fill=border_c,
            width=int(2 * scale),
            joint="curve",
        )

        # 圆角矩形（里程碑卡片）带阴影
        box_w = min_node_w - int(10 * scale)
        box_x = cx - box_w / 2
        # ✅ BUG 修复：首尾里程碑卡片因以 cx 为中心计算，一半宽度会伸出画布外
        #    （首个 box_x 为负，末个 right 超出 total_w），导致卡片/文字被裁剪。
        #    这里将 box_x 限制在 [padding, total_w - padding - box_w] 范围内。
        box_x = max(padding, min(box_x, total_w - padding - box_w))
        shadow_offset = int(3 * scale)
        card_h = int(56 * scale)
        radius = int(6 * scale)
        draw.rounded_rectangle(
            [
                box_x + shadow_offset,
                box_y + shadow_offset,
                box_x + box_w + shadow_offset,
                box_y + card_h + shadow_offset,
            ],
            radius=radius,
            fill="#D1D5DB",
            outline=None,
        )
        draw.rounded_rectangle(
            [box_x, box_y, box_x + box_w, box_y + card_h],
            radius=radius,
            fill=fill_c,
            outline=border_c,
            width=int(2 * scale),
        )
        # 名称
        pad = int(6 * scale)
        _draw_text_center(
            draw,
            (box_x + pad, box_y + int(4 * scale), box_x + box_w - pad, box_y + int(22 * scale)),
            m["name"],
            font_name,
            border_c,
            max_lines=1,
        )
        # 日期
        if m["date"]:
            _draw_text_center(
                draw,
                (
                    box_x + pad,
                    box_y + int(22 * scale),
                    box_x + box_w - pad,
                    box_y + int(38 * scale),
                ),
                m["date"],
                font_date,
                "#1E3A5F",
                max_lines=1,
            )
        # 描述
        if m["desc"]:
            _draw_text_center(
                draw,
                (
                    box_x + pad,
                    box_y + int(38 * scale),
                    box_x + box_w - pad,
                    box_y + int(54 * scale),
                ),
                m["desc"],
                font_desc,
                "#374151",
                max_lines=1,
            )

        # 菱形里程碑节点（位于轴上，带阴影）
        d = int(12 * scale)
        # 阴影
        draw.polygon(
            [
                cx + shadow_offset,
                axis_y - d + shadow_offset,
                cx + d + shadow_offset,
                axis_y + shadow_offset,
                cx + shadow_offset,
                axis_y + d + shadow_offset,
                cx - d + shadow_offset,
                axis_y + shadow_offset,
            ],
            fill="#D1D5DB",
            outline=None,
        )
        # 主体
        draw.polygon(
            [
                (cx, axis_y - d),
                (cx + d, axis_y),
                (cx, axis_y + d),
                (cx - d, axis_y),
            ],
            fill="#FBBF24",
            outline="#92400E",
            width=int(3 * scale),
        )
        # 节点编号
        bbox = draw.textbbox((0, 0), str(i + 1), font=font_axis)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw / 2, axis_y - th / 2), str(i + 1), fill="#7C2D12", font=font_axis)

    # ---- 图例（阶段分组）----
    if all_groups:
        legend_y = total_h - int(30 * scale)
        legend_x = padding
        draw.text((legend_x, legend_y - int(4 * scale)), "阶段：", fill="#1E3A5F", font=font_axis)
        legend_x += int(50 * scale)
        for g in all_groups:
            gi = all_groups.index(g) % len(group_colors)
            border_c, fill_c = group_colors[gi]
            draw.rectangle(
                [legend_x, legend_y, legend_x + int(14 * scale), legend_y + int(12 * scale)],
                fill=fill_c,
                outline=border_c,
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(18 * scale), legend_y - int(2 * scale)),
                g,
                fill="#1E3A5F",
                font=font_axis,
            )
            bbox = draw.textbbox((0, 0), g, font=font_axis)
            legend_x += bbox[2] - bbox[0] + int(32 * scale)
            if legend_x + int(100 * scale) > total_w:
                break

    # ✅ R64：把「印到纸上多大字」变成日志里的事实，而不是等人拿尺子量屏幕。
    #    字号直接取**真正用于绘制的字体对象**（抄常量表的话，日后渲染器改字号，
    #    播报会静默失真 —— 与 R57「源码级契约静默失效」同一类坑）。
    #    折行已生效时把行数写进建议，避免「已经折到 6 行还叫人拆图」这种自相矛盾。
    paper_floor_note(
        "时间轴",
        [font_title, font_name, font_date, font_desc, font_axis],
        img.size[0], img.size[1],
        primary=[font_name],
        # ⚠️ 单行分支的文案是**红线**：① 不许写「单行已是最优形态」（同一句话先说
        #    字太小、再说没办法）；② 也不许把三种成因混成一句「折了不值」——开关被关
        #    时说成收益不够，运维会去调 `_WRAP_GAIN`，而调它根本不起作用（本轮取证表
        #    就是这么撞出来的）。⇒ 一律交 `_single_row_reason()` 分叉说实话。
        suggest=("（%d 事件已折为 %d 行仍不达标，建议减少条目或拆成多张图）" % (n, rows))
        if rows > 1 else "（%s）" % _single_row_reason(n),
    )
    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 时间轴渲染完成: 里程碑=%d 分组=%d 尺寸=%dKB DPI=300",
        len(norm),
        len(all_groups),
        size_kb,
    )
    return result_stream

def _repair_timeline_data(data: dict) -> dict | None:
    """修复时间轴数据：补齐缺失字段，确保渲染器能处理

    Args:
        data: 原始时间轴数据

    Returns:
        修复后的数据字典，或 None 如果无法修复
    """
    if not isinstance(data, dict):
        return None
    repaired = copy.deepcopy(data)

    # 保证 milestones 存在
    if not repaired.get("milestones"):
        repaired["milestones"] = [
            {
                "name": "项目开工",
                "date": "第1天",
                "description": "项目正式开工",
                "status": "completed",
            },
            {
                "name": "基础完成",
                "date": "第30天",
                "description": "基础工程验收",
                "status": "completed",
            },
            {
                "name": "主体封顶",
                "date": "第75天",
                "description": "主体结构封顶",
                "status": "in_progress",
            },
            {
                "name": "竣工验收",
                "date": "第120天",
                "description": "项目整体竣工验收",
                "status": "pending",
            },
        ]

    # 补齐每个 milestone 的缺失字段
    for i, m in enumerate(repaired.get("milestones", [])):
        if not isinstance(m, dict):
            continue
        if not m.get("name"):
            m["name"] = m.get("title", f"里程碑{i+1}")
        if not m.get("date"):
            m["date"] = f"第{(i+1)*30}天"
        if not m.get("status"):
            m["status"] = "pending"
        if not m.get("description"):
            m["description"] = ""

    if not repaired.get("title"):
        repaired["title"] = "项目关键里程碑时间线"

    return repaired

