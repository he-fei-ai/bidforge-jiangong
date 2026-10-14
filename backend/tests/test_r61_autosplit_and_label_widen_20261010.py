"""R61 护栏（2026-10-10）：超宽自动拆图（架构图）+ 三图标签加宽（非竖排修复）。

背景与边界（勿改判据前请先读）：
  · A（架构图自动拆图）收口 R59 §9 遗留项①的真解：R60 只做「预算守卫 + 建议拆分」
    告警，本轮落地**真拆图**。方案刻意选「顶层分支分组 → 递归渲染子图 → 竖向拼接回
    单张 PNG」——契约（单 BytesIO / 单 PNG）不变，缓存/预览/导出/PDF/图号全部零改动。
  · B（三图标签加宽）收口 R59 §9 遗留项④的**非竖排**修复：对比表/时间轴/平面布置
    几何是「宽而矮」，竖排会破坏语义；正确修复 = 加宽标签容器预留宽度 + 省略号兜底
    （不换行、不竖排）。
"""
from __future__ import annotations

import io
import logging
import os
import py_compile
from pathlib import Path

import pytest
from PIL import Image

import app.services.ai.mermaid_architecture as ma
import app.services.ai.mermaid_comparison as mc
import app.services.ai.mermaid_layout as ml
import app.services.ai.mermaid_timeline as mt

SCALE = 2.5
_AI_DIR = Path(ma.__file__).resolve().parent
_TOUCHED = [
    "mermaid_architecture.py",
    "mermaid_comparison.py",
    "mermaid_timeline.py",
    "mermaid_layout.py",
]


def _set_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)


def _clean_arch_env():
    """清掉所有架构图相关 env，保证用例从默认值出发（互相不污染）。"""
    for k in (
        ma.ENV_MAX_LEAVES, ma.ENV_MAX_NODES, ma.ENV_AUTO_SPLIT,
        ma.ENV_SPLIT_MAX_LEAVES, ma.ENV_SPLIT_MAX_PARTS,
        ma.ENV_VERTICAL_SWITCH, ma.ENV_VERTICAL_THRESHOLD,
        ma.ENV_PAPER_FONT_MIN_MM, ma.ENV_AUTO_VERTICAL,
    ):
        _set_env(k, None)


def _branch(b: int, leaves: int) -> dict:
    return {
        "label": f"分支{b}",
        "children": [{"label": f"分支{b}叶子{i}", "children": []} for i in range(leaves)],
    }


def _root(nb: int, lp: int) -> dict:
    return {"label": "项目部", "children": [_branch(b, lp) for b in range(nb)]}


def _data(nb: int, lp: int) -> dict:
    return {"type": "architecture", "root": _root(nb, lp)}


# ==========================================================================
# A · 架构图超宽自动拆图：判定 / 几何 / 契约
# ==========================================================================
class TestArchAutoSplitDecision:

    def test_under_budget_no_split(self):
        """不超预算 ⇒ 判定为不拆（2 分支 × 3 叶 = 6 叶 < 默认上限 24）。"""
        _clean_arch_env()
        assert ma._should_split_arch(_root(2, 3)) is False

    def test_over_budget_multi_branch_splits(self):
        """超预算 + 顶层 ≥2 分支 ⇒ 触发拆图。"""
        _clean_arch_env()
        assert ma._should_split_arch(_root(4, 10)) is True

    def test_over_budget_single_branch_no_split(self):
        """仅 1 个顶层分支时即便超预算也不拆（拆单链无法缓解宽度，只会换来一张同宽图）。"""
        _clean_arch_env()
        one_branch = {"label": "项目部", "children": [_branch(0, 30)]}
        over, leaves, _nodes = ma.architecture_over_budget(one_branch)
        assert over is True, "前置：单分支 30 叶应超默认预算 24"
        assert ma._should_split_arch(one_branch) is False


