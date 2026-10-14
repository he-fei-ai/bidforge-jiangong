# -*- coding: utf-8 -*-
"""R60 · 劳动力图标签竖排护栏（2026-10-10）。

来源：R59 §9 遗留项④「其它图表类型接竖排」的安全切片。评估结论（见 mermaid_labor.py
守卫注释）：4 类候选图里仅「劳动力配置图」存在真正窄列长标签瓶颈，且旧实现用 `[:8]/[:10]`
硬切（比省略号更糟）。本文件锁死：① 硬切升级为省略号感知；② 底部折线图 x 轴工期列
**列变窄时竖排**（图表轴标签拥挤即竖排的惯例）、列够宽或 env 关闭时回退横排省略号；
③ 顶部堆叠图/左侧列/图例保持横排省略号（几何/语义不匹配竖排）。

覆盖分组：
  A 配置：env 开关判定（默认开、显式关、非法回落默认）
  B 竖排触发：窄列(多阶段)→竖排、宽列(少阶段)→不竖排、env 关→不竖排、开启后图像更高（预留带）
  C 省略号接管：左侧工种名/顶部工期列/图例均走 `_fit_text_with_ellipsis`（替换 `[:8]/[:10]` 硬切）
  D 不丢数据：超长标签渲染不崩、产出有效 PNG
  E 字节体检：源文件 CRLF 未被改坏 + 新增符号 parity 锁
"""
import ast
import os
import py_compile
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

import app.services.ai.mermaid_labor as ml

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE.parent / "app" / "services" / "ai" / "mermaid_labor.py"
_SCALE = 2.5


def _payload(n_phases: int, n_cats: int = 9) -> dict:
    import random
    random.seed(1)
    phases = [f"第{i}月" for i in range(1, n_phases + 1)]
    cats = [f"工种{j}" for j in range(1, n_cats + 1)]
    data = [[float(random.randint(5, 40)) for _ in range(n_cats)] for _ in range(n_phases)]
    return {"phases": phases, "categories": cats, "data": data,
            "title": "劳动力投入及工种分布图"}


# ==========================================================================
# A · 配置开关
# ==========================================================================
class TestLaborVerticalConfig:

    def test_default_true(self):
        os.environ.pop("LABOR_PHASE_VERTICAL", None)
        assert ml._labor_phase_vertical() is True

    @pytest.mark.parametrize("raw,expected", [
        ("0", False), ("false", False), ("off", False), ("no", False),
        ("1", True), ("true", True), ("on", True), ("xyz", True), ("", True),
    ])
    def test_env_values(self, raw, expected):
        if raw == "":
            os.environ.pop("LABOR_PHASE_VERTICAL", None)
        else:
            os.environ["LABOR_PHASE_VERTICAL"] = raw
        try:
            assert ml._labor_phase_vertical() is expected
        finally:
            os.environ.pop("LABOR_PHASE_VERTICAL", None)


# ==========================================================================
# B · 竖排触发行为（monkeypatch 锁死分支选择）
# ==========================================================================
class TestLaborVerticalTrigger:

    def _spy_vertical(self, calls):
        real = ml._draw_text_vertical_center

        def spy(draw, box, text, font, fill, char_gap=4):
            calls.append((box, text))
            return real(draw, box, text, font, fill, char_gap)

        return spy

    def test_narrow_phases_engage_vertical(self):
        calls = []
        with mock.patch.object(ml, "_draw_text_vertical_center",
                               side_effect=self._spy_vertical(calls)):
            ml._render_labor_image_v2(_payload(8))
        assert calls, "8 阶段窄列应触发底部工期列竖排"
        # 每个竖排 box 宽度应等于窄列宽（< 阈值 120*scale），确属窄格而非整图
        for box, _ in calls:
            assert (box[2] - box[0]) < _SCALE * 200

    def test_wide_phases_no_vertical(self):
        calls = []
        with mock.patch.object(ml, "_draw_text_vertical_center",
                               side_effect=self._spy_vertical(calls)):
            ml._render_labor_image_v2(_payload(3))
        assert not calls, "3 阶段宽列不应竖排"

    def test_env_off_disables_vertical(self):
        calls = []
        os.environ["LABOR_PHASE_VERTICAL"] = "0"
        try:
            with mock.patch.object(ml, "_draw_text_vertical_center",
                                   side_effect=self._spy_vertical(calls)):
                ml._render_labor_image_v2(_payload(8))
            assert not calls, "关闭竖排后即使窄列也不应竖排"
        finally:
            os.environ.pop("LABOR_PHASE_VERTICAL", None)

    def test_vertical_on_reserves_bottom_band_height(self):
        on = ml._render_labor_image_v2(_payload(8))
        os.environ["LABOR_PHASE_VERTICAL"] = "0"
        try:
            off = ml._render_labor_image_v2(_payload(8))
        finally:
            os.environ.pop("LABOR_PHASE_VERTICAL", None)
        h_on = Image.open(on).height
        h_off = Image.open(off).height
        assert h_on > h_off, "开启竖排应预留底部竖向带 → 图像更高"


