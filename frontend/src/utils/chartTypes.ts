/**
 * 图表类型白名单 —— **前端唯一事实来源**。
 *
 * 与后端 `app/services/chart_validators.py::PIL_RENDERABLE_CHART_TYPES` 严格同值
 * （7 类结构化可渲染图表）。改动时必须同步后端该常量，并由
 * `tests/chartTypesParity.test.ts` 锁定两侧一致。
 *
 * 用途：前端只做"**显式声明是否合法**"的判断；载荷省略 `type` 时**不猜**，
 * 一律传空串交由后端按结构推断（`infer_chart_type_from_payload`），
 * 避免前端复制一份推断逻辑而与后端分叉（历史上正是"前端硬编码 labor
 * 兜底"导致甘特图/架构图预览失败、导出却正常）。
 */
export const RENDERABLE_CHART_TYPES: ReadonlySet<string> = new Set([
  "flowchart",
  "gantt",
  "architecture",
  "labor",
  "comparison",
  "layout",
  "timeline",
]);

/** 图题名兜底：解析不出类型且后端未回传时使用（避免界面出现空白标题）。 */
export const CHART_TITLE_FALLBACK = "数据图表";