class TestArchAutoSplitGeometry:
    """几何判据是「真的拆了」的硬证据：更窄 + 更高。"""

    @staticmethod
    def _render_pair():
        """返回 (拆图结果宽高, 关闭拆图后单图宽高)。"""
        _clean_arch_env()
        stream = ma._render_architecture_image_v2(_data(4, 10))
        assert stream is not None
        split_size = Image.open(stream).size
        _set_env(ma.ENV_AUTO_SPLIT, "0")
        stream2 = ma._render_architecture_image_v2(_data(4, 10))
        assert stream2 is not None
        single_size = Image.open(stream2).size
        _set_env(ma.ENV_AUTO_SPLIT, None)
        return split_size, single_size

    def test_split_is_narrower_and_taller(self):
        split_size, single_size = self._render_pair()
        sw, sh = split_size
        ow, oh = single_size
        assert sw < ow, f"拆图应更窄（子图只含部分分支）：{sw} vs 单图 {ow}"
        assert sh > oh, f"拆图应更高（子图竖向堆叠）：{sh} vs 单图 {oh}"

    def test_split_vertical_stack_matches_plan(self):
        """⚠️ R62 跨轮契约变更：原名 `test_split_vertical_stack_count`，硬锁「堆 4 张」。

        份数自 R62 起由**纸面毫米**决定（`plan_architecture_split`）：4 张竖向堆叠会
        顶穿 22cm 高上限，整张拼接图（含每张子图）被二次缩放，实测比 2 张**更小**
        （1.64mm vs 2.14mm）—— 把份数当契约锁死，只会让后人去改代码迁就旧测试。
        升级为三条更强而非更弱的判据：
          ① 交付高度 **逐像素等于**「决策分组各自渲染后之和 + 间距」（决策与几何
             之间不许有第二套口径，份数一漂移立刻红）；
          ② 份数 ≥ 2（本用例不空转，不接受「没拆也算过」）；
          ③ 决策必须「严格更好」才拆 —— 这正是 R62 的存在理由。
        毫米级端到端不变量在 `test_arch_split_legibility_r62_20261010.py` D 组
        （真实 PNG + 真实落墨字号），两处不重复。
        """
        _clean_arch_env()
        _set_env(ma.ENV_SPLIT_MAX_LEAVES, "8")
        try:
            from app.services.chart_validators import normalize_architecture_tree

            root = normalize_architecture_tree(_data(4, 10))
            assert root is not None, "夹具已失效"
            groups, mm, single = ma.plan_architecture_split(
                root, max_leaves=ma._arch_split_max_leaves(),
                max_parts=ma._arch_split_max_parts())
            parts = len(groups)
            assert parts >= 2, f"该配置本应真的拆图，实际份数={parts}（用例空转）"
            assert mm > single, f"拆图只在严格更好时发生：{mm} vs 不拆 {single}"

            stream = ma._render_architecture_image_v2(_data(4, 10))
            assert stream is not None
            height = Image.open(stream).height

            # 与渲染路径**同形态**构造每张子图（bare 节点根 + 「第k/g部分」标题），
            # 逐张量高后求和：拆图交付的高度必须正好等于决策分组的高度总和 + 间距
            root_label = str(root.get("label") or root.get("name") or "组织架构图")
            gap = int(ma._SPLIT_GAP_DESIGN_PX * ma.ARCH_RENDER_SCALE)
            part_h = []
            for k, grp in enumerate(groups, 1):
                ps = ma._render_architecture_image_v2(
                    {"label": f"{root_label}（第{k}/{parts}部分）", "children": grp},
                    _allow_split=False)
                assert ps is not None, f"第 {k}/{parts} 张子图渲染失败"
                part_h.append(Image.open(ps).height)
            expect = sum(part_h) + gap * (parts - 1)
            assert height == expect, (
                f"交付堆叠必须与决策份数逐张对齐（{parts} 张，各高 {part_h}）："
                f"应为 {expect}，实际 {height}"
            )
        finally:
            _set_env(ma.ENV_SPLIT_MAX_LEAVES, None)


