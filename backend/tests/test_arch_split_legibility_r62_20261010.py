# -*- coding: utf-8 -*-
"""R62 护栏：拆图纸面收益决策（收口 R60 §9「字号自适应 × 自动拆图」联合复测）。

一句话背景：R61 的超宽自动拆图拿宽度换高度，而 DOCX 插图同时受 16cm 宽 / 22cm 高
**两个**上限、取更紧者 —— 份数一多，高上限反超宽度上限，整张拼接图（含每张子图）被
二次缩放，7 组超预算用例实测 4 组**越拆越小**（−12% ~ −25%）。本轮把「拆几张」交给
纸面毫米决定，并把双上限口径下沉为所有 PIL 渲染器共用的单一事实源。

分组：
  A 公共纸面层纯函数（与**真实** `export._fit_image_cm` 逐例对账）
  B 单一事实源委派（架构图不再各写一份）
  C `plan_architecture_split` / 分组函数（纯决策，可脱离渲染路径变异）
  D 端到端不变量「拆图绝不变差」——毫米数由**真实 PNG + 真实落墨字号**算出，
    不用本轮闭式自证（闭式若失真，这组护栏仍会红）
  E 源文件字节体检 + 单告警点 AST（R60 契约回归）
"""
import ast
import io
import logging
import re
from pathlib import Path

import pytest
from PIL import Image

from app.routers.export import _fit_image_cm
from app.services.ai import mermaid_architecture as ma
from app.services.ai import mermaid_common as mc

SCALE = ma.ARCH_RENDER_SCALE
DPI = ma.ARCH_OUTPUT_DPI
GAP = ma._SPLIT_GAP_DESIGN_PX
_MIN_TARGET = ma.PAPER_FONT_MIN_MM
_ARCH_LOGGER = "app.services.ai.mermaid_architecture"


# ==========================================================================
# 公共夹具
# ==========================================================================
def _clean_env(monkeypatch):
    for k in (ma.ENV_AUTO_VERTICAL, ma.ENV_PAPER_FONT_MIN_MM, ma.ENV_VERTICAL_SWITCH,
              ma.ENV_VERTICAL_THRESHOLD, ma.ENV_MAX_LEAVES, ma.ENV_MAX_NODES,
              ma.ENV_AUTO_SPLIT, ma.ENV_SPLIT_MAX_LEAVES, ma.ENV_SPLIT_MAX_PARTS):
        monkeypatch.delenv(k, raising=False)


def _branch(i, n_leaf):
    return {"label": f"部{i}", "children": [{"label": f"岗位{i}-{j}"} for j in range(n_leaf)]}


def _root(nb, nl):
    return {"label": "项目组织机构", "children": [_branch(i, nl) for i in range(nb)]}


def _data(nb, nl):
    return {"type": "architecture", "root": _root(nb, nl)}


def render_and_watch(data, split, monkeypatch):
    """渲染一次，并同时抓到：交付 PNG 真实尺寸 + **真正落墨**的全部字号（设备px）。

    各张子图分别取字号，`Image.new` 天然把字号切成一段一段（每张图一次 new，
    且字号一定在 new 之后请求），所以最后一段属于拼接画布本身（无字号）。
    """
    fonts_per_image: list[list[int]] = []
    cur: list[int] = []
    orig_font, orig_new = ma._image_font, Image.new

    def spy_font(size, *a, **k):
        cur.append(int(size))
        return orig_font(size, *a, **k)

    def spy_new(mode, size, *a, **k):
        if cur:
            fonts_per_image.append(list(cur))
            cur.clear()
        return orig_new(mode, size, *a, **k)

    monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1" if split else "0")
    ma._image_font = spy_font
    Image.new = spy_new
    try:
        stream = ma._render_architecture_image_v2(data)
    finally:
        ma._image_font = orig_font
        Image.new = orig_new
    if cur:
        fonts_per_image.append(list(cur))
    if stream is None:
        return None, fonts_per_image
    with Image.open(io.BytesIO(stream.getvalue())) as im:
        size = im.size
        dpi = (im.info.get("dpi") or (300.0, 300.0))[0]
    return {"size": size, "dpi": dpi, "bytes": stream.getvalue(),
            "fonts_per_image": fonts_per_image}, fonts_per_image


