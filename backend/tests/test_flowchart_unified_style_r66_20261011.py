# -*- coding: utf-8 -*-
"""R66 流程图统一样式规范（竖向单链 + 横向单链双模式）回归护栏。

来源：《流程图统一样式规范：竖向单链 + 横向单链双模式》（2026-10-11 落地）。
落地决策（经确认适配本仓零依赖 PIL 管道，**不引入 Playwright**）：
- 配色：节点淡紫底 #f0edff + 紫边框 #a89aff + 深灰字/连线 #333333；
  强调边框（start/end/highlight）#6C4FB8 → #7C63D8（#a89aff 同系加深）。
- 单链几何（scale=2.5 ⇒ 像素）：纵向（TD/BT）节点 280×80 → 700×200px、行距
  40 → 100px；横向（LR/RL）节点 200×80 → 500×200px、列距 40 → 100px。
- 方向自动选择**只在提示词层**（≤8 横 / >8 竖）：校验器/渲染器缺省 TD 不变，
  显式 direction 一律原样生效 —— 本文件同时反向锁住这一点（F 组）。
- `_RENDERER_VERSION` 5 → 6：改的是外观与产物尺寸，不升版本 = 修复从未生效
  （R62/R64 纪律第三次应用）。

护栏组：A 配色（两渲染器 × 竖/横 四组合）/ B 单链几何（**像素探针对真正交付的
PNG**，不做公式自证 —— R64 教训）/ C 缓存版本 / D 提示词 parity /
E 源文件字节体检 / F 定向只在提示词层的反向锁。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services.ai import mermaid_renderer as mr
from app.services.ai.mermaid_flowchart import (
    _render_flowchart_image_v2,
    _render_flowchart_with_edges,
    flowchart_json_to_mermaid,
)
from app.services.chart_validators import normalize_flowchart_data
from app.services.ai.prompts._registry import get_default_prompt

_AI_DIR = Path(mr.__file__).parent

# ---- R66 新配色 / 被替换的 R65 旧配色（RGB 元组）----
NEW_FILL = (240, 237, 255)      # #f0edff
NEW_BORDER = (168, 154, 255)    # #a89aff
NEW_BORDER_DARK = (124, 99, 216)  # #7C63D8
DARK_GRAY = (51, 51, 51)        # #333333（不变）
OLD_FILL = (236, 236, 255)      # #ECECFF
OLD_BORDER = (147, 112, 219)    # #9370DB
OLD_BORDER_DARK = (108, 79, 184)  # #6C4FB8

_SCALE = 2.5
_WHITE = (255, 255, 255)


def _render_v2(code: str) -> Image.Image:
    bio = _render_flowchart_image_v2(code)
    assert isinstance(bio, BytesIO), f"v2 渲染返回 None: {code!r}"
    return Image.open(bio).convert("RGB")


def _render_we(data: dict) -> Image.Image:
    bio = _render_flowchart_with_edges(data)
    assert isinstance(bio, BytesIO), f"with_edges 渲染返回 None: {data!r}"
    return Image.open(bio).convert("RGB")


def _present(img: Image.Image) -> set[tuple[int, int, int]]:
    return {c for _n, c in img.getcolors(maxcolors=1 << 24) or []}


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


def _col(img: Image.Image, x: int, y0: int, y1: int):
    px = img.load()
    return [px[x, y] for y in range(y0, y1)]


def _row(img: Image.Image, y: int, x0: int, x1: int):
    px = img.load()
    return [px[x, y] for x in range(x0, x1)]


def _lens(runs):
    return [e - s + 1 for s, e in runs]


def _gap(runs):
    """相邻区间之间的空白像素数（runs 至少 2 段，取第一处）。"""
    return runs[1][0] - runs[0][1] - 1


# 单链样例：纯线性、无判定关键词（避开 _DECISION_KEYWORDS 启发式）
_TD_CODE = (
    "flowchart TD\n"
    '    A["测量放线"] --> B["基层清理"]\n'
)
_LR_CODE = (
    "flowchart LR\n"
    '    A["测量放线"] --> B["基层清理"]\n'
)


def _we_payload(direction: str) -> dict:
    return {
        "title": "施工流程",
        "direction": direction,
        "nodes": [
            {"id": "A", "label": "测量放线", "type": "start"},
            {"id": "B", "label": "基层清理", "type": "process"},
            {"id": "C", "label": "面层安装", "type": "end"},
        ],
        "edges": [
            {"from": "A", "to": "B"},
            {"from": "B", "to": "C"},
        ],
    }


# ==========================================================================
# A · 配色：两套渲染器 × 竖/横 四组合，新色在、旧色（R65 淡紫对）不得残留
# ==========================================================================

class TestPaletteR66:

    def _assert_new_no_old(self, img, *, expect_dark: bool):
        present = _present(img)
        assert NEW_FILL in present, "节点淡紫底 #f0edff 未出现"
        assert NEW_BORDER in present, "节点紫边框 #a89aff 未出现"
        assert DARK_GRAY in present, "深灰字/连线 #333333 未出现"
        if expect_dark:
            assert NEW_BORDER_DARK in present, "强调深紫边框 #7C63D8 未出现"
        assert OLD_FILL not in present, "R65 旧淡紫底 #ECECFF 残留"
        assert OLD_BORDER not in present, "R65 旧紫边框 #9370DB 残留"
        assert OLD_BORDER_DARK not in present, "R65 旧强调边框 #6C4FB8 残留"

    def test_v2_vertical_palette(self):
        code = (
            "flowchart TD\n"
            '    A["测量放线"] --> B["关键：基层清理"]\n'
            '    B --> C["面层安装"]\n'
        )
        # 「关键：」标签 → highlight 推断 ⇒ 同图覆盖 淡紫底/紫边框/深紫强调 三色
        self._assert_new_no_old(_render_v2(code), expect_dark=True)

    def test_v2_horizontal_palette(self):
        self._assert_new_no_old(_render_v2(_LR_CODE), expect_dark=False)

    def test_with_edges_vertical_palette(self):
        # start/end 圆角矩形用深紫强调边框
        self._assert_new_no_old(
            _render_we(_we_payload("TD")), expect_dark=True)

    def test_with_edges_horizontal_palette(self):
        self._assert_new_no_old(
            _render_we(_we_payload("LR")), expect_dark=True)


# ==========================================================================
# B · 单链几何：像素探针对**真正交付的 PNG**（R64 教训：纯字面量几何锁对
# 「模型 ≠ 产物」是盲的，这里全部读回渲染结果本身）
# ==========================================================================

class TestSingleChainGeometryR66:

    # ---- v2（Mermaid 载荷）----

    def test_v2_td_node_is_280x80_scaled(self):
        img = _render_v2(_TD_CODE)
        # 首节点中心行：单个非白区间，宽 == 280*2.5 = 700px
        runs = _nonwhite_runs(_row(img, 350, 0, img.width))
        assert len(runs) == 1, f"TD 首节点中心行应有 1 个节点区间，实得 {runs}"
        assert abs(runs[0][1] - runs[0][0] + 1 - 700) <= 6, \
            f"纵向节点宽应为 280px×2.5=700px，实得 {runs[0][1] - runs[0][0] + 1}"

    def test_v2_td_row_gap_is_40_scaled(self):
        img = _render_v2(_TD_CODE)
        # x=300（避开中轴连线与标题/图例窗口）：两节点之间的空白 == 40*2.5 = 100px
        runs = _nonwhite_runs(_col(img, 300, 150, 900))
        assert len(runs) == 2, f"TD 两节点应产生 2 个区间，实得 {runs}"
        for s, e in runs:
            assert abs(e - s + 1 - 200) <= 6, f"纵向节点高应为 80px×2.5=200px，实得 {e - s + 1}"
        assert abs(_gap(runs) - 100) <= 6, f"纵向行距应为 40px×2.5=100px，实得 {_gap(runs)}"

    def test_v2_lr_node_is_200x80_scaled(self):
        img = _render_v2(_LR_CODE)
        # y=300（避开中轴连线）：两个节点区间，宽 == 200*2.5 = 500px
        runs = _nonwhite_runs(_row(img, 300, 0, img.width))
        assert len(runs) == 2, f"LR 两节点应产生 2 个区间，实得 {runs}"
        for length in _lens(runs):
            assert abs(length - 500) <= 6, f"横向节点宽应为 200px×2.5=500px，实得 {length}"

    def test_v2_lr_col_gap_is_40_scaled(self):
        img = _render_v2(_LR_CODE)
        runs = _nonwhite_runs(_row(img, 300, 0, img.width))
        assert len(runs) == 2
        assert abs(_gap(runs) - 100) <= 6, f"横向列距应为 40px×2.5=100px，实得 {_gap(runs)}"
        # 节点高同样 80×2.5（在首节点内取一列）
        col_runs = _nonwhite_runs(_col(img, 350, 150, 700))
        assert len(col_runs) == 1
        assert abs(col_runs[0][1] - col_runs[0][0] + 1 - 200) <= 6

    # ---- with_edges（JSON 载荷）----

    def test_with_edges_td_geometry(self):
        img = _render_we(_we_payload("TD"))
        runs = _nonwhite_runs(_row(img, 350, 0, img.width))
        assert len(runs) == 1, f"TD 单列中心行应为 1 个节点区间，实得 {runs}"
        assert abs(runs[0][1] - runs[0][0] + 1 - 700) <= 6
        # 线性链 A→B→C 竖排 = 3 层 3 区间，每区间高 200px、层间隙 100px
        col_runs = _nonwhite_runs(_col(img, 300, 150, 1100))
        assert len(col_runs) == 3, f"TD 三节点链应得 3 区间，实得 {col_runs}"
        for s, e in col_runs:
            assert abs(e - s + 1 - 200) <= 6, f"with_edges 纵向节点高应为 200px，实得 {e - s + 1}"
        assert abs(_gap(col_runs) - 100) <= 6, \
            f"with_edges 纵向行距应为 100px，实得 {_gap(col_runs)}"

    def test_with_edges_lr_geometry(self):
        img = _render_we(_we_payload("LR"))
        # 线性链 A→B→C 横排 = 3 列 3 区间，每区间宽 500px、列间隙 100px
        runs = _nonwhite_runs(_row(img, 300, 0, img.width))
        assert len(runs) == 3, f"LR 三节点链应得 3 区间，实得 {runs}"
        for length in _lens(runs):
            assert abs(length - 500) <= 6, f"with_edges 横向节点宽应为 500px，实得 {length}"
        assert abs(_gap(runs) - 100) <= 6, \
            f"with_edges 横向列距应为 100px，实得 {_gap(runs)}"


# ==========================================================================
# C · 渲染缓存版本：改外观/尺寸必须踢缓存（R62→R64→R66 第三次应用）
# ==========================================================================

class TestRendererVersionR66:

    def test_renderer_version_is_7(self):
        # R67（2026-10-11 流程图渲染可读性）：菱形铺满形状盒 + 文字内接自适应 +
        # 连线分道，菱形版式/连线走向/画布尺寸都变，版本号必须 6→7。
        assert mr._RENDERER_VERSION == 7, \
            "R67 改了菱形版式与连线通道，版本号必须 6→7，否则旧缓存图永不失效"

    def test_version_appears_exactly_once(self):
        import inspect
        assert inspect.getsource(mr).count("_RENDERER_VERSION = ") == 1


# ==========================================================================
# D · 提示词 parity：方向自动选择 + 浅紫样张写进生成/修复两条链路
#     （仿 test_chart_prompt_coverage 的锁字面做法；锁**出厂默认**防基线污染）
# ==========================================================================

class TestPromptDirectionParityR66:

    def test_generation_prompt_has_dual_direction_rule(self):
        p = get_default_prompt("content_generation_system")
        assert "节点 ≤8 个用 `flowchart LR`" in p, "生成提示词缺「≤8 横向」定向"
        assert "节点 >8 个用 `flowchart TD`" in p, "生成提示词缺「>8 竖向」定向"
        assert "含检验判定菱形分支的一律" in p, "生成提示词缺「判定分支一律 TD」例外"

    def test_generation_prompt_carries_new_palette_literals(self):
        p = get_default_prompt("content_generation_system")
        assert "#f0edff" in p and "#a89aff" in p, "生成提示词未同步浅紫样张配色"

    def test_generation_prompt_chartjson_rule(self):
        p = get_default_prompt("content_generation_system")
        assert 'steps ≤8 个用 "LR"' in p, "格式 B 缺 steps 双模式定向"
        assert 'steps >8 个用 "TD"' in p, "格式 B 缺 steps 双模式定向"
        assert "steps 3-15 个" in p, "格式 B steps 上限未放宽到 15（竖向）"
        assert 'direction 固定 "TD"' not in p, "格式 B 仍在声明「direction 固定 TD」（旧口径残留）"

    def test_mermaid_fix_prompt_has_dual_direction_rule(self):
        p = get_default_prompt("chart_mermaid_fix_flowchart")
        assert "≤8 用 `flowchart LR`" in p, "专项修复缺「≤8 横向」定向"
        assert ">8 用 `flowchart TD`" in p, "专项修复缺「>8 竖向」定向"
        assert "不因定向规则翻转" in p, "专项修复缺「修复语境不因定向翻转」保守约束"

    def test_json_fix_prompt_has_dual_direction_rule(self):
        p = get_default_prompt("chart_json_fix")
        assert 'steps ≤8 用 "LR"' in p, "json 修复缺 direction 双模式口径"
        assert "steps 3-15 个" in p, "json 修复 steps 上限未放宽到 15"


# ==========================================================================
# E · 源文件字节体检（CRLF 仓库，本轮触碰的生产 + 测试文件全查）
# ==========================================================================

_TOUCHED_PROD = ["mermaid_flowchart.py", "mermaid_renderer.py"]
_TOUCHED_TESTS = [
    "test_chart_render_enhancements.py",
    "test_flowchart_unified_style_r66_20261011.py",
]


class TestR66ByteIntegrity:

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

    @pytest.mark.parametrize("name", _TOUCHED_TESTS)
    def test_touched_test_files_are_clean(self, name):
        raw = (_AI_DIR.parents[2] / "tests" / name).read_bytes()
        crlf = raw.count(b"\r\n")
        assert crlf > 0, name
        assert raw.count(b"\n") == crlf, f"{name} 有裸 LF"
        assert raw.count(b"\r") == crlf, f"{name} 有孤立 CR"
        assert not raw.startswith(b"\xef\xbb\xbf"), name
        assert bytes([0xEF, 0xBF, 0xBD]) not in raw, name
        compile(raw.decode("utf-8"), name, "exec")


# ==========================================================================
# F · 反向锁：定向**只在提示词层**，校验/渲染缺省 TD 不变、显式方向原样生效
# ==========================================================================

class TestDirectionStaysPromptLayerOnlyR66:

    def test_validator_default_stays_td_for_short_chain(self):
        # 5 步短链：若有人把自动定向下沉到校验器，这里会变成 LR → 本护栏打红
        norm = normalize_flowchart_data({
            "type": "flowchart",
            "steps": ["测量放线", "基层清理", "面层安装", "勾缝处理", "验收移交"],
        })
        assert norm is not None
        assert norm["direction"] == "TD", "校验器缺省方向被改成了自动定向（应为 TD）"

    def test_explicit_lr_payload_is_honored_verbatim(self):
        norm = normalize_flowchart_data({
            "type": "flowchart",
            "direction": "LR",
            "steps": ["测量放线", "基层清理", "面层安装", "勾缝处理", "验收移交",
                      "成品保护", "资料归档", "验收移交二"],
        })
        assert norm["direction"] == "LR", "显式 LR 被缺省逻辑覆盖（显式方向必须原样生效）"
        mermaid_code = flowchart_json_to_mermaid(norm)
        assert mermaid_code.startswith("flowchart LR")
