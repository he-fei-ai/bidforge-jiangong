"""Gantt 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _build_gantt_marks,
    _draw_text_center,
    _image_font,
    _image_to_stream,
    _render_hq,
)

logger = logging.getLogger(__name__)



# Mermaid 甘特图行内的日期 token（2026-01-01 / 2026/1/1）与工期 token（10d / 2w / 1m / 1y）
_DATE_TOKEN_RE = re.compile(r"^\d{2,4}[-/]\d{1,2}[-/]\d{1,2}$")
_DURATION_TOKEN_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)


def _parse_mermaid_gantt(mermaid_code: str) -> list[dict] | None:
    """解析 Mermaid 甘特图代码，提取任务及真实起止日期（相对天数偏移）。

    相比旧实现（按任务顺序伪造 5 天间隔），这里解析 ``dateFormat`` 以及
    每行任务中的真实开始日期与工期，计算相对最早日期的天数偏移，使导出的
    横道图与实际进度计划保持一致。
    """
    date_format = "%Y-%m-%d"
    for line in mermaid_code.split("\n"):
        m = re.search(r"dateFormat\s+(\S+)", line)
        if m:
            fmt = m.group(1)
            if "YYYY" in fmt or "%Y" in fmt:
                date_format = "%Y-%m-%d" if "-" in fmt else "%Y/%m/%d"
            elif "YY" in fmt:
                date_format = "%y-%m-%d"
            break

    # ✅ BUG 修复：解析任务 id 与 `after <id>` 依赖，使横道图真实反映任务间的
    #    先后顺序。原实现只按行顺序用 `cursor` 顺序顺延，凡 `after X` 任务都被
    #    当成"无开始日期"放在 cursor 之后 —— 于是只要依赖任务之间有**独立给定日期**
    #    的任务，或依赖任务以**前向引用**出现在后面，依赖关系就被彻底忽略（例如
    #    `a2 after a1` 排在 `aX 2026-03-01` 之后，a2 会被推到 aX 之后而非 a1 之后）。
    #    现解析出任务 id 与依赖，再按 id 解析真实起止偏移。
    _STATUS_TOKENS = ("done", "active", "crit", "milestone")

    raw_tasks: list[dict] = []
    current_section = ""
    for line in mermaid_code.strip().split("\n"):
        line = line.strip()
        if line.startswith("section "):
            current_section = line[len("section ") :].strip()
            continue
        if ":" not in line or any(line.startswith(k) for k in ("date", "title", "axis")):
            continue
        name, _, spec = line.partition(":")
        name = name.strip()
        spec = spec.strip()
        if not name:
            continue
        tokens = [t.strip() for t in spec.split(",")]
        task_id = None
        is_milestone = False
        deps: list[str] = []
        date_tokens: list[str] = []
        numeric_tokens: list[int] = []
        duration_days = None
        for tok in tokens:
            low = tok.lower()
            if low in _STATUS_TOKENS:
                # milestone 是 0 工期标记，其余为状态标签（done/active/crit）
                if low == "milestone":
                    is_milestone = True
                continue
            if low.startswith("after "):
                dep = tok[5:].strip()
                if dep:
                    deps.append(dep)
                continue
            if _DATE_TOKEN_RE.match(tok):
                date_tokens.append(tok)
                continue
            m_dur = _DURATION_TOKEN_RE.match(tok)
            if m_dur:
                unit_mul = {"d": 1, "w": 7, "m": 30, "y": 365}
                duration_days = int(m_dur.group(1)) * unit_mul[m_dur.group(2).lower()]
                continue
            # ✅ 纯数字 token（dateFormat X 数字轴下的「起始天 / 天数」）先收集，
            #    不在此处定值——需在确定"无日期 token"后再按位置解释。
            if re.fullmatch(r"\d+", tok):
                numeric_tokens.append(int(tok))
                continue
            # 其余合法标识符（状态/日期/工期/after 之外）视为任务 id
            # （mermaid 允许 `:id` 或 `:milestone, id` 形式，id 可出现在任意位置）
            if task_id is None and re.fullmatch(r"[A-Za-z_]\w*", tok):
                task_id = tok
        start_str = date_tokens[0] if date_tokens else None
        end_str = date_tokens[1] if len(date_tokens) >= 2 else None
        # ✅ BUG 修复（甘特图失真根因）：正文提示词要求 `dateFormat X` 数字轴，任务写法为
        #    `任务名 :id, 起始天, 天数`（二者均为纯数字）。旧实现只识别日期 token 与带单位
        #    工期 token（10d/2w），纯数字被整体丢弃 → start_str/duration 恒为空 →
        #    所有任务退化为「cursor+1 起、每任务 5 天」的顺序排列，导出的横道图与 AI
        #    给出的真实进度计划完全不符（进度计划是专项施工方案第三章「施工计划」的
        #    核心内容，失真等于方案进度承诺与现场执行脱节）。
        #    现将纯数字按 [起始天, 天数] 位置解析；仅在「无日期 token」时生效，
        #    避免 dateFormat YYYY-MM-DD 场景下把杂散数字误判为进度。
        start_day = None
        if not date_tokens and numeric_tokens:
            start_day = numeric_tokens[0]
            if duration_days is None and len(numeric_tokens) >= 2:
                duration_days = numeric_tokens[1]
        raw_tasks.append(
            {
                "section": current_section,
                "task": name,
                "spec": spec,
                "task_id": task_id or name,
                "deps": deps,
                "is_milestone": is_milestone,
                "start_str": start_str,
                "end_str": end_str,
                "start_day": start_day,
                "duration": duration_days,
            }
        )

    if not raw_tasks:
        return None

    def _to_date(text: str | None):
        if not text:
            return None
        for fmt in (date_format, "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    parsed = [
        {**rt, "start_date": _to_date(rt["start_str"]), "end_date": _to_date(rt.get("end_str"))}
        for rt in raw_tasks
    ]

    dated = [p["start_date"] for p in parsed if p["start_date"]]
    min_date = min(dated) if dated else None

    # id → 已解析的结束偏移，供 after 依赖查找（前向引用在解析到时回填）
    _id_end: dict[str, int] = {}

    cursor = 0
    result = []
    for p in parsed:
        if p["start_date"] and min_date is not None:
            start_off = (p["start_date"] - min_date).days + 1
        elif p.get("start_day") is not None:
            # ✅ dateFormat X：显式起始天（1 基），直接采用（不再顺序顺延）
            start_off = max(1, int(p["start_day"]))
        elif p["deps"]:
            # ✅ 依赖解析：紧随其全部依赖中结束最晚者之后开始
            _dep_ends = [
                _id_end.get(d) for d in p["deps"]
                if _id_end.get(d) is not None
            ]
            start_off = (max(_dep_ends) + 1) if _dep_ends else (cursor + 1)
        else:
            # 未给出日期且无条件依赖 → 顺序顺延，避免重叠
            start_off = cursor + 1
        # ✅ BUG 修复：`0d` 是 Mermaid 表达**里程碑**的标准写法，而旧实现用
        #    `if p["duration"] else 5` 判定 —— 0 是 falsy，被当成"未给出工期"，
        #    里程碑于是被画成 5 天的任务条，菱形标记与里程碑语义整体丢失。
        raw_dur = p["duration"]
        if p["start_date"] is not None and p["end_date"] is not None:
            # 写法 ②：由起止日期反推工期（含首尾，故 +1）
            dur = max(1, (p["end_date"] - p["start_date"]).days + 1)
        elif raw_dur is None:
            dur = 5      # 完全未给出工期信息 → 顺序顺延 5 天
        elif raw_dur <= 0:
            dur = 1      # 0d / 0 → 当天单点（里程碑），end == start
        else:
            dur = raw_dur
        end_off = start_off + dur - 1
        cursor = max(cursor, end_off)
        _id_end[str(p["task_id"])] = end_off
        result.append(
            {
                "section": p["section"],
                "task": p["task"],
                "time": p["spec"],
                "start": start_off,
                "end": end_off,
            }
        )
    return result if result else None

@_render_hq
def _render_gantt_image_v2(plan: dict, highlight_critical_path: bool = False) -> BytesIO | None:
    """商业级甘特图渲染器 v2

    适用：
    - Mermaid gantt 解析后的任务列表
    - 直接传入的 GanttPlan JSON

    特性：
    - 自动归一化时间轴
    - 真实依赖关系连线
    - 里程碑节点特殊标识（橙色菱形）
    - 多色任务条（按阶段分组）
    - 关键路径高亮（highlight_critical_path=True 时启用红色填充+红色边框）
    - 工日柱状 + 文本双信息
    - 优化：批量渐变绘制，避免逐行循环

    Args:
        highlight_critical_path: 是否启用关键路径高亮（红色填充+红色依赖线）
    """
    if not isinstance(plan, dict):
        return None
    # ✅ 统一容错规则：与 chart_validators.normalize_gantt_plan 共用同一套归一逻辑。
    #    （校验侧与渲染侧容错度必须一致，否则会出现"渲染器能画、校验器判非法→正文删块"。）
    #    · items/rows 容器别名；
    #    · 任务 id 缺失时回落到 name/序号；
    #    · dependencies 支持用**任务名**引用（AI 常这么写）→ 归一为 id。
    #      渲染器按 str(task["id"]) 连线，name 形式的引用此前**连不出箭头**。
    from app.services.chart_validators import normalize_gantt_plan

    normalized = normalize_gantt_plan(plan)
    if normalized is None:
        return None
    # 归一结果只含 tasks；title/totalDays/duration 等字段从原载荷保留
    plan = {**plan, **normalized}

    # 提取任务
    tasks = plan.get("tasks", [])
    logger.info(
        "[PIL v2渲染] 甘特图渲染启动: 任务数=%d 总工期=%d 依赖数=%d highlight_critical_path=%s",
        len(tasks),
        plan.get("duration", 0),
        len([t for t in tasks if t.get("dependencies")]),
        highlight_critical_path,
    )
    raw_tasks = plan.get("tasks") or []
    if not raw_tasks and "items" in plan:
        raw_tasks = plan["items"]

    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None

    # 规范化
    tasks: list[dict] = []

    # ✅ 修复（BUG-2）：任务 start/end 可能是日期串（如 "2026-01-01"，提示词示例即如此），
    #    或只有 days 工期。旧的 int(...) 对日期串抛 ValueError 被吞 → 所有任务塌缩到第 1 天
    #    单天条、进度计划彻底失真；且 "days" 字段从未被读取。现统一把值转成"相对最早日期
    #    的天数偏移"（base 当天=1），整数值保持原绝对天偏移语义，缺 end 时用 days 推导。
    from datetime import datetime as _dt

    def _gantt_to_day(v, base):
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            if s.isdigit():
                return int(s)
            try:
                d = _dt.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return None
            return (d - base).days + 1 if base is not None else 1
        return None

    # 以所有任务的 start 中最早日期串为基准
    _base_date = None
    for _t in raw_tasks[:30]:
        if not isinstance(_t, dict):
            continue
        _sv = _t.get("start") or _t.get("start_day") or _t.get("from")
        if isinstance(_sv, str):
            try:
                _d = _dt.strptime(_sv.strip()[:10], "%Y-%m-%d")
                if _base_date is None or _d < _base_date:
                    _base_date = _d
            except ValueError:
                pass

    for idx, t in enumerate(raw_tasks[:30]):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("task") or t.get("title") or "").strip()
        if not name:
            continue
        start_v = _gantt_to_day(t.get("start") or t.get("start_day") or t.get("from"), _base_date) or 1
        _end_raw = t.get("end") or t.get("end_day") or t.get("to")
        _days = t.get("days")
        _end_v = _gantt_to_day(_end_raw, _base_date)
        if _end_v is None:
            if _days is not None:
                try:
                    _end_v = start_v + int(_days) - 1
                except (TypeError, ValueError):
                    _end_v = start_v
            else:
                _end_v = start_v
        end_v = max(start_v, _end_v)
        is_milestone = bool(t.get("isMilestone")) or (start_v == end_v)
        deps = t.get("dependencies") or []
        if not isinstance(deps, list):
            deps = []
        # 关键路径标记
        is_critical = bool(t.get("critical"))
        tasks.append(
            {
                "id": t.get("id") or (idx + 1),
                "name": name,
                "start": start_v,
                "end": max(start_v, end_v),
                "isMilestone": is_milestone,
                "dependencies": deps,
                "critical": is_critical,
                # BUG 修复：规范化时遗漏 remark 字段，导致下方备注区
                # `remarks = [t.get("remark") ...]` 永远为空、备注区高度恒为 0 ——
                # 甘特图备注（如「春节停工」「雨季顺延」）从未被渲染过。此处补回。
                "remark": str(t.get("remark") or t.get("note") or "").strip(),
                # ✅ 增强：保留阶段划分（阶段划分是进度计划的关键内容，直接体现
                # 方案对工期节点的组织能力）。
                #    旧实现丢弃 section，导出的横道图看不出"准备/主体/收尾"阶段。
                "section": str(t.get("section") or t.get("phase") or "").strip(),
            }
        )

    if not tasks:
        return None

    # 总工期
    total_days = plan.get("totalDays") or plan.get("duration") or 0
    try:
        total_days = int(total_days)
    except (TypeError, ValueError):
        total_days = 0
    max_end = max((t["end"] for t in tasks), default=0)
    if total_days < max_end:
        total_days = max_end
    if total_days <= 0:
        total_days = max_end or 90


    # ---- 尺寸 ----
    scale = 2.5
    table_left = int(50 * scale)
    table_top = int(70 * scale)
    row_h = int(48 * scale)
    header_h = int(80 * scale)
    # ✅ BUG 修复：最后一列「工期(天)」表头在 font_header 55px 下宽度不足，
    #    被 _draw_text_center 截断为「工期(...」。将列宽从 80 提升到 120，
    #    确保 5 个汉字的表头完整显示。
    left_widths = [int(w * scale) for w in [60, 360, 80, 80, 120]]
    axis_w = int(1100 * scale)
    table_w = sum(left_widths) + axis_w

    # ---- 行序列（阶段带 + 任务行）----
    # ✅ 增强：Mermaid gantt 的 `section xxx` 此前被解析出来后从未渲染
    #    （渲染器只遍历 tasks）。这里在每个阶段的首个任务前插入一条"阶段带"行，
    #    使导出的横道图体现"准备/主体/收尾"等阶段划分。
    row_items: list[tuple[str, object]] = []
    _prev_section = ""
    for _ti, _task in enumerate(tasks):
        _sec = str(_task.get("section") or "").strip()
        if _sec and _sec != _prev_section:
            row_items.append(("section", _sec))
            _prev_section = _sec
        row_items.append(("task", _ti))

    # 备注区域（两种模式均渲染）
    remarks = [t.get("remark") for t in tasks if t.get("remark")][:4]
    remarks_h = int(min(80, 30 + len(remarks) * 18) * scale) if remarks else 0

    # 图例区域
    legend_h = int(50 * scale) if highlight_critical_path else int(40 * scale)

    width = table_left * 2 + table_w
    height = (table_top + header_h + len(row_items) * row_h
              + remarks_h + legend_h + int(30 * scale))

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    # 注意：width/height 已按 scale 预缩放（上方各尺寸分量均已 * scale），此处不得二次缩放，
    # 否则画布比内容大 2.5 倍（右侧/下方大片空白）且内存膨胀 6.25 倍（曾触发 PIL DecompressionBombWarning）
    sw, sh = int(width), int(height)
    img = Image.new("RGB", (sw, sh), "white")
    draw = ImageDraw.Draw(img)
    font_header = _image_font(int(22 * scale), bold=True)
    font_title = _image_font(int(26 * scale), bold=True)
    font_body = _image_font(int(20 * scale))
    font_small = _image_font(int(17 * scale))
    font_axis = _image_font(int(18 * scale))

    # ---- 配色 ----
    border = "#6B7280"
    grid = "#D1D5DB"
    header_fill = "#EAF2F8"
    alt_fill = "#F8FAFC"
    text = "#111827"
    palette = [
        "#2B579A",
        "#E8A33D",
        "#70AD47",
        "#C00000",
        "#7030A0",
        "#4472C4",
        "#9966CC",
        "#5B9BD5",
        "#DC2626",
        "#16A34A",
    ]
    # 关键路径配色
    critical_outline = "#DC2626"

    # ---- 标题 ----
    title = str(plan.get("title") or "施工进度计划")
    draw.text((table_left, int(22 * scale)), title, fill="#1E3A5F", font=font_title)
    # 总工期标识
    total_label = f"总工期：{total_days}天"
    bbox = draw.textbbox((0, 0), total_label, font=font_small)
    tw = bbox[2] - bbox[0]
    draw.text(
        (width - table_left - tw, int(30 * scale)), total_label, fill="#1E3A5F", font=font_small
    )

    # ---- 表头 ----
    x = table_left
    left_headers = ["#", "工作内容", "开始", "完成", "工期(天)"]
    for col, col_w in zip(left_headers, left_widths, strict=False):
        draw.rectangle(
            [x, table_top, x + col_w, table_top + header_h],
            fill=header_fill,
            outline=border,
            width=int(2 * scale),
        )
        _draw_text_center(
            draw,
            (x + int(4 * scale), table_top, x + col_w - int(4 * scale), table_top + header_h),
            col,
            font_header,
            text,
            max_lines=1,
        )
        x += col_w

    # ---- 时间轴头部 ----
    axis_left = table_left + sum(left_widths)
    draw.rectangle(
        [axis_left, table_top, axis_left + axis_w, table_top + header_h],
        fill=header_fill,
        outline=border,
        width=int(2 * scale),
    )
    _draw_text_center(
        draw,
        (axis_left, table_top + int(4 * scale), axis_left + axis_w, table_top + int(30 * scale)),
        f"施工进度时间轴（{total_days}天）",
        font_header,
        text,
        max_lines=1,
    )

    # ---- 时间刻度 ----
    marks = _build_gantt_marks(total_days, max(1, total_days // 12), int(18 * scale))
    axis_label_top = table_top + int(36 * scale)
    for mark in marks:
        px = axis_left + int((mark / total_days) * axis_w)
        draw.line([px, axis_label_top, px, table_top + header_h], fill=border, width=int(1 * scale))
        bbox = draw.textbbox((0, 0), str(mark), font=font_axis)
        tw_m = bbox[2] - bbox[0]
        draw.text(
            (px - tw_m / 2, axis_label_top + int(8 * scale)), str(mark), fill=text, font=font_axis
        )

    # ---- 行绘制（阶段带 + 任务行）----
    y = table_top + header_h
    task_pos: dict[str, dict] = {}
    task_bar_regions: list[dict] = []  # 收集渐变区域用于批量绘制
    row_index = 0

    for kind, payload in row_items:
        if kind == "section":
            # 阶段带：整行浅蓝底 + 左侧色条 + 阶段名，不参与任务编号/网格/进度条
            draw.rectangle(
                [table_left, y, table_left + table_w, y + row_h],
                fill="#DCE6F1",
                outline=border,
                width=int(1 * scale),
            )
            draw.rectangle(
                [table_left, y, table_left + int(6 * scale), y + row_h],
                fill="#2B579A",
            )
            draw.text(
                (table_left + int(16 * scale), y + int(13 * scale)),
                f"◆ {payload}",
                fill="#1E3A5F",
                font=font_body,
            )
            y += row_h
            continue

        task = tasks[payload] if isinstance(payload, int) else payload
        row_index += 1
        row_fill = alt_fill if row_index % 2 == 0 else "white"
        draw.rectangle(
            [table_left, y, table_left + table_w, y + row_h],
            fill=row_fill,
            outline=grid,
            width=int(1 * scale),
        )

        # 单元格
        x = table_left
        cells = [
            str(row_index),
            task["name"],
            str(task["start"]),
            str(task["end"]),
            f"{task['end'] - task['start'] + 1}天",
        ]
        for value, col_w in zip(cells, left_widths, strict=False):
            draw.rectangle([x, y, x + col_w, y + row_h], outline=grid, width=int(1 * scale))
            _draw_text_center(
                draw,
                (
                    x + int(6 * scale),
                    y + int(4 * scale),
                    x + col_w - int(6 * scale),
                    y + row_h - int(4 * scale),
                ),
                value,
                font_body,
                text,
                max_lines=2,
            )
            x += col_w

        # 网格线
        for mark in marks:
            px = axis_left + int((mark / total_days) * axis_w)
            draw.line([px, y, px, y + row_h], fill=grid, width=int(1 * scale))

        # 任务条
        start_x = axis_left + int(((task["start"] - 1) / total_days) * axis_w)
        end_x = axis_left + int((task["end"] / total_days) * axis_w)

        if task["isMilestone"]:
            # 里程碑：橙色菱形（带阴影）
            d = int(12 * scale)
            cx = (start_x + end_x) / 2
            cy = y + row_h / 2
            shadow_offset = int(3 * scale)
            draw.polygon(
                [
                    (cx + shadow_offset, cy - d + shadow_offset),
                    (cx + d + shadow_offset, cy + shadow_offset),
                    (cx + shadow_offset, cy + d + shadow_offset),
                    (cx - d + shadow_offset, cy + shadow_offset),
                ],
                fill="#D1D5DB",
                outline=None,
            )
            draw.polygon(
                [(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)],
                fill="#FBBF24",
                outline="#92400E",
                width=int(2 * scale),
            )
            # 编号
            bbox = draw.textbbox((0, 0), "◆", font=font_axis)
            draw.text(
                (cx - int(4 * scale), cy - int(8 * scale)), "◆", fill="#7C2D12", font=font_axis
            )
        else:
            # 普通任务
            bar_top = y + int(10 * scale)
            bar_bottom = y + row_h - int(10 * scale)
            end_x_safe = max(start_x + int(8 * scale), end_x)

            if highlight_critical_path and task.get("critical"):
                # 关键路径高亮模式：红色边框 + 浅红填充
                for i in range(int(bar_top), int(bar_bottom)):
                    draw.line([(start_x, i), (end_x_safe, i)], fill=(254, 226, 226))
                draw.rounded_rectangle(
                    [start_x, bar_top, end_x_safe, bar_bottom],
                    radius=int(8 * scale),
                    outline=critical_outline,
                    width=int(3 * scale),
                )
            else:
                # 普通任务：圆角矩形 + 渐变效果（收集区域延迟绘制）
                color = palette[(row_index - 1) % len(palette)]
                # 解析颜色
                r0, g0, b0 = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
                # 亮色版本
                r1, g1, b1 = min(r0 + 31, 255), min(g0 + 153, 255), min(b0 + 93, 255)
                task_bar_regions.append(
                    {
                        "x1": start_x,
                        "x2": end_x_safe,
                        "y1": bar_top,
                        "y2": bar_bottom,
                        "r0": r0,
                        "g0": g0,
                        "b0": b0,
                        "r1": r1,
                        "g1": g1,
                        "b1": b1,
                        "critical": task.get("critical", False),
                    }
                )
                # 轮廓
                outline_color = "#DC2626" if task.get("critical") else "#1D4ED8"
                outline_width = int(2 * scale) if task.get("critical") else int(1 * scale)
                draw.rounded_rectangle(
                    [start_x, bar_top, end_x_safe, bar_bottom],
                    radius=int(8 * scale),
                    outline=outline_color,
                    width=outline_width,
                )

            # 任务起止文本
            if end_x - start_x > int(80 * scale):
                label = f"{task['start']}-{task['end']}"
                text_color = (
                    "#DC2626" if (highlight_critical_path and task.get("critical")) else "white"
                )
                _draw_text_center(
                    draw,
                    (start_x + int(4 * scale), bar_top, end_x - int(4 * scale), bar_bottom),
                    label,
                    font_small,
                    text_color,
                    max_lines=1,
                )

        # BUG 修复：task_pos 统一以 **字符串键** 索引。
        # AI 产出的 id 可能是数字（1/2）或字符串（"T1"），而 dependencies 里的引用
        # 类型又常与 id 不一致（id=1 但 dependencies=["1"]）。原实现用原始类型做键，
        # 类型不匹配时 `dep_id not in task_pos` 恒真 → 依赖箭头整体丢失，关键路径
        # 图形化失效。统一 str() 归一化后，数字/字符串引用都能正确连线。
        task_pos[str(task["id"])] = {
            "y": y,
            "h": row_h,
            "start_x": start_x,
            "end_x": end_x,
            "cy": y + row_h / 2,
        }
        y += row_h

    # 批量绘制所有渐变任务条（优化性能）
    for region in task_bar_regions:
        for i in range(int(region["y1"]), int(region["y2"])):
            t = (i - region["y1"]) / max(1, (region["y2"] - region["y1"]))
            r = int(region["r0"] + (region["r1"] - region["r0"]) * t)
            g = int(region["g0"] + (region["g1"] - region["g0"]) * t)
            b = int(region["b0"] + (region["b1"] - region["b0"]) * t)
            draw.line([(region["x1"], i), (region["x2"], i)], fill=(r, g, b))

    # ---- 依赖关系连线 ----
    for task in tasks:
        if not task.get("dependencies"):
            continue
        target = task_pos.get(str(task["id"]))
        if target is None:
            continue
        for dep_id in task["dependencies"]:
            source = task_pos.get(str(dep_id))
            if source is None:
                continue
            # 从源行右中到目标行左中
            sx = source["end_x"]
            sy = source["cy"]
            ex = target["start_x"] - int(6 * scale)
            ey = target["cy"]
            # 折线
            mid_x = (sx + ex) / 2

            if highlight_critical_path:
                # 关键路径模式：依赖线颜色根据关键路径状态
                dep_color = (
                    "#DC2626"
                    if (
                        task.get("critical")
                        or any(t.get("critical") for t in tasks if str(t["id"]) == str(dep_id))
                    )
                    else "#94A3B8"
                )
                # 连线阴影
                draw.line(
                    [
                        (sx + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), ey + int(1 * scale)),
                        (ex - int(4 * scale) + int(1 * scale), ey + int(1 * scale)),
                    ],
                    fill="#E5E7EB",
                    width=int(2 * scale),
                )
                draw.line(
                    [(sx, sy), (mid_x, sy), (mid_x, ey), (ex - int(4 * scale), ey)],
                    fill=dep_color,
                    width=int(1 * scale),
                )
                # 箭头
                draw.polygon(
                    [
                        (ex, ey),
                        (ex - int(6 * scale), ey - int(3 * scale)),
                        (ex - int(6 * scale), ey + int(3 * scale)),
                    ],
                    fill=dep_color,
                )
            else:
                # 普通模式：灰色依赖线
                draw.line(
                    [
                        (sx + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), ey + int(1 * scale)),
                        (ex - int(4 * scale) + int(1 * scale), ey + int(1 * scale)),
                    ],
                    fill="#E5E7EB",
                    width=int(2 * scale),
                )
                draw.line(
                    [(sx, sy), (mid_x, sy), (mid_x, ey), (ex - int(4 * scale), ey)],
                    fill="#94A3B8",
                    width=int(2 * scale),
                )
                # 箭头
                draw.polygon(
                    [
                        (ex, ey),
                        (ex - int(7 * scale), ey - int(4 * scale)),
                        (ex - int(7 * scale), ey + int(4 * scale)),
                    ],
                    fill="#94A3B8",
                )

    # ---- 外边框 ----
    draw.rectangle(
        [table_left, table_top, table_left + table_w,
         table_top + header_h + len(row_items) * row_h],
        outline=border,
        width=int(2 * scale),
    )

    # ---- 备注（两种模式均渲染）----
    # ✅ BUG 修复：旧实现 `if remarks and not highlight_critical_path`，
    #    而 highlight_critical_path=True 正是"任务带 dependencies/critical"
    #    的默认模式（_render_gantt_with_dependencies）—— 于是凡带依赖的进度计划
    #    备注（如「春节停工」「雨季顺延」）一律被丢弃，同时 height 里仍预留了
    #    remarks_h 造成底部空白。现统一渲染。
    if remarks:
        note_top = y + int(16 * scale)
        notes_text = "  ".join(f"· {r}" for r in remarks if r)
        if notes_text:
            draw.text((table_left, note_top), notes_text, fill="#374151", font=font_small)
        y += remarks_h

    # ---- 图例 ----
    legend_y = y + int(18 * scale)
    legend_x = table_left
    if highlight_critical_path:
        # 增强版图例（含关键路径）
        legend_items = [
            ("#2B579A", "普通任务"),
            ("#FBBF24", "里程碑 ◆"),
            ("#DC2626", "关键路径"),
        ]
        for color, label in legend_items:
            if label == "关键路径":
                draw.rounded_rectangle(
                    [
                        legend_x,
                        legend_y - int(12 * scale),
                        legend_x + int(24 * scale),
                        legend_y + int(4 * scale),
                    ],
                    radius=int(3 * scale),
                    fill="#FEE2E2",
                    outline="#DC2626",
                    width=int(2 * scale),
                )
                draw.text(
                    (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                    label,
                    fill="#DC2626",
                    font=font_small,
                )
            else:
                draw.rounded_rectangle(
                    [
                        legend_x,
                        legend_y - int(12 * scale),
                        legend_x + int(24 * scale),
                        legend_y + int(4 * scale),
                    ],
                    radius=int(3 * scale),
                    fill=color,
                    outline="#1E3A5F",
                    width=int(1 * scale),
                )
                draw.text(
                    (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                    label,
                    fill="#1E3A5F",
                    font=font_small,
                )
            legend_x += int(130 * scale)
    else:
        legend_items = [
            ("#2B579A", "任务条"),
            ("#FBBF24", "里程碑 ◆"),
            ("#DC2626", "关键路径（红色边框）"),
        ]
        for color, label in legend_items:
            draw.rounded_rectangle(
                [
                    legend_x,
                    legend_y - int(12 * scale),
                    legend_x + int(24 * scale),
                    legend_y + int(4 * scale),
                ],
                radius=int(3 * scale),
                fill=color,
                outline="#1E3A5F",
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                label,
                fill="#1E3A5F",
                font=font_small,
            )
            legend_x += int(200 * scale)

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 甘特图渲染完成: 任务=%d 尺寸=%dKB DPI=300",
        len(tasks),
        size_kb,
    )
    return result_stream

def gantt_json_to_mermaid(plan: dict) -> str:
    """将 tasks + dependencies 结构转换为 Mermaid Gantt 代码字符串

    自动计算日期偏移和持续时间。

    Args:
        plan: {"tasks": [{"id":"T1","name":"施工准备","start":"2026-01-01",
                          "end":"2026-01-10","dependencies":[],"critical":true},...]}

    Returns:
        Mermaid Gantt 代码字符串
    """
    try:
        tasks = plan.get("tasks", [])
        if not tasks:
            return ""

        lines = [
            "gantt",
            f'    title {plan.get("title", "施工进度计划")}',
            "    dateFormat YYYY-MM-DD",
            "    axisFormat %m-%d",
        ]

        # 计算基准日期

        base_date = None
        for t in tasks:
            start_str = t.get("start", "")
            if isinstance(start_str, str) and start_str.strip():
                try:
                    d = datetime.strptime(start_str.strip(), "%Y-%m-%d")
                    if base_date is None or d < base_date:
                        base_date = d
                except ValueError:
                    pass

        if base_date is None:
            base_date = datetime.now()

        current_section = ""
        for t in tasks:
            name = t.get("name", "")
            start_str = t.get("start", "")
            end_str = t.get("end", "")
            section = t.get("section", "")

            if section and section != current_section:
                lines.append(f"    section {section}")
                current_section = section

            if (
                isinstance(start_str, str)
                and isinstance(end_str, str)
                and start_str.strip()
                and end_str.strip()
            ):
                lines.append(f"    {name} : {start_str}, {end_str}")
            elif isinstance(start_str, (int, float)):
                start_day = int(start_str)
                end_day = int(t.get("end", start_day))
                start_date = base_date + timedelta(days=start_day - 1)
                end_date = base_date + timedelta(days=end_day - 1)
                lines.append(
                    f'    {name} : {start_date.strftime("%Y-%m-%d")}, {end_date.strftime("%Y-%m-%d")}'
                )

        return "\n".join(lines)
    except Exception as e:
        logger.warning("gantt_json_to_mermaid 转换失败: %s", e)
        return ""

def _render_gantt_with_dependencies(plan: dict) -> BytesIO | None:
    """增强版甘特图渲染器（向后兼容别名）

    已合并到 _render_gantt_image_v2，使用 highlight_critical_path=True 参数。
    保留此函数作为向后兼容的别名。

    Args:
        plan: 甘特图计划数据

    Returns:
        BytesIO PNG 图片，失败时返回 None
    """
    return _render_gantt_image_v2(plan, highlight_critical_path=True)

