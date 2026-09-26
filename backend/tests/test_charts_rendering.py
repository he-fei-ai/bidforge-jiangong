"""图表渲染模块测试 — 覆盖 BUG-A~E 修复及增强"""
import hashlib
import json
import os
import tempfile
from io import BytesIO
from unittest.mock import patch

import pytest


# ---------- BUG-A: charts.py skip_http 参数传递 ----------

def test_skip_http_passed_to_get_or_render():
    """BUG-A: render_chart API 的 skip_http 参数应传递到 get_or_render"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    captured_kwargs = {}

    def mock_render(code, chart_type, **kwargs):
        captured_kwargs.update(kwargs)
        return BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

    with patch("app.services.ai.mermaid_renderer.render_mermaid_to_bytes", side_effect=mock_render):
        result = cache.get_or_render("flowchart TD\n A-->B", "flowchart", skip_http=True)
        assert result is not None
        assert captured_kwargs.get("skip_http") is True


def test_skip_http_default_false():
    """BUG-A: skip_http 默认为 False"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    captured_kwargs = {}

    def mock_render(code, chart_type, **kwargs):
        captured_kwargs.update(kwargs)
        return BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

    with patch("app.services.ai.mermaid_renderer.render_mermaid_to_bytes", side_effect=mock_render):
        result = cache.get_or_render("flowchart TD\n A-->B", "flowchart")
        assert result is not None
        assert captured_kwargs.get("skip_http") is False


# ---------- BUG-B: ChartCache.set() 覆写时 total_size 同步 ----------

def test_cache_overwrite_updates_total_size():
    """BUG-B: 覆写已缓存条目时 total_size 应按差值调整"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)

    cache.set("code1", b"\x00" * 100, fmt="png", chart_type="flowchart")
    assert cache._stats["total_size"] == 100

    cache.set("code1", b"\x00" * 300, fmt="png", chart_type="flowchart")
    assert cache._stats["total_size"] == 300

    cache.set("code1", b"\x00" * 50, fmt="png", chart_type="flowchart")
    assert cache._stats["total_size"] == 50


def test_cache_overwrite_multiple_keys():
    """BUG-B: 多 key 场景下覆写应正确维护 total_size"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)

    cache.set("code1", b"\x00" * 100, chart_type="flowchart")
    cache.set("code2", b"\x00" * 200, chart_type="gantt")
    assert cache._stats["total_size"] == 300

    cache.set("code1", b"\x00" * 150, chart_type="flowchart")
    assert cache._stats["total_size"] == 350

    cache.set("code2", b"\x00" * 80, chart_type="gantt")
    assert cache._stats["total_size"] == 230


# ---------- BUG-C: ChartCache.get() misses 计数器线程安全 ----------

def test_cache_get_miss_increments_misses():
    """BUG-C: 缓存未命中时 misses 应正确递增"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)

    result = cache.get("nonexistent_code", chart_type="flowchart")
    assert result is None
    assert cache._stats["misses"] == 1

    result = cache.get("another_nonexistent", chart_type="gantt")
    assert result is None
    assert cache._stats["misses"] == 2


def test_cache_get_hit_increments_hits():
    """BUG-C: 缓存命中时 hits 应正确递增"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    cache.set("code1", b"\x00" * 100, chart_type="flowchart")

    result = cache.get("code1", chart_type="flowchart")
    assert result is not None
    assert cache._stats["hits"] == 1
    assert cache._stats["misses"] == 0


# ---------- BUG-D: flowchart_json_to_mermaid 决策节点引号 ----------

def test_decision_node_has_quoted_label():
    """BUG-D: 决策节点标签应加引号，与其他节点类型一致"""
    from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid

    data = {
        "nodes": [
            {"id": "A", "label": "开始", "type": "start"},
            {"id": "B", "label": "是否合格", "type": "decision"},
            {"id": "C", "label": "结束", "type": "end"},
        ],
        "edges": [
            {"from": "A", "to": "B", "label": ""},
            {"from": "B", "to": "C", "label": ""},
        ],
    }
    result = flowchart_json_to_mermaid(data)
    assert 'B{"是否合格"}' in result
    assert 'A(["开始"])' in result
    assert 'C(["结束"])' in result


def test_decision_node_label_with_special_chars():
    """BUG-D: 决策节点标签含特殊字符时应正确引用"""
    from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid

    data = {
        "nodes": [
            {"id": "A", "label": "检查", "type": "process"},
            {"id": "B", "label": "质量是否达标?", "type": "decision"},
        ],
        "edges": [{"from": "A", "to": "B", "label": ""}],
    }
    result = flowchart_json_to_mermaid(data)
    assert 'B{"质量是否达标?"}' in result


# ---------- BUG-E: mermaid_layout.py 不再调用 img.tobytes() ----------

