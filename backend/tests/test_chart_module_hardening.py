"""图表模块加固回归测试（2026-09-22 深度审查）。

覆盖三项修复 + 各图表家族语义守卫：
1. 幽灵图口径：登记侧 `_scan_inline_charts` 对"超过 MAX_INLINE_CODE_BLOCK_LINES
   行但围栏已闭合"的块，须与导出侧同口径按合法块提取（旧实现一律判未闭合跳过，
   导出却照常渲染 → chart_predictions 查不到 → 幽灵图）；
2. 图题优先级（需求规格）：Mermaid 块取题顺序为 Mermaid title 指令 > 引导语 >
   类型通用名；chart-json 块为 载荷 title > 引导语；
3. 渲染缓存副本：`_render_cache_get` 命中须返回独立 BytesIO（旧实现返回缓存中的
   同一对象，任一调用方 read()/seek() 会污染其他并发消费方）。
"""
import json

from app.routers._chart_pipeline import (
    MAX_INLINE_CODE_BLOCK_LINES,
    _scan_inline_charts,
    _validate_inline_chart,
)
from app.services.ai.mermaid_renderer import (
    _render_cache_get,
    _render_cache_key,
    _render_cache_put,
    clear_render_cache,
)


# ---------------------------------------------------------------------------
# ① 幽灵图口径：超长但已闭合的块正常登记；真未闭合块仍被拒
# ---------------------------------------------------------------------------

def _fenced(lang: str, body_lines: list[str]) -> str:
    return f"```{lang}\n" + "\n".join(body_lines) + "\n```"


def test_overlong_but_closed_mermaid_block_is_registered():
    """超长（>MAX 行）但围栏齐全的 mermaid 块必须被登记（旧实现判未闭合 → 幽灵图）。"""
    lines = ["flowchart TD"]
    # 构造 MAX_INLINE_CODE_BLOCK_LINES + 100 行的合法流程图（远超旧 500 行阈值）
    n = MAX_INLINE_CODE_BLOCK_LINES + 100
    for idx in range(1, n):
        lines.append(f'A{idx}["工序{idx}"] --> A{idx + 1}["工序{idx + 1}"]')
    content = _fenced("mermaid", lines)
    charts = _scan_inline_charts(content)
    assert len(charts) == 1, "超长闭合块应被登记"
    ct, code = charts[0]
    assert ct == "flowchart"
    assert code.count("-->") == n - 1, "代码行不得被截断"


