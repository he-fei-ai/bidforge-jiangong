# -*- coding: utf-8 -*-
"""图表生成/渲染增强与 BUG 修复回归测试。

覆盖：
- 架构图：环数据不再 RecursionError；字符串子节点/别名键不再被静默丢弃；
  样张配色（深藏青根 + highlight 琥珀橙高亮）
- 流程图：横向 LR 布局、样张配色（start/end 深藏青、process 中蓝、highlight/decision
  琥珀）、steps/edges/variant chart-json 载荷校验与渲染闭环
- 甘特图：`section` 阶段带渲染；关键路径模式下备注不再丢失、不再留白
- 流程图：字符串形式边（含"下标"端点）不再产生幽灵节点
- 劳动力图：非法容器（phases/categories 非序列）优雅返回 None
- 对比图：超长标签/大数值渲染不崩溃且画布有界
- 字体：跨平台字体路径只探测一次（性能回归）
- 图表清单：历史裸 Mermaid / 规范信封载荷都能拿到可渲染代码
"""
import json
import os
from io import BytesIO

from app.services.ai.mermaid_architecture import (
    _layout_architecture_tree,
    _render_architecture_image_v2,
)
from app.services.ai.mermaid_common import _FONT_PATH_CACHE, _image_font
from app.services.ai.mermaid_comparison import _render_comparison_image_v2
from app.services.ai.mermaid_flowchart import (
    _parse_flowchart_structure,
    _render_flowchart_image_v2,
    flowchart_json_to_mermaid,
)
from app.services.ai.mermaid_gantt import _render_gantt_image_v2
from app.services.ai.mermaid_labor import _render_labor_image_v2
from app.services.ai.mermaid_layout import _render_layout_image_v2
from app.services.ai.mermaid_timeline import _render_timeline_image_v2
from app.services.chart_validators import (
    count_all_nodes,
    normalize_architecture_tree,
    normalize_flowchart_data,
    normalize_gantt_plan,
    normalize_labor_data,
    normalize_layout_data,
    normalize_timeline_data,
    validate_architecture_tree,
    validate_flowchart_data,
    validate_gantt_plan,
    validate_labor_data,
    validate_layout_data,
    validate_timeline_data,
)
from PIL import Image

# 甘特图备注文字专用色（mermaid_gantt 中仅备注使用）
_NOTE_RGB = (55, 65, 81)


def _open(bio: BytesIO) -> Image.Image:
    bio.seek(0)
    return Image.open(bio)


def _count_color(img: Image.Image, rgb: tuple[int, int, int]) -> int:
    # 用 getcolors 而非已弃用的 getdata（Pillow 14 将移除 getdata）
    for count, color in (img.convert("RGB").getcolors(maxcolors=1 << 24) or []):
        if color == rgb:
            return count
    return 0


# ---------------- 架构图 ----------------

def test_architecture_cycle_does_not_crash():
    """环数据（子节点引用祖先）不应触发 RecursionError，且能正常出图。"""
    root: dict = {"label": "根节点"}
    child: dict = {"label": "子节点", "children": [root]}
    root["children"] = [child]

    # 归一化后应无环（剪除了回指祖先的子节点）
    normalized = normalize_architecture_tree(root)
    assert normalized is not None
    assert id(normalized) != id(root)

    bio = _render_architecture_image_v2(root)
    assert bio is not None
    assert len(bio.getvalue()) > 100


def test_architecture_string_children_not_dropped():
    """children 为字符串列表时不应被静默丢弃（旧实现只剩根节点）。"""
    data = {"label": "项目部", "children": ["技术部", "安全部", "质检部"]}
    normalized = normalize_architecture_tree(data)
    assert normalized is not None
    assert all(isinstance(c, dict) for c in normalized["children"])
    assert count_all_nodes(normalized) == 4
    assert validate_architecture_tree(data)[0] is True

    nodes, _w, _h = _layout_architecture_tree(normalized)
    assert len(nodes) == 4  # 根 + 3 个子节点

    bio = _render_architecture_image_v2(data)
    assert bio is not None and len(bio.getvalue()) > 100


def test_architecture_alias_children_key_normalized():
    """子节点列表使用别名键 nodes 时应统一为 children（校验/渲染同口径）。"""
    data = {"label": "系统架构", "nodes": [{"label": "前端"}, {"label": "后端"}]}
    normalized = normalize_architecture_tree(data)
    assert normalized is not None
    assert count_all_nodes(normalized) == 3
    nodes, _w, _h = _layout_architecture_tree(normalized)
    assert len(nodes) == 3