def test_layout_render_does_not_call_tobytes():
    """BUG-E: 布局图渲染不应调用 img.tobytes()（内存浪费）"""
    from app.services.ai.mermaid_layout import _render_layout_image_v2

    data = {
        "title": "测试布局图",
        "zones": [
            {"name": "办公区", "category": "办公区"},
            {"name": "生活区", "category": "生活区"},
        ],
    }
    with patch("PIL.Image.Image.tobytes", side_effect=AssertionError("tobytes should not be called")):
        result = _render_layout_image_v2(data)
        assert result is not None
        assert len(result.getvalue()) > 100


# ---------- 死代码清理验证 ----------

def test_flowchart_render_no_dead_code():
    """验证流程图渲染不因死代码移除而报错"""
    from app.services.ai.mermaid_flowchart import _render_flowchart_image_v2

    code = 'flowchart TD\n    A["开始"] --> B["处理"]\n    B --> C["结束"]'
    result = _render_flowchart_image_v2(code)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_architecture_render_no_dead_code():
    """验证架构图渲染不因死代码移除而报错"""
    from app.services.ai.mermaid_architecture import _render_architecture_image_v2

    data = {
        "label": "项目经理部",
        "children": [
            {"label": "技术部"},
            {"label": "安全部"},
        ],
    }
    result = _render_architecture_image_v2(data)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_timeline_render_no_dead_code():
    """验证时间轴渲染不因死代码移除而报错"""
    from app.services.ai.mermaid_timeline import _render_timeline_image_v2

    data = {
        "title": "里程碑",
        "milestones": [
            {"name": "开工", "date": "第1天"},
            {"name": "竣工", "date": "第100天"},
        ],
    }
    result = _render_timeline_image_v2(data)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_gantt_render_no_dead_code():
    """验证甘特图渲染不因死代码移除而报错"""
    from app.services.ai.mermaid_gantt import _render_gantt_image_v2

    plan = {
        "title": "施工计划",
        "totalDays": 30,
        "tasks": [
            {"id": 1, "name": "基础施工", "start": 1, "end": 10},
            {"id": 2, "name": "主体施工", "start": 11, "end": 30},
        ],
    }
    result = _render_gantt_image_v2(plan)
    assert result is not None
    assert len(result.getvalue()) > 100


# ---------- ChartCache 综合测试 ----------

def test_cache_clear_resets_total_size():
    """ChartCache.clear() 应重置 total_size"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    cache.set("code1", b"\x00" * 100, chart_type="flowchart")
    cache.set("code2", b"\x00" * 200, chart_type="gantt")
    assert cache._stats["total_size"] == 300

    cache.clear()
    assert cache._stats["total_size"] == 0
    assert len(cache._access_order) == 0


def test_cache_get_or_render_caches_result():
    """get_or_render 成功渲染后应缓存结果，第二次调用应命中缓存"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    call_count = 0

    def mock_render(code, chart_type, **kwargs):
        nonlocal call_count
        call_count += 1
        return BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

    with patch("app.services.ai.mermaid_renderer.render_mermaid_to_bytes", side_effect=mock_render):
        r1 = cache.get_or_render("test_code", "flowchart")
        r2 = cache.get_or_render("test_code", "flowchart")

    assert r1 is not None and r2 is not None
    assert call_count == 1
    assert cache._stats["hits"] == 1
    assert cache._stats["misses"] == 1


def test_cache_chart_type_in_key():
    """同一 payload 不同 chart_type 应独立缓存"""
    from app.services.ai.mermaid_renderer import ChartCache

    cache = ChartCache(cache_dir=tempfile.mkdtemp(), max_size_mb=10)
    cache.set("same_code", b"\x00" * 100, chart_type="flowchart")
    cache.set("same_code", b"\x00" * 100, chart_type="gantt")

    assert cache.get("same_code", chart_type="flowchart") is not None
    assert cache.get("same_code", chart_type="gantt") is not None
    assert cache._stats["hits"] == 2


# ---------- render_mermaid_to_bytes 基硜测试 ----------

