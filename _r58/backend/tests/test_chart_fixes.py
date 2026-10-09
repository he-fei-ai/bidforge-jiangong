# -*- coding: utf-8 -*-
"""图表生成/渲染深度修复回归测试。

覆盖本轮探索定位并修复的缺陷（每条测试在修复前均应失败）：
1. Mermaid HTTP 原生渲染主路径（协程被重复 await → 必然失败）
2. 劳动力图堆叠柱状图各工种色带被顶层覆盖
3. 导出侧图表类型映射（pie / xychart → comparison）与载荷规范解析（信封承载 data）
4. 甘特图备注区丢失 + 依赖 ID 类型不匹配导致依赖箭头缺失
5. 流程图 v2 箭头几何未随 2.5x 超采样缩放
"""
import json
from io import BytesIO

from PIL import Image

# ---------- 1. HTTP 原生渲染主路径 ----------


def test_http_primary_path_renders_once(monkeypatch):
    """回归：HTTP Service（Mermaid 原生引擎）为第一优先路径，不得因协程被
    二次 await 抛 RuntimeError 而**每一次尝试都失败**。"""
    import app.services.ai.mermaid_renderer as mr

    buf = BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
    png = buf.getvalue()

    calls = {"n": 0}

    class _FakeClient:
        async def render_mermaid_via_http(self, code, theme="neutral", scale=2):
            calls["n"] += 1
            return png

    monkeypatch.setattr(mr, "get_mermaid_service_client", lambda *a, **k: _FakeClient())

    res = mr.render_mermaid_to_bytes(
        'flowchart TD\n    A["开始"] --> B["结束"]',
        "flowchart",
        skip_http=False,
        allow_pil=False,  # 不降级：只有 HTTP 主路径可用时才会成功
    )
    assert res is not None, "HTTP 主路径失败（协程很可能被重复 await）"
    assert calls["n"] == 1, f"HTTP 渲染应只调用一次，实际 {calls['n']} 次"


# ---------- 2. 劳动力堆叠柱状图 ----------


def test_labor_stacked_bars_keep_each_category_color():
    """回归：堆叠柱每段色带只覆盖自身高度，不得被后画的工种整条覆盖
    （否则整根柱子只剩最顶层工种一种颜色，各工种分布丢失）。"""
    from app.services.ai.mermaid_labor import _render_labor_image_v2

    data = {
        "type": "labor",
        "phases": ["主体阶段"],
        "categories": ["钢筋工", "木工", "混凝土工"],
        "data": [[10, 10, 10]],
    }
    img = Image.open(_render_labor_image_v2(data)).convert("RGB")

    scale = 2.5
    right_x0 = int((24 + 380 + 24) * scale)
    sc_w = int(720 * scale - 80 * scale)
    sc_y0 = int(60 * scale) + int(24 * scale) + int(32 * scale)
    sc_h = int(220 * scale) - int(50 * scale)
    base = sc_y0 + sc_h
    cx = right_x0 + int(sc_w * 0.15) + 40  # 柱体内部取样点

    def sample(frac_from_bottom):
        return img.getpixel((cx, int(base - sc_h * frac_from_bottom)))

    # 三段各占 1/3，取各段中心（1/6、1/2、5/6）避免取样到白色分隔线
    assert sample(1 / 6) == (43, 87, 154), "底部色带不是钢筋工颜色（被覆盖）"
    assert sample(3 / 6) == (232, 163, 61), "中部色带不是木工颜色（被覆盖）"
    assert sample(5 / 6) == (112, 173, 71), "顶部色带不是混凝土工颜色"


# ---------- 3. 导出侧类型映射与载荷解析 ----------


def test_export_mermaid_type_map_normalizes_pie_and_xychart():
    """回归：内联 ```mermaid pie/xychart``` 必须归一为渲染器认识的 comparison。"""
    from app.routers.export import _MERMAID_TYPE_MAP

    assert _MERMAID_TYPE_MAP["pie"] == "comparison"
    assert _MERMAID_TYPE_MAP["xychart"] == "comparison"
    assert _MERMAID_TYPE_MAP["xychart-beta"] == "comparison"


