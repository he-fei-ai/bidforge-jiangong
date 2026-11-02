"""本地模板兜底图表（AI 未配图时的保底轨道，**默认关闭**）。

来源
----
《报告_图表生成机制深度探索与全套技术方案》中「招投标方案平台」的本地兜底轨道
（``_generate_local_*`` + ``AUTO_FALLBACK_CHARTS`` 默认 false）。本仓按
「正文一体配图」架构移植为：

- :func:`build_local_chart_payload` —— 按图表类型生成**最小合法载荷**
  （纯函数、无 AI、无 DB），载荷统一带 ``source="local"``；
- :func:`maybe_append_local_chart` —— 仅当「正文没有任何图表围栏」且词库判定
  「应配图」时，在文末追加一个带免责声明的 ``chart-json`` 围栏。

设计纪律
--------
1. **默认关闭**（``settings.chart_local_fallback_enabled = False``）：
   关闭时与引入前逐字一致，零行为变化。
2. **宁缺毋滥**：载荷只做「结构合法的最小样例」，并在引导语中明确标注
   「模板示例数据，请按项目实际核定」；数据可信度由使用者核定。
3. 不占用 ai_image 轨道（ai_image 需要真实文生图，无法模板化）。
"""
from __future__ import annotations

import json
import logging

from app.services.chart_triggers import (
    CHART_TYPE_LABELS,
    analyze_section_chart_need,
)
from app.services.chart_validators import (
    validate_architecture_tree,
    validate_comparison_data,
    validate_flowchart_data,
    validate_gantt_plan,
    validate_labor_data,
    validate_layout_data,
    validate_timeline_data,
)

__all__ = [
    "LOCAL_TEMPLATE_DISCLAIMER",
    "SUPPORTED_LOCAL_TYPES",
    "build_local_chart_payload",
    "build_local_chart_fence",
    "maybe_append_local_chart",
]

logger = logging.getLogger(__name__)

#: 与导出图题/前端提示同口径的免责声明（载荷 ``source="local"`` 时展示）。
LOCAL_TEMPLATE_DISCLAIMER = "模板示例数据，请按项目实际核定"

#: 可模板化的类型（ai_image 需要文生图，不在其中）。
SUPPORTED_LOCAL_TYPES: frozenset[str] = frozenset({
    "flowchart", "gantt", "architecture", "labor",
    "comparison", "layout", "timeline",
})

#: 「正文是否已有图表围栏」判据 —— 复用围栏唯一事实源 ``fence_common``（R76）。
#: ✅ R167（2026-11-01）：旧实现自带 ``_CHART_FENCE_RE`` 正则副本，被
#:    ``test_content_fence_single_source_r76_20261013`` 的单源护栏判红
#:    （同一「围栏行」判据在本仓只能有一份实现）。现改为解析信息串首词，
#:    与 ``fence_common.iter_all_blocks`` 的 ``lang`` 口径恒等。
_CHART_FENCE_LANGS: frozenset[str] = frozenset({"mermaid", "chart-json", "ai_image"})


def _has_chart_fence(content: str) -> bool:
    """正文里是否已存在图表类围栏（``mermaid`` / ``chart-json`` / ``ai_image``）。"""
    from app.services.fence_common import iter_all_blocks

    return any(b["lang"] in _CHART_FENCE_LANGS for b in iter_all_blocks(content))

#: 兜底载荷的合法性校验器（放行 ⇔ 必出图，与管线同一批校验器）。
_VALIDATORS = {
    "flowchart": validate_flowchart_data,
    "gantt": validate_gantt_plan,
    "architecture": validate_architecture_tree,
    "labor": validate_labor_data,
    "comparison": validate_comparison_data,
    "layout": validate_layout_data,
    "timeline": validate_timeline_data,
}

_TITLE = {
    "gantt": "施工总进度计划",
    "flowchart": "施工工艺流程",
    "architecture": "项目管理组织机构图",
    "labor": "劳动力配置计划",
    "comparison": "方案比选对比",
    "layout": "施工总平面布置图",
    "timeline": "关键里程碑时间轴",
}


def _clamp_days(total_days) -> int:
    try:
        d = int(float(total_days))
    except (TypeError, ValueError):
        d = 90
    return max(30, min(365, d))


