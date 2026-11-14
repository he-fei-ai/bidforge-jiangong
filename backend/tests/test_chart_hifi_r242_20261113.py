# -*- coding: utf-8 -*-
"""R242（2026-11-13 · hifi 通道接线收口）护栏。

收口 R237 四增强的「服务层就绪 ≠ 功能可达」：
- FIX-A：三个 chart_gen_* 提示词产出契约主链化（``<!-- AI_Chart_Start -->`` +
  ``[CHART_TYPE: 中文]`` + ``[DATA:`` 是 2026-09-13 已删除的 Phase2/Phase4 管线
  死路标记——主链只认三围栏，该形态整块不可见）→ `` ```chart-json `` 围栏形态。
- FIX-B：主链 hifi 载荷通道（infer 补 nodes.level1→architecture；
  normalize_architecture_tree 补矩阵总线归一；_build_gantt_tasks 补 duration
  别名——PIL 降级轨不丢工期）。
- FIX-C：渲染分发（route_hifi_spec 窄判据 + render_hifi_png + export._render_one
  hifi 分支 → 失败降级既有轨；SCALE_PRESETS 收敛转发 IMAGE_SCALE_PRESETS 单一事实源）。
- FIX-D：协同决策接线（route_visual_strategy 接进 ai_image 生图前观测；
  parse_img_gen_prompt 接进 ai_image prompt 构建；content.py 三形态压缩说明）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.ai.hifi_charts.exporter import (
    IMAGE_SCALE_PRESETS,  # noqa: F401 - 转发引用面
    SCALE_PRESETS,
    inject_chart_data,
    render_hifi_png,
    route_hifi_spec,
)
from app.services.chart_validators import (
    infer_chart_type_from_payload,
    normalize_architecture_tree,
    normalize_flowchart_data,
    validate_architecture_tree,
    validate_flowchart_data,
    validate_gantt_plan,
)

_WS_ROOT = Path(__file__).resolve().parents[2]

_ORG = {
    "title": "项目组织管理结构图",
    "nodes": {
        "level1": [{"id": "pm", "label": "项目经理"}],
        "level2": [{"id": "chief", "label": "项目总工"}, {"id": "vp1", "label": "项目副经理"}],
        "level3": [
            {"id": "tech", "label": "技术部", "parent": "chief"},
            {"id": "office", "label": "综合办公室", "parent": None},
        ],
        "level4": [{"id": "team1", "label": "各劳务队"}],
    },
}
_FLOW = {
    "type": "flowchart",
    "title": "钢结构加工工艺流程图",
    "steps": [
        {"id": "s1", "label": "深化设计", "variant": "highlight", "x": 260, "y": 40},
        {"id": "s2", "label": "钢板预处理", "variant": "process", "x": 260, "y": 110},
        {"id": "s3", "label": "发运", "variant": "end", "x": 260, "y": 180},
    ],
    "edges": [{"from": "s1", "to": "s2", "path": "h-v-h"}, {"from": "s2", "to": "s3"}],
}
_GANTT = {
    "type": "gantt",
    "title": "施工进度横道图",
    "start_date": "2026-03-01",
    "end_date": "2026-03-10",
    "total_days": 10,
    "tasks": [
        {"id": "T1", "name": "拆除", "start": "2026-03-01", "end": "2026-03-06", "duration": 6},
        {"id": "T2", "name": "水电", "start": "2026-03-05", "end": "2026-03-20", "duration": 16},
    ],
}


# ===========================================================================
# A · FIX-A：三提示词产出契约主链化
# ===========================================================================


def _prompt_src(name: str) -> str:
    from app.services.ai.prompts._registry import get_prompt

    return get_prompt(name) if callable(get_prompt) else ""


@pytest.mark.parametrize(
    "name",
    ["chart_gen_gantt_spec", "chart_gen_org_matrix_spec", "chart_gen_flow_complex_spec"],
)
def test_chart_gen_prompt_fence_contract(name: str):
    """三提示词必须产出 ```chart-json 围栏形态；死路标记清零。"""
    src = _prompt_src(name)
    assert src, f"{name} 未注册"
    assert "```chart-json" in src, f"{name} 缺围栏形态（仍是死路标记契约）"
    for dead in ("AI_Chart_Start", "AI_Chart_End", "[CHART_TYPE:", "[CHART_NAME:", "[DATA:"):
        assert dead not in src, f"{name} 残留死路标记 {dead}"
    assert "【角色】" in src and "【反幻觉" in src, "七段式结构保留"