def test_render_empty_code_returns_none():
    """空代码应返回 None"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    assert render_mermaid_to_bytes("", "flowchart") is None
    assert render_mermaid_to_bytes(None, "flowchart") is None
    assert render_mermaid_to_bytes("   ", "flowchart") is None


def test_render_flowchart_skip_http():
    """skip_http=True 时应跳过 HTTP Service，直接走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = 'flowchart TD\n    A["开始"] --> B["结束"]'
    result = render_mermaid_to_bytes(code, "flowchart", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_gantt_skip_http():
    """甘特图 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "title": "测试",
        "totalDays": 10,
        "tasks": [{"id": 1, "name": "任务A", "start": 1, "end": 5}],
    })
    result = render_mermaid_to_bytes(code, "gantt", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_architecture_skip_http():
    """架构图 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "label": "项目部",
        "children": [{"label": "技术部"}, {"label": "安全部"}],
    })
    result = render_mermaid_to_bytes(code, "architecture", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_comparison_skip_http():
    """对比图 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "title": "方案对比",
        "items": [
            {"label": "方案A", "value": 40},
            {"label": "方案B", "value": 60},
        ],
    })
    result = render_mermaid_to_bytes(code, "comparison", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_layout_skip_http():
    """布局图 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "title": "总平面布置",
        "zones": [{"name": "办公区", "category": "办公区"}],
    })
    result = render_mermaid_to_bytes(code, "layout", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_timeline_skip_http():
    """时间轴 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "title": "里程碑",
        "milestones": [{"name": "开工", "date": "第1天"}],
    })
    result = render_mermaid_to_bytes(code, "timeline", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


def test_render_labor_skip_http():
    """劳动力图 skip_http=True 时应走 PIL v2"""
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
    code = json.dumps({
        "title": "劳动力配置",
        "phases": ["基础", "主体"],
        "categories": ["钢筋工", "木工"],
        "data": [[10, 5], [20, 8]],
    })
    result = render_mermaid_to_bytes(code, "labor", skip_http=True)
    assert result is not None
    assert len(result.getvalue()) > 100


# ---------- BUG-2: 流程图 JSON 节点经 flowchart_json_to_mermaid 不应超宽 ----------

def test_flowchart_chain_not_superwide():
    """BUG-2: 字符串节点列表经 flowchart_json_to_mermaid 生成的「独立定义行 + 纯 ID 边」
    形态，旧逻辑会把边端点规范化为带标签串、而节点 id 为纯 ID，导致邻接表对不上、
    所有节点被排到层级 0、画布被拉成 6550×710 超宽细条。修复后应纵向布局（宽高比<=3）。"""
    from io import BytesIO
    from PIL import Image

    from app.services.ai.mermaid_flowchart import (
        _parse_flowchart_structure,
        _render_flowchart_image_v2,
        flowchart_json_to_mermaid,
    )

    chain_data = {
        "nodes": ["施工准备", "测量放线", "基础施工", "主体施工", "装饰装修", "竣工验收"],
        "edges": [
            {"from": 0, "to": 1}, {"from": 1, "to": 2}, {"from": 2, "to": 3},
            {"from": 3, "to": 4}, {"from": 4, "to": 5},
        ],
    }
    code = flowchart_json_to_mermaid(chain_data)
    assert "N1 --> N2" in code  # 首条边未被 BUG-3 丢弃

    parsed = _parse_flowchart_structure(code)
    adj = {n["id"]: [] for n in parsed["nodes"]}
    for e in parsed["edges"]:
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].append(e["to"])
    captured = sum(1 for e in parsed["edges"] if e["from"] in adj and e["to"] in adj)
    assert captured == len(parsed["edges"]), "存在未进入邻接表的边（超宽根因）"

    result = _render_flowchart_image_v2(code)
    assert result is not None
    result.seek(0)
    w, h = Image.open(result).size
    assert w / h <= 3, f"流程图仍超宽: {w}x{h} 宽高比={w / h:.2f}"


# ---------- BUG-3: 0 基索引边（from=0）不应被静默丢弃 ----------

def test_flowchart_zero_index_edge_not_dropped():
    """BUG-3: 旧逻辑 `if f and t:` 会把 from=0 误判为 Falsy 从而静默丢弃首条边
    （{'from':0,'to':1} 被漏掉），导致拓扑断裂。修复后首条边应保留且边数与输入一致。"""
    from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid

    edges = [
        {"from": 0, "to": 1}, {"from": 1, "to": 2},
        {"from": 2, "to": 3}, {"from": 3, "to": 4}, {"from": 4, "to": 5},
    ]
    data = {"nodes": ["A", "B", "C", "D", "E", "F"], "edges": edges}
    result = flowchart_json_to_mermaid(data)

    edge_lines = [ln for ln in result.split("\n") if "-->" in ln]
    assert len(edge_lines) == len(edges), f"边数不符：期望 {len(edges)} 实际 {len(edge_lines)}"
    assert "N1 --> N2" in result, "首条边 {from:0,to:1} 被丢弃"
    assert "N6 --> N7" not in result, "不应越界生成多余边"


def test_flowchart_zero_string_id_edge():
    """BUG-3 边界：节点 ID 本身就是数字字符串（'0'/'1'）时不应被误当数组下标改写。"""
    from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid

    data = {
        "nodes": [
            {"id": "0", "label": "开始"},
            {"id": "1", "label": "结束"},
        ],
        "edges": [{"from": "0", "to": "1"}],
    }
    result = flowchart_json_to_mermaid(data)
    assert "0 --> 1" in result or '0["开始"]' in result
    assert "N1 --> N2" not in result, "数字字符串 ID 被误当索引改写"
