# -*- coding: utf-8 -*-
"""R60 · 架构图规模预算守卫护栏（2026-10-10）。

来源：R59 §9 遗留项①「限制单张架构图规模（AI 侧约束叶子数或超宽拆图）」的安全切片。
AI 侧「单图节点 4~16，超则拆多张」约束已接线（content_checkpoint.py:1129，DLV-04），
但服务端此前**零检测**——本文件锁死「配置化上限 + 纯函数计数 + 超限结构化告警」，
不拆分、不丢数据（拆图需改 get_or_render 契约，跨 7 类图表高风险，列为独立授权轮）。

覆盖分组：
  A 配置化上限：env 阈值、默认、非法/负值回落默认（绝不抛）
  B 纯函数计数：深树 / 混合树 / 环安全 / 超深安全（复用 _build_nodes 单一口径）
  C 超预算判定：叶子超、节点超、皆不超、双口径独立
  D 渲染集成：超限时结构化告警发出 + 图仍按完整数据产出（不丢数据）
  E 字节体检：源文件 CRLF 未被文本工具改坏 + 新增符号 parity 锁
"""
import ast
import os
import py_compile
from pathlib import Path

import app.services.ai.mermaid_architecture as ma
import pytest

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE.parent / "app" / "services" / "ai" / "mermaid_architecture.py"


# ------------------------------------------------------------------ 造树工具
def _deep_leaf_tree(leaf_count: int, group_size: int = 3) -> dict:
    """根 → 若干二级 → 叶子，叶子总数恰为 leaf_count（全部落在 depth=2）。"""
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
    """深度混合树：depth=1 直属叶子 + depth=2 叶子/内部节点 + depth=3 叶子。"""
    return {"label": "质量管控体系", "children": [
        {"label": "直属专员"},
        {"label": "部门A", "children": [{"label": f"A{i}组"} for i in range(4)]},
        {"label": "部门B", "children": [{"label": f"B{i}组"} for i in range(4)]},
        {"label": "部门C", "children": [
            {"label": "C分部", "children": [{"label": "C1组"}, {"label": "C2组"}]},
        ]},
    ]}


def _set_env(key: str, value: str | None):
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


# ==========================================================================
# A · 配置化上限
# ==========================================================================
class TestBudgetConfig:

    def test_defaults_without_env(self):
        _set_env("ARCH_MAX_LEAVES", None)
        _set_env("ARCH_MAX_NODES", None)
        assert ma.arch_max_leaves() == 24
        assert ma.arch_max_nodes() == 60

    @pytest.mark.parametrize("raw,expected", [
        ("1", 1), ("8", 8), ("100", 100),
        ("0", 24), ("-5", 24), ("abc", 24), ("", 24), ("  12  ", 12),
    ])
    def test_leaf_threshold_env_and_fallback(self, raw, expected):
        _set_env("ARCH_MAX_LEAVES", raw)
        try:
            assert ma.arch_max_leaves() == expected
        finally:
            _set_env("ARCH_MAX_LEAVES", None)

    @pytest.mark.parametrize("raw,expected", [
        ("1", 1), ("80", 80), ("0", 60), ("-1", 60), ("x", 60),
    ])
    def test_node_threshold_env_and_fallback(self, raw, expected):
        _set_env("ARCH_MAX_NODES", raw)
        try:
            assert ma.arch_max_nodes() == expected
        finally:
            _set_env("ARCH_MAX_NODES", None)


# ==========================================================================
# B · 纯函数计数（复用 _build_nodes 单一遍历口径）
# ==========================================================================
class TestBudgetCounting:

    def test_deep_leaf_tree_count(self):
        # 10 组 × 3 叶 = 30 叶；节点 = 1 根 + 10 组 + 30 叶 = 41
        t = _deep_leaf_tree(30, group_size=3)
        assert ma.architecture_leaf_count(t) == 30
        assert ma.architecture_node_count(t) == 41

    def test_mixed_depth_tree_count(self):
        t = _mixed_depth_tree()
        # 直属专员(1) + A1..4(4) + B1..4(4) + C1/C2(2) = 11 叶
        # 节点 = 1 根 + 4 一级 + 4(A) + 4(B) + 1(C分部) + 2(C1/C2) = 16
        assert ma.architecture_leaf_count(t) == 11
        assert ma.architecture_node_count(t) == 16

    def test_cycle_is_safe_not_infinite(self):
        # 自引用环：按叶子截断，绝不无限递归
        ring = {"label": "a"}
        ring["children"] = [ring]
        leaves = ma.architecture_leaf_count(ring)
        nodes = ma.architecture_node_count(ring)
        assert leaves == 1                       # 环被截断为一个叶子
        # 与真正出图节点数完全一致（单一口径）：根 + 截断叶 = 2
        assert nodes == len(ma._layout_architecture_tree(ring)[0])

    def test_oversize_chain_is_safe(self):
        # 制造一条极深链（> _MAX_DEPTH），必须安全返回而非爆栈
        node = {"label": "leaf0"}
        head = node
        for i in range(200):
            nxt = {"label": f"leaf{i + 1}"}
            node["children"] = [nxt]
            node = nxt
        # 链只有 1 个叶子（末端），不应抛异常
        assert ma.architecture_leaf_count(head) == 1

    def test_non_dict_root(self):
        assert ma.architecture_leaf_count(None) == 0
        assert ma.architecture_node_count("not a dict") == 0

    def test_count_matches_layout_node_count(self):
        # 计数口径必须与真正出图的节点数完全一致（防「计数说 N、实画 M」分叉）
        for t in (_deep_leaf_tree(9), _mixed_depth_tree()):
            nodes, _w, _h = ma._layout_architecture_tree(t)
            assert ma.architecture_node_count(t) == len(nodes)
            assert ma.architecture_leaf_count(t) == sum(
                1 for n in nodes if not n["has_children"])