def paper_mm(obs):
    """交付图里**最小**那档落墨字号在纸上的毫米数（按真实 `_fit_image_cm` 折算）。"""
    w_px, h_px = obs["size"]
    dpi = obs["dpi"]
    w_cm, _h_cm = _fit_image_cm(w_px, h_px, dpi)
    fit = w_cm * dpi / (w_px * 2.54)
    ink = [f for img in obs["fonts_per_image"] for f in img]
    assert ink, "没抓到任何字号，探针失效"
    return min(ink) * 2.54 / dpi * fit * 10


# ==========================================================================
# A · 公共纸面层（mermaid_common）—— 闭式必须与真实导出函数逐例相等
# ==========================================================================
class TestSharedPaperLayer:

    @pytest.mark.parametrize("w_px,h_px", [
        (2000, 400),                     # 宽绑
        (600, 900),                      # 双上限都不绑
        (5000, 6000),                    # 高绑（拆图堆叠后的典型形态）
        (int(1148 * SCALE), int(2412 * SCALE)),
        (int(2870 * SCALE), int(6030 * SCALE)),
        (int(2240 * SCALE), int(1196 * SCALE)),
    ])
    def test_paper_font_mm_equals_real_fit(self, w_px, h_px):
        font_px = 28.0
        w_cm, _h_cm = _fit_image_cm(w_px, h_px, DPI)
        fit = w_cm * DPI / (w_px * 2.54)
        truth = font_px * SCALE * 2.54 / DPI * fit * 10
        model = mc.paper_font_mm(font_px, w_px / SCALE, h_px / SCALE)
        # `_fit_image_cm` 返回值保留两位小数：高度绑定时反推 fit 会带 ~1e-4 偏差。
        # 真实缺陷（漏掉高上限）量级是 50%，1e-3 既容得下导出取整又抓得住它。
        assert model == pytest.approx(truth, rel=1e-3), (w_px, h_px, model, truth)

    def test_height_cap_must_be_modeled(self):
        """本轮靶心：只看宽度会得出「拆图更好」的错误结论（方向都会反）。

        堆叠高度由 16:22 比例**推**出来（3 倍余量，正是多张竖向拼接的形态）——
        不写死数字，纸面上限一改，用例不会悄悄失去意义。
        """
        w = 1148
        h = w * 3 * (mc.PAPER_MAX_HEIGHT_CM / mc.PAPER_COLUMN_WIDTH_CM)
        only_w = mc.paper_font_mm(28.0, w, 0)
        stacked = mc.paper_font_mm(28.0, w, h)
        assert only_w > _MIN_TARGET > stacked, (only_w, _MIN_TARGET, stacked)
        assert stacked == pytest.approx(only_w / 3, rel=1e-6), "3 倍余量应恰为 1/3"
        assert mc.paper_height_binds(w, h) is True

    @pytest.mark.parametrize("font_px,w,h", [(0, 10, 10), (-3, 10, 10), (10, 0, 10),
                                             (10, -5, 10)])
    def test_illegal_inputs_return_zero_not_raise(self, font_px, w, h):
        assert mc.paper_font_mm(font_px, w, h) == 0.0

    def test_negative_canvas_h_means_no_height_constraint(self):
        """`canvas_h<=0` 的契约是「不受高上限约束」，不是「整体作废」（代码 `if canvas_h > 0`）。
        若返回 0，调用方会把「高度未知」误判成「纸面 0mm」而永久否决拆图。"""
        assert mc.paper_font_mm(28, 1148, -1) == mc.paper_font_mm(28, 1148, 0) > 0

    def test_stitch_geometry_max_width_sum_height(self):
        assert mc.stitch_geometry([(100, 40), (160, 60), (120, 50)], gap=10) == (160, 170)
        assert mc.stitch_geometry([], 10) == (0, 0)
        assert mc.stitch_geometry([(80, 80)], 10) == (80, 80), "单张不应有间隙"

    def test_height_binds_boundary_is_exact(self):
        """16:22 临界比按「不算绑定」处理（宁可少判一次二次缩放）。"""
        assert mc.paper_height_binds(16 * 100, 22 * 100) is False
        assert mc.paper_height_binds(16 * 100, 22 * 100 + 1) is True
        assert mc.paper_height_binds(0, 10) is False
        assert mc.paper_height_binds(10, 0) is False

    def test_delivered_worst_mm_shared_fit_and_worst_part(self):
        """所有子图共享整图 fit；最差那张决定本图可读性 —— 不能取平均。"""
        sizes = [(1148, 600), (1148, 600)]
        even = mc.delivered_worst_mm([28, 28], sizes, GAP)
        one_small = mc.delivered_worst_mm([28, 15], sizes, GAP)
        assert one_small == pytest.approx(even * 15 / 28)
        assert mc.delivered_worst_mm([], sizes, GAP) == 0.0
        assert mc.delivered_worst_mm([28], [], GAP) == 0.0

    def test_more_parts_is_not_always_better(self):
        """「拆得越多越窄」的反例：曲线先升后降，R61 的隐含假设就错在这里。"""
        curve = {g: mc.delivered_worst_mm([28] * g, [(500 + 1600 // g, 600)] * g, GAP)
                 for g in (2, 3, 4, 6, 8)}
        best = max(curve, key=lambda g: curve[g])
        assert best in (2, 3, 4), f"最多份数竟最优，用例失去意义：{curve}"
        assert curve[8] < curve[best]


# ==========================================================================
# B · 单一事实源：架构图的同名函数必须委派公共层
# ==========================================================================
class TestSingleSourceOfTruth:

    def test_architecture_delegates_to_common(self):
        assert mc.paper_font_mm is ma._common_paper_font_mm

    def test_constants_match_common_defaults(self):
        assert (ma.PAPER_COLUMN_WIDTH_CM, ma.PAPER_MAX_HEIGHT_CM) == \
               (mc.PAPER_COLUMN_WIDTH_CM, mc.PAPER_MAX_HEIGHT_CM)
        assert (ma.ARCH_RENDER_SCALE, ma.ARCH_OUTPUT_DPI) == \
               (mc.PAPER_SCALE_DEFAULT, mc.PAPER_DPI_DEFAULT)

    @pytest.mark.parametrize("w,h", [(900, 400), (4424, 588), (1148, 2412), (2240, 1196)])
    def test_delegation_is_value_identical(self, w, h):
        for title_h in (0, 56):
            assert ma.paper_font_mm(28, w, h, title_h) == mc.paper_font_mm(
                28, w, h, title_h, scale=SCALE, dpi=DPI,
                col_cm=ma.PAPER_COLUMN_WIDTH_CM, max_h_cm=ma.PAPER_MAX_HEIGHT_CM)

    def test_export_caps_are_the_truth(self):
        """双上限数值必须与导出侧同源；export 改了这里就必须跟着改（否则闭式失真）。"""
        import inspect

        sig = inspect.signature(_fit_image_cm)
        assert sig.parameters["max_w"].default == mc.PAPER_COLUMN_WIDTH_CM, \
            "导出宽上限已变，本轮常量需同步"
        assert sig.parameters["max_h"].default == mc.PAPER_MAX_HEIGHT_CM, \
            "导出高上限已变，本轮闭式会系统性高估拆图收益"
        src = inspect.getsource(_fit_image_cm)
        # 两条夹紧分支都必须在位：只剩宽夹紧，就是 R62 之前的世界
        assert "max_w / w_cm" in src and "max_h / h_cm" in src, src

    def test_review_outlet_is_the_shared_criterion(self):
        """第④步复核出口必须**真的**委托共享判据，不能退化成常量或占位实现。

        D 组安全网用例是整个替换这个出口来证明「第④步在路径上」的，所以出口的
        **函数体**不在任何用例的视线里 —— A/B M11 实测「出口形同虚设（返回 inf，
        复核永远通过）」时 69 例全绿。本用例补的是另外半边，与那条互不冗余：
        那条锁「有没有调用」，这条锁「调用的是不是唯一那份判据」。
        """
        fonts = [52, 60, 75]
        sizes = [(3440.0, 1196.0), (1376.0, 2412.0), (824.0, 3020.0)]
        got = ma.review_delivered_mm(fonts, sizes)
        # ① 与共享判据逐值相等（换成 inf / 换成规划值都会在这里红）
        assert got == pytest.approx(mc.delivered_worst_mm(fonts, sizes, GAP), rel=1e-9)
        assert 0 < got < _MIN_TARGET, "夹具失真：这批子图本就应低于可读下限"
        # ② 间距参数真的进了判据：写死成 0 ⇒ 拼接高度算少 ⇒ 毫米被高估
        assert got < mc.delivered_worst_mm(fonts, sizes, 0), "拼接间距未被采信"
        # ③ 空输入的降级方向与共享判据一致（返回 0 ⇒ 复核判不出收益 ⇒ 回落单图）
        assert ma.review_delivered_mm([], []) == mc.delivered_worst_mm([], [], GAP) == 0.0


# ==========================================================================
# C · 拆图决策纯函数
# ==========================================================================
class TestSplitPlanner:

    def test_single_branch_over_budget_cannot_split(self):
        """1 个超重分支：顶层没有第二支可分 ⇒ 如实返回「拆无可拆」（R61 遗留①）。"""
        root = {"label": "项目部", "children": [_branch(0, 30)]}
        groups, mm, single = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        # 顶层不足 2 支走早退：连单图毫米都不测（调用方保持单图即可，无需知情）
        assert groups == [] and mm == 0.0 and single == 0.0

    def test_leaf_step_still_bounds_part_count(self):
        """R61 契约保留：每组叶子步长仍是**份数上界**（放到 100 ⇒ 贪心只剩 1 组 ⇒ 不拆）。"""
        root = _root(4, 10)
        big, _, _ = ma.plan_architecture_split(root, max_leaves=100, max_parts=12)
        assert big == []
        some, mm, single = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        assert len(some) >= 2 and mm > single

    def test_max_parts_cap_and_branch_cap(self):
        root = _root(8, 6)
        capped, _, _ = ma.plan_architecture_split(root, max_leaves=8, max_parts=1)
        assert capped == []
        wide, _, _ = ma.plan_architecture_split(root, max_leaves=8, max_parts=99)
        assert len(wide) <= len(root["children"])

    def test_every_part_within_size_budget(self):
        """拆完每张子图自己也必须过 R60 预算守卫，否则拆图没达成目的。"""
        for nb, nl in [(5, 8), (4, 10), (6, 6), (12, 4)]:
            groups, _, _ = ma.plan_architecture_split(_root(nb, nl), max_leaves=8,
                                                      max_parts=12)
            for grp in groups:
                leaves = sum(ma._arch_counts(b)[0] for b in grp)
                assert leaves <= ma.arch_max_leaves(), (nb, nl, leaves)

    def test_plan_is_strictly_better_than_not_splitting(self):
        """判据③：返回的方案毫米必须 > 不拆毫米；否则只能返回空组。"""
        for nb, nl in [(4, 7), (4, 10), (6, 6), (8, 6), (2, 15), (12, 4), (3, 20)]:
            groups, mm, single = ma.plan_architecture_split(_root(nb, nl), max_leaves=8,
                                                            max_parts=12)
            if not groups:
                continue
            assert len(groups) >= 2 and mm > single, (nb, nl, mm, single)

    def test_part_count_follows_legibility_not_group_step(self, monkeypatch):
        """份数由**纸面毫米**决定，不由 R61 的每组叶子步长决定（本轮的立身之本）。

        4枝×10叶在步长 8 下贪心必得 4 组，毫米最优是 2 组：4 张竖向堆叠顶穿 22cm
        高上限后被整体二次缩放。只锁「不变差」抓不到这种「变好了但没变到最好」，
        所以这里同时锁两件事：实际份数严格少于步长份数 + 被否决那档确实更差。
        """
        _clean_env(monkeypatch)
        br = _root(4, 10)["children"]
        greedy = len(ma._group_branches_by_budget(br, 8))
        assert greedy >= 3, f"夹具已失效：步长 8 下本该多组，实际 {greedy}"
        groups, mm, single = ma.plan_architecture_split(_root(4, 10),
                                                        max_leaves=8, max_parts=12)
        assert 2 <= len(groups) < greedy, (
            f"份数仍由分组步长说了算：{len(groups)} vs 贪心 {greedy}")
        assert mm > single
        over = [ma._part_geometry({"label": "", "children": g})
                for g in ma._group_branches_balanced(br, greedy)]
        worse = ma.delivered_worst_mm([o[2] for o in over],
                                       [(o[0], o[1]) for o in over],
                                       ma._SPLIT_GAP_DESIGN_PX)
        assert mm > worse, f"少拆不是拍脑袋：{greedy} 张本应更差（{worse} vs {mm}）"

    def test_budget_guard_is_consulted_by_planner(self, monkeypatch):
        """规模预算必须**真的参与**份数决策：压到比单个分支还小 ⇒ 拆无可拆。"""
        _clean_env(monkeypatch)
        root = _root(4, 10)
        base, _, _ = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        assert len(base) >= 2, "前提：默认预算下本该出多张"
        monkeypatch.setattr(ma, "arch_max_leaves", lambda: 4)   # 单分支已 10 叶
        denied, mm, _ = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        assert denied == [] and mm == 0.0, (
            "预算守卫失效：会交付超出规模预算的子图，拆图本身就没有意义")

    def test_groups_are_contiguous_order_preserving_lossless(self):
        root = _root(7, 5)
        groups, _, _ = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        flat = [id(b) for grp in groups for b in grp]
        assert flat == [id(b) for b in root["children"]], "分支必须不重不漏且保序"

    @pytest.mark.parametrize("nb", [2, 3, 4, 5, 8, 12])
    def test_balanced_partition_yields_exactly_parts(self, nb):
        br = [_branch(i, 4) for i in range(nb)]
        for parts in range(2, nb + 1):
            got = ma._group_branches_balanced(br, parts)
            assert len(got) == parts, (nb, parts, len(got))
            assert all(g for g in got)
            assert sum(len(g) for g in got) == nb

    def test_balanced_partition_never_empty_even_skewed(self):
        """可行性上界（后面的组各留一支）一旦被摘除，悬殊权重就会切出空组。

        等权分支下这条边界**碰巧**不会被越过 —— 所以只喂等权夹具的护栏是假覆盖
        （A/B M7 实测全绿）。[1,1,1,1,96] 切 3 份时，第二刀会跳到末尾 ⇒ 末组为空。
        """
        br = [_branch(i, 1) for i in range(4)] + [{"label": "肥",
                                                   "children": [{"label": f"z{i}"}
                                                                for i in range(96)]}]
        for parts in (2, 3, 4, 5):
            got = ma._group_branches_balanced(br, parts)
            assert len(got) == parts, (parts, [len(g) for g in got])
            assert all(g for g in got), (parts, [len(g) for g in got])
            assert sum(len(g) for g in got) == len(br)

    def test_balanced_partition_prefers_leaf_balance(self):
        br = [{"label": "肥", "children": [{"label": f"x{i}"} for i in range(20)]},
              {"label": "瘦", "children": [{"label": "y"}]}]
        got = ma._group_branches_balanced(br, 2)
        assert [len(g) for g in got] == [1, 1], "重量悬殊时不该把两支塞进同一组"

    def test_planner_is_pure_and_does_not_mutate_input(self):
        root = _root(6, 6)
        before = ast.dump(ast.parse(str(sorted(root.keys()))))
        a = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        b = ma.plan_architecture_split(root, max_leaves=8, max_parts=12)
        assert [len(g) for g in a[0]] == [len(g) for g in b[0]]
        assert a[1:] == b[1:]
        assert len(root["children"]) == 6
        assert ast.dump(ast.parse(str(sorted(root.keys())))) == before

    @pytest.mark.parametrize("bad", [None, [], "x", 7, {"label": "空"},
                                     {"children": "不是列表"},
                                     {"children": [None, 3]}])
    def test_illegal_root_never_raises(self, bad):
        groups, mm, single = ma.plan_architecture_split(bad, max_leaves=8, max_parts=12)
        assert groups == [] and mm == 0.0


# ==========================================================================
# D · 端到端不变量：拆图绝不变差（真实渲染 + 真实插入尺寸 + 真实落墨字号）
# ==========================================================================
OVER_BUDGET_CASES = [(4, 7), (4, 10), (6, 6), (8, 6), (2, 15), (12, 4), (3, 20), (5, 8)]


class TestSplitNeverWorseEndToEnd:

    @pytest.mark.parametrize("nb,nl", OVER_BUDGET_CASES)
    def test_delivered_not_worse_than_single(self, nb, nl, monkeypatch):
        """本轮唯一的硬指标：交付图最差落墨字号的纸面毫米 ≥ 不拆时的毫米。

        毫米数**不用本轮闭式**算：真实 PNG 尺寸 + 真实 `_fit_image_cm` + 真实请求过的
        最小字号，闭式失真时这组护栏同样会红（自证是最危险的护栏形态）。
        """
        _clean_env(monkeypatch)
        single, _ = render_and_watch(_data(nb, nl), split=False, monkeypatch=monkeypatch)
        split, _ = render_and_watch(_data(nb, nl), split=True, monkeypatch=monkeypatch)
        assert single and split
        mm_before = paper_mm(single)
        mm_after = paper_mm(split)
        assert mm_after + 1e-6 >= mm_before, (
            f"{nb}枝×{nl}叶：拆图后 {mm_after:.2f}mm 反而小于不拆 {mm_before:.2f}mm"
            f"（交付 {split['size'][0]}x{split['size'][1]}px）—— 22cm 高上限二次缩放又回来了")

    def test_multi_part_delivery_is_actually_multi(self, monkeypatch):
        """护栏不能靠「没拆」蒙过不变量：默认配置下确实出了多张子图。"""
        _clean_env(monkeypatch)
        single, _ = render_and_watch(_data(4, 10), split=False, monkeypatch=monkeypatch)
        split, _ = render_and_watch(_data(4, 10), split=True, monkeypatch=monkeypatch)
        # 每张子图各留一段字号（拼接画布自身不再取号），2 段 ⇒ 真的拼过
        assert len(split["fonts_per_image"]) >= 2, split["fonts_per_image"]
        assert split["size"][1] > single["size"][1], "多张堆叠必须比整树单图更高"

    def test_split_is_still_narrower_and_taller(self, monkeypatch):
        _clean_env(monkeypatch)
        single, _ = render_and_watch(_data(4, 10), split=False, monkeypatch=monkeypatch)
        split, _ = render_and_watch(_data(4, 10), split=True, monkeypatch=monkeypatch)
        sw, sh = split["size"]
        ow, oh = single["size"]
        assert sw < ow and sh > oh, f"拆图必须仍是更窄更高：{sw}x{sh} vs {ow}x{oh}"

    def test_under_budget_chart_byte_identical_across_switch(self, monkeypatch):
        """未超预算的图：开关两种状态必须**逐字节一致**（本轮零回归面）。"""
        _clean_env(monkeypatch)
        off, _ = render_and_watch(_data(2, 3), split=False, monkeypatch=monkeypatch)
        on, _ = render_and_watch(_data(2, 3), split=True, monkeypatch=monkeypatch)
        assert off["bytes"] == on["bytes"]
        assert off["size"] == on["size"]

    def test_review_safety_net_discards_a_bad_split(self, monkeypatch):
        """第④步复核真在路径上：交付毫米算不出收益时必须回落单图（逐字节）。

        patch 的必须是**复核专用出口** `review_delivered_mm` 而不是共享符号
        `delivered_worst_mm`：后者会在第①步就把方案否决，等于什么都没测到（A/B M6）。
        """
        _clean_env(monkeypatch)
        data = _data(4, 10)
        # ① 「不拆」的真实交付作为对照基准（不写死尺寸字面量，纸面口径一变就失效）
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "0")
        ref = ma._render_architecture_image_v2(data).getvalue()
        # ② 前提：默认配置下这个用例确实拆了，否则本探针什么都没测到
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        split_bytes = ma._render_architecture_image_v2(data).getvalue()
        assert split_bytes != ref, "前提失效：该配置本应触发拆图"
        assert (Image.open(io.BytesIO(split_bytes)).size[0]
                < Image.open(io.BytesIO(ref)).size[0]), "拆图应当更窄"
        # ③ 人为把复核毫米压成 0 ⇒ 第④步必须否决这次拆分，逐字节回到不拆
        # 只摘复核：决策侧必须仍然给出多张，否则本用例退化成「第①步否决」的重复
        kept = ma.plan_architecture_split(
            data["root"], max_leaves=ma._arch_split_max_leaves(),
            max_parts=ma._arch_split_max_parts())
        assert len(kept[0]) >= 2, "前提失效：决策本身已被误伤"
        monkeypatch.setattr(ma, "review_delivered_mm", lambda *a, **k: 0.0)
        got = ma._render_architecture_image_v2(data).getvalue()
        assert got == ref, "复核未通过应回落整树单图，而不是交付变差的多张拼接"

    def test_planner_decline_keeps_single_image(self, monkeypatch):
        _clean_env(monkeypatch)
        data = _data(4, 10)
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "0")
        ref_size = Image.open(io.BytesIO(
            ma._render_architecture_image_v2(data).getvalue())).size
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        monkeypatch.setattr(ma, "plan_architecture_split",
                            lambda root, **kw: ([], 0.0, 1.0))
        got = ma._render_architecture_image_v2(data)
        assert Image.open(io.BytesIO(got.getvalue())).size == ref_size

    def test_exactly_one_warning_and_cross_round_literals(self, monkeypatch, caplog):
        """R60 单告警点 + R61 字面契约：三处短语都在，且整张图只落 1 条 warning。"""
        _clean_env(monkeypatch)
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        caplog.set_level(logging.WARNING, logger=_ARCH_LOGGER)
        assert ma._render_architecture_image_v2(_data(4, 10)) is not None
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "架构图" in r.getMessage()]
        assert len(warns) == 1, [r.getMessage() for r in warns]
        msg = warns[0].getMessage()
        for lit in ("纸面字号仅", "架构图超出规模预算", "建议拆分"):
            assert lit in msg, msg

    def test_warning_after_split_quotes_delivered_state(self, monkeypatch, caplog):
        """拆图**交付成功**时，那条告警必须报「你真的拿到多少毫米」并写明已拆。

        收口 R61 遗留③。判据故意不自己套闭式：把第④步复核出口包一层间谍，
        用它**实际收到的**入参重算交付毫米，再与日志里的数字对账 —— 告警若还在
        用拆分前的整树口径（旧实现），两个数不可能相等（1.00mm vs 交付值）。
        """
        _clean_env(monkeypatch)
        data = _data(4, 10)
        real_outlet = ma.review_delivered_mm
        seen: dict = {}

        def spy(fonts_px, sizes):
            seen["fonts"] = list(fonts_px)
            seen["sizes"] = list(sizes)
            return real_outlet(fonts_px, sizes)

        monkeypatch.setattr(ma, "review_delivered_mm", spy)
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        caplog.set_level(logging.WARNING, logger=_ARCH_LOGGER)
        assert ma._render_architecture_image_v2(data) is not None

        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "架构图" in r.getMessage()]
        assert len(warns) == 1, warns
        assert len(seen.get("fonts", [])) >= 2, "前提失效：这次渲染根本没拆"
        expected = real_outlet(seen["fonts"], seen["sizes"])
        reported = re.search(r"纸面字号仅 ([\d.]+)mm", warns[0])
        assert reported, warns[0]
        assert float(reported.group(1)) == pytest.approx(expected, abs=5e-3), (
            f"告警报 {reported.group(1)}mm，实际交付 {expected:.2f}mm —— 文案还在用整树口径")
        assert "本轮已自动拆分为" in warns[0], warns[0]

    def test_veto_path_warns_exactly_once(self, monkeypatch, caplog):
        """拆图被否决回落单图时**只能有 1 条**告警，且不许出现「已自动拆分」。

        旧实现在第②步就打了一条整树告警，回落路径上单图渲染又打一条 —— 同一张图
        两条一模一样的 warning（R60 的单告警点在拆图分支里失守）。本用例与上一条
        合起来把「拆成功」和「拆失败」两条出口各自的告警口径都钉住。
        """
        _clean_env(monkeypatch)
        data = _data(4, 10)
        w, h, eff, _nv = ma._part_geometry(data["root"])
        whole_mm = ma.paper_font_mm(eff, w, h)          # 整树（不拆）口径
        monkeypatch.setattr(ma, "review_delivered_mm", lambda *a, **k: 0.0)
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        caplog.set_level(logging.WARNING, logger=_ARCH_LOGGER)
        assert ma._render_architecture_image_v2(data) is not None

        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "架构图" in r.getMessage()]
        assert len(warns) == 1, warns
        assert "本轮已自动拆分为" not in warns[0], warns[0]
        reported = re.search(r"纸面字号仅 ([\d.]+)mm", warns[0])
        assert reported and float(reported.group(1)) == pytest.approx(whole_mm, abs=5e-3), \
            warns[0]

    def test_delivery_log_reports_scaling_binding(self, monkeypatch, caplog):
        """「缩放绑定=高/宽」必须落进 info：这条边曾经只能靠人工复测发现。"""
        _clean_env(monkeypatch)
        monkeypatch.setenv(ma.ENV_AUTO_SPLIT, "1")
        caplog.set_level(logging.INFO, logger=_ARCH_LOGGER)
        assert ma._render_architecture_image_v2(_data(12, 4)) is not None
        info = " | ".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        assert "缩放绑定=" in info, info[-400:]
        assert "不拆" in info and "拆后" in info, "拆图决策日志要写清换来多少毫米"


