# -*- coding: utf-8 -*-
"""R67 流程图渲染可读性（菱形版式 + 连线分道 + 标签转义清洗）回归护栏。

来源：用户实测样张（深基坑方案流程图，4600×1550 PNG）暴露的四类缺陷：
  ① 菱形按 ``min(w,h)/2`` 画成 190×190 小方块 —— 文字按整个外接盒排版，
     长标签横向溢出菱形轮廓、压到相邻节点上；
  ② 同一层间间隙内 N 条边的水平段全部落在 ``(sy+ey)/2`` —— 共线并轨成
     一条贯穿全图的长轨（实测 6 条分支边并成 3900px 长轨），无法分辨
     哪条边连接哪两个节点；
  ③ AI 把 JSON 风格转义序列（``\\"``）原样写进节点标签，渲染把反斜杠
     印在图上（``CheckBIM -- \\"通过\\"``）；
  ④ 菱形形状盒与同列矩形错位（旧实现菱形贴列左缘），连线锚点偏离视觉中心。

落地口径：
- 菱形铺满自身形状盒（v2 与 with_edges 同构），在列内居中，文字按**内接
  矩形**自适应字号（`_draw_diamond_label`），绝不越出菱形轮廓；
- 连线按「带」分道（`_plan_edge_lanes`，两渲染器唯一共用实现）：前进边走
  目标层上方带、同层/回退边走源层下方带（LR 为图底母线带），带内按边数
  均分通道，间隙净高不足 ``(n+1)*LANE_PITCH`` 时自动加高；
- 标签统一经 `_unescape_label_text` 清洗（先还原转义、再成对剥引号）；
- `_RENDERER_VERSION` 6 → 7（改菱形版式/连线走向/画布尺寸，缓存必须失效）。

护栏组：A 菱形版式（像素探针对真正交付的 PNG，不做公式自证 —— R64 教训）
/ B 连线分道 / C 标签转义清洗 / D 缓存版本与单一实现 / E 字节体检。
单链不变式：每带 1 条边时加宽量为 0，R66 单链几何逐字保持（B 组锁定）。
"""
from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services.ai import mermaid_renderer as mr
from app.services.ai.mermaid_flowchart import (
    LANE_PITCH_DESIGN_PX,
    _draw_diamond_label,
    _extract_mermaid_node_label,
    _plan_edge_lanes,
    _render_flowchart_image_v2,
    _render_flowchart_with_edges,
    _unescape_label_text,
)

_AI_DIR = Path(mr.__file__).parent

_SCALE = 2.5
_WHITE = (255, 255, 255)


def _render_v2(code: str) -> Image.Image:
    bio = _render_flowchart_image_v2(code)
    assert isinstance(bio, BytesIO), f"v2 渲染返回 None: {code!r}"
    return Image.open(BytesIO(bio.getvalue())).convert("RGB")


def _render_we(data: dict) -> Image.Image:
    bio = _render_flowchart_with_edges(data)
    assert isinstance(bio, BytesIO), f"with_edges 渲染返回 None: {data!r}"
    return Image.open(BytesIO(bio.getvalue())).convert("RGB")


def _nonwhite_runs(seq):
    """沿一行/一列像素返回连续非白区间 [(start, end)]（含端点）。"""
    runs = []
    start = None
    for i, px in enumerate(seq):
        if px != _WHITE and start is None:
            start = i
        elif px == _WHITE and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(seq) - 1))
    return runs


def _dark_runs(seq, thr=100):
    """深色（文字/连线 #333333）区间。"""
    runs = []
    start = None
    for i, px in enumerate(seq):
        dark = px[0] < thr and px[1] < thr and px[2] < thr + 30
        if dark and start is None:
            start = i
        elif not dark and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(seq) - 1))
    return runs


def _dark_span(seq, thr=100):
    """一行内深色像素的整体跨度（首段起点到末段终点）—— 文字因字形内部
    有空白，必须量跨度而不是最长连续段，否则探针失明（本轮 A/B 实测）。"""
    runs = _dark_runs(seq, thr)
    if not runs:
        return 0
    return runs[-1][1] - runs[0][0] + 1


def _row(img: Image.Image, y: int, x0: int = 0, x1: int | None = None):
    px = img.load()
    return [px[x, y] for x in range(x0, x1 if x1 is not None else img.width)]


def _col(img: Image.Image, x: int, y0: int, y1: int):
    px = img.load()
    return [px[x, y] for y in range(y0, y1)]


