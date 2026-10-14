"""Architecture 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
import math
import os
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _draw_text_center,
    _draw_text_vertical_center,
    _image_font,
    _image_to_stream,
    _render_hq,
    _vertical_text_capacity,
    delivered_worst_mm,
    paper_font_mm as _common_paper_font_mm,
    paper_height_binds,
    stitch_geometry,
)

logger = logging.getLogger(__name__)

# ============================================================
# 布局尺寸（单一事实源；单位=设计像素，渲染时统一乘 scale）
# ============================================================
NODE_W = 220        # 普通节点宽
NODE_H = 52         # 普通节点高
H_GAP = 24          # 兄弟节点水平间距
V_GAP = 48          # 层间垂直间距
TOP_PAD = 60        # 首层顶部留白（旧实现内联在 `depth * (h + gap) + 60` 里）
SIDE_START = 40     # DFS 起始 x（布局后按 min_x 归一到 0，仅影响相对位移）
CANVAS_PAD_X = 80   # 画布右侧留白
CANVAS_PAD_Y = 40   # 画布底部留白

# ✅ L3（叶子层）竖排版式（2026-10-10 ·《架构图 L3 叶子层文字垂直显示优化方案》）
#   旧版所有节点同尺寸横排：11 个叶子 ≈ 2660 设计像素宽，而导出 DOCX 是按
#   「栏宽 16cm」把整图**等比缩放**塞进页面 —— 画布越宽，同一字号在纸面上越小
#   （实测压到不可读，屏幕预览也要横向滚动）。竖排把叶子改成"窄高节点 +
#   文字自上而下直立排列"（等价 CSS writing-mode: vertical-rl +
#   text-orientation: upright），画布宽度约减半，字号在纸面上的相对尺寸随之
#   放大约 2 倍。
LEAF_VERTICAL_W = 96            # 竖排叶子宽（≈ 横排 220 的 0.44 倍，对应文档 130→60）
LEAF_VERTICAL_H = 232           # 竖排叶子高（对应文档 50→200）
LEAF_VERTICAL_GAP = 12          # 竖排叶子水平间距（比横排更紧凑，对应 10→8）
LEAF_VERTICAL_MIN_DEPTH = 2     # 仅 depth≥2（L3 及更深）的叶子参与竖排；二级保持横排
LEAF_VERTICAL_THRESHOLD = 6     # 可竖排叶子数 > 6 自动切竖排（≤6 保持横排）
LEAF_VERTICAL_FONT_SIZE = 15    # 竖排字号**下限**（R60 起会按画布宽自适应放大，见下）
LEAF_VERTICAL_PAD_X = 6         # 竖排节点内边距（横向，对应文档 padding 左右 6px）
LEAF_VERTICAL_PAD_Y = 12        # 竖排节点内边距（纵向，对应文档 padding 上下 15px）
LEAF_VERTICAL_CHAR_GAP = 4      # 竖排字间距（对应文档 letter-spacing: 2px）

# ============================================================
# ✅ 纸面可读性下限（2026-10-10 · 收口 R59 §9 遗留项①「竖排不是万能解」）
# ============================================================
#   R59 把叶子改竖排后画布约减半，但**极宽的树在纸上仍然偏小**（16 叶/4 组实测
#   纸面字号仅 1.31mm）；更糟的是**叶子不多却排得宽的横排图**——6 叶 + 1 直属岗
#   只有 1764px 宽、纸面 1.36mm，比 16 叶的竖排图还小，因为旧切换**只看叶子数**。
#   机理（与 `routers/export.py:_fit_image_cm` 同口径，已用真实 PNG 逐例核对）：
#     纸面字号(mm) = 图内字号(px) × scale × 2.54 / DPI × fit × 10
#     宽度受限时 fit = 16cm ÷ 纸面宽 ⇒ **scale 与 DPI 全部约掉**
#     ⇒ 只有「字号 ÷ 画布宽」这一个比值决定纸上多大。
#   ⇒ 最优解不是"限制单图规模"（那会删内容），而是**让字号跟着画布宽走**，
#     并把「横排会不会印不清」也纳入版式选择。两者都以「纸面毫米数」为唯一标尺。
ARCH_RENDER_SCALE = 2.5         # 超采样倍数（渲染器内 `scale` 与本常量同源）
ARCH_OUTPUT_DPI = 300.0         # `_image_to_stream` 写入的 PNG DPI
PAPER_COLUMN_WIDTH_CM = 16.0    # DOCX 正文栏宽上限（与 export._CHART_MAX_WIDTH_CM 同值）
PAPER_MAX_HEIGHT_CM = 22.0      # 单图最大高度（与 export._CHART_MAX_HEIGHT_CM 同值）
PAPER_FONT_MIN_MM = 2.4         # 纸面字号下限 ≈ 6.8pt（中文「小六」2.3~「六号」2.65mm）
NODE_FONT_BASE = 15             # 横排节点字号（R59 及以前的固定值）
NODE_FONT_MAX = 18              # 横排节点字号硬上限：220×52 的 box 要能容 2 行
                                #   `_draw_text_center` 的 line_h = 字高 + 4，
                                #   2×(18+4) = 44 = NODE_H − 2×4 内边距，19 就会压边框
LEAF_VERTICAL_FONT_MAX = 28     # 竖排叶子字号上限（96×232 box 在 28 号下容量仍余 12 字）
TITLE_FONT_BASE = 20            # 标题字号（画在框外，无 box 容量约束）
TITLE_FONT_MAX = 30             # 标题上限：title_h=56 的标题带里 20+36 富余，视觉上限而已

# 运维回退通道（环境变量，无需改代码）：
#   ARCH_LEAF_VERTICAL=0       整体关闭竖排，逐字回退旧横排版式
#   ARCH_LEAF_VERTICAL_MIN=N   调整自动切换阈值（非法/负值回落默认 6）
#   ARCH_LEAF_PAPER_MIN_MM=0   关闭整套字号自适应与可读性升级（回退固定字号）
#   ARCH_LEAF_AUTO_VERTICAL=0  只关「横排印不清就切竖排」，保留字号自适应与叶子数规则
ENV_VERTICAL_SWITCH = "ARCH_LEAF_VERTICAL"
ENV_VERTICAL_THRESHOLD = "ARCH_LEAF_VERTICAL_MIN"
ENV_PAPER_FONT_MIN_MM = "ARCH_LEAF_PAPER_MIN_MM"
ENV_AUTO_VERTICAL = "ARCH_LEAF_AUTO_VERTICAL"

_OFF_VALUES = {"0", "false", "no", "off", "关闭"}

# 递归深度上限：畸形/超深数据按叶子截断，避免递归爆栈（RecursionError）
_MAX_DEPTH = 30


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


def _children_of(node: dict) -> list[dict]:
    """取有效子节点列表（children / nodes / sub_units / branches 四种别名同口径）。

    刻意收敛为唯一出口：布局 DFS 与「竖排候选计数」必须共用同一套子节点判定，
    否则会出现「计数说有 8 个叶子 → 实际只竖排 6 个」的口径分叉。
    非 dict 子项在此直接过滤（旧实现在循环里逐条 `isinstance` 跳过，效果等价：
    子项全部非法时节点按叶子处理）。
    """
    children = (
        node.get("children")
        or node.get("nodes")
        or node.get("sub_units")
        or node.get("branches")
        or []
    )
    if not isinstance(children, list):
        return []
    return [c for c in children if isinstance(c, dict)]


def _env_flag(name: str, default: bool = True) -> bool:
    """布尔型环境变量：未设置取默认值；显式 0/false/no/off/关闭 视为关。"""
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in _OFF_VALUES


def leaf_vertical_enabled() -> bool:
    """竖排版式总开关（默认开）。关闭即逐字回退旧横排版式。"""
    return _env_flag(ENV_VERTICAL_SWITCH, True)


def vertical_leaf_threshold() -> int:
    """自动切竖排的叶子数阈值（非法值回落默认，绝不抛）。"""
    raw = str(os.environ.get(ENV_VERTICAL_THRESHOLD, "") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return LEAF_VERTICAL_THRESHOLD
    return value if value >= 0 else LEAF_VERTICAL_THRESHOLD


def paper_font_min_mm() -> float:
    """纸面字号下限（mm）。

    显式置 0/负数 → 返回 0.0，表示**整套可读性自适应关闭**（字号回退固定值、
    版式只按叶子数判定），逐字回到 R59 行为。非法字符串**回落默认值**而不是
    关闭 —— 运维打错一个字母不该让交付文档里的图突然变小。
    """
    raw = str(os.environ.get(ENV_PAPER_FONT_MIN_MM, "") or "").strip()
    if not raw:
        return PAPER_FONT_MIN_MM
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s 不是数字（%r），按默认 %.1fmm 处理",
                       ENV_PAPER_FONT_MIN_MM, raw, PAPER_FONT_MIN_MM)
        return PAPER_FONT_MIN_MM
    return value if value > 0 else 0.0


def auto_vertical_enabled() -> bool:
    """「横排印不清就改竖排」的升级开关（默认开；字号下限置 0 时一并失效）。"""
    return _env_flag(ENV_AUTO_VERTICAL, True) and paper_font_min_mm() > 0
# ============================================================
# ✅ 架构图规模预算守卫（2026-10-10 · 收口 R59 §9 遗留项①「限制单张架构图规模」安全切片）
#   文档明确：AI 侧「单图节点 4~16，超则拆多张」约束已接线（content_checkpoint.py:1129，
#   DLV-04）；但服务端此前**零检测**——AI 偶尔不拆时，超大图静默渲染成纸面不可读的巨图。
#   本守卫只做「可观测 + 可配置上限」，不拆分、不丢数据（拆图需改 get_or_render 契约，
#   跨 7 类图表高风险，列为独立授权轮）。
ENV_MAX_LEAVES = "ARCH_MAX_LEAVES"      # 单张架构图叶子数上限（默认 24，超则告警建议拆分）
ENV_MAX_NODES = "ARCH_MAX_NODES"        # 单张架构图节点数上限（默认 60，超则告警建议拆分）
_ARCH_MAX_LEAVES_DEFAULT = 24
_ARCH_MAX_NODES_DEFAULT = 60


def arch_max_leaves() -> int:
    """单张架构图叶子数上限（非法/负值回落默认，绝不抛）。"""
    raw = str(os.environ.get(ENV_MAX_LEAVES, "") or "").strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _ARCH_MAX_LEAVES_DEFAULT
    return v if v > 0 else _ARCH_MAX_LEAVES_DEFAULT


def arch_max_nodes() -> int:
    """单张架构图节点数上限（非法/负值回落默认，绝不抛）。"""
    raw = str(os.environ.get(ENV_MAX_NODES, "") or "").strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _ARCH_MAX_NODES_DEFAULT
    return v if v > 0 else _ARCH_MAX_NODES_DEFAULT


# ============================================================
# ✅ 超宽自动拆图（2026-10-10 · R61 授权轮 · 收口 R59 §9 遗留项①真解）
#   背景：R60 已加「规模预算守卫」（仅告警建议拆分）。本回合落地**真拆图**——
#   超预算时把架构树按顶层分支分组，每组渲染为子图后**竖向拼接成单张更高 PNG**
#   返回，契约（单 BytesIO / 单 PNG）不变 ⇒ 缓存/预览/导出/PDF/图号零改动。
ENV_AUTO_SPLIT = "ARCH_AUTO_SPLIT"          # 超宽自动拆图总开关（默认开）
ENV_SPLIT_MAX_LEAVES = "ARCH_SPLIT_MAX_LEAVES"  # 每组叶子数上限（默认 8）
ENV_SPLIT_MAX_PARTS = "ARCH_SPLIT_MAX_PARTS"    # 子图数量硬上限（默认 12）
_ARCH_SPLIT_MAX_LEAVES_DEFAULT = 8
_ARCH_SPLIT_MAX_PARTS_DEFAULT = 12


def _arch_autosplit_enabled() -> bool:
    """超宽自动拆图总开关（默认开；env 显式 0/false/no/off/关闭 才关）。"""
    return _env_flag(ENV_AUTO_SPLIT, True)


def _arch_split_max_leaves() -> int:
    raw = str(os.environ.get(ENV_SPLIT_MAX_LEAVES, "") or "").strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _ARCH_SPLIT_MAX_LEAVES_DEFAULT
    return v if v > 0 else _ARCH_SPLIT_MAX_LEAVES_DEFAULT


def _arch_split_max_parts() -> int:
    raw = str(os.environ.get(ENV_SPLIT_MAX_PARTS, "") or "").strip()
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _ARCH_SPLIT_MAX_PARTS_DEFAULT
    return v if v > 0 else _ARCH_SPLIT_MAX_PARTS_DEFAULT


def _should_split_arch(root: dict) -> bool:
    """是否触发自动拆图：顶层 ≥2 分支 且 整体超规模预算（叶子>上限 或 节点>上限）。"""
    if len(_children_of(root)) < 2:
        return False
    over, _, _ = architecture_over_budget(root)
    return over


# ============================================================
# ✅ 拆图纸面收益决策（2026-10-10 · R62 · 收口 R60 §9「字号自适应 × 自动拆图」联合复测）
# ============================================================
#   R61 按「每组叶子数上限」贪心分组后**无条件**竖向拼接，隐含假设「越窄越好」。
#   本轮对 8 组超预算用例逐份数扫描真实交付毫米
#   （`artifacts/r62_leftover/_r62_curve_probe.py`）证明该假设不成立：
#   **收益曲线先升后降**，R61 的自然份数只在 2/8 组落在顶点，其余 6 组「拆过头」——
#   拼接图高度越过 22cm 上限后整张图被二次缩放，实测 4 组越拆越小（−12% ~ −25%），
#   插入宽度只剩 3.25~7.52cm（半个版面凭空浪费）。
#   ⇒ 份数必须由**纸面毫米**决定而不是由每组叶子数决定；叶子预算继续做**搜索上界 +
#     每份硬约束**（R60 规模预算守卫与 R61 `_arch_split_max_leaves` 的契约都原样成立）。
_SPLIT_GAP_DESIGN_PX = 20      # 竖向拼接间隙（设计像素；落图时乘 scale）
_TITLE_BAND_PX = 56            # 标题带高度（与渲染路径内联的 `title_h = 56` 同值）


def _part_geometry(root: dict) -> tuple[int, int, int, int]:
    """纯布局探针：返回 (画布宽, 含标题带总高, 该版式实际字号px, 可竖排叶子数)，**不绘制**。

    取字号口径与 `_render_architecture_image_v2` **逐字同源**（同一对 resolver、同一
    标题带高、同一 `paper_font_min_mm()`），所以「拆图前的预估」与「交付后的实测」可以
    直接对账（`_render_architecture_split` 第④步会用真实 PNG 尺寸复核一次）。
    畸形/空树一律返回 (0, 0, 0, 0) 且绝不抛 —— 拆图是增强，不能反过来把渲染打挂。
    """
    try:
        nodes_pos, w, h = _layout_architecture_tree(root)
    except Exception:
        return 0, 0, 0, 0
    if not nodes_pos or w <= 0:
        return 0, 0, 0, 0
    labels = [n["label"] for n in nodes_pos if n.get("vertical")]
    _min = paper_font_min_mm()
    if labels:
        px = resolve_leaf_vertical_font_px(w, labels, canvas_h=h,
                                          title_h=_TITLE_BAND_PX, min_mm=_min)
    else:
        px = resolve_paper_font_px(w, base=NODE_FONT_BASE, ceiling=NODE_FONT_MAX,
                                   canvas_h=h, title_h=_TITLE_BAND_PX, min_mm=_min)
    return int(w), int(h + _TITLE_BAND_PX), int(px), len(labels)


def _group_branches_by_budget(branches: list[dict], max_leaves: int) -> list[list[dict]]:
    """按「每组叶子数 ≤ max_leaves」贪心连续分组（R61 原口径，逐字保留）。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    cur_leaves = 0
    for b in branches:
        leaves, _ = _arch_counts(b)
        if cur and cur_leaves + leaves > max_leaves:
            groups.append(cur)
            cur = []
            cur_leaves = 0
        cur.append(b)
        cur_leaves += leaves
    if cur:
        groups.append(cur)
    return groups