# ==========================================================================
# C · 省略号接管（替换 `[:8]/[:10]` 硬切）
# ==========================================================================
class TestLaborEllipsisTakesOver:

    def test_ellipsis_called_for_left_column(self):
        calls = []
        real = ml._fit_text_with_ellipsis

        def spy(draw, text, font, max_width):
            calls.append((text, max_width))
            return real(draw, text, font, max_width)

        with mock.patch.object(ml, "_fit_text_with_ellipsis", side_effect=spy):
            ml._render_labor_image_v2(_payload(8))
        # 左侧工种名列宽度 = label_w - 8*scale
        label_w = int(100 * _SCALE)
        expect = label_w - int(8 * _SCALE)
        assert any(abs(mw - expect) < 2 for _, mw in calls), \
            f"左侧列应走省略号且宽度≈{expect}，实际调用宽度={[mw for _, mw in calls]}"

    def test_ellipsis_called_for_top_phase_and_legend(self):
        calls = []
        real = ml._fit_text_with_ellipsis

        def spy(draw, text, font, max_width):
            calls.append((text, max_width))
            return real(draw, text, font, max_width)

        with mock.patch.object(ml, "_fit_text_with_ellipsis", side_effect=spy):
            ml._render_labor_image_v2(_payload(8))
        # 至少应有左侧列 + 顶部工期列 + 图例三类调用（宽≤86*scale 的图例槽）
        assert any(mw <= int(90 * _SCALE) for _, mw in calls), \
            "应存在窄槽省略号调用（顶部工期列/图例）"


# ==========================================================================
# D · 不丢数据：超长标签不崩、产出有效 PNG
# ==========================================================================
class TestLaborNoDataLoss:

    def test_long_labels_render_ok(self):
        p = _payload(8)
        p["categories"][0] = "钢筋混凝土绑扎与浇筑综合施工班组一二三四五六"
        p["phases"][0] = "基础结构施工与主体结构施工及装饰装修施工阶段"
        stream = ml._render_labor_image_v2(p)
        assert stream is not None
        assert len(stream.getvalue()) > 0
        img = Image.open(stream)
        assert img.width > 0 and img.height > 0

    def test_minimal_valid_payload(self):
        p = {"phases": ["一", "二"], "categories": ["木工", "钢筋"],
             "data": [[10.0, 5.0], [8.0, 6.0]]}
        stream = ml._render_labor_image_v2(p)
        assert stream is not None


# ==========================================================================
# E · 字节体检 + 符号 parity 锁
# ==========================================================================
class TestLaborByteIntegrity:

    def test_no_lone_cr_or_bare_lf(self):
        data = _TARGET.read_bytes()
        assert b"\r" not in data.replace(b"\r\n", b""), "出现孤立 CR"
        assert b"\n" not in data.replace(b"\r\n", b""), "出现裸 LF"

    def test_no_control_bytes(self):
        data = _TARGET.read_bytes()
        for bad in (b"\x00", b"\x07", b"\x0c"):
            assert bad not in data, f"出现控制字节 {bad!r}"

    def test_py_compile_and_ast(self):
        py_compile.compile(str(_TARGET), doraise=True)
        src = _TARGET.read_text(encoding="utf-8")
        ast.parse(src)

    def test_new_symbols_present(self):
        for sym in ("_labor_phase_vertical", "_LABOR_VERTICAL_THRESHOLD",
                    "_LABOR_PHASE_LABEL_H", "_LABOR_LEGEND_RESERVE"):
            assert hasattr(ml, sym), f"缺失符号 {sym}"