# ==========================================================================
# C · 超预算判定
# ==========================================================================
class TestBudgetOver:

    def test_within_budget_by_leaves(self):
        _set_env("ARCH_MAX_LEAVES", None)
        _set_env("ARCH_MAX_NODES", None)
        # 默认叶上限 24：9 叶 + 13 节点 → 不超
        t = _deep_leaf_tree(9)
        over, leaves, nodes = ma.architecture_over_budget(t)
        assert over is False
        assert leaves == 9 and nodes == 13

    def test_over_by_leaves(self):
        _set_env("ARCH_MAX_LEAVES", "8")
        try:
            t = _deep_leaf_tree(9)
            over, leaves, _ = ma.architecture_over_budget(t)
            assert over is True and leaves == 9
        finally:
            _set_env("ARCH_MAX_LEAVES", None)

    def test_over_by_nodes_only(self):
        # 叶子少但节点多（深而窄的树）：叶上限放宽、节点上限收紧 → 仅节点触发
        _set_env("ARCH_MAX_LEAVES", "100")
        _set_env("ARCH_MAX_NODES", "5")
        try:
            # 2 组各 1 叶 = 2 叶，但节点 = 1+2+2 = 5，加一个直属叶变 6 → 超
            t = {"label": "根", "children": [
                {"label": "a", "children": [{"label": "a1"}]},
                {"label": "b", "children": [{"label": "b1"}]},
                {"label": "c"},
            ]}
            # 叶子 3，节点 6 > 5 → 超（且叶不超）
            over, leaves, nodes = ma.architecture_over_budget(t)
            assert over is True and leaves == 3 and nodes == 6
        finally:
            _set_env("ARCH_MAX_LEAVES", None)
            _set_env("ARCH_MAX_NODES", None)

    def test_threshold_flip_is_deterministic(self):
        # A/B 代理：同一棵树，阈值跨过叶子数边界时判定必须翻转（双向）
        t = _deep_leaf_tree(10)
        _set_env("ARCH_MAX_LEAVES", "9")
        over_low = ma.architecture_over_budget(t)[0]
        _set_env("ARCH_MAX_LEAVES", "11")
        over_high = ma.architecture_over_budget(t)[0]
        _set_env("ARCH_MAX_LEAVES", None)
        assert over_low is True and over_high is False


# ==========================================================================
# D · 渲染集成：超限告警 + 不丢数据
# ==========================================================================
class TestBudgetRenderIntegration:

    def test_over_budget_emits_warning_and_still_renders(self, caplog):
        import logging
        _set_env("ARCH_MAX_LEAVES", "5")
        try:
            caplog.set_level(logging.WARNING, logger="app.services.ai.mermaid_architecture")
            big = _deep_leaf_tree(30)  # 30 叶 > 5
            stream = ma._render_architecture_image_v2(big)
            # 不丢数据：仍按完整树渲染出图
            assert stream is not None
            assert len(stream.getvalue()) > 0
            # 超限告警发出（含拆分建议句式）
            assert "架构图超出规模预算" in caplog.text
            assert "建议拆分" in caplog.text
        finally:
            _set_env("ARCH_MAX_LEAVES", None)

    def test_within_budget_no_budget_warning(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="app.services.ai.mermaid_architecture")
        small = _deep_leaf_tree(6)  # 默认上限 24，不超
        stream = ma._render_architecture_image_v2(small)
        assert stream is not None
        # 默认阈值下小图不应触发预算告警
        assert "架构图超出规模预算" not in caplog.text


# ==========================================================================
# E · 字节体检 + 符号 parity 锁
# ==========================================================================
class TestBudgetByteIntegrity:

    def test_no_lone_cr_or_bare_lf(self):
        data = _TARGET.read_bytes()
        # 孤立 CR：去掉所有 \r\n 后不得残留 \r（CRLF 仓库的文本编辑坑）
        assert b"\r" not in data.replace(b"\r\n", b""), "出现孤立 CR（\\r\\n 之外）"
        # 裸 LF：去掉所有 \r\n 后不得残留 \n
        assert b"\n" not in data.replace(b"\r\n", b""), "出现裸 LF（行尾未统一为 CRLF）"

    def test_no_control_bytes(self):
        data = _TARGET.read_bytes()
        for bad in (b"\x00", b"\x07", b"\x0c"):  # NUL / BEL / FF
            assert bad not in data, f"出现控制字节 {bad!r}"

    def test_py_compile(self):
        py_compile.compile(str(_TARGET), doraise=True)

    def test_new_symbols_present(self):
        # parity 锁：新增公共符号必须都在，防止重构误删
        for sym in ("arch_max_leaves", "arch_max_nodes",
                    "architecture_leaf_count", "architecture_node_count",
                    "architecture_over_budget"):
            assert hasattr(ma, sym), f"缺失公共符号 {sym}"

    def test_ast_parses(self):
        src = _TARGET.read_text(encoding="utf-8")
        ast.parse(src)
