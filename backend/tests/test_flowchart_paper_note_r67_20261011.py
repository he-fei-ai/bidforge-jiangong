# -*- coding: utf-8 -*-
"""R67 续 · 流程图纸面毫米播报接线（2026-10-11）。

来源：R67 校准块「未落地④」—— R64 给五个 PIL 渲染器（时间轴 / 布局 / 对比 /
甘特 / 劳动力）各接了一行 ``paper_floor_note`` 纸面可读性播报，架构图刻意不接
（它有自己的预算告警，接了就是两条红灯）。**流程图是第七个 PIL 渲染器**，
R66/R67 才落地（统一样式 + 菱形版式 + 连线分道），R64 的五类清单里没有它，
于是流程图至今**没有任何纸面可读性播报** —— 一张 30 节点的深基坑流程图印到
纸上节点字可能只有 1.5mm，运维却看不到任何告警。

本轮收口：给流程图两个渲染函数（``_render_flowchart_image_v2`` 与
``_render_flowchart_with_edges``）各接一行 ``paper_floor_note``，主文字档取
节点字号（``font_node``），与 R64 五渲染器**同口径**（只写日志、不改像素、
不改返回值、不改任何分支）。

护栏组：A 两个渲染函数都播报 / B 每张图最多一条 warning / C 播报自洽 /
D AST 接线 / E primary 接线 / F 字节体检。
"""
from __future__ import annotations

import ast
import inspect
import logging
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services.ai import mermaid_common as mc
from app.services.ai import mermaid_flowchart as mfc

_AI_DIR = Path(inspect.getsourcefile(mc)).resolve().parent

_TD_CODE = 'flowchart TD\n    A["测量放线"] --> B["基层清理"]\n'
_TD_DATA = {
    "title": "施工流程",
    "direction": "TD",
    "nodes": [
        {"id": "A", "label": "测量放线", "type": "process"},
        {"id": "B", "label": "基层清理", "type": "process"},
    ],
    "edges": [{"from": "A", "to": "B"}],
}


def _png(stream) -> tuple[int, int]:
    """从 BytesIO 取真实交付 PNG 尺寸（px）。"""
    with Image.open(BytesIO(stream.getvalue() if hasattr(stream, "getvalue") else bytes(stream))) as im:
        return im.size


class TestBothRenderersEmit:

    def test_v2_emits_floor_note(self, caplog):
        with caplog.at_level(logging.INFO):
            out = mfc._render_flowchart_image_v2(_TD_CODE)
        assert out is not None, "v2 渲染失败（本轮改动打断了出图）"
        msgs = [r.getMessage() for r in caplog.records if "[纸面可读性]" in r.getMessage()]
        assert any("流程图" in m for m in msgs), f"v2 没有播报流程图：{msgs}"

    def test_with_edges_emits_floor_note(self, caplog):
        with caplog.at_level(logging.INFO):
            out = mfc._render_flowchart_with_edges(_TD_DATA)
        assert out is not None, "with_edges 渲染失败（本轮改动打断了出图）"
        msgs = [r.getMessage() for r in caplog.records if "[纸面可读性]" in r.getMessage()]
        assert any("流程图" in m for m in msgs), f"with_edges 没有播报流程图：{msgs}"


class TestAtMostOneWarning:

    def test_v2_at_most_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            mfc._render_flowchart_image_v2(_TD_CODE)
        assert sum(1 for r in caplog.records
                   if "[纸面可读性]" in r.getMessage()) <= 1

    def test_with_edges_at_most_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            mfc._render_flowchart_with_edges(_TD_DATA)
        assert sum(1 for r in caplog.records
                   if "[纸面可读性]" in r.getMessage()) <= 1