def _group_branches_balanced(branches: list[dict], parts: int) -> list[list[dict]]:
    """把分支**连续**切成 parts 组（按叶子数尽量均分；2 ≤ parts ≤ len 时必得 parts 组）。

    连续切而不是交错分配：拆出来是给人读的，同一子系统必须留在同一张子图里。
    切点取「前缀和最接近 k·total/parts」的下标，并保证后面的组各自至少还剩一个分支
    （否则 parts 组数会塌成更少，候选份数在搜索域里凭空消失 —— R62 首版就栽在这里）。
    """
    if not branches:
        return []
    if parts <= 1:
        return [list(branches)]
    n = len(branches)
    if parts > n:
        parts = n
    weights = [max(1, _arch_counts(b)[0]) for b in branches]
    total = sum(weights)
    prefix = [0]
    for w in weights:
        prefix.append(prefix[-1] + w)
    cuts = [0]
    for k in range(1, parts):
        want = total * k / float(parts)
        lo = cuts[-1] + 1
        hi_i = n - (parts - k)
        cuts.append(min(range(lo, hi_i + 1), key=lambda j: abs(prefix[j] - want)))
    cuts.append(n)
    return [branches[cuts[k]:cuts[k + 1]] for k in range(parts)]


def plan_architecture_split(root: dict, *, max_leaves: int,
                            max_parts: int) -> tuple[list[list[dict]], float, float]:
    """拆图份数决策：返回 (分组, 该方案交付的最差纸面毫米, 不拆时的纸面毫米)。

    纯函数、不绘制、绝不抛。`len(分组) < 2` ⇒ **拆无可拆**，调用方应保持单图。

    三条设计约束各有护栏：
      ① 目标函数 = `delivered_worst_mm`（宽 16cm / 高 22cm **双上限取更紧者**）——
         R61 缺的正是高度这一项，只按「变窄」判收益；
      ② 份数上界 = 叶子预算贪心组数（`max_leaves` 仍然 drives 分组规模，R61 的
         `_arch_split_max_leaves` 行为契约含 monkeypatch 翻转测试原样保留），且
         **每份自身不得超过规模预算 `ARCH_MAX_LEAVES`**（R60 预算守卫）——
         拆完还是超限图就没有意义；
      ③ 只在「严格更好」时才拆：搜索域内取毫米最大者，都不如单图就返回空。
    """
    if not isinstance(root, dict):   # 畸形入参：拆图是增强，不能反过来打挂渲染
        return [], 0.0, 0.0
    branches = _children_of(root)
    if len(branches) < 2:
        return [], 0.0, 0.0
    w0, h0, px0, _ = _part_geometry(root)
    single_mm = paper_font_mm(px0, w0, h0)
    if single_mm <= 0:
        return [], 0.0, 0.0
    hi = min(len(_group_branches_by_budget(branches, max_leaves)), max_parts, len(branches))
    budget_leaves = arch_max_leaves()      # R60 规模预算守卫：拆完每张子图自己也必须达标
    best_groups: list[list[dict]] = []
    best_mm = 0.0
    for parts in range(2, hi + 1):
        groups = _group_branches_balanced(branches, parts)
        if len(groups) < parts:
            continue
        # 每份都必须在**预算守卫**（ARCH_MAX_LEAVES）内：拆完还是超限图就没有意义。
        # ⚠️ 这里刻意不用 `max_leaves`（R61 的每组步长，默认 8）做硬约束 —— 4 分支
        #    ×10 叶在步长 8 下每个单分支都已 10 > 8，硬约束会把**所有**候选一起否掉，
        #    于是「拆无可拆」，纸面停在 1.01mm；而 g=2 实测 2.00mm。步长只当作
        #    **份数上界**（`hi`）保留，R61 的 `_arch_split_max_leaves` 契约不破。
        if any(sum(max(1, _arch_counts(b)[0]) for b in grp) > budget_leaves for grp in groups):
            continue
        geo = [_part_geometry({"label": "", "children": grp}) for grp in groups]
        if any(w <= 0 or h <= 0 for w, h, _, _ in geo):
            continue
        mm = delivered_worst_mm([gpx for _, _, gpx, _ in geo],
                                [(gw, gh) for gw, gh, _, _ in geo], _SPLIT_GAP_DESIGN_PX)
        if mm > best_mm + 1e-9:
            best_mm, best_groups = mm, groups
    if best_mm <= single_mm + 1e-9:
        return [], 0.0, single_mm
    return best_groups, best_mm, single_mm