def test_chart_payload_reads_envelope_data():
    """回归：信封承载结构化数据（{"mermaid_code":"", "data":{...}}）必须能被规范解析器
    读出，供导出 chart_lookup 使用（旧实现只读 mermaid_code，数据型图表会被静默丢弃）。"""
    from app.services.chart_payload import build_chart_envelope, extract_chart_payload

    data = {"type": "labor", "phases": ["A"], "categories": ["B"], "data": [[1]]}
    env = build_chart_envelope(data=data, title="劳动力配置")
    got = extract_chart_payload(env)
    assert got.lstrip().startswith("{"), f"未能读出数据载荷: {got!r}"
    assert json.loads(got) == data


# ---------- 4. 甘特图备注与依赖连线 ----------


def _gantt_plan(**overrides):
    plan = {
        "title": "施工进度计划",
        "totalDays": 30,
        "tasks": [
            {"id": 1, "name": "基础施工", "start": 1, "end": 10},
            {"id": 2, "name": "主体施工", "start": 11, "end": 30},
        ],
    }
    plan.update(overrides)
    return plan


def test_gantt_remark_region_rendered():
    """回归：任务 remark 必须被保留并渲染出备注区（旧实现规范化时丢弃 remark，
    备注区高度恒为 0，备注从未出现）。"""
    from app.services.ai.mermaid_gantt import _render_gantt_image_v2

    plan_no_remark = _gantt_plan()
    plan_with_remark = _gantt_plan(
        tasks=[
            {"id": 1, "name": "基础施工", "start": 1, "end": 10, "remark": "春节停工 7 天"},
            {"id": 2, "name": "主体施工", "start": 11, "end": 30},
        ]
    )
    h0 = Image.open(_render_gantt_image_v2(plan_no_remark)).size[1]
    h1 = Image.open(_render_gantt_image_v2(plan_with_remark)).size[1]
    assert h1 > h0, f"备注区未生效（高度 {h0} -> {h1}）"


def test_gantt_dependency_drawn_across_id_type_mismatch():
    """回归：任务 id 为数字、dependencies 为数字字符串时依赖箭头也必须绘制。"""
    from app.services.ai.mermaid_gantt import _render_gantt_image_v2

    without_dep = _gantt_plan()
    with_dep = _gantt_plan(
        tasks=[
            {"id": 1, "name": "基础施工", "start": 1, "end": 10},
            {"id": 2, "name": "主体施工", "start": 11, "end": 30, "dependencies": ["1"]},
        ]
    )
    a = _render_gantt_image_v2(without_dep).getvalue()
    b = _render_gantt_image_v2(with_dep).getvalue()
    assert a != b, "依赖箭头缺失（int id 与 str 依赖类型不匹配）"


# ---------- 5. 流程图箭头缩放 ----------


def test_flowchart_arrow_scaled_with_supersample():
    """回归：2.5x 超采样画布上箭头几何必须同步放大，
    否则箭头底边仅约 14px、几乎与 5px 线宽重合，看不出方向。"""
    from app.services.ai.mermaid_flowchart import _render_flowchart_image_v2

    img = Image.open(
        _render_flowchart_image_v2('flowchart TD\n    A["开始"] --> B["结束"]')
    ).convert("RGB")
    arrow_color = (90, 124, 168)  # #5A7CA8（2026-09-18 样张改版后连线色）

    # 两节点之间的连线段（A 底边 y≈390，B 顶边 y≈530）内，找箭头色最长连续横向宽度
    best = 0
    for y in range(392, 528):
        xs = [x for x in range(img.width) if img.getpixel((x, y)) == arrow_color]
        if not xs:
            continue
        run = 1
        for left, right in zip(xs, xs[1:]):
            run = run + 1 if right == left + 1 else 1
            best = max(best, run)
        best = max(best, run)

    # 修复后箭头底边宽 = 2 * int(7 * 2.5) = 34px；线宽仅 int(2 * 2.5) = 5px
    assert best >= 25, f"箭头未随超采样缩放（最长连续宽度={best}，期望>=25）"


# ===========================================================================
# 第二轮：图表生成与渲染链路深度修复
# ===========================================================================


# ---------- 6. Mermaid 关键字→类型映射唯一化 ----------


