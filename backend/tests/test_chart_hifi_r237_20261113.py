"""R237（2026-11-13）· 高保真图表与图片生成四增强护栏。

用户指令「读取文档，增强图片生成；增强图表生成关键词；增强提示词模块；
增强导出图表渲染」落地后，本文件锁定下列不变式：

  A · 图表生成关键词（chart_validators）：
      `detect_chart_type` 只返回 3 类高保真类型（甘特/组织/工艺），
      强/中词触发、负向词排除；`infer_chart_type` 旧兜底接口原样保留。
  B · 提示词模块（charts.py）：3 个七段式生成提示词注册且可检索，
      均含 `{scheme_type}` 变量；既有修复提示词（chart_mermaid_fix）不动。
  C · 图片生成（image_engine）：DPI 预设 / 协同决策 / Prompt 模板解析。
  D · 导出图表渲染（hifi_charts）：schemas fail-closed 校验、
      exporter 双轨接口、模板含文档关键视觉要素（3D 硬阴影/直角布线/三色块）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WS_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _WS_ROOT / "backend" / "app" / "services" / "ai" / "hifi_charts" / "templates"


# ===========================================================================
# A · 图表生成关键词增强
# ===========================================================================


def _validators():
    from app.services import chart_validators as C

    return C


def test_keyword_routes_structure():
    C = _validators()
    routes = C.CHART_KEYWORD_ROUTES
    # 恰好三类路由，且键 = 既有类型词汇（不另立第三套类型词汇）
    assert set(routes.keys()) == {"gantt", "architecture", "flowchart"}
    for key, spec in routes.items():
        assert "strong" in spec and isinstance(spec["strong"], (list, tuple))
        assert "medium" in spec and isinstance(spec["medium"], (list, tuple))
        assert "negative" in spec and isinstance(spec["negative"], (list, tuple))
        assert spec["strong"], f"{key} 缺强触发词"


def test_detect_chart_type_strong_routes():
    C = _validators()
    assert C.detect_chart_type("本工程横道图施工进度计划") == "gantt"
    assert C.detect_chart_type("项目组织架构图与职能部门设置") == "architecture"
    assert C.detect_chart_type("钢结构加工工艺流程与多支线工序流转") == "flowchart"


def test_detect_chart_type_negative_excludes():
    C = _validators()
    # 负向排除优先：强触发 + 负向词 → type None（rules 版本）
    assert C.detect_chart_type("施工进度计划 施工顺序") == "gantt"  # 裸 detect 仍强触发
    r = C.detect_chart_type_with_rules("施工进度计划 施工顺序")
    assert r["type"] is None and r["trigger"] == "negative"
    assert r["negative_hit"] == "施工顺序"


def test_detect_chart_type_no_match():
    C = _validators()
    assert C.detect_chart_type("本工程位于城北，场地平整") is None
    assert C.detect_chart_type("") is None


def test_detect_chart_type_medium_routes():
    C = _validators()
    # medium 词（如「工期安排」）亦能触发，且避开 negative
    r = C.detect_chart_type_with_rules("工期安排需合理")
    assert r["type"] == "gantt" and r["trigger"] == "medium"


def test_detect_chart_type_only_known_types():
    C = _validators()
    # 返回值闭集 = 既有类型词汇（不引入第四种）
    for text in ("横道图", "组织架构图", "工艺流程图", "施工进度"):
        t = C.detect_chart_type(text)
        if t is not None:
            assert t in {"gantt", "architecture", "flowchart"}


def test_infer_chart_type_unchanged_fallback():
    C = _validators()
    # 旧兜底接口原样保留（零消费，不删除不修改）
    assert callable(getattr(C, "infer_chart_type", None))
    assert callable(getattr(C, "detect_chart_type_with_rules", None))


# ===========================================================================
# B · 提示词模块增强
# ===========================================================================


@pytest.fixture(scope="module")
def registry():
    from app.services.ai.prompts import _registry as R

    R.register_lazy_prompts()
    return R


_GEN_NAMES = (
    "chart_gen_gantt_spec",
    "chart_gen_org_matrix_spec",
    "chart_gen_flow_complex_spec",
)


def test_three_gen_prompts_registered(registry):
    for name in _GEN_NAMES:
        p = registry.get_prompt(name)
        assert p, f"{name} 未注册"
        assert len(p) > 500, f"{name} 过短（疑似空壳）"


def test_three_gen_prompts_seven_segments(registry):
    segments = ("【角色】", "【任务】", "【输出契约】", "【约束", "【反幻觉", "【少样本】", "【自检")
    for name in _GEN_NAMES:
        p = registry.get_prompt(name)
        for seg in segments:
            assert seg in p, f"{name} 缺段落 {seg}"


def test_three_gen_prompts_have_scheme_type_var(registry):
    for name in _GEN_NAMES:
        p = registry.get_prompt(name)
        assert "{scheme_type}" in p, name + " 缺 {scheme_type} 变量"


def test_existing_fix_prompts_intact(registry):
    # 既有修复提示词（mermaid 修复类）原样保留，不因本轮新增被覆盖
    for name in ("chart_mermaid_fix",):
        p = registry.get_prompt(name)
        assert p, f"{name} 被破坏或删除"


# ===========================================================================
# C · 图片生成增强
# ===========================================================================


def _image_engine():
    from app.services.ai import image_engine as I

    return I


def test_image_scale_presets():
    I = _image_engine()
    assert I.IMAGE_SCALE_PRESETS == {"screen": 2, "print": 3, "vector": 4}


def test_route_visual_strategy_structured():
    I = _image_engine()
    r = I.route_visual_strategy("本章含钢结构加工工艺流程")
    assert r["route"] == "structured"
    assert r["chart_type"] in {"gantt", "architecture", "flowchart"}
    assert r["reason"]


def test_route_visual_strategy_illustration():
    I = _image_engine()
    r = I.route_visual_strategy("需配一张三维实景效果图")
    assert r["route"] == "illustration"
    assert r["chart_type"] is None


def test_route_visual_strategy_none():
    I = _image_engine()
    r = I.route_visual_strategy("本工程位于城北，场地平整，工期合理")
    assert r["route"] == "none"


def test_route_visual_strategy_uses_detect_chart_type():
    # 协同决策必须内部引用 detect_chart_type（单一事实源，非各写一份）
    import inspect

    I = _image_engine()
    src = inspect.getsource(I.route_visual_strategy)
    assert "detect_chart_type" in src


def test_parse_img_gen_prompt_hit():
    I = _image_engine()
    r = I.parse_img_gen_prompt(
        "[IMG_GEN_PROMPT: 主题=钢结构节点焊接三维示意；风格=写实工程渲染；"
        "构图=等轴测；比例=16:9；负面词=手绘、模糊]"
    )
    assert r["ok"] is True
    assert r["subject"] == "钢结构节点焊接三维示意"
    assert r["ratio"] == "16:9"
    assert "钢结构节点焊接" in r["prompt"]


def test_parse_img_gen_prompt_miss():
    I = _image_engine()
    r = I.parse_img_gen_prompt("无模板标记的普通正文")
    assert r["ok"] is False
    assert "未命中" in r["reason"]


# ===========================================================================
# D · 导出图表渲染增强（hifi_charts）
# ===========================================================================


def _hifi():
    from app.services.ai.hifi_charts import exporter as X
    from app.services.ai.hifi_charts import schemas as S

    return X, S


def test_schema_enum_locks():
    _, S = _hifi()
    assert S.GANTT_TYPES == frozenset({"primary", "secondary", "light"})
    assert S.ORG_LEVEL_KEYS == ("level1", "level2", "level3", "level4")
    assert S.FLOW_TYPES == frozenset({"primary", "warning", "success"})


def _gantt_ok() -> dict:
    return {
        "title": "施工进度横道图",
        "start_date": "2026-12-10",
        "end_date": "2027-01-08",
        "total_days": 30,
        "tasks": [
            {"id": "t1", "label": "施工准备", "start": "2026-12-10", "end": "2026-12-19", "duration": 10, "type": "primary"},
            {"id": "t2", "label": "主体施工", "start": "2026-12-20", "end": "2026-12-29", "duration": 10, "type": "primary"},
            {"id": "t3", "label": "装饰装修", "start": "2026-12-30", "end": "2027-01-08", "duration": 10, "type": "secondary"},
        ],
        "dependencies": [{"from": "t1", "to": "t2", "lag": 1}],
    }


def test_validate_gantt_ok():
    _, S = _hifi()
    ok, msg = S.validate_gantt_spec(_gantt_ok())
    assert ok, msg


def test_validate_gantt_fail_closed():
    _, S = _hifi()
    bad = _gantt_ok()
    bad["total_days"] = 31  # 不自洽
    ok, msg = S.validate_gantt_spec(bad)
    assert not ok and "不自治" in msg
    # 越界日期
    bad2 = _gantt_ok()
    bad2["tasks"][0]["end"] = "2027-02-01"
    ok2, msg2 = S.validate_gantt_spec(bad2)
    assert not ok2


def test_validate_org_ok_and_parent_null():
    _, S = _hifi()
    data = {
        "title": "项目组织架构",
        "nodes": {
            "level1": [{"id": "pm", "label": "项目经理"}],
            "level2": [{"id": "m1", "label": "技术组"}],
            "level3": [
                {"id": "s1", "label": "深化设计", "parent": "m1"},
                {"id": "s2", "label": "综合办", "parent": None},  # 穿越中层直连
            ],
            "level4": [],
        },
    }
    ok, msg = S.validate_org_matrix_spec(data)
    assert ok, msg


def test_validate_org_fail_closed():
    _, S = _hifi()
    bad = {
        "nodes": {
            "level1": [],
            "level2": [],
            "level3": [],
            "level4": [],
        }
    }
    ok, msg = S.validate_org_matrix_spec(bad)
    assert not ok and "根节点" in msg
    # parent 引用不存在 id
    bad2 = {
        "nodes": {
            "level1": [{"id": "pm", "label": "PM"}],
            "level2": [{"id": "m1", "label": "技术组"}],
            "level3": [{"id": "s1", "label": "深化", "parent": "ghost"}],
            "level4": [],
        }
    }
    ok2, msg2 = S.validate_org_matrix_spec(bad2)
    assert not ok2


def _flow_ok() -> dict:
    return {
        "title": "钢结构加工工艺流程图",
        "nodes": [
            {"id": "n1", "label": "深化设计", "x": 260, "y": 50, "type": "primary"},
            {"id": "n2", "label": "工艺评定", "x": 80, "y": 60, "type": "warning"},
            {"id": "n3", "label": "钢板预处理", "x": 260, "y": 120, "type": "primary"},
        ],
        "links": [
            {"from": "n1", "to": "n3", "path": "h-v-h"},
            {"from": "n2", "to": "n3", "path": "h-v-h"},
        ],
    }


def test_validate_flow_ok():
    _, S = _hifi()
    ok, msg = S.validate_flow_complex_spec(_flow_ok())
    assert ok, msg


def test_validate_flow_fail_closed():
    _, S = _hifi()
    # 闭环引用 → 拒绝出图（死循环）
    bad = _flow_ok()
    bad["links"] = [
        {"from": "n1", "to": "n2", "path": "h"},
        {"from": "n2", "to": "n3", "path": "h"},
        {"from": "n3", "to": "n1", "path": "h"},  # 环
    ]
    ok, msg = S.validate_flow_complex_spec(bad)
    assert not ok and "闭环" in msg
    # 坐标越界
    bad2 = _flow_ok()
    bad2["nodes"][0]["x"] = 999
    ok2, msg2 = S.validate_flow_complex_spec(bad2)
    assert not ok2
    # 孤立引用
    bad3 = _flow_ok()
    bad3["links"] = [{"from": "n1", "to": "ghost", "path": "h"}]
    ok3, msg3 = S.validate_flow_complex_spec(bad3)
    assert not ok3


def test_exporter_surface():
    X, _ = _hifi()
    assert hasattr(X.ChartExporter, "to_png")
    assert hasattr(X.ChartExporter, "to_pdf")
    assert hasattr(X.ChartExporter, "to_svg")
    assert hasattr(X.ChartExporter, "batch_export")
    assert hasattr(X.ChartExporter, "render_code_to_png")
    assert X.ChartExporter.SCALE_PRESETS == {"screen": 2, "print": 3, "vector": 4}


def test_inject_chart_data_hit_and_miss():
    X, _ = _hifi()
    tpl = "<script>const data = {...};</script>"
    out = X.inject_chart_data(tpl, {"title": "T"})
    assert 'const data = {"title": "T"};' in out
    # 未命中占位符 → 原样返回（不静默破坏模板）
    no_ph = "<script>const x = 1;</script>"
    assert X.inject_chart_data(no_ph, {"a": 1}) == no_ph


def test_templates_exist_and_contain_elements():
    g = (_TEMPLATES / "gantt.html").read_text(encoding="utf-8")
    o = (_TEMPLATES / "org_matrix.html").read_text(encoding="utf-8")
    f = (_TEMPLATES / "flow_complex.html").read_text(encoding="utf-8")
    # 甘特：里程碑菱形 + 搭接倒三角 + 依赖虚线
    assert "diamond" in g or "milestone" in g.lower() or "◆" in g
    assert "lag" in g or "搭接" in g
    assert "dependency" in g
    # 组织图：level 层级 + 总线 + parent:null 穿越直连
    assert "level3" in o or "level4" in o
    assert "parent" in o
    assert "bus" in o or "busY" in o
    # 工艺图：3D 硬阴影 + 黑边 + 直角布线提示 + 三色块
    assert "box-shadow: 4px 4px 0 #a0a0a0" in f
    assert "border: 2px solid #000000" in f
    assert "h-v-h" in f and "v-h-v" in f
    assert "t-primary" in f and "t-warning" in f and "t-success" in f
    assert "#00ffff" in f and "#fce4d6" in f and "#e2f0d9" in f


# ===========================================================================
# E · 渲染边角与 fail-closed 加固（Playwright 轨；无 playwright 则跳过）
# ===========================================================================


def _gantt_nodeps() -> dict:
    return {
        "title": "横道图",
        "start_date": "2026-12-10",
        "end_date": "2027-01-08",
        "total_days": 30,
        "tasks": [
            {"id": "t1", "label": "准备", "start": "2026-12-10", "end": "2026-12-19", "duration": 10, "type": "primary"},
        ],
        "dependencies": [],
    }


def test_to_svg_source_has_fail_closed_check():
    """加固锁：`to_svg` 必须含「无实质绘制内容 → 抛错」判定（防止空白矢量静默写盘）。"""
    import inspect

    X, _ = _hifi()
    src = inspect.getsource(X.ChartExporter.to_svg)
    assert "无实质绘制内容" in src
    assert "raise RuntimeError" in src


@pytest.mark.asyncio
async def test_to_svg_flow_has_content(tmp_path):
    pytest.importorskip("playwright")
    from app.services.ai.hifi_charts.exporter import ChartExporter, inject_chart_data

    html = inject_chart_data((_TEMPLATES / "flow_complex.html").read_text(encoding="utf-8"), _flow_ok())
    svg_path = await ChartExporter().to_svg(html, str(tmp_path / "flow.svg"), ".flow-container")
    s = Path(svg_path).read_text(encoding="utf-8")
    assert "<path" in s  # 连线路径在场


@pytest.mark.asyncio
async def test_to_svg_empty_shell_raises(tmp_path):
    pytest.importorskip("playwright")
    from app.services.ai.hifi_charts.exporter import ChartExporter, inject_chart_data

    html = inject_chart_data((_TEMPLATES / "gantt.html").read_text(encoding="utf-8"), _gantt_nodeps())
    with pytest.raises(RuntimeError, match="无实质绘制内容"):
        await ChartExporter().to_svg(html, str(tmp_path / "empty.svg"), ".gantt-container")


@pytest.mark.asyncio
async def test_render_edge_cases(tmp_path):
    """加固锁：单节点/自动路由/空 level4 均能出非空白图（模板不崩不空白）。"""
    pytest.importorskip("playwright")
    from app.services.ai.hifi_charts.exporter import ChartExporter, inject_chart_data

    ex = ChartExporter(scale=2)
    flow_tpl = (_TEMPLATES / "flow_complex.html").read_text(encoding="utf-8")
    # 单节点无连线
    fd1 = {"title": "单节点", "nodes": [{"id": "n1", "label": "深化", "x": 260, "y": 50, "type": "primary"}], "links": []}
    r1 = await ex.to_png(inject_chart_data(flow_tpl, fd1), str(tmp_path / "f1.png"), ".flow-container")
    assert Path(r1).stat().st_size > 1000
    # 自动路由（无 path 提示）
    fd2 = {"title": "自动", "nodes": [
        {"id": "a", "label": "输入", "x": 260, "y": 60, "type": "primary"},
        {"id": "b", "label": "支线", "x": 120, "y": 200, "type": "warning"},
        {"id": "c", "label": "输出", "x": 260, "y": 340, "type": "success"}],
        "links": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}
    r2 = await ex.to_png(inject_chart_data(flow_tpl, fd2), str(tmp_path / "f2.png"), ".flow-container")
    assert Path(r2).stat().st_size > 1000
    # org level4 为空
    od = {"title": "组织", "nodes": {"level1": [{"id": "pm", "label": "PM"}],
          "level2": [{"id": "m1", "label": "技术组"}],
          "level3": [{"id": "s1", "label": "深化", "parent": "m1"}], "level4": []}}
    r3 = await ex.to_png(inject_chart_data((_TEMPLATES / "org_matrix.html").read_text(encoding="utf-8"), od), str(tmp_path / "o1.png"), ".org-chart-container")
    assert Path(r3).stat().st_size > 1000