class TestArchAutoSplitContract:
    """契约不变 = 下游零改动的前提，必须逐条锁死。"""

    def test_returns_single_png_stream(self):
        _clean_arch_env()
        stream = ma._render_architecture_image_v2(_data(4, 10))
        assert isinstance(stream, io.BytesIO), "必须仍是单个 BytesIO（单 PNG 契约）"
        assert stream.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_output_dpi_300(self):
        """拼接后仍须 300 DPI，否则导出侧 `_fit_image_cm` 换算出别的尺寸。"""
        _clean_arch_env()
        stream = ma._render_architecture_image_v2(_data(4, 10))
        dpi = Image.open(stream).info.get("dpi")
        assert dpi and len(dpi) == 2
        assert round(dpi[0]) == 300 and round(dpi[1]) == 300

    def test_over_budget_warning_still_emitted(self, caplog):
        """R60 预算护栏契约：拆图路径仍须产出「超出规模预算 / 建议拆分」句式。"""
        _clean_arch_env()
        caplog.set_level(logging.WARNING, logger="app.services.ai.mermaid_architecture")
        stream = ma._render_architecture_image_v2(_data(4, 10))
        assert stream is not None, "拆图后仍必须出图（不丢数据）"
        assert "架构图超出规模预算" in caplog.text
        assert "建议拆分" in caplog.text

    def test_sub_render_does_not_re_split(self):
        """递归渲染子图必须传 `_allow_split=False`，否则会二次拆图 → 无限递归风险。"""
        src = Path(ma.__file__).read_text(encoding="utf-8")
        assert "_allow_split=False" in src
        assert "_render_architecture_image_v2(part_root, _allow_split=False)" in src


# ==========================================================================
# B · 行为翻转锁（A/B）：证明护栏真的观测到开关，而非恒绿
# ==========================================================================
class TestArchAutoSplitBehaviorFlip:
    """M0 无变异对照 + M1/M2 定向翻转：翻转后几何必须跟着变。"""

    @staticmethod
    def _size(nb, lp):
        stream = ma._render_architecture_image_v2(_data(nb, lp))
        assert stream is not None
        return Image.open(stream).size

    def test_m0_control_over_budget_splits(self):
        _clean_arch_env()
        sw, sh = self._size(4, 10)
        _set_env(ma.ENV_AUTO_SPLIT, "0")
        ow, oh = self._size(4, 10)
        _set_env(ma.ENV_AUTO_SPLIT, None)
        # M0 对照：默认开启 ⇒ 更窄更高
        assert sw < ow and sh > oh

    def test_m1_switch_off_flips_result(self, monkeypatch):
        """关闭总开关 ⇒ 逐字回退单图（宽度回到整树宽度，不再堆叠）。"""
        _clean_arch_env()
        _set_env(ma.ENV_AUTO_SPLIT, "0")
        off_w, off_h = self._size(4, 10)
        monkeypatch.setattr(ma, "_arch_autosplit_enabled", lambda: True)
        on_w, on_h = self._size(4, 10)
        _set_env(ma.ENV_AUTO_SPLIT, None)
        assert off_w > on_w and off_h < on_h, (
            "env 关闭应回退单图（更宽更矮）；强制开启应拆图（更窄更高）"
        )

    def test_m2_group_size_flips_part_count(self, monkeypatch):
        """每组叶子上限放大到 100 ⇒ 全部分支落进 1 组 ⇒ 拼接近似等于单图。"""
        _clean_arch_env()
        _set_env(ma.ENV_SPLIT_MAX_LEAVES, "8")
        many_w, many_h = self._size(4, 10)
        monkeypatch.setattr(ma, "_arch_split_max_leaves", lambda: 100)
        one_w, one_h = self._size(4, 10)
        _set_env(ma.ENV_SPLIT_MAX_LEAVES, None)
        assert many_h > one_h, f"4 组堆叠({many_h}) 应高于 1 组({one_h})"
        assert many_w < one_w, "多组时每组更窄"

    def test_m3_parts_cap_respected(self, monkeypatch):
        """子图数量硬上限生效：把上限压到 1 ⇒ 合并为单组（不再堆叠成多张）。"""
        _clean_arch_env()
        monkeypatch.setattr(ma, "_arch_split_max_parts", lambda: 1)
        w1, h1 = self._size(4, 10)
        monkeypatch.setattr(ma, "_arch_split_max_parts", lambda: 12)
        w2, h2 = self._size(4, 10)
        assert h1 < h2, "上限 1 组时高度必须小于上限 12 组（多组堆叠更高）"