def test_mermaid_keyword_map_is_single_source_of_truth():
    """回归：映射表曾在 3 处各维护一份（8/20/6 条）且互相不一致。
    现必须全部指向 chart_validators 的唯一表。"""
    from app.routers._chart_pipeline import _MERMAID_TYPE_MAP as pipeline_map
    from app.routers.export import _MERMAID_TYPE_MAP as export_map
    from app.services.chart_validators import (
        MERMAID_KEYWORD_TO_CHART_TYPE,
        detect_mermaid_chart_type,
    )

    assert export_map is MERMAID_KEYWORD_TO_CHART_TYPE
    assert pipeline_map is MERMAID_KEYWORD_TO_CHART_TYPE

    # 曾经被漏掉/丢弃的类型必须可识别
    for keyword, expected in (
        ("mindmap", "mindmap"),
        ("journey", "journey"),
        ("sequenceDiagram", "sequence"),
        ("classDiagram", "class"),
        ("erDiagram", "er"),
        ("quadrantChart", "quadrant"),
        ("gitGraph", "git"),
        ("sankey-beta", "sankey"),
        ("block-beta", "block"),
        ("kanban", "kanban"),
        ("xychart-beta", "comparison"),
    ):
        assert detect_mermaid_chart_type(keyword, default="") == expected, keyword

    # 注释行必须跳过；未知关键字返回 default
    assert detect_mermaid_chart_type(
        "%%{init: {'theme':'neutral'}}%%\nsequenceDiagram\n  A->>B: hi"
    ) == "sequence"
    assert detect_mermaid_chart_type("unknownDiagram\n  x", default="") == ""


def test_validate_mermaid_accepts_full_keyword_set():
    """回归：mindmap / journey / xychart / gitGraph / sankey / block / kanban /
    quadrantChart 此前全部落到兜底分支返回"不支持的 Mermaid 类型"。"""
    from app.services.ai.image_engine import validate_mermaid

    for code in (
        "mindmap\n  root((项目部))\n    工程部",
        "journey\n  title 施工流程\n  section 准备\n    测量: 5: 施工员",
        "gitGraph\n  commit\n  branch develop\n  commit",
        "sankey-beta\n  A,B,10\n  B,C,5",
        "block-beta\n  columns 2\n  A B",
        "kanban\n  todo[待办]\n    task1[测量放线]",
        "quadrantChart\n  title 风险\n  x-axis 低 --> 高\n  y-axis 低 --> 高\n  A: [0.3, 0.6]",
        "xychart-beta\n  x-axis [1, 2, 3]\n  bar [10, 20, 30]",
    ):
        ok, msg = validate_mermaid(code)
        assert ok is True, f"{code.splitlines()[0]} 被判为非法: {msg}"

    # 首行注释不应导致类型判定全部落空
    ok, msg = validate_mermaid(
        "%% 施工流程\nflowchart LR\n    A --> B\n    B --> C")
    assert ok is True, f"注释开头的流程图被判为非法: {msg}"

    # 真正不支持的仍是失败
    assert validate_mermaid("nonsenseDiagram\n  x")[0] is False


def test_extract_inline_charts_registers_formerly_dropped_types():
    """回归：mindmap / journey 内联图表此前从不被登记（图表清单里查不到）。"""
    from app.routers._chart_pipeline import extract_inline_charts

    content = (
        "正文\n```mermaid\nmindmap\n  root((项目部))\n    工程部\n```\n\n"
        "```mermaid\njourney\n  title 流程\n  section 准备\n    测量: 5: 施工员\n```"
    )
    types = [ct for ct, _ in extract_inline_charts(content)]
    assert "mindmap" in types, f"mindmap 未被登记: {types}"
    assert "journey" in types, f"journey 未被登记: {types}"


# ---------- 7. 内联图表校验：JSON 数据块不再被误删 ----------


def test_validate_inline_chart_accepts_labor_and_architecture_json():
    """回归：```chart-json 的 labor / architecture 块此前落到 mermaid 校验 →
    必然失败 → 触发"校验失败删块"，AI 生成的劳动力图/架构图数据被整块删除。"""
    from app.routers._chart_pipeline import _validate_inline_chart

    labor = json.dumps({
        "type": "labor", "phases": ["基础"], "categories": ["钢筋工"], "data": [[10]],
    }, ensure_ascii=False)
    ok, code = _validate_inline_chart("labor", labor)
    assert ok is True and code == labor

    # 渲染器认识的"字符串根 + nodes 列表"形态（校验器此前只认顶层 label/name）
    arch = json.dumps({"type": "architecture", "root": "项目部",
                       "nodes": [{"label": "工程部"}]}, ensure_ascii=False)
    ok, code = _validate_inline_chart("architecture", arch)
    assert ok is True and code == arch