def test_flow_gen_prompt_declares_type_field():
    """flow 提示词要求载荷顶层 type（infer 判据依赖）。"""
    src = _prompt_src("chart_gen_flow_complex_spec")
    assert '"type": "flowchart"' in src or '\\"type\\": \\"flowchart\\"' in src


# ===========================================================================
# B · FIX-B：主链 hifi 载荷通道
# ===========================================================================


def test_infer_org_matrix_payload_is_architecture():
    """nodes 为 dict 且含 level1-4 → architecture（既有 nodes=list → flowchart 不变）。"""
    assert infer_chart_type_from_payload(_ORG) == "architecture"
    # 既有形态回归：nodes=list 仍 flowchart
    assert infer_chart_type_from_payload({"nodes": [{"id": "a", "label": "x"}, {"id": "b", "label": "y"}]}) == (
        "flowchart"
    )


def test_normalize_arch_tree_org_matrix_becomes_tree():
    """矩阵总线形态归一为 root/children 树；parent=null 直连根、parent 引用挂父。"""
    tree = normalize_architecture_tree(_ORG)
    assert tree is not None
    ok, errs = validate_architecture_tree(tree)
    assert ok, errs
    assert tree["label"] == "项目经理"
    labels_root = [c["label"] for c in tree["children"]]
    assert "项目总工" in labels_root and "各劳务队" in labels_root
    assert "综合办公室" in labels_root, "parent=null（总线直连）必须挂根"
    chief = next(c for c in tree["children"] if c["label"] == "项目总工")
    assert [c["label"] for c in chief["children"]] == ["技术部"], "parent 引用必须挂对应 level2"


def test_normalize_arch_tree_legacy_shapes_unchanged():
    """既有形态回归：label/children 树与字符串根+nodes 列表逐字不变。"""
    tree1 = normalize_architecture_tree({"label": "项目部", "children": [{"label": "技术部"}]})
    assert tree1 and tree1["label"] == "项目部"
    tree2 = normalize_architecture_tree({"type": "architecture", "root": "项目部", "nodes": ["技术部", "安全部"]})
    assert tree2 and tree2["label"] == "项目部"


def test_flow_hifi_payload_passes_main_chain():
    """坐标化 flow 载荷（steps+数值 x/y）全链：infer→normalize→validate。"""
    assert infer_chart_type_from_payload(_FLOW) == "flowchart"
    norm = normalize_flowchart_data(_FLOW)
    assert norm is not None, "多节点坐标化载荷必须归一成功（登记链通）"
    ok, errs = validate_flowchart_data(_FLOW)
    assert ok, errs


def test_gantt_spec_duration_alias_kept_in_pil_fallback():
    """gantt spec（日期域+duration）在 PIL 降级轨工期保留（days≡duration 别名）。"""
    assert infer_chart_type_from_payload(_GANTT) == "gantt"
    assert validate_gantt_plan(_GANTT)[0]
    from app.services.ai.mermaid_gantt import _build_gantt_tasks, _gantt_base_date

    base = _gantt_base_date(_GANTT["tasks"])
    tasks = _build_gantt_tasks(_GANTT["tasks"], base)
    assert len(tasks) == 2
    assert tasks[0]["end"] - tasks[0]["start"] + 1 == 6, "duration 别名生效（否则退化为单日横道）"
    assert tasks[1]["end"] - tasks[1]["start"] + 1 == 16


# ===========================================================================
# C · FIX-C：渲染分发（窄判据 + TTL 短路 + 降级）
# ===========================================================================


def test_route_hifi_spec_three_routes():
    assert route_hifi_spec(json.dumps(_ORG, ensure_ascii=False))[0] == "org_matrix"
    assert route_hifi_spec(json.dumps(_GANTT, ensure_ascii=False))[0] == "gantt"
    assert route_hifi_spec(json.dumps(_FLOW, ensure_ascii=False))[0] == "flow_complex"