def build_local_chart_payload(chart_type: str, *,
                              title: str = "",
                              total_days: int = 90) -> dict | None:
    """生成某类型的最小合法模板载荷（``source="local"``）。

    Args:
        chart_type: 7 类之一（见 :data:`SUPPORTED_LOCAL_TYPES`）。
        title: 图题；空则用类型通用图题。
        total_days: 总工期（仅 gantt/timeline 使用），夹取到 30~365 天。

    Returns:
        可直接写入 ``chart-json`` 围栏的 dict；类型不支持时返回 ``None``。
    """
    ct = (chart_type or "").strip().lower()
    if ct not in SUPPORTED_LOCAL_TYPES:
        return None
    name = (title or "").strip() or _TITLE.get(ct, CHART_TYPE_LABELS.get(ct, ct))
    d = _clamp_days(total_days)
    base = {"type": ct, "title": name, "fig_no": "", "source": "local"}

    if ct == "gantt":
        p = lambda r: int(round(d * r))  # noqa: E731 - 局部比例换算
        tasks = [
            {"id": 1, "name": "施工准备", "start": 0, "end": p(0.11), "dependencies": []},
            {"id": 2, "name": "基础工程", "start": p(0.09), "end": p(0.33), "dependencies": [1]},
            {"id": 3, "name": "主体结构施工", "start": p(0.30), "end": p(0.62), "dependencies": [2]},
            {"id": 4, "name": "装饰装修与安装", "start": p(0.58), "end": p(0.82), "dependencies": [3]},
            {"id": 5, "name": "系统调试", "start": p(0.78), "end": p(0.90), "dependencies": [4]},
            {"id": 6, "name": "竣工验收", "start": d, "end": d, "dependencies": [5],
             "isMilestone": True},
        ]
        return {**base, "totalDays": d, "tasks": tasks}

    if ct == "flowchart":
        steps = [
            {"id": "s1", "label": "施工准备", "variant": "start"},
            {"id": "s2", "label": "基础工程施工", "variant": "process"},
            {"id": "s3", "label": "主体结构施工", "variant": "highlight"},
            {"id": "s4", "label": "质量检查验收", "variant": "decision"},
            {"id": "s5", "label": "竣工验收移交", "variant": "end"},
        ]
        edges = [
            {"from": "s1", "to": "s2"},
            {"from": "s2", "to": "s3"},
            {"from": "s3", "to": "s4"},
            {"from": "s4", "to": "s5", "label": "通过"},
        ]
        return {**base, "direction": "LR", "steps": steps, "edges": edges}

    if ct == "architecture":
        return {
            **base,
            "root": {"label": "项目经理部", "children": [
                {"label": "技术管理组", "children": [
                    {"label": "技术负责人"}, {"label": "质量员", "highlight": True}]},
                {"label": "施工管理组", "children": [
                    {"label": "施工员"}, {"label": "安全员"}]},
                {"label": "商务管理组", "children": [
                    {"label": "造价员"}, {"label": "材料员"}]},
            ]},
        }

    if ct == "labor":
        return {
            **base,
            "phases": ["施工准备", "基础工程", "主体工程", "装饰装修", "竣工验收"],
            "categories": ["普工", "钢筋工", "木工", "混凝土工", "管理人员"],
            "data": [
                [6, 3, 2, 1, 2],
                [14, 10, 8, 3, 4],
                [22, 18, 15, 8, 6],
                [12, 8, 9, 5, 5],
                [5, 3, 3, 2, 3],
            ],
        }

    if ct == "comparison":
        return {**base, "items": [
            {"label": "方案A", "value": 86},
            {"label": "方案B", "value": 78},
        ]}

    if ct == "layout":
        zones = [
            {"id": "Z1", "name": "办公生活区", "category": "临时设施",
             "x": 5, "y": 4, "w": 26, "h": 18, "description": "办公室、宿舍"},
            {"id": "Z2", "name": "材料堆场", "category": "材料堆场",
             "x": 36, "y": 4, "w": 26, "h": 18, "description": "主材分类堆放"},
            {"id": "Z3", "name": "钢筋加工棚", "category": "加工场地",
             "x": 67, "y": 4, "w": 28, "h": 18, "description": "钢筋加工与半成品"},
            {"id": "Z4", "name": "施工作业区", "category": "大型机械",
             "x": 5, "y": 26, "w": 26, "h": 30, "description": "主体作业面"},
            {"id": "Z5", "name": "塔吊覆盖区", "category": "大型机械",
             "x": 36, "y": 26, "w": 26, "h": 30, "description": "垂直运输覆盖范围"},
            {"id": "Z6", "name": "大门及门卫", "category": "安全设施",
             "x": 67, "y": 26, "w": 28, "h": 30, "description": "出入口与门卫室"},
        ]
        return {**base, "zones": zones}

    if ct == "timeline":
        p = lambda r: max(1, int(round(d * r)))  # noqa: E731
        return {**base, "milestones": [
            {"name": "施工准备完成", "date": f"第{p(0.11)}天", "status": "completed"},
            {"name": "基础工程完工", "date": f"第{p(0.33)}天", "status": "completed"},
            {"name": "主体结构封顶", "date": f"第{p(0.62)}天", "status": "in_progress"},
            {"name": "竣工验收", "date": f"第{d}天", "status": "pending"},
        ]}

    return None