# ==========================================================================
# C · 三图标签加宽（非竖排修复）几何锁
# ==========================================================================
class TestChartLabelWiden:

    def test_comparison_label_slot_wider(self):
        """对比图标签槽 120→160、柱区 480→540 ⇒ 画布宽 = (75+850+100+1350) = 2450。"""
        stream = mc._render_comparison_image_v2(
            {"rows": [{"label": "超长方案名称用于验证加宽后显示更多字符", "value": 30},
                      {"label": "方案B", "value": 70}]}
        )
        assert stream is not None
        assert Image.open(stream).size == (2450, Image.open(stream).size[1])
        assert Image.open(stream).width == 2450

    def test_timeline_card_wider(self, monkeypatch):
        """时间轴卡片 min_node_w 180→230 ⇒ 单卡 575px（= 230×scale）不许回退。

        R64 语义更正（**不是放宽**）：本例真正要锁的是「单卡宽度」，整图宽度只是
        当时可用的代理量。R64 起 6 事件会按纸面毫米折成 2 行 × 3 卡（整图 2400×1250），
        代理量不再单调反映卡片宽度 ⇒ 换成两条各自独立的判据：
          ① 关掉折行开关 ⇒ 整图宽度仍是原字面量 `200+6*575+5*60 = 3950`（原口径逐字保留）；
          ② 开着折行 ⇒ 取「每行 4 卡」的形态（n=24 ⇒ 6 行 × 4 卡），此时 960 设计 px
             的**最小宽度地板**不再遮蔽，整图宽 = `200+4*575+3*60 = 2680`，575 仍可观测。
        """
        ms = [{"name": "很长的里程碑名称测试显示效果", "date": "2026-01-01",
               "desc": "说明", "group": f"阶段{i % 2}"} for i in range(6)]
        monkeypatch.setenv("TIMELINE_AUTO_WRAP", "0")
        stream = mt._render_timeline_image_v2({"milestones": ms})
        assert stream is not None
        assert Image.open(stream).width == 3950

        # ② 折行形态下：每行 4 卡 ⇒ 单卡 575px 依然直接决定整图宽度
        monkeypatch.delenv("TIMELINE_AUTO_WRAP", raising=False)
        assert mt.plan_timeline_rows(24) == (6, 4), "24 事件应折成 6 行 × 每行≤4 卡"
        s24 = mt._render_timeline_image_v2({"milestones": ms * 4})
        assert s24 is not None
        assert Image.open(s24).width == 200 + 4 * 575 + 3 * 60 == 2680

    def test_layout_zone_wider(self):
        """布局图区域块 zone_w 220→260 ⇒ 2 区域(2 列) total_w = 200+2*650+80 = 1580。"""
        stream = ml._render_layout_image_v2(
            {"zones": [{"name": "很长的区域名称测试显示效果", "category": "生产区"},
                       {"name": "生活区", "category": "生活区"}]}
        )
        assert stream is not None
        assert Image.open(stream).width == 1580

    def test_no_vertical_text_introduced(self):
        """三图修复必须是『非竖排』：不得引入竖排原语。"""
        for name in ("mermaid_comparison.py", "mermaid_timeline.py", "mermaid_layout.py"):
            src = (_AI_DIR / name).read_text(encoding="utf-8")
            assert "_draw_text_vertical_center" not in src, f"{name} 不应引入竖排原语"


# ==========================================================================
# D · 字节体检（CRLF 仓库的 .py 一律字节级改动，改完立即体检）
# ==========================================================================
class TestR61ByteIntegrity:

    @pytest.mark.parametrize("name", _TOUCHED)
    def test_file_bytes_clean(self, name):
        path = _AI_DIR / name
        raw = path.read_bytes()
        # 孤立 CR（去掉 CRLF 后不应再有 \r）
        assert b"\r" not in raw.replace(b"\r\n", b""), f"{name} 存在孤立 CR"
        # 裸 LF（去掉 CRLF 后不应再有 \n）
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{name} 存在裸 LF"
        # 控制字节 / 替换字符
        for bad, label in ((b"\x00", "NUL"), (b"\x07", "BEL"), (b"\x0c", "FF"),
                           (b"\xef\xbf\xbd", "U+FFFD")):
            assert bad not in raw, f"{name} 含 {label}"
        py_compile.compile(str(path), doraise=True, cfile=str(path) + "c")
        try:
            os.remove(str(path) + "c")
        except OSError:
            pass