def test_route_hifi_spec_negative_for_regular_payloads():
    """主链常规载荷一律不命中（窄判据：宁缺勿误——误路由会丢 PIL 版式）。"""
    assert route_hifi_spec(json.dumps({"type": "gantt", "tasks": [{"name": "a", "start": 1, "days": 5}]})) is None, (
        "天数域 gantt 不得命中"
    )
    assert route_hifi_spec(
        json.dumps({"type": "flowchart", "steps": [{"id": "s1", "label": "a"}, {"id": "s2", "label": "b"}]})
    ) is None, "无坐标 steps 不得命中"
    assert route_hifi_spec(
        json.dumps({"label": "项目部", "children": [{"label": "技术部"}]})
    ) is None, "树形 architecture 不得命中"
    assert route_hifi_spec("flowchart TD\nA --> B") is None, "Mermaid 语法载荷不得命中"
    assert route_hifi_spec("") is None and route_hifi_spec("not json") is None


def test_scale_presets_single_source():
    """exporter.SCALE_PRESETS 与 image_engine.IMAGE_SCALE_PRESETS 同一对象（副本收敛）。"""
    from app.services.ai.image_engine import IMAGE_SCALE_PRESETS

    assert SCALE_PRESETS is IMAGE_SCALE_PRESETS, "第二份 DPI 预设副本不得回流"
    assert SCALE_PRESETS == {"screen": 2, "print": 3, "vector": 4}


def test_export_render_one_hifi_branch_wired():
    """export._render_one 的 hifi 分支源码锁（分支在 fe 之后、既有轨之前；失败降级）。"""
    src = (_WS_ROOT / "backend" / "app" / "routers" / "export.py").read_text(encoding="utf-8")
    assert "_hifi_route_spec(code)" in src
    assert "await _hifi_render_png(*_hifi_route)" in src
    assert 'render_stats["hifi_ok"]' in src and 'render_stats["hifi_failed"]' in src
    # 分支次序：fe 命中在前（所见即所得优先），hifi 失败后仍进入既有 try 轨
    i_fe = src.index("render_stats[\"fe\"] += 1")
    i_hifi = src.index("_hifi_route_spec(code)")
    i_cache = src.index("get_or_render, code, ct")
    assert i_fe < i_hifi < i_cache


@pytest.mark.asyncio
async def test_render_hifi_png_smoke_org_matrix():
    """hifi 渲染轨端到端冒烟（Playwright 可用环境；不可用环境 skip）。"""
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("Playwright 未安装，hifi 模板轨不可用（降级 PIL 轨）")
    png = await render_hifi_png("org_matrix", _ORG)
    assert png and len(png) > 1000


def test_render_hifi_ttl_shortcircuit_guard():
    """TTL 短路只对环境级失败置位（单图失败不误杀后续图）——源码锁。"""
    src = (_WS_ROOT / "backend" / "app" / "services" / "ai" / "hifi_charts" / "exporter.py").read_text(
        encoding="utf-8"
    )
    assert '"Playwright 未安装" in str(exc)' in src
    assert "_HIFI_UNAVAILABLE_UNTIL = time.monotonic() + _HIFI_PROBE_TTL" in src


# ===========================================================================
# D · FIX-D：协同决策接线
# ===========================================================================


def test_route_visual_strategy_wired_into_ai_image_pipeline():
    """route_visual_strategy 接进 _auto_generate_ai_image_blocks（观测不干预）。"""
    src = (_WS_ROOT / "backend" / "app" / "routers" / "export.py").read_text(encoding="utf-8")
    assert "route_visual_strategy" in src
    assert "协同决策观测" in src, "观测 INFO 锚在位"
    assert "_strategy.get(\"route\") == \"structured\"" in src


def test_parse_img_gen_prompt_wired_into_prompt_build():
    """parse_img_gen_prompt 接进 ai_image prompt 构建（模板命中优先、未命中原样）。"""
    src = (_WS_ROOT / "backend" / "app" / "routers" / "export.py").read_text(encoding="utf-8")
    assert "parse_img_gen_prompt" in src
    assert '_imgtpl.get("ok")' in src