def test_architecture_highlight_node_amber_and_layout_passthrough():
    """回归（2026-09-18 样张改版）：highlight:true 节点必须穿透布局层进入渲染，
    渲染为琥珀橙 #FFC000 + 深藏青文字；根节点恒为深藏青 #1F3864；
    旧金色 #FBBF24 根样式不得残留。"""
    tree = {"label": "项目经理部", "children": [
        {"label": "技术部", "highlight": True},
        {"label": "质量部"},
        {"label": "安全部", "highlight": "true"},  # 字符串形态同样按真值解释
    ]}
    nodes, _w, _h = _layout_architecture_tree(tree)
    hl_labels = {n["label"] for n in nodes if n.get("highlight")}
    assert hl_labels == {"技术部", "安全部"}, "highlight 标志未随布局透传"

    bio = _render_architecture_image_v2(tree)
    assert bio is not None and len(bio.getvalue()) > 100
    img = _open(bio)
    AMBER = (255, 192, 0)    # #FFC000
    NAVY = (31, 56, 100)     # #1F3864
    assert _count_color(img, AMBER) > 500, "highlight 节点未渲染为琥珀橙"
    assert _count_color(img, NAVY) > 500, "根节点未使用深藏青"
    assert _count_color(img, (251, 191, 36)) == 0, "旧金色根样式残留"


def test_flowchart_horizontal_lr_and_doc_palette():
    """回归（2026-09-18 横向流程图样张改版）：
    - LR 线性链必须横排（宽 > 高，旧缺陷是方向被当 TD 处理）；
    - 样张配色：start/end 深藏青 #1F3A63、process 中蓝 #4A7CC7、
      decision 琥珀 #F5B800（菱形）、关键词高亮 highlight 琥珀。"""
    code = (
        "flowchart LR\n"
        '    A["施工准备"] --> B["关键：主体结构施工"]\n'
        '    B --> C["装饰装修"]\n'
        '    C --> D{"验收是否通过"}\n'
        '    D -->|通过| E["竣工验收"]\n'
        '    D -->|不通过| C\n'
    )
    bio = _render_flowchart_image_v2(code)
    assert bio is not None and len(bio.getvalue()) > 100
    img = _open(bio)
    assert img.size[0] > img.size[1], f"LR 未横向排布（{img.size}）"
    colors = img.convert("RGB").getcolors(maxcolors=1 << 24) or []
    present = {c for _n, c in colors}
    assert (31, 58, 99) in present, "start/end 深藏青 #1F3A63 未出现"
    assert (74, 124, 199) in present, "process 中蓝 #4A7CC7 未出现"
    assert (245, 184, 0) in present, "decision/highlight 琥珀 #F5B800 未出现"
    assert (90, 124, 168) in present, "连线 #5A7CA8 未出现"


def test_flowchart_steps_payload_validated_and_rendered():
    """回归：施工/工艺流程图 chart-json 载荷（steps/edges/variant）必须通过
    校验-渲染闭环，显式 variant 经 overrides 生效，direction 缺省 LR。"""
    payload = {
        "type": "flowchart",
        "title": "项目总体施工流程图",
        "steps": [
            {"id": "s1", "label": "施工准备", "variant": "start"},
            {"id": "s2", "label": "基础施工", "variant": "process"},
            {"id": "s3", "label": "关键：主体结构施工", "variant": "highlight"},
            {"id": "s4", "label": "竣工验收", "variant": "end"},
        ],
        "edges": [
            {"from": "s1", "to": "s2"},
            {"from": "s2", "to": "s3"},
            {"from": "s3", "to": "s4"},
        ],
    }
    ok, errs = validate_flowchart_data(payload)
    assert ok, errs
    norm = normalize_flowchart_data(payload)
    assert norm is not None
    assert norm["direction"] == "LR", "steps 载荷缺省方向应为 LR"
    assert [n["type"] for n in norm["nodes"]] == ["start", "process", "highlight", "end"]
    assert [n["id"] for n in norm["nodes"]] == ["s1", "s2", "s3", "s4"], "下标/别名端点应解析"

    mermaid_code = flowchart_json_to_mermaid(norm)
    assert mermaid_code.startswith("flowchart LR")
    bio = _render_flowchart_image_v2(
        mermaid_code,
        variant_overrides={
            n["id"]: n["type"] for n in norm["nodes"] if n["type"] != "process"
        },
    )
    assert bio is not None and len(bio.getvalue()) > 100
    img = _open(bio)
    assert img.size[0] > img.size[1], "steps 载荷未按 LR 横向渲染"
    colors = img.convert("RGB").getcolors(maxcolors=1 << 24) or []
    present = {c for _n, c in colors}
    assert (31, 58, 99) in present, "start/end 深藏青未出现"
    assert (245, 184, 0) in present, "highlight 琥珀未出现"