class TestR62ByteIntegrity:

    FILES = ("mermaid_common.py", "mermaid_architecture.py")

    @pytest.mark.parametrize("name", FILES)
    def test_line_endings_and_no_replacement_char(self, name):
        raw = (Path(ma.__file__).parent / name).read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0
        assert raw.count(b"\n") == crlf, f"{name} 混入裸 LF（R58 的整文件 LF 化坑）"
        assert raw.count(b"\r") == crlf, f"{name} 混入孤立 CR（R57 的 \\r\\r\\n 坑）"
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, f"{name} 含 U+FFFD 替换符"

    @pytest.mark.parametrize("name", FILES)
    def test_compiles(self, name):
        import py_compile

        py_compile.compile(str(Path(ma.__file__).parent / name), doraise=True)

    def test_split_function_body_has_exactly_one_warning_call(self):
        """新增日志一律 info：warning 多一条就打破 R60「同一张图只告警 1 次」。"""
        assert _warning_calls(_func("_render_architecture_split")) == 1

    def test_render_function_body_still_has_one_warning_call(self):
        assert _warning_calls(_func("_render_architecture_image_v2")) == 1

    def test_no_bare_lf_only_block_leftovers(self):
        """本轮新增段落全部走 CRLF；顺带锁 `plan_architecture_split` 只有一处定义。"""
        src = Path(ma.__file__).read_text(encoding="utf-8")
        assert src.count("def plan_architecture_split") == 1
        assert src.count("def _part_geometry") == 1
        assert src.count("_SPLIT_GAP_DESIGN_PX") >= 3


def _func(name):
    tree = ast.parse(Path(ma.__file__).read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _warning_calls(fn):
    return sum(1 for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "warning"
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger")