# ==========================================================================
# C · 播报自洽：日志 px 与 mm 用真实交付 PNG 尺寸复算
# ==========================================================================
class TestReportedMmMatchesPng:

    def test_v2_reported_px_matches_delivered_png(self, caplog):
        """日志里的画布 px 必须等于真正交付的 PNG 尺寸（不是预估）。"""
        with caplog.at_level(logging.INFO):
            stream = mfc._render_flowchart_image_v2(_TD_CODE)
        w, h = _png(stream)
        msg = next(r.getMessage() for r in caplog.records
                   if "[纸面可读性]" in r.getMessage())
        assert f"画布 {w}x{h}px" in msg, f"播报画布尺寸与交付 PNG 不符：{msg}"
        m = re.search(r"最差字号 (\d+)px → 纸面 ([\d.]+)mm", msg)
        assert m, f"info 行缺少 px→mm 口径：{msg}"
        worst_px, worst_mm = int(m.group(1)), float(m.group(2))
        assert worst_px > 0 and worst_mm > 0
        assert worst_mm == pytest.approx(
            round(mc.paper_font_mm(worst_px, w, h), 2), abs=0.01), msg
        m2 = re.search(r"主文字 → ([\d.]+)mm", msg)
        assert m2, f"info 行缺少主文字口径：{msg}"
        assert float(m2.group(1)) >= worst_mm - 1e-9, "主文字档不可能优于最差档"


# ==========================================================================
# D · AST 级：两个函数各调用 paper_floor_note 恰一次（全文件 2 次）
# ==========================================================================
class TestAstWiring:

    def test_both_functions_call_note_exactly_once(self):
        """全文件恰 2 次 paper_floor_note 调用（v2 + with_edges 各 1）。"""
        src = (_AI_DIR / "mermaid_flowchart.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        n = sum(1 for x in ast.walk(tree)
                if isinstance(x, ast.Call)
                and getattr(x.func, "id", "") == "paper_floor_note")
        assert n == 2, f"mermaid_flowchart.py 调用 paper_floor_note {n} 次（要求 2）"

    def test_import_present(self):
        src = (_AI_DIR / "mermaid_flowchart.py").read_text(encoding="utf-8")
        assert "paper_floor_note" in src, "未导入 paper_floor_note"


# ==========================================================================
# E · primary 接线：节点字号是主文字档
# ==========================================================================
class TestPrimaryWiring:

    def test_node_font_is_primary_not_caption(self, caplog, monkeypatch):
        """小字档（caption 13px）低于下限时 warning **不该**因为 caption 而响
        —— primary 锁的是节点字号（16px），只有节点字真的读不了才红灯。"""
        monkeypatch.setenv("CHART_PAPER_MIN_MM", "4.0")
        with caplog.at_level(logging.WARNING):
            mfc._render_flowchart_image_v2(_TD_CODE)
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert not warns, f"节点字号高于下限却告警（primary 未锁节点档）：{[r.getMessage() for r in warns]}"

    def test_warning_fires_when_node_below_floor(self, caplog, monkeypatch):
        """节点字号真的低于下限时必须告警（不是静默不报）。"""
        monkeypatch.setenv("CHART_PAPER_MIN_MM", "10.0")
        with caplog.at_level(logging.WARNING):
            mfc._render_flowchart_image_v2(_TD_CODE)
        warns = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.WARNING and "[纸面可读性]" in r.getMessage()]
        assert len(warns) == 1, f"节点低于下限应恰 1 条 warning（实得 {len(warns)}）"
        assert "流程图" in warns[0]


# ==========================================================================
# F · 字节体检：mermaid_flowchart.py 保持 CRLF / 无 BOM / 无 U+FFFD / 可编译
# ==========================================================================
class TestByteIntegrity:

    def test_flowchart_crlf_and_no_corruption(self):
        raw = (_AI_DIR / "mermaid_flowchart.py").read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0, "mermaid_flowchart.py 非 CRLF"
        assert raw.count(b"\n") == crlf, "有裸 LF（被工具展开过）"
        assert raw.count(b"\r") == crlf, "有孤立 CR（\\r\\r\\n 污染）"
        assert not raw.startswith(b"\xef\xbb\xbf"), "有 BOM"
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, "有 U+FFFD（编码丢失）"
        assert b"\x00" not in raw and b"\x07" not in raw and b"\x0c" not in raw

    def test_flowchart_still_compiles(self):
        compile((_AI_DIR / "mermaid_flowchart.py").read_text(encoding="utf-8"),
                "mermaid_flowchart.py", "exec")