def test_flowchart_validator_rejects_broken_payload():
    """校验宽进口径下的底线：无节点 / 单节点 / 边端点全部无法解析必须拦截。"""
    assert validate_flowchart_data({"type": "flowchart"})[0] is False
    assert validate_flowchart_data(
        {"type": "flowchart", "steps": [{"id": "s1", "label": "只有一步"}]})[0] is False
    ok, _ = validate_flowchart_data({
        "type": "flowchart",
        "steps": ["施工准备", "主体施工", "竣工验收"],
        "edges": [[0, 1], [1, 2]],  # 下标形态边
    })
    assert ok, "字符串节点 + 下标边应放行（渲染器可画）"


# ---------------- 甘特图 ----------------

def _gantt_plan(remark: str = "") -> dict:
    task = {"id": 1, "name": "基础施工", "start": 1, "end": 10, "critical": True}
    if remark:
        task["remark"] = remark
    return {
        "title": "计划",
        "totalDays": 30,
        "tasks": [task, {"id": 2, "name": "主体施工", "start": 11, "end": 30,
                         "dependencies": [1]}],
    }


def test_gantt_remarks_rendered_in_critical_mode():
    """关键路径模式（带依赖的计划默认走此模式）下备注必须被绘制，而非丢弃留白。"""
    without = _render_gantt_image_v2(_gantt_plan(), highlight_critical_path=True)
    with_remark = _render_gantt_image_v2(
        _gantt_plan("春节停工"), highlight_critical_path=True)
    assert without is not None and with_remark is not None

    img_without = _open(without)
    img_with = _open(with_remark)
    # 备注预留高度：int(min(80, 30 + 1*18) * 2.5) == int(48 * 2.5) == 120
    assert img_with.size[1] - img_without.size[1] == 120
    # 无备注图不含备注专用色；有备注图必须出现备注文字像素
    assert _count_color(img_without, _NOTE_RGB) == 0
    assert _count_color(img_with, _NOTE_RGB) > 0


def test_gantt_section_band_adds_rows():
    """`section` 阶段划分应渲染为阶段带（每个新阶段多一行）。"""
    base = {
        "title": "计划",
        "totalDays": 30,
        "tasks": [
            {"id": 1, "name": "场地平整", "start": 1, "end": 10, "section": "准备阶段"},
            {"id": 2, "name": "主体施工", "start": 11, "end": 30, "section": "主体阶段"},
        ],
    }
    no_section = {
        **base,
        "tasks": [{k: v for k, v in t.items() if k != "section"} for t in base["tasks"]],
    }
    b_sec = _render_gantt_image_v2(base)
    b_no = _render_gantt_image_v2(no_section)
    assert b_sec is not None and b_no is not None
    # 2 个阶段 → 2 条阶段带 = 2 行（每行 48 * 2.5 = 120）
    assert _open(b_sec).size[1] - _open(b_no).size[1] == 240


# ---------------- 流程图 ----------------

def test_flowchart_string_edges_with_index_endpoints_resolved():
    """字符串边使用"下标端点"（"0 --> 1"）时应映射为真实节点 ID，不产生幽灵节点。"""
    code = flowchart_json_to_mermaid(
        {"nodes": ["开始", "处理", "结束"], "edges": ["0 --> 1", "1 --> 2"]})
    assert "N1 --> N2" in code and "N2 --> N3" in code
    assert "0 --> 1" not in code

    parsed = _parse_flowchart_structure(code)
    assert {n["id"] for n in parsed["nodes"]} == {"N1", "N2", "N3"}
    assert [(e["from"], e["to"]) for e in parsed["edges"]] == [("N1", "N2"), ("N2", "N3")]


# ---------------- 劳动力图 ----------------