def test_content_prompt_has_hifi_compact_specs():
    """content_generation_system 三形态压缩说明在场（AI 自主选用的提示词闭环）。"""
    src = (_WS_ROOT / "backend" / "app" / "services" / "ai" / "prompts" / "content.py").read_text(
        encoding="utf-8"
    )
    for anchor in ("日期域形态", "坐标化形态", "平铺形态"):
        assert anchor in src, f"缺 {anchor} 压缩说明（hifi 契约的 AI 选用入口）"
    # {null} 花括号形态不得回流（会被契约检查误判为占位符变量 contract_var_added）
    assert "{null}" not in src


def test_content_prompt_renders_without_contract_drift():
    """content_generation_system 出厂默认零契约漂移（R233 双 CR 事故后的回归锚）。"""
    from app.services.ai.prompts._registry import _ALL_PROMPTS, validate_prompt_content

    default = _ALL_PROMPTS["content_generation_system"]["default_content"]
    issues = validate_prompt_content("content_generation_system", default)
    assert not issues, f"出厂默认不得有任何契约问题: {[i['code'] for i in issues]}"


# ===========================================================================
# E · 字节体检（含本轮自伤教训锁：双 CR=0）
# ===========================================================================


@pytest.mark.parametrize(
    "relpath",
    [
        "app/services/ai/prompts/charts.py",
        "app/services/ai/prompts/content.py",
        "app/services/chart_validators.py",
        "app/services/ai/mermaid_gantt.py",
        "app/services/ai/hifi_charts/exporter.py",
        "app/routers/export.py",
    ],
)
def test_bytes_health(relpath: str):
    """触碰生产源纯 CRLF / BOM=0 / FFFD=0 / 双 CR=0（R242 自伤教训锁）。"""
    raw = (_WS_ROOT / "backend" / relpath).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{relpath} BOM"
    text = raw.decode("utf-8")
    assert chr(0xFFFD) not in text, f"{relpath} U+FFFD"
    assert "\r\r\n" not in text, f"{relpath} 双 CR（R242 自伤教训：bytes 替换脚本禁用 text.replace('\\n', CRLF) 全文形态）"
    assert "\r\n" in text, f"{relpath} 必须保持 CRLF"


def test_gantt_spec_duration_alias_end_missing_shape():
    """duration 别名的真实生效面：任务只有 start+duration、无 end（M3 A/B 归因补强）。"""
    from app.services.ai.mermaid_gantt import _build_gantt_tasks, _gantt_base_date

    tasks_in = [{"id": "T1", "name": "拆除", "start": "2026-03-01", "duration": 6}]
    base = _gantt_base_date(tasks_in)
    tasks = _build_gantt_tasks(tasks_in, base)
    assert tasks[0]["end"] - tasks[0]["start"] + 1 == 6, "end 缺失时 duration 必须生效（否则单日横道）"


@pytest.mark.asyncio
async def test_route_visual_strategy_observation_logs(db_conn, monkeypatch, caplog):
    """协同决策观测的行为实证（M8 A/B 归因补强）：structured 命中时 INFO 必现。"""
    import logging

    from app.routers import export as E
    from app.services.ai import image_engine as ie

    monkeypatch.setattr(
        ie, "route_visual_strategy", lambda text: {"route": "structured", "chart_type": "gantt", "reason": ""}
    )
    blocks = {"sec1": [{"type": "ai_image", "code": '{"prompt":"效果图","title":"图A"}', "inline": True}]}
    sections = [{"id": "sec1", "content": "施工进度计划与工期安排，横道图如下。"}]
    # 生图快速失败（不产生网络调用）：config 保持默认 True 让入口走满（观测段在入口
    # 判定之后——ai_image_auto_generate=False 会整体早退连观测段都不执行）。
    from app.services.ai import image_engine as ie2

    async def _fail_generate(*a, **k):
        return None

    monkeypatch.setattr(ie2, "generate_illustration_image", _fail_generate)
    with caplog.at_level(logging.INFO, logger="export"):
        await E._auto_generate_ai_image_blocks(sections, blocks, {})
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "协同决策观测" in joined, "structured 命中时观测 INFO 必须现身"
    assert "gantt" in joined
