# -*- coding: utf-8 -*-
"""生成链路单元测试：表格取数、正文取数、类型推荐、渲染产物。"""

from pathlib import Path

from docchart.config import Config
from docchart.generation.datasource import extract_from_paragraph, extract_from_table
from docchart.generation.recommender import build_spec, recommend_type
from docchart.generation.renderer import render, BUILDERS
from docchart.models import Block, BlockKind, ChartGap, ChartSpec, GapType


def _table(rows):
    return Block(id=0, kind=BlockKind.TABLE, rows=rows, text="")


def test_extract_from_table():
    data = extract_from_table(_table([
        ["月份", "浇筑量"], ["3月", "1200"], ["4月", "2600"], ["5月", "3100"]]))
    assert data and data.categories == ["3月", "4月", "5月"]
    assert data.series[0].values == [1200.0, 2600.0, 3100.0]
    assert data.complete is True


def test_extract_from_table_partial_todo():
    data = extract_from_table(_table([
        ["楼", "高度"], ["1#", "24"], ["2#", "【待补充：高度】"], ["3#", "36"]]))
    assert data is not None and data.complete is False


def test_extract_from_paragraph_pairs():
    b = Block(id=0, kind=BlockKind.PARAGRAPH,
              text="总面积 52000 平方米，其中住宅 34000 平方米，车库 10000 平方米。")
    data = extract_from_paragraph(b)
    assert data and len(data.categories) >= 2
    assert 52000.0 in data.series[0].values


def test_recommend_type_rules():
    # 时间类目 -> line
    d1 = extract_from_table(_table([
        ["月份", "量"], ["3月", "1"], ["4月", "2"], ["5月", "3"], ["6月", "4"]]))
    assert recommend_type(d1, "") == "line"
    # 占比合计~100 -> pie
    d2 = extract_from_table(_table([
        ["项", "占比"], ["住宅", "62"], ["配套", "18"], ["车库", "20"]]))
    assert recommend_type(d2, "") == "pie"
    # 无数据 + flow 意图 -> flow
    assert recommend_type(None, "flow") == "flow"


def test_build_spec_and_render_all_types(tmp_path: Path):
    data = extract_from_table(_table([
        ["名", "值"], ["A", "10"], ["B", "20"], ["C", "30"]]))
    gap = ChartGap(anchor_block_id=0, gap_type=GapType.TABLE_NO_CHART,
                   reason="", confidence=1, suggested_type="bar", caption="测试图")
    spec = build_spec(gap, data, "文档")
    assert spec is not None
    cfg = Config()
    for ctype in ("bar", "line", "pie", "scatter", "timeline"):
        s = ChartSpec(chart_type=ctype, title="t", data=data)
        out = tmp_path / f"{ctype}.png"
        render(s, cfg, out)
        assert out.exists() and out.stat().st_size > 1000, ctype
    for ctype in ("flow", "relation"):
        s = ChartSpec(chart_type=ctype, title="t", steps=["开始", "作业", "验收"])
        out = tmp_path / f"{ctype}.png"
        render(s, cfg, out)
        assert out.exists() and out.stat().st_size > 1000, ctype


def test_svg_output_and_registry(tmp_path: Path):
    assert {"bar", "line", "pie", "flow", "relation", "timeline", "scatter"} <= set(BUILDERS)
    data = extract_from_table(_table([["名", "值"], ["A", "1"], ["B", "2"], ["C", "3"]]))
    s = ChartSpec(chart_type="bar", title="svg测试", data=data)
    out = tmp_path / "c.svg"
    render(s, Config(), out)
    assert out.exists() and "<svg" in out.read_text(encoding="utf-8")[:200]


def test_unknown_type_raises():
    import pytest
    with pytest.raises(ValueError):
        render(ChartSpec(chart_type="nope", title="x"), Config(), Path("x.png"))