def build_local_chart_fence(chart_type: str, *, title: str = "",
                            total_days: int = 90) -> tuple[str, dict | None]:
    """构造「引导语 + chart-json 围栏」文本。

    Returns:
        ``(fence_text, payload)``；类型不支持或载荷未通过校验器时返回 ``(\"\", None)``。
    """
    payload = build_local_chart_payload(
        chart_type, title=title, total_days=total_days)
    if not payload:
        return "", None
    validator = _VALIDATORS.get(chart_type)
    if validator is not None:
        try:
            ok, _errs = validator(payload)
        except Exception as e:  # noqa: BLE001 - 校验器异常不得产出坏图
            logger.warning("本地模板 %s 校验异常，放弃兜底: %s", chart_type, e)
            return "", None
        if not ok:
            logger.warning("本地模板 %s 未通过校验，放弃兜底: %s", chart_type, _errs)
            return "", None
    label = CHART_TYPE_LABELS.get(chart_type, chart_type)
    lead = f"{label}如下（{LOCAL_TEMPLATE_DISCLAIMER}）："
    body = json.dumps(payload, ensure_ascii=False)
    fence = f"\n\n{lead}\n\n```chart-json\n{body}\n```\n"
    return fence, payload


def maybe_append_local_chart(content: str, *,
                             section_title: str = "",
                             section_description: str = "",
                             scheme_type: str = "",
                             used_counts: dict | None = None,
                             word_count: int | None = None,
                             min_confidence: float = 0.70,
                             total_days: int = 90) -> tuple[str, dict | None]:
    """正文无图且词库判定「应配图」时，文末追加一张本地模板图（**默认关闭**）。

    ⚠️ 调用方必须由开关 ``settings.chart_local_fallback_enabled`` 控制；
    关闭时不应调用本函数（本函数自身不读配置，保持纯函数可测）。

    Args:
        content: 章节正文。
        section_title / section_description / scheme_type: 判定输入。
        used_counts: 全方案各类型已用数量（跳过已超上限的类型）。
        word_count: 正文字数（``None`` 时按正文实算）。
        min_confidence: 触发兜底的置信度阈值。
        total_days: 总工期（模板比例换算）。

    Returns:
        ``(新正文, meta)``；未追加时 meta 为 ``None``、正文逐字不变。
    """
    if not isinstance(content, str) or not content.strip():
        return content, None
    if _has_chart_fence(content):
        return content, None
    if word_count is None:
        try:
            from app.services.content_utils import text_word_count
            word_count = text_word_count(content)
        except Exception:  # noqa: BLE001 - 字数工具不可用时不做字数排除
            word_count = None
    res = analyze_section_chart_need(
        title=section_title, description=section_description,
        content=content, scheme_type=scheme_type,
        word_count=word_count, used_counts=used_counts,
        min_confidence=min_confidence)
    if not res["needed"] or res["chart_type"] not in SUPPORTED_LOCAL_TYPES:
        return content, None
    fence, payload = build_local_chart_fence(
        res["chart_type"], title=res["title_hint"], total_days=total_days)
    if not fence:
        return content, None
    meta = {
        "chart_type": res["chart_type"],
        "confidence": res["confidence"],
        "reason": res["reason"],
        "title": res["title_hint"],
        "source": "local",
    }
    logger.info("本地模板兜底配图：%s（conf=%.2f，%s）",
                res["chart_type"], res["confidence"], res["reason"])
    return content.rstrip() + fence, meta