def test_labor_invalid_container_does_not_crash():
    """phases/categories 非序列（dict）时不得崩溃：能救则救，救不活则 None。

    ✅ 行为变更（labor 载荷归一化）：旧实现直接 return None —— 连数据本身可渲染
    （``[[1]]``）的载荷也被一并放弃，导出只剩红字占位。现在归一器丢弃无法解析的
    标签、按数据行/列补造标签，图仍画得出来；只有**数据本身**无法识别时才 None。
    """
    # 标签容器非法但数据可用 → 归一补位后仍能出图
    for payload in (
        {"phases": {"a": 1}, "categories": ["x"], "data": [[1]]},
        {"phases": ["P1"], "categories": {"a": 1}, "data": [[1]]},
    ):
        bio = _render_labor_image_v2(payload)
        assert bio is not None and len(bio.getvalue()) > 100, payload
    # 数据本身无法识别 → 优雅返回 None（由调用方出占位）
    assert _render_labor_image_v2(
        {"phases": ["P1"], "categories": ["x"], "data": []}) is None
    assert _render_labor_image_v2({"note": "暂无数据"}) is None


# ---------------- 劳动力图：载荷归一（normalize_labor_data） ----------------
# labor 曾是唯一"校验侧 + 渲染侧都没有容错"的 JSON 图表类型：只认
# phases/categories/data 三件套，形状稍偏就整块消失（校验判非法 → 删块；
# 或校验通过 → 渲染 None → 导出占位）。现与 architecture 一样收敛到共享归一器。

def test_labor_normalize_natural_shapes():
    """AI 产出的各种自然形态都应归一为 phases/categories/data 三件套。"""
    # 工种 → 序列（转置：行 = 阶段，列 = 工种）
    assert normalize_labor_data(
        {"data": {"木工": [10, 20], "钢筋工": [5, 8]}}) == {
            "phases": ["阶段1", "阶段2"],
            "categories": ["木工", "钢筋工"],
            "data": [[10.0, 5.0], [20.0, 8.0]],
        }

    # ECharts series
    assert normalize_labor_data(
        {"series": [{"name": "木工", "data": [10, 20]}]}) == {
            "phases": ["阶段1", "阶段2"], "categories": ["木工"],
            "data": [[10.0], [20.0]],
        }

    # Chart.js datasets（labels 为阶段轴）
    assert normalize_labor_data({
        "labels": ["基础", "主体"],
        "datasets": [{"label": "木工", "data": [10, 20]}],
    }) == {"phases": ["基础", "主体"], "categories": ["木工"],
           "data": [[10.0], [20.0]]}

    # 行内阶段名：["基础", 10, 5]
    norm = normalize_labor_data({"categories": ["木工", "钢筋工"],
                                 "data": [["基础", 10, 5]]})
    assert norm["phases"] == ["基础"]
    assert norm["data"] == [[10.0, 5.0]]

    # 阶段 × 工种 计数
    norm = normalize_labor_data({"data": [{"phase": "基础", "木工": 10, "钢筋工": 5}]})
    assert norm == {"phases": ["基础"], "categories": ["木工", "钢筋工"],
                    "data": [[10.0, 5.0]]}

    # 工种清单（单阶段）：trades + peak
    norm = normalize_labor_data({"trades": [{"name": "木工", "peak": 30},
                                            {"name": "钢筋工", "peak": 18}]})
    assert norm["categories"] == ["木工", "钢筋工"]
    assert norm["data"] == [[30.0, 18.0]]

    # 阶段对象内嵌工种清单：{"phases":[{"name":..,"workers":[{trade,count}]}]}
    # （修复提示词 chart_json_fix 教出去的形态）
    assert normalize_labor_data({"phases": [
        {"name": "基础", "workers": [{"trade": "木工", "count": 10},
                                     {"trade": "钢筋工", "count": 5}]},
        {"name": "主体", "workers": [{"trade": "木工", "count": 20},
                                     {"trade": "钢筋工", "count": 8}]},
    ]}) == {"phases": ["基础", "主体"], "categories": ["木工", "钢筋工"],
            "data": [[10.0, 5.0], [20.0, 8.0]]}

    # 同上，但阶段对象里平铺工种计数
    assert normalize_labor_data({"phases": [{"name": "基础", "木工": 10, "钢筋工": 5}]}) \
        == {"phases": ["基础"], "categories": ["木工", "钢筋工"], "data": [[10.0, 5.0]]}

    # 嵌套包装
    assert normalize_labor_data({
        "type": "labor",
        "data": {"phases": ["基础"], "categories": ["木工"], "data": [[10]]},
    }) == {"phases": ["基础"], "categories": ["木工"], "data": [[10.0]]}

    # 别名键（time/types）+ 字符串分隔
    assert normalize_labor_data({
        "time": "基础,主体", "types": "木工、钢筋工", "data": [[10, 5], [20, 8]],
    }) == {"phases": ["基础", "主体"], "categories": ["木工", "钢筋工"],
           "data": [[10.0, 5.0], [20.0, 8.0]]}

    # 一维数值序列（单工种 × 各阶段）
    norm = normalize_labor_data({"labels": ["1月", "2月"], "values": [30, 45]})
    assert norm == {"phases": ["1月", "2月"], "categories": ["劳动力"],
                    "data": [[30.0], [45.0]]}