def test_overlong_but_closed_chart_json_block_is_registered():
    """超长但闭合的 chart-json 块同样登记（与导出侧 _parse_content_blocks 同口径）。"""
    zones = [{"id": f"Z{i}", "name": f"区域{i}", "x": (i % 10) * 10,
              "y": (i // 10) * 10, "w": 8, "h": 8}
             for i in range(85)]  # indent=1 序列化后约 680 行，落在 2×MAX 前视窗口内
    body = json.dumps({"type": "layout", "title": "施工总平面布置图", "zones": zones},
                      ensure_ascii=False, indent=1).split("\n")
    charts = _scan_inline_charts(_fenced("chart-json", body))
    assert len(charts) == 1 and charts[0][0] == "layout"


def test_truly_unclosed_overlong_block_is_still_rejected():
    """真未闭合（闭合围栏在前视窗口之外）的块仍判未闭合：不登记、不无限吞内存。"""
    lines = ["flowchart TD"] + [f'A{i}["x"] --> B{i}["y"]' for i in range(1200)]
    content = f"```mermaid\n" + "\n".join(lines) + "\n"  # 无闭合围栏
    assert _scan_inline_charts(content) == []


def test_normal_short_block_unaffected():
    """常规短块行为不变（反例回归：修复不得影响主路径）。"""
    content = _fenced("mermaid", [
        "flowchart TD", 'A["施工准备"] --> B["主体施工"]', 'B --> C["竣工验收"]'])
    assert [ct for ct, _ in _scan_inline_charts(content)] == ["flowchart"]


# ---------------------------------------------------------------------------
# ② 图题优先级：Mermaid title 指令 > 引导语 > 类型通用名
# ---------------------------------------------------------------------------

def _chart_blocks(content: str):
    from app.routers.export import _parse_content_blocks
    return [b for b in _parse_content_blocks(content) if b["type"] == "chart"]


def test_mermaid_title_directive_beats_lead_in():
    """Mermaid title 指令与引导语并存 → 取 Mermaid title（规格优先级）。"""
    content = ("外墙保温工程进度安排如下图所示：\n\n"
               "```mermaid\n"
               "gantt\n"
               "    title 外墙保温工程总进度计划\n"
               "    dateFormat X\n"
               "    施工准备 :a1, 0, 10d\n"
               "```\n")
    charts = _chart_blocks(content)
    assert len(charts) == 1
    assert charts[0]["title"] == "外墙保温工程总进度计划"


def test_mermaid_lead_in_fallback_without_directive():
    """无 Mermaid title 指令 → 回落引导语（既有行为不变）。"""
    content = ("脚手架分段搭设与转换顺序如下图所示：\n\n"
               "```mermaid\n"
               "flowchart LR\n"
               " A[基础验收] --> B[落地段搭设]\n"
               " B --> C[悬挑段搭设]\n"
               "```\n")
    charts = _chart_blocks(content)
    assert len(charts) == 1
    assert charts[0]["title"] == "脚手架分段搭设与转换顺序"


def test_chart_json_payload_title_still_first():
    """chart-json 块：载荷 title 仍最优先（规格第 1 级）。"""
    content = ('组织架构如下图所示：\n\n'
               '```chart-json\n'
               '{"type":"architecture","title":"项目安全管理组织机构图",'
               '"root":{"label":"项目经理","children":[{"label":"安全员"}]}}\n'
               '```\n')
    charts = _chart_blocks(content)
    assert len(charts) == 1
    assert charts[0]["title"] == "项目安全管理组织机构图"


# ---------------------------------------------------------------------------
# ③ 渲染缓存副本：命中返回独立 BytesIO，不互相污染
# ---------------------------------------------------------------------------

def test_render_cache_hit_returns_independent_copy():
    clear_render_cache()
    payload = b"\x89PNG-fake-bytes-0123456789"
    key = _render_cache_key("code-x", "flowchart", 90, "天", 5, False, True)
    _render_cache_put(key, __import__("io").BytesIO(payload))
    first = _render_cache_get(key)
    assert first is not None
    first.read()  # 消费副本的读取位置
    second = _render_cache_get(key)
    assert second is not None
    assert second.getvalue() == payload, "缓存命中必须返回完整副本（读取位置隔离）"
    assert first is not second, "命中必须返回新对象而非缓存中的同一 BytesIO"


# ---------------------------------------------------------------------------
# ④ 各图表家族校验守卫（_validate_inline_chart 语义回归）
# ---------------------------------------------------------------------------

def test_json_chart_types_pass_and_fail_correctly():
    """7 类 chart-json：合法载荷放行；空壳/错位载荷判非法（宁缺勿滥删块口径）。"""
    ok_cases = {
        "architecture": {"type": "architecture",
                         "root": {"label": "项目部", "children": [{"label": "技术部"}]}},
        "labor": {"type": "labor", "phases": ["准备", "主体"], "categories": ["普工"],
                  "data": [[5], [20]]},
        "layout": {"type": "layout", "zones": [{"name": "办公区", "x": 0, "y": 0,
                                                "w": 10, "h": 10}]},
        "timeline": {"type": "timeline",
                     "milestones": [{"name": "开工", "date": "2026-03-01"},
                                    {"name": "竣工", "date": "2026-06-01"}]},
        "gantt": {"type": "gantt", "tasks": [{"id": 1, "name": "准备", "start": 0,
                                              "end": 10, "dependencies": []}]},
        "comparison": {"type": "comparison",
                       "items": [{"label": "方案A", "value": 80},
                                 {"label": "方案B", "value": 65}]},
        "flowchart": {"type": "flowchart", "direction": "LR",
                      "steps": [{"id": "s1", "label": "基层清理"},
                                {"id": "s2", "label": "弹线定位"}],
                      "edges": [{"from": "s1", "to": "s2"}]},
    }
    for ct, payload in ok_cases.items():
        is_valid, _ = _validate_inline_chart(ct, json.dumps(payload, ensure_ascii=False))
        assert is_valid, f"{ct} 合法载荷被误判非法"

    bad_cases = {
        "labor": {"type": "labor", "phases": ["准备"], "categories": [], "data": []},
        "layout": {"type": "layout", "note": "只有说明文字"},
        "timeline": {"type": "timeline", "milestones": [{"name": "唯一事件"}]},
        "gantt": {"type": "gantt", "note": "空壳"},
        "comparison": {"type": "comparison", "items": [{"label": "A", "value": 0}]},
        "flowchart": {"type": "flowchart", "steps": [{"id": "s1", "label": "唯一节点"}]},
    }
    for ct, payload in bad_cases.items():
        is_valid, _ = _validate_inline_chart(ct, json.dumps(payload, ensure_ascii=False))
        assert not is_valid, f"{ct} 空壳载荷被误放行（导出将出现渲染失败占位）"


def test_json_payload_form_routing_not_by_type_alone():
    """按载荷形态分流（与渲染器 startswith('{') 一致）：mermaid 语法的 gantt
    不走 JSON 校验（历史上会被 json.loads 必然失败 → 整块删除）。"""
    mermaid_gantt = ("gantt\n    title 进度计划\n    dateFormat X\n"
                     "    施工准备 :a1, 0, 10d\n"
                     "    主体施工 :a2, after a1, 60d\n"
                     "    竣工验收 :milestone, m1, after a2, 0d\n")
    is_valid, _ = _validate_inline_chart("gantt", mermaid_gantt)
    assert is_valid, "Mermaid 语法的 gantt 不应被送进 JSON 校验链路"