def test_validate_inline_chart_keeps_mermaid_pie():
    """回归：分流必须按载荷形态而不是 chart_type ——
    `pie` 与 `{"type":"comparison"}` 都归一为 comparison，若按类型分流，
    Mermaid 语法的饼图会被当成 JSON 解析失败而删块。"""
    from app.routers._chart_pipeline import _validate_inline_chart

    pie = "pie title 方案比选\n    \"方案A\" : 40\n    \"方案B\" : 60"
    ok, code = _validate_inline_chart("comparison", pie)
    assert ok is True, "Mermaid 饼图被误判为 JSON 载荷"
    assert code == pie

    cmp_json = json.dumps({"type": "comparison", "items": [
        {"label": "方案A", "value": 40}, {"label": "方案B", "value": 60}]})
    ok, _ = _validate_inline_chart("comparison", cmp_json)
    assert ok is True


async def test_register_inline_charts_writes_canonical_envelope(db_conn):
    """回归：登记落库必须走 chart_payload 的规范信封 ——
    JSON 数据型走 data 分支（旧实现塞进 mermaid_code，形状被判为 envelope:mermaid）。"""
    from app.routers._chart_pipeline import register_inline_charts
    from app.services.chart_payload import chart_payload_shape, extract_chart_payload

    labor = json.dumps({
        "type": "labor", "title": "劳动力配置",
        "phases": ["基础阶段"], "categories": ["钢筋工"], "data": [[10]],
    }, ensure_ascii=False)
    arch = json.dumps({"type": "architecture", "root": "项目部",
                       "nodes": [{"label": "工程部"}]}, ensure_ascii=False)
    content = (
        '正文\n```mermaid\nflowchart TD\n    A --> B\n    B --> C\n```\n\n'
        f"```chart-json\n{labor}\n```\n\n"
        f"```chart-json\n{arch}\n```"
    )
    n, new_content = await register_inline_charts(db_conn, "s1", "c1", content)
    assert n == 3, f"登记数不符（正文块被误删？）实际 {n}；正文={new_content!r}"
    assert new_content == content, "校验通过不应改动正文"

    cur = await db_conn.execute(
        "SELECT chart_type, data_json FROM chart_predictions WHERE section_id='c1'")
    payloads = {r["chart_type"]: r["data_json"] for r in await cur.fetchall()}
    assert chart_payload_shape(payloads["flowchart"]) == "envelope:mermaid"
    assert chart_payload_shape(payloads["labor"]) == "envelope:data"
    assert chart_payload_shape(payloads["architecture"]) == "envelope:data"
    assert json.loads(extract_chart_payload(payloads["labor"]))["phases"] == ["基础阶段"]


# ---------- 8. 甘特图解析 ----------


def test_gantt_zero_duration_is_milestone():
    """回归：`0d` 是 Mermaid 表达里程碑的标准写法，旧实现 `if duration else 5`
    把 0 当"未给出工期" → 里程碑被画成 5 天的任务条。"""
    from app.services.ai.mermaid_gantt import _parse_mermaid_gantt

    tasks = _parse_mermaid_gantt(
        "gantt\n"
        "    dateFormat YYYY-MM-DD\n"
        "    section 准备\n"
        "    开工 :m1, 2026-01-01, 0d\n"
        "    基础施工 :a1, 2026-01-05, 10d"
    )
    assert tasks is not None
    assert tasks[0]["start"] == tasks[0]["end"] == 1, f"里程碑被当成多天任务: {tasks[0]}"
    assert tasks[1]["start"] == 5 and tasks[1]["end"] == 14, f"普通任务工期错误: {tasks[1]}"


