/**
 * 方案工作台页面级契约类型（G1 · 纯类型抽取，零运行时）。
 *
 * 这些接口此前内联在 `pages/SchemeWorkbenchPage.tsx`（8600+ 行单体组件）里，
 * 只服务该页面的 SSE 事件断言与 REST 响应读取。抽为独立模块后：
 *   1. 页面只保留 `import type`，运行时产物零变化；
 *   2. 事件/响应形状可被新抽出的面板与 hook 共用，避免再造第二份副本。
 *
 * 注意：`ContentGenEvent` 的注释不得写出正文链 SSE 端点的完整 URL 字面量
 * （护栏 `contentTerminalParity20261011.test.ts` 按该字面量定位页面内的正文
 * SSE 循环切片，任何文件出现都会干扰切片起点）。
 */
import type { ChartDropItem, GenStats, StandardReport } from "../../utils/contentEvents";
import type { ExportIssueItem, ExportPreflight } from "./useExportPresets";
import type { SelectedSectionLike } from "./useReviewAudit";
import type { TreeNode } from "./outlineTreeModel";

/** 后端章节/目录节点的传输形状（sections.tree 项与 SSE outline 字段共用） */
export interface OutlineNodeInput {
  id?: string | number;
  title?: string;
  level?: number;
  status?: string;
  word_count?: number;
  word_budget?: number;
  content?: string;
  description?: string;
  /** 落库章节为 JSON 字符串或对象（读取侧两种形态都兼容） */
  outline_json?: string | { id?: string | number } | null;
  facts_stale?: boolean;
  children?: OutlineNodeInput[];
}

/** 所有 SSE 事件的公共键（onSseEvent 统一消费层）。
 *  索引签名对齐 api 层 SseEvent（Record<string, unknown>）：消费侧 for-await 一次性
 *  断言到具体事件类型时需要双向可比（TS2352），运行期取值不受影响。 */
export interface SseBaseEvent {
  [k: string]: unknown;
  task_id?: string;
  event?: string;
  message?: string;
  progress?: number;
  failed_sections?: unknown[];
}

/** stats / ping / progress 事件内嵌运行统计的公共键（GenStats 字段子集） */
export interface SseStatsFields {
  done?: number;
  total?: number;
  failed?: number;
  /** R175 BUG-4：已处理章节数（成功 + 失败），运行期与终态同口径 */
  processed?: number;
  words?: number;
  avg_section_ms?: number | null;
  concurrency?: number;
  phase?: string;
  phase_label?: string;
  stepwise?: boolean;
  nodes?: number;
  running?: GenStats["running"];
}

/** 目录生成链事件（POST /sse/generate-outline/{id}）—— 后端 SSE 契约 */
export interface OutlineGenEvent extends SseBaseEvent, SseStatsFields {
  outline?: OutlineNodeInput[];
  failed_chapters?: string[];
  failed_count?: number;
  /** 生成后自动校验报告（summarizeOutlineQuality / summarizeReviewBasis 消费） */
  review?: unknown;
  /** ✅ R179 N1（2026-11-05）：目录自动确认前置判定（加法式，旧后端不带此键）。
   *  `{ready, blockers}` 由后端 `outline_auto_confirm.evaluate()` 产出；
   *  前端只在开关开启且 ready 时启动倒计时，否则逐字回落到人工闸门。 */
  auto_confirm?: unknown;
  elapsed_ms?: number;
  eta_ms?: number | null;
}

/** 正文生成链事件（SSE 端点 generate-content/{id}，经 sseFetch 调用）—— 后端契约。
 *  section_id/title 为章节级事件（section_start/done/error/stage）的必备键。 */
export interface ContentGenEvent extends SseBaseEvent, SseStatsFields {
  section_id: string;
  title: string;
  index?: number;
  stage?: string;
  stage_label?: string;
  generation_standard?: "precise" | "fuzzy";
  word_count?: number;
  word_budget?: number;
  word_status?: string;
  continue_failed?: boolean;
  quality_issues?: unknown;
  chart_count?: number;
  charts_dropped_count?: number;
  charts_dropped?: ChartDropItem[];
  content?: string;
  reason?: string;
  standard_report?: StandardReport | null;
  elapsed_ms?: number;
  eta_ms?: number | null;
  summary?: { total?: number };
  repaired?: number;
  skipped?: number;
  failed_count?: number;
  consistency_summary?: {
    scan_id?: string;
    total?: number;
    repaired?: number;
    failed?: number;
  } | null;
  standard_summary?: unknown;
  fact_autofix_summary?: unknown;
  /** progress 事件随附的权威统计快照（pickStats 消费） */
  stats?: GenStats;
}