def test_labor_normalize_unrecognizable_returns_none():
    """无法识别为 labor 载荷时返回 None（调用方据此删块/出占位）。"""
    assert normalize_labor_data({}) is None
    assert normalize_labor_data({"note": "暂无数据"}) is None
    assert normalize_labor_data([1, 2, 3]) is None
    assert normalize_labor_data("文字") is None
    assert normalize_labor_data(None) is None


def test_labor_normalize_keeps_data_on_axis_mismatch():
    """阶段名与数据行数不等时以**数据**为准：补造/裁剪标签，绝不丢数据行。"""
    # 阶段名多于数据行 → 裁剪阶段名（否则渲染器补一整行 0，校验器又判"全 0"非法）
    norm = normalize_labor_data({"phases": ["基础", "主体", "装修"],
                                 "categories": ["木工"], "data": [[10], [20]]})
    assert len(norm["phases"]) == len(norm["data"]) == 2

    # 数据行多于阶段名 → 补造阶段名，保住多出来的数据行
    norm = normalize_labor_data({"phases": ["基础"], "categories": ["木工"],
                                 "data": [[10], [20], [30]]})
    assert len(norm["phases"]) == len(norm["data"]) == 3
    assert norm["data"] == [[10.0], [20.0], [30.0]]

    # 矩阵列多于工种 → 补造工种名，而不是丢弃该列
    norm = normalize_labor_data({"phases": ["基础"], "categories": ["木工"],
                                 "data": [[10, 5]]})
    assert len(norm["categories"]) == 2
    assert norm["data"] == [[10.0, 5.0]]


def test_labor_normalize_validate_render_agree():
    """归一 / 校验 / 渲染三者必须同向：能归一 → 校验通过 → 能出图。

    任一处掉队就是"图表凭空少一张"：校验掉队 → 删块；渲染掉队 → 导出红字占位。
    """
    shapes = [
        {"data": {"木工": [10, 20], "钢筋工": [5, 8]}},
        {"series": [{"name": "木工", "data": [10, 20]}]},
        {"labels": ["基础", "主体"], "datasets": [{"label": "木工", "data": [10, 20]}]},
        {"data": [{"phase": "基础", "木工": 10, "钢筋工": 5}]},
        {"data": {"基础": {"木工": 10}, "主体": {"木工": 20}}},
        {"trades": [{"name": "木工", "peak": 30}]},
        {"phases": [{"name": "基础", "workers": [{"trade": "木工", "count": 10}]}]},
        {"data": [["基础", 10, 5], ["主体", 20, 8]]},
        {"time": "基础,主体", "types": "木工、钢筋工", "data": [[10, 5], [20, 8]]},
    ]
    for payload in shapes:
        assert normalize_labor_data(payload) is not None, payload
        ok, errs = validate_labor_data(payload)
        assert ok, (payload, errs)
        bio = _render_labor_image_v2(payload)
        assert bio is not None and len(bio.getvalue()) > 100, payload


def test_labor_render_keeps_title_through_normalize():
    """归一后 title 等展示字段不能被丢掉（渲染器仍要画标题）。"""
    base = {"trades": [{"name": "木工", "peak": 30}]}
    a = _render_labor_image_v2({**base, "title": "劳动力投入计划"})
    b = _render_labor_image_v2({**base, "title": "另一个标题"})
    assert a is not None and b is not None
    assert a.getvalue() != b.getvalue()      # 标题确实参与绘制


# ---------------- 布局图 / 时间轴：载荷归一（与 labor 同型） ----------------
# 这两类此前同样是"渲染器能画、校验器判非法" → 正文图表被整块删块。
# 判据：渲染器读字段的口径必须与校验器判非法的口径来自同一个函数。