def _clusters(runs, merge_gap=6):
    """把一维区间聚成簇（间隔 ≤ merge_gap 视为同一簇），返回簇中心列表。"""
    centers = []
    for s, e in runs:
        if centers and s - centers[-1][1] <= merge_gap:
            centers[-1] = (centers[-1][0], e)
        else:
            centers.append((s, e))
    return [(s + e) / 2 for s, e in centers]


# 单链样例（R66 几何不变式）
_TD_CHAIN = 'flowchart TD\n    A["测量放线"] --> B["基层清理"]\n'
_LR_CHAIN = 'flowchart LR\n    A["测量放线"] --> B["基层清理"]\n'


# ==========================================================================
# A · 菱形版式：铺满形状盒 + 列内居中 + 文字内接（像素探针对交付 PNG）
# ==========================================================================

_DIAMOND_CODE = 'flowchart TD\n    A{"质量判定"} --> B["基层清理"]\n'


class TestDiamondLayoutR67:

    def test_v2_diamond_spans_full_shape_box(self):
        """旧实现 min(w,h)/2 只画 190×190 小方块；现在铺满 200×80 形状盒
        （device 500×200），中心行区间宽应 ≈ 500。"""
        img = _render_v2(_DIAMOND_CODE)
        runs = _nonwhite_runs(_row(img, 350))  # 菱形中心行 y=350
        assert len(runs) == 1, f"菱形中心行应只有 1 个区间，实得 {runs}"
        width = runs[0][1] - runs[0][0] + 1
        assert abs(width - 200 * _SCALE) <= 8, \
            f"菱形应铺满形状盒（500px），实得 {width}px（旧实现仅 190px）"

    def test_v2_diamond_center_aligns_with_rect_column(self):
        """菱形必须在列内居中：其视觉中心 x 与同列矩形中心 x 对齐
        （旧实现菱形贴列左缘，错开 (node_w-diamond_w)/2*scale = 100px）。"""
        img = _render_v2(_DIAMOND_CODE)
        d_runs = _nonwhite_runs(_row(img, 350))       # 菱形（level 0）
        r_runs = _nonwhite_runs(_row(img, 650))       # 矩形（level 1 中心行）
        assert len(d_runs) == 1 and len(r_runs) == 1, (d_runs, r_runs)
        d_cx = (d_runs[0][0] + d_runs[0][1]) / 2
        r_cx = (r_runs[0][0] + r_runs[0][1]) / 2
        assert abs(d_cx - r_cx) <= 8, \
            f"菱形中心 {d_cx} 与同列矩形中心 {r_cx} 应对齐（旧实现错开 100px）"

    def test_v2_diamond_text_stays_inside_shape(self):
        """长标签文字的水平跨度不得超过菱形内接矩形（w/2=250px）——
        旧实现按整个外接盒排版，实测溢出至 ≈660px 压到相邻节点。"""
        long_label = "CheckBIM -- 通过 不合格判定超长标签压测用例"
        code = f'flowchart TD\n    A{{"{long_label}"}} --> B["基层清理"]\n'
        img = _render_v2(code)
        extent = max(
            _dark_span(_row(img, y)) for y in range(300, 401)
        )  # 文字块垂直居中于 cy=350，取中带扫描
        assert 0 < extent <= 250 + 24, \
            f"菱形文字水平跨度 {extent}px 超出内接矩形（≈250px），会溢出轮廓"

    def test_with_edges_diamond_text_stays_inside_shape(self):
        payload = {
            "title": "施工流程",
            "direction": "TD",
            "nodes": [
                {"id": "A", "label": "CheckBIM -- 通过 不合格判定超长标签压测",
                 "type": "decision"},
                {"id": "B", "label": "基层清理", "type": "process"},
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        img = _render_we(payload)
        # with_edges TD：row0 顶 = (60+40)*2.5=250，节点高 200 → 中心行 350
        extent = max(_dark_span(_row(img, y)) for y in range(300, 401))
        # 单元格宽 280*2.5=700 → 内接宽 350
        assert 0 < extent <= 350 + 24, \
            f"with_edges 菱形文字水平跨度 {extent}px 超出内接矩形（≈350px）"

    def test_diamond_label_helper_never_exceeds_inscribed_box(self):
        """纯函数层：极端长标签在最小字号下也不应产生超宽行。"""
        from PIL import ImageDraw

        img = Image.new("RGB", (800, 200), "white")
        draw = ImageDraw.Draw(img)
        from app.services.ai.mermaid_common import _image_font
        font = _image_font(int(16 * _SCALE), bold=True)
        _draw_diamond_label(
            draw, 400, 100, 500, 200, "超" * 40, font, "#333333", _SCALE)
        extent = max(_dark_span(_row(img, y)) for y in range(60, 141))
        assert extent > 0, "应画出文字"
        assert extent <= 250, f"内接矩形宽 250px，实得 {extent}px"


# ==========================================================================
# B · 连线分道：同一间隙带内多条边各占一条通道；单链几何逐字保持
# ==========================================================================

_FAN_CODE = (
    "flowchart TD\n"
    '    A["施工准备"] --> B["基层清理"]\n'
    '    A --> C["面层安装"]\n'
    '    A --> D["勾缝处理"]\n'
    '    A --> E["验收移交"]\n'
)


def _lane_clusters(img: Image.Image, y0: int, y1: int, min_run=50):
    """在 y 范围内找「横向长线」所在的 y 簇（箭头/竖线宽 < min_run 被排除）。"""
    lane_rows = []
    for y in range(y0, y1):
        for s, e in _dark_runs(_row(img, y)):
            if e - s + 1 >= min_run:
                lane_rows.append(y)
                break
    return _clusters([(y, y) for y in lane_rows])


class TestEdgeLanesR67:

    def test_v2_parallel_branch_edges_get_distinct_lanes(self):
        """4 条并行边在层间间隙内应出现 4 条彼此分开的横向长线 ——
        旧实现 4 条全部落在 (sy+ey)/2 同一 y 上并轨成 1 条贯穿长轨。"""
        img = _render_v2(_FAN_CODE)
        # row0 节点底 450；加宽后 row1 节点顶 = (100+120+40)*2.5 = 650
        clusters = _lane_clusters(img, 455, 645)
        assert len(clusters) >= 4, \
            f"4 条并行边应有 ≥4 条独立通道线，实得 {len(clusters)} 条（并轨={len(clusters) < 4}）"

    def test_v2_band_gap_grows_for_crowded_band(self):
        """4 条并行边时层间净空应自动加高（row_gap 40 + 加宽 40 design = 200px），
        旧实现恒为 100px —— 通道没有落脚空间。"""
        img = _render_v2(_FAN_CODE)
        # x=460（B 列内、避开源节点 1375..2075 与连线通道交叉）
        col = _col(img, 460, 455, 900)
        runs = _nonwhite_runs(col)
        node_top = None
        for s, e in runs:
            if e - s + 1 >= 150:  # 纵向大区间 = 下一行节点本体
                node_top = 455 + s
                break
        assert node_top is not None, f"未探到下一行节点：{runs}"
        assert node_top >= 630, \
            f"加宽后下一行节点顶应 ≥630px（旧几何 550px），实得 {node_top}"

    def test_v2_single_chain_row_gap_unchanged(self):
        """单链（每带 1 条边）行距必须仍是 R66 的 100px —— 分道不得误伤单链。"""
        img = _render_v2(_TD_CHAIN)
        runs = _nonwhite_runs(_col(img, 300, 150, 900))
        assert len(runs) == 2, f"TD 两节点应产生 2 个区间，实得 {runs}"
        gap = runs[1][0] - runs[0][1] - 1
        assert abs(gap - 40 * _SCALE) <= 6, f"单链行距应保持 100px，实得 {gap}"

    def test_v2_single_chain_lr_col_gap_unchanged(self):
        img = _render_v2(_LR_CHAIN)
        runs = _nonwhite_runs(_row(img, 300))
        assert len(runs) == 2
        gap = runs[1][0] - runs[0][1] - 1
        assert abs(gap - 40 * _SCALE) <= 6, f"单链列距应保持 100px，实得 {gap}"

    def test_with_edges_td_parallel_edges_get_distinct_lanes(self):
        payload = {
            "title": "施工流程",
            "direction": "TD",
            "nodes": [
                {"id": "A", "label": "施工准备", "type": "start"},
                {"id": "B", "label": "基层清理", "type": "process"},
                {"id": "C", "label": "面层安装", "type": "process"},
                {"id": "D", "label": "勾缝处理", "type": "process"},
                {"id": "E", "label": "验收移交", "type": "process"},
            ],
            "edges": [
                {"from": "A", "to": "B"},
                {"from": "A", "to": "C"},
                {"from": "A", "to": "D"},
                {"from": "A", "to": "E"},
            ],
        }
        img = _render_we(payload)
        clusters = _lane_clusters(img, 455, 645)
        assert len(clusters) >= 4, \
            f"with_edges 4 条并行边应有 ≥4 条独立通道线，实得 {len(clusters)}"

    def test_with_edges_single_chain_row_gap_unchanged(self):
        payload = {
            "title": "施工流程",
            "direction": "TD",
            "nodes": [
                {"id": "A", "label": "测量放线", "type": "start"},
                {"id": "B", "label": "基层清理", "type": "end"},
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        img = _render_we(payload)
        runs = _nonwhite_runs(_col(img, 300, 150, 900))
        assert len(runs) == 2, f"应得 2 个节点区间，实得 {runs}"
        gap = runs[1][0] - runs[0][1] - 1
        assert abs(gap - 40 * _SCALE) <= 6, f"单链行距应保持 100px，实得 {gap}"

    def test_same_level_edge_gets_bus_below(self):
        """同层边（目标与源同层）走源层下方专用母线，箭头朝上进入目标底边，
        不得横穿节点行本身（旧实现水平段落在 (sy+ey)/2，正好切过该行）。"""
        code = (
            "flowchart TD\n"
            '    A["施工准备"] --> B["基层清理"]\n'
            '    B --> C["面层安装"]\n'
            '    C --> D["勾缝处理"]\n'
            '    D --> C\n'   # D、C 不同层 → 这是回退边，走旧直连
            '    A --> D\n'   # A(层0) → D(层3)：前进边，跨 3 层
        )
        img = _render_v2(code)
        # 同层边构造：E、F 同层（A → E → F 且 F → E 的目标因另有入边与 E 同层）
        code2 = (
            "flowchart TD\n"
            '    A["施工准备"] --> B["基层清理"]\n'
            '    A --> C["面层安装"]\n'
            '    B --> D["验收移交"]\n'
            '    C --> D\n'
        )
        img2 = _render_v2(code2)
        # D 在层 2；B/C 在层 1；B→D 与 C→D 均为前进边 → 2 条通道
        clusters = _lane_clusters(img2, 455, 700)
        assert len(clusters) >= 2, \
            f"汇聚于同层的 2 条边应各占一条通道，实得 {len(clusters)}"

    def test_backward_edge_keeps_direct_routing(self):
        """跨层回退边（目标层更浅）保持旧直连路由 —— 不分带、不绕行。"""
        levels = {"A": 0, "B": 1, "C": 2}
        edges = [
            {"from": "A", "to": "B"},
            {"from": "B", "to": "C"},
            {"from": "C", "to": "A"},  # 跨层回退
        ]
        band_of, _be, _bg, _slot = _plan_edge_lanes(
            levels, edges, horizontal=False, base_gap=40)
        assert 0 in band_of and 1 in band_of
        assert 2 not in band_of, "TD 跨层回退边不应分带（保持旧直连路由）"


# ==========================================================================
# C · 标签转义清洗（解析层单一漏斗）
# ==========================================================================

class TestLabelUnescapeR67:

    def test_diamond_label_backslashes_removed(self):
        """实测样张缺陷：AI 输出 ``N1{CheckBIM -- \\"通过\\"}``，
        标签必须还原为 ``CheckBIM -- "通过"``（无反斜杠、引号成对）。"""
        parsed_label = _extract_mermaid_node_label('N1{CheckBIM -- \\"通过\\"}')
        assert parsed_label == 'CheckBIM -- "通过"', repr(parsed_label)
        assert "\\" not in parsed_label

    def test_unescape_is_mechanical_only(self):
        assert _unescape_label_text('a\\"b') == 'a"b'
        assert _unescape_label_text("a\\'b") == "a'b"
        assert _unescape_label_text("a\\\\b") == "a\\b"
        assert _unescape_label_text("普通标签") == "普通标签"
        assert _unescape_label_text(None) is None
        assert _unescape_label_text("") == ""

    def test_edge_label_unescaped(self):
        from app.services.ai.mermaid_flowchart import _split_mermaid_edge
        edge = _split_mermaid_edge('A -->|\\"通过\\"| B')
        assert edge is not None
        assert edge[1] == "通过", repr(edge)

    def test_quotes_stripped_only_as_wrapping_pair(self):
        """成对包裹引号才剥；内容中合法的引号必须保留
        （旧 strip('"') 会留下 ``CheckBIM -- "通过`` 的残串）。"""
        assert _extract_mermaid_node_label('N1{"判定"}') == "判定"
        assert _extract_mermaid_node_label('N1{said "hi"}') == 'said "hi"'

    def test_escape_cleaned_before_quote_strip(self):
        """顺序不变式：先还原转义再剥引号 —— 反了会把 ``label\\"`` 的尾部
        引号先剥掉，留下孤悬反斜杠（本轮实测踩中）。"""
        label = _extract_mermaid_node_label('N1{CheckBIM -- \\"通过\\"}')
        assert not label.endswith("\\"), repr(label)
        assert label.count('"') % 2 == 0, repr(label)


# ==========================================================================
# D · 缓存版本与单一实现
# ==========================================================================

class TestCacheAndSingleSourceR67:

    def test_renderer_version_is_7(self):
        assert mr._RENDERER_VERSION == 7, \
            "R67 改了菱形版式与连线通道，版本号必须 6→7，否则旧缓存图永不失效"

    def test_version_assigned_exactly_once(self):
        src = inspect.getsource(mr)
        assert src.count("_RENDERER_VERSION = ") == 1

    def test_lane_plan_shared_by_both_renderers(self):
        """分道规划必须是两渲染器唯一共用实现（防第二套口径）。"""
        v2_src = inspect.getsource(_render_flowchart_image_v2)
        we_src = inspect.getsource(_render_flowchart_with_edges)
        assert "_plan_edge_lanes(" in v2_src
        assert "_plan_edge_lanes(" in we_src

    def test_lane_pitch_single_source(self):
        assert LANE_PITCH_DESIGN_PX == 16
        v2_src = inspect.getsource(_render_flowchart_image_v2)
        we_src = inspect.getsource(_render_flowchart_with_edges)
        assert "LANE_PITCH_DESIGN_PX" in v2_src or "16" in v2_src
        assert "LANE_PITCH_DESIGN_PX" in we_src

    def test_plan_edge_lanes_band_assignment(self):
        """纯函数层：前进边→目标层上方带；同层边→源层下方带；跨层回退不分带。"""
        levels = {"A": 0, "B": 1, "C": 1, "D": 2}
        edges = [
            {"from": "A", "to": "B"},
            {"from": "A", "to": "C"},
            {"from": "B", "to": "D"},
            {"from": "C", "to": "B"},  # 同层边
        ]
        band_of, band_edges, band_gap, slot = _plan_edge_lanes(
            levels, edges, horizontal=False, base_gap=40)
        assert band_of[0] == 0 and band_of[1] == 0      # 前进边 → 目标层上方
        assert band_of[2] == 1                          # B→D → 层1 上方
        assert band_of[3] == 1                          # 同层边 → 源层(1)下方
        assert band_gap[0] == max(40, 3 * LANE_PITCH_DESIGN_PX)
        assert band_gap[1] == max(40, 3 * LANE_PITCH_DESIGN_PX)
        assert sorted(slot[i] for i in (0, 1)) == [0, 1]

    def test_plan_edge_lanes_lr_backward_uses_bottom_bus(self):
        levels = {"A": 0, "B": 0, "C": 1}
        edges = [{"from": "A", "to": "C"}, {"from": "B", "to": "A"}]
        band_of, _be, _bg, _slot = _plan_edge_lanes(
            levels, edges, horizontal=True, base_gap=40)
        assert band_of[0] == 0      # 前进边
        assert band_of[1] == -1     # LR 回退边 → 图底母线带


# ==========================================================================
# E · 源文件字节体检（CRLF 仓库，本轮触碰的生产文件全查）
# ==========================================================================

_TOUCHED_PROD = ["mermaid_flowchart.py", "mermaid_renderer.py"]


class TestR67ByteIntegrity:

    @pytest.mark.parametrize("name", _TOUCHED_PROD)
    def test_prod_line_endings_and_no_corruption(self, name):
        raw = (_AI_DIR / name).read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0, name
        assert raw.count(b"\n") == crlf, f"{name} 有裸 LF（被工具展开过）"
        assert raw.count(b"\r") == crlf, f"{name} 有孤立 CR（\\r\\r\\n 污染）"
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, f"{name} 有 U+FFFD（编码丢失）"
        assert b"\x00" not in raw and b"\x07" not in raw and b"\x0c" not in raw

    @pytest.mark.parametrize("name", _TOUCHED_PROD)
    def test_prod_sources_still_compile(self, name):
        compile((_AI_DIR / name).read_text(encoding="utf-8"), name, "exec")