def test_gantt_explicit_start_and_end_dates():
    """回归：`任务 :id, 开始日期, 结束日期` 写法下，旧实现用同一个变量循环覆盖，
    结束日期会把开始日期覆盖掉 —— 任务被挪到结束日那天下发、工期退回默认 5 天。"""
    from app.services.ai.mermaid_gantt import _parse_mermaid_gantt

    tasks = _parse_mermaid_gantt(
        "gantt\n"
        "    dateFormat YYYY-MM-DD\n"
        "    基础施工 :a1, 2026-03-01, 2026-03-10\n"
        "    主体施工 :a2, 2026-03-11, 2026-04-09"
    )
    assert tasks is not None
    assert tasks[0]["start"] == 1 and tasks[0]["end"] == 10, f"起止日期解析错误: {tasks[0]}"
    assert tasks[1]["start"] == 11 and tasks[1]["end"] == 40, f"起止日期解析错误: {tasks[1]}"


# ---------- 9. 流程图方向 / 链式连线 ----------


def test_flowchart_chained_edges_parsed():
    """回归：同一行链式连线 `A --> B --> C` 此前只解析出 A→B，
    右半段被注册成幽灵节点 `B["基础"] --> C["竣工"]`，B→C 的边被丢弃。"""
    from app.services.ai.mermaid_flowchart import _parse_flowchart_structure

    parsed = _parse_flowchart_structure(
        'flowchart TD\n    A["开工"] --> B["基础"] --> C["竣工"]')
    assert [n["id"] for n in parsed["nodes"]] == ["A", "B", "C"], \
        f"节点 ID 解析异常: {[n['id'] for n in parsed['nodes']]}"
    assert {n["id"]: n["label"] for n in parsed["nodes"]} == \
        {"A": "开工", "B": "基础", "C": "竣工"}, \
        f"节点标签解析异常: {[(n['id'], n['label']) for n in parsed['nodes']]}"
    assert {(e["from"], e["to"]) for e in parsed["edges"]} == {("A", "B"), ("B", "C")}, \
        f"边解析异常: {parsed['edges']}"


def test_flowchart_reversed_directions_are_honored():
    """回归：RL / BT 此前被当作 LR / TD 渲染（方向形同虚设）。"""
    from app.services.ai.mermaid_flowchart import _render_flowchart_image_v2

    body = '    A["开工"] --> B["基础"]\n    B["基础"] --> C["竣工"]'

    def render(d: str) -> bytes:
        return _render_flowchart_image_v2(f"flowchart {d}\n{body}").getvalue()

    assert render("TD") != render("BT"), "flowchart BT 被当作 TD 渲染（方向未生效）"
    assert render("LR") != render("RL"), "flowchart RL 被当作 LR 渲染（方向未生效）"


# ---------- 10. 架构图根节点样式 ----------


def _count_color_in_node(img, node: dict, color: tuple, scale: float = 2.5,
                         title_h: int = 56) -> int:
    x0 = int(node["x"] * scale) + 8
    y0 = int(node["y"] * scale) + title_h + 8
    x1 = int((node["x"] + node["w"]) * scale) - 8
    y1 = int((node["y"] + node["h"]) * scale) + title_h - 8
    cnt = 0
    for x in range(x0, x1):
        for y in range(y0, y1):
            if img.getpixel((x, y)) == color:
                cnt += 1
    return cnt


def test_architecture_root_node_gets_root_style():
    """回归：布局为后序遍历（子节点先 append），nodes_pos[0] 是最左侧叶子而非根，
    旧实现据此判定根节点 → 根样式被画到第一个叶子上，真正的根显示为普通节点。
    2026-09-18 样张改版：根样式 = 深藏青 #1F3864（不再是金色双框）。"""
    from app.services.ai.mermaid_architecture import _layout_architecture_tree, _render_architecture_image_v2
    from PIL import Image

    tree = {"label": "项目部", "children": [
        {"label": "工程部", "children": [{"label": "土建组"}, {"label": "安装组"}]},
        {"label": "安质部", "children": [{"label": "安全组"}, {"label": "质量组"}]},
    ]}
    nodes, _, _ = _layout_architecture_tree(tree)
    assert nodes[0]["depth"] != 0, "测试前提不成立：布局已不是后序"
    root = next(n for n in nodes if n["depth"] == 0)
    leaf = nodes[0]

    img = Image.open(_render_architecture_image_v2(tree)).convert("RGB")
    NAVY = (31, 56, 100)  # #1F3864 根节点填充色
    assert _count_color_in_node(img, root, NAVY) > 500, "真正的根节点未使用根样式（深藏青）"
    assert _count_color_in_node(img, leaf, NAVY) == 0, "根样式被错误地画到了叶子节点上"
    # 旧金色双框根样式必须彻底移除
    GOLD = (251, 191, 36)  # #FBBF24
    for n in nodes:
        assert _count_color_in_node(img, n, GOLD) == 0, f"旧金色根样式残留在 {n['label']}"