def review_delivered_mm(fonts_px: list[int], sizes: list[tuple[float, float]]) -> float:
    """第④步复核的唯一出口：用**真实 PNG 反推的设计像素尺寸**重算最差子图纸面毫米。

    与 `plan_architecture_split` 共用 `delivered_worst_mm`（判据只有一份），但单独成函数
    才能让护栏**只摘掉复核**而不误伤决策 —— 否则 patch 共享符号会在第①步就否决，
    「安全网失效」这种缺陷任何测试都不会红（A/B M6 实测）。
    """
    return delivered_worst_mm(fonts_px, sizes, _SPLIT_GAP_DESIGN_PX)


def _render_architecture_split(root: dict) -> BytesIO | None:
    """超预算时按顶层分支分组渲染，竖向拼接回单张 PNG（契约不变）。

    仅做顶层分组（不递归拆分支）；任一子图渲染异常则跳过，全部失败回落 None
    （由调用方 fallback 单图渲染）。拼接后调用 `_image_to_stream` 写入 300 DPI，
    与单图契约一致，导出/预览无感。

    ⚠️ R62 起「拆几张」由 `plan_architecture_split` 按**交付纸面毫米**决定：竖向拼接
    是拿高度换宽度，而 DOCX 插图同时受 16cm 宽 / 22cm 高两个上限、取更紧者，份数过
    多会让高上限反超宽度上限、整张图（含每张子图）被二次缩放 —— 越拆越小。因此
    **拆图只有在「确实变大」时才发生**，且落图前用真实 PNG 尺寸再复核一次。
    """
    branches = _children_of(root)
    if len(branches) < 2:
        return None
    root_label = str(root.get("label") or root.get("name") or "组织架构图")
    max_leaves = _arch_split_max_leaves()
    max_parts = _arch_split_max_parts()

    # ---- ① 份数决策（纯几何，不绘制） ------------------------------------------
    groups, plan_mm, single_mm = plan_architecture_split(root, max_leaves=max_leaves,
                                                        max_parts=max_parts)
    if len(groups) < 2:
        # 拆无可拆：拆完不会更好（高度反噬 / 单分支本身超预算）。保持单图，由
        # `_render_architecture_image_v2` 的那一条告警向用户说清还剩多少毫米。
        logger.info("架构图超预算但拆图无收益（不拆 %.2fmm），保持单图不堆叠", single_mm)
        return None
    g = len(groups)

    # ---- ② 只记决策日志；告警推迟到交付确认之后（见第⑤步） --------------------
    #      旧实现在这里按**整树**口径打唯一一条告警，两处后果（R62 护栏实测）：
    #      ① 拆成功后仍打印「纸面字号仅 1.01mm … 建议拆分为约 2 张子图」——数字是
    #         过期值（实际交付 2.00mm），并且叫用户去做程序刚刚做过的事；
    #      ② 中途任何一步回落单图时，第②步已经打过，单图路径再打一次 ⇒ 同一张图
    #         2 条一模一样的 warning，打破 R60「同一张图只告警 1 次」。
    #      两处都是 R62 合并态复测新撞出的，**不属于** R61 登记的三条未落地项。
    logger.info(
        "架构图超预算，自动拆分为 %d 张子图（不拆 %.2fmm → 拆后 ≥%.2fmm，仍为单张 PNG）",
        g, single_mm, plan_mm,
    )

    # ---- ③ 渲染子图（递归传 _allow_split=False 防二次拆图） ---------------------
    sub_streams: list[BytesIO] = []
    part_fonts: list[int] = []
    part_roots: list[dict] = []   # 与 part_fonts 同序：第⑤步取「最差那张」的几何
    for k, grp in enumerate(groups, 1):
        part_root = {"label": f"{root_label}（第{k}/{g}部分）", "children": grp}
        part_px = _part_geometry(part_root)[2]
        try:
            s = _render_architecture_image_v2(part_root, _allow_split=False)
        except Exception:  # 子图渲染异常不应令整图失败
            s = None
        if s is not None:
            s.seek(0)
            sub_streams.append(s)
            part_fonts.append(part_px)
            part_roots.append(part_root)
    if len(sub_streams) < 2:
        return None
    try:
        sub_imgs = [Image.open(s).convert("RGB") for s in sub_streams]
    except Exception:
        return None

    # ---- ④ 用真实 PNG 尺寸复核（安全网）：预估一旦失守则宁可不拆 ----------------
    #      探针与落图尺寸理论上同源，但归一化/字体度量都可能让二者偏离；「拆图绝不
    #      变差」这条不变量必须靠**真实交付尺寸**成立，而不是靠预估自证。
    real_sizes = [(im.width / ARCH_RENDER_SCALE, im.height / ARCH_RENDER_SCALE)
                  for im in sub_imgs]
    delivered = review_delivered_mm(part_fonts, real_sizes)
    if delivered <= single_mm:
        logger.info("架构图拆图复核未通过（交付 %.2fmm ≤ 不拆 %.2fmm），回落单图",
                    delivered, single_mm)
        return None
    gap = int(_SPLIT_GAP_DESIGN_PX * ARCH_RENDER_SCALE)
    width = max(im.width for im in sub_imgs)
    height = sum(im.height for im in sub_imgs) + gap * (len(sub_imgs) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for im in sub_imgs:
        x = (width - im.width) // 2
        canvas.paste(im, (x, y))
        y += im.height + gap
    # 「绑定=高」一旦出现就说明本次交付又被 22cm 上限二次缩放（本函数已尽力避免，
    # 但仍要在日志里留痕，否则这条边又只能靠人工复测发现）。
    _wa, _ha = stitch_geometry(real_sizes, _SPLIT_GAP_DESIGN_PX)
    logger.info(
        "架构图拆图交付: 份数=%d 整图=%dx%dpx 缩放绑定=%s 最差纸面=%.2fmm（不拆=%.2fmm）",
        g, width, height, "高" if paper_height_binds(_wa, _ha) else "宽",
        delivered, single_mm,
    )

    # ---- ⑤ 告警：口径 = 你真的拿到什么（R62：拆完再报，数字才是交付值） --------
    #      用「最差那张子图」的几何 + 第④步**真实交付**毫米组同一条文案，判据仍是
    #      `compose_readability_warning` 那一份；`delivered_parts=g` 把「已经拆了」
    #      写进文案，同时保留「建议拆分」子串（test_arch_size_budget 的字面契约）。
    #      拆失败的路径早在第④步就 return None，告警留给单图路径打它那一条 ⇒
    #      两条出口各自恒为 1 条 warning。
    _min_mm = paper_font_min_mm()
    _worst_i = min(range(len(part_fonts)), key=lambda i: part_fonts[i])
    _pw, _ph, _peff, _pnv = _part_geometry(part_roots[_worst_i])
    _msg = compose_readability_warning(
        _pw, _peff, delivered, _min_mm, _pnv,
        architecture_over_budget(root),
        (arch_max_leaves(), arch_max_nodes()),
        delivered_parts=g,
    )
    if _msg:
        logger.warning(_msg)
    return _image_to_stream(canvas)


def _arch_counts(root: dict) -> tuple[int, int]:
    """统计 (叶子数, 节点总数)，复用 `_build_nodes` 单一遍历口径（环/超深按叶子截断）。

    非法根（None / 非 dict）直接返回 (0, 0)——统计是只读探针，绝不因脏数据抛异常。
    """
    if not isinstance(root, dict):
        return 0, 0
    nodes_pos = _build_nodes(root, False)
    return sum(1 for n in nodes_pos if not n["has_children"]), len(nodes_pos)


def architecture_leaf_count(root: dict) -> int:
    """架构树叶子节点数（环/超深数据按叶子截断，绝不抛）。"""
    leaves, _ = _arch_counts(root)
    return leaves


def architecture_node_count(root: dict) -> int:
    """架构树节点总数（口径同上）。"""
    _, nodes = _arch_counts(root)
    return nodes


def architecture_over_budget(root: dict) -> tuple[bool, int, int]:
    """返回 (是否超预算, 叶子数, 节点数)。超预算 = 叶子>上限 或 节点>上限。"""
    leaves, nodes = _arch_counts(root)
    over = leaves > arch_max_leaves() or nodes > arch_max_nodes()
    return over, leaves, nodes




def paper_font_mm(font_px: float, canvas_w: int, canvas_h: int = 0,
                  title_h: int = 0) -> float:
    """估算 DOCX 插入后图内文字的**纸面高度**（mm）。

    与 `routers/export.py:_fit_image_cm` 同口径：自然尺寸 = 像素 × 2.54 / DPI，
    再同时受「栏宽 16cm」「单图高 22cm」约束等比缩小。`canvas_h=0` 表示只看宽度。
    超采样倍数在分子分母同时出现、互相抵消 —— 所以结果与设计像素口径无关。
    """
    # R62 起委派公共层（同一判据不留两份）：双上限口径与 scale/DPI 全部由参数
    # 带下去，本模块的四个常量仍是唯一事实源，护栏逐例对账二者等价。
    return _common_paper_font_mm(
        font_px, canvas_w, canvas_h, title_h,
        scale=ARCH_RENDER_SCALE, dpi=ARCH_OUTPUT_DPI,
        col_cm=PAPER_COLUMN_WIDTH_CM, max_h_cm=PAPER_MAX_HEIGHT_CM,
    )


def resolve_paper_font_px(canvas_w: int, *, base: int, ceiling: int, min_mm: float,
                          canvas_h: int = 0, title_h: int = 0) -> int:
    """把字号抬到「纸面不低于 min_mm」，但**永不改小、永不越过 ceiling**。

    `min_mm` 是**必填关键字参数**，刻意不给默认值：默认值只能取「常量」或「环境」
    之一，取常量会让忘记传参的调用点对运维杀开关静默失明（本轮 A/B 前就踩到这个），
    取环境又会让本函数不再是纯函数。必填 = 忘传直接 TypeError。

    纯函数，三条不变量各有护栏：
      · 返回 ≥ base —— 窄图/默认字号已达标的图**逐字不变**（本轮零回归面的来源）；
      · 返回 ≤ ceiling —— 各版式 box 的物理容量上限，绝不放大到压出边框；
      · min_mm ≤ 0 → 直接返回 base（运维关闭态即回退旧字号）。
    """
    _min = min_mm
    if _min <= 0 or canvas_w <= 0:
        return base
    unit = paper_font_mm(1000.0, canvas_w, canvas_h, title_h)   # 每 1000px 字号换多少毫米
    if unit <= 0:
        return base
    want = int(math.ceil(_min * 1000.0 / unit))
    return int(min(max(want, base), ceiling))


def resolve_leaf_vertical_font_px(canvas_w: int, labels, *, min_mm: float,
                                  canvas_h: int = 0, title_h: int = 0) -> int:
    """竖排叶子字号：纸面下限之上再加一条硬约束 —— **放大不得新增省略号**。

    字号越大，「每列字数 × 可容纳列数」越小；不加这条约束，长标签的图会被自适应
    撑到 `…` 收口（宁可字小一点也必须看得全）。容量公式与真正绘制**共用**
    `_vertical_text_capacity`，不留两套口径。

    ⚠️ 估算取「字高 ≈ 字号」的工程近似（真实字高由字体度量决定，可能差 1px），
       故本函数只在「字号已到上限且仍不达标」时触发告警，不作为精确承诺。
       端到端「到底漏没漏字」由真实绘制护栏锁（tests R60 C 组），不靠本近似自证。
    """
    box_w = max(0, LEAF_VERTICAL_W - 2 * LEAF_VERTICAL_PAD_X)
    box_h = max(0, LEAF_VERTICAL_H - 2 * LEAF_VERTICAL_PAD_Y)
    longest = max((len(str(s or "").strip()) for s in labels), default=0)
    font = resolve_paper_font_px(canvas_w, base=LEAF_VERTICAL_FONT_SIZE,
                                 ceiling=LEAF_VERTICAL_FONT_MAX, min_mm=min_mm,
                                 canvas_h=canvas_h, title_h=title_h)
    while font > LEAF_VERTICAL_FONT_SIZE:
        _per_col, _max_cols, capacity, _rs, _cs = _vertical_text_capacity(
            box_w, box_h, max(12, font), LEAF_VERTICAL_CHAR_GAP)
        if longest <= capacity:
            break
        font -= 1
    return font


def _dims_for(depth: int, is_leaf: bool, vertical_mode: bool) -> tuple[int, int, bool]:
    """返回 (宽, 高, 是否竖排)。竖排只作用于「L3 及更深的叶子节点」。

    二级（depth==1）节点恒为横排：文档 §1 的优化对象是最底部那一层，
    把二级节点也画成竖条会让中间层失去可读的横向标题。
    """
    if vertical_mode and is_leaf and depth >= LEAF_VERTICAL_MIN_DEPTH:
        return LEAF_VERTICAL_W, LEAF_VERTICAL_H, True
    return NODE_W, NODE_H, False


def _gap_after(node_w: int) -> int:
    """兄弟节点间水平间距（按前一子树根节点宽度取，与 `_dims_for` 同源）。"""
    return LEAF_VERTICAL_GAP if node_w == LEAF_VERTICAL_W else H_GAP


def _build_nodes(root: dict, vertical_mode: bool) -> list[dict]:
    """DFS 布局一遍，返回节点记录列表（y 由 `_assign_rows_y` 统一赋值）。

    `vertical_mode=False` 时，所有节点的 x/y/w/h 与旧实现**逐值相同**（横排回退
    即零风险路径）；`True` 时仅叶子层换尺寸并收紧间距。
    """
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

    def _append(node: dict, nid: str, x: float, w: int, h: int, depth: int,
                has_children: bool, parent_id: str | None, vertical: bool) -> float:
        label = str(node.get("label") or node.get("name") or "").strip()
        nodes_pos.append({
            "id": nid,
            "label": label,
            "x": x,
            "y": 0,
            "w": w,
            "h": h,
            "depth": depth,
            "has_children": has_children,
            "parent_id": parent_id,
            "highlight": _is_highlighted(node),
            "vertical": vertical,
        })
        return x + w

    def _dfs(node: dict, depth: int, x_start: float, parent_id: str | None = None,
             visited: set | None = None) -> tuple[float, int]:
        """DFS，返回 (本子树右边界, 本子树根节点宽度)。

        右边界**不含**尾随间距 —— 由调用方用 `_gap_after()` 补，避免旧实现
        「返回值里混进一个 gap、父层再加一个 gap」的隐式约定（混合间距下会算错步距）。
        """
        if visited is None:
            visited = set()
        # ✅ BUG 修复（环检测形同虚设）：旧实现用 `_unique_id()` 产出的"全局唯一 ID"
        #    做 visited 判重 —— 而 _unique_id 保证任何两次调用返回的 ID 都不同，
        #    于是 `nid in visited` **恒为 False**，环检测从未生效。当树数据含环
        #    （子节点直接/间接引用祖先 dict）时 _dfs 无限递归，最终 RecursionError
        #    崩溃，架构图整张丢失（被 render_mermaid_to_bytes 的兜底 except 静默吞掉）。
        #    改用**对象身份** id(node) 判重：同一 dict 被重复访问即判定为环。
        #    同时加入深度上限，防御超深链导致的爆栈。
        if id(node) in visited or depth > _MAX_DEPTH:
            # 环 / 超深：按叶子截断（不登记 leaf_counter，与旧实现逐字一致）
            nid = _unique_id(node.get("id"))
            w, h, vertical = _dims_for(depth, True, vertical_mode)
            return _append(node, nid, x_start, w, h, depth, False, parent_id, vertical), w
        visited.add(id(node))
        nid = _unique_id(node.get("id"))
        leaf_counter[0] += 1

        children = _children_of(node)
        if not children:
            # 叶子节点：自己占用一个位置
            w, h, vertical = _dims_for(depth, True, vertical_mode)
            return _append(node, nid, x_start, w, h, depth, False, parent_id, vertical), w

        # 有子节点：先布局子节点，再居中放置自己
        cursor = x_start
        spans: list[tuple[float, float]] = []
        for child in children:
            end, child_root_w = _dfs(child, depth + 1, cursor, parent_id=nid, visited=visited)
            spans.append((cursor, end))
            cursor = end + _gap_after(child_root_w)

        w, h, vertical = _dims_for(depth, False, vertical_mode)
        children_center = (spans[0][0] + spans[-1][1]) / 2
        x = children_center - w / 2
        own_end = _append(node, nid, x, w, h, depth, True, parent_id, vertical)
        return max(spans[-1][1], own_end), w

    _dfs(root, 0, SIDE_START)

    # 归一化坐标（让最左侧为 0）
    if nodes_pos:
        min_x = min(n["x"] for n in nodes_pos)
        for n in nodes_pos:
            n["x"] -= min_x
    _assign_rows_y(nodes_pos)
    return nodes_pos


def _assign_rows_y(nodes_pos: list[dict]) -> None:
    """按层赋值 y：层高取该层节点的最大高度。

    竖排叶子远高于横排节点，若仍用旧公式 `depth * (固定层高 + V_GAP)`，
    下一层会直接压进竖条里。各层等高时本函数退化为旧公式
    `y = depth * (NODE_H + V_GAP) + TOP_PAD`，横排几何逐值不变。
    """
    if not nodes_pos:
        return
    row_h: dict[int, int] = {}
    for n in nodes_pos:
        row_h[n["depth"]] = max(row_h.get(n["depth"], 0), n["h"])
    y_by_depth: dict[int, int] = {}
    cursor = TOP_PAD
    for depth in sorted(row_h):
        y_by_depth[depth] = cursor
        cursor += row_h[depth] + V_GAP
    for n in nodes_pos:
        n["y"] = y_by_depth[n["depth"]]


def _vertical_candidates(nodes: list[dict]) -> int:
    """可竖排叶子数（depth ≥ LEAF_VERTICAL_MIN_DEPTH 且无子节点）—— 唯一口径。"""
    return sum(1 for n in nodes
               if not n["has_children"] and n["depth"] >= LEAF_VERTICAL_MIN_DEPTH)


def count_vertical_leaf_candidates(root: dict) -> int:
    """统计「可竖排叶子」数量（depth ≥ LEAF_VERTICAL_MIN_DEPTH 且无子节点）。

    刻意复用 `_build_nodes` 而不是另写一遍树遍历：环/超深截断、别名键、
    非法子项过滤的口径必须与真正出图时**完全一致**（见 `_children_of` 注释）。
    """
    return _vertical_candidates(_build_nodes(root, False))


def compose_readability_warning(canvas_w: int, eff_px: int, eff_mm: float,
                                min_mm: float, vertical_candidates: int,
                                budget: tuple[bool, int, int],
                                caps: tuple[int, int],
                                delivered_parts: int = 0) -> str:
    """把「纸面字号不足」与「规模超预算」两条诊断合并为**一条**告警文本。

    为什么合并：二者是同因一果 —— 树太宽/太大是**因**，DOCX 按 16cm 栏等比缩放后
    印不下是**果**。旧实现各打一条 warning，同一张超大图上用户会连看两条「请拆分」，
    读起来像两个独立故障（本仓与并行的规模守卫同轮落地后暴露的接线重叠）。

    为什么值得合并而不是删一条：两条各自握着对方没有的**唯一可操作量** ——
    纸面毫米数是「为什么非拆不可」的证据，预算数字是「拆成几张」的直接输入。
    合并后一条里两个都在。

    `delivered_parts >= 2`（自动拆图**已交付**）时在预算那半句后追加
    「（本轮已自动拆分为 N 张子图）」；默认 0 时整条文案与旧实现**逐字一致**，
    单图路径零改动，上面两个子串也仍然同时出现。

    必须守住的既有契约（他轮护栏的字面判据，勿改）：超预算分支里
    `架构图超出规模预算` 与 `建议拆分` 两个子串同时出现
    （test_arch_size_budget_20261010.py::TestBudgetRenderIntegration）。
    无问题时返回空串，由调用方决定是否落日志。
    """
    over, leaves, nodes = budget
    max_leaves, max_nodes = caps
    unreadable = min_mm > 0 and eff_mm + 1e-6 < min_mm
    if not unreadable and not over:
        return ""

    parts = []
    if unreadable:
        parts.append(
            "架构图纸面字号仅 %.2fmm（目标 ≥%.1fmm）：画布 %dpx 宽、可竖排叶子 %d 个，"
            "字号已到该版式上限 %dpx，DOCX 按 %.0fcm 栏等比缩放后画布越宽纸上越小"
            % (eff_mm, min_mm, canvas_w, vertical_candidates, eff_px, PAPER_COLUMN_WIDTH_CM)
        )
    if over:
        suggest = max(1, (leaves + max_leaves - 1) // max_leaves) if max_leaves > 0 else 1
        done = ("（本轮已自动拆分为 %d 张子图）" % delivered_parts
                if delivered_parts >= 2 else "")
        parts.append(
            "架构图超出规模预算（叶子 %d > %d 或 节点 %d > %d），建议拆分为约 %d 张子图%s"
            % (leaves, max_leaves, nodes, max_nodes, suggest, done)
        )
    if len(parts) == 2:
        parts[1] = "同时" + parts[1]
    elif unreadable:
        # 单讲字号不足时把「怎么办」补上（超预算分支自带「建议拆分为约 N 张子图」，无需重复）
        parts.append("请减少单张架构图的叶子数或拆分为多张图")
    return "；".join(parts) + "。本图仍按完整数据渲染，不做删减。"


def horizontal_canvas_width(root: dict) -> int:
    """横排版式下的画布宽（纸面字号换算用）。

    与 `_layout_architecture_tree` 的宽度公式同源（`max(x+w) + CANVAS_PAD_X`），
    只是强制 `vertical=False` —— 版式判定的两条分支必须量同一把尺。
    """
    nodes = _build_nodes(root, False)
    if not nodes:
        return 0
    return int(max(n["x"] + n["w"] for n in nodes) + CANVAS_PAD_X)


def resolve_leaf_vertical_mode(root: dict, *, min_mm: float | None = None,
                               auto_vertical: bool | None = None) -> bool:
    """自动判定本次出图是否启用叶子层竖排。

    两条**相互独立、可分别关闭**的判据（用关键字参数注入即可单独回归）：
      ① 数量规则（R59 · 文档 §5）：可竖排叶子数 > 阈值；
      ② 可读性规则（R60 · 收口 R59 §9 遗留项①）：横排时**字号已到 box 硬上限
         仍印不出 min_mm**。旧规则只看叶子数，漏掉「叶子少但排得宽」的形态
         ——6 叶 + 1 直属岗的横排图画布 1764px、纸面 1.36mm，比 16 叶竖排图还小。
    """
    if not leaf_vertical_enabled():
        return False
    _min = paper_font_min_mm() if min_mm is None else min_mm
    _auto = auto_vertical_enabled() if auto_vertical is None else auto_vertical
    nodes = _build_nodes(root, False)
    candidates = _vertical_candidates(nodes)
    if _min > 0 and _auto and candidates > 0:
        _w = int(max(n["x"] + n["w"] for n in nodes) + CANVAS_PAD_X)
        if paper_font_mm(NODE_FONT_MAX, _w) < _min:
            return True
    return candidates > vertical_leaf_threshold()


def _layout_architecture_tree(root: dict, vertical: bool | None = None,
                              ) -> tuple[list[dict], int, int]:
    """为架构树节点计算坐标（自顶向下、横向展开）。

    `vertical=None`（默认）按叶子数自动选版式；显式传 True/False 锁定版式
    （护栏测试、以及人工回退旧横排形态时用）。
    节点记录在旧字段基础上**加法式**新增 `vertical`（是否按竖条绘制）。
    """
    if vertical is None:
        vertical = resolve_leaf_vertical_mode(root)
    nodes_pos = _build_nodes(root, vertical)
    if not nodes_pos:
        return nodes_pos, 0, 0
    max_x = max((n["x"] + n["w"] for n in nodes_pos), default=0)
    max_y = max((n["y"] + n["h"] for n in nodes_pos), default=0)
    return nodes_pos, int(max_x + CANVAS_PAD_X), int(max_y + CANVAS_PAD_Y)

@_render_hq
def _render_architecture_image_v2(data: dict, *, _allow_split: bool = True) -> BytesIO | None:
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

    # ✅ 超宽自动拆图（R61 授权轮）：超预算时拆成多张子图竖向拼接为单张 PNG，
    #    缓存/预览/导出/PDF/图号全部零改动。递归渲染子图传 _allow_split=False 防二次拆图。
    if _allow_split and _arch_autosplit_enabled() and _should_split_arch(root):
        _stream = _render_architecture_split(root)
        if _stream is not None:
            return _stream

    nodes_pos, width, height = _layout_architecture_tree(root)
    if not nodes_pos:
        return None
    vertical_mode = any(n.get("vertical") for n in nodes_pos)
    logger.info(
        "[PIL v2渲染] 架构图渲染启动: 节点数=%d 画布=%dx%dpx 叶子版式=%s",
        len(nodes_pos),
        width,
        height,
        "竖排" if vertical_mode else "横排",
    )

    # 高度至少包含标题
    title_h = 56
    canvas_h = height      # 节点区高度；纸面换算要用「节点区 + 标题带」的总高
    height += title_h

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    scale = ARCH_RENDER_SCALE

    img = Image.new("RGB", (int(width * scale), int(height * scale)), "white")
    draw = ImageDraw.Draw(img)
    # ✅ 纸面可读性下限（R60）：字号随画布宽放大，否则会被「按 16cm 栏等比缩放」抹平。
    #    三条不变量：① 永不改小（窄图逐字不变）；② 不超过各版式 box 的容量上限；
    #    ③ 竖排叶子不得因放大而新增省略号（`resolve_leaf_vertical_font_px` 内含此约束）。
    _min_mm = paper_font_min_mm()
    _leaf_labels = [n["label"] for n in nodes_pos if n.get("vertical")]
    font_title_px = resolve_paper_font_px(width, base=TITLE_FONT_BASE, ceiling=TITLE_FONT_MAX,
                                          canvas_h=canvas_h, title_h=title_h, min_mm=_min_mm)
    font_node_px = resolve_paper_font_px(width, base=NODE_FONT_BASE, ceiling=NODE_FONT_MAX,
                                         canvas_h=canvas_h, title_h=title_h, min_mm=_min_mm)
    font_leaf_px = resolve_leaf_vertical_font_px(width, _leaf_labels, canvas_h=canvas_h,
                                                 title_h=title_h, min_mm=_min_mm)
    _eff_px = font_leaf_px if vertical_mode else font_node_px
    _eff_mm = paper_font_mm(_eff_px, width, canvas_h, title_h)
    # 单一告警出口（R60）：字号不足与规模超预算合并成一条，判据本身是纯函数
    # （`compose_readability_warning`），可脱离渲染路径直接变异验证。
    _msg = compose_readability_warning(width, _eff_px, _eff_mm, _min_mm,
                                       len(_leaf_labels), architecture_over_budget(root),
                                       (arch_max_leaves(), arch_max_nodes()))
    if _msg and _allow_split:
        # 只有「字号封顶仍印不清」或「超出规模预算」才到这里，普通图不进本分支 ⇒ 零噪音。
        # `_allow_split=False` 即子图递归渲染：告警由父级 `_render_architecture_split`
        # 统一发 1 条，避免 N 张子图各发一条（R60 断言「超预算只告警 1 次」）。
        logger.warning(_msg)


    font_title = _image_font(int(font_title_px * scale), bold=True)
    font_node = _image_font(int(font_node_px * scale), bold=True)
    font_leaf_v = _image_font(int(font_leaf_px * scale), bold=True)

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
    #    连线的两端都按「父底边中点 → 子顶边中点」从节点自身宽高推导，
    #    因此叶子换成窄高竖条后无需另写分支，对齐关系自动成立。
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
    pad_x = int(LEAF_VERTICAL_PAD_X * scale)
    pad_y = int(LEAF_VERTICAL_PAD_Y * scale)
    char_gap = int(LEAF_VERTICAL_CHAR_GAP * scale)
    for n in nodes_pos:
        depth_idx = min(n["depth"], len(depth_colors) - 1)
        if n.get("highlight") and n["depth"] > 0:
            fill_c, text_c = HIGHLIGHT_FILL, "#1F3864"
        else:
            fill_c, text_c = depth_colors[depth_idx]
        x, y, w, h = n["x"], n["y"] + title_h, n["w"], n["h"]
        draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=fill_c)
        if n.get("vertical"):
            # 叶子竖排：文字自上而下、列自右向左，汉字保持直立
            _draw_text_vertical_center(
                draw,
                (x + pad_x, y + pad_y, x + w - pad_x, y + h - pad_y),
                n["label"],
                font_leaf_v,
                text_c,
                char_gap=char_gap,
            )
        else:
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
        "[PIL v2渲染] 架构图渲染完成: 节点=%d 尺寸=%dKB DPI=%g 叶子版式=%s "
        "字号(标题/节点/叶子)=%d/%d/%dpx 纸面≈%.2fmm",
        len(nodes_pos),
        size_kb,
        ARCH_OUTPUT_DPI,
        "竖排" if vertical_mode else "横排",
        font_title_px,
        font_node_px,
        font_leaf_px,
        _eff_mm,
    )
    return result_stream
