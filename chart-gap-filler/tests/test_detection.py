# -*- coding: utf-8 -*-
"""检测规则单元测试：五类缺失信号各自可触发，且已有图时不误报。"""

from pathlib import Path

from docchart.config import Config
from docchart.detection import Detector
from docchart.detection.rules import extract_flow_steps, numeric_cell, table_numeric_density
from docchart.models import GapType
from docchart.parsers import parse_file


def _detect(md_text: str, cfg=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.md"
        f.write_text(md_text, encoding="utf-8")
        doc = parse_file(f)
        return Detector(doc, cfg or Config()).detect()


def test_dangling_numbered_reference():
    gaps = _detect("# A\n\n施工进度如图 9-9 所示。\n")
    assert any(g.gap_type == GapType.DANGLING_REF and "图9-9" in g.reason for g in gaps)


def test_reference_satisfied_by_image_not_flagged():
    gaps = _detect("# A\n\n施工进度如图 9-9 所示。\n\n![](x.png)\n\n图 9-9 进度计划\n")
    assert not [g for g in gaps if g.gap_type in (GapType.DANGLING_REF,
                                                  GapType.CAPTION_NO_IMAGE)]


def test_unnumbered_reference_with_flow_steps():
    gaps = _detect("# A\n\n拆除工艺流程如下图所示：\n\n脚手架→安全网→连墙件\n")
    g = next(g for g in gaps if g.gap_type == GapType.DANGLING_REF)
    assert g.suggested_type == "flow"
    assert g.steps == ["脚手架", "安全网", "连墙件"]


def test_caption_without_image():
    gaps = _detect("# A\n\n图 1-1 节点示意\n\n后文。\n")
    g = next(g for g in gaps if g.gap_type == GapType.CAPTION_NO_IMAGE)
    assert g.insert_before is True


def test_empty_placeholder():
    gaps = _detect("# A\n\n【插入图表】\n")
    assert any(g.gap_type == GapType.EMPTY_PLACEHOLDER and g.confidence > 0.9 for g in gaps)


def test_table_without_chart_detected():
    gaps = _detect(
        "# A\n\n## B\n\n| 月 | 量 |\n| --- | --- |\n| 3月 | 10 |\n| 4月 | 20 |\n| 5月 | 30 |\n")
    g = next(g for g in gaps if g.gap_type == GapType.TABLE_NO_CHART)
    assert g.confidence >= 0.6


def test_table_with_todo_cells_flags_needs_data():
    gaps = _detect(
        "# A\n\n## B\n\n| 楼 | 高度 |\n| --- | --- |\n| 1# | 【待补充：高度】 |\n"
        "| 2# | 【待补充：高度】 |\n| 3# | 【待补充：高度】 |\n")
    g = next(g for g in gaps if g.gap_type == GapType.TABLE_NO_CHART)
    assert g.needs_data is True


def test_numeric_helpers():
    assert numeric_cell("1,200.5 m³") == 1200.5
    assert numeric_cell("【待补充：用量】") is None
    density, cols = table_numeric_density(
        [["名", "值"], ["a", "1"], ["b", "2"], ["c", "x"]])
    assert cols == [1] and density > 0.3
    assert extract_flow_steps("顺序为：验电、放电、接地、悬挂标示牌") == \
        ["验电", "放电", "接地", "悬挂标示牌"]


def test_semantic_hint_requires_numbers():
    gaps = _detect("# A\n\n本季度材料消耗呈现逐月下降趋势，从 320 吨降至 150 吨。\n")
    assert any(g.gap_type == GapType.SEMANTIC_HINT and g.suggested_type == "line"
               for g in gaps)
    # 无具体数值时不产出低质建议
    gaps2 = _detect("# B\n\n后续消耗趋势将随天气变化而波动，具体数据另行统计。\n")
    assert not [g for g in gaps2 if g.gap_type == GapType.SEMANTIC_HINT]
