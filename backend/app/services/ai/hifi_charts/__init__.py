"""高保真图表规格 · 包入口

把文档《定制化甘特图/横道图同步生成》《矩阵总线式组织架构图》
《带 3D 阴影与复杂支线工艺流程图》的增强点收敛为一个包：

  · schemas.py  —— 3 种新增数据契约的渲染前校验（fail-closed）
  · exporter.py —— 统一导出器 `ChartExporter`（PNG/PDF/SVG/批量/高DPI/字体兜底）

生成侧增强（关键词路由 / 七段式提示词 / 图片生成协同）分别落在：
  · app.services.chart_validators  —— `detect_chart_type` 全局路由表
  · app.services.ai.prompts.charts —— 七段式生成提示词 ×3
  · app.services.ai.image_engine    —— `route_visual_strategy` / `parse_img_gen_prompt`

既有 PIL v2 商业级渲染器（7 类全冒烟 PASS）保持为默认渲染轨；本包的
`ChartExporter` 提供 HTML 模板轨（Playwright）与结构化数据轨（复用 PIL）
双轨导出，不侵入既有生成链路。
"""
from app.services.ai.hifi_charts.exporter import (
    SCALE_PRESETS,
    ChartExporter,
    inject_chart_data,
)
from app.services.ai.hifi_charts.schemas import (
    FLOW_TYPES,
    GANTT_TYPES,
    ORG_LEVEL_KEYS,
    validate_flow_complex_spec,
    validate_gantt_spec,
    validate_org_matrix_spec,
)

__all__ = [
    "SCALE_PRESETS",
    "ChartExporter",
    "inject_chart_data",
    "GANTT_TYPES",
    "ORG_LEVEL_KEYS",
    "FLOW_TYPES",
    "validate_gantt_spec",
    "validate_org_matrix_spec",
    "validate_flow_complex_spec",
]