/** 全局事实提取链事件（POST /sse/generate-facts/{id}）—— 后端 SSE 契约 */
export interface FactsGenEvent extends SseBaseEvent {
  warnings?: unknown[];
  elapsed_ms?: number;
  segment_stats?: { failed?: number } & Record<string, unknown>;
}

/** 方案记录（schemesApi.get / list 响应）—— 页面消费字段 —— 后端契约 */
export interface SchemeRecord {
  project_id?: string;
  name?: string;
  type?: string;
  status?: string;
  word_budget?: number;
  word_count?: number;
  generation_standard?: string;
}

/** 选中章节（selectedSection / selectedSectionRef）：useReviewAudit 的 SelectedSectionLike
 *  超集（TreeNode 的全可选投影）。hook 只读写 key/content/status；页面展示读其余字段。 */
export interface WorkbenchSection extends SelectedSectionLike {
  title?: string;
  level?: number;
  word_count?: number;
  word_budget?: number;
  word_status?: string;
  description?: string;
  generation_standard?: string;
  facts_stale?: boolean;
  children?: TreeNode[];
}

/** 导入目录时检测到的多标段提示（对齐 OpenBidKit bidSectionDetector）—— 后端契约 */
export interface MultiSectionHint {
  has_multiple?: boolean;
  total_declared?: number;
  detected_count?: number;
}

/** 事实提取分段统计（completed 事件 segment_stats；FactsSegmentFailuresAlert 消费） */
export interface FactsSegmentStats {
  ok?: number;
  failed: number;
  total?: number;
  failed_details?: Array<{ index?: number; heading?: string; reason?: string }>;
  /** ✅ R179：模型声明「本段无事实」的段数（不是失败，下次增量提取会重试） */
  declared_failed?: number;
  declared_failed_details?: Array<{ index?: number; heading?: string; reason?: string }>;
}

/** 导出预检返回的就绪度总检快照（preflight_summary = ExportPreflight + has_run 判别键） */
export type ExportPreflightSummary = ExportPreflight & { has_run?: boolean };

/** 正文质量自检结果（GET /schemes/{id}/sections/quality）—— 后端契约 */
export interface QualityCheckResult {
  summary?: {
    checked?: number;
    problem_sections?: number;
    colloquial?: number;
    abolished_standards?: number;
  };
  items?: Array<{
    section_id?: string;
    title?: string;
    colloquial_hits?: string[];
    abolished_standards?: string[];
  }>;
  standard_db_version?: string;
  standard_db_checked_at?: string;
}

/** 结构化提取项人工校正弹窗的条目形状（baEditItem；label/output_type 展示用） */
export interface BaEditItemLike {
  item_id?: string;
  label?: string;
  output_type?: string;
  content?: string;
  [k: string]: unknown;
}

/** 目录库条目（GET /outline-library 响应 items[]）—— 后端契约 */
export interface OutlineLibraryItem {
  id?: string;
  name?: string;
  version?: string;
  type?: string;
  ref_count?: number;
  engineering_type?: string;
  profession?: string;
  source?: string;
  [k: string]: unknown;
}

/** 合规检查历史单条（GET /compliance/results 响应 items[]）—— 后端契约 */
export interface ComplianceHistoryItem {
  id?: string;
  created_at?: string;
  rule_id?: string;
  item?: string;
  severity?: string;
  suggestion?: string;
}

/** 后端任务列表项（GET /sse/tasks 响应 tasks[]；页面刷新接管消费字段）—— 后端契约 */
export interface TaskListItem {
  id?: string;
  task_type?: string;
  status?: string;
  progress?: number;
}

/** 导出预检问题（exportIssues state）：useExportPresets.ExportIssueItem 叠加
 *  deriveExportGate 消费的 type/severity 门控字段（后端 issues 明细实际两者都带）。 */
export type ExportIssueGate = ExportIssueItem & { type?: string; severity?: string };
