/**
 * 《待补充清单》前端派生纯函数（2026-09-24，治 F 层：人工补录兜底）
 *
 * 后端数据来源：GET /api/v1/schemes/{id}/export/placeholder-report（逐条版）
 * 与 POST /export/check 的 placeholder_report 键（精简版，无 occurrences）。
 * 本模块只做展示层派生（严重度、文案、排序、截断），不做任何请求与副作用，
 * 与仓库「utils/ 派生纯函数层」约定一致。
 */

export interface PlaceholderFieldEntry {
  field: string;
  count: number;
  section_ids: string[];
  section_titles: string[];
}

export interface PlaceholderSectionEntry {
  section_id: string;
  title: string;
  count: number;
  fields: string[];
}

export interface PlaceholderReport {
  total: number;
  formatted_total: number;
  bare_total: number;
  fuzzy_total: number;
  field_count: number;
  section_count: number;
  by_field: PlaceholderFieldEntry[];
  by_section: PlaceholderSectionEntry[];
  occurrences?: {
    section_id: string;
    section_title: string;
    kind: string;
    field: string;
    snippet: string;
  }[];
  truncated?: boolean;
}

/** 展示上限：字段/章节聚合条目最多渲染条数（超出提示「其余略」） */
export const DISPLAY_ENTRY_LIMIT = 20;

export type PlaceholderSeverity = "none" | "info" | "warn" | "error";

/**
 * 派生清单严重度：
 * - 无占位 → none；
 * - 仅规范占位（AI 按「数据真实性红线」留下的可检索标记）→ warn；
 * - 含裸标记 / 模糊占位（违反提示词规范的写法，需回改）→ error。
 */
export function derivePlaceholderSeverity(report: PlaceholderReport | null | undefined): PlaceholderSeverity {
  if (!report || !Number.isFinite(report.total) || report.total <= 0) return "none";
  if ((report.bare_total || 0) + (report.fuzzy_total || 0) > 0) return "error";
  return "warn";
}

/** 严重度 → antd Alert type / 文案（集中映射，避免组件内散落三元） */
export function severityToAlertProps(sev: PlaceholderSeverity): { type: "success" | "info" | "warning" | "error"; message: string } {
  switch (sev) {
    case "none":
      return { type: "success", message: "未发现【待补充】占位符" };
    case "error":
      return { type: "error", message: "存在不规范占位写法（裸标记 / ××），需回改后再导出" };
    default:
      return { type: "warning", message: "存在待补充的关键数据占位（按字段补录后可消除）" };
  }
}

/** 摘要一行文案：如「共 12 处 · 涉及 5 个字段 · 分布于 4 个章节」 */
export function summarizePlaceholderReport(report: PlaceholderReport | null | undefined): string {
  if (!report || !report.total) return "";
  return `共 ${report.total} 处 · 涉及 ${report.field_count} 个字段 · 分布于 ${report.section_count} 个章节`
    + (report.truncated ? "（逐条记录已截断）" : "");
}

/** 聚合条目截断（展示上限），返回 [可见条目, 被隐藏数] */
export function limitEntries<T>(entries: T[] | undefined, limit = DISPLAY_ENTRY_LIMIT): [T[], number] {
  const list = Array.isArray(entries) ? entries : [];
  return [list.slice(0, limit), Math.max(0, list.length - limit)];
}

// ---- 监控基线趋势（2026-09-24，六层方案第 6 层） ----

export interface PlaceholderBaselineRow {
  total: number;
  formatted_total: number;
  bare_total: number;
  fuzzy_total: number;
  field_count: number;
  section_count: number;
  created_at: string;
}

export interface PlaceholderTrend {
  /** 最新 total 与上一次 total 的差值（负数=下降=改善）；无历史或仅一条时为 null */
  delta: number | null;
  latest: number | null;
  previous: number | null;
}

/**
 * 由历史基线（按时间**倒序**，最新在前）派生趋势。
 * 非法行（total 非数值）过滤掉，不抛异常。
 */
export function deriveTrendDelta(history: PlaceholderBaselineRow[] | null | undefined): PlaceholderTrend {
  const list = (Array.isArray(history) ? history : [])
    .filter((r) => r && Number.isFinite(r.total));
  if (list.length === 0) return { delta: null, latest: null, previous: null };
  const latest = list[0].total;
  const previous = list.length > 1 ? list[1].total : null;
  return { delta: previous === null ? null : latest - previous, latest, previous };
}

/** 重跑计划（后端 GET /export/placeholder-rerun-plan）展示所需的最小类型 */
export interface PlaceholderRerunPlan {
  rerunnable_sections?: string[];
  rerunnable_count?: number;
  fillable_field_count?: number;
  total_field_count?: number;
}

