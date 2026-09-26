"""Architecture 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _build_gantt_marks,
    _draw_text_center,
    _fit_text_with_ellipsis,
    _image_font,
    _image_to_stream,
    _render_hq,
    _text_height,
    _text_width,
    _wrap_text,
)


import re

logger = logging.getLogger(__name__)


def _is_highlighted(node: dict) -> bool:
    """判断节点是否为「核心职责高亮节点」（样张中的琥珀橙节点）。

    容错取值：AI 产出的布尔可能是 true/"true"/1/"是"，统一按真值解释。
    """
    v = node.get("highlight")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return str(v).strip().lower() in {"true", "yes", "1", "是"}



def _layout_architecture_tree(root: dict) -> tuple[list[dict], int, int]:
    """为架构树节点计算坐标（自顶向下、横向展开）。"""
    nodes_pos: list[dict] = []
    leaf_counter = [0]
    used_ids: set[str] = set()

    def _unique_id(raw_id) -> str:
        """生成树内唯一节点 ID。

        ✅ BUG 修复：旧实现自动 ID 固定取 ``n{counter}``，而 AI 数据里常出现
        显式 ``id="n1"/"n2"``（尤其从别处复制整棵子树时）。一旦自动 ID 与显式
        ID 撞车，nodes_pos 里就会出现两个同 id 节点 → node_by_id 字典互相覆盖 →
        按 parent_id 查父节点时连到错误分支，表现为"连线连到别的分支、
        部分节点悬空无连线"。这里登记已用 ID，自动 ID 递增到不冲突为止。
        """
        base = str(raw_id) if raw_id else f"n{leaf_counter[0]}"
        candidate = base
        suffix = 1
        while candidate in used_ids:
            suffix += 1
            candidate = f"{base}#{suffix}"
        used_ids.add(candidate)
        return candidate

    # 递归深度上限：畸形/超深数据按叶子截断，避免递归爆栈（RecursionError）
    _MAX_DEPTH = 30

    def _dfs(node: dict, depth: int, x_start: float, parent_id: str | None = None,
             visited: set | None = None) -> float:
        """DFS 返回该子树的 x 结束位置（独占宽度）。"""
        if visited is None:
            visited = set()
        node_w = 220
        node_h = 52
        h_gap = 24
        v_gap = 48
        label = str(node.get("label") or node.get("name") or "").strip()
        # ✅ BUG 修复（环检测形同虚设）：旧实现用 `_unique_id()` 产出的"全局唯一 ID"
        #    做 visited 判重 —— 而 _unique_id 保证任何两次调用返回的 ID 都不同，
        #    于是 `nid in visited` **恒为 False**，环检测从未生效。当树数据含环
        #    （子节点直接/间接引用祖先 dict）时 _dfs 无限递归，最终 RecursionError
        #    崩溃，架构图整张丢失（被 render_mermaid_to_bytes 的兜底 except 静默吞掉）。
        #    改用**对象身份** id(node) 判重：同一 dict 被重复访问即判定为环。
        #    同时加入深度上限，防御超深链导致的爆栈。
        if id(node) in visited or depth > _MAX_DEPTH:
            nid = _unique_id(node.get("id"))
            x = x_start
            y = depth * (node_h + v_gap) + 60
            nodes_pos.append(
                {
                    "id": nid,
                    "label": label,
                    "x": x,
                    "y": y,
                    "w": node_w,
                    "h": node_h,
                    "depth": depth,
                    "has_children": False,
                    "parent_id": parent_id,
                    "highlight": _is_highlighted(node),
                }
            )
            return x + node_w + h_gap
        visited.add(id(node))
        nid = _unique_id(node.get("id"))
        leaf_counter[0] += 1

        children = (
            node.get("children")
            or node.get("nodes")
            or node.get("sub_units")
            or node.get("branches")
            or []
        )
        if not isinstance(children, list):
            children = []

        if not children:
            # 叶子节点：自己占用一个位置
            x = x_start
            y = depth * (node_h + v_gap) + 60
            nodes_pos.append(
                {
                    "id": nid,
                    "label": label,
                    "x": x,
                    "y": y,
                    "w": node_w,
                    "h": node_h,
                    "depth": depth,
                    "has_children": False,
                    "parent_id": parent_id,
                    "highlight": _is_highlighted(node),
                }
            )
            return x + node_w + h_gap

        # 有子节点：先布局子节点，再居中放置自己
        child_x = x_start
        child_positions: list[float] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            end_x = _dfs(child, depth + 1, child_x, parent_id=nid, visited=visited)
            child_positions.append((child_x, end_x - child_x - h_gap))
            child_x = end_x

        if not child_positions:
            # 过滤后无有效子节点
            x = x_start
            y = depth * (node_h + v_gap) + 60
            nodes_pos.append(
                {
                    "id": nid,
                    "label": label,
                    "x": x,
                    "y": y,
                    "w": node_w,
                    "h": node_h,
                    "depth": depth,
                    "has_children": False,
                    "parent_id": parent_id,
                    "highlight": _is_highlighted(node),
                }
            )
            return x + node_w + h_gap

        # 自己的 x = 第一个子节点中心 - 自己宽度的一半
        first_x, first_w = child_positions[0]
        last_x, last_w = child_positions[-1]
        children_center = (first_x + last_x + last_w) / 2
        x = children_center - node_w / 2
        y = depth * (node_h + v_gap) + 60
        nodes_pos.append(
            {
                "id": nid,
                "label": label,
                "x": x,
                "y": y,
                "w": node_w,
                "h": node_h,
                "depth": depth,
                "has_children": True,
                "parent_id": parent_id,
                "highlight": _is_highlighted(node),
            }
        )
        return max(child_x, x + node_w + h_gap)

    _dfs(root, 0, 40)

    # 归一化坐标（让最左侧为 0）
    if nodes_pos:
        min_x = min(n["x"] for n in nodes_pos)
        for n in nodes_pos:
            n["x"] -= min_x

    max_x = max((n["x"] + n["w"] for n in nodes_pos), default=0)
    max_y = max((n["y"] + n["h"] for n in nodes_pos), default=0)
    return nodes_pos, int(max_x + 80), int(max_y + 40)

@_render_hq
def _render_architecture_image_v2(data: dict) -> BytesIO | None:
    """商业级架构图渲染器 v2：真正的树形可视化，含父子连线。"""
    # ✅ 统一容错规则：与 chart_validators.normalize_architecture_tree 共用同一套归一逻辑
    #    （校验侧与渲染侧容错度必须一致，否则会出现"渲染器能画、校验器判非法→正文删块"）。
    #    顺带修复：{"type":"architecture","root":"项目部","nodes":[...]} 这种
    #    "字符串根 + 节点列表"形态此前直接走到 `root = data.get("root")`（取到字符串）
    #    → not isinstance(root, dict) → return None，本渲染器自身无法处理该形态，
    #    只能依赖调用方预先归一。
    from app.services.chart_validators import normalize_architecture_tree

    if not isinstance(data, dict):
        return None
    root = normalize_architecture_tree(data)
    if root is None:
        return None

    nodes_pos, width, height = _layout_architecture_tree(root)
    if not nodes_pos:
        return None
    logger.info(
        "[PIL v2渲染] 架构图渲染启动: 节点数=%d 画布=%dx%dpx",
        len(nodes_pos),
        width,
        height,
    )

    # 高度至少包含标题
    title_h = 56
    height += title_h

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    scale = 2.5

    img = Image.new("RGB", (int(width * scale), int(height * scale)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_node = _image_font(int(15 * scale), bold=True)

    # ---- 标题 ----
    title = str(root.get("label") or root.get("name") or "组织架构图")
    draw.text((int(40 * scale), int(16 * scale)), title, fill="#2E5FA3", font=font_title)

    # 缩放节点位置
    for n in nodes_pos:
        n["x"] = int(n["x"] * scale)
        n["y"] = int(n["y"] * scale)
        n["w"] = int(n["w"] * scale)
        n["h"] = int(n["h"] * scale)

    node_by_id = {n["id"]: n for n in nodes_pos}

    # ---- 绘制连线（先画连线）----
    # ✅ 样张风格（2026-09-18）：灰色直角折线、扁平无阴影、无箭头。
    edge_color = "#8C8C8C"
    for n in nodes_pos:
        if n["depth"] == 0:
            continue
        # BUG-FIX-19: 使用 parent_id 直接查找父节点，替代原启发式算法
        # 原实现用"depth-1且x最近的"启发式，对多分支树会找到错误的父节点
        parent = node_by_id.get(n.get("parent_id")) if n.get("parent_id") else None
        if not parent:
            continue
        # 父节点底边中点 -> 子节点顶边中点（折线）
        sx = parent["x"] + parent["w"] / 2
        sy = parent["y"] + parent["h"] + title_h
        ex = n["x"] + n["w"] / 2
        ey = n["y"] + title_h
        mid_y = (sy + ey) / 2
        draw.line(
            [(sx, sy), (sx, mid_y), (ex, mid_y), (ex, ey)], fill=edge_color, width=int(2 * scale)
        )

    # ---- 绘制节点 ----
    # ✅ 样张配色（2026-09-18）：根=深藏青 → 一级 → 二级 → 三级 → 四级逐层变浅；
    #    highlight:true 的节点（本方案履约核心部门）用琥珀橙 + 深藏青粗体字；
    #    扁平无阴影无描边。
    depth_colors = [
        ("#1F3864", "#FFFFFF"),  # 根
        ("#2E5FA3", "#FFFFFF"),  # 一级
        ("#4472C4", "#FFFFFF"),  # 二级
        ("#5B9BD5", "#FFFFFF"),  # 三级
        ("#A9C6E8", "#1F3864"),  # 四级及以下
    ]
    HIGHLIGHT_FILL = "#FFC000"
    for n in nodes_pos:
        depth_idx = min(n["depth"], len(depth_colors) - 1)
        if n.get("highlight") and n["depth"] > 0:
            fill_c, text_c = HIGHLIGHT_FILL, "#1F3864"
        else:
            fill_c, text_c = depth_colors[depth_idx]
        x, y, w, h = n["x"], n["y"] + title_h, n["w"], n["h"]
        draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=fill_c)
        _draw_text_center(
            draw,
            (x + 4, y + 4, x + w - 4, y + h - 4),
            n["label"],
            font_node,
            text_c,
            max_lines=2,
        )

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 架构图渲染完成: 节点=%d 尺寸=%dKB DPI=300",
        len(nodes_pos),
        size_kb,
    )
    return result_stream