def test_layout_normalize_fills_missing_zone_name():
    """区域没写 name 时归一为「区域N」（渲染器本来就这么回落）。"""
    payload = {"type": "layout", "zones": [{"id": "Z1", "description": "办公区"},
                                           {"id": "Z2", "category": "加工区"}]}
    norm = normalize_layout_data(payload)
    assert [z["name"] for z in norm["zones"]] == ["区域1", "区域2"]
    # 原有字段必须保留（id / description / category 不能被丢）
    assert norm["zones"][0]["id"] == "Z1"
    assert norm["zones"][0]["description"] == "办公区"

    # 容器别名 areas / regions
    assert normalize_layout_data({"areas": [{"name": "A"}]})["zones"][0]["name"] == "A"
    assert normalize_layout_data({"regions": [{"name": "B"}]})["zones"][0]["name"] == "B"
    # 无法识别
    assert normalize_layout_data({"note": "暂无"}) is None
    assert normalize_layout_data({"zones": []}) is None


def test_timeline_normalize_natural_shapes():
    """时间轴的 4 种容器别名 + 无名/无日期 + status 别名都应归一。"""
    norm = normalize_timeline_data({"events": [{"date": "2026-01-01", "name": "开工"},
                                               {"date": "2026-06-01", "name": "竣工"}]})
    assert [m["name"] for m in norm["milestones"]] == ["开工", "竣工"]

    assert normalize_timeline_data({"points": [{"name": "A"}, {"name": "B"}]}) is not None
    assert normalize_timeline_data({"items": [{"name": "A"}, {"name": "B"}]}) is not None

    # 无名 → 里程碑N；无日期 → 允许为空（渲染器画空日期）
    norm = normalize_timeline_data({"milestones": [{"date": "2026-01-01"}, {"name": "竣工"}]})
    assert norm["milestones"][0]["name"] == "里程碑1"
    assert norm["milestones"][1]["date"] == ""

    # status 别名映射
    norm = normalize_timeline_data(
        {"milestones": [{"name": "A", "date": "D", "status": "done"},
                        {"name": "B", "date": "D", "status": "进行中"},
                        {"name": "C", "date": "D", "status": "未开始"},
                        {"name": "D", "date": "D", "status": "乱写的"}]})
    assert [m["status"] for m in norm["milestones"][:3]] == [
        "completed", "in_progress", "pending"]
    # 无法识别的 status 直接丢弃，而不是让整块被判非法
    assert "status" not in norm["milestones"][3]

    assert normalize_timeline_data({"note": "暂无"}) is None
    assert normalize_timeline_data({"milestones": []}) is None


def test_layout_timeline_validate_render_agree():
    """归一 / 校验 / 渲染三者必须同向（任一处掉队即"图表凭空少一张"）。"""
    layout_shapes = [
        {"type": "layout", "zones": [{"name": "办公区"}, {"name": "加工区"}]},
        {"type": "layout", "areas": [{"name": "办公区"}, {"name": "加工区"}]},
        {"type": "layout", "zones": [{"id": "Z1", "description": "办公区"},
                                     {"id": "Z2", "category": "加工区"}]},
        {"type": "layout", "zones": [{"label": "办公区"}, {"label": "加工区"}]},
    ]
    for payload in layout_shapes:
        assert normalize_layout_data(payload) is not None, payload
        ok, errs = validate_layout_data(payload)
        assert ok, (payload, errs)
        assert _render_layout_image_v2(payload) is not None, payload

    timeline_shapes = [
        {"type": "timeline",
         "milestones": [{"date": "2026-01-01", "name": "开工"},
                        {"date": "2026-06-01", "name": "竣工"}]},
        {"type": "timeline", "events": [{"date": "第30天", "name": "基础完成"},
                                        {"date": "T+90", "name": "主体封顶"}]},
        {"type": "timeline", "milestones": [{"date": "2026-01-01"},
                                            {"name": "竣工"}]},
        {"type": "timeline", "milestones": [{"date": "2026-01-01", "name": "开工",
                                             "status": "done"},
                                            {"date": "2026-06-01", "name": "竣工",
                                             "status": "已完成"}]},
        # ✅ 回归（2026-09-16）：校验器认 items 别名，而渲染器只认
        #    milestones/events/points → `{"type":"timeline","items":[...]}` 校验放行、
        #    渲染返回 None、导出变红字占位。现渲染器统一过 normalize_timeline_data。
        {"type": "timeline", "items": [{"date": "第1天", "name": "进场"},
                                       {"date": "第30天", "name": "验收"}]},
        {"type": "timeline", "items": [{"date": "第1天", "name": "进场", "status": "done"},
                                       {"date": "第30天", "name": "验收"}]},
    ]
    for payload in timeline_shapes:
        assert normalize_timeline_data(payload) is not None, payload
        ok, errs = validate_timeline_data(payload)
        assert ok, (payload, errs)
        bio = _render_timeline_image_v2(payload)
        assert bio is not None and len(bio.getvalue()) > 100, payload