# ---------- 11. ChartCache 容量与 key 统一 ----------


def test_chart_cache_restores_state_from_disk(tmp_path):
    """回归：进程重启后必须按磁盘真实占用恢复 LRU 状态，
    否则 500MB 容量上限形同虚设（每次重启后又能再写满一轮）。"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=str(tmp_path))
    for i in range(3):
        cache.set(f"code-{i}", b"z" * 100, chart_type="flowchart")
    on_disk = sum(f.stat().st_size for f in tmp_path.glob("*.png"))
    assert on_disk > 0

    reopened = ChartCache(cache_dir=str(tmp_path))
    assert reopened._stats["total_size"] == on_disk, "重启后未按磁盘占用恢复 total_size"
    assert len([k for k in reopened._access_order if not k.endswith(".fmt")]) == 3, \
        "重启后未恢复 LRU 顺序"


def test_chart_cache_get_reads_get_or_render_entry(monkeypatch, tmp_path):
    """回归：get() 与 get_or_render() 此前用两套 key 规则，
    get() 永远读不到 get_or_render 写入的缓存。"""
    import app.services.ai.mermaid_renderer as mr

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", lambda *a, **k: BytesIO(b"PNG" * 40))
    cache = mr.ChartCache(cache_dir=str(tmp_path))
    code = "graph TD\n    A-->B"

    assert cache.get_or_render(code, "flowchart", allow_pil=True) is not None
    hit = cache.get(code, fmt="png", chart_type="flowchart", allow_pil=True)
    assert hit is not None, "get() 读不到 get_or_render 写入的缓存（key 规则未统一）"
    assert hit.read_bytes() == b"PNG" * 40


def test_mermaid_client_is_available_tristate():
    """回归：客户端缺 is_available() 导致 /charts/render 的错误提示恒报
    "HTTP Service 可用: False"，把代码语法错误误导成服务未部署。"""
    from app.services.ai.mermaid_service import MermaidServiceClient

    client = MermaidServiceClient()
    assert client.is_available() is None, "尚未探测时应返回 None"
    client._backend_available = {k: False for k in client._backend_available}
    assert client.is_available() is False
    client._backend_available[next(iter(client._backend_available))] = True
    assert client.is_available() is True


# ---------- 12. 甘特图 after 依赖解析 ----------


def test_gantt_after_dependency_resolved():
    """回归：甘特图 `after <id>` 依赖此前被完全忽略——解析器不捕获任务 id 与依赖，
    所有 `after X` 任务都被当作"无开始日期"用 cursor 顺序顺延到 day 1。
    一旦依赖任务之间夹着独立给定日期的任务，或依赖以**前向引用**出现在后面，
    依赖关系就被彻底丢失（例如 a2 after a1 排在 aX 2026-03-01 之后，a2 被推到 aX
    之后而非 a1 之后）。现应按 id 解析真实起止偏移。"""
    from app.services.ai.mermaid_gantt import _parse_mermaid_gantt

    code = (
        "gantt\n"
        "    title 施工进度计划\n"
        "    dateFormat YYYY-MM-DD\n"
        "    施工准备 :a1, 2026-01-01, 15d\n"
        "    设备采购 :aX, 2026-03-01, 10d\n"
        "    基础工程 :a2, after a1, 30d\n"
        "    主体施工 :a3, after a2, 60d\n"
        "    竣工验收 :milestone, a4, after a3, 0d"
    )
    tasks = _parse_mermaid_gantt(code)
    assert tasks is not None, "解析结果为空"
    by_name = {t["task"]: t for t in tasks}
    # 基础工程必须紧随其依赖 a1（结束于第 15 天）之后，而非被设备采购的 cursor 推后
    assert by_name["基础工程"]["start"] == 16, f"after 依赖未解析: {by_name['基础工程']}"
    assert by_name["基础工程"]["end"] == 45, f"after 依赖未解析: {by_name['基础工程']}"
    assert by_name["主体施工"]["start"] == 46, f"after 依赖未解析: {by_name['主体施工']}"
    # 里程碑（0d）应 end == start
    assert by_name["竣工验收"]["start"] == by_name["竣工验收"]["end"] == 106, \
        f"里程碑工期错误: {by_name['竣工验收']}"


def test_gantt_after_dependency_renders_valid_png():
    """回归：含 `after` 依赖的甘特图应能经完整渲染管线上屏为合法 PNG。"""
    import io

    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

    code = (
        "gantt\n"
        "    title 施工进度计划\n"
        "    dateFormat YYYY-MM-DD\n"
        "    施工准备 :a1, 2026-01-01, 15d\n"
        "    基础工程 :a2, after a1, 30d\n"
        "    主体施工 :a3, after a2, 60d\n"
        "    竣工验收 :milestone, a4, after a3, 0d"
    )
    png = render_mermaid_to_bytes(code, "gantt", skip_http=True, allow_pil=True)
    assert png is not None, "含 after 依赖的甘特图渲染失败"
    head = png.getvalue()[:8]
    assert head == b"\x89PNG\r\n\x1a\n", "渲染结果不是合法 PNG"


# ---------- 10. 图表清单 placed 语义（正文同步内联） ----------

_VALID_FLOW = "flowchart TD\n    A[\"开工\"] --> B[\"施工\"]\n    B --> C[\"验收\"]"


async def test_list_charts_placed_detects_inline_block(db_conn):
    """回归：图表编排并入正文生成后，落点是**正文内联代码块**（```mermaid /
    ```chart-json）。旧实现的 placed 只认历史 [CHART_TYPE: x] 标记，
    导致"正文已内联出图"的条目恒被判为未落位（清单口径与事实不符）。"""
    from app.routers._chart_pipeline import register_inline_charts
    from app.routers.charts import list_charts

    content = f"工艺流程：\n```mermaid\n{_VALID_FLOW}\n```\n"
    n, content = await register_inline_charts(
        db_conn, "scheme-placed", "sec-placed", content)
    assert n == 1
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, content) VALUES (?,?,?,?)",
        ("sec-placed", "scheme-placed", "工艺", content))
    await db_conn.commit()

    data = await list_charts("scheme-placed", db=db_conn)
    item = next(i for i in data["items"] if i["section_id"] == "sec-placed")
    assert item["placed"] is True, "正文内联块未被判为落位"
    assert item["chart_type"] == "flowchart"


async def test_list_charts_placed_false_without_inline_block(db_conn):
    """反向：章节正文不含内联块时 placed 必须为 False（不虚报落位）。"""
    from app.routers.charts import list_charts
    from app.services.chart_payload import build_chart_envelope

    await db_conn.execute(
        "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
        " needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
        ("p1", "sec-none", "scheme-none", "flowchart", "正文同步生成", 5, "done",
         build_chart_envelope(code=_VALID_FLOW)))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, content) VALUES (?,?,?,?)",
        ("sec-none", "scheme-none", "无图章节", "纯文字，没有图。"))
    await db_conn.commit()

    data = await list_charts("scheme-none", db=db_conn)
    assert data["items"][0]["placed"] is False


async def test_list_charts_placed_legacy_marker(db_conn):
    """兼容：历史 [CHART_TYPE: x] 标记仍应被判为落位。"""
    from app.routers.charts import list_charts
    from app.services.chart_payload import build_chart_envelope

    await db_conn.execute(
        "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
        " needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
        ("p2", "sec-mark", "scheme-mark", "gantt", "历史", 5, "done",
         build_chart_envelope(code="gantt\n    dateFormat X\n    A :a1, 0, 5")))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, content) VALUES (?,?,?,?)",
        ("sec-mark", "scheme-mark", "进度", "前文\n\n[CHART_TYPE: gantt]\n"))
    await db_conn.commit()

    data = await list_charts("scheme-mark", db=db_conn)
    assert data["items"][0]["placed"] is True