# ---------------- 甘特图 ----------------

def test_gantt_normalize_container_aliases_and_id_fallback():
    """gantt 的 3 种容器别名 + id 回落 + dependencies 可用 name 引用。"""
    # 容器别名：tasks / items / rows
    for key in ("tasks", "items", "rows"):
        norm = normalize_gantt_plan({key: [{"name": "A", "start": 0, "end": 5},
                                           {"name": "B", "start": 5, "end": 9}]})
        assert norm is not None, key
        assert len(norm["tasks"]) == 2, key

    # id 缺失 → 依次回落 name/task/title → 序号
    norm = normalize_gantt_plan({"tasks": [{"name": "进场"}, {"task": "开挖"},
                                           {"title": "垫层"}, {"start": 0, "end": 1}]})
    assert [t["id"] for t in norm["tasks"]] == ["进场", "开挖", "垫层", 4]

    # dependencies 允许用任务名引用 → 归一为 id 的字符串形式
    norm = normalize_gantt_plan({"tasks": [{"id": 1, "name": "施工准备"},
                                           {"id": 2, "name": "基础工程",
                                            "dependencies": ["施工准备", 1]}]})
    assert norm["tasks"][1]["dependencies"] == ["1", "1"]
    # 引用不到的依赖直接丢弃，而不是让整块被判非法
    norm = normalize_gantt_plan({"tasks": [{"id": 1, "name": "A",
                                            "dependencies": ["不存在的任务"]}]})
    assert norm["tasks"][0]["dependencies"] == []

    # 无法识别的载荷 → None（与渲染侧一致，交给占位/删除链路处理）
    assert normalize_gantt_plan({"tasks": []}) is None
    assert normalize_gantt_plan({"note": "暂无进度计划"}) is None
    assert normalize_gantt_plan([1, 2, 3]) is None
    assert normalize_gantt_plan(None) is None


def test_gantt_validate_render_agree():
    """gantt 的归一/校验/渲染三者必须同向。

    ⚠️ 这是本轮最重的一处回归防线：gantt 原先**不在 _JSON_CHART_TYPES 里**，
    正文里的 gantt JSON 数据块被当成 Mermaid 语法校验 → 必然失败 → 整块删除；
    而渲染器实测 0.34s 就能画出图。两侧必须同时放行、同时拒绝。
    """
    shapes = [
        {"type": "gantt", "title": "施工进度计划", "totalDays": 180,
         "tasks": [{"id": 1, "name": "施工准备", "start": 0, "end": 15, "dependencies": []},
                   {"id": 2, "name": "基础工程", "start": 15, "end": 60, "dependencies": [1]}]},
        {"type": "gantt",
         "items": [{"name": "进场准备", "start": "2026-01-01", "end": "2026-01-20"},
                   {"name": "主体施工", "start": "2026-01-20", "end": "2026-05-01",
                    "dependencies": ["进场准备"]}]},
        {"type": "gantt",
         "rows": [{"title": "测量放线", "start": 0, "end": 5},
                  {"title": "土方开挖", "start": 5, "end": 25,
                   "dependencies": ["测量放线"]}]},
        {"type": "gantt",
         "tasks": [{"name": "A", "start": 0, "end": 10},
                   {"name": "B", "start": 10, "end": 20, "dependencies": ["A"]}]},
    ]
    for payload in shapes:
        assert normalize_gantt_plan(payload) is not None, payload
        ok, errs = validate_gantt_plan(payload)
        assert ok, (payload, errs)
        bio = _render_gantt_image_v2(payload)
        assert bio is not None and len(bio.getvalue()) > 100, payload

    # 空载荷/纯文字：两侧都应拒绝（渲染器不得产出退化空画布）
    for payload in ({"type": "gantt", "tasks": []}, {"note": "暂无进度计划"}):
        assert normalize_gantt_plan(payload) is None, payload
        ok, _errs = validate_gantt_plan(payload)
        assert not ok, payload
        assert _render_gantt_image_v2(payload) is None, payload


def test_gantt_invalid_json_payload_does_not_render_garbage():
    """非法 gantt JSON 不得被"宽松 mermaid 解析"画成任务名叫 `{"type"` 的垃圾图。

    回归背景：mermaid_renderer 的 gantt 兜底分支曾漏加「非 JSON 载荷」守卫，
    导致校验已拒绝的 JSON 被 _parse_mermaid_gantt 逐行误解析，产出 93845B 的
    无意义图并成功返回 —— 与校验侧口径完全相反，AI 修复/删除链路因此失效。
    """
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

    for payload in ({"type": "gantt", "title": "T", "totalDays": 180, "tasks": []},
                    {"type": "gantt", "title": "T", "note": "只是文字"}):
        code = json.dumps(payload, ensure_ascii=False)
        assert render_mermaid_to_bytes(code, "gantt") is None, payload

    # 反向验证：合法载荷仍要出图（别把守卫写死成"一律 None"）
    good = {"type": "gantt", "title": "进度", "totalDays": 90,
            "tasks": [{"id": 1, "name": "施工准备", "start": 0, "end": 15},
                      {"id": 2, "name": "基础施工", "start": 15, "end": 45,
                       "dependencies": [1]}]}
    assert render_mermaid_to_bytes(json.dumps(good, ensure_ascii=False), "gantt") is not None


def test_gantt_is_registered_as_json_chart_type():
    """gantt 必须在 JSON 图表类型集合里，否则数据块会被当 Mermaid 语法校验。"""
    from app.routers import _chart_pipeline as cp

    assert "gantt" in cp._JSON_CHART_TYPES
    assert "gantt" in cp._JSON_VALIDATORS


# ---------------- 对比图 ----------------

def test_comparison_long_labels_and_large_values():
    """超长标签 + 大数值：应正常出图且画布宽度有界（不因数值文本溢出而裁切）。"""
    bio = _render_comparison_image_v2({
        "title": "对比",
        "items": [
            {"label": "方案甲：超长名称用于验证标签截断逻辑是否生效", "value": 123456.7},
            {"label": "方案乙", "value": 60},
        ],
    })
    assert bio is not None
    w, h = _open(bio).size
    assert 2000 < w < 3000 and h > 300


# ---------------- 字体加载性能 ----------------

def test_font_path_resolution_cached():
    """字体路径解析应被记忆（跨平台列表只探测一次，避免每次渲染线性扫描）。"""
    _FONT_PATH_CACHE.clear()
    _image_font(20)
    _image_font(30, bold=True)
    assert (False, os.environ.get("FONT_PATH", "").strip()) in _FONT_PATH_CACHE
    assert (True, os.environ.get("FONT_PATH", "").strip()) in _FONT_PATH_CACHE


# ---------------- 图表清单载荷解析 ----------------

async def test_list_charts_parses_all_payload_shapes(db_conn):
    """list_charts 应对齐 chart_payload 唯一解析器：历史裸 Mermaid / 规范信封 / 数据信封
    都能拿到可渲染代码（旧实现只认 JSON 对象，裸 Mermaid 载荷 code 恒为空）。"""
    from app.routers.charts import list_charts

    conn = db_conn
    await conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES ('s1','p1','方案')")
    await conn.execute(
        "INSERT INTO sections (id, scheme_id, title, content) "
        "VALUES ('sec1','s1','章节','正文')")
    rows = [
        ("c1", "flowchart", "flowchart TD\n    A --> B"),          # 历史裸 Mermaid
        ("c2", "flowchart", json.dumps({"mermaid_code": "graph TD\n    X --> Y",
                                        "title": "图", "reason": ""},
                                       ensure_ascii=False)),        # 规范信封
        ("c3", "labor", json.dumps({"mermaid_code": "",
                                    "data": {"phases": ["P1"], "categories": ["a"],
                                             "data": [[1]]}}, ensure_ascii=False)),  # 数据信封
    ]
    for cid, ct, payload in rows:
        await conn.execute(
            "INSERT INTO chart_predictions "
            "(id, section_id, scheme_id, chart_type, needed, purpose, priority, status, data_json) "
            "VALUES (?,?,?,?,1,?,3,'done',?)",
            (cid, "sec1", "s1", ct, "x", payload))
    await conn.commit()

    res = await list_charts("s1", db=conn)
    by_id = {i["id"]: i for i in res["items"]}

    assert by_id["c1"]["code"] == "flowchart TD\n    A --> B"
    assert by_id["c2"]["code"] == "graph TD\n    X --> Y"
    assert json.loads(by_id["c3"]["code"])["phases"] == ["P1"]
