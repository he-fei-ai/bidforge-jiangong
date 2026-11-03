import { useEffect, useState, useCallback, useRef, useMemo, memo } from "react";
import {
  App, Card, Button, Tree, Progress, Space, Tabs, Input, InputNumber,
  Radio, Checkbox,
  Empty, Tag, Modal, Form, Select, Typography, Descriptions,
  Collapse, Upload, Divider, Tooltip, List, Switch, Alert, Spin, Table,
  Row, Col, Drawer, Popover, Pagination,
} from "antd";
// ✅ any 清理专项（2026-11-02）：目录树拖拽回调复用 antd TreeProps 的 onDrop 事件类型，
//    编辑树/展示树的构建函数返回 antd TreeDataNode
import type { TreeProps, TreeDataNode } from "antd";
import {
  PlayCircleOutlined, PauseOutlined, StopOutlined,
  FileTextOutlined, FilePdfOutlined, ExportOutlined, AuditOutlined,
  GlobalOutlined, FolderOpenOutlined, UploadOutlined, AppstoreOutlined,
  SwapOutlined, ReloadOutlined, CaretRightOutlined,
  EditOutlined, FontSizeOutlined, CheckCircleOutlined,
  CloseCircleOutlined, LoadingOutlined, ExclamationCircleOutlined, WarningOutlined,
  SafetyOutlined, AimOutlined, InfoCircleOutlined, ArrowRightOutlined,
  ArrowUpOutlined, ArrowDownOutlined, DeleteOutlined, PlusOutlined,
  MinusCircleOutlined, FileSearchOutlined, SyncOutlined,
  ClockCircleOutlined, HourglassOutlined, ThunderboltOutlined,
  PauseCircleOutlined,
  BarChartOutlined,
} from "@ant-design/icons";
import { useParams } from "react-router-dom";
import {
sectionsApi, schemesApi, factsApi, exportApi, complianceApi,
sseFetch, sseGetStream, tasksApi, uploadOutlineApi, outlineLibraryApi,
chartsApi, consistencyRepairApi, bidAnalysisApi, docPipelineApi,
systemApi,
} from "../api";
import MarkdownRenderer from "../components/MarkdownRenderer";
import UploadParseTab from "../components/UploadParseTab";
// ✅ any 清理专项（2026-11-02）：UploadParseTab.docs 的元素契约（DocRecord 的严格投影）
import type { DocumentParseItem } from "../components/DocumentParseList";
import ParseResultCategoryPanel, {
  type ParsePanelSharedState,
} from "../components/ParseResultCategoryPanel";
import DocumentPipelineSummary, {
  type DocumentPipelineStatusLike, type PipelineActionKey,
} from "../components/DocumentPipelineSummary";
import BidAnalysisTab, { BidAnalysisItemBody } from "../components/BidAnalysisTab";
// ✅ 2026-09-24 B2/T2：import 子 Tab 标签栏与「18 项扁平清单」抽为受控组件（可单测）
import ImportSubTabBar from "../components/ImportSubTabBar";
import BaItemFlatList from "../components/BaItemFlatList";
import {
recomputeBaSummary, normalizeBaGroups, findFirstDoneBaItem,
type BaItemDef, type BaStoredItem, type BaTextStats,
type BaGroup, type BaSummary,
} from "../utils/bidAnalysis";
import {
  buildUploadNotice, computeDocStats, isDocParsed, isDocFailed, NEXT_TAB, pickInitialTab,
  selectPlacedExportCharts, findDefaultExportPreset, canShrinkSection,
  type WorkflowTabKey, type DocRecord,
} from "../utils/workflowDerived";
import {
  UPLOAD_FILE_ACCEPT, splitByUploadTotalQuota, splitByUploadFileCount,
  partitionUploadFiles, MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES_FALLBACK,
  resolveMaxUploadBytes, formatUploadLimitMb,
} from "../utils/uploadAccept";
// ✅ 2026-10-05（D5 · 前端常量收敛）：目录层级上限统一取 utils/outlineConstants，
//    与目录库编辑弹窗共用同一事实源（此前两处各自内联，易漂移）。
import { MAX_OUTLINE_DEPTH } from "../utils/outlineConstants";
// ✅ 2026-09-26 解析可信度提示：把后端的「文档被截断 / 预算耗尽被跳过 / 编码降级」
//    翻译成用户可见的告警。集中在 utils 的纯函数里，避免页面各处拼文案而漂移。
import { sourceNotice, previewNotices } from "../utils/parseSourceNotice";
// ✅ R86 L1：目录写入检出占位/泛化标题时的可操作提示（后端 create/update/
//    save-outline/upload 四条入口加法式回传 `generic_residual` 明细）。
//    文案集中在 utils 纯函数里（vitest 可直接覆盖），页面只负责 msg.warning。
import { genericAutofixNotice, genericResidualNotice } from "../utils/genericResidual";
// ✅ R87 FIX-1：章节编号**合法性判据**（与后端 `numbering.stored_id_parts` 同口径）。
//    此前本页面自持一份「逐段 parseInt 挑数字」的松过滤，会把脏编号抢救成
//    看似合法的章号，而后端 fail-closed ⇒ 目录树与导出成稿编号不一致。
import { storedIdParts } from "../utils/outlineNumbering";
import ContentGenerationTab from "../components/ContentGenerationTab";
import {
  upsertSectionLog, finalizeRunningLogsIn, mergeFailedSectionsInto,
  contentResultFailedSections, contentResultSummary,
  normalizeQualityIssues, normalizeChartYield, chartDropTexts,
  isTerminalTaskStatus, isTerminalContentEvent, degradedConsistencyHint,
  // ✅ R77b（2026-10-15）：事实冲突定点修复终态提示（与标准校验提示同纪律）
  factAutofixHint,
  type SectionLogItem, type GenStats,
  type StandardReport, type ChartDropItem,
} from "../utils/contentEvents";
// F-CONTENT-STANDARD(2026-09-26): 生成标准纯函数层（请求体映射 / 标签 / 报告判定）
import {
  buildStandardRequestFields, standardSummaryHint,
  sectionStandardOptionLabel, normalizeStandard,
  STANDARD_LABELS, issueLabel, reportHasIssues, reportIssueCount,
  type GenerationStandardChoice,
} from "../utils/contentStandard";
import { useSchemeLiveTask } from "../hooks/useSchemeLiveTask";
import { useAntdMessageHub } from "../utils/activityCenter";
// ✅ 2026-09-25：SSE 批量器抽到 utils/sseBatcher.ts（TaskStatusBar 也要复用，
//  工具函数不再寄生在 9000+ 行的页面文件里）
import { createSseBatcher } from "../utils/sseBatcher";
import {
  chartRenderStatsParts,
  // ✅ D1 加固（2026-10-06）：跨章借图告警文案（此前跨章借图零信号）
  fallbackBorrowWarning,
  fixStatsParts,
  parseChartRenderStats,
  parseFixStats,
  resolveExportFilename,
} from "../utils/exportResponse";
import SectionContentCard, { countPlainTextWords } from "../components/SectionContentCard";
import ReadinessDashboard from "../components/review/ReadinessDashboard";
// ✅ 《待补充清单》面板（2026-09-24，人工补录兜底层）：占位符按字段/按章节聚合
import PlaceholderReportPanel from "../components/PlaceholderReportPanel";
import ReviewWorkflowPanel from "../components/review/ReviewWorkflowPanel";
import type { AuditRule, Severity } from "../types/audit";
import { SEVERITY_COLOR, SEVERITY_LABEL } from "../types/audit";
import type { facts_result as GeneratedFactsResult } from "../types/checkpointSchema";
/**
 * ✅ R89（2026-10-17）：接入 `types/facts.ts` 真实契约（此前 facts 主链路整体 `any`）。
 * 该文件自 2026-10-06 建立却**只被 api/index.ts 当读路径泛型**引用，组件层零引用
 * ⇒ `facts: any[]` / `factsSummary: any` / `FactsGroupList.groups: any[]`，
 * 字段名漂移在编译期完全失明 —— BUG-1（by_chapter 形状）正是这样活下来的。
 * 契约本身必须独立核对后端源码，不得由前端实现反推。
 */
import type {
  FactGroup,
  FactItem,
  FactStats,
  FactChapterStat,
  FactChapterStatBlock,
  FactPagination,
  FactChaptersResponse,
  FactDangerResponse,
  FactsSummary,
  FactGroupLike,
  FactItemLike,
  FactCrossConflict,
} from "../types/facts";
import {
  deriveExportGate,
  renderChartsForExport,
  type ExportChartItem,
  type ExportChartImage,
} from "../utils/exportCharts";
const { Title, Text } = Typography;

// ============================================================
// ✅ any 清理专项（2026-11-02）：后端 SSE 事件与目录节点契约（局部类型）。
// SSE 载荷是本项目后端自有契约（routers/sse_handlers.py），字段随 event 变化；
// api 层 sseFetch 产出 SseEvent（Record<string, unknown>），消费侧在
// for-await 循环体内按链路一次性断言到下列事件类型（编译期行为，运行期取值不变）。
// ============================================================

/** 后端章节/目录节点的传输形状（sections.tree 项与 SSE outline 字段共用） */
interface OutlineNodeInput {
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
interface SseBaseEvent {
  [k: string]: unknown;
  task_id?: string;
  event?: string;
  message?: string;
  progress?: number;
  failed_sections?: unknown[];
}

/** stats / ping / progress 事件内嵌运行统计的公共键（GenStats 字段子集） */
interface SseStatsFields {
  done?: number;
  total?: number;
  failed?: number;
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
interface OutlineGenEvent extends SseBaseEvent, SseStatsFields {
  outline?: OutlineNodeInput[];
  failed_chapters?: string[];
  failed_count?: number;
  /** 生成后自动校验报告（summarizeOutlineQuality / summarizeOutlineReviewBasis 消费） */
  review?: unknown;
  elapsed_ms?: number;
  eta_ms?: number | null;
}

/** 正文生成链事件（SSE 端点 generate-content/{id}，经 sseFetch 调用）—— 后端契约。
 *  ⚠️ 本注释不得写出正文链 SSE 端点的完整 URL 字面量（含前导 /sse/ 与尾部斜杠）：
 *  源码扫描护栏 contentTerminalParity20261011.test.ts 按该字面量定位正文 SSE 循环
 *  的切片起点，注释里出现会把切片劫持到此处，导致 F 组事件覆盖比对失真。
 *  section_id/title 为章节级事件（section_start/done/error/stage）的必备键。 */
interface ContentGenEvent extends SseBaseEvent, SseStatsFields {
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
  consistency_summary?: { scan_id?: string; total?: number; repaired?: number; failed?: number } | null;
  standard_summary?: unknown;
  fact_autofix_summary?: unknown;
  /** progress 事件随附的权威统计快照（pickStats 消费） */
  stats?: GenStats;
}

/** 全局事实提取链事件（POST /sse/generate-facts/{id}）—— 后端 SSE 契约 */
interface FactsGenEvent extends SseBaseEvent {
  warnings?: unknown[];
  elapsed_ms?: number;
  segment_stats?: { failed?: number } & Record<string, unknown>;
}

// ============================================================
// ✅ any 清理专项（2026-11-02）：页面 state / REST 响应契约（局部类型）。
//    R-P1 拆分的三个 hook（useReviewAudit / useExportPresets / useDocumentWorkflow）
//    已定稿各自 deps 的具体类型，下列 state 类型与其对齐；
//    其余为页面独自消费的后端响应形状（字段按实际读取推导，未知字段 ? 修饰）。
// ============================================================

/** 方案记录（schemesApi.get / list 响应）—— 页面消费字段 —— 后端契约 */
interface SchemeRecord {
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
interface WorkbenchSection extends SelectedSectionLike {
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
interface MultiSectionHint {
  has_multiple?: boolean;
  total_declared?: number;
  detected_count?: number;
}

/** 事实提取分段统计（completed 事件 segment_stats；FactsSegmentFailuresAlert 消费） */
interface FactsSegmentStats {
  ok?: number;
  failed: number;
  total?: number;
  failed_details?: Array<{ index?: number; heading?: string; reason?: string }>;
}

/** 跨段矛盾单条（completed 事件 cross_conflicts[]）—— 复用 types/facts 的后端契约
 *  （applyFactsCompletedEvent 返回的就是该类型，页面侧不再维护第二份副本） */

/** 导出预检返回的就绪度总检快照（preflight_summary = ExportPreflight + has_run 判别键） */
type ExportPreflightSummary = ExportPreflight & { has_run?: boolean };

/** 正文质量自检结果（GET /schemes/{id}/sections/quality）—— 后端契约 */
interface QualityCheckResult {
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
interface BaEditItemLike {
  item_id?: string;
  label?: string;
  output_type?: string;
  content?: string;
  [k: string]: unknown;
}

/** 目录库条目（GET /outline-library 响应 items[]）—— 后端契约 */
interface OutlineLibraryItem {
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
interface ComplianceHistoryItem {
  id?: string;
  created_at?: string;
  rule_id?: string;
  item?: string;
  severity?: string;
  suggestion?: string;
}

/** 后端任务列表项（GET /sse/tasks 响应 tasks[]；页面刷新接管消费字段）—— 后端契约 */
interface TaskListItem {
  id?: string;
  task_type?: string;
  status?: string;
  progress?: number;
}

/** 导出预检问题（exportIssues state）：useExportPresets.ExportIssueItem 叠加
 *  deriveExportGate 消费的 type/severity 门控字段（后端 issues 明细实际两者都带）。 */
type ExportIssueGate = ExportIssueItem & { type?: string; severity?: string };

// ============================================================
// ✅ P0-6 计时器隔离：实时耗时只让本行组件每秒自更新（React.memo），
//    消除生成期间整页 1Hz 重渲（旧实现：父组件 setNowTick 驱动全页重渲）
// ============================================================

import { FactsAdjustPanel } from "./schemeWorkbench/FactsAdjustPanel.tsx";
import { FactsCategoryPanel } from "./schemeWorkbench/FactsCategoryPanel.tsx";
import { FactsChapterCoveragePanel } from "./schemeWorkbench/FactsChapterCoveragePanel.tsx";
import { FactsDiagnosticsPanel } from "./schemeWorkbench/FactsDiagnosticsPanel.tsx";
import { FactsExtractProgressCard } from "./schemeWorkbench/FactsExtractProgressCard.tsx";
import { FactsSegmentFailuresAlert } from "./schemeWorkbench/FactsSegmentFailuresAlert.tsx";
import { GenerationProgressCard } from "./schemeWorkbench/GenerationProgressCard.tsx";
import { OutlineGateList } from "./schemeWorkbench/OutlineGateList.tsx";
import { OutlineNodeBudgetPanel } from "./schemeWorkbench/OutlineNodeBudgetPanel.tsx";
import { OutlineTreeActions } from "./schemeWorkbench/OutlineTreeActions.tsx";
import { SchemeTreePanel } from "./schemeWorkbench/SchemeTreePanel.tsx";
import { SectionLogList } from "./schemeWorkbench/SectionLogList.tsx";
import { TaskControlButtons } from "./schemeWorkbench/TaskControlButtons.tsx";
import { EXPORT_ISSUE_LABEL, EXPORT_STAGE_LABEL, EXPORT_STAGE_ORDER, TASK_TYPE_LABEL, deepMergeExportConfig, describeExportIssue, statusColor, statusText } from "./schemeWorkbench/exportShared.ts";
import { FACTS_PAGE_SIZE, FACT_CATEGORY_OPTIONS, FACT_CHAPTER_TITLES, FACT_VALUE_KIND_OPTIONS, FALLBACK_FACT_ATTR_TITLES, FALLBACK_SOURCE_KIND_TITLES, applyFactsCompletedEvent, applyFactsFilter, buildBatchResolveCopy, buildBatchResolveResultCopy, buildFactsCategoryEntries, buildFactsPagination, buildFactsSummary, factAttrTitle, factChapterTitle, factSourceKindTitle, factValueKindLabel, filterFactsGroupsByCategory, hasFactsFilterMatch, isCurrentSchemeRequest, mergeFactsDiagnosticsSettled, resolveSelectedFactCategory } from "./schemeWorkbench/factsShared.ts";
import { type FactsLogItem, type OutlineLogItem } from "./schemeWorkbench/logTypes.ts";
import { formatOutlineTitle } from "./schemeWorkbench/outlineNumbering.ts";
import { buildPartialOutlineHint, collectAllKeys, countGeneratedLeaves, countOutline, findFirstContentDescendant, findSectionById, flattenTreeKeys, hasAnyContent, hasDeepOutlineNodes, hasUnsavedLocalNodes, isUnsavedLocalKey, moveSiblingInTree, outlineToTreeNode, outlineTreeDepth, renumberOutline, renumberTreeLocally, scrollToNodeInContainer, summarizeUploadOutlineResult, treeFingerprint, treeNodesWithContent, treeToOutline, updateNodeFields, type TreeNode } from "./schemeWorkbench/outlineTreeModel.ts";
import { CR_ITEM_STATUS, CR_REPAIR_STATUS, CR_STATUS_LABEL, CR_TYPE_LABEL, DEFAULT_COMPLIANCE_CHECKLIST, summarizeOutlineQuality } from "./schemeWorkbench/reviewShared.ts";
import { pickStats, statsEqual } from "./schemeWorkbench/statsFormat.ts";
import { findRecentOutlineTaskImpl, pollTaskUntilTerminalImpl, reattachTaskImpl, type TaskTerminalInfo } from "./schemeWorkbench/taskPolling.ts";
import { useExportPresets } from "./schemeWorkbench/useExportPresets.tsx";
import { useDocumentWorkflow } from "./schemeWorkbench/useDocumentWorkflow.tsx";
import { useBidAnalysis } from "./schemeWorkbench/useBidAnalysis.tsx";
import { useReviewAudit } from "./schemeWorkbench/useReviewAudit.tsx";
import { useOutlineTreeEdit } from "./schemeWorkbench/useOutlineTreeEdit.tsx";
// ✅ any 清理专项（2026-11-02）：R-P1 拆分 hook 已定稿的域契约 —— 页面 state
//    类型与 hook deps 对齐（setter 传入前受 hook props 类型约束）
import type {
  ComplianceResultItem, CrConflictItem, CrRepairHistoryItem, CrRepairResult,
  CrSummary, ExpertReviewResult, SelectedSectionLike,
} from "./schemeWorkbench/useReviewAudit";
import type {
  ExportCacheStatus, ExportIssueItem, ExportPresetItem, ExportPreflight,
  ExportRerunPlan, ExportStats,
} from "./schemeWorkbench/useExportPresets";
import type { DocPreviewData } from "./schemeWorkbench/useDocumentWorkflow";
import type { PlaceholderReport } from "../utils/placeholderReport";

export { ElapsedTimer } from "./schemeWorkbench/ElapsedTimer.tsx";
export { FactsAdjustPanel } from "./schemeWorkbench/FactsAdjustPanel.tsx";
export { FactsCategoryPanel } from "./schemeWorkbench/FactsCategoryPanel.tsx";
export { FactsChapterCoveragePanel } from "./schemeWorkbench/FactsChapterCoveragePanel.tsx";
export { FactsDiagnosticsPanel } from "./schemeWorkbench/FactsDiagnosticsPanel.tsx";
export { FactsExtractProgressCard } from "./schemeWorkbench/FactsExtractProgressCard.tsx";
export { FactsSegmentFailuresAlert } from "./schemeWorkbench/FactsSegmentFailuresAlert.tsx";
export { GenerationProgressCard, OUTLINE_LOG_RENDER_MAX, type GenerationProgressProps } from "./schemeWorkbench/GenerationProgressCard.tsx";
export { OutlineGateList } from "./schemeWorkbench/OutlineGateList.tsx";
export { OutlineNodeBudgetPanel, WORD_BUDGET_MAX, WORD_BUDGET_MIN } from "./schemeWorkbench/OutlineNodeBudgetPanel.tsx";
export { OutlineTreeActions } from "./schemeWorkbench/OutlineTreeActions.tsx";
export { SchemeTreePanel, type SchemeTreePanelProps } from "./schemeWorkbench/SchemeTreePanel.tsx";
export { SECTION_LOG_BG, SECTION_LOG_ICON, SECTION_LOG_RENDER_MAX, SectionLogList } from "./schemeWorkbench/SectionLogList.tsx";
export { TaskControlButtons } from "./schemeWorkbench/TaskControlButtons.tsx";
export { EXPORT_ISSUE_LABEL, EXPORT_STAGE_LABEL, EXPORT_STAGE_ORDER, TASK_TYPE_LABEL, deepMergeExportConfig, describeExportIssue, statusColor, statusText } from "./schemeWorkbench/exportShared.ts";
export { FACTS_ADJUST_IGNORE_REASONS, FACTS_ADJUST_IGNORE_REASON_LABELS, FACTS_ADJUST_PREVIEW_LIMIT, FACTS_PAGE_SIZE, FACT_CATEGORY_OPTIONS, FACT_CHAPTER_TITLES, FACT_VALUE_KIND_OPTIONS, FALLBACK_FACT_ATTR_TITLES, FALLBACK_SOURCE_KIND_TITLES, applyFactsCompletedEvent, applyFactsFilter, buildBatchResolveCopy, buildBatchResolveResultCopy, buildFactChapterStats, buildFactsCategoryEntries, buildFactsPagination, buildFactsSummary, factAttrTitle, factChapterTitle, factSourceKindTitle, factValueKindLabel, factsAdjustIgnoreText, factsAdjustIgnoredCount, filterFactsGroupsByCategory, formatFactsAdjustOperation, hasFactsFilterMatch, isCurrentSchemeRequest, mergeFactsDiagnosticsSettled, normalizeFactsAdjustPlan, resolveSelectedFactCategory, type FactChapterStatRow, type FactsAdjustIgnoredItem, type FactsAdjustOperation, type FactsAdjustPlanShape, type FactsCategoryEntry } from "./schemeWorkbench/factsShared.ts";
export { type FactsLogItem, type OutlineLogItem } from "./schemeWorkbench/logTypes.ts";
export { CN_NUMBERS, LEADING_NUMBER_CJK, LEADING_NUMBER_RE, LEADING_NUMBER_SEP, PURE_NUMBER_TITLE_RE, YEAR_PREFIX_RE, formatOutlineTitle, stripOutlineNumbering } from "./schemeWorkbench/outlineNumbering.ts";
export { STAGE_COLOR, UNSAVED_KEY_PREFIXES, _PURE_NUMERIC_ID_RE, buildPartialOutlineHint, collectAllKeys, countGeneratedLeaves, countOutline, describeReorganizeReport, findFirstContentDescendant, findSectionById, flattenTreeKeys, hasAnyContent, hasDeepOutlineNodes, hasUnsavedLocalNodes, isUnsavedLocalKey, moveSiblingInTree, outlineToTreeNode, outlineTreeDepth, renumberOutline, renumberTreeLocally, scrollToNodeInContainer, summarizeUploadOutlineResult, treeFingerprint, treeNodesWithContent, treeToOutline, updateNodeFields, type PartialOutlineHint, type TreeNode, type UploadOutlineSummary } from "./schemeWorkbench/outlineTreeModel.ts";
export { CR_ITEM_STATUS, CR_REPAIR_STATUS, CR_STATUS_LABEL, CR_TYPE_LABEL, DEFAULT_COMPLIANCE_CHECKLIST, summarizeOutlineQuality } from "./schemeWorkbench/reviewShared.ts";
export { fmtDuration, pickStats, statsEqual } from "./schemeWorkbench/statsFormat.ts";
export { findRecentOutlineTaskImpl, pollTaskUntilTerminalImpl, reattachTaskImpl, type TaskTerminalInfo } from "./schemeWorkbench/taskPolling.ts";

export function summarizeOutlineReviewBasis(review: unknown): {
  level: "info" | "none";
  message: string;
  logs: string[];
} {
  const empty = { level: "none" as const, message: "", logs: [] as string[] };
  if (!review || typeof review !== "object") return empty;
  const r = review as { review_mode?: unknown; suggestions?: unknown };
  const logs: string[] = [];
  const mode = typeof r.review_mode === "string" ? r.review_mode : "";
  if (mode.startsWith("programmatic")) {
    const detailMap: Record<string, string> = {
      surgical: "外科式补齐缺失章节",
      titled: "占位标题外科式重写",
    };
    // 未登记的模式 token 照原文透出（后端新增细分时前端不得静默咽掉）
    const tokens = mode.split("+").slice(1).map((t) => detailMap[t] || t);
    const detail = tokens.length ? `，含${tokens.join("、")}` : "，全部必备项已覆盖";
    logs.push(`📗 审核依据：程序化预检${detail}（本次结论未经完整 AI 审核）`);
  } else {
    logs.push("📘 审核依据：AI 审核链路");
  }
  // ⚠️ 脏数据防御：与 summarizeOutlineQuality 同口径 —— suggestions 元素可能
  //    是 null/非字符串（弱模型回传），逐个过滤，绝不在日志路径上抛。
  const sugg = Array.isArray(r.suggestions)
    ? r.suggestions.filter((s): s is string => typeof s === "string" && s.trim() !== "")
    : [];
  // ⚠️ 前缀全角空格走普通字符串拼接：模板字符串内的 U+3000 会被 eslint
  //    no-irregular-whitespace 判红（该规则 skipTemplates 默认 false）。
  sugg.slice(0, 8).forEach((s) => logs.push("　· " + s));
  if (sugg.length > 8) logs.push("　· …共 " + sugg.length + " 条");
  return { level: "info", message: logs.join("\n"), logs };
}

// ============================================================
// SSE 断线兜底轮询（2026-09-20 提取为模块级可注入实现）：
// 原为组件内闭包（依赖 tasksApi/setProgress），无法在测试中驱动其
// 终态判定/兜底分支；提取后组件侧仅剩 setState 胶水，断线重挂契约
// （含 stuckAtFull 兜底保留 outline_result）可直接行为单测。
// ============================================================

function WorkflowTabLabel({
  step,
  title,
  badge,
  badgeColor,
  hint,
}: {
  step: number;
  title: string;
  badge?: string;
  badgeColor?: string;
  hint?: string;
}) {
  return (
    <Space size={3} style={{ lineHeight: 1.3 }}>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 16,
          height: 16,
          borderRadius: "50%",
          background: "#e6f4ff",
          color: "#1677ff",
          fontSize: 10,
          fontWeight: 600,
          flexShrink: 0,
          lineHeight: 1,
        }}
      >
        {step}
      </span>
      {/* ✅ swb-tab-title / swb-tab-hint 供 index.css 窄屏媒体查询隐藏标题与提示图标
          （2026-09-20 重建时类名丢失导致自适应失效、导航栏拥挤，勿再删） */}
      <span className="swb-tab-title" style={{ fontSize: 13 }}>{title}</span>
      {badge && (
        <Tag
          color={badgeColor || "blue"}
          style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: "16px", padding: "0 4px" }}
        >
          {badge}
        </Tag>
      )}
      {hint && (
        <Tooltip title={hint}>
          <InfoCircleOutlined className="swb-tab-hint" style={{ color: "#bfbfbf", fontSize: 11 }} />
        </Tooltip>
      )}
    </Space>
  );
}

/** 统计目录树中叶子章节的生成进度（正文页标签徽标用） */

export const FactsGroupList = memo(function FactsGroupList({
  groups,
  filter,
  onEditGroup,
  onDeleteGroup,
  onEditItem,
  onResolveItem,
  onResolveConflict,
  onAckStaleOne,
  // ✅ 2026-10-06：三套枚举的可选注入（来自 /global-facts/category-map）。
  //    设为可选以兼容既有渲染点与测试替身；缺省时各标题函数用本地兜底表。
  chapterTitles,
  factAttrTitles,
  sourceKindTitles,
}: {
  // ✅ R89（2026-10-17）：接入真实契约（此前 groups / 各回调全是 any）。
  //   本组件是 facts 交互覆盖最充分的组件（既有 13+ 例），却对「后端字段改名」
  //   完全失明 —— BUG-1（by_chapter 形状错位）正属此类。
  //   ⚠️ 刻意**不**把 items/回调参数收到 FactItem：既有测试夹具是**局部对象**
  //   （只列被断言的字段），强行收紧会让 20+ 处夹具与断言一起改写 ——
  //   「判据过严 → 后人改松」比判据缺失更糟（§5.14）。形状正确性改由
  //   `types/facts.ts ↔ 后端真实响应` 的跨语言 parity 护栏担守（pytest 侧）。
  groups: FactGroupLike[];
  filter: string;
  onEditGroup: (group: FactGroupLike) => void;
  onDeleteGroup: (group: FactGroupLike) => void;
  onEditItem: (group: FactGroupLike, item: FactItemLike) => void;
  onResolveItem: (item: FactItemLike) => void;
  onResolveConflict: (item: FactItemLike, value: string) => void;
  onAckStaleOne?: (itemId: string, title?: string) => void;
  chapterTitles?: Record<string, string>;
  factAttrTitles?: Record<string, string>;
  sourceKindTitles?: Record<string, string>;
}) {
  // ✅ 性能优化：按输入记忆化过滤结果（原实现每次渲染无条件重算）
  const visible = useMemo(() => applyFactsFilter(groups, filter), [groups, filter]);
  return (
    <Collapse
      items={visible.map((g) => {
        const allItems: FactItemLike[] = g.items || [];
        const items: FactItemLike[] = g.filteredItems || allItems;
        // ✅ 性能优化：单次遍历同时统计三项计数（原实现 3 次独立 filter，
        // 事实条目数百条时每组重复扫描 3 遍）
        let simCount = 0;
        let conflictCount = 0;
        let unresolvedCount = 0;
        for (const it of allItems) {
          if (it.is_simulated) simCount += 1;
          if (it.has_conflict) conflictCount += 1;
          if (!it.is_resolved) unresolvedCount += 1;
        }

        return {
          key: g.id || g.title,
          label: (
            <Space>
              <span>{g.title}</span>
              <Tag>{allItems.length} 项</Tag>
              {simCount > 0 && <Tag color="orange">⚠️{simCount}</Tag>}
              {conflictCount > 0 && <Tag color="red">🔸{conflictCount}</Tag>}
              {unresolvedCount > 0 && <Tag color="blue">📝{unresolvedCount}</Tag>}
            </Space>
          ),
          // ✅ 断链修复：分组级「编辑」（旧前端 openFactEdit 为死代码，
          //    分组重建弹窗/接口能力不可见）与「删除」（factsApi.delete 无调用点）
          extra: (
            <Space size={4} onClick={(e) => e.stopPropagation()}>
              <Button
                size="small"
                type="link"
                icon={<EditOutlined />}
                onClick={() => onEditGroup(g)}
              >
                编辑分组
              </Button>
              <Button
                size="small"
                type="link"
                danger
                icon={<DeleteOutlined />}
                onClick={() => onDeleteGroup(g)}
              >
                删除
              </Button>
            </Space>
          ),
          children: (
            <div>
              {/* 增强版：展示结构化 items（无结构化条目时回退整段 Markdown） */}
              {items.length > 0 && items.some((it) => it.name || it.value) ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {items.map((it, idx: number) => (
                    // ✅ 性能优化：稳定 key。旧实现用数组下标，切换筛选条件
                    //（全部/模拟值/矛盾/待审核）导致条目顺序变化时整块 DOM 被销毁
                    // 重建（展开态、输入焦点全部丢失）
                    <div
                      key={it.id || it.fact_id || `${g.id || g.title}#${idx}`}
                      style={{
                        padding: "8px 12px",
                        borderRadius: 6,
                        border: it.has_conflict ? "1px solid #ff4d4f" :
                          it.is_simulated ? "1px solid #faad14" :
                          it.is_resolved ? "1px solid #d9d9d9" : "1px solid #91caff",
                        background: it.is_simulated ? "#fffbe6" :
                          it.has_conflict ? "#fff2f0" :
                          it.is_resolved ? "#fafafa" : "#e6f4ff",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          {/* 事实名称 + 值 */}
                          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                            <Text strong style={{ color: it.has_conflict ? "#ff4d4f" : undefined }}>
                              {it.name || `事实 ${idx + 1}`}
                            </Text>
                            <span style={{ color: "#666" }}>:</span>
                            <Text style={{
                              color: it.is_simulated ? "#fa8c16" :
                                it.has_conflict ? "#ff4d4f" : "#1677ff",
                              fontWeight: 500,
                            }}>
                              {it.value}
                            </Text>
                          </div>

                          {/* 标记行：章节 / 事实属性 / 模拟值 / 矛盾 / 置信度 / 来源 */}
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4, flexWrap: "wrap" }}>
                            {/* ✅ 2026-09-24：九大章节徽标（建办质〔2018〕31号）。
                                chapter 由后端在提取/读路径惰性派生；历史行也能展示。 */}
                            {it.scope === "project" && (
                              <Tooltip title="项目共享事实：当前项目下各方案均会读取，编辑或删除会影响其它方案">
                                <Tag color="cyan" style={{ margin: 0 }}>项目共享</Tag>
                              </Tooltip>
                            )}
                            {factChapterTitle(it.chapter, chapterTitles) && (
                              <Tooltip title={`九大章节归属：${factChapterTitle(it.chapter, chapterTitles)}`}>
                                <Tag color="geekblue" style={{ margin: 0 }}>
                                  📖 {factChapterTitle(it.chapter, chapterTitles)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_shared && (
                              <Tooltip title="跨章节共性事实：多个章节都需要引用，避免重复提取">
                                <Tag color="purple" style={{ margin: 0 }}>
                                  🔗 共性事实
                                </Tag>
                              </Tooltip>
                            )}
                            {factAttrTitle(it.fact_attr, factAttrTitles) && (
                              <span style={{ fontSize: 11, color: "#888" }}>
                                {factAttrTitle(it.fact_attr, factAttrTitles)}
                              </span>
                            )}
                            {/* ✅ 2026-10-06 新增展示：数据来源类型。
                                此前后端逐条下发 source_kind、统计里也有
                                by_source_kind，但界面零展示 —— 用户无法分辨
                                一条事实来自项目资料 / 图纸 / 现场勘察 / 总体方案 /
                                人工手册，而这正是判断「资料原文 vs AI 编造」的关键依据。 */}
                            {factSourceKindTitle(it.source_kind, sourceKindTitles) && (
                              <Tooltip title={`数据来源：${factSourceKindTitle(it.source_kind, sourceKindTitles)}`}>
                                <Tag color="default" style={{ margin: 0, fontSize: 11 }}>
                                  🗂 {factSourceKindTitle(it.source_kind, sourceKindTitles)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_stale && (
                              <Tooltip title="来源资料已重新解析或删除，本条旧事实已停止注入正文与导出；点击可确认「该值仍有效」并解除标记">
                                <Tag
                                  color="volcano"
                                  style={{ margin: 0, cursor: "pointer" }}
                                  onClick={() => onAckStaleOne?.((it.fact_id || it.id) as string, it.name || (it as { title?: string }).title)}
                                >
                                  来源已变化 ✓
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_simulated && (
                              <Tooltip title="AI 模拟生成，需人工确认">
                                <Tag color="orange" style={{ margin: 0 }}>
                                  ⚠️ 模拟值
                                </Tag>
                              </Tooltip>
                            )}
                            {/* ✅ R80（2026-10-15）：要求口径事实 —— 该值是招标/规范
                                要求原文，正文引用时后端会提示 AI 按「满足/响应要求」
                                口径书写。空值（未区分/本方案口径）不渲染徽标。 */}
                            {it.value_kind === "requirement" && (
                              <Tooltip title="该条目是招标/规范要求原文口径：正文引用时将写为「满足/响应招标要求」，不会当作本方案既定值">
                                <Tag color="geekblue" style={{ margin: 0 }}>
                                  {/* ✅ R81：徽标文案走单一导出（与测试同源） */}
                                  📋 {factValueKindLabel(it.value_kind)}
                                </Tag>
                              </Tooltip>
                            )}
                            {it.is_safety_critical && (
                              <Tooltip title="安全关键参数：禁止使用模拟值，正文引用前必须核对原始资料">
                                <Tag color="volcano" style={{ margin: 0 }}>
                                  🛡 安全关键
                                </Tag>
                              </Tooltip>
                            )}
                            {it.has_conflict && (
                              <Tooltip title={
                                <div>
                                  <div><strong>矛盾选项：</strong></div>
                                  {(it.conflict_values || []).map((cv, ci: number) => (
                                    <div key={ci}>
                                      {cv.value as string} ({cv.source}, 置信度 {(cv.confidence! * 100).toFixed(0)}%)
                                    </div>
                                  ))}
                                </div>
                              }>
                                <Tag color="red" style={{ margin: 0 }}>
                                  🔸 存在矛盾
                                </Tag>
                              </Tooltip>
                            )}
                            {!it.is_resolved && (
                              <Tag color="blue" style={{ margin: 0 }}>
                                📝 待审核
                              </Tag>
                            )}
                            {/* 置信度小条 */}
                            <Tooltip title={`置信度 ${(it.confidence! * 100).toFixed(0)}%`}>
                              <div style={{
                                width: 50, height: 6, borderRadius: 3,
                                background: it.confidence! >= 0.9 ? "#52c41a" :
                                  it.confidence! >= 0.7 ? "#1677ff" :
                                  it.confidence! >= 0.4 ? "#fa8c16" : "#ff4d4f",
                                opacity: 0.8,
                              }} />
                            </Tooltip>
                            {/* 来源 */}
                            {it.source && (
                              <span style={{ fontSize: 11, color: "#999" }}>
                                📎 {it.source}
                                {(it as { source_ref?: string }).source_ref && ` · ${(it as { source_ref?: string }).source_ref!.slice(0, 30)}`}
                              </span>
                            )}
                          </div>

                          {/* 矛盾候选值列表 + 一键选值 */}
                          {it.has_conflict && (it.conflict_values?.length ?? 0) > 0 && (
                            <div style={{ marginTop: 4, padding: "4px 8px", background: "#fff7e6", borderRadius: 4 }}>
                              <Text type="secondary" style={{ fontSize: 12 }}>
                                候选值（点击「选此值」裁决）：
                              </Text>
                              <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
                                {/* ✅ 缺口修复：候选值列表只含「其它取值」，事实自身
                                    的当前值不在其中。这里补「保留当前值」入口，
                                    走同一裁决接口。 */}
                                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                  <Tag color="green" style={{ margin: 0 }}>{it.value}（当前）</Tag>
                                  <Button
                                    size="small"
                                    type="link"
                                    style={{ padding: 0, height: "auto" }}
                                    onClick={() => onResolveConflict(it, it.value!)}
                                  >
                                    保留当前值
                                  </Button>
                                </div>
                                {it.conflict_values!.map((cv, ci: number) => (
                                  <div key={ci} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                    <Tag color="orange" style={{ margin: 0 }}>{cv.value as string}</Tag>
                                    <span style={{ fontSize: 11, color: "#999" }}>来自 {cv.source}</span>
                                    <Button
                                      size="small"
                                      type="link"
                                      style={{ padding: 0, height: "auto" }}
                                      onClick={() => onResolveConflict(it, cv.value as string)}
                                    >
                                      选此值
                                    </Button>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>

                        {/* 操作按钮 */}
                        <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                          {/* 单条编辑：走 item_updates，不整组重建 */}
                          {it.fact_id && (
                            <Button
                              size="small"
                              type="link"
                              icon={<EditOutlined />}
                              onClick={() => onEditItem(g, it)}
                            >
                              编辑
                            </Button>
                          )}
                          {!it.is_resolved && (
                            <Button
                              size="small"
                              type="link"
                              icon={<CheckCircleOutlined />}
                              onClick={() => onResolveItem(it)}
                            >
                              确认
                            </Button>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <MarkdownRenderer content={g.content || ""} />
              )}
            </div>
          ),
        };
      })}
    />
  );
});


export default function SchemeWorkbenchPage() {
  const { id } = useParams();
  const { message: _antdMsg, modal } = App.useApp();
  // ✅ 消息中心：页面所有 msg.* 弹出消息（目录/正文生成、审核与预检、导出文档等）
  //    自动同步进全局活动记录，标题栏中部 ActivityHint 可实时查看
  const msg = useAntdMessageHub(_antdMsg, "方案工作台");
  const [scheme, setScheme] = useState<SchemeRecord | null>(null);
  // R-P1 拆分：project_id 提取为组件级 const —— 既作 useDocumentWorkflow /
  // useBidAnalysis 的 deps 值，也供 loadDocuments / loadBaResults 等依赖数组
  // 引用同一绑定（值语义与 scheme?.project_id 完全一致）。
  const schemeProjectId = scheme?.project_id;
  const [tree, setTree] = useState<TreeNode[]>([]);
  // 长方案分步目录生成时，子目录生成失败的章节标题列表（用于高亮提示）
  const [failedOutlineChapters, setFailedOutlineChapters] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  // 编制要求 / 评审要点（对齐 OpenBidKit score-planning 的「评分大项→目录分支」）：
  // 存于 schemes.config_json.requirements；生成目录时逐条注入（缺失即不合格），
  // 审核阶段做覆盖检查并自动修复补齐
  const [requirements, setRequirements] = useState("");
  // 导入目录时检测到的多标段提示（对齐 OpenBidKit bidSectionDetector）
  const [multiSectionHint, setMultiSectionHint] = useState<MultiSectionHint | null>(null);
  // 加载已保存的编制要求（config_json.requirements）
  useEffect(() => {
    const pid = scheme?.project_id;
    if (!id || !pid) return;
    let cancelled = false;
    schemesApi
      .get(pid, id)
      .then(({ data }) => {
        if (cancelled) return;
        try {
          const cfg = JSON.parse(data.config_json || "{}");
          setRequirements(String(cfg.requirements || ""));
        } catch {
          /* config_json 非法时忽略 */
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [scheme?.project_id, id]);
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genType, setGenType] = useState("");
  // ✅ F3 暂停状态可见：当前活跃任务是否已暂停（暂停/恢复按钮互斥 + 「已暂停」Tag）
  const [taskPaused, setTaskPaused] = useState(false);
  // ✅ R106 FIX-4：「停止中」态。stop 指令 accepted ≠ 已停止（后端协作式退出，
  //    要等在飞 AI 调用被 cancel / 段边界检查点命中），按钮保持置灰直至任务落
  //    终态。旧实现发完指令立刻乐观报「已停止」，用户以为还能接着点。
  const [taskStopping, setTaskStopping] = useState(false);
  const [selectedSection, setSelectedSection] = useState<WorkbenchSection | null>(null);
  const [facts, setFacts] = useState<FactGroup[]>([]);
  const [factsSummary, setFactsSummary] = useState<FactsSummary | null>(null);
  // ✅ facts 加载已落定（成功或失败都置 true）：起始 Tab 判定以它为「可以决策」
  //    信号 —— 旧实现只看 factsSummary===null，加载失败时永远停在等待态。
  const [factsReady, setFactsReady] = useState(false);
  const [docList, setDocList] = useState<DocRecord[]>([]);
  // ===== ✅ 接线 factsApi.create / factsApi.update（手工新增与修改事实）=====
  const [factModalOpen, setFactModalOpen] = useState(false);
  const [factEditingId, setFactEditingId] = useState<string | null>(null);
  const [factEditingCategory, setFactEditingCategory] = useState<string>("");
  const [factSubmitting, setFactSubmitting] = useState(false);
  const [factForm] = Form.useForm();
  // ===== 单条事实编辑（item_updates：改名称/值/分类/模拟值标记，不重建分组）=====
  const [factItemModalOpen, setFactItemModalOpen] = useState(false);
  const [factItemEditing, setFactItemEditing] = useState<(FactItemLike & { groupId?: string; groupTitle?: string }) | null>(null);
  const [factItemSubmitting, setFactItemSubmitting] = useState(false);
  const [factItemForm] = Form.useForm();
  // 全局事实 Tab 筛选：全部 / 模拟值 / 矛盾 / 待审核
  const [factsFilter, setFactsFilter] = useState<string>("all");
  // ✅ 缺值模式（对齐 OpenBidKit 全局事实三模式）：
  // fabricate（智能补全，模拟值标记）/ omit（不杜撰，剔除模拟值）/ placeholder（留待填写）
  const [missingValueMode, setMissingValueMode] = useState<
    "fabricate" | "omit" | "placeholder">("fabricate");
  // 全局事实提取进度日志（阶段/分段级，实时滚动显示）
  const [factsLogs, setFactsLogs] = useState<FactsLogItem[]>([]);
  const factsLogsRef = useRef<FactsLogItem[]>([]);
  // 最近一次提取的分段统计（用于展示"哪些段失败、为什么失败"）
  const [factsSegmentStats, setFactsSegmentStats] = useState<FactsSegmentStats | null>(null);
  // ✅ SSE completed 事件的跨段矛盾清单（旧版前端未消费，2026-09-20 接线）
  const [factsCrossConflicts, setFactsCrossConflicts] = useState<FactCrossConflict[]>([]);
  // ✅ 事实分类白名单：初始为本地 FACT_CATEGORY_OPTIONS，挂载后由 GET /global-facts/categories 覆盖
  const [factCategoryOptions, setFactCategoryOptions] = useState<{ value: string; label: string }[]>(FACT_CATEGORY_OPTIONS);
  // ✅ 2026-10-06：九大章节标题 / 事实属性名 / 来源类型名 —— 权威来源是
  //    GET /global-facts/category-map（此前该端点从未被调用，三套枚举各自
  //    硬编码或干脆不展示）。初值为本地兜底表，拉取成功后覆盖。
  const [factChapterTitles, setFactChapterTitles] = useState<Record<string, string>>(FACT_CHAPTER_TITLES);
  const [factAttrTitles, setFactAttrTitles] = useState<Record<string, string>>(FALLBACK_FACT_ATTR_TITLES);
  const [factSourceKindTitles, setFactSourceKindTitles] = useState<Record<string, string>>(FALLBACK_SOURCE_KIND_TITLES);
  // ✅ 2026-10-06：分页状态（消费后端 pagination；此前从不下发 limit/offset）
  const [factsPage, setFactsPage] = useState(1);
  // ✅ R166：loadFacts 的默认页码走 ref 镜像 —— 旧实现把 factsPage 放进
  //    loadFacts 的 useCallback deps，分页 setState → loadFacts 身份变化 →
  //    挂载 effect 整体重跑（目录全量 + 合规历史 + 专家项 3-4 个无关请求
  //    随每次翻页重发）。ref 镜像后 loadFacts 引用稳定，分页只发事实列表请求。
  const factsPageRef = useRef(1);
  const [factsPagination, setFactsPagination] = useState<FactPagination | null>(null);
  // ✅ 左侧分类面板（2026-09-23）：全局事实 Tab 选中分类（悬空时由
  //    resolveSelectedFactCategory 回退）+ 项目资料卡片折叠态（默认收起）
  const [selectedFactCategory, setSelectedFactCategory] = useState<string>("");
  const [factsDocsCollapsed, setFactsDocsCollapsed] = useState(true);
  // 九大章节字段完整性与危大阈值诊断（后端确定性计算，零 AI）。
  const [factsChapterReport, setFactsChapterReport] = useState<FactChaptersResponse | null>(null);
  const [factsDangerReport, setFactsDangerReport] = useState<FactDangerResponse | null>(null);
  const [factsDiagnosticsLoading, setFactsDiagnosticsLoading] = useState(false);
  // ✅ R45 · D5：自然语言调整（预览 → 二次确认 → 应用）的状态机已抽到
  // 模块级 FactsAdjustPanel，页面上不再持有这组瞬态。
  // 分类聚合：左侧分类面板数据源（label 取分类白名单中文名，缺失回退原文）
  const factCategoryEntries = useMemo(
    () => buildFactsCategoryEntries(
      facts,
      (cat) => factCategoryOptions.find((o) => o.value === cat)?.label || "",
    ),
    [facts, factCategoryOptions],
  );
  const activeFactCategory = resolveSelectedFactCategory(factCategoryEntries, selectedFactCategory);
  const activeFactGroups = useMemo(
    () => filterFactsGroupsByCategory(facts, activeFactCategory || ""),
    [facts, activeFactCategory],
  );
  // 目录生成进度日志（阶段/章节级，实时显示工作内容）
  const [outlineLogs, setOutlineLogs] = useState<OutlineLogItem[]>([]);
  const outlineLogsRef = useRef<OutlineLogItem[]>([]);

  const [exportForm] = Form.useForm();
  const [exporting, setExporting] = useState(false);
  // ✅ D2 加固（2026-10-06）：导出阶段枚举 + 真实进度百分比。
  //    旧实现只有一个文本 phase 字符串，文档生成/转 PDF 阶段
  //    （真正耗时数十秒）只有一句静态文字，用户无从判断是否卡死。
  //    现在由 stage + pct 驱动真实进度条（各阶段权重加权）。
  const [exportStage, setExportStage] = useState<"idle" | "charts" | "docx" | "pdf">("idle");
  const [exportPct, setExportPct] = useState(0);
  // ✅ 导出预检独立 loading（旧实现复用 exporting，点预检不显示加载态、还会误亮导出按钮）
  const [checkingExport, setCheckingExport] = useState(false);
  const [exportIssues, setExportIssues] = useState<ExportIssueGate[]>([]);
  const [exportStats, setExportStats] = useState<ExportStats | null>(null);
  // ✅ G1：就绪度总检摘要（由导出预检接口一并返回，不再需要来回切换页面）
  const [exportPreflight, setExportPreflight] = useState<ExportPreflight | null>(null);
  // ✅ 《待补充清单》（2026-09-24）：导出预检返回的占位符聚合（含按字段/按章节）
  const [placeholderReport, setPlaceholderReport] = useState<PlaceholderReport | null>(null);
  // ✅ 重跑计划（治 F 层第 3 条）：哪些章节的占位字段已有补录数据、可自动重跑
  const [rerunPlan, setRerunPlan] = useState<ExportRerunPlan | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [exportPhase, setExportPhase] = useState<string>("");
  // ✅ 2026-10-01：预检结论只用于展示提示，不再用于禁用导出按钮
  // （旧实现会让「审核与预检未做完」直接卡死导出）。
  const exportGate = useMemo(() => deriveExportGate({
    hasPreflight: exportStats !== null,
    issues: exportIssues,
    readiness: exportPreflight,
  }), [exportIssues, exportPreflight, exportStats]);
  // ===== ✅ 导出格式预设库（对标 OpenBidKit exportFormatPresets）=====
  const [exportPresets, setExportPresets] = useState<ExportPresetItem[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  // 导出域四类请求各自独立取消：预设读取、导出预检、缓存查询、文档导出。
  const presetListAbortRef = useRef<AbortController | null>(null);
  const exportCheckAbortRef = useRef<AbortController | null>(null);
  const cacheAbortRef = useRef<AbortController | null>(null);
  // ✅ BUG 修复（R-1，2026-10-04）：预设列表 4 个操作按钮（刷新 / 保存 / 设为默认 / 删除）
  //    之前完全无 loading 状态，快速双击会并发 POST 造成数据竞态（例如两次保存同名预设）。
  //    用一个"当前进行中的预设操作键"统一标记 —— 任一操作进行中，其他 3 个按钮全部 disabled，
  //    操作结束（成功/失败/取消）后清空。这样比 4 个独立 boolean 少一半状态，也不易漏置位。
  const [presetOpLoading, setPresetOpLoading] = useState<string | null>(null);
  // ✅ BUG 修复（R-3，2026-10-04）：handleExportCheck 只有 abort ref、无同步锁；
  //    React 状态更新异步批处理，同一帧双击仍会并发进入（与 handleExport 的 exportingRef 同病）。
  const checkingExportRef = useRef(false);
  // R-P1 拆分：currentSchemeIdRef 定义自下方 1175 行上移至此 —— useExportPresets
  // hook 调用点在预设块原位置，deps 对象在 render 期即求值，必须先于其声明（避 TDZ）。
  const currentSchemeIdRef = useRef(id || "");
  currentSchemeIdRef.current = id || "";
  // ===== ✅ 接线 exportApi.cacheStatus（历史导出缓存可见）=====
  // R-P1 拆分：声明自下方 816 行上移，setCacheStatus 是 useExportPresets 的 deps
  const [cacheStatus, setCacheStatus] = useState<ExportCacheStatus | null>(null);
  const [cacheLoading, setCacheLoading] = useState(false);
  const {
    fetchExportPresets, handleRefreshPresets, handleSavePreset,
    handleSetDefaultPreset, handleDeletePreset, loadCacheStatus,
  } = useExportPresets({
    id, exportForm, presetOpLoading, setPresetOpLoading, selectedPresetId,
    cacheAbortRef, setCacheLoading,
    currentSchemeIdRef, presetListAbortRef,
    setExportPresets, setSelectedPresetId, setExportIssues,
    setExportStats, setExportPreflight, setPlaceholderReport,
    setRerunPlan, setCacheStatus, msg, modal,
  });
  // ===== ✅ 接线 sectionsApi.quality（正文质量自检：口语化/AI 腔残留 + 废止标准）=====
  //  后端早有该能力但前端从未调用（孤儿接口），属于"后端算得出、用户看不见"的
  //  信息断链；交付前自检的三个预检项在此补齐最后一块。
  const [checkingQuality, setCheckingQuality] = useState(false);
  const [qualityResult, setQualityResult] = useState<QualityCheckResult | null>(null);
  // ===== ✅ 全文一致性 Agent 修复（F-AGENT-CONSISTENCY-REPAIR）=====
  //  正文全部生成后自动执行「扫描 → 仲裁 → 定向修复」：修复前自动留版本快照，
  //  修复结果先写正文但待确认，用户可在「修复工作台」逐条确认或一键回滚。
  const [autoConsistencyRepair, setAutoConsistencyRepair] = useState(true);
  const [consistencySeverity, setConsistencySeverity] = useState<string>("high");
  // ✅ 2026-09-22：强制全量重修 —— 关闭后端「跳过已修复且修复成果仍在正文里」的
  //    优化。默认关（省 AI 调用）；勾选后本批对全部命中冲突重新调用 AI 修复。
  //    同时作用于「正文生成收尾的自动一致性修复」与修复工作台的「一键修复」。
  const [forceFullRepair, setForceFullRepair] = useState(false);
  // ✅ 超字数自动压缩：接通后端早已支持、但前端从未暴露的 auto_shrink_over 能力
  //   （开启后正文生成结束对超目标 130% 的章节自动跑 AI 压缩，只删不改）。
  // ✅ R66（2026-10-11）默认改为开：偏差量化证实生成结果系统性超纲
  //   （均值 137%、57.6% 超 1.3X、零偏低），「偏高永不纠正」是主根因之一；
  //   与后端「给了档位/覆盖即默认开压缩」的新语义对齐。用户可手动关掉。
  const [autoShrinkOver, setAutoShrinkOver] = useState(true);
  // F-CONTENT-STANDARD(2026-09-26): 任务级生成标准选择（"inherit" 表示沿用方案/章节默认）
  // 初始值恒为 "inherit"（= 逐章回落），**不**直接选方案默认：若初始就选中
  // precise，会让「按章节设置」在有章节级覆盖的方案里被静默忽略且用户看不出来。
  // 方案默认改由弹窗文案与章节 Select 的「沿用方案（X）」呈现。
  const [generationStandard, setGenerationStandard] = useState<GenerationStandardChoice>("inherit");
  // 「存为方案默认」按钮的 loading 态
  const [savingStandardDefault, setSavingStandardDefault] = useState(false);
  // 修复工作台（Drawer）
  const [crOpen, setCrOpen] = useState(false);
  const [crScanning, setCrScanning] = useState(false);
  const [crRepairing, setCrRepairing] = useState(false);
  const [crLoading, setCrLoading] = useState(false);
  const [crConflicts, setCrConflicts] = useState<CrConflictItem[]>([]);
  const [crSummary, setCrSummary] = useState<CrSummary | null>(null);
  const [crScanId, setCrScanId] = useState<string>("");
  const [crRepair, setCrRepair] = useState<CrRepairResult | null>(null);
  const [crHistory, setCrHistory] = useState<CrRepairHistoryItem[]>([]);
  const [expertItems, setExpertItems] = useState<string[]>([]);
  const [expertResult, setExpertResult] = useState<ExpertReviewResult | null>(null);
  // ===== ✅ 接线 complianceApi.check / complianceApi.results（规范符合性检查 + 历史记录）=====
  const [checklistText, setChecklistText] = useState(DEFAULT_COMPLIANCE_CHECKLIST.join("\n"));
  const [checkingCompliance, setCheckingCompliance] = useState(false);
  const [complianceResults, setComplianceResults] = useState<ComplianceResultItem[]>([]);
  const [complianceHistory, setComplianceHistory] = useState<ComplianceHistoryItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  // ===== 审核规则库（后端唯一事实源）=====
  // 此前清单在前端硬编码、后端 EXPERT_CHECK_ITEMS、提示词内联文案三处重复维护，
  // 一旦漂移就会出现"前端显示通过、AI 实际判定缺失"。现以后端规则注册表为准：
  // 默认清单由 AI 规则标题生成，提交时携带 rule_ids，使 rule_id 语义稳定可比对。
  const [aiRules, setAiRules] = useState<AuditRule[]>([]);
  const [checklistDirty, setChecklistDirtyState] = useState(false);
  // ✅ BUG 修复配套：镜像脏标记到 ref，供「规则加载完成后回填清单」的 effect 读取
  // 最新值（该 effect 依赖仅 [id]，闭包里的 state 是挂载帧的旧值）。
  const checklistDirtyRef = useRef(false);
  const setChecklistDirty = (v: boolean) => { checklistDirtyRef.current = v; setChecklistDirtyState(v); };
  // 每次进入「审核与预检」Tab 递增，用于触发子组件刷新（总检结果 / 审核清单）
  const [reviewTick, setReviewTick] = useState(0);
  const [uploadingFacts, setUploadingFacts] = useState(false);
  const [parsingDocs, setParsingDocs] = useState(false);
  const [uploadedFiles, setUploadedFiles] = useState<string[]>([]);
  // =====「上传解析」Tab（import）页面态 =====
  // 单文件大小上限（字节）：挂载时从后端 /system/upload-limits 动态下发取得。
  // 未到达 / 请求失败时保持 undefined，由 UploadParseTab 回落内置兜底默认（30MB），
  // 前端不再把 30MB 常量当作权威上限，避免与后端配置漂移。
  const [maxUploadBytes, setMaxUploadBytes] = useState<number | undefined>(undefined);
  // 单次请求累计体积上限（字节）：与单文件上限同源（/system/upload-limits）。
  // 未到达 / 请求失败时保持 undefined，预检回落内置兜底（200MB），与后端一致。
  const [maxUploadTotalBytes, setMaxUploadTotalBytes] = useState<number | undefined>(undefined);
  // 单次请求文件数上限（个）：与单文件上限同源（/system/upload-limits）。
  // 未到达 / 请求失败时保持 undefined，预检回落内置兜底（20），与后端一致。
  const [maxUploadFiles, setMaxUploadFiles] = useState<number | undefined>(undefined);
  // 正在解析的单份文档 id（列表行内按钮 loading / 全列表禁用依据）
  const [parsingDocId, setParsingDocId] = useState<string | null>(null);
  // 文件分类下拉选项（后端 /global-facts/documents/category-options）
  const [categoryOptions, setCategoryOptions] = useState<string[]>([]);
  // 解析内容预览弹窗
  const [previewDoc, setPreviewDoc] = useState<DocRecord | null>(null);
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  // 预览接口的完整响应：用于派生「文档内容不完整 / 仅预览截取 / 解析告警」提示。
  // 只存 text 会把后端已经算好的 truncated / parse_warnings 丢掉，
  // 用户在预览里看到的是一段看起来正常的短正文，无法判断内容是否真的完整。
  const [previewData, setPreviewData] = useState<DocPreviewData | null>(null);
  // ✅ 四层存储（解析质量）预览接线：预览弹窗内展示 status/completeness 概览，
  //    并提供重新解析 / 物化提取层 / 交叉校验 / 刷新质量四个动作。
  const [pipelineStatus, setPipelineStatus] = useState<DocumentPipelineStatusLike | null>(null);
  const [pipelineLoading, setPipelineLoading] = useState(false);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [pipelineAction, setPipelineAction] = useState<PipelineActionKey | null>(null);
  // =====「提取项目」Tab（bidAnalysis）页面态 =====
  const [baDefs, setBaDefs] = useState<BaItemDef[]>([]);
  // 解析项定义（/bid-analysis/items）加载失败原因：非空时 Tab 展示错误态 + 重试
  const [baDefsError, setBaDefsError] = useState<string | null>(null);
  const [baGroups, setBaGroups] = useState<BaGroup[]>([]);
  const [baItems, setBaItems] = useState<BaStoredItem[]>([]);
  const [baSummary, setBaSummary] = useState<BaSummary | null>(null);
  const [selectedBaItem, setSelectedBaItem] = useState<BaStoredItem | null>(null);
  // 2026-09-23 需求拆分：左侧列表 & 右侧详情分别渲染 ParseResultCategoryPanel，
  // 通过共享对象串联展开/激活状态，避免"点击左侧分类，右侧详情不同步"的割裂。
  const sharedParseState = useRef<ParsePanelSharedState>({
    expanded: {},
    activeKey: null,
  }).current;
  const [sectionChecking, setSectionChecking] = useState(false);
  const [sectionCheckResult, setSectionCheckResult] = useState<Record<string, unknown> | null>(null);
  // 配置弹窗（mode = key 必选 / full 全部 / custom 自定义勾选）
  const [baConfigOpen, setBaConfigOpen] = useState(false);
  const [baModeOpt, setBaModeOpt] = useState<"key" | "full" | "custom">("key");
  const [baSelectedIds, setBaSelectedIds] = useState<string[]>([]);
  const [baForceRerun, setBaForceRerun] = useState(false);
  // 「查看完整」弹窗 + 本地 SSE 运行态（进度优先取本地事件，轮询作兜底）
  const [baFullItem, setBaFullItem] = useState<BaStoredItem | null>(null);
  // ✅ 人工校正闭环（2026-09-23 复原）：AI 抽错关键参数时直接改写单项结果（source='manual'）。
  //   后端 PUT/DELETE /results/{item_id} 已实现；组件 BidAnalysisTab 与「解析信息分类
  //   显示栏」均有「人工校正」入口（onEditItem 真值才渲染）。此前页面从未传 onEditItem
  //   → 按钮在生产环境从不渲染，这里补齐 openBaEdit / handleBaSaveEdit / handleBaClearEdit。
  const [baEditItem, setBaEditItem] = useState<BaEditItemLike | null>(null);
  const [baEditValue, setBaEditValue] = useState("");
  const [baEditSaving, setBaEditSaving] = useState(false);
  // 「解析信息分类显示栏」加载/错误态（供上传解析 Tab 复用）
  const [baLoading, setBaLoading] = useState(false);
  const [baError, setBaError] = useState<string | null>(null);
  const [baRunning, setBaRunning] = useState(false);
  const [baProgress, setBaProgress] = useState(0);
  const [baProgressMsg, setBaProgressMsg] = useState("");
  // ✅ 提取规模（SSE text_stats 事件）：项数 × 切段数 ≈ 模型调用次数。
  //   缺了它，超长文档要等十几分钟后从账单里才发现额度被烧光。
  const [baTextStats, setBaTextStats] = useState<BaTextStats | null>(null);
  const baSseAbortRef = useRef<AbortController | null>(null);
  const baDefsRef = useRef<BaItemDef[]>([]);
  const [activeTab, setActiveTab] = useState("content");
  // ✅ 2026-09-23「上传解析 + 提取项目」合并：import 内部子 Tab（docs = 文档解析 / extract = 项目提取）
  //    顶层 Tab 数量从 7 降到 6，bidAnalysis 不再是独立 Tab key，而是 import 下的子 Tab。
  const [importSubTab, setImportSubTab] = useState<"docs" | "extract">("docs");
  // ✅ F2 导航竞态守卫：镜像当前 Tab 到 ref，异步回调（SSE 完成等）里的自动跳转
  //   仅在用户仍停留在「触发跳转的那个 Tab」时才执行，避免把已手动切走的用户拽回。
  const activeTabRef = useRef<string>("content");
  useEffect(() => { activeTabRef.current = activeTab; }, [activeTab]);
  // ✅ F2 导航竞态守卫：仅当用户仍停留在 origin 页时才自动跳到 target，
  //   避免异步完成回调（提取/生成结束）把已手动切走的用户强行拽回。
  const autoJumpFrom = useCallback((origin: WorkflowTabKey, target: WorkflowTabKey) => {
    if (activeTabRef.current === origin) setActiveTab(target);
  }, []);
  // 首次数据加载完成后是否已按方案状态选定起始 Tab（只修正一次，避免后续数据刷新回跳）
  const initialTabDecidedRef = useRef(false);
  // 目录树默认展开所有节点
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([]);
  // ✅ 性能优化：用 ref 持有 tree / expandedKeys 的最新值，使目录树编辑回调可稳定为
  //    useCallback（不再随每次渲染重建引用），从而保护 SchemeTreePanel 的 memo，
  //    避免生成期间（progress 每帧变化）整棵目录树被重渲——这是桌面端生成卡顿根因之一。
  //    这些回调均为用户事件触发（点击/拖拽），调用时读取 ref.current 即拿到最新值，与
  //    原先闭包捕获的「当前渲染帧 tree」语义等价。
  const treeRef = useRef(tree);
  treeRef.current = tree;
  const expandedKeysRef = useRef(expandedKeys);
  expandedKeysRef.current = expandedKeys;
  // 字数预算设置
  const [wordBudgetOption, setWordBudgetOption] = useState<string>("default");
  const [customWordBudget, setCustomWordBudget] = useState<number>(2000);
  // 章节级生成日志（滚动显示）
  const [sectionLogs, setSectionLogs] = useState<SectionLogItem[]>([]);
  const sectionLogsRef = useRef<SectionLogItem[]>([]);
  /**
   * ✅ 合并后端下发的失败章节明细（completed / stopped / **error** 三类终态共用）。
   *   提到此处（`onSseEvent` 之上）是因为 `onSseEvent` 是 `useCallback([])`：
   *   它捕获的是**首帧闭包**，只能安全引用同样只依赖 ref + 稳定 setter 的函数。
   *   本函数只写 `sectionLogsRef` 与 `setSectionLogs`（后者是稳定引用），
   *   因此跨渲染恒定安全。纯函数在无明细时原样返回同一引用 → 跳过 setState。
   */
  const mergeFailedSectionsIntoRef = useCallback((list: unknown) => {
    const next = mergeFailedSectionsInto(sectionLogsRef.current, list);
    if (next === sectionLogsRef.current) return;
    sectionLogsRef.current = next;
    setSectionLogs([...next]);
  }, []);
  // ✅ 进度增强：正文生成运行统计（后端 stats / ping 事件下发）
  const [genStats, setGenStats] = useState<GenStats>({});
  // 1 秒心跳：用于"生成中"条目的实时耗时显示
// 选中章节的编辑模式
const [isEditingSection, setIsEditingSection] = useState(false);
const [editContent, setEditContent] = useState("");
const [savingSection, setSavingSection] = useState(false);
// ✅ 编辑未保存追踪：editDirty=true 表示有未保存的更改
const [editDirty, setEditDirty] = useState(false);
const prevSectionKeyRef = useRef<string | null>(null);
// ✅ 草稿 localStorage key（依赖 selectedSection 和 id）
const draftKey = selectedSection && id
  ? `scheme_draft_${id}_${selectedSection.key}`
  : null;
  // 并发数设置
  const [concurrencyOption, setConcurrencyOption] = useState<string>("balanced");

  // ===== ✅ 接线 outlineLibraryApi.applyAndSave / uploadOutlineApi.saveAsLibrary =====
  /** 目录库选择弹窗（只列「已通过」的库） */
  const [libOpen, setLibOpen] = useState(false);
  const [libItems, setLibItems] = useState<OutlineLibraryItem[]>([]);
  const [libLoading, setLibLoading] = useState(false);
  const [libApplying, setLibApplying] = useState(false);
  const [libKeyword, setLibKeyword] = useState("");
  /** 最近一次「导入目录（智能识别）」的 upload 记录，用于「存为目录库」 */
  const [lastUpload, setLastUpload] = useState<{ id: string; name: string; outline: OutlineNodeInput[] } | null>(null);
  // ✅ 接线后端 reorganize 能力：导入识别时是否先按标准章节骨架归位
  const [outlineImportReorganize, setOutlineImportReorganize] = useState(false);
  const [saveLibOpen, setSaveLibOpen] = useState(false);
  const [saveLibForm] = Form.useForm();

  const activeTaskIdRef = useRef<string>("");
  // ✅ R106 FIX-4：控制指令同步锁。React state 更新异步批处理，快速连点会在同
  //    一帧内多次 POST /sse/task/{id}/control。pause/resume 在 finally 释放，
  //    stop 的锁延后到 generating 复位（终态）才释放，与「停止中」置灰同生命周期。
  const controllingRef = useRef(false);
  // ✅ R106 FIX-4：结构化提取（bid_analysis）停止锁 + 置灰态。协作式停止要在途
  //    解析项收口，故发完指令后轮询终态再回执（旧实现只 toast 一句就结束）。
  const baStoppingRef = useRef(false);
  const [baStopping, setBaStopping] = useState(false);
  // ✅ 生成世代计数：重入生成（如双击）时旧 SSE 协程被 abort 后其收尾代码
  // 会在新任务启动后才执行，若无世代校验，旧协程的 finally 会把新任务的
  // generating 复位为 false → 暂停/停止按钮消失，运行中的任务无法停止。
  const genSeqRef = useRef(0);
  // ✅ 2026-09-26：本次「目录生成」是否已就正文覆盖风险做过二次确认。
  // 闸门保存是唯一会整表重建 sections 的入口（AI 回传的 id 是展示编号，必然匹配不到
  // 旧 DB 主键）—— 旧实现直接静默保存、用户毫无提示地丢掉全部已生成正文。
  // 每次新生成重置，允许每代各确认一次。
  const hasContentRef = useRef(false);
  // 生成中的刷新定时器
  const refreshTimerRef = useRef<number | null>(null);
  // ✅ 修复 stale closure：用 ref 保存 selectedSection 最新值，避免 load 回调读到旧值
  const selectedSectionRef = useRef<SelectedSectionLike | null>(null);
  selectedSectionRef.current = selectedSection;
  // ✅ SSE 取消控制器：组件卸载或用户中止时中断 fetch
  const abortControllerRef = useRef<AbortController | null>(null);
  // ✅ 性能优化：SSE 事件高频 setState 的 rAF 批量器（sectionLogs / progress / progressMsg / genStats）。
  //    后端 stats/ping 每 100–1000ms 推一次；正文 section_start/stage/done 在单章节期间也是
  //    亚秒级密集触发。每事件一次 render 会把整页 + GenerationProgressCard + SectionLogList 打满，
  //    INP 与掉帧都会跟着放大。批量器按 key 只保留最新值、下一帧统一刷，保留最终语义同时把
  //    render 频率压到 ≤ 1 次/帧。组件卸载时 stop() 释放所有 pending。
  const sseBatcherRef = useRef<ReturnType<typeof createSseBatcher> | null>(null);
  if (sseBatcherRef.current === null) {
    sseBatcherRef.current = createSseBatcher();
  }
  // ✅ 性能优化：导出专用的进度合帧批量器（独立于 sseBatcherRef —— 导出与 SSE
  // 生成可能同时存在，共用会互相覆盖同 key 的待刷任务）
  const exportBatcherRef = useRef<ReturnType<typeof createSseBatcher> | null>(null);
  if (exportBatcherRef.current === null) {
    exportBatcherRef.current = createSseBatcher();
  }
  // ✅ 全局事实上传提取取消控制器：组件卸载时取消进行中的上传，避免 net::ERR_ABORTED 控制台噪音
  const uploadAbortRef = useRef<AbortController | null>(null);
  // ✅ 文档解析取消控制器：解析（尤其 OCR）耗时可达数分钟，卸载/切方案时中断请求
  const parseAbortRef = useRef<AbortController | null>(null);
  // 列表请求独立取消通道：切方案时必须同时中止 SSE、上传、解析与普通 GET。
  const factsListAbortRef = useRef<AbortController | null>(null);
  const factsDocsListAbortRef = useRef<AbortController | null>(null);
  // 仅在 effect 中推进，用于识别真实方案切换；render 阶段不能改它。
  const mountedSchemeIdRef = useRef(id || "");
  const loadFactsRef = useRef<() => void>(() => undefined);
  // ✅ 文档导出取消控制器：覆盖图表清单、Mermaid 渲染和最终 HTTP 请求，切方案/卸载时整链取消。
  const exportAbortRef = useRef<AbortController | null>(null);
  const exportSeqRef = useRef(0);

  const load = useCallback(async () => {
    if (!id) return;
    const requestedSchemeId = id;
    setLoading(true);
    try {
      const { data } = await sectionsApi.list(requestedSchemeId);
      if (requestedSchemeId !== currentSchemeIdRef.current) return;
      setScheme(data.scheme);
      const newTree = buildTree(data.tree || []);
      setTree(newTree);
      // 默认展开全部
      const allKeys = collectAllKeys(newTree);
      setExpandedKeys((prev) => (prev.length === 0 ? allKeys : prev));
      // 使用 ref 读取最新的 selectedSection，解决 stale closure 问题
      const currentSelected = selectedSectionRef.current;
      if (currentSelected) {
        const fresh = findSectionById(newTree, currentSelected.key!);
        if (fresh) setSelectedSection(fresh);
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      if (requestedSchemeId === currentSchemeIdRef.current) msg.error(err.message || "加载失败");
    }
    if (requestedSchemeId === currentSchemeIdRef.current) setLoading(false);
  }, [id]);

  // ✅ 后台任务监听：轮询本方案运行中的后台任务（实时态同步到全局 Header 消息中心
  //    展示进度提示）；任务进入终态后自动刷新页面数据（兑现「完成后自动刷新」）。
  //    suppressNotify：页面自身生成流程已有自己的消息记录，避免重复记入消息中心。
  const generatingRef = useRef(false);
  generatingRef.current = generating;
  const liveTask = useSchemeLiveTask(id, {
    onFinished: () => {
      load();
      // 后台事实任务完成/失败/停止均可能已改变事实表；不能只刷新目录。
      loadFactsRef.current();
    },
    suppressNotify: () => generatingRef.current,
  });
  // 「提取项目」运行态兜底：页面刷新后本地 SSE 态丢失，用轮询任务恢复 UI
  const liveBa = liveTask && liveTask.task_type === "bid_analysis" ? liveTask : null;
  const baActive = baRunning || (!!liveBa && (liveBa.status === "running" || liveBa.status === "paused"));
  const baShownProgress = baRunning ? baProgress : (liveBa?.progress || 0);
  const baShownMsg = baRunning ? baProgressMsg : (liveBa?.message || "");
  baDefsRef.current = baDefs;

  // ✅ P0-7 轮询瘦身：生成期间的轻量目录刷新（不拉 content 大字段、不覆盖
  //    正在编辑的 selectedSection），增量由 SSE 事件即时推送，轮询仅作状态兜底
  const refreshTreeLight = useCallback(async () => {
    if (!id) return;
    try {
      const { data } = await sectionsApi.list(id, { include_content: false });
      const newTree = buildTree(data.tree || []);
      // ✅ 性能优化：3s 轮询即使数据未变也会产生全新树对象，新引用触发
      // treeData / outlineEditTreeData / countGeneratedLeaves / collectAllKeys
      // 全量重算 + 整页重渲。加指纹守卫（对齐 setGenStats 的 statsEqual），
      // 无变化时保留旧引用，React 直接跳过本次渲染。
      setTree((prev) => (treeFingerprint(prev) === treeFingerprint(newTree) ? prev : newTree));
      const allKeys = collectAllKeys(newTree);
      setExpandedKeys((prev) => (prev.length === 0 ? allKeys : prev));
    } catch {
      // 生成中轮询失败静默：SSE 与断线兜底（pollTaskUntilTerminal）继续工作
    }
  }, [id]);

  const loadFactsDiagnostics = useCallback(async (options?: { signal?: AbortSignal }) => {
    if (!id) return;
    const requestedSchemeId = id;
    setFactsDiagnosticsLoading(true);
    try {
      // ✅ R75 · G-D：Promise.all → Promise.allSettled，单侧失败只降级
      //    该侧（成功侧照常落板），不再连带丢弃另一侧成果。
      const [chaptersRes, dangerRes] = await Promise.allSettled([
        factsApi.chapters(requestedSchemeId, { signal: options?.signal }),
        factsApi.dangerCheck({}, requestedSchemeId, { signal: options?.signal }),
      ]);
      if (requestedSchemeId !== currentSchemeIdRef.current) return;
      const merged = mergeFactsDiagnosticsSettled(chaptersRes, dangerRes);
      if (merged.aborted) return;
      if (merged.chapters !== undefined) setFactsChapterReport(merged.chapters);
      if (merged.danger !== undefined) setFactsDangerReport(merged.danger);
      if (merged.warning && requestedSchemeId === currentSchemeIdRef.current) {
        msg.warning(merged.warning);
      }
    } finally {
      if (requestedSchemeId === currentSchemeIdRef.current) setFactsDiagnosticsLoading(false);
    }
  }, [id, msg]);

  const loadFacts = useCallback(async (pageOverride?: number) => {
    if (!id) return;
    const requestedSchemeId = id;
    factsListAbortRef.current?.abort();
    const ac = new AbortController();
    factsListAbortRef.current = ac;
    // ✅ 2026-10-06：真正使用后端分页（此前从不传 limit/offset，
    //   pagination 每次下发却被丢弃 —— 分组一多就整屏渲染，且首屏白等全量）。
    const wantPage = Math.max(1, Number(pageOverride) || factsPageRef.current);
    const offset = (wantPage - 1) * FACTS_PAGE_SIZE;
    try {
      const { data } = await factsApi.list(requestedSchemeId, {
        signal: ac.signal, limit: FACTS_PAGE_SIZE, offset,
      });
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal)) return;
      setFacts(data.groups || []);
      setFactsSummary(buildFactsSummary(data.stats));
      setFactsPagination(data.pagination || null);
      setFactsPage(wantPage);
      factsPageRef.current = wantPage;
      void loadFactsDiagnostics();
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) return;
      console.warn("加载全局事实失败:", err?.message || e);
      msg.warning(err?.message || "加载全局事实失败，请稍后重试");
    } finally {
      if (isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) {
        setFactsReady(true);
      }
      if (factsListAbortRef.current === ac) factsListAbortRef.current = null;
    }
  }, [id, msg, loadFactsDiagnostics]);

  loadFactsRef.current = loadFacts;

  // ✅ R45 · D5：AI 调整事实的状态机已抽到模块级 FactsAdjustPanel，
  // 页面只保留后端契约接线（见下方 <FactsAdjustPanel /> 处的 preview/apply）。

  // ✅ 加固：以 scheme id 为主查询（后端反查 project_id），
  //    不再依赖 scheme.project_id —— 该字段缺失时旧实现会静默 return，导致列表恒空
  const loadDocuments = useCallback(async () => {
    if (!id) return;
    const requestedSchemeId = id;
    factsDocsListAbortRef.current?.abort();
    const ac = new AbortController();
    factsDocsListAbortRef.current = ac;
    try {
      const { data } = await factsApi.listDocuments({
        schemeId: requestedSchemeId,
        projectId: scheme?.project_id,
        signal: ac.signal,
      });
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal)) return;
      setDocList(data.documents || []);
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) return;
      msg.warning(err?.message || "加载已上传资料列表失败");
    } finally {
      if (factsDocsListAbortRef.current === ac) factsDocsListAbortRef.current = null;
    }
  }, [id, schemeProjectId, msg]);

  // =====「提取项目」(bidAnalysis) 数据加载 =====
  /** 解析项定义 + 分组（后端 /bid-analysis/items 为唯一权威源） */
  const loadBaMeta = useCallback(async () => {
    try {
      const { data } = await bidAnalysisApi.items();
      const defs: BaItemDef[] = data.items || [];
      setBaDefs(defs);
      setBaGroups(normalizeBaGroups(data.groups || [], defs));
      setBaDefsError(null);
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string; response?: { data?: { detail?: unknown } } };
      console.warn("加载解析项定义失败:", err?.message || e);
      // 不再只留 console.warn：旧实现失败后组件永久停在「定义加载中…」，
      // 用户既不知道出错也没有重试入口（2026-09-23 遗留项修复）。
      setBaDefsError(String(err?.response?.data?.detail || err?.message || "网络异常"));
    }
  }, []);

  /** 已存储的解析项结果 + 汇总（后端 /bid-analysis/results） */
  const loadBaResults = useCallback(async () => {
    if (!id) return;
    setBaLoading(true);
    setBaError(null);
    try {
      const { data } = await bidAnalysisApi.results(id, scheme?.project_id);
      const items: BaStoredItem[] = data.items || [];
      setBaItems(items);
      setBaSummary(data.summary || recomputeBaSummary(items, baDefsRef.current));
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string; response?: { data?: { detail?: string } } };
      console.warn("加载结构化提取结果失败:", err?.message || e);
      setBaError(err?.response?.data?.detail || err?.message || "加载解析结果失败");
    } finally {
      setBaLoading(false);
    }
  }, [id, schemeProjectId]);

  // ✅ BUG 修复：筛选器"悬空"。用户选中「待审核」后批量确认 → unresolved 归零
  //    → 对应的 Radio.Button 因 `>0` 条件被隐藏，但 factsFilter 仍停在
  //    "unresolved" → 所有分组被过滤掉，界面变成空白且无法点回「全部」，
  //    只能刷新页面。这里在当前筛选已无结果时自动回退到「全部」。
  useEffect(() => {
    if (factsFilter === "all" || facts.length === 0) return;
    // ✅ 口径收敛（2026-09-21）：命中判定唯一事实源 hasFactsFilterMatch（模块级可单测）
    if (!hasFactsFilterMatch(facts, factsFilter)) setFactsFilter("all");
  }, [facts, factsFilter]);

  /** ✅ 接线 complianceApi.results：拉取历史检查记录（check_type=compliance） */
  const loadComplianceHistory = useCallback(async () => {
    if (!id) return;
    setHistoryLoading(true);
    try {
      // ✅ G7：只取最近 50 条历史 —— 大方案反复检查会累积数百条记录，
      // 全量拉回既拖慢首屏也影响列表渲染；列表本身已明确「最近 N 次」
      const { data } = await complianceApi.results(id, "compliance", 50);
      setComplianceHistory(data.items || []);
    } catch { /* ignore */ }
    setHistoryLoading(false);
  }, [id]);

  // ===== ✅ 全局事实：手工新增 / 修改（factsApi.create / factsApi.update）=====
  function openFactCreate() {
    setFactEditingId(null);
    setFactEditingCategory("");
    factForm.resetFields();
    setFactModalOpen(true);
  }

  function openFactEdit(group: FactGroupLike) {
    setFactEditingId(group.id!);
    setFactEditingCategory(group.category || "");
    // ✅ 回传 category：否则后端重建分组时会因缺省把归类降级为 other
    factForm.setFieldsValue({
      title: group.title,
      content: group.content,
      category: group.category || undefined,
    });
    setFactModalOpen(true);
  }

  async function submitFact() {
    if (!id) return;
    try {
      const values = await factForm.validateFields();
      setFactSubmitting(true);
      const category = values.category || factEditingCategory || undefined;
      if (factEditingId) {
        // FactGroupUpdate 模型要求 id 必填，故一并提交
        await factsApi.update(factEditingId, {
          id: factEditingId,
          title: values.title,
          content: values.content,
          category,
        }, id);
        msg.success("事实条目已更新");
      } else {
        await factsApi.create(
          { title: values.title, content: values.content, category }, id);
        msg.success("事实条目已新增");
      }
      setFactModalOpen(false);
      setFactEditingId(null);
      setFactEditingCategory("");
      factForm.resetFields();
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string; errorFields?: unknown };
      if (err.errorFields) return;
      msg.error(err.message || "保存失败");
    } finally {
      setFactSubmitting(false);
    }
  }

  // ===== ✅ 单条事实编辑（走 PATCH item_updates，不重建整个分组）=====
  function openFactItemEdit(group: FactGroupLike | null, item: FactItemLike) {
    setFactItemEditing({ ...item, groupId: group?.id, groupTitle: group?.title });
    factItemForm.setFieldsValue({
      name: item.name,
      value: item.value,
      category: item.category || group?.category || undefined,
      is_simulated: !!item.is_simulated,
      // ✅ R81：要求 vs 事实口径回填（缺省未区分 → 空串）
      value_kind: item.value_kind || "",
    });
    setFactItemModalOpen(true);
  }

  async function submitFactItem() {
    if (!factItemEditing?.fact_id) return;
    try {
      const values = await factItemForm.validateFields();
      setFactItemSubmitting(true);
      await factsApi.update(factItemEditing.fact_id, {
        id: factItemEditing.fact_id,
        item_updates: [{
          fact_id: factItemEditing.fact_id,
          name: values.name,
          value: values.value,
          category: values.category || undefined,
          is_simulated: !!values.is_simulated,
          // ✅ R81：口径随保存透传（后端 _apply_item_updates 归一；
          //    undefined 归一为 '' = 未区分，与旧行为等价）
          value_kind: values.value_kind ?? "",
        }],
      }, id);
      msg.success("事实已更新");
      setFactItemModalOpen(false);
      setFactItemEditing(null);
      factItemForm.resetFields();
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string; errorFields?: unknown };
      if (err.errorFields) return;
      msg.error(err.message || "保存失败");
    } finally {
      setFactItemSubmitting(false);
    }
  }

  // ===== ✅ 断链修复：分组删除 / 单条确认 / 矛盾裁决 / 一键清空 =====
  //    此前 factsApi.delete / clearAll 已封装但前端无任何调用点，
  //    openFactEdit 为死代码；分组级删不掉、也不能一键清空。
  const handleDeleteFactGroup = (group: FactGroupLike) => {
    modal.confirm({
      title: "删除事实分组",
      content: `将删除「${group.title}」及其全部 ${group.items?.length ?? 0} 条事实（含已确认与手动录入），不可撤销。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      // ✅ 避免 antd 命令式 confirm 在 onOk 抛错时产生 unhandled rejection：
      //    异常在内部 try/catch 消化，不向外抛出
      onOk: async () => {
        try {
          await factsApi.delete(group.id!, id);
          msg.success("事实分组已删除");
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err?.message || "删除失败");
        }
      },
    });
  };

  const handleResolveFactItem = async (item: FactItemLike) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolve(item.fact_id, id);
      msg.success("已确认");
      // ✅ 等待刷新完成（2026-09-21）：旧实现未 await，用户紧接着点「一键清空」/
      //    切 Tab 时会读到旧 stats，徽标 /「全部就绪」入口短暂错乱。
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "确认失败");
    }
  };

  const handleResolveConflictItem = async (item: FactItemLike, value: string) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolveConflict(item.fact_id, value, id);
      msg.success("矛盾已裁决");
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "裁决失败");
    }
  };

  const handleClearAllFacts = () => {
    if (!id) return;
    modal.confirm({
      title: "一键清空全部事实",
      content: "将删除本方案可见的全部全局事实（包含项目共享事实，会影响同项目其它方案），同时重置 AI 增量提取进度并清空相关导出缓存。已上传的资料文档保留，可重新执行「③ AI 提取事实」重建。",
      okText: "全部清空",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await factsApi.clearAll(id);
          setFactsFilter("all");
          setFactsSegmentStats(null);
          setFactsCrossConflicts([]);
          msg.success(`已清空 ${data?.deleted ?? 0} 条事实，提取进度已重置`);
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err?.message || "清空失败");
        }
      },
    });
  };

  useEffect(() => {
    load();
    loadFacts();
    loadComplianceHistory();
    complianceApi.expertItems().then(({ data }) => setExpertItems(data.items || [])).catch(() => {});
  }, [id, load, loadFacts, loadComplianceHistory]);

  // 上传配额动态下发：该配置全局通用、与具体方案无关，挂载时拉取一次即可。
  // 失败静默保留 undefined（组件回落兜底默认）——后端临时不可达不应阻断页面主流程。
  useEffect(() => {
    let alive = true;
    systemApi
      .uploadLimits()
      .then(({ data }) => {
        if (!alive) return;
        const n = Number(data?.max_upload_bytes);
        if (Number.isFinite(n) && n > 0) setMaxUploadBytes(n);
        const t = Number(data?.max_total_bytes);
        if (Number.isFinite(t) && t > 0) setMaxUploadTotalBytes(t);
        const f = Number(data?.max_files_per_request);
        if (Number.isFinite(f) && f > 0) setMaxUploadFiles(f);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // ✅ 加载后端审核规则库，用规则标题覆盖前端默认清单（消除与后端/提示词的分叉）。
  //    用户一旦手工编辑过清单（checklistDirty），后续不再覆盖，保留自定义能力。
  useEffect(() => {
    if (!id) return;
    let alive = true;
    complianceApi.rules()
      .then(({ data }) => {
        if (!alive) return;
        const rules: AuditRule[] = (data.items || []).filter(
          (r: AuditRule) => r.mode === "ai");
        setAiRules(rules);
        // ✅ BUG 修复：React 要求 state updater 必须是纯函数，禁止在 updater 内
        // 调用其它 setState。旧实现把 setChecklistText 塞进 setChecklistDirty 的
        // updater —— StrictMode 开发态下 updater 会被调用两次，产生两次冗余
        // setChecklistText（额外渲染 + 不可预期的重入）。副作用移到 updater 外，
        // 用 ref 读取最新脏标记（updater 保持纯）。语义不变：用户手工编辑过
        // 清单就不再覆盖，保留自定义能力。
        if (rules.length && !checklistDirtyRef.current) {
          setChecklistText(rules.map((r) => r.title).join("\n"));
        }
      })
      .catch(() => { /* 规则加载失败时沿用前端内置默认清单 */ });
    return () => { alive = false; };
  }, [id]);

  // ✅ 每次切入「审核与预检」Tab 触发子组件刷新（总检历史 / 审核清单）
  useEffect(() => {
    if (activeTab === "review") setReviewTick((t) => t + 1);
  }, [activeTab]);

  // ✅ 进入工作台即拉取资料文档列表（按 scheme id 查询，不依赖 project_id 是否已加载）
  useEffect(() => {
    if (id) loadDocuments();
  }, [id, loadDocuments]);

  // 「上传解析」/「提取项目」模块的一次性元数据加载（分类选项 + 解析项定义）
  useEffect(() => {
    if (!id) return;
    let alive = true;
    factsApi.categoryOptions()
      .then(({ data }) => { if (alive) setCategoryOptions(data.options || []); })
      .catch(() => { /* 选项加载失败时分类降级为只读 Tag */ });
    // ✅ 事实分类单一事实源（后端 CATEGORY_TITLES），失败时静默保留本地兜底
    factsApi.categories()
      .then(({ data }) => { if (alive && Array.isArray(data.categories) && data.categories.length) setFactCategoryOptions(data.categories); })
      .catch(() => { /* 拉取失败时沿用 FACT_CATEGORY_OPTIONS */ });
    // ✅ 2026-10-06：消费此前**从未被调用**的 /global-facts/category-map。
    //    该端点由后端 facts_classification.category_map_payload() 生成，
    //    是九大章节标题 / 事实属性名 / 来源类型名的唯一权威来源；前端此前
    //    各自硬编码（章节标题 2 份副本 + 事实属性 4 个内联三元 + 来源类型
    //    干脆不展示）→ 后端增删枚举后界面静默失真。
    //    失败时静默沿用本地兜底表（不阻断页面）。
    factsApi.categoryMap()
      .then(({ data }) => {
        if (!alive) return;
        const chapters = Array.isArray(data?.chapters) ? data.chapters : [];
        if (chapters.length) {
          const m: Record<string, string> = {};
          chapters.forEach((c) => { if (c?.key && c?.title) m[c.key] = c.title; });
          if (Object.keys(m).length) setFactChapterTitles(m);
        }
        if (data?.fact_attr_titles && Object.keys(data.fact_attr_titles).length) {
          setFactAttrTitles(data.fact_attr_titles);
        }
        if (data?.source_kind_titles && Object.keys(data.source_kind_titles).length) {
          setFactSourceKindTitles(data.source_kind_titles);
        }
      })
      .catch(() => { /* 拉取失败时沿用本地兜底表 */ });
    loadBaMeta();
    return () => { alive = false; };
  }, [id, loadBaMeta]);

  // 「提取项目」结果加载（scheme/project 就绪后拉一次）
  useEffect(() => {
    loadBaResults();
  }, [loadBaResults]);

  // ✅ 切入「提取项目」子 Tab 时自动选中第一个已完成项（2026-09-23 合并：改由 importSubTab 触发）
  useEffect(() => {
    if (activeTab === "import" && importSubTab === "extract") {
      setSelectedBaItem((prev) => {
        if (prev) return prev;
        return findFirstDoneBaItem(baItems, baDefs);
      });
      setSectionCheckResult(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, importSubTab]);

  // ✅ 切换方案时重置起始 Tab 判定 + facts 瞬态。
  //    /scheme/:id 是同一个路由组件，方案间切换不会卸载组件；
  //    旧实现只在首次挂载判定一次，打开第二个方案时会沿用上一个方案的 Tab；
  //    且提取日志/分段失败/跨段矛盾会残留上一个方案的数据，直到下一次提取才被覆盖。
  useEffect(() => {
    const previousSchemeId = mountedSchemeIdRef.current;
    mountedSchemeIdRef.current = id || "";
    currentSchemeIdRef.current = id || "";
    // 上一轮 effect 的清理与本轮 effect 都会执行；这里在 id 变化时立即中止旧请求。
    if (previousSchemeId && previousSchemeId !== id) {
      ++genSeqRef.current;
      ++exportSeqRef.current;
      abortControllerRef.current?.abort();
      uploadAbortRef.current?.abort();
      parseAbortRef.current?.abort();
      factsListAbortRef.current?.abort();
      factsDocsListAbortRef.current?.abort();
      baSseAbortRef.current?.abort();
      exportCheckAbortRef.current?.abort();
      cacheAbortRef.current?.abort();
      exportAbortRef.current?.abort();
      sseBatcherRef.current?.stop();
      exportBatcherRef.current?.stop();
      exportingRef.current = false;
      setExporting(false);
      setExportPhase("");
      setCheckingExport(false);
      setCacheLoading(false);
    }
    initialTabDecidedRef.current = false;
    setFacts([]);
    setFactsSummary(null);
    setFactsReady(false);
    setFactsFilter("all");
    setFactsLogs([]);
    factsLogsRef.current = [];
    setFactsSegmentStats(null);
    setFactsCrossConflicts([]);
    setFactsChapterReport(null);
    setFactsDangerReport(null);
    setFactModalOpen(false);
    setFactItemModalOpen(false);
    setSelectedFactCategory("");
    // ✅ 切换方案时重置「提取项目」的选中态：/scheme/:id 是同一路由组件，
    //    不重置则右侧面板继续显示上一个方案的提取项详情与多标段检测结果
    //    （与上方 facts 瞬态残留同因：旧实现只在进入 extract 子 Tab 时判定一次）。
    setSelectedBaItem(null);
    setSectionCheckResult(null);
  }, [id]);

  // ✅ 起始 Tab 判定：统一收敛到 utils/workflowDerived 的 pickInitialTab
  //    （2026-09-17 产品要求：打开工作台一律先落在第一步「上传解析」）。
  //    旧实现在此内联了另一套「智能跳步」逻辑，两套口径并存且互相矛盾。
  //    factsTotal 仅作"数据是否加载完成"的判据（加载失败也会在 finally 落定）。
  useEffect(() => {
    if (initialTabDecidedRef.current) return;
    if (!scheme || loading) return;
    const target = pickInitialTab({
      docs: docList,
      tree,
      factsTotal: factsReady ? (factsSummary?.total ?? 0) : null,
    });
    if (target) {
      setActiveTab(target);
      initialTabDecidedRef.current = true;
    }
  }, [scheme, loading, factsSummary, docList, tree, factsReady]);

  // 生成过程中定时刷新目录树（让用户看到各章节生成进度）
  // ✅ P0-7 轮询瘦身：生成期间改用轻量接口（不含 content 大字段），
  //    增量由 SSE 事件即时推送，轮询仅作状态兜底，避免每 3s 全量拉取 MB 级正文
  useEffect(() => {
    if (generating && genType === "content") {
      refreshTimerRef.current = window.setInterval(() => {
        refreshTreeLight();
      }, 3000);
    } else {
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    }
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [generating, genType, refreshTreeLight]);

  // ✅ 组件卸载时取消所有进行中的 SSE 连接 & 上传/解析请求
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
      uploadAbortRef.current?.abort();
      parseAbortRef.current?.abort();
      presetListAbortRef.current?.abort();
      exportCheckAbortRef.current?.abort();
      cacheAbortRef.current?.abort();
      exportAbortRef.current?.abort();
      presetListAbortRef.current = null;
      exportCheckAbortRef.current = null;
      cacheAbortRef.current = null;
      ++exportSeqRef.current;
      // ✅ BUG 修复：「提取项目」SSE 的 AbortController 此前**从未被 abort** ——
      //    卸载清单里独缺它。结构化提取可跑数十分钟，用户切走方案页后连接与
      //    后端 generator 继续存活；提取结束时的 finally 还会在已卸载组件上
      //    setBaRunning(false) + loadBaResults()（多发一次请求 + setState）。
      baSseAbortRef.current?.abort();
      // ✅ BUG 修复（2026-10-05）：「全局事实」两路列表请求的 AbortController
      //    此前只在**切换方案**（[id] effect）时中止，卸载清单里独缺它们 ——
      //    而 [id] effect 没有返回 cleanup，卸载时那段中止逻辑根本不会执行。
      //    与上方 baSseAbortRef 同类：请求继续跑完 + 在已卸载组件上 setState。
      factsListAbortRef.current?.abort();
      factsDocsListAbortRef.current?.abort();
      // ✅ 性能优化：卸载时清掉 SSE 批量器所有未刷任务 + cancel 已排的 rAF，
      //    防止"组件已卸载后 setState"警告与内存泄漏。
      sseBatcherRef.current?.stop();
      exportBatcherRef.current?.stop();
    };
  }, []);


  // ✅ SSE 断线兜底：轮询任务状态直到终态（断线重挂接）。
  // 逻辑已提取到模块级 pollTaskUntilTerminalImpl（可注入依赖、可单测），
  // 返回终态响应全量字段（含 checkpoint 回传的 outline_result，供目录部分成果恢复）。
  async function pollTaskUntilTerminal(taskId: string): Promise<TaskTerminalInfo | null> {
    return pollTaskUntilTerminalImpl(
      taskId,
      async (id) => (await tasksApi.status(id)).data as TaskTerminalInfo,
      (d) => {
        setProgress(d.progress || 0);
        setProgressMsg(`（后台继续）${d.message || ""}`);
      },
    );
  }

  // ✅ 页面加载/刷新后：检测后台遗留的进行中任务并重新挂接（轮询到终态后自动刷新）
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const { data } = await tasksApi.list(id);
        const allRunning = (data.tasks || []).filter(
          (t: TaskListItem) => t.status === "running" || t.status === "paused"
        );
        // ✅ 跳过 progress 已满的僵尸（后端没来得及标 completed），直接 load 刷新
        const stuckFull = allRunning.find((t: TaskListItem) => (t.progress || 0) >= 0.95);
        if (stuckFull) {
          console.warn("resume: 发现 progress≥95% 但 status 仍 running 的僵尸任务，直接 load 刷新");
          msg.success("检测到后台任务已完成，正在刷新");
          load();
          return;
        }
        const active = allRunning.find(Boolean);
        if (!active || cancelled) return;
        msg.info(
          `检测到后台「${TASK_TYPE_LABEL[active.task_type] || active.task_type}」任务仍在进行（${Math.round((active.progress || 0) * 100)}%），完成后自动刷新`,
          6
        );
        // ✅ F1 刷新接管：把后台仍在跑的正文/目录/事实任务接管回本页，恢复
        //    generating/genType/activeTaskIdRef——否则暂停/停止按钮不显示，
        //    用户无法对刷新后仍在跑的任务施加控制。
        const genTypeMap: Record<string, string> = {
          content_generation: "content",
          outline_generation: "outline",
          facts_generation: "facts",
        };
        const gt = genTypeMap[active.task_type];
        // 世代快照：接管期间若用户手动发起新生成（会 bump genSeqRef），
        // 本后台轮询循环到终态时不得越位复位新任务的状态。
        const takeoverSeq = genSeqRef.current;
        const takenOver = !!gt;
        if (takenOver) {
          setGenerating(true);
          setGenType(gt);
          activeTaskIdRef.current = active.id;
          setTaskPaused(active.status === "paused");
        }
        const resetTakeover = () => {
          if (!takenOver || genSeqRef.current !== takeoverSeq) return;
          setGenerating(false);
          setGenType("");
          setTaskPaused(false);
          if (activeTaskIdRef.current === active.id) activeTaskIdRef.current = "";
        };
        let stuckAtFull = 0;
        for (let i = 0; i < 400; i++) {
          await new Promise((r) => setTimeout(r, 3000));
          if (cancelled) return;
          try {
            const { data: st } = await tasksApi.status(active.id);
            // ✅ F3：接管期间同步暂停态（后台可能被其他入口暂停/恢复）
            if (takenOver) setTaskPaused(st.status === "paused");
            // ✅ R89 S4：接管轮询期间同步进度。旧实现只同步暂停态，用户刷新页面
            // 后接管一个 30 章长任务（最长十几分钟）期间进度条一直停在接管那一刻
            // 的值，看起来像卡死（stuckAtFull 兜底只在 >=0.99 时生效，中段无信号）。
            if (takenOver && typeof st.progress === "number") setProgress(st.progress);
            // ✅ 2026-10-10：终态判定改用单一事实源 —— 旧白名单漏了 degraded，
            //    降级完成的后台任务在这里会被一直判为「仍在跑」。
            if (isTerminalTaskStatus(st.status)) {
              resetTakeover();
              const ckpt = st.outline_result;
              if (active.task_type === "outline_generation"
                && ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
                // ✅ 刷新页面后恢复后台跑完的目录成果（与在线 completed 走同一闸门）
                setTree(buildTree(ckpt.outline));
                if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                  setFailedOutlineChapters(ckpt.failed_chapters);
                  msg.warning(`后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`);
                } else {
                  msg.success("后台目录生成已完成，请确认保存");
                }
                // ✅ R74（G1）：接管恢复路径与在线 completed 同口径呈现审核依据
                //    （checkpoint 白名单含 review，此前无处安放）。
                summarizeOutlineReviewBasis(ckpt.review).logs.forEach((line) => pushOutlineLog(1, line));
                openOutlineGate(ckpt.outline);
              } else if (st.status === "completed") {
                msg.success("后台任务已完成，正在刷新");
                load();
              } else if (st.status === "degraded") {
                // ✅ 2026-10-10：降级完成同样要刷新（正文已落库），但必须告警
                // ✅ R89 S2：目录检查点写失败也会降级 —— 成果只经 SSE 送出一次，
                //    刷新后 checkpoint_json 为空、无法恢复，因此文案同时覆盖两种成因。
                msg.warning("后台任务降级完成：成稿可能含未修复缺陷，或成果未能写入检查点（刷新后可能无法恢复，建议立即保存）");
                load();
              } else if (st.status === "failed") {
                msg.error(`后台任务失败：${st.message || ""}`);
              } else if (st.status === "stopped") {
                // ✅ R89 S4：接管路径此前对 stopped 无分支 —— 用户主动停止后台
                // 目录生成后刷新页面，这里静默 return，进度条与状态停在接管前。
                msg.info(`后台任务已停止${st.message ? `：${st.message}` : ""}`);
              }
              return;
            }
            // ✅ 进度已满但 status 没更新：3 次连续就兜底
            if ((st.progress || 0) >= 0.99) {
              stuckAtFull++;
              if (stuckAtFull >= 3) {
                console.warn(`resume: task ${active.id} stuck at 100% for ${stuckAtFull} polls, treating as completed`);
                resetTakeover();
                msg.success("后台任务已完成，正在刷新");
                load();
                return;
              }
            } else {
              stuckAtFull = 0;
            }
          } catch {
            return;
          }
        }
      } catch {
        /* 接口异常静默 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, load]);

  // ✅ 切换章节时：有未保存更改则弹出确认，无更改则直接重置
  useEffect(() => {
    const prevKey = prevSectionKeyRef.current;
    prevSectionKeyRef.current = selectedSection?.key || null;
    if (prevKey === selectedSection?.key) return;
    if (isEditingSection && editDirty) {
      modal.confirm({
        title: "有未保存的更改",
        content: "当前章节的编辑尚未保存，切换后将丢失更改。确定要放弃吗？",
        okText: "放弃更改",
        okType: "danger",
        cancelText: "继续编辑",
        onOk: () => {
          setIsEditingSection(false);
          setEditContent("");
          setEditDirty(false);
        },
        onCancel: () => {
          // 回退到之前的章节
          if (prevKey) {
            const prev = findSectionById(tree, prevKey);
            if (prev) setSelectedSection(prev);
          }
        },
      });
    } else {
      setIsEditingSection(false);
      setEditContent("");
      setEditDirty(false);
    }
  }, [selectedSection?.key]);

  // ✅ 关闭页面前检查未保存更改
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (isEditingSection && editDirty) {
        e.preventDefault();
        e.returnValue = "有未保存的更改，确定要离开吗？";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isEditingSection, editDirty]);

  // ✅ 草稿持久化已下沉到 SectionContentCard（组件自持 draft + localStorage 自动保存）；
  //    页面旧版草稿自动保存 effect 会用陈旧 editContent 覆盖卡片草稿，已移除（勿恢复）。

  function buildTree(nodes: OutlineNodeInput[]): TreeNode[] {
    return nodes.map((n) => {
      // ✅ 从 outline_json 里提取后端权威编号（点分路径，如 "1"/"1.1"/"1.1.1"）
      // 这是目录树编号正确显示的关键 —— UUID 无点号无法解析
      let outlineId: string | undefined;
      try {
        const oj = typeof n.outline_json === "string"
          ? JSON.parse(n.outline_json)
          : n.outline_json;
        if (oj?.id && /^\d+(\.\d+)*$/.test(String(oj.id))) {
          outlineId = String(oj.id);
        }
      } catch { /* ignore */ }
      return {
        key: n.id as string,
        title: n.title!,
        level: n.level!,
        status: n.status!,
        word_count: n.word_count || 0,
        word_budget: n.word_budget || 0,
        content: n.content,
        description: n.description || "",
        outlineId,
        // ✅ 2026-09-29：事实变更失效标记（后端 _build_tree 读侧派生）
        facts_stale: !!n.facts_stale,
        children: n.children ? buildTree(n.children) : [],
      };
    });
  }

  // ✅ 性能优化：onSseEvent 被每条 SSE 事件调用一次，若每次渲染重建会破坏上层
  //    对它的引用稳定性（未来 memo 化子组件依赖此引用时也会失效）。
  //    此函数只依赖 ref 与 msg，可安全 useCallback([])
  const onSseEvent = useCallback((evt: SseBaseEvent): boolean => {
    if (evt.task_id) activeTaskIdRef.current = evt.task_id;
    // ✅ R72（2026-10-12）解析提取链 warning 事件统一消费点。
    //    目录生成链（§generate_outline）与正文生成链（§generate_content）共用本函数，
    //    旧实现只认 error：后端为「文档解析被截断、本次生成仅基于已解析部分」下发的
    //    `event:'warning'` 在此被静默丢弃 → 用户看不见依据不完整（事实链早已本地 pushLog
    //    消费，目录/正文两链却没有）。与 error 分支对称：warning 不中断流、只 toast 告知，
    //    故 return false。单点接线覆盖两链，与事实链既有「日志行 + toast」双通道形态一致。
    if (evt.event === "warning") {
      msg.warning(evt.message || "存在需人工核对的提示");
      return false;
    }
    if (evt.event === "error") {
      msg.error(evt.message || "生成失败");
      // ✅ 修复（2026-10-05 · 正文生成 R-4）：error 事件**也带失败明细**，
      //    旧实现只弹一句 message 就 abort，`failed_sections` 被整个丢弃。
      //    而 error 恰恰是最需要明细的终态 —— 整批异常可能发生在任何章节
      //    进入生成循环**之前**（例如章节树查询失败），此时一条
      //    `section_error` 都没下发过，用户只看到病因文案、
      //    逐章失败原因彻底丢失，章节日志区拿不到「哪几章失败」。
      //    与 completed / stopped 分支走同一合并入口（mergeFailedSectionsInto
      //    是纯函数 + ref 写入，useCallback([]) 捕获首帧闭包安全）。
      if (Array.isArray(evt.failed_sections) && evt.failed_sections.length > 0) {
        mergeFailedSectionsIntoRef(evt.failed_sections);
      }
      // ✅ 修复：error 后中断 SSE 连接（旧实现仅 break 未 abort，后端任务继续运行，
      // 用户此时再次点生成会造成新旧两个任务并发写同一方案）
      abortControllerRef.current?.abort();
      return true;
    }
    return false;
  }, []);

  /** SSE 统一推送进度日志（供 onSseEvent、doGenerateContent 等多处复用） */
  const pushOutlineLog = useCallback((p: number, m: string, done?: number, total?: number) => {
    if (!m) return;
    const list = outlineLogsRef.current;
    if (list.length && list[list.length - 1].message === m) return;
    // ✅ 修复：短方案一次性直出链路的 progress 事件内嵌统计快照（done=0/total=0，
    //    无章节维度），原样写入会渲染出无意义的「0/0 章」。仅长方案分步链路
    //    （total>0）才记录章节计数。
    const hasChapterDim = typeof total === "number" && total > 0;
    outlineLogsRef.current = [...list, {
      progress: p, message: m,
      done: hasChapterDim ? done : undefined,
      total: hasChapterDim ? total : undefined,
      time: Date.now(),
    }];
    setOutlineLogs(outlineLogsRef.current);
  }, []);

  /** 保存编制要求到 schemes.config_json.requirements */
  async function saveRequirements() {
    if (!id || !scheme?.project_id) {
      msg.error("缺少项目信息，无法保存");
      return;
    }
    try {
      const { data: s } = await schemesApi.get(scheme.project_id, id);
      let cfg: Record<string, unknown> = {};
      try {
        cfg = JSON.parse(s.config_json || "{}");
      } catch {
        cfg = {};
      }
      cfg.requirements = requirements.trim();
      await schemesApi.update(scheme.project_id, id, {
        config_json: JSON.stringify(cfg),
      });
      msg.success("编制要求已保存：生成目录时将逐条注入并强制覆盖，审核阶段自动补齐缺失章节");
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "保存编制要求失败");
    }
  }

  /** 整表重建可能清除未匹配章节的已生成正文（后端 2026-09-26 起量化回传）。
   * 此前「重新生成目录 → 确认闸门保存」是无提示地清空正文，用户不可恢复。 */
  function notifyClearedContent(data: { cleared_content_sections?: number } | null | undefined, prefix = "") {
    const n = Number(data?.cleared_content_sections || 0);
    if (n > 0) {
      msg.warning(
        `${prefix}${n} 个章节的已生成正文因未匹配到新目录而被清除（不可恢复）` +
        `——如需保留，请在生成目录前先备份或用「AI 调整目录」定向修改`);
    }
  }

  /** 目录写入检出占位/泛化标题（后端 R85/R86 加法式回传 `generic_residual` 明细）。
   *
   * 背景：用户在 TOC 里看到的「专项6」「总体实施策划（2）」有两批来源 ——
   * 一批来自 AI 生成（有占位闸门 + 修复链路），另一批来自**写入路径**
   * （手工新增/改名、上传目录识别、目录库套用）。后端已在 create/update/
   * save-outline/upload 四条入口统一加法式回传明细（绝不阻断、绝不静默改写），
   * 但前端此前 `generic_residual` **零消费** ⇒ 用户完全无感知。
   *
   * 文案由纯函数 `genericResidualNotice` 产出（utils 层，vitest 可直接覆盖）。
   * 契约：仅当确有残留才回传该键（数组，元素 `{path,title,kind}`），
   * 旧后端 / 干净目录都不带此键 ⇒ 老版本一字不变、无提示。
   *
   * ✅ 本轮补：后端落库前可能已**自动改写**部分占位标题（外科式重写，只改标题
   * 不增删节点），并以加法式键 `generic_autofixed`（整数，仅 >0 才回传）告知
   * 改写条数。此前前端零消费 ⇒ 自动改写**静默生效**，用户以为目录原样保存。
   * 现一并提示（同一函数出口，7 个调用点零改动即生效）。
   */
  function notifyGenericResidual(data: unknown) {
    const text = genericResidualNotice(data);
    if (text) msg.warning(text);
    const fixed = genericAutofixNotice(data);
    if (fixed) msg.info(fixed);
  }

  /** 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
   * 生成完成后不自动入库，由用户勾选确认一级章节后保存并锁定（locked=1） */
  function openOutlineGate(outline: OutlineNodeInput[]) {
    if (!id) return;
    const roots = (outline || []).filter((n) => n && typeof n === "object");
    if (!roots.length) {
      msg.warning("AI 返回空目录，未保存");
      return;
    }
    const selectedRef: { current: number[] } = { current: roots.map((_, i) => i) };
    modal.confirm({
      title: "🔍 一级目录确认（生成后入库前的最后一道闸门）",
      width: 660,
      content: <OutlineGateList outline={roots} selectedRef={selectedRef} />,
      okText: "确认并保存目录",
      cancelText: "暂不入库（手动编辑）",
      onOk: async () => {
        const filtered = roots.filter((_, i) => selectedRef.current.includes(i));
        if (!filtered.length) {
          msg.error("至少保留一个一级章节");
          return Promise.reject(new Error("empty"));
        }
        // ✅ 已有正文时二次确认：AI 新目录与旧章节无法按主键匹配，整表重建会清空正文
        if (hasAnyContent(treeRef.current) && !hasContentRef.current) {
          modal.confirm({
            title: "确认覆盖已有目录？",
            okButtonProps: { danger: true },
            content:
              "当前目录已有章节生成过正文。AI 新目录与旧章节无法按主键匹配，" +
              "确认保存将按新目录重建章节结构，**未匹配上的章节正文会被清除且不可恢复**。" +
              "如需保留原文，请点「取消」并改用「AI 调整目录」定向修改。",
            okText: "仍要保存（可能清除正文）",
            cancelText: "取消",
            onOk: () => {
              // 仅在用户**确认后**置位：取消不应"消耗"掉这次风险提示，
              // 否则用户取消一次后再点保存就静默清空正文（比原缺陷更隐蔽）。
              hasContentRef.current = true;
              return doSaveGate(filtered);
            },
          });
          return;
        }
        await doSaveGate(filtered);
      },
      onCancel: () => {
        // 目录已显示在目录树预览中，未入库 —— 用户可手动编辑后点「保存目录」
        msg.info("目录未入库：可在目录树中手动调整后点击「保存目录」");
      },
    });
  }

  async function doSaveGate(filtered: OutlineNodeInput[]) {
    if (!id) return;
    try {
      const { data } = await sectionsApi.saveOutline(id, {
        outline: filtered,
        source: "ai",
        lock_roots: true,
      });
      msg.success(`目录已确认保存（${filtered.length} 个一级章节已锁定）`);
      notifyClearedContent(data);
      notifyGenericResidual(data);
    } catch (saveErr: unknown) {
      const err = saveErr as { message?: string };
      msg.error(
        `目录已生成但保存失败：${err?.message || "未知错误"}，请点击「保存目录」重试`);
    }
    load();
  }

  const doGenerateOutline = async () => {
    if (!id) return;
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = "";  // R106 FIX-8 · 目录链
    // 每代生成重新武装「正文覆盖风险」二次确认
    hasContentRef.current = false;
    // 创建新的 AbortController，取消上一次未完成的 SSE
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：每次目录生成任务创建独立的 rAF 批处理器。
    sseBatcherRef.current?.stop();
    sseBatcherRef.current?.flushNow();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    setGenType("outline");
    setProgress(0);
    setProgressMsg("正在连接...");
    setActiveTab("outline");
    // 清空本次目录生成日志
    setOutlineLogs([]);
    outlineLogsRef.current = [];
    // ✅ 进度增强：同时清空运行统计，避免上一次正文生成的统计（字数/并发）
    //    或上一次目录任务的阶段标签残留在本次进度卡上
    setGenStats({});
    let lastP = 0;
    let taskId = "";
    try {
      for await (const raw of sseFetch(`/sse/generate-outline/${id}`, undefined, { signal: ac.signal })) {
        // 后端 SSE 契约：目录生成链事件，一次性断言（字段见 OutlineGenEvent）
        const evt = raw as OutlineGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (evt.event === "error") {
          onSseEvent(evt);
          // ✅ 遗留修复：全部章节子目录失败时，后端在 error 事件中携带已生成的
          //    一级章标题（outline + partial）。询问用户是否保存这部分成果，
          //    与 stopped 分支同策略（save-outline 智能保留已有正文）。
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const partialCount = countOutline(evt.outline);
            const hasContent = hasAnyContent(tree);
            modal.confirm({
              title: "目录生成失败，但有部分成果",
              okButtonProps: hasContent ? { danger: true } : undefined,
              content: hasContent ? (
                <div>
                  已生成 {partialCount} 个章节节点尚未保存。
                  <b style={{ color: "#cf1322" }}>
                    保存会按新目录重建章节结构，当前已有正文可能因章节无法匹配而被删除
                  </b>
                  ，且不可恢复。确定保存吗？
                </div>
              ) : (
                `已生成 ${partialCount} 个章节节点尚未保存到数据库。是否保存这部分已生成的目录？`
              ),
              okText: hasContent ? "仍要保存（可能删除正文）" : "保存已生成部分",
              cancelText: "不保存",
              onOk: async () => {
                try {
                  const { data } = await sectionsApi.saveOutline(id, { outline: evt.outline!, source: "ai" });
                  msg.success(`已保存 ${partialCount} 个章节节点`);
                  notifyClearedContent(data);
                  notifyGenericResidual(data);
                  load();
                } catch (saveErr: unknown) {
                  const err = saveErr as { message?: string };
                  msg.error(err?.message || "保存失败");
                }
              },
            });
          }
          break;
        }
        if (onSseEvent(evt)) break;
        if (evt.event === "completed") {
          // ✅ 性能优化：终态事件立即 flush，确保 UI 与后续操作一致
          batcher.flushNow();
          setProgress(1);
          setProgressMsg("目录生成完成");
          pushOutlineLog(1, "✅ 目录生成完成");
          // 长方案分步生成：记录失败章节，前端高亮提示
          if (evt.failed_chapters && Array.isArray(evt.failed_chapters) && evt.failed_chapters.length > 0) {
            setFailedOutlineChapters(evt.failed_chapters);
            const fcList = evt.failed_chapters.slice(0, 5).join("、");
            const more = evt.failed_chapters.length > 5 ? `等${evt.failed_chapters.length}章` : "";
            msg.warning(`目录生成完成，但 ${evt.failed_count || evt.failed_chapters.length} 章子目录生成失败（${fcList}${more}），已在目录树中高亮标注，建议手动补充或重新生成`);
            pushOutlineLog(1, `⚠️ ${evt.failed_count || evt.failed_chapters.length} 章子目录生成失败：${fcList}${more}`);
          } else {
            setFailedOutlineChapters([]);
          }
          if (evt.outline && evt.outline.length > 0) {
            // ✅ 立即用已生成的 3 级目录刷新目录树（不依赖 load 时序，避免「目录生成至三级但树未更新」）
            setTree(buildTree(evt.outline));
            // ✅ P1-5：把后端「生成后自动校验」报告（全面性缺口 / 连续性问题 /
            //    冗余候选）呈现在生成日志与提示条里 —— 此前 review.quality
            //    没有任何消费方，用户看不到"方案名称里的工序没落到章节"。
            //    只提示、绝不自动删章（删章不可逆）。
            const _q = summarizeOutlineQuality(evt.review);
            if (_q.level !== "none") {
              _q.logs.forEach((line) => pushOutlineLog(1, line));
              if (_q.level === "warn") msg.warning(_q.message);
            }
            // ✅ R74（G1）：审核依据与已执行动作同样必须呈现 —— 后端每次
            //    程序化出口都带了 review_mode/suggestions，此前前端静默丢弃。
            summarizeOutlineReviewBasis(evt.review).logs.forEach((line) => pushOutlineLog(1, line));
            // 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
            //    旧实现 completed 即自动入库，用户没有确认一级结构的机会；
            //    现改为弹闸门勾选确认后再保存（lock_roots=true 锁定一级章节）。
            openOutlineGate(evt.outline);
          } else {
            msg.warning("AI 返回空目录，未保存");
          }
          break;
        } else if (evt.event === "stopped") {
          // ✅ 性能优化：终态事件立即 flush
          batcher.flushNow();
          setProgressMsg("已停止");
          pushOutlineLog(lastP, "⏹ 已停止");
          // ✅ BUG 修复：长方案分步生成被停止时，已生成的部分目录此前只更新到
          // 前端内存 tree，刷新页面即丢失。现询问用户是否保存这部分成果
          //（后端 save-outline 智能保留已有正文，按需重建结构）。
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const partialCount = countOutline(evt.outline);
            const hasContent = hasAnyContent(tree);
            modal.confirm({
              title: "任务已停止",
              okButtonProps: hasContent ? { danger: true } : undefined,
              content: hasContent ? (
                <div>
                  已生成 {partialCount} 个章节的目录尚未保存。
                  <b style={{ color: "#cf1322" }}>
                    保存会按新目录重建章节结构，当前已有正文可能因章节无法匹配而被删除
                  </b>
                  ，且不可恢复。确定保存吗？
                </div>
              ) : (
                `已生成 ${partialCount} 个章节的目录尚未保存到数据库。是否保存这部分已生成的目录？`
              ),
              okText: hasContent ? "仍要保存（可能删除正文）" : "保存已生成部分",
              cancelText: "不保存",
              onOk: async () => {
                try {
                  const { data } = await sectionsApi.saveOutline(id, { outline: evt.outline!, source: "ai" });
                  msg.success(`已保存 ${partialCount} 个章节的目录`);
                  notifyClearedContent(data);
                  notifyGenericResidual(data);
                  load();
                } catch (saveErr: unknown) {
                  const err = saveErr as { message?: string };
                  msg.error(err?.message || "保存失败");
                }
              },
            });
          } else {
            msg.info("目录生成已停止");
          }
          break;
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ 进度增强：长 AI 调用（单次最长 180s）与审核+修复期间，
          //    后端由 _await_with_stats / 心跳通道持续推送运行统计。
          //    这里只更新统计与进度条，不写日志、不清空当前提示文案
          //    —— stats 事件本身不带 message，落到下面的通用分支会把
          //    progressMsg 覆盖成空串。
          // ✅ 性能优化：rAF 批处理 + 相同值去重，避免高频事件引发多余渲染。
          const statsSnapshot = pickStats(evt);
          batcher.schedule("genStats", () =>
            setGenStats((prev) => statsEqual(prev, statsSnapshot) ? prev : statsSnapshot),
          );
          if (typeof evt.progress === "number" && evt.progress > lastP) {
            lastP = evt.progress;
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
            // ✅ 实时增强：日志列表最新一行的百分比同步跟随主进度渐近填充，
            //    不再冻结在阶段进入值（如「正在生成目录... 10%」静止数分钟）
            const logs = outlineLogsRef.current;
            if (logs.length && logs[logs.length - 1].progress < lastP) {
              const next = [...logs];
              next[next.length - 1] = { ...next[next.length - 1], progress: lastP };
              outlineLogsRef.current = next;
              batcher.schedule("outlineLogs", () => setOutlineLogs(next));
            }
          }
        } else {
          lastP = evt.progress || 0;
          const p = lastP;
          batcher.schedule("progress", () => setProgress(p));
          batcher.schedule("progressMsg", () => setProgressMsg(evt.message || ""));
          pushOutlineLog(lastP, evt.message || "", evt.done, evt.total);
          // progress 事件内嵌运行统计（阶段/已耗时/ETA/章节数）
          if (evt.phase || typeof evt.elapsed_ms === "number") {
            const statsSnapshot = pickStats(evt);
            batcher.schedule("genStats", () =>
              setGenStats((prev) => statsEqual(prev, statsSnapshot) ? prev : statsSnapshot),
            );
          }
          // ✅ 长方案分步生成：progress 事件携带已生成 outline 部分，实时更新目录树
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const newTree = buildTree(evt.outline);
            batcher.schedule("tree", () => setTree(newTree));
          }
        }
      }
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err.name !== "AbortError") {
        msg.warning(err.message || "SSE 连接中断");
        // ✅ BUG 修复（2026-09-23 · 「30s 连接超时后任务孤儿运行」）：
        //    建连阶段（首个事件到达前）超时/失败时 taskId 尚未产生，旧实现
        //    直接跳过重挂 —— 但后端任务可能已注册并继续生成（AI 照常计费），
        //    成果只躺在 checkpoint。此处按方案检索最近仍在运行的
        //    outline_generation 任务重挂（检索不到才维持旧行为：仅提示）。
        // ✅ 收口（2026-10-14）：检索 → 重挂 → 轮询三段收敛到 reattachTaskImpl
        //    （与正文/事实同源；目录链默认 typeFilter=outline_generation）。
        //    终态后的目录成果恢复（outline_result / failed_chapters / review）
        //    各链语义不同，仍由本块处理。
        const hit = await reattachTaskImpl(
          async () => (await tasksApi.list(id)).data,
          pollTaskUntilTerminal,
          {
            taskId,
            onReattach: () =>
              setProgressMsg("连接中断，正在重新挂接后台任务..."),
          },
        );
        if (hit) {
          const fin = hit.final;
          if (fin?.status === "completed") {
            // ✅ 成果恢复：后台完成的目录只存在于任务 checkpoint 中，
            // 仅 load() 刷不到任何东西（目录需用户闸门确认才入库）。
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），经 Array.isArray 判别后收窄
              const restoredOutline = ckpt.outline as OutlineNodeInput[];
              setTree(buildTree(restoredOutline));
              if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                setFailedOutlineChapters(ckpt.failed_chapters);
                msg.warning(`后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`);
              } else {
                msg.success("目录生成已在后台完成，请确认保存");
              }
              // ✅ R74（G1）：断线重挂路径同样呈现审核依据（与 completed 同口径）
              summarizeOutlineReviewBasis(ckpt.review).logs.forEach((line) => pushOutlineLog(1, line));
              openOutlineGate(restoredOutline);
            } else {
              msg.success("目录生成已在后台完成，正在刷新");
              load();
            }
          } else if (fin?.status === "failed") {
            msg.error(fin.message || "后台任务失败");
            // ✅ 遗留修复：全部章节子目录失败时后端也写了 checkpoint（仅一级章标题），
            //    断线重连后同样提供部分成果保存入口，与在线 error 分支对齐。
            const failCkpt = fin.outline_result;
            if (failCkpt?.outline && Array.isArray(failCkpt.outline) && failCkpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），收窄一次供闭包复用
              const partialOutline = failCkpt.outline as OutlineNodeInput[];
              setTree(buildTree(partialOutline));
              const partialCount = countOutline(partialOutline);
              // ✅ 文案收口到 buildPartialOutlineHint（旧实现两处各写一份，且不说中断原因）
              const hint = buildPartialOutlineHint({
                event: failCkpt.event,
                partial: failCkpt.partial,
                nodeCount: partialCount,
              });
              modal.confirm({
                title: hint.title,
                content: hint.content,
                okText: hint.okText,
                cancelText: hint.cancelText,
                okButtonProps: hint.tone === "warning" ? { danger: true } : undefined,
                onOk: async () => {
                  try {
                    const { data } = await sectionsApi.saveOutline(id, { outline: partialOutline, source: "ai" });
                    msg.success(`已保存 ${partialCount} 个章节节点`);
                    notifyClearedContent(data);
                    notifyGenericResidual(data);
                    load();
                  } catch (saveErr: unknown) {
                    const err = saveErr as { message?: string };
                    msg.error(err?.message || "保存失败");
                  }
                },
              });
            }
          } else if (fin?.status === "stopped") {
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），收窄一次供闭包复用
              const stoppedOutline = ckpt.outline as OutlineNodeInput[];
              setTree(buildTree(stoppedOutline));
              const partialCount = countOutline(stoppedOutline);
              const hint = buildPartialOutlineHint({
                event: ckpt.event,
                partial: ckpt.partial,
                nodeCount: partialCount,
              });
              modal.confirm({
                title: hint.title,
                content: hint.content,
                okText: hint.okText,
                cancelText: hint.cancelText,
                onOk: async () => {
                  try {
                    const { data } = await sectionsApi.saveOutline(id, { outline: stoppedOutline, source: "ai" });
                    msg.success(`已保存 ${partialCount} 个章节的目录`);
                    notifyClearedContent(data);
                    notifyGenericResidual(data);
                    load();
                  } catch (saveErr: unknown) {
                    const err = saveErr as { message?: string };
                    msg.error(err?.message || "保存失败");
                  }
                },
              });
            } else {
              // ✅ BUG 修复（2026-09-25）：ckpt 可能为 undefined（stopped 且无 checkpoint
              //    成果），旧实现直接 ckpt.event 抛 TypeError 使收尾逻辑中断，用户既看不到
              //    「已停止」提示也没有弹窗。同函数其它分支均用可选链，唯独此处漏了。
              msg.info(ckpt?.event === "error" ? "后台任务因异常中断，且无可恢复的部分成果" : "后台任务已停止");
            }
          }
        }
      }
    } finally {
      if (abortControllerRef.current === ac) {
        abortControllerRef.current = null;
      }
    }
    // ✅ 世代校验：被重入（abort）的旧协程不得复位新任务的状态
    if (genSeqRef.current === mySeq) {
      setGenerating(false);
      setGenType("");
    }
    // ✅ 性能优化：任务结束或异常退出后，排空残余批处理并停止
    batcher.flushNow();
    batcher.stop();
    if (sseBatcherRef.current === batcher) sseBatcherRef.current = null;
  };

  /**
   * ✅ 增强：重新生成目录前的覆盖风险确认。
   * 目录生成会整表重建 sections（AI 返回的编号无法与已有章节主键一一对应），
   * 已有正文不会保留；旧实现直接执行，用户容易误点导致正文丢失。
   */
  const handleGenerateOutline = async () => {
    if (!id) return;
    if (hasAnyContent(tree)) {
      modal.confirm({
        title: "⚠️ 确认重新生成目录？",
        content: (
          <div>
            当前方案已有生成好的正文。<b>重新生成目录会重建章节结构，
            已生成的正文内容将无法保留</b>，且不可恢复。
            <br />
            如需保留正文，请先取消，改用「目录」页的手动编辑或「套用目录库」。
          </div>
        ),
        okText: "仍要重新生成",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: () => doGenerateOutline(),
      });
      return;
    }
    await doGenerateOutline();
  };

  /**
   * ✅ R66（2026-10-11）四档字数控制：档位数值表（与后端真源
   * content_utils.WORD_BUDGET_TIERS 一致，前端仅用于预估展示）。
   */
  const WORD_BUDGET_TIER_VALUES: Record<string, number> = { brief: 800, standard: 1200, detailed: 2000, ultra: 2500 };

  /** 选中档位（brief/standard/detailed/ultra）→ word_budget_tier；其余返回 undefined */
  function resolveWordBudgetTier(): string | undefined {
    return ["brief", "standard", "detailed", "ultra"].includes(wordBudgetOption)
      ? wordBudgetOption
      : undefined;
  }

  /** 计算实际传递给后端的字数预算覆盖值（R66 起仅「自定义」走二级总量分配口径；未选则不覆盖） */
  function resolveWordBudgetOverride(): number | undefined {
    if (wordBudgetOption === "custom") return customWordBudget;
    return undefined;
  }

  /** 收集目标章节的叶子节点信息（用于预估弹窗） */
  function collectLeavesInfo(scope: "all" | "missing" | "section", sectionId?: string, forceRewrite = false): {
    leaves: TreeNode[];
    totalBudget: number;
    willOverwrite: number;
    missingCount: number;
  } {
    // 先截取子树
    let nodes = tree;
    if (scope === "section" && sectionId) {
      const sec = findSectionById(tree, sectionId);
      if (!sec) return { leaves: [], totalBudget: 0, willOverwrite: 0, missingCount: 0 };
      nodes = [sec];
    }
    // DFS 取所有叶子
    const leaves: TreeNode[] = [];
    const dfs = (arr: TreeNode[]) => {
      arr.forEach((n) => {
        if (!n.children || n.children.length === 0) leaves.push(n);
        else dfs(n.children);
      });
    };
    dfs(nodes);

    const wbOverride = resolveWordBudgetOverride();
    // ✅ R66：档位模式下每个叶子按档位字数预估（与后端 tier 分支同口径）
    const wbTierValue = resolveWordBudgetTier();
    const wbTier = wbTierValue ? WORD_BUDGET_TIER_VALUES[wbTierValue] : 0;
    let totalBudget = 0;
    let willOverwrite = 0;
    let missingCount = 0;

    let filtered = leaves;
    // ✅ 与后端 select_target_leaves 对齐：统一以"是否已有非空正文"为判据。
    //    旧实现只看 status==='generated'，导致 reviewed/expanded 等已有正文的
    //    章节被前端算作"将生成"，预估数量/字数与实际生成范围不符。
    const hasContent = (l: TreeNode) => !!(l.content || "").trim() && l.word_count > 0;
    if (scope === "missing") {
      filtered = leaves.filter((l) => ["empty", "failed", "pending"].includes(l.status) && !hasContent(l));
    } else if (!forceRewrite) {
      filtered = leaves.filter((l) => !hasContent(l));
    }
    // forceRewrite=true 时不过滤

    filtered.forEach((l) => {
      const budget = wbTier || wbOverride || l.word_budget || 1200;
      totalBudget += budget;
      if (hasContent(l)) willOverwrite++;
      missingCount++;
    });
    return { leaves: filtered, totalBudget, willOverwrite, missingCount };
  }

  /** 保存章节手动编辑内容（content 由 SectionContentCard 的自持草稿上抛） */
  async function saveSectionEdit(content: string) {
    if (!selectedSection || !id) return;
    setSavingSection(true);
    try {
      const { data } = await sectionsApi.update(id, selectedSection.key!, {
        content: content,
        status: content.trim() ? "generated" : "empty",
      });
      msg.success(`已保存，当前 ${data.word_count || 0} 字`);
      setIsEditingSection(false);
      setEditDirty(false);
      // ✅ G10（2026-09-21）：正文已变 → 审核结论随之失效。
      // 旧实现 reviewTick 只在切 Tab 时递增：用户在正文生成 Tab 改完正文，
      // 不切走再回来，审核工作台的「已通过」徽标与就绪度总分都还是旧的。
      setReviewTick((t) => t + 1);
      if (data.review_reset) {
        // ✅ G9：后端把该章节从 approved/reviewing/rejected 退回了「待审核」
        msg.warning("正文已变更，该章节审核结论已失效，需重新送审");
      }
      // 保存成功后清除草稿
      if (draftKey) {
        try { localStorage.removeItem(draftKey); } catch { /* ignore */ }
      }
      // ✅ 优化：局部更新当前章节而非全量 load()，避免重置目录展开/选中状态
      const updatedSection = {
        ...selectedSection,
        content: content,
        word_count: data.word_count ?? 0,
        status: content.trim() ? "generated" : "empty",
      };
      setSelectedSection(updatedSection);
      // 同步更新 tree 中对应节点
      const updateTreeNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === selectedSection.key
            ? { ...n, content: content, word_count: data.word_count ?? 0,
                status: content.trim() ? "generated" : "empty" }
            : { ...n, children: n.children ? updateTreeNode(n.children) : undefined }
        );
      setTree((prev) => updateTreeNode(prev));
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "保存失败");
    } finally {
      setSavingSection(false);
    }
  }

  /**
   * ✅ F-CONTENT-STANDARD(2026-09-26 · F3）：章节级生成标准变更 → PATCH 落库。
   *
   * 语义：传 `""` = 清除章节覆盖、回落方案默认（后端 `SectionUpdate` 接受空串，
   * 路由内归一为「沿用」）。失败时**不更新本地 state** —— Select 是受控的，
   * 保持旧值即视觉回滚，无需额外 revert（避免出现"界面显示已改、库里没改"）。
   *
   * 该字段不触发审核重置（后端未把 generation_standard 计入正文变更判定），
   * 因此**不**递增 reviewTick。
   */
  async function handleSectionStandardChange(v: string) {
    if (!selectedSection || !id) return;
    if (generating) {
      msg.warning("生成进行中，请等待本轮结束后再修改生成标准");
      return;
    }
    const prev = selectedSection.generation_standard || "";
    if (prev === v) return;
    const sectionKey = selectedSection.key!;
    try {
      await sectionsApi.update(id, sectionKey, { generation_standard: v });
      const patchNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === sectionKey
            ? { ...n, generation_standard: v }
            : { ...n, children: n.children ? patchNode(n.children) : undefined }
        );
      setTree((prevTree) => patchNode(prevTree));
      setSelectedSection((s) => (s && s.key === sectionKey ? { ...s, generation_standard: v } : s));
      msg.success(v ? `本章生成标准已设为「${STANDARD_LABELS[v as "precise" | "fuzzy"]}」` : "本章已改为沿用方案默认");
    } catch (e: unknown) {
      // 受控 Select 未改 state → 自动回滚到旧值
      const err = e as { message?: string };
      msg.error(err.message || "生成标准保存失败");
    }
  }

  /**
   * ✅ F-CONTENT-STANDARD(2026-09-26 · F5）：把当前任务级选择存为**方案默认**。
   * 仅 precise/fuzzy 可存（「按章节设置」不是一个方案级取值）。
   */
  async function handleSaveSchemeDefaultStandard() {
    if (!id || !scheme) return;
    const std = generationStandard === "inherit" ? "" : generationStandard;
    if (!std) {
      msg.info("「按章节设置」不能存为方案默认，请先选择精准内容或模糊内容");
      return;
    }
    setSavingStandardDefault(true);
    try {
      const { data } = await schemesApi.update(scheme.project_id!, id, {
        generation_standard: std,
      });
      setScheme((s) => (s ? { ...s, generation_standard: data?.generation_standard || std } : s));
      msg.success(`已存为方案默认：${STANDARD_LABELS[std as "precise" | "fuzzy"]}`);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "保存方案默认失败");
    } finally {
      setSavingStandardDefault(false);
    }
  }

  /** 重写当前章节（带二次确认） */
  function regenerateCurrentSection() {
    if (!selectedSection) return;
    const hasContent = selectedSection.word_count! > 0 || selectedSection.content;
    modal.confirm({
      title: "⚠️ 确认重写本章？",
      content: (
        <div>
          将重新生成章节 <b>「{selectedSection.title}」</b> 的全部叶子子章节，
          {hasContent ? "已有的正文内容将被<b style={{ color: '#ff4d4f' }}>覆盖</b>。" : "章节目前为空。"}
        </div>
      ),
      okText: "确认重写",
      cancelText: "取消",
      onOk: () => {
        // ✅ BUG 修复：这里必须显式 force_rewrite。
        //    后端 select_target_leaves 对"已有非空正文"的章节默认跳过（智能跳过），
        //    故不带 force_rewrite 的 mode="section" 会一章都不生成 ——
        //    用户点了「确认重写」「覆盖」却毫无反应。
        handleGenerateContent({
          section_id: selectedSection.key,
          mode: "section",
          force_rewrite: true,
        });
      },
    });
  }

  /** 字数压缩当前章节（对齐 OpenBidKit 的 shrink 修复器：AI 只输出局部
   *  replace/delete 操作，表格/图表/代码块/图片与技术参数受程序级保护） */
  const [shrinking, setShrinking] = useState(false);
  function handleShrinkCurrentSection() {
    if (!id || !selectedSection) return;
    const wc = selectedSection.word_count || 0;
    const budget = selectedSection.word_budget || 1200;
    if (wc <= budget) {
      msg.info(`当前字数 ${wc} 未超出目标 ${budget}，无需压缩`);
      return;
    }
    modal.confirm({
      title: "确认压缩本章？",
      content: (
        <div>
          「{selectedSection.title}」当前 <b>{wc}</b> 字，目标 <b>{budget}</b> 字。
          AI 将删除重复、空泛内容进行压缩（最多 3 轮）；
          <b>技术参数、表格、图表、代码块与图片受保护不会被修改</b>。
        </div>
      ),
      okText: "开始压缩",
      cancelText: "取消",
      onOk: async () => {
        setShrinking(true);
        try {
          const { data } = await sectionsApi.shrink(id, selectedSection.key!);
          // 就地更新选中章节与树（避免整页 load 闪烁）
          setSelectedSection(
            (prev) =>
              prev && prev.key === selectedSection.key
                ? { ...prev, content: data.content, word_count: data.word_count, word_status: data.word_status }
                : prev
          );
          setTree((prev) =>
            updateNodeFields(prev, selectedSection.key!, {
              content: data.content,
              word_count: data.word_count,
              word_status: data.word_status,
            })
          );
          msg.success(
            `压缩完成：${data.before} → ${data.word_count} 字（${data.rounds_used} 轮，${data.stop_reason}）`
          );
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "压缩失败");
        } finally {
          setShrinking(false);
        }
      },
    });
  }

  /** 重置正文：一键清空目录树中所有章节已生成的正文（目录结构保留，不可恢复） */
  function handleResetContent() {
    if (!id) return;
    modal.confirm({
      title: "确认重置正文？",
      content: (
        <div>
          将清空本方案目录树中<b style={{ color: "#ff4d4f" }}>所有章节已生成的正文</b>
          （含随正文生成的内联图表与章节审核状态），目录结构与字数预算保留。
          <div style={{ marginTop: 6 }}>
            该操作<b style={{ color: "#ff4d4f" }}>不可恢复</b>，重置后可重新生成。
          </div>
        </div>
      ),
      okText: "确认重置",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await sectionsApi.resetContent(id);
          await load();
          msg.success(`已重置：清除 ${data.cleared ?? 0} 个章节的正文`);
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "重置失败");
        }
      },
    });
  }

  /** 启动生成前的预估确认（统一入口，默认智能跳过已生成章节） */
  function confirmAndGenerate(scope: "all" | "missing" | "section", sectionId?: string) {
    const smartInfo = collectLeavesInfo(scope, sectionId, false);
    if (smartInfo.leaves.length === 0) {
      if (scope === "missing") {
        modal.info({
          title: "无需补全",
          content: (
            <div>
              范围内所有章节都已有正文内容。
              <div style={{ marginTop: 4 }}>
                如果需要重新生成已有的章节，请使用「🔄 重写本章」或
                在预估弹窗中勾选「覆盖已生成章节」。
              </div>
            </div>
          ),
        });
      } else {
        msg.info("没有找到可生成的章节");
      }
      return;
    }

    const concurrencyMap: Record<string, { label: string; eta: string }> = {
      slow: { label: "精细模式（并发 2）", eta: "约 2-3 分钟 / 10 章" },
      balanced: { label: "平衡模式（并发 3）", eta: "约 1-2 分钟 / 10 章" },
      fast: { label: "快速模式（并发 5）", eta: "约 30-60 秒 / 10 章" },
    };
    const cm = concurrencyMap[concurrencyOption];

    const totalAll = collectLeavesInfo(scope, sectionId, true).leaves.length;
    const smartSkipped = totalAll - smartInfo.leaves.length;

    const buildContent = (forceRewrite: boolean) => {
      const info = collectLeavesInfo(scope, sectionId, forceRewrite);
      return (
        <div style={{ lineHeight: 1.8 }}>
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="生成范围">
              {scope === "all" ? "本方案全部叶子章节"
               : scope === "missing" ? "仅未生成 / 失败章节"
               : `章节「${selectedSection?.title}」子树`}
            </Descriptions.Item>
            <Descriptions.Item label="本次将生成">
              <Text strong>{info.leaves.length}</Text> 个叶子章节
              {smartSkipped > 0 && !forceRewrite && scope !== "missing" && (
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                  （已智能跳过 {smartSkipped} 个已生成章节 ✅）
                </Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="预估总字数">
              <Text strong>{info.totalBudget.toLocaleString()}</Text> 字
            </Descriptions.Item>
            {info.willOverwrite > 0 && (
              <Descriptions.Item label="⚠️ 风险提示">
                <Text type="danger">将覆盖 {info.willOverwrite} 章已有正文</Text>
              </Descriptions.Item>
            )}
            <Descriptions.Item label="并发 / 模式">{cm.label}</Descriptions.Item>
            {/* ✅ F-CONTENT-STANDARD(2026-09-26 · F6)：弹窗必须显示本次实际生效的
                生成标准。缺这一行，用户点「开始生成」时无法确认要的是精准还是模糊 ——
                而这正是两种模式的唯一区别。 */}
            <Descriptions.Item label="生成标准">
              {generationStandard === "precise"
                ? "精准内容（数值与全局事实严格一致，缺失数据标注待补充）"
                : generationStandard === "fuzzy"
                  ? "模糊内容（以全局事实为基础，允许概括归纳，不得与事实冲突）"
                  : `按章节设置（章节级 → 方案默认「${STANDARD_LABELS[normalizeStandard(scheme?.generation_standard) || "precise"]}」）`}
            </Descriptions.Item>
            <Descriptions.Item label="预计耗时">{cm.eta}</Descriptions.Item>
          </Descriptions>
          <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
            💡 生成过程中可随时「暂停」/「恢复」/「停止」，失败章节可单独重试
          </div>
          {/* ✅ 强制全量重修：非受控（弹窗内容是快照，受控 checked 不会回显） */}
          <div style={{ marginTop: 8 }}>
            <Checkbox
              defaultChecked={forceFullRepair}
              onChange={(e) => setForceFullRepair(e.target.checked)}
            >
              强制全量重修（不跳过已修复且正文未变的冲突）
            </Checkbox>
            <div style={{ marginTop: 4, fontSize: 12, color: "#999" }}>
              💡 默认关闭：收尾的一致性修复会跳过「已修复且修复成果仍在正文里」的冲突以节省
              AI 调用；勾选后本次对所有命中的冲突重新调用 AI 修复（费用随冲突章节数增加）。
            </div>
          </div>
        </div>
      );
    };

    // 先弹默认（智能跳过）预估
    modal.confirm({
      title: "🚀 正文生成预估",
      width: 540,
      content: buildContent(false),
      okText: "开始生成（智能跳过）",
      okButtonProps: { type: "primary" },
      cancelText: "取消",
      onOk: () => {
        handleGenerateContent({
          section_id: scope === "section" ? sectionId : undefined,
          mode: scope === "section" ? "section" : scope,
          force_rewrite: false,
        });
      },
      footer: (_, { OkBtn, CancelBtn }) => (
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <Button
            danger
            onClick={() => {
              Modal.destroyAll();
              // 弹 force rewrite 确认
              modal.confirm({
                title: "⚠️ 强制覆盖所有章节",
                width: 540,
                content: buildContent(true),
                okText: "确认覆盖并重写",
                okButtonProps: { danger: true, type: "primary" },
                cancelText: "返回智能跳过",
                onOk: () => {
                  handleGenerateContent({
                    section_id: scope === "section" ? sectionId : undefined,
                    mode: scope === "section" ? "section" : scope,
                    force_rewrite: true,
                  });
                },
              });
            }}
          >
            强制覆盖（重新生成所有）
          </Button>
          <div>
            <CancelBtn />
            <OkBtn />
          </div>
        </div>
      ),
    });
  }

  /**
   * ✅ 二次增强：把仍处于「进行中」的章节日志项收尾，避免终态后残留转圈图标。
   *   终止事件（completed / stopped / 连接中断）后不可能再有 section_stage，
   *   留着 running 会让用户以为"还在跑"。
   *   status 用 skipped 表达"未完成"，与 failed（真失败）区分开。
   */
  const finalizeRunningLogs = (reason: string, status: "skipped" | "failed" = "skipped") => {
    const next = finalizeRunningLogsIn(sectionLogsRef.current, reason, status);
    if (next === sectionLogsRef.current) return; // 无 running 项，纯函数原样返回 → 跳过 setState
    sectionLogsRef.current = next;
    setSectionLogs([...next]);
  };

  /**
   * ✅ 二次增强：合并后端 completed / stopped 事件下发的失败章节明细。
   *   失败章节可能压根没进日志（异常路径 / 保留旧正文未改状态），
   *   只有后端知道"是哪几章、为什么失败"，这里补进日志供用户定位与重试。
   *   实现直接委托给 `mergeFailedSectionsIntoRef`（R-4 新增，供
   *   `useCallback([])` 的 `onSseEvent` 安全引用）—— **单一实现**，
   *   避免三处终态各写一份合并逻辑而行为漂移。
   */
  const mergeFailedSections = (list: unknown) => {
    mergeFailedSectionsIntoRef(list);
  };

  const handleGenerateContent = async (extra?: {
    section_id?: string;
    mode?: string;
    force_rewrite?: boolean;
    // F-CONTENT-STANDARD(2026-09-26): 任务级生成标准覆盖
    task_standard?: "precise" | "fuzzy";
    override_section_standard?: boolean;
  }) => {
    if (!id) return;
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = "";  // R106 FIX-8 · 正文链
    // 创建新的 AbortController，取消上一次未完成的 SSE
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：每次生成任务创建独立的 rAF 批处理器。先把上一次任务的
    //    残留批处理排空并 stop，再启用新实例，确保 stop() 的 stopped 标记
    //    不会污染本次任务的 schedule() 调用。
    sseBatcherRef.current?.stop();
    sseBatcherRef.current?.flushNow();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    setGenType("content");
    setTaskPaused(false);
    setProgress(0);
    setProgressMsg("正在连接...");
    setActiveTab("content");
    // 清空本次生成的章节日志与运行统计
    setSectionLogs([]);
    sectionLogsRef.current = [];
    setGenStats({});

    // 构造请求体
    const body: Record<string, unknown> = {};
    // ✅ R66：档位与总量覆盖互斥，档位优先（与后端 if/elif 分支同口径）
    const wbTier = resolveWordBudgetTier();
    if (wbTier) body.word_budget_tier = wbTier;
    const wbOverride = resolveWordBudgetOverride();
    if (wbOverride && !wbTier) body.word_budget_override = wbOverride;
    if (extra?.section_id) body.section_id = extra.section_id;
    if (extra?.mode) body.mode = extra.mode;
    body.concurrency = concurrencyOption;
    body.force_rewrite = !!extra?.force_rewrite;
    // ✅ 全文一致性 Agent 修复：正文生成后自动扫描 + 定向修复（默认开启）
    body.auto_consistency_repair = autoConsistencyRepair;
    body.consistency_severity = consistencySeverity;
    // ✅ 超字数自动压缩：接通后端 auto_shrink_over（仅对超目标 130% 章节生效）
    body.auto_shrink_over = autoShrinkOver;
    // ✅ 强制全量重修：true 时收尾的一致性修复不跳过「已修复且成果仍在」的冲突
    body.force_full_repair = forceFullRepair;
    // ✅ F-CONTENT-STANDARD(2026-09-26 · F1/F2)：生成标准**必须**进入请求体。
    //    旧实现只读 `extra.task_standard`，而所有调用方都不传 → 选项形同虚设
    //    （用户切「模糊内容」，生成结果与精准完全一致）。
    //    现统一由 contentStandard 纯函数按 UI 三选一映射（页面与 hook 共用一份口径）：
    //      precise/fuzzy → task_standard + override_section_standard=true
    //      inherit       → {}（不传任何字段，逐章回落，与旧客户端逐字节一致）
    //    `extra` 仍作为「单次调用的显式覆盖」保留，优先级高于 UI 选择。
    if (extra?.task_standard) {
      body.task_standard = extra.task_standard;
      // ⚠️ 显式传了标准值就必须同时要求覆盖，否则后端 override=false 会**静默忽略**
      //    该值（FR-3：override=false 时任务值一律不看）—— 旧实现此处漏判。
      body.override_section_standard = true;
    } else {
      Object.assign(body, buildStandardRequestFields(generationStandard));
    }

    let taskId = "";
    try {
      for await (const raw of sseFetch(`/sse/generate-content/${id}`, body, { signal: ac.signal })) {
        // 后端 SSE 契约：正文生成链事件，一次性断言（字段见 ContentGenEvent）
        const evt = raw as ContentGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (onSseEvent(evt)) {
          // ✅ 二次增强：任务终止（error）后收尾残留的「进行中」日志项，不再转圈
          finalizeRunningLogs("生成失败（任务已终止）", "failed");
          break;
        }

        // ---- 章节级细粒度事件 ----
        if (evt.event === "section_start") {
          const item: SectionLogItem = {
            section_id: evt.section_id,
            title: evt.title,
            status: "running",
            index: evt.index,
            total: evt.total,
            time: Date.now(),
            // ✅ 进度增强：章节起始阶段（后端 section_start 一并下发）
            stage: evt.stage,
            stage_label: evt.stage_label,
            // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：本章生效标准（模式徽标）
            generation_standard: evt.generation_standard,
          };
          sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, item);
          batcher.schedule("sectionLogs", () => setSectionLogs([...sectionLogsRef.current]));
        } else if (evt.event === "section_done") {
          const existing = sectionLogsRef.current.find((x) => x.section_id === evt.section_id);
          const item: SectionLogItem = {
            section_id: evt.section_id,
            title: evt.title,
            status: "success",
            word_count: evt.word_count,
            word_budget: evt.word_budget,
            word_status: evt.word_status,
            // ✅ 续写失败信号（后端 section_done 下发）：区别于「要点不足写不满」
            continue_failed: !!evt.continue_failed,
            // ✅ 缺口修复（2026-09-24 / 2026-10-04 修正）：后端 section_done 一并下发
            //    本章程序化质检问题（quality_issues），此前前端完全丢弃 —— 只有重新拉整篇
            //    正文才可能发现。现落到日志项，日志区直接提示。
            //    ⚠️ 2026-10-04：后端载荷是**对象**
            //    （{colloquial_hits:[], abolished_standards:[]}），旧实现按
            //    Array.isArray 判定 → 恒 undefined → 告警从未展示。统一走归一出口。
            quality_issues: normalizeQualityIssues(evt.quality_issues),
            // ✅ R49：本章图表产出闭环（配图 N 张 / 已删 M 张）—— 后端 section_done
            //    实时下发，日志区即时可见，用户不必等整批结束再翻报告才知道「少了几张图」。
            //    经 normalizeChartYield 归一（缺键/类型不符一律回退 0 与 []，不抛异常）。
            chart_yield: normalizeChartYield({
              chart_count: evt.chart_count,
              charts_dropped_count: evt.charts_dropped_count,
              charts_dropped: evt.charts_dropped,
            }),
            // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：本章生效标准 + 校验报告
            //    （字段存在却丢弃 = 用户完全看不到生成标准的执行结果）
            generation_standard: evt.generation_standard,
            standard_report: evt.standard_report ?? undefined,
            index: existing?.index,
            total: existing?.total,
            time: Date.now(),
            duration: existing ? Date.now() - existing.time : undefined,
          };
          sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, item);
          batcher.schedule("sectionLogs", () => setSectionLogs([...sectionLogsRef.current]));
          // ✅ BUG修复：如果完成的是当前选中章节，实时更新内容，不用等全量 load()。
          //    旧实现只更新字数/状态、**漏了 content**，导致章节重新生成后
          //    预览区仍停留在旧内容（甚至"（章节尚未生成…）"），直到整批结束才刷新。
          //    同时改用 selectedSectionRef 读取最新选中项（SSE 循环闭包里的
          //    selectedSection 是启动时的旧值，用户中途切换章节会更新错对象）。
          if (selectedSectionRef.current?.key === evt.section_id) {
            setSelectedSection((prev) => prev ? {
              ...prev,
              content: typeof evt.content === "string" ? evt.content : prev.content,
              word_count: evt.word_count || prev.word_count,
              word_budget: evt.word_budget || prev.word_budget,
              status: "generated",
            } : prev);
          }
        } else if (evt.event === "section_error") {
          const existing = sectionLogsRef.current.find((x) => x.section_id === evt.section_id);
          const item: SectionLogItem = {
            section_id: evt.section_id,
            title: evt.title,
            status: "failed",
            reason: evt.reason,
            index: existing?.index,
            total: existing?.total,
            time: Date.now(),
            duration: existing ? Date.now() - existing.time : undefined,
            stage: existing?.stage,
            stage_label: existing?.stage_label,
          };
          sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, item);
          batcher.schedule("sectionLogs", () => setSectionLogs([...sectionLogsRef.current]));
        } else if (evt.event === "section_stage") {
          // ✅ 进度增强：章节内阶段切换（构建上下文 → AI 生成中 → 续写扩充中 → 清洗落库）。
          //    单章 AI 调用可达数分钟，展示阶段与耗时才能让用户看清"这一章在做什么"。
          const existing = sectionLogsRef.current.find((x) => x.section_id === evt.section_id);
          if (existing) {
            const updated: SectionLogItem = {
              ...existing,
              stage: evt.stage,
              stage_label: evt.stage_label,
            };
            sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, updated);
            batcher.schedule("sectionLogs", () => setSectionLogs([...sectionLogsRef.current]));
          }
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ 进度增强：运行统计（含心跳通道周期推送的 ping）——单章 AI 调用期间
          //    唯一的实时通道，保证已耗时 / ETA / 累计字数 / 进行中章节持续刷新。
          // ✅ 性能优化：rAF 批处理 + 相同值去重，避免高频事件引发多余渲染。
          const statsSnapshot = pickStats(evt);
          batcher.schedule("genStats", () =>
            setGenStats((prev) => statsEqual(prev, statsSnapshot) ? prev : statsSnapshot),
          );
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
          }
        } else if (evt.event === "consistency_scan_start") {
          batcher.schedule("progress", () => {
            setProgress(evt.progress ?? 0.95);
            setProgressMsg("正文完成，开始全文一致性扫描...");
          });
        } else if (evt.event === "consistency_scan_progress") {
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
          }
          batcher.schedule("progressMsg", () => setProgressMsg(evt.message || "正在扫描全文一致性..."));
        } else if (evt.event === "consistency_scan_done") {
          batcher.schedule("progress", () => {
            setProgress(0.98);
            setProgressMsg(`一致性扫描完成：发现 ${evt.summary?.total ?? 0} 处冲突，正在定向修复...`);
          });
        } else if (evt.event === "consistency_repair_progress") {
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
          }
          batcher.schedule("progressMsg", () => setProgressMsg(evt.message || "正在定向修复冲突..."));
        } else if (evt.event === "consistency_repair_done") {
          batcher.schedule("progress", () => {
            setProgress(1);
            setProgressMsg(`一致性修复完成：已修复 ${evt.repaired ?? 0} 处（待你确认），跳过 ${evt.skipped ?? 0} 处（待人工），失败 ${evt.failed ?? 0} 处`);
          });
          // ✅ G11：生成链路里的自动一致性修复改写了正文 → 审核 / 总检结论失效
          if ((evt.repaired ?? 0) > 0) setReviewTick((t) => t + 1);
        } else if (evt.event === "consistency_skipped") {
          batcher.schedule("progress", () => {
            setProgress(1);
            setProgressMsg(evt.reason || "方案尚无正文，已跳过一致性检查");
          });
        } else if (evt.event === "consistency_failed") {
          batcher.schedule("progressMsg", () =>
            setProgressMsg(`全文一致性处理失败（不影响正文）：${evt.reason || ""}`),
          );
        }

        // ✅ 2026-10-10：`degraded` 必须一并收口 —— 后端一致性修复有失败项时
        //   终态事件名就是 degraded（同语义的加法式终态）。旧实现只认 completed
        //   ⇒ 降级完成时不 break / 不 load() / 不给任何提示，正文停留在旧内容，
        //   且成稿含未修复缺陷而用户完全无感（P0-3 的下半截没接到前端）。
        if (isTerminalContentEvent(evt)) {
          // ✅ 性能优化：终态事件立即 flush，确保 UI 与后续 load() 前的最后一次渲染一致
          batcher.flushNow();
          setProgress(1);
          // ✅ 优先采用后端统计的失败章节数（done_ids 口径，含未进入日志的失败）
          const failedCount = (typeof evt.failed_count === "number")
            ? evt.failed_count
            : sectionLogsRef.current.filter((l) => l.status === "failed").length;
          // ✅ 二次增强：先补失败明细（章节可能未进日志），再收尾残留的进行中项
          mergeFailedSections(evt.failed_sections);
          finalizeRunningLogs("未完成（生成已结束）", "failed");
          const doneMsg = evt.message || `正文生成完成，总字数 ${evt.word_count || 0}`;
          // ✅ 2026-10-10：降级完成优先于「成功」—— 一致性修复有失败项时，
          //   成稿可导出但含未修复缺陷，报 success 会让用户误以为一切就绪。
          const _degradedHint = degradedConsistencyHint(evt);
          if (_degradedHint) {
            msg.warning(_degradedHint);
            setProgressMsg(`${doneMsg} | ${_degradedHint}`);
          } else if (failedCount > 0) {
            msg.warning(doneMsg + `（${failedCount} 章失败）`);
            setProgressMsg(doneMsg + `（${failedCount} 章失败）`);
          } else {
            msg.success(doneMsg);
            setProgressMsg(doneMsg);
          }
          // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：生成标准校验汇总提示
          //    （仅在「有问题章数 > 0」时追加，无问题不制造噪音）
          const _stdHint = standardSummaryHint(evt.standard_summary);
          if (_stdHint) { msg.warning(_stdHint); setProgressMsg(`${doneMsg} | ${_stdHint}`); }
          // ✅ R77b（2026-10-15）：事实冲突定点修复汇总提示（与标准校验提示
          //    独立成条；无冲突返回空串 → 不追加，不制造噪音）
          const _faHint = factAutofixHint(evt.fact_autofix_summary);
          if (_faHint) { msg.info(_faHint); setProgressMsg(`${doneMsg} | ${_faHint}`); }
          // ✅ 全文一致性 Agent 修复结果摘要（后端随 completed 一并下发）
          if (evt.consistency_summary) {
            const cs = evt.consistency_summary;
            setCrSummary(cs);
            if (cs.scan_id) setCrScanId(cs.scan_id);
            if ((cs.total ?? 0) > 0) {
              msg.info(
                `全文一致性 Agent 修复：发现 ${cs.total} 处冲突，已修复 ${cs.repaired ?? 0} 处，`
                + `${cs.failed ?? 0} 处失败 —— 可在「正文生成 → 修复工作台」逐条确认或回滚`
              );
            }
          }
          load();
          break;
        } else if (evt.event === "stopped") {
          // ✅ 性能优化：终态事件立即 flush，确保停止时的最终进度立即展示
          batcher.flushNow();
          // ✅ 二次增强：停止时把未完成章节收尾为"已停止"（不再转圈），
          //    并保留停止时的真实进度（后端 stopped 事件携带 progress）。
          if (typeof evt.progress === "number") setProgress(evt.progress);
          // ✅ 缺口修复（2026-09-24）：后端 stopped 事件现已随事件下发
          //    failed_sections / failed_count（此前只落 checkpoint，在线路径拿不到）。
          //    与 completed 分支走同一合并入口，停止后立即可见失败明细。
          mergeFailedSections(evt.failed_sections);
          finalizeRunningLogs("已停止，未生成完成", "skipped");
          const stoppedFailed = typeof evt.failed_count === "number" ? evt.failed_count : 0;
          msg.info(stoppedFailed > 0 ? `已停止（${stoppedFailed} 章失败，详见下方日志）` : "已停止");
          load();
          break;
        } else {
          // ✅ 修复：章节级事件（section_start/done/error、consistency_*）不携带
          // progress 字段，旧实现 `evt.progress || 0` 会把进度条打回 0 造成跳变。
          // 仅在事件真正携带进度/消息时才更新。
          if (evt.progress !== undefined) {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
          }
          if (evt.message) {
            batcher.schedule("progressMsg", () => setProgressMsg(evt.message!));
          }
          // ✅ 进度增强：progress 事件随附的权威统计（ETA / 累计字数 / 进行中章节）
          if (evt.stats) {
            const statsSnapshot = pickStats(evt.stats);
            batcher.schedule("genStats", () =>
              setGenStats((prev) => statsEqual(prev, statsSnapshot) ? prev : statsSnapshot),
            );
          }
        }
      }
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err.name !== "AbortError") {
        msg.warning(err.message || "SSE 连接中断");
        // ✅ 二次增强：连接中断时先收尾「进行中」日志项（后台任务可能仍在跑，
        //    但本页已无法再收到 section_stage —— 残留转圈会误导用户）。
        finalizeRunningLogs("连接中断，等待后台任务收尾", "skipped");
        // ✅ BUG 修复（2026-10-14 · P1 正文生成断线不重挂）：与目录生成同口径。
        //    旧实现仅当 taskId 非空才 pollTaskUntilTerminal 重挂；但 SSE 在收到
        //    首个携带 task_id 的事件之前就断连（30s 建连超时 / 网络抖动 / 网关重连）
        //    时 taskId 恒为空串 → 整个重挂分支被跳过。此时后端任务已注册并继续
        //    生成（AI 照常计费），成果只落在 task_registry.checkpoint_json，用户
        //    只看到「SSE 连接中断」，逐章失败明细与已生成字数彻底丢失，必须手动
        //    刷新才能看到结果。
        //    后端 GET /sse/tasks 本就是为「按方案检索最近任务」设计的
        //    （见 sse_handlers.list_tasks 的注释），findRecentOutlineTaskImpl 也已
        //    支持 typeFilter 参数（目录生成在 :5373 就这么用），此处复用同一机制，
        //    只把任务类型换成 content_generation。检索不到时维持旧行为：仅提示。
        // ✅ 收口（2026-10-14）：与目录/事实同源走 reattachTaskImpl
        //    （typeFilter=content_generation）。终态后的逐章失败明细 / 进度摘要
        //    恢复仍由本块处理（content_result 各链语义不同）。
        const hit = await reattachTaskImpl(
          async () => (await tasksApi.list(id)).data,
          pollTaskUntilTerminal,
          {
            taskId,
            typeFilter: "content_generation",
            onReattach: () =>
              setProgressMsg("连接中断，正在重新挂接后台任务..."),
          },
        );
        if (hit) {
          taskId = hit.taskId;
          const fin = hit.final;
          // 后端早已把「失败哪几章、为什么失败」落进 checkpoint（_CHECKPOINT_KINDS
          // 的 content_generation 白名单 + task_status 的 _attach_checkpoint_result），
          // 但前端此前从未读取 fin.content_result —— 断线/刷新后用户只看到
          // 「后台任务已停止」，逐章失败原因彻底丢失，只能凭目录树一章章猜。
          // 这里与在线 completed 分支（mergeFailedSections）口径对齐。
          const ck = contentResultFailedSections(fin?.content_result);
          if (ck.sections.length) {
            mergeFailedSections(ck.sections);
            msg.warning(
              `后台正文生成有 ${ck.failedCount} 章失败，已在下方日志列出明细（可逐章重试）`,
            );
          }
          // ✅ 增强（2026-09-21）：断线重挂时展示 checkpoint 进度摘要（done/total/words/over_count），
          // 用户刷新后不再只看到「后台任务已停止」，而是能判断「生成了多少章、写了多少字」。
          const summary = contentResultSummary(fin?.content_result);
          if (summary.total > 0) {
            const overMsg = summary.overCount > 0 ? `，${summary.overCount} 章超字数` : "";
            msg.info(
              `后台正文生成：已完成 ${summary.done}/${summary.total} 章，` +
              `共 ${summary.words} 字${overMsg}`,
            );
          }
          if (fin?.status === "completed") {
            msg.success("正文生成已在后台完成，正在刷新");
            load();
          } else if (fin?.status === "degraded") {
            // ✅ 2026-10-10：降级完成（一致性修复有失败）也要刷新 + 告警
            msg.warning("正文生成已在后台降级完成：一致性修复有失败项，成稿含未修复缺陷");
            load();
          } else if (fin?.status === "failed") {
            msg.error(fin.message || "后台任务失败");
          } else if (fin?.status === "stopped") {
            msg.info("后台任务已停止");
            load();
          }
        }
      }
    } finally {
      if (abortControllerRef.current === ac) {
        abortControllerRef.current = null;
      }
    }
    // ✅ 世代校验：被重入（abort）的旧协程不得复位新任务的状态
    if (genSeqRef.current === mySeq) {
      setGenerating(false);
      setGenType("");
      setTaskPaused(false);
    }
    // ✅ 性能优化：任务结束或异常退出后，排空残余批处理并停止，
    //    避免旧任务的 schedule() 闭包在新任务已开始后才触发，污染 UI 状态。
    batcher.flushNow();
    batcher.stop();
    if (sseBatcherRef.current === batcher) sseBatcherRef.current = null;
  };

  /** 生成当前选中章节及其子树 */
  const handleGenerateCurrentSection = async () => {
    if (!selectedSection) {
      msg.warning("请先从左侧目录树选择一个章节");
      return;
    }
    confirmAndGenerate("section", selectedSection.key);
  };

  /** 补全生成所有空章节 */
  const handleGenerateMissing = async () => {
    confirmAndGenerate("missing");
  };

  /** ✅ 续写当前章节（对齐 OpenBidKit 任意点续写）：
      以已有正文为底稿继续补充内容，不覆盖既有正文 */
  const handleContinueCurrentSection = () => {
    if (!selectedSection) {
      msg.warning("请先从左侧目录树选择一个章节");
      return;
    }
    if (generating) return;
    handleGenerateContent({
      section_id: selectedSection.key,
      mode: "continue",
      force_rewrite: false,
    });
  };

  const runGenerateFactsSse = async () => {
    if (!id) return;
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = "";  // R106 FIX-8 · 事实链
    // 创建新的 AbortController
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：为事实提取 SSE 创建独立的 rAF 批处理器
    sseBatcherRef.current?.stop();
    sseBatcherRef.current?.flushNow();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    setGenType("facts");
    setProgress(0);
    setProgressMsg("正在提取...");
    // ✅ 修复：旧实现无条件 setActiveTab("facts")，confirm 弹窗停留期间用户
    //    切走 Tab 会被强行拽回。入口按钮就在 facts Tab 内，无需跳转。
    // 清空本次提取日志与上次的分段统计
    setFactsLogs([]);
    factsLogsRef.current = [];
    setFactsSegmentStats(null);
    setFactsCrossConflicts([]);
    let lastProgress = 0;
    const pushLog = (p: number, m: string) => {
      if (!m) return;
      const list = factsLogsRef.current;
      if (list.length && list[list.length - 1].message === m) return;
      factsLogsRef.current = [...list, { progress: p, message: m, time: Date.now() }];
      batcher.schedule("factsLogs", () => setFactsLogs(factsLogsRef.current));
    };
    let taskId = "";
    try {
      for await (const raw of sseFetch(`/sse/generate-facts/${id}`, { missing_value_mode: missingValueMode }, { signal: ac.signal })) {
        // 后端 SSE 契约：全局事实提取链事件，一次性断言（字段见 FactsGenEvent）
        const evt = raw as FactsGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (evt.event === "error") {
          pushLog(lastProgress, `❌ ${evt.message || "提取失败"}`);
        } else if (evt.event === "warning") {
          const wm = evt.message || (Array.isArray(evt.warnings) ? evt.warnings.join("；") : "");
          if (wm) pushLog(lastProgress, `⚠️ ${wm}`);
        }
        if (onSseEvent(evt)) break;
        if (evt.event === "completed") {
          lastProgress = 1;
          setProgress(1);
          const doneMsg = evt.message || "全局事实提取完成";
          setProgressMsg(doneMsg);
          pushLog(1, `✅ ${doneMsg}`);
          // ✅ BUG 修复（2026-09-21）：收口到 applyFactsCompletedEvent ——
          //    增量「全部跳过（all_skipped）」的 completed 不带 segment_stats，
          //    旧实现 `if (evt.segment_stats)` 只写不清，上一次提取的失败告警 /
          //    跨段矛盾横幅会残留到本次（甚至跨方案，直到下一次提取覆盖）。
          const _done = applyFactsCompletedEvent(evt);
          setFactsSegmentStats(_done.segmentStats);
          setFactsCrossConflicts(_done.crossConflicts);
          if ((evt.segment_stats?.failed || 0) > 0) {
            msg.warning(doneMsg);
          } else {
            msg.success("全局事实提取完成");
          }
          loadFacts();
          break;
        } else if (evt.event === "stopped") {
          pushLog(lastProgress, "⏹ 已停止");
          msg.info("已停止");
          break;
        } else if (evt.event === "progress" || evt.event === "connecting") {
          lastProgress = evt.progress || 0;
          setProgress(lastProgress);
          setProgressMsg(evt.message || "");
          pushLog(lastProgress, evt.message || "");
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ P1 修复（2026-09-27 · 心跳统计消费）：后端 facts 链路已接入
          //    stats_provider（sse_handlers.py generate_facts），单段 AI 调用期间
          //    event_stream 挂起无法 yield progress，靠心跳周期推 ping 携带
          //    progress/elapsed_ms。此处消费它消除「进度条长时间静止」。
          //    注意：ping **不得**推进 lastProgress 的语义之外的东西，也不写日志
          //    （否则日志行会被心跳刷屏）；仅按单调口径更新进度条与耗时提示。
          //    未知/未来事件一律落到此分支之外被静默忽略，保持向前兼容。
          //
          // ✅ 2026-10-12（技术债 P2-11 · 事件名口径对齐）：
          //    本分支原只认 "ping"，而同一文件的目录生成分支（:5200）与正文生成
          //    分支（:6022）都是 `"stats" || "ping"` 双认；且后端
          //    `services/ai/task_registry.py` 的 broadcast 确实会推
          //    `{"event": "stats", ...}` —— 三处消费同一套 SSE 协议却有两种
          //    白名单，stats 到达本链路时被静默丢弃（进度条不更新、已用时不动），
          //    且症状只在这一个入口出现，极易被误判成「后端没推」。
          //    现与其它两处同口径（纯加法：原本被忽略的事件现在被消费）。
          const p = typeof evt.progress === "number" ? evt.progress : null;
          if (p !== null && p > lastProgress) {
            lastProgress = p;
            setProgress(p);
          }
          const el = typeof evt.elapsed_ms === "number" ? evt.elapsed_ms : null;
          if (el !== null && el >= 0) {
            setProgressMsg(`正在提取全局事实…（已用时 ${Math.round(el / 1000)}s）`);
          }
        }
      }
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err.name !== "AbortError") {
        msg.warning(err.message || "SSE 连接中断");
        // ✅ BUG 修复（2026-10-14 · P1 事实提取断线不重挂）：与目录/正文生成同口径。
        //    旧实现仅当 taskId 非空才重挂；建连阶段（首个事件前）断连时 taskId 为空
        //    → 重挂分支整体跳过，而后端任务已注册并继续跑（AI 照常计费），
        //    segment_stats / cross_conflicts 只落在 facts_result checkpoint，
        //    用户只看到「SSE 连接中断」，失败段数与跨段矛盾彻底丢失。
        //    复用 findRecentOutlineTaskImpl 的 typeFilter 参数（原设计即为此），
        //    检索不到时维持旧行为：仅提示。
        // ✅ 收口（2026-10-14）：与目录/正文同源走 reattachTaskImpl
        //    （typeFilter=facts_generation）。终态后的 segment_stats / cross_conflicts
        //    恢复仍由本块处理（facts_result 各链语义不同）。
        const hit = await reattachTaskImpl(
          async () => (await tasksApi.list(id)).data,
          pollTaskUntilTerminal,
          {
            taskId,
            typeFilter: "facts_generation",
            onReattach: () =>
              setProgressMsg("连接中断，正在重新挂接后台任务..."),
          },
        );
        if (hit) {
          taskId = hit.taskId;
          const fin = hit.final;
          if (fin?.status === "completed") {
            // ✅ P0 修复（2026-09-27 · 断线重挂成果恢复）：后端已把 completed 事件的
            //    segment_stats / cross_conflicts 落进 facts_result checkpoint 并由
            //    GET /sse/task/{id} 回传。此处复用**同一个** applyFactsCompletedEvent
            //    解析器（与在线 completed 分支 :5408 完全同源），保证重挂接后
            //    失败段数/跨段矛盾横幅照常呈现，而不是只弹一句"已完成"。
            //    缺 facts_result 时（后端版本旧 / checkpoint 写失败）回退原行为。
            const _fin = fin.facts_result;
            // ✅ 进度条收尾（2026-09-27）：断线重挂成功后必须把进度推到 100%
            //    并回填终态文案，否则进度条永远停在中断刻度（对比在线分支
            //    :5420-5423 的 setProgress(1) + setProgressMsg(doneMsg)）。
            const _doneMsg = _fin?.message || "全局事实提取已在后台完成";
            lastProgress = 1;
            setProgress(1);
            setProgressMsg(_doneMsg);
            pushLog(1, `✅ ${_doneMsg}`);
            if (_fin) {
              const _d = applyFactsCompletedEvent(_fin);
              setFactsSegmentStats(_d.segmentStats);
              setFactsCrossConflicts(_d.crossConflicts);
              // segment_stats 为生成的宽松契约（Record<string, unknown>），failed 实际为 number
              if (((_fin.segment_stats?.failed as number | undefined) || 0) > 0) {
                msg.warning(_doneMsg);
              } else {
                msg.success(_doneMsg);
              }
            } else {
              // ✅ 状态必须**显式清空**（2026-09-27）：在线路径的
              // applyFactsCompletedEvent 语义是「缺字段即清空」，而重挂接
              // 拿不到 checkpoint 时若什么都不做，上一次提取的失败告警 /
              // 跨段矛盾横幅会残留在界面上（甚至跨方案）。此处复用**同一个**
              // 解析器（传空对象）拿到规范化的「空」值，与在线路径逐字一致 ——
              // 不手写 null/[]，避免两处口径再次分叉。
              const _empty = applyFactsCompletedEvent({});
              setFactsSegmentStats(_empty.segmentStats);
              setFactsCrossConflicts(_empty.crossConflicts);
              msg.success(_doneMsg);
            }
            loadFacts();
          } else if (fin?.status === "failed") {
            msg.error(fin.message || "后台任务失败");
          } else if (fin?.status === "stopped") {
            msg.info("后台任务已停止");
          }
        }
      }
    } finally {
      if (abortControllerRef.current === ac) {
        abortControllerRef.current = null;
      }
    }
    // ✅ 世代校验：被重入（abort）的旧协程不得复位新任务的状态
    if (genSeqRef.current === mySeq) {
      setGenerating(false);
      setGenType("");
    }
    // ✅ 性能优化：任务结束或异常退出后，排空残余批处理并停止
    batcher.flushNow();
    batcher.stop();
    if (sseBatcherRef.current === batcher) sseBatcherRef.current = null;
  };

  /** ✅ 优化：合并原「从资料文档提取」（REST）与 SSE 提取（两者功能重复），
      统一入口 + 已有事实时的覆盖确认 */
  const handleGenerateFacts = async () => {
    if (!id) return;
    // ✅ 分步工作流 ③ 前置校验：必须有已解析的文档
    const parsedCount = docList.filter((d) => isDocParsed(d)).length;
    if (parsedCount === 0) {
      const failed = docList.filter((d) => isDocFailed(d)).length;
      msg.warning(failed > 0
        ? `有 ${failed} 个文件解析失败，请先重试解析`
        : docList.length > 0
          ? `有 ${docList.length} 个文件尚未解析，请先点击「解析文档」`
          : "请先上传资料文件（上传保存 → 解析文档 → AI 提取）");
      return;
    }
    if ((factsSummary?.total || 0) > 0) {
      modal.confirm({
        title: "重新提取全局事实",
        content: "将基于已上传的项目资料（设计文件/地勘报告/合同等）重新提取全局事实变量。已确认和手动新增的事实会保留，仅未确认的 AI 提取事实会被刷新覆盖。",
        okText: "开始提取",
        cancelText: "取消",
        onOk: () => runGenerateFactsSse(),
      });
      return;
    }
    await runGenerateFactsSse();
  };

  // R-P1 拆分：上传/解析/预览/四层管线/分类/批量确认/过期解除/删除文档域处理器
  // 迁至 useDocumentWorkflow（调用点保持在原位；批确认/过期解除/删除段原在
  // 「提取项目」块之后，随域合并进同一 hook，见该文件头注释）。
  const {
    handleUploadDocuments, handleParseDocuments, handleParseDocument,
    handlePreviewDocument, handlePipelineAction, handleCategoryChange,
    handleBatchResolve, handleAckStaleAll, handleAckStaleOne, handleDeleteDocument,
  } = useDocumentWorkflow({
    id, schemeProjectId, msg, modal,
    uploadingFacts, setUploadingFacts, setUploadedFiles,
    maxUploadBytes, maxUploadTotalBytes, maxUploadFiles,
    uploadAbortRef, parseAbortRef,
    parsingDocs, setParsingDocs, parsingDocId, setParsingDocId,
    docList, loadDocuments,
    setPreviewDoc, setPreviewText, setPreviewData, setPreviewLoading,
    previewDoc, setPipelineStatus, setPipelineError, setPipelineLoading,
    pipelineAction, setPipelineAction,
    setDocList, loadFacts, factsSummary,
    factsListAbortRef, currentSchemeIdRef,
  });

  // ============================================================
  // 「提取项目」(bidAnalysis) 处理器
  // ============================================================

  // R-P1 拆分：「提取项目」域处理器（SSE/停止/多标段/校正/配置启动）迁至 useBidAnalysis
  const {
    runBaSse, handleStopBa, handleCheckSections, handleRerunItem,
    openBaEdit, handleBaSaveEdit, handleBaClearEdit, handleStartBaFromConfig,
  } = useBidAnalysis({
    id, schemeProjectId, msg,
    baSseAbortRef, setBaRunning, setBaProgress, setBaProgressMsg,
    setBaTextStats, setBaItems, baItems, baDefsRef, setBaSummary,
    setSelectedBaItem, loadBaResults, autoJumpFrom,
    baStoppingRef, setBaStopping,
    sectionChecking, setSectionChecking, setSectionCheckResult,
    baEditItem, setBaEditItem, baEditValue, setBaEditValue,
    setBaEditSaving, setBaConfigOpen,
    baModeOpt, baSelectedIds, baForceRerun,
  });


  // ============================================================
  // 目录生成 Tab 的辅助函数
  // （treeToOutline / renumberOutline / outlineToTreeNode 已提升至
  //   模块级导出，行为由 outlineTab / outlineTreeLogic 测试钉住）
  // ============================================================

  // R-P1 拆分：两个滚动 ref 声明自下方 4244 行上移至此（moveNode 依赖，避 TDZ）。
  const editTreeScrollRef = useRef<HTMLDivElement>(null);
  const leftTreeScrollRef = useRef<HTMLDivElement>(null);
  // ------ 目录 Tab 中的 Tree 渲染（支持编辑、增删） ------
  // R-P1 拆分：renamingKey / renamingValue 声明自下方 3996 行上移
  // （setRenamingKey / setRenamingValue 是 useOutlineTreeEdit 的 deps，避 TDZ）
  const [renamingKey, setRenamingKey] = useState<string | null>(null);
  const [renamingValue, setRenamingValue] = useState<string>("");
  // R-P1 拆分：目录树编辑回调（新增/同级/删除/重命名/上下移）迁至
  // useOutlineTreeEdit；flashReorderHint 为组件内提升的函数声明，经 deps 传入。
  const {
    addChildNode, addSiblingNode, deleteNode, renameNode, moveNode,
    findParentKey,
  } = useOutlineTreeEdit({
    id, msg, modal, load,
    treeRef, expandedKeysRef, editTreeScrollRef, leftTreeScrollRef,
    setTree, setSelectedSection, setRenamingKey, setRenamingValue,
    setExpandedKeys, flashReorderHint,
  });

  // （hasDeepOutlineNodes 已提升至模块级导出）

  /** 保存当前目录树到后端（含自动编号） */
  const saveOutlineTree = useCallback(async () => {
    if (!id) return;
    // ✅ BUG 修复：手动编辑/拖拽可能产生 4 级及更深节点，后端 normalize_outline
    // 会静默裁剪并把深层标题并入父章节描述——用户"保存成功"后节点消失却无提示。
    // 现保存前检测并二次确认，让用户知情。
    if (hasDeepOutlineNodes(treeRef.current)) {
      modal.confirm({
        title: "目录包含超过三级的章节",
        content: (
          <div>
            系统目录最多支持 <b>三级</b>。保存时，三级以下的深层章节标题将
            <b>自动并入其父章节的章节说明</b>，不会作为独立章节保留。
            <br />
            如需保留这些内容，请先取消并手动将其调整为二级/三级章节。
          </div>
        ),
        okText: "仍然保存",
        cancelText: "返回调整",
        onOk: () => doSaveOutlineTree(),
      });
      return;
    }
    await doSaveOutlineTree();
  }, [id, modal, msg, load]);

  async function doSaveOutlineTree() {
    if (!id) return;
    const outline = renumberOutline(treeToOutline(treeRef.current));
    try {
      const { data } = await sectionsApi.saveOutline(id, { outline, source: "手动编辑" });
      msg.success(`目录已保存，共 ${data.count || collectAllKeys(treeRef.current).length} 章节`);
      notifyClearedContent(data);
      notifyGenericResidual(data);
      // ✅ G10：目录结构变更会改变预检的章节集合 → 刷新审核 / 就绪度工作台
      setReviewTick((t) => t + 1);
      await load();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "保存目录失败");
    }
  }

  /**
   * ✅ 字数预算编辑回抛（OutlineNodeBudgetPanel → 本处统一落库）。
   *
   * 口径与「改名」完全对齐：
   *   · 已落库章节 → 立即 PATCH /sections/{id}，失败即明示（不静默吞掉）；
   *   · 临时节点（local_/upload_）→ 仅改本地，等「保存目录」随树一起落库。
   * 旧实现整条链路无预算写入入口（见 OutlineNodeBudgetPanel 说明）。
   */
  function handleBudgetChange(key: string, budget: number) {
    const node = findSectionById(tree, key);
    if (!node) return;
    setTree(updateNodeFields(tree, key, { word_budget: budget }));
    setSelectedSection((prev) =>
      prev && prev.key === key ? { ...prev, word_budget: budget } : prev
    );
    if (isUnsavedLocalKey(key)) {
      flashReorderHint("字数预算已本地调整，点击「保存目录」后生效");
      return;
    }
    if (!id) return;
    sectionsApi
      .update(id, key, { word_budget: budget })
      .then(() => {
        flashReorderHint(`「${node.title}」字数预算已更新为 ${budget} 字`);
      })
      .catch((err: unknown) => {
        const saveFail = err as { message?: string };
        msg.warning(
          `本地已把字数预算改为 ${budget} 字，但服务端保存失败（${saveFail?.message || "未知错误"}）——` +
            `请点击「保存目录」持久化`
        );
        flashReorderHint("⚠️ 字数预算未同步到服务端，请点击「保存目录」持久化");
      });
  }

  /**
   * ✅ 接线 sectionsApi.exportTree：
   * 从后端取目录树（程序化重排编号后）并导出为 JSON 文件。
   * 与「保存目录」的区别：这里导出的是后端权威编号（level 已归一），
   * 可直接给外部工具/存档使用，不改动数据库。
   */
  const [exportingTree, setExportingTree] = useState(false);
  async function handleExportTree() {
    if (!id || exportingRef.current || exportingTree) return;
    setExportingTree(true);
    try {
      const { data } = await sectionsApi.exportTree(id);
      const outline = data.outline || [];
      const total = countOutline(outline);
      if (total === 0) {
        msg.info("当前目录为空，无可导出内容");
        return;
      }
      // ✅ 编码修复：JSON 下载前置 UTF-8 BOM（\uFEFF），避免 Windows 记事本打开乱码；
      //    重新导入时前端会剥离 BOM，故不影响解析。
      const blob = new Blob(["\uFEFF" + JSON.stringify(outline, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${scheme?.name || "方案"}目录树.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      msg.success(`目录树已导出，共 ${total} 个章节`);
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "导出目录树失败");
    } finally {
      setExportingTree(false);
    }
  }

  // ===== ✅ 目录库：套用到当前方案 =====
  /** 打开目录库选择器（只列审核通过的库） */
  const [libError, setLibError] = useState("");
  async function openLibraryPicker() {
    setLibOpen(true);
    setLibLoading(true);
    setLibError("");
    try {
      const { data } = await outlineLibraryApi.list({ review_status: "已通过" });
      setLibItems(data.items || []);
      if ((data.items || []).length === 0) {
        setLibError("目录库中暂无「已通过」审核的目录。请先到「专项方案目录库」页新建并审核通过。");
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      setLibError(err.message || "加载目录库失败");
    } finally {
      setLibLoading(false);
    }
  }

  /** 套用某个目录库到当前方案：apply 取 outline_json → 写入 sections */
  function handleApplyLibrary(lib: OutlineLibraryItem) {
    if (!id) return;
    modal.confirm({
      title: "⚠️ 确认套用该目录库？",
      content: (
        <div>
          将用「<b>{lib.name}</b>」的目录（{lib.version || "-"}）<b style={{ color: "#ff4d4f" }}>覆盖</b>当前方案目录，
          当前已生成的正文（含其中内嵌的图表代码块）将<b style={{ color: "#ff4d4f" }}>一并丢失</b>，不可恢复。
        </div>
      ),
      okText: "覆盖套用",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setLibApplying(true);
        try {
          // ✅ 优化：改用 /apply-and-save 一步完成（后端取库内容 + 裁剪三级
          //    + 按目录库结构重建 sections + 累加引用计数），
          //    替代原来"apply 取 JSON → 前端解析 → save-outline 再提交"的两次往返
          //    （旧链路在第二步失败时引用计数已被累加，且大目录 JSON 多传一次）。
          //    ⚠️ 注意：库节点 id 是编号而非章节主键，无法与已有章节匹配 →
          //    套用是**整表重建**，已有正文不保留（上方确认弹窗文案与此口径一致）。
          const { data } = await outlineLibraryApi.applyAndSave(lib.id!, { scheme_id: id });
          msg.success(`已套用「${lib.name}」，共 ${data.count || 0} 个章节`);
          // ✅ R88 FIX-9：本消费点此前**两个加法式键都不读** ⇒ 双重静默：
          //   ① `cleared_content_sections` —— 套用是整表重建、**已有正文被级联
          //      删除**，后端为这件事专门量化回传（2026-09-30 P1-7），此处不读
          //      ⇒ **用户静默丢正文**；
          //   ② `generic_residual` —— 套用命中占位/泛化标题时的明细清单，
          //      后端 R79 起就回传，经 R88 FIX-8 才真正透传到此处。
          notifyClearedContent(data);
          notifyGenericResidual(data);
          setLibOpen(false);
          setSelectedSection(null);
          await load();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "套用目录库失败");
        } finally {
          setLibApplying(false);
        }
      },
    });
  }

  /** 把「导入目录（智能识别）」的结果存为目录库（待审核） */
  async function handleSaveAsLibrary() {
    if (!lastUpload) {
      msg.warning("请先用「导入目录（智能识别）」上传文件");
      return;
    }
    setSaveLibOpen(true);
    saveLibForm.setFieldsValue({ name: lastUpload.name.replace(/\.[^.]+$/, "") });
  }

  async function submitSaveAsLibrary() {
    if (!lastUpload) return;
    try {
      const values = await saveLibForm.validateFields();
      const { data } = await uploadOutlineApi.saveAsLibrary(lastUpload.id, {
        name: values.name,
        outline: lastUpload.outline,
      });
      msg.success(`已存入目录库（${data.id?.slice(0, 8) || "ok"}），状态为「待审核」`);
      setSaveLibOpen(false);
    } catch (e: unknown) {
      const err = e as { errorFields?: unknown; message?: string };
      if (err.errorFields) return;
      msg.error(err.message || "存入目录库失败");
    }
  }

  /** 清除所有目录（调 saveOutline 传空数组 → 后端先 DELETE 再 INSERT，自然清空） */
  function clearAllOutline() {
    if (!id) return;
    const total = collectAllKeys(tree).length;
    if (total === 0) {
      msg.info("当前已经没有目录");
      return;
    }
    modal.confirm({
      title: "⚠️ 确认清除所有目录？",
      content: (
        <div>
          本方案下的 <b style={{ color: "#ff4d4f" }}>{total}</b> 个目录章节将被<b style={{ color: "#ff4d4f" }}>全部删除</b>，
          包括已生成的正文内容。此操作不可恢复！
        </div>
      ),
      okText: "确认清除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await sectionsApi.saveOutline(id, { outline: [], source: "手动清除" });
          msg.success("所有目录已清除");
          notifyClearedContent(data, "清空目录同时删除了：");
          notifyGenericResidual(data);
          setTree([]);
          setSelectedSection(null);
          setExpandedKeys([]);
          await load();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "清除失败");
        }
      },
    });
  }

  /** 从上传文件识别目录 */
  const handleUploadParseOutline = async (file: File) => {
    // ✅ BUG 修复：上传识别结果会直接替换本地目录树（尚未保存也会丢）。
    // 若当前树中已有生成正文的章节，误操作会让用户在调整后保存时整组重建、
    // 溯源/正文归属被重置。这里先要求显式确认。
    const chaptersWithContent = treeNodesWithContent(tree);
    if (tree.length > 0) {
      const confirmed = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: "用识别结果替换当前目录？",
          content: chaptersWithContent > 0 ? (
            <div>
              当前目录中有 <b style={{ color: "#ff4d4f" }}>{chaptersWithContent}</b> 个章节已生成正文。
              替换后保存目录可能影响这些章节的结构归属，正文本身不会立即删除，
              但建议先确认已保存或导出。是否继续？
            </div>
          ) : "当前未保存的目录调整将被识别结果替换，是否继续？",
          okText: "继续替换",
          cancelText: "取消",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!confirmed) return;
    }
    const hide = msg.loading("正在解析文件并识别目录...", 0);
    try {
      // ✅ 补齐调用链路：把「整理为标准结构」开关与方案名透传后端
      //    （后端 match_template / reorganize_to_standard 依赖 scheme_name）
      // ✅ 链路修复（2026-10-03）：补传 project_id，使 uploaded_outlines 记录
      //    建立项目归属链（删项目时才能级联清理；无项目上下文时为空、行为不变）
      const { data } = await uploadOutlineApi.parse(file, {
        scheme_name: scheme?.name || "",
        reorganize: outlineImportReorganize,
        project_id: scheme?.project_id || "",
      });
      // ✅ 多标段提示（对齐 OpenBidKit bidSectionDetector）
      if (data.multi_section_hint?.has_multiple) {
        setMultiSectionHint(data.multi_section_hint);
      } else {
        setMultiSectionHint(null);
      }
      // ✅ 结果诊断收口（纯函数）：空结果 / 截断 / 归位统计一律如实告知
      const summary = summarizeUploadOutlineResult(data);
      if (!summary.ok) {
        // 识别失败/空目录：绝不清空用户当前目录树，也不覆盖「存为目录库」来源
        msg.warning(summary.message);
        summary.notices.forEach((n) => msg.info(n));
        return;
      }
      summary.notices.forEach((n) => msg.info(n));
      msg.success(summary.message);
      const outline = summary.outline;
      // 记录 upload 记录，供「存为目录库」使用
      setLastUpload({ id: data.id, name: data.file_name, outline });
      // 构造本地临时节点替换树
      const newTree = outlineToTreeNode(outline);
      setTree(newTree);
      setSelectedSection(null);
      setExpandedKeys(collectAllKeys(newTree));
      setActiveTab("outline");
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "文件识别失败");
    } finally {
      hide();
    }
  };

  // （outlineToTreeNode 已提升至模块级导出，行为由 outlineTab 测试钉住）

  // （renumberTreeLocally 已提升至模块级导出）

  /** 目录树拖拽排序 */
  function onOutlineTreeDrop(info: Parameters<NonNullable<TreeProps["onDrop"]>>[0]) {
    // 本页树节点 key 一律为章节主键 string（antd Tree 的 Key 联合在此收窄）
    const dropKey = info.node.key as string;
    const dragKey = info.dragNode.key as string;
    const dropPos = info.node.pos.split("-");
    const dropPosition = info.dropPosition - Number(dropPos[dropPos.length - 1]); // -1 到 +1

    // ✅ BUG 修复：禁止拖入自身或自身子孙（数据成环后递归遍历/保存栈溢出）。
    const findByKey = (nodes: TreeNode[], key: string): TreeNode | null => {
      for (const n of nodes) {
        if (n.key === key) return n;
        if (n.children?.length) {
          const hit = findByKey(n.children, key);
          if (hit) return hit;
        }
      }
      return null;
    };
    const containsKey = (root: TreeNode | null, key: string): boolean => {
      if (!root) return false;
      if (root.key === key) return true;
      return (root.children || []).some((c) => containsKey(c, key));
    };
    const dragNode = findByKey(tree, dragKey);
    if (!dragNode || dragKey === dropKey || containsKey(dragNode, dropKey)) {
      msg.warning("不能将章节移动到自身或其子章节内");
      return;
    }

    const loop = (data: TreeNode[], key: string, callback: (node: TreeNode, i: number, arr: TreeNode[]) => void) => {
      for (let i = 0; i < data.length; i++) {
        if (data[i].key === key) {
          return callback(data[i], i, data);
        }
        if (data[i].children) {
          loop(data[i].children!, key, callback);
        }
      }
    };

    // ✅ BUG 修复：旧实现仅浅拷贝根数组，splice/unshift 直接突变 state 的
    //    嵌套数组与节点对象。深拷贝后再操作，保证 React 状态不可变。
    const data: TreeNode[] = JSON.parse(JSON.stringify(tree));
    let dragObj: TreeNode | undefined;
    loop(data, dragKey, (item, index, arr) => {
      arr.splice(index, 1);
      dragObj = item;
    });
    if (!dragObj) return;

    if (!info.dropToGap && dropPosition === 0) {
      // 作为子节点
      loop(data, dropKey, (item) => {
        item.children = item.children || [];
        item.children!.unshift(dragObj!);
      });
    } else {
      // 兄弟节点位置
      let ar: TreeNode[] = [];
      let i = 0;
      loop(data, dropKey, (_item, index, arr) => {
        ar = arr;
        i = index;
      });
      if (dropPosition === -1) {
        ar.splice(i, 0, dragObj!);
      } else {
        ar.splice(i + 1, 0, dragObj!);
      }
    }
    // ✅ BUG 修复：拖拽可能把节点拖成四级及更深（如拖到三级章节之下）。旧实现
    //    仅在本地接受，点「保存目录」时后端 normalize_outline 会静默裁剪并把深层
    //    标题并入父章节描述——用户"保存成功"后节点却消失。这里与「新增子章节」
    //    入口保持同一口径，在拖拽落地前拦截。
    if (outlineTreeDepth(data) > MAX_OUTLINE_DEPTH) {
      msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，已取消本次拖拽（请调整目标层级后重试）`);
      return;
    }
    // ✅ 编辑增强：拖拽后本地按位置重算编号，让"编号已更新"实时可见，
    //    并弹出高亮提示（落库仍由「保存目录」统一执行 normalize_outline）。
    setTree(renumberTreeLocally(data));
    flashReorderHint();
  }

  // ------ 目录 Tab 中的 Tree 渲染（支持编辑、增删） ------
  // R-P1 拆分：renamingKey / renamingValue 已上移至 useOutlineTreeEdit 调用点之前。

  // ✅ 编辑增强：拖拽/上移下移后实时高亮"编号已更新"提示
  const [reorderHint, setReorderHint] = useState<string | null>(null);
  const reorderHintTimer = useRef<number | null>(null);
  // ✅ 编辑增强：编辑树 / 左侧导航树滚动容器 ref，用于移动后滚动定位到目标节点
  function flashReorderHint(msg = "目录顺序已调整，编号已实时更新（记得点击「保存目录」同步到服务端）") {
    setReorderHint(msg);
    if (reorderHintTimer.current) window.clearTimeout(reorderHintTimer.current);
    reorderHintTimer.current = window.setTimeout(() => setReorderHint(null), 3200);
  }

  // 预计算每个节点是否可上移/下移（同级首/尾禁用），供目录树行内按钮使用
  const moveFlags = useMemo(() => {
    const flags: Record<string, { up: boolean; down: boolean }> = {};
    const scan = (ns: TreeNode[], _parent: TreeNode[] | null) => {
      ns.forEach((n, i) => {
        flags[n.key] = { up: i > 0, down: i < ns.length - 1 };
        if (n.children?.length) scan(n.children, ns);
      });
    };
    scan(tree, null);
    return flags;
  }, [tree]);

  // ✅ 性能优化：改为 useCallback —— 目录树 300 个节点时，每次整页渲染都会
  //    重建 ~1200 个 JSX 元素（每节点 Tooltip/Input/图标 + 3 个内联闭包）+
  //    每节点 O(失败章数) 子串匹配。配合下方 outlineEditTreeData 的 useMemo，
  //    无关 state（progress / sectionLogs / genStats …）变化时整棵编辑树不再重算。
  // ⚠️ 必须定义在 renameNode 与 moveFlags 之后：deps 数组是立即求值，前向引用会 TDZ。
  const convertToEditTree = useCallback((node: TreeNode): TreeDataNode => {
    const isGenerated = node.status === "generated" || node.word_count > 0;
    const isGenerating = node.status === "running" || node.status === "pending";
    const isFailed = node.status === "failed";
    // 长方案分步目录生成时子目录生成失败的章节（橙色警告，区别于正文生成失败的红色）
    const isOutlineFailed = failedOutlineChapters.some((t) => node.title.includes(t) || t.includes(node.title));

    const statusDot = isGenerated
      ? <CheckCircleOutlined style={{ color: "#52c41a", fontSize: 12 }} />
      : isFailed
        ? <CloseCircleOutlined style={{ color: "#ff4d4f", fontSize: 12 }} />
        : isOutlineFailed
          ? <WarningOutlined style={{ color: "#fa8c16", fontSize: 12 }} />
          : isGenerating
            ? <LoadingOutlined style={{ color: "#1677ff", fontSize: 12 }} />
            : <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "#d9d9d9" }} />;

    const rowBg = isGenerated
      ? "rgba(82,196,26,0.04)"
      : isFailed
        ? "rgba(255,77,79,0.04)"
        : isOutlineFailed
          ? "rgba(250,140,22,0.06)"
          : isGenerating
            ? "rgba(22,119,255,0.04)"
            : "transparent";

    return {
      key: node.key,
      title: (
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            width: "100%",
            padding: "2px 8px",
            margin: "0 -8px",
            borderRadius: 4,
            background: rowBg,
          }}
          onDoubleClick={() => {
            setRenamingKey(node.key);
            setRenamingValue(node.title);
          }}
        >
          {renamingKey === node.key ? (
            <Input
              size="small"
              autoFocus
              value={renamingValue}
              onChange={(e) => setRenamingValue(e.target.value)}
              onBlur={() => {
                renameNode(node.key, renamingValue);
                setRenamingKey(null);
              }}
              onPressEnter={() => {
                renameNode(node.key, renamingValue);
                setRenamingKey(null);
              }}
              style={{ width: 220 }}
            />
          ) : (
            <>
            {statusDot}
            <span style={{
              fontWeight: node.level === 1 ? 600 : 400,
              color: isGenerated ? "#389e0d" : isFailed ? "#cf1322" : isOutlineFailed ? "#d46b08" : isGenerating ? "#0958d9" : undefined,
            }}>
              {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
            </span>
              <span style={{ fontSize: 11, color: "#999" }}>
                (L{node.level} · {node.word_budget}字)
              </span>
              {node.word_count > 0 && !node.children?.length && (
                <Tag
                  color={isGenerated ? "success" : isFailed ? "error" : "processing"}
                  style={{ margin: "0 0 0 8px", fontSize: 11, lineHeight: "16px" }}
                >
                  {node.word_count}字
                </Tag>
              )}
              {/* ✅ 2026-09-29（事实变更失效标记）：正文写于全局事实变更之前。
                  只提示不阻断 —— 后端不会自动重写用户已编辑的章节。 */}
              {node.facts_stale && (
                <Tooltip title="全局事实已变更，本章正文引用的参数可能已过期，建议重新生成">
                  <Tag
                    color="warning"
                    icon={<ExclamationCircleOutlined />}
                    style={{ margin: "0 0 0 8px", fontSize: 11, lineHeight: "16px", cursor: "help" }}
                  >
                    事实已变更
                  </Tag>
                </Tooltip>
              )}
              <span
                style={{
                  marginLeft: "auto",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 2,
                  flexShrink: 0,
                }}
              >
                <Button
                  type="text"
                  size="small"
                  tabIndex={-1}
                  disabled={!moveFlags[node.key]?.up}
                  icon={<ArrowUpOutlined />}
                  title="上移"
                  onClick={(e) => {
                    e.stopPropagation();
                    moveNode(node.key, -1);
                  }}
                />
                <Button
                  type="text"
                  size="small"
                  tabIndex={-1}
                  disabled={!moveFlags[node.key]?.down}
                  icon={<ArrowDownOutlined />}
                  title="下移"
                  onClick={(e) => {
                    e.stopPropagation();
                    moveNode(node.key, 1);
                  }}
                />
              </span>
            </>
          )}
        </div>
      ),
      children: node.children?.map((c) => convertToEditTree(c)),
    };
  }, [moveFlags, moveNode, renameNode, failedOutlineChapters, renamingKey, renamingValue]);

  // ✅ 性能优化：编辑树数据 useMemo 化。旧实现在组件顶层无条件 `tree.map(...)` ——
  //    300 节点 = 每次整页渲染都重建全量树元素，且与 outline Tab 是否激活无关
  //    （该语句在函数体中，会拖慢正文/审核/导出等所有 Tab 的渲染）。
  //    现在仅在树结构或影响节点渲染的状态（可移动性 / 失败章 / 重命名节点 /
  //    重命名输入值）变化时才重算。deps 用 convertToEditTree 而非逐项展开，
  //    其自身的 useCallback deps 已精确覆盖全部输入。
  const outlineEditTreeData = useMemo(
    () => tree.map((n) => convertToEditTree(n)),
    [tree, convertToEditTree],
  );

  // ✅ R106 FIX-4：任务不再运行（SSE stopped / 断线轮询接管 / 异常收尾）时复位
  //    「停止中」态与控制锁。放在这里统一兜底，避免在十几个 stopped 分支各埋一次。
  useEffect(() => {
    if (!generating) {
      setTaskStopping(false);
      controllingRef.current = false;
    }
  }, [generating]);

  const handleControl = async (action: "pause" | "resume" | "stop") => {
    const tid = activeTaskIdRef.current;
    if (!tid) {
      // ✅ R106 FIX-8：区分「刚发起、task_id 还没回」与「真没有任务」。
      //    发起生成时已主动清空 ref（配合后端 FIX-7 把 connecting 前移到装配之前，
      //    窗口从实测 30s 压到 ~1s），此窗口的停止必须如实告知"稍候"，
      //    绝不能把指令发给上一代已终结的 id —— 那正是活体 E2E 里
      //    「点了停止、后端回任务不存在、新任务照跑」的机理。
      if (generating) {
        msg.info("任务正在建立连接，尚未取得任务标识，请稍等 1~2 秒后再点");
        return;
      }
      msg.warning("没有活跃的任务");
      return;
    }
    // ✅ R106 FIX-4：同步锁防连点（同一条指令未回执/未落终态前不再重复发）
    if (controllingRef.current) return;
    controllingRef.current = true;
    if (action === "stop") setTaskStopping(true);
    try {
      const { data } = await tasksApi.control(tid, action);
      if (data.ok) {
        // ✅ F3 暂停状态可见：控制成功后同步本地 paused 态（按钮互斥 + Tag）
        if (action === "pause") { setTaskPaused(true); msg.success("已暂停"); }
        else if (action === "resume") { setTaskPaused(false); setTaskStopping(false); msg.success("已恢复"); }
        else {
          // ✅ R106 FIX-4：停止是协作式的，指令 accepted ≠ 已停止 —— 这里只确认
          //    「指令已送达」，真正的「已停止」由 SSE stopped 事件 / 轮询终态提示，
          //    旧实现在此处直接 msg.success("已停止") 是假回执。
          setTaskPaused(false);
          if (data.orphaned) {
            // 后端僵尸兜底：内存已无运行实例（多为外部进程残留），已直接落终态
            setTaskStopping(false);
            controllingRef.current = false;
            msg.info(data.message || "任务已无运行实例，已标记为已停止");
          } else {
            msg.info("停止指令已发送，任务将在当前步骤边界退出");
          }
        }
      } else {
        // ✅ R106 FIX-4：失败不再静默（旧实现 data.ok=false 时什么都不提示，
        //    按钮却已复位，用户以为停止成功）
        setTaskStopping(false);
        controllingRef.current = false;
        msg.error(data.message || "控制指令未被接受，任务可能已结束");
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      setTaskStopping(false);
      controllingRef.current = false;
      msg.error(err.message || "控制失败");
    } finally {
      // stop 的成功路径由 generating 复位 effect 释放锁（终态才算结束）；
      // 失败/orphaned 路径已在各自分支内立即释放。
      if (action !== "stop") controllingRef.current = false;
    }
  };

  // ===== ✅ R106 FIX-5：审核与预检 AI 调用的取消入口 =====
  // 这些端点是同步 await REST（不注册 task_registry）：客户端 abort 后，服务端在
  // 下一个 await 边界收到 CancelledError 即中断在飞 AI 调用（Starlette 断连取消），
  // 无后台残留 —— 所以前端的「取消」对它们就是真停止，不需要轮询终态。
  const checkAbortRef = useRef<AbortController | null>(null);
  const auditAbortRef = useRef<AbortController | null>(null);
  const expertAbortRef = useRef<AbortController | null>(null);
  const [expertRunning, setExpertRunning] = useState(false);

  // R-P1 拆分：专家论证 / 合规检查 / 一致性审计（含 auditRunning、consistencyResult
  // 两个 useState）/ CR 修复工作台处理器迁至 useReviewAudit（调用点在原位置）。
  const {
    handleExpertReview, handleCancelExpertReview,
    handleComplianceCheck, handleCancelComplianceCheck,
    auditRunning, consistencyResult, setConsistencyResult,
    handleConsistencyAudit, handleCancelConsistencyAudit,
    loadCrConflicts, loadCrHistory, openConsistencyWorkbench,
    handleCrScan, handleCancelCrScan, handleCrRepair, handleCancelCrRepair,
    crRepairItemIds, handleCrConfirm, handleCrRollback,
  } = useReviewAudit({
    id, msg, modal,
    expertAbortRef, expertRunning, setExpertRunning, setExpertResult,
    checkAbortRef, setCheckingCompliance, checklistText, checklistDirty,
    aiRules, setComplianceResults, loadComplianceHistory,
    auditAbortRef,
    consistencySeverity, forceFullRepair,
    crScanning, setCrScanning, crRepairing, setCrRepairing, crScanId,
    setCrRepair, crRepair, setCrConflicts, setCrSummary, setCrScanId,
    setCrHistory, setCrOpen, setCrLoading,
    selectedSectionRef, setSelectedSection, load, setReviewTick,
  });

  const loadConsistencyLatest = async () => {
    if (!id) return;
    try {
      const { data } = await complianceApi.consistencyLatest(id);
      if (data?.exists) {
        setConsistencyResult({ score: data.score, issues: data.issues || [], created_at: data.created_at });
      }
    } catch {
      // 历史结果加载失败不阻塞页面
    }
  };

  // ✅ 修复（2026-09-20 深度审查）：导出入口重入保护。
  //    loading={exporting} 只能在重渲染后禁用按钮，React 状态更新异步批处理，
  //    同一帧内的双击 / 连点（包括 DOCX 与 PDF 两个按钮互点）仍会并发进入导出流程。
  //    用 ref 同步标志做函数级防护，确保任何时刻只有一个导出在进行。
  const exportingRef = useRef(false);
  // ✅ 导出图表渲染进度：上一次已刷新的整数百分比（同百分比内不再排队刷新）
  const exportPctRef = useRef(-1);
  const handleExport = async (format: "docx" | "pdf" = "docx") => {
    if (!id || exportingRef.current) return;
    const sid = id;
    const controller = new AbortController();
    const exportSeq = ++exportSeqRef.current;
    exportAbortRef.current?.abort();
    exportAbortRef.current = controller;
    exportingRef.current = true;
    exportPctRef.current = -1;
    setExporting(true);
    setExportStage("charts");
    setExportPct(0);
    setExportPhase("正在渲染图表...");
    try {
      // ✅ P0 修复（2026-10-06 · 折叠面板配置被静默丢弃）：`validateFields()` **只返回
      //    当前已挂载的表单项**。导出配置表单的「封面与页面」面板默认折叠
      //    （defaultActiveKey=["basic","body","headings"]），其中的 12 个配置项
      //    —— margins(4) / cover_info(4) / scheme_forms(4)，即**页边距、封面项目
      //    信息表、专项施工方案四张法定前置表单**—— 未挂载即不进返回值。
      //    实测（schemeWorkbenchExportTab20261006）请求体里 `config.scheme_forms`
      //    恒为 undefined：用户把表单开关打开过、面板收起后再导出，开关被无声
      //    丢弃，后端回落自己的默认值 → "界面拨了开关、导出毫无变化"。
      //    修法：`getFieldsValue(true)` 取整份 store（含未挂载项，antd 的
      //    `preserve` 默认 true 保证折叠不丢值），与校验结果**深合并**，
      //    既保留 validateFields 的校验语义，又不丢折叠面板里的配置。
      //    ⚠️ 后端 config_hash 会随之变化（原本缺键、现在带默认值）→ 既有导出
      //    缓存一次性失效，属"让真实设置首次生效"的必要代价。
      const validated = await exportForm.validateFields();
      const config = deepMergeExportConfig(exportForm.getFieldsValue(true), validated);
      // ✅ 统一渲染轨：用与预览一致的 mermaid.js 把图表渲染成 PNG 随导出提交，
      // 后端优先采用（未命中/失败的图表由后端渲染轨兜底），导出与预览所见即所得
      let chartImages: ExportChartImage[] = [];
      try {
        const { data } = await chartsApi.list(sid, controller.signal);
        if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
        const items: ExportChartItem[] = selectPlacedExportCharts(data.items || [])
          .map((p) => ({ chart_type: p.chart_type, mermaid_code: p.code! }));
        chartImages = await renderChartsForExport(items, (doneCount, totalCount) => {
          // ✅ 大方案图表渲染可达数十秒，旧实现只显示静态"正在渲染图表..."，
          // 用户无法判断是否卡死。透传工具的逐图进度。
          // ✅ 性能优化：180 张图 × 并发 4 = 高频回调，旧实现每图一次 setExportPhase
          // → 每次触发 9000+ 行整页重渲（连带 outlineEditTreeData / 7 张 Table 重建）。
          // 按「帧内只记最新值 + rAF 合帧 + 百分比去重」压到 ≤1 次/帧。
          if (totalCount > 0) {
            const pct = Math.floor((doneCount / totalCount) * 100);
            const text = `正在渲染图表...（${doneCount}/${totalCount}）`;
            if (pct === exportPctRef.current) return;
exportPctRef.current = pct;
          // ✅ D2：图表阶段占总量 0~70%（文档生成可能更耗时）
          exportBatcherRef.current?.schedule("phase", () => {
            setExportPhase(text);
            setExportPct(Math.round(pct * 0.7));
          });
          }
        }, controller.signal);
        if (chartImages.length > 0) {
          msg.info(`已按预览样式渲染 ${chartImages.length}/${items.length} 张图表`, 3);
        }
      } catch (e) {
        if (controller.signal.aborted) throw e;
        chartImages = []; // 前端渲染失败不阻塞导出，图表走后端渲染轨兜底
        // ✅ R-4（2026-10-04）：此前静默吞错，用户无从判断"图表渲染轨未启用"。
        //    现在至少给一条 info 提示，便于用户排查图表缺失原因。
        msg.info("图表前端渲染轨未启用（网络/浏览器异常），图表将由后端兜底渲染", 4);
      }
      // ✅ 收尾前把图表渲染进度的最后一帧刷下去，保证用户看到最终进度文本
      exportBatcherRef.current?.flushNow();
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      // ✅ D2：文档生成阶段显式置位；PDF 走「DOCX(70%) → 转换(→100%)」两段
      setExportStage(format === "pdf" ? "pdf" : "docx");
      setExportPct(70);
      setExportPhase(format === "pdf" ? "正在生成文档并转换PDF..." : "正在生成文档...");
      const resp = format === "pdf"
        ? await exportApi.pdf(sid, config, chartImages, controller.signal)
        : await exportApi.docx(sid, config, chartImages, controller.signal);
      if (
        controller.signal.aborted
        || exportSeqRef.current !== exportSeq
        || currentSchemeIdRef.current !== sid
      ) return;
      const mime = format === "pdf"
        ? "application/pdf"
        : "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      const blob = new Blob([resp.data], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      // ✅ 导出文件名统一走后端规则「专项方案名称 + 导出日期 + 导出轮次」：
      //    blob 下载时 a.download 会覆盖响应头 Content-Disposition，故必须显式取用
      //    后端回传的 X-Export-Filename（百分号编码，解码还原）。
      //    T3：解析逻辑提取为纯函数 resolveExportFilename（可单测），头缺失/解码
      //    异常时沿用兜底名，不阻断下载（与原行为一致）。
      const downloadName = resolveExportFilename(
        resp.headers, `${scheme?.name || "方案"}.${format}`);
      a.download = downloadName;
      // ✅ 修复：① Firefox 要求 <a> 挂在文档中才会触发下载；
      //          ② click() 后同步 revokeObjectURL 会撤销尚未开始的下载
      //            （Safari / Firefox 上表现为"点了没反应"），改为延迟回收。
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // ✅ R-5（2026-10-04）：延迟从 2s 提到 10s。
      //    大文件（数十 MB 的方案 PDF/DOCX）在弱网 / 移动热点 / 云盘同步下，
      //    浏览器下载器可能超过 2s 才真正从 blob 读取字节。此时如果 URL 已被
      //    revoke，Safari / Firefox 会静默失败（点按钮没反应）。10s 兼顾
      //    兼容性（不永久占内存）与稳定性。
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      msg.success(format === "pdf" ? "PDF 导出成功" : "导出成功");
      if (currentSchemeIdRef.current === sid) await loadCacheStatus();
      if (
        controller.signal.aborted
        || exportSeqRef.current !== exportSeq
        || currentSchemeIdRef.current !== sid
      ) return;
      // 展示图表渲染轨统计（前端 mermaid.js / 后端 mermaid 原生 / 失败）
      // T3：头部解析与文案拼装提取为纯函数（parseChartRenderStats / chartRenderStatsParts）
      const stats = parseChartRenderStats(resp.headers);
      if (stats) {
        const parts = chartRenderStatsParts(stats);
        if (parts.length) msg.info(`图表渲染：${parts.join(" / ")}`, 5);
        if (stats.failed && stats.failed > 0) {
          msg.warning(`${stats.failed} 张图表渲染失败，建议检查图表代码或开启「允许 PIL 兜底」后重试`);
        }
        // ✅ D1 加固（2026-10-06）：跨章借图此前零信号 —— 成稿里 B 章可能出现
        //    A 章的流程图而用户毫不知情。此处按 warning 单独提示（不阻断导出），
        //    并指向"回查该章节的图表登记"这一可执行动作。
        const fbWarn = fallbackBorrowWarning(stats);
        if (fbWarn) msg.warning(fbWarn, 8);
      }
      // 展示公式/乱码自动修复统计（导出时后端已内建修复：公式转 Word 数学排版、清理乱码符号）
      // T3：头部解析与文案拼装提取为纯函数（parseFixStats / fixStatsParts）
      const fs = parseFixStats(resp.headers);
      if (fs) {
        const parts = fixStatsParts(fs);
        if (parts.length) msg.success(`导出前已自动修复：${parts.join(" / ")}`, 6);
      }
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      const canceled = err.name === "AbortError" || err.code === "ERR_CANCELED";
      if (!canceled && exportSeqRef.current === exportSeq && currentSchemeIdRef.current === sid) {
        msg.error(err.message || "导出失败");
      }
    } finally {
      if (exportSeqRef.current === exportSeq && exportAbortRef.current === controller) {
        exportAbortRef.current = null;
        exportingRef.current = false;
        setExporting(false);
        setExportPhase("");
        // ✅ D2：阶段与进度随导出一并回 0（否则下次导出会残留上一次的进度条）
        setExportStage("idle");
        setExportPct(0);
      }
    }
  };

  /**
   * ✅ D2 加固（2026-10-06）：取消导出。
   *
   * 此前导出只能被「意外」中断（切换方案 / 卸载页面 / 发起一次新导出覆盖上一次），
   * 用户盯着「正在生成文档...」无法主动退出。`exportAbortRef` 里的
   * AbortController 本就存在，这里只补一个入口：abort → 前端 catch 识别
   * AbortError/ERR_CANCELED → 不弹错误提示 → finally 释放同步锁。
   * ✅ R106 FIX-3：此前「取消导出」只 abort 前端 fetch —— 后端的
   * `asyncio.to_thread(_build_docx_sync)` 线程不可取消，会继续跑完（AI 配图 /
   * 图表兜底渲染白烧）。现先 POST /export/cancel 置位取消事件，构建线程与渲染段
   * 在下一段边界协作式退出（不落地产物、不写缓存），再 abort 前端 fetch。
   * cancel 为 fire-and-forget：后端不可达时降级为纯 abort（与原行为一致）。
   */
  const handleCancelExport = () => {
    if (!exportingRef.current) return;
    if (id) exportApi.cancel(id).catch(() => {});
    exportAbortRef.current?.abort();
    msg.info("已发送停止指令，服务端将在当前步骤边界退出");
  };

  const handleExportCheck = async () => {
    // ✅ R-3（2026-10-04）：同步锁，与 handleExport 的 exportingRef 同型。
    //    React 状态更新异步批处理，仅靠 exportCheckAbortRef 中止旧请求不够——
    //    快速双击会在同一帧内并发进入，两次 await 交错 setState 造成 UI 状态撕裂。
    if (!id || checkingExportRef.current) return;
    const sid = id;
    checkingExportRef.current = true;
    exportCheckAbortRef.current?.abort();
    const controller = new AbortController();
    exportCheckAbortRef.current = controller;
    setCheckingExport(true);
    // 新一轮预检开始即撤销旧结论；失败时保持阻断，避免旧方案的放行状态继续可用。
    setExportIssues([]);
    setExportStats(null);
    setExportPreflight(null);
    setPlaceholderReport(null);
    setRerunPlan(null);
    try {
      const { data } = await exportApi.check(sid, controller.signal);
      if (
        controller.signal.aborted
        || currentSchemeIdRef.current !== sid
        || exportCheckAbortRef.current !== controller
      ) return;
      setExportIssues(data.issues || []);
      setExportStats({
        section_count: data.section_count,
        generated_count: data.generated_count,
        total_words: data.total_words,
        empty_ratio: data.empty_ratio,
        chart_total: data.chart_total,
        chart_done: data.chart_done,
      });
      // ✅ G1：导出预检反向打通就绪度总检 —— 同一份数据、同一套规则词表，
      // 用户不用在两个页面之间来回比对「到底能不能交付」
      // 后端 SSE 契约：export/check 响应的 preflight_summary（ExportPreflight + has_run）
      const ps = (data.preflight_summary || null) as ExportPreflightSummary | null;
      setExportPreflight(ps && ps.has_run ? ps : null);
      // ✅ 《待补充清单》（2026-09-24）：预检一并返回占位符聚合，供面板展示
      setPlaceholderReport(data.placeholder_report || null);
      // ✅ 重跑计划：对照当前数据源判定哪些章节可自动重跑（失败静默为无计划）
      try {
        const { data: plan } = await exportApi.placeholderRerunPlan(sid, controller.signal);
        if (
          !controller.signal.aborted
          && currentSchemeIdRef.current === sid
          && exportCheckAbortRef.current === controller
        ) setRerunPlan(plan || null);
      } catch { /* 非致命：重跑计划失败不影响预检结论 */ }
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      if (ps && ps.has_run) {
        if (ps.stale) {
          msg.warning(`就绪度总检结论已过期（${ps.total ?? 0} 分 / ${ps.grade} 级），正文变更后建议重新总检`);
        } else if (!ps.released) {
          msg.warning(`导出预检之外，就绪度总检也不建议放行：${ps.total ?? 0} 分 / ${ps.grade} 级（${ps.verdict || "未达放行线"}）`);
        }
      }
      if (!data.issues?.length) {
        msg.success(`预检通过：${data.section_count} 个章节均无异常`);
      } else {
        msg.warning(`发现 ${data.issues.length} 个问题，建议处理后再导出`);
      }
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      const canceled = err.name === "AbortError" || err.code === "ERR_CANCELED";
      if (!canceled && currentSchemeIdRef.current === sid) msg.error(err.message || "预检失败");
    } finally {
      if (exportCheckAbortRef.current === controller) {
        exportCheckAbortRef.current = null;
        setCheckingExport(false);
      }
      // R-3：无论成功/失败/取消都释放同步锁，避免死锁
      checkingExportRef.current = false;
    }
  };

  /**
   * ✅ 接线 sectionsApi.quality：正文质量自检（交付前最后一道关）。
   * 与同页另两项预检互补，三者覆盖三个不同层次：
   *   - 导出预检：结构层 —— 章节完整性 / 字数达标度 / 图表完成率
   *   - 规范符合性检查：内容层 —— 是否覆盖规范条目（AI 逐条判定）
   *   - 质量自检：文字层 —— 口语化/AI 腔残留、引用了已废止或被替代的标准编号
   */
  const handleQualityCheck = async () => {
    // ✅ R168（FIX-10）：重入锁 —— 同帧双击此前可并发两个请求（checkingQuality 是
    //    异步置位，挡不住连点），结果相互覆盖；与同页其它预检入口的 running 态口径一致。
    if (!id || checkingQuality) return;
    setCheckingQuality(true);
    try {
      const { data } = await sectionsApi.quality(id);
      setQualityResult(data);
      const s = data.summary || {};
      if (!s.problem_sections) {
        msg.success(`质量自检通过：已扫描 ${s.checked ?? 0} 个章节，无口语化残留、无废止标准`);
      } else {
        msg.warning(`${s.problem_sections} 个章节存在质量问题，详见清单`);
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "质量自检失败");
    } finally {
      setCheckingQuality(false);
    }
  };

  // ✅ 性能优化：与 convertToEditTree 同款 useCallback 化，并修正下方 treeData 的
  //    陈旧闭包问题 —— 旧 deps 只有 [tree]，但闭包内读了 failedOutlineChapters，
  //    导致「失败章高亮」在只改 failedOutlineChapters 时不刷新（当前被「setTree 与
  //    setFailedOutlineChapters 总同批次提交」掩盖，新增单点改动路径就会暴露）。
  const convertToAntTree = useCallback((node: TreeNode, depth = 0): TreeDataNode => {
    const isGenerated = node.status === "generated" || node.word_count > 0;
    const isGenerating = node.status === "running" || node.status === "pending";
    const isFailed = node.status === "failed";
    // ✅ 统一左右两树状态：目录生成失败（长方案分步生成）在左侧树同样橙色高亮
    const isOutlineFailed = failedOutlineChapters.some((t) => node.title.includes(t) || t.includes(node.title));

    // 状态样式
    const statusIcon = isGenerated
      ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
      : isFailed
        ? <CloseCircleOutlined style={{ color: "#ff4d4f" }} />
        : isOutlineFailed
          ? <WarningOutlined style={{ color: "#fa8c16" }} />
          : isGenerating
            ? <LoadingOutlined style={{ color: "#1677ff" }} />
            : <span style={{ color: "#d9d9d9" }}>○</span>;

    const rowBg = isGenerated
      ? "rgba(82,196,26,0.06)"
      : isFailed
        ? "rgba(255,77,79,0.06)"
        : isOutlineFailed
          ? "rgba(250,140,22,0.06)"
          : isGenerating
            ? "rgba(22,119,255,0.06)"
            : "transparent";

    const wordTagColor = isGenerated
      ? "success"
      : isFailed
        ? "error"
        : isGenerating
          ? "processing"
          : "default";

    return {
      key: node.key,
      title: (
        <Tooltip
          title={
            <div style={{ fontSize: 12 }}>
              <div>层级：L{node.level}</div>
              <div>状态：{isGenerated ? "已生成" : isFailed ? "生成失败" : isGenerating ? "生成中" : "未生成"}</div>
              <div>字数：{node.word_count}/{node.word_budget}</div>
            </div>
          }
          placement="right"
        >
          <span style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            width: "100%",
            padding: "2px 6px",
            margin: "0 -6px",
            borderRadius: 4,
            background: rowBg,
          }}>
            {statusIcon}
            {node.level > 1 && <span style={{ color: "#bfbfbf" }}>·</span>}
            <span style={{
              fontWeight: node.level === 1 ? 600 : 400,
              color: isGenerated ? "#389e0d" : isFailed ? "#cf1322" : isOutlineFailed ? "#d46b08" : isGenerating ? "#0958d9" : undefined,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              maxWidth: 180,
            }}>
              {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
            </span>
            {node.word_count > 0 && !node.children?.length && (
              <Tag color={wordTagColor} style={{ margin: "0 0 0 auto", fontSize: 11, lineHeight: "16px" }}>
                {node.word_count}字
              </Tag>
            )}
          </span>
        </Tooltip>
      ),
      children: node.children?.map((c) => convertToAntTree(c, depth + 1)),
    };
  }, [failedOutlineChapters]);

  const treeData = useMemo(() => tree.map((n) => convertToAntTree(n)), [tree, convertToAntTree]);

  // ✅ P0-5：树选择/展开逻辑提取为稳定回调（配合 SchemeTreePanel memo 隔离重渲）
  // ✅ 树选择：仅选中章节，不再强制跳转「正文生成」Tab。
  //    旧实现 setActiveTab("content") 会让用户在「全局事实 / 目录生成」等页操作时
  //    误点左侧树就被拽回正文页，打断当前工作流——这是导航混乱的核心来源之一。
  //    选中后若用户当前就在正文页，内容区自然跟随更新；在其他页则保持当前步骤。
  const handleTreeSelect = useCallback((key: React.Key) => {
    let sec = findSectionById(tree, key as string);
    if (sec) {
      // 父节点（无 content）→ 自动跳第一个有内容的后代
      if (!sec.content && sec.children && sec.children.length > 0) {
        const first = findFirstContentDescendant(sec);
        if (first) {
          sec = first;
          setExpandedKeys((prev) => Array.from(new Set([...prev, first.key])));
        }
      }
      setSelectedSection(sec);
    }
  }, [tree]);

  const handleTreeExpand = useCallback((keys: React.Key[]) => setExpandedKeys(keys), []);

  /** ✅ 《待补充清单》跳转（2026-09-24）：切到「正文生成」并选中目标章节。
   *  展开其祖先链保证树中可见；章节已被删除时如实提示（避免「点了没反应」）。
   *  必须定义在 handleTreeSelect 之后（useCallback deps 数组是立即求值，前向引用会 TS2448）。 */
  const handlePlaceholderJump = useCallback((sectionId: string) => {
    const sec = findSectionById(tree, sectionId);
    if (!sec) {
      msg.warning("章节不存在或已被删除，请重新预检刷新清单");
      return;
    }
    const keysToExpand: string[] = [sectionId];
    let cur = sectionId;
    for (let i = 0; i < 20; i++) {   // 祖先链有界回溯（防御环状脏数据）
      const p = findParentKey(tree, cur);
      if (!p) break;
      keysToExpand.push(p);
      cur = p;
    }
    setExpandedKeys((prev) => Array.from(new Set([...prev, ...keysToExpand])));
    handleTreeSelect(sectionId);
    setActiveTab("content");
    msg.info(`已定位到「${sec.title}」`);
  }, [tree, handleTreeSelect]);

  /** ✅ 重跑受影响章节（2026-09-24，治 F 层第 3 条）：补录后只重跑受影响章节。
   *  逐章 mode=section + force_rewrite（智能跳过会跳过已有正文的章节，见 4023
   *  行同款修复），串行 await 保证任务不并发打架；完成后自动刷新预检，
   *  占位符清单与基线趋势随之更新。 */
  const handleRerunAffected = useCallback(async () => {
    if (!id) return;
    if (generating) { msg.warning("当前有生成任务进行中，请等待完成后再重跑"); return; }
    const targets: string[] = rerunPlan?.rerunnable_sections || [];
    if (!targets.length) {
      msg.info("没有可自动重跑的章节（占位字段均无补录数据，请先补录全局事实/解析提取，或手动回改正文）");
      return;
    }
    setRerunning(true);
    try {
      for (const sid of targets) {
        await handleGenerateContent({
          section_id: sid, mode: "section", force_rewrite: true,
        });
      }
      msg.success(`已重跑 ${targets.length} 个受影响章节，正在刷新清单…`);
      await handleExportCheck();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "重跑中断，可稍后重新预检查看进度");
    } finally {
      setRerunning(false);
    }
  }, [id, generating, rerunPlan, handleGenerateContent, handleExportCheck]);

  const handleToggleExpand = useCallback(() => {
    if (expandedKeys.length > 0) {
      setExpandedKeys([]);
    } else {
      setExpandedKeys(collectAllKeys(tree));
    }
  }, [tree, expandedKeys]);

  // ✅ 左侧面板的「前往目录生成」：稳定引用，避免 memo 面板重渲
  const handleGoToOutline = useCallback(() => setActiveTab("outline"), []);

  // ✅ 目录生成进度：最近一次"已完成章节/总数"（长方案分步生成时后端下发 done/total；
  //    短方案链路的 0/0 快照不算章节维度，避免统计色块显示「章节 0/0」）
  const outlineDoneTotal = useMemo(() => {
    for (let i = outlineLogs.length - 1; i >= 0; i--) {
      const l = outlineLogs[i];
      if (typeof l.done === "number" && typeof l.total === "number" && l.total > 0) return l;
    }
    return null;
  }, [outlineLogs]);
  // ✅ 目录生成阶段：优先采用后端权威阶段标签（单一来源，与后端 _OUTLINE_PHASE_MODEL
  //    对齐，覆盖「AI 生成目录 / 目录审核中 / 按审核意见修复」等原实现无法推断的阶段）；
  //    stats 尚未到达时（连接初期）按进度回退推断。
  const outlinePhase = genStats.phase_label
    ? genStats.phase_label
    : progress >= 1 ? "已完成"
    : progress >= 0.93 ? "审核校对"
    : progress >= 0.3 ? "生成二三级目录"
    : progress >= 0.1 ? "生成一级目录"
    : "准备中";

  // ================== ✅ 2026-09-25 性能治理：稳定回调 + 派生数据收敛 ==================
  // 背景：本页面是 9000+ 行单体组件（132 个 useState），任一 state 变更都会重跑整个
  // 函数体并重建 tabItems（6 个 Tab 的全部子树 JSX）。下方两个子组件 BidAnalysisTab
  // 与 SectionContentCard 已经 memo 化（前者含 7 个 useMemo，后者把草稿 state 下沉到
  // 组件内），但父级传入的是内联箭头函数 —— Object.is 比较必然全败，memo 被彻底击穿，
  // 退化为裸函数组件（等于每帧全量重渲 922 行 + 275 行的子树）。这里把内联回调收敛
  // 为 useCallback（依赖全是稳定 setter / 既有稳定回调 / ref），让 memo 真正生效。
  const baOpenConfig = useCallback(() => setBaConfigOpen(true), []);
  const baRefreshResults = useCallback(() => { loadBaResults(); }, [loadBaResults]);
  const baRefreshAll = useCallback(() => { loadDocuments(); loadBaResults(); }, [loadDocuments, loadBaResults]);
  const baDismissSectionCheck = useCallback(() => setSectionCheckResult(null), []);
  const baGoImport = useCallback(() => { setActiveTab("import"); setImportSubTab("docs"); }, []);
  const baGoOutline = useCallback(() => setActiveTab("outline"), []);
  const baGoFacts = useCallback(() => setActiveTab("facts"), []);

  const sectionEditStart = useCallback(() => setIsEditingSection(true), []);
  const sectionEditCancel = useCallback(() => {
    setIsEditingSection(false);
    setEditContent("");
    setEditDirty(false);
  }, []);
  // 图表 AI 修复回写：用 selectedSectionRef 读最新选中章节，避免闭包捕获旧值
  // （与文件内既有 selectedSectionRef / treeRef 镜像模式一致）
  const sectionChartFixed = useCallback((newContent: string, wordCount?: number) => {
    const sec = selectedSectionRef.current;
    if (!sec) return;
    // ✅ F2（R71）：图表修复后端已重写正文并回带权威 word_count（fix-mermaid 响应），
    //    优先用后端值；老后端未回带时保留章节原字数，绝不用 newContent.length 伪造字符口径。
    const hasWc = typeof wordCount === "number";
    const patchNode = (nodes: TreeNode[]): TreeNode[] =>
      nodes.map((n) =>
        n.key === sec.key
          ? { ...n, content: newContent, ...(hasWc ? { word_count: wordCount } : {}) }
          : { ...n, children: n.children ? patchNode(n.children) : undefined }
      );
    setSelectedSection((prev) =>
      prev && prev.key === sec.key
        ? { ...prev, content: newContent, ...(hasWc ? { word_count: wordCount } : {}) }
        : prev
    );
    setTree((prev) => patchNode(prev));
  }, []);

  // ✅ 派生数据收敛：旧实现在同一渲染内 computeDocStats 调用 5 次、
  // countGeneratedLeaves 2 次、collectAllKeys 2 次，且每次都返回新对象字面量。
  // 统一算一次并记忆化，数值与渲染结果完全不变。
  const docStats = useMemo(() => computeDocStats(docList), [docList]);
  const outlineLeafCounts = useMemo(() => countGeneratedLeaves(tree), [tree]);
  const treeNodeCount = useMemo(() => (tree.length ? collectAllKeys(tree).length : 0), [tree]);
  const baSummaryDerived = useMemo(
    () => baSummary ?? ((baItems.length || baDefs.length) ? recomputeBaSummary(baItems, baDefs) : null),
    [baSummary, baItems, baDefs],
  );

  const tabItems = [
    // ================== Tab 1: 上传解析（import） ==================
    // 2026-09-23 合并：把「文档解析」（原 import 主体）与「项目提取」（原 bidAnalysis）
    // 收进同一个顶层 Tab，内部用 Ant Design 二级 Tabs 承载，避免顶层步数虚增。
    {
      key: "import",
      label: (
        // 2026-09-23 更名：原「上传解析」→「解析提取」，与合并后的模块职责对齐（解析 + 提取一体化）
        <WorkflowTabLabel
          step={1}
          title="解析提取"
          badge={docList.length
            ? `${docStats.parsedCount}/${docList.length}`
            : ""}
          badgeColor={docList.length > 0 && docStats.allParsed
            ? "green"
            : "default"}
          hint="第一步：上传项目资料并解析为纯文本，随后在同页内完成 AI 结构化提取（供目录/正文引用）"
        />
      ),
      // 2026-09-23：import 模块左面板（25vw）随子 Tab 切换内容：
      //   docs → 解析分类列表（list-only 变体）
      //   extract → 解析分类（复用同一套 groups/items，与 BidAnalysisTab 同源）
      // 右侧「信息显示窗口」已移到 docs 子 Tab 的上传区下方（detail-only），
      // 与左侧分类栏通过 sharedParseState 联动，避免同一分类在两侧同时展示造成的视觉重复。
      children: (
        <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {/* ===== 子 Tab 栏（文档解析 / 项目提取）—— 2026-09-23 单独一行，横跨全宽 =====
              原布局把子 Tab 塞在「右侧栏」内部（tabPosition=left 竖排），切换时只能切换右侧主体。
              现在拆成两行：
                第 1 行：子 Tab 标签栏（横排单行，只渲染标签）
                第 2 行：左侧分类/清单面板 Card + 当前子 Tab 内容（左右并排） */}
          {/* ✅ 2026-09-24 T2：子 Tab 标签栏抽为受控组件 ImportSubTabBar（含提取徽标） */}
          <ImportSubTabBar
            activeKey={importSubTab}
            onChange={setImportSubTab}
            summary={baSummary}
          />
          {/* ===== 第 2 行：左面板 + 当前子 Tab 内容（左右并排） ===== */}
          <div style={{ display: "flex", flex: 1, minHeight: 0, minWidth: 0, gap: 8 }}>
          {/* ===== import 模块专属左面板（随子 Tab 切换展示内容）=====
              两者的展开/激活状态互不干扰（docs 走 sharedParseState，extract 走自身本地 state） */}
          <Card
            size="small"
            // 2026-09-23：左面板宽度从 vw+紧 max 改为容器内 22%，min/max 保底。
            // 之前 vw 在大屏上被 maxWidth:300 压死，实际只占 ~15%，与右侧 ~85% 比例悬殊。
            // 改为百分比后：1920→422px, 1440→317px, 1280→282px，比例稳定在 22:78 ≈ 1:3.5。
            style={{ flex: "0 0 22%", minWidth: 260, maxWidth: 420, display: "flex", flexDirection: "column" }}
            styles={{ body: { flex: 1, minHeight: 0, padding: 4, overflow: "hidden" } }}
            title={
              <Space size={4}>
                <AppstoreOutlined />
                <Text strong>
                  {importSubTab === "docs"
                    ? "解析信息分类"
                    // ✅ 口径与右侧面板（baScopeText）同源：按已加载的提取项定义实报条数。
                    //    旧实现写死「18 项结构化提取」，后端增减项后标题即失真
                    //    （同 UploadParseTab:138 与 BidAnalysisTab:61 已修过的硬编码）。
                    //    定义未加载时不报数，只说「结构化提取」。
                    : baDefs.length
                      ? `${baDefs.length} 项结构化提取`
                      : "结构化提取"}
                </Text>
                <Tag color="blue">{(baGroups || []).length} 分类</Tag>
              </Space>
            }
          >
            {importSubTab === "docs" ? (
              <ParseResultCategoryPanel
                variant="list-only"
                groups={baGroups}
                items={baItems}
                selectedItem={selectedBaItem ?? null}
                loading={baLoading}
                error={baError}
                sharedState={sharedParseState}
                onSelectItem={setSelectedBaItem}
                onEditItem={openBaEdit}
                onOpenFullView={setBaFullItem}
                onRefresh={baRefreshResults}
              />
            ) : (
              /* extract 子 Tab 下：18 项扁平清单（受控组件 BaItemFlatList），
                 每项状态口径与后端 success/error/idle/running + isMissingBaResult 对齐，
                 点击切换到右侧 BidAnalysisTab 中对应项的详情 */
              <BaItemFlatList
                defs={baDefs}
                items={baItems}
                loading={baLoading && baDefs.length === 0}
                defsError={baDefsError}
                selectedItemId={selectedBaItem?.item_id ?? null}
                onSelectItem={setSelectedBaItem}
              />
            )}
          </Card>

          {/* ===== 右侧内容区（占剩余空间）—— 按 importSubTab 直接渲染 =====
              2026-09-23 版式重构：子 Tab 标签已上移到顶部独立行（横跨左面板 + 右侧），
              此处不再嵌套 Tabs，只按当前激活的子 Tab 渲染对应主体，
              避免"标签栏 + 内容栏"双层结构造成的高度浪费与视觉冗余。 */}
          <div style={{ flex: 1, minHeight: 0, minWidth: 0, display: "flex", flexDirection: "column" }}>
            {importSubTab === "docs" ? (
                    <div className="scroll-area" style={{ height: "100%", minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
                      {/* 上传解析主体（含上传区、资料与解析列表、下一步等） */}
                      <UploadParseTab
                        docs={docList as DocumentParseItem[]}
                        maxUploadBytes={maxUploadBytes}
                        categoryOptions={categoryOptions}
                        generating={generating}
                        uploadingFacts={uploadingFacts}
                        uploadedFiles={uploadedFiles}
                        parsingDocs={parsingDocs}
                        parsingDocId={parsingDocId}
                        onUploadFiles={handleUploadDocuments}
                        onParseAll={() => handleParseDocuments()}
                        onReparseAll={() => handleParseDocuments(true)}
                        onRefresh={loadDocuments}
                        onParse={handleParseDocument}
                        onPreview={handlePreviewDocument}
                        onDelete={handleDeleteDocument}
                        onCategoryChange={handleCategoryChange}
                        onNavigate={(t: WorkflowTabKey) => setActiveTab(t)}
                        // 2026-09-23 合并：上传解析「下一步」→ 切到同 Tab 的「项目提取」子页
                        onSwitchToExtract={() => setImportSubTab("extract")}
                        // ✅ 修复（2026-09-25）：把「解析信息分类显示栏」props 接入
                        // UploadParseTab，使其内置的「信息显示窗口」真正渲染（此前页面
                        // 在下方另写了一份 detail-only 面板导致重复渲染，且 UploadParseTab
                        // 的解析项数「下一步」动态文案恒失真）。现由 UploadParseTab 统一
                        // 渲染 detail-only 详情，与左栏 list-only 经同一 sharedParseState 联动。
                        parseGroups={baGroups}
                        parseItems={baItems}
                        parseSelectedItem={selectedBaItem}
                        parseLoading={baLoading}
                        parseError={baError}
                        sharedState={sharedParseState}
                        onParseSelectItem={setSelectedBaItem}
                        onParseEditItem={openBaEdit}
                        onParseFullView={setBaFullItem}
                        onParseRefresh={baRefreshResults}
                        parseRunning={baActive}
                      />
                    </div>
            ) : (
                    <BidAnalysisTab
                      defs={baDefs}
                      groups={baGroups}
                      schemeId={id || undefined}
                      projectId={scheme?.project_id}
                      items={baItems}
                      summary={baSummaryDerived}
                      running={baActive}
                      progress={baShownProgress}
                      progressMsg={baShownMsg}
                      parsedDocCount={docStats.parsedCount}
                      selectedItem={selectedBaItem}
                      sectionChecking={sectionChecking}
                      sectionCheckResult={sectionCheckResult}
                      textStats={baTextStats}
                      defsError={baDefsError}
                      // 2026-09-23：extract 子 Tab 下，「18 项结构化提取」清单已上移到左侧面板，
                      // 右侧仅保留进度/操作栏 + 详情阅读区，避免与左面板重复展示同一份清单。
                      contentOnly
                      onRetryDefs={loadBaMeta}
                      onStart={baOpenConfig}
                      onStop={handleStopBa}
                      stopping={baStopping}
                      onOpenConfig={baOpenConfig}
                      onCheckSections={handleCheckSections}
                      onRefresh={baRefreshAll}
                      onSelectItem={setSelectedBaItem}
                      onRerunItem={handleRerunItem}
                      onOpenFullView={setBaFullItem}
                      onEditItem={openBaEdit}
                      onDismissSectionResult={baDismissSectionCheck}
                      // 2026-09-23 合并：BidAnalysisTab 内「前往上传解析」→ 切回同 Tab 的 docs 子页
                      onGoImport={baGoImport}
                      onGoOutline={baGoOutline}
                      onGoFacts={baGoFacts}
                    />
            )}
          </div>
          </div>
        </div>
      ),
    },
    // ================== Tab 2: 目录生成（outline） ==================
    {
      key: "outline",
      label: (
        <WorkflowTabLabel
          step={2}
          title="目录生成"
          badge={tree.length > 0 ? `${treeNodeCount} 章` : ""}
          badgeColor={tree.length > 0 ? "blue" : "default"}
          hint="第二步：AI 生成 / 导入识别 / 套用目录库，可拖拽排序、双击改名、增删章节"
        />
      ),
      children: (
        // ✅ 修复：目录生成内容（编制要求卡 + 多标段提示 + 编辑树 + 目录库/AI 生成区）较长，
        //    与其余 Tab 同一模式：根容器必须 flex:1 + minHeight:0 + overflowY:auto，
        //    否则超出视口的底部内容会被外层 overflow:hidden 裁掉，无法滚动查看/操作。
        <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
          {/* 编制要求 / 评审要点（对齐 OpenBidKit score-planning：要求→目录必含映射） */}
          <Card
            size="small"
            style={{ marginBottom: 12, background: "#fafafa" }}
            styles={{ body: { padding: "8px 12px" } }}
            title={
              <Space size={8}>
                <AimOutlined style={{ color: "#1677ff" }} />
                <Text strong style={{ fontSize: 13 }}>
                  编制要求 / 评审要点（目录必须逐条覆盖）
                </Text>
              </Space>
            }
            extra={
              <Button size="small" type="primary" onClick={saveRequirements}>
                保存
              </Button>
            }
          >
            <Input.TextArea
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              placeholder={
                "逐条填写编制要求或评审要点（每行一条），AI 生成目录时将强制逐条覆盖，审核阶段自动补齐缺失章节。示例：\n危大工程清单及管控措施\n监测方案与信息化施工要求\n应急预案与应急物资配置"
              }
              autoSize={{ minRows: 3, maxRows: 8 }}
            />
          </Card>
          {/* ✅ 多标段提示（对齐 OpenBidKit bidSectionDetector）：导入的招标文件疑似多标段时给出拆分建议 */}
          {multiSectionHint?.has_multiple && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={`检测到疑似多标段项目资料（约 ${multiSectionHint.total_declared || multiSectionHint.detected_count} 个标段）`}
              description="当前导入的项目资料可能包含多个标段 / 标包。建议按标段拆分后分别生成方案，避免目录与正文互相混淆。"
            />
          )}
          {/* 顶部操作区 */}
          <Space style={{ marginBottom: 12 }} wrap>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={handleGenerateOutline}
              disabled={generating}
              loading={generating && genType === "outline"}
            >
              {tree.length > 0 ? "AI 重新生成目录" : "AI 生成目录"}
            </Button>
            <Upload
              accept={UPLOAD_FILE_ACCEPT}
              beforeUpload={(file) => {
                // ✅ 2026-10-10（FIX-2 类型预检）：accept 只过滤文件选择框，拖拽可
                //    绕过。与链① UploadParseTab 同口径，先按扩展名白名单 + 大小
                //    上限拦截，避免非法文件整份传完才被后端 400 拒绝。
                const { accepted, rejected } = partitionUploadFiles(
                  [file], resolveMaxUploadBytes(maxUploadBytes));
                if (rejected.length > 0) {
                  const byType = rejected.filter((r) => r.reason === "type");
                  const bySize = rejected.filter((r) => r.reason === "size");
                  if (byType.length > 0) {
                    msg.error(
                      `不支持的文件类型，已忽略：${byType.map((r) => r.file.name).join("、")}`
                    );
                  }
                  if (bySize.length > 0) {
                    msg.error(`文件过大，请上传 ${formatUploadLimitMb(maxUploadBytes)} 以内的文件`);
                  }
                  return false;
                }
                handleUploadParseOutline(accepted[0]);
                return false; // 阻止自动上传
              }}
              showUploadList={false}
            >
              <Button icon={<UploadOutlined />} disabled={generating}>
                导入目录（智能识别）
              </Button>
            </Upload>
            <Tooltip title="勾选后，识别结果先按标准章节骨架归位（保留用户细分内容，未归位内容进「补充章节」）再应用">
              <Checkbox
                checked={outlineImportReorganize}
                disabled={generating}
                onChange={(e) => setOutlineImportReorganize(e.target.checked)}
              >
                整理为标准结构
              </Checkbox>
            </Tooltip>
            <Button
              icon={<FolderOpenOutlined />}
              onClick={openLibraryPicker}
              disabled={generating}
            >
              从目录库套用
            </Button>
            <Tooltip
              title={lastUpload
                ? `把「${lastUpload.name}」的识别结果存为目录库（待审核）`
                : "请先「导入目录（智能识别）」上传文件"}
            >
              <Button
                icon={<FileTextOutlined />}
                onClick={handleSaveAsLibrary}
                disabled={generating || !lastUpload}
              >
                存为目录库
              </Button>
            </Tooltip>
            <Button
              icon={<CaretRightOutlined />}
              onClick={() => addChildNode()}
              disabled={generating}
            >
              新增一级章节
            </Button>
            <Button
              onClick={saveOutlineTree}
              type="primary"
              ghost
              disabled={generating || tree.length === 0}
            >
              💾 保存目录
            </Button>
            <Button
              onClick={clearAllOutline}
              danger
              type="primary"
              ghost
              disabled={generating || tree.length === 0}
            >
              🗑️ 清除所有目录
            </Button>
            {/* ✅ R106 FIX-2：目录生成 Tab 补停止入口（此前只能靠全局任务抽屉停止）*/}
            {generating && genType === "outline" && (
              <TaskControlButtons
                paused={taskPaused}
                stopping={taskStopping}
                onControl={handleControl}
              />
            )}
            <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              💡 双击章节标题可改名；行内 ↑/↓ 或拖拽可调整顺序；选中后可一键上移/下移/删除
            </Text>
          </Space>

          {/* ✅ 目录生成进度已统一到页面顶部进度区（此处不再重复渲染第二个进度条） */}

          {/* 目录树编辑区 */}
          <Card
            size="small"
            title={
              <Space>
                <Text strong>目录结构</Text>
                <Tag color="blue">{collectAllKeys(tree).length} 个章节</Tag>
                {hasUnsavedLocalNodes(tree) && (
                  <Tag color="orange">有未保存的修改</Tag>
                )}
              </Space>
            }
            extra={
              <OutlineTreeActions
                selected={selectedSection as TreeNode | null}
                moveFlags={moveFlags}
                showNext={tree.length > 0}
                onAddChild={() => selectedSection && addChildNode(selectedSection.key!)}
                onMove={(dir) => selectedSection && moveNode(selectedSection.key!, dir)}
                onDelete={() => selectedSection && deleteNode(selectedSection.key!)}
                onNextStep={() => setActiveTab(NEXT_TAB.outline)}
              />
            }
          >
            {reorderHint && (
              <Alert
                type="success"
                showIcon
                banner
                closable
                message={reorderHint}
                style={{ marginBottom: 8 }}
                onClose={() => setReorderHint(null)}
              />
            )}
            {/* ✅ 字数预算编辑入口（2026-09-21 补齐功能缺口）：
                此前预算全链路只读，用户无法在正文生成前规划每章篇幅 */}
            {selectedSection && tree.length > 0 && (
              <OutlineNodeBudgetPanel
                node={selectedSection as TreeNode}
                onBudgetChange={handleBudgetChange}
              />
            )}
            {tree.length === 0 ? (
              <Empty description="暂无目录，请点击上方 AI 生成目录 或 导入目录" />
            ) : (
              <div ref={editTreeScrollRef} className="scroll-area" style={{ minHeight: 200, maxHeight: "calc(100vh - 420px)", overflow: "auto" }}>
                <Tree
                  treeData={outlineEditTreeData}
                  draggable
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpandedKeys(keys as React.Key[])}
                  selectedKeys={selectedSection ? [selectedSection.key!] : []}
                  onSelect={(keys) => {
                    if (keys.length > 0) {
                      const sec = findSectionById(tree, keys[0] as string);
                      if (sec) setSelectedSection(sec);
                    }
                  }}
                  onDrop={onOutlineTreeDrop}
                  showLine={{ showLeafIcon: false }}
                  blockNode
                />
              </div>
            )}
          </Card>
        </div>
      ),
    },
    // ================== Tab 3: 全局事实（facts） ==================
    {
      key: "facts",
      label: (
        <WorkflowTabLabel
          step={3}
          title="全局事实"
          badge={factsSummary?.total ? String(factsSummary.total) : ""}
          badgeColor={factsSummary?.has_warnings
            ? "orange"
            : factsSummary?.total
              ? "green"
              : "default"}
          hint={factsSummary?.has_warnings
            ? `待确认模拟值 ${factsSummary.simulated || 0} 项 · 待审核 ${factsSummary.unresolved || 0} 项 · 矛盾 ${factsSummary.conflicts || 0} 项${factsSummary.stale > 0 ? ` · 来源过期 ${factsSummary.stale} 项` : ""}`
            : "第三步：AI 从已解析资料提取全篇统一的事实变量，供正文生成保持一致"}
        />
      ),
      children: (
        // ✅ 修复：全局事实内容可能很长（资料列表 + 分组事实 Collapse），
        //    容器必须可滚动，否则超出视口的内容被外层 overflow:hidden 裁剪，无法查看/点击
        <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
          {/* ===== 顶部：统计面板 + 告警 ===== */}
          {factsSummary && factsSummary.total > 0 && (
            <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: "8px 16px" } }}>
              <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
                <Text strong>📊 事实概览：</Text>
                <span>共 <Text strong>{factsSummary.total}</Text> 项</span>
                {factsSummary.simulated > 0 && (
                  <Tooltip title="AI 模拟生成，需人工确认后才能注入正文">
                    <Tag color="orange">⚠️ 模拟值 {factsSummary.simulated}</Tag>
                  </Tooltip>
                )}
                {factsSummary.unresolved > 0 && (
                  <Tag color="blue">📝 待审核 {factsSummary.unresolved}</Tag>
                )}
                {factsSummary.conflicts > 0 && (
                  <Tooltip title="同一事实存在多个取值，需人工裁决">
                    <Tag color="red">🔸 矛盾 {factsSummary.conflicts}</Tag>
                  </Tooltip>
                )}
                {/* ✅ BUG 修复（2026-09-27）：过期事实此前既不计入 has_warnings
                    也不展示，用户无法得知「为什么事实没进正文」。资料变更/重新提取
                    会把旧值标记 is_stale=1 并被注入门控排除，必须显式可见。
                    ✅ G3（2026-10-04）：补「确认仍有效」入口 —— 此前一旦被打上
                    is_stale=1 就没有任何解除手段（改值/裁决之外），用户核对过
                    但值未变时只能重跑 AI 提取。 */}
                {factsSummary.stale > 0 && (
                  <Tooltip title="来源资料已变化或已被重新提取取代，这些事实不会注入正文；请重新提取或人工核对">
                    <Tag color="volcano">🕒 来源过期 {factsSummary.stale}</Tag>
                  </Tooltip>
                )}
                {factsSummary.stale > 0 && (
                  <Button size="small" onClick={handleAckStaleAll}>
                    我已核对，仍有效
                  </Button>
                )}
                {factsSummary.has_warnings && (
                  <Button size="small" type="primary" danger onClick={handleBatchResolve}>
                    一键确认全部
                  </Button>
                )}
                {!factsSummary.has_warnings && factsSummary.total > 0 && (
                  <>
                    <Tag color="green"><CheckCircleOutlined /> 全部就绪</Tag>
                    <Button
                      size="small"
                      type="primary"
                      ghost
                      icon={<ArrowRightOutlined />}
                      onClick={() => setActiveTab(NEXT_TAB.facts)}
                    >
                      下一步：正文生成
                    </Button>
                  </>
                )}
              </div>
              {/* ✅ 缺陷 C 修复：展示 stats.by_category 分类分布（旧前端未消费）*/}
              {Array.isArray(factsSummary.by_category) && factsSummary.by_category.length > 0 && (
                <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>分类分布：</Text>
                  {factsSummary.by_category.map((c) => (
                    <Tooltip
                      key={c.category}
                      title={`${c.title || c.category}：${c.groups} 组 / ${c.items} 条·模拟 ${c.simulated}·矛盾 ${c.conflicts}·待审 ${c.unresolved}`}
                    >
                      <Tag color={c.conflicts > 0 ? "red" : c.unresolved > 0 ? "blue" : c.simulated > 0 ? "orange" : undefined}>
                        {c.title || c.category} {c.items}
                      </Tag>
                    </Tooltip>
                  ))}
                </div>
              )}
              {/* ✅ R89：`simulated_ratio` 缺失时必须判 0 —— 旧写法
                  `stats.simulated_ratio > 0.3` 在字段缺席（后端未下发/旧版本）
                  时得到 undefined>0.3 = false，虽不崩但也读不出「后端没给」；
                  归一后与 buildFactsSummary 对其它计数的处理同口径。 */}
              {Number(factsSummary.simulated_ratio) > 0.3 && (
                <Alert
                  style={{ marginTop: 8 }}
                  type="warning"
                  showIcon
                  message={`模拟值占比 ${(Number(factsSummary.simulated_ratio) * 100).toFixed(0)}% 超过 30% 阈值，建议补充更多项目资料以减少 AI 模拟`}
                />
              )}
              {/* ✅ 缺陷 B 修复：展示 SSE completed 事件的跨段矛盾清单（旧前端未消费）*/}
              {factsCrossConflicts.length > 0 && (
                <Alert
                  style={{ marginTop: 8 }}
                  type="error"
                  showIcon
                  message={`检测到 ${factsCrossConflicts.length} 处跨段矛盾，请在下方对应条目中手动裁决`}
                  description={
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {factsCrossConflicts.slice(0, 8).map((cf, idx: number) => {
                        const sevColor = cf.severity === "high" ? "red" : cf.severity === "medium" ? "orange" : "blue";
                        return (
                          <span key={cf.rule_id || idx} style={{ fontSize: 12 }}>
                            <Tag color={sevColor}>{cf.severity || "low"}</Tag>
                            <Text strong>{cf.side_a?.name}</Text>：
                            {/* 后端 value 契约为 string（facts_extractor 下发），编译期收窄 */}
                            <Text delete type="danger">{cf.side_a?.value as string | undefined}</Text>
                            {" ↔ "}
                            <Text type="warning">{cf.side_b?.value as string | undefined}</Text>
                            {cf.resolution_hint ? <Text type="secondary"> · {cf.resolution_hint}</Text> : null}
                          </span>
                        );
                      })}
                      {factsCrossConflicts.length > 8 && (
                        <Text type="secondary" style={{ fontSize: 12 }}>…另有 {factsCrossConflicts.length - 8} 处矛盾</Text>
                      )}
                    </div>
                  }
                />
              )}
            </Card>
          )}

          {/* ===== 操作按钮（分步工作流：① 上传保存 → ② 解析 → ③ AI 提取 → ④ 修改）===== */}
          <Space style={{ marginBottom: 12 }} wrap>
            <Upload
              multiple
              accept={UPLOAD_FILE_ACCEPT}
              showUploadList={false}
              disabled={generating || uploadingFacts || parsingDocs}
              beforeUpload={(file, fileList) => {
                if (fileList[0] === file) {
                  // antd Upload 元素的 originFileObj 为可选键（受控链路才有），结构断言读取
                  const files = fileList.map((f) => (f as { originFileObj?: File }).originFileObj || f) as File[];
                  setTimeout(() => handleUploadDocuments(files), 0);
                }
                return false;
              }}
            >
              <Button icon={<UploadOutlined />} disabled={generating || uploadingFacts || parsingDocs} loading={uploadingFacts}>
                ① 上传文件保存
              </Button>
            </Upload>
            <Button
              icon={<FileTextOutlined />}
              onClick={() => handleParseDocuments()}
              disabled={generating || uploadingFacts || parsingDocs
                || docStats.actionableCount === 0}
              loading={parsingDocs}
            >
              {/* ✅ 2026-09-24 B4：与 import Tab 按钮同口径（待解析 + 失败均可批量处理）
                  ✅ 2026-09-25：改用 useMemo 化的 docStats，消除每次渲染的重复计算与 IIFE */}
              ② 解析文档{docStats.pendingCount > 0
                ? `（${docStats.actionableCount} 待解析）`
                : docStats.failedCount > 0
                  ? `（${docStats.failedCount} 解析失败可重试）`
                  : ""}
            </Button>
            <Button
              type="primary"
              icon={<GlobalOutlined />}
              onClick={handleGenerateFacts}
              disabled={generating || uploadingFacts || parsingDocs}
              loading={generating && genType === "facts"}
            >
              ③ AI 提取事实
            </Button>
            <Tooltip title="资料未给出的确定值如何处理：智能补全（标记模拟值）· 不杜撰（剔除模拟值）· 留待填写（写【待填写】）">
              <Radio.Group
                value={missingValueMode}
                onChange={(e) => setMissingValueMode(e.target.value)}
                size="small"
                buttonStyle="solid"
                disabled={generating || uploadingFacts || parsingDocs}
              >
                <Radio.Button value="fabricate">🧩 智能补全</Radio.Button>
                <Radio.Button value="omit">🚫 不杜撰</Radio.Button>
                <Radio.Button value="placeholder">📝 留待填写</Radio.Button>
              </Radio.Group>
            </Tooltip>
            {/* ✅ R45 · D5：AI 调整事实抽为独立子组件（FactsAdjustPanel）。
                状态机 / 竞态守卫 / 二次确认全部归位到组件内，交互行为由
                factsAdjust.test.tsx 钉住；页面只保留后端契约接线。 */}
            <FactsAdjustPanel
              schemeId={id}
              busy={generating || uploadingFacts || parsingDocs}
              disabled={(factsSummary?.total || 0) === 0}
              confirmDialog={(opts) => modal.confirm(opts)}
              preview={async (instruction) => {
                const { data } = await factsApi.adjust({
                  instruction,
                  scheme_id: id!,
                  apply: false,
                });
                return data;
              }}
              apply={async (operations, instruction) => {
                // 应用用户刚确认的预览计划，禁止后端再次调用 AI 生成另一份计划
                const { data } = await factsApi.adjust({
                  instruction,
                  scheme_id: id!,
                  apply: true,
                  operations,
                });
                await loadFacts();
                // ✅ 2026-10-08（F3 前端接线）：被丢弃的操作要在成功提示里也说清楚，
                //    否则「改了 3 条只落 2 条」在 toast 上仍是一个「已应用」。
                const ignoredCount = Array.isArray(data?.ignored)
                  ? data.ignored.length : 0;
                msg.success(ignoredCount > 0
                  ? `事实调整已应用，${ignoredCount} 项未纳入（详见面板提示）`
                  : "事实调整已应用");
                // 回传给面板渲染丢弃明细（组件把 void 当「无丢弃」）
                return { ignored: data?.ignored };
              }}
              onError={(fallback, e) => {
                // 结构化窄断言：axios 错误体上的 response.data.detail / message
                const err = e as { response?: { data?: { detail?: string } }; message?: string };
                msg.error(
                  err?.response?.data?.detail
                  || err?.message
                  || fallback,
                );
              }}
            />
            <Button
              icon={<CaretRightOutlined />}
              onClick={openFactCreate}
              disabled={generating || uploadingFacts || parsingDocs}
            >
              手动新增
            </Button>
            {/* ✅ 缺陷 A 修复：接线 factsApi.clearAll（一键清空全部事实）*/}
            {factsSummary && factsSummary.total > 0 && (
              <Button
                danger
                icon={<DeleteOutlined />}
                onClick={handleClearAllFacts}
                disabled={generating || uploadingFacts || parsingDocs}
              >
                清空全部事实
              </Button>
            )}
          </Space>


          {/* ===== 全局事实提取进度（阶段 + 分段级实时日志）===== */}
          {/* ✅ 组件级可测（2026-09-21）：渲染抽到模块级 FactsExtractProgressCard，
               行为由 factsTab.test.tsx 钉住 */}
          {generating && genType === "facts" && (
            <FactsExtractProgressCard
              progress={progress}
              progressMsg={progressMsg}
              logs={factsLogs}
              controls={
                <TaskControlButtons
                  paused={taskPaused}
                  stopping={taskStopping}
                  onControl={handleControl}
                />
              }
            />
          )}

          <FactsDiagnosticsPanel
            loading={factsDiagnosticsLoading}
            chapterReport={factsChapterReport}
            dangerReport={factsDangerReport}
            onRefresh={loadFactsDiagnostics}
          />

          {/* ✅ 2026-10-06：消费此前被整体丢弃的 stats.by_chapter。
              与上方 DiagnosticsPanel 口径不同（那个查必填字段覆盖率，
              这个查各章实际落了多少条事实 + 缺哪些字段）。 */}
          <FactsChapterCoveragePanel stats={factsSummary} titles={factChapterTitles} />

          {/* ===== 上次提取的分段失败详情（部分未完成时可见）===== */}
          {/* ✅ 组件级可测（2026-09-21）：渲染抽到模块级 FactsSegmentFailuresAlert */}
          {factsSegmentStats && factsSegmentStats.failed > 0 && (
            <FactsSegmentFailuresAlert stats={factsSegmentStats} />
          )}

          {/* ===== 筛选器 ===== */}
          {factsSummary && factsSummary.total > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Radio.Group
                value={factsFilter}
                onChange={(e) => setFactsFilter(e.target.value)}
                size="small"
              >
                <Radio.Button value="all">全部 ({factsSummary.total})</Radio.Button>
                {factsSummary.simulated > 0 && (
                  <Radio.Button value="simulated">
                    <span style={{ color: "#fa8c16" }}>⚠️ 模拟 ({factsSummary.simulated})</span>
                  </Radio.Button>
                )}
                {factsSummary.conflicts > 0 && (
                  <Radio.Button value="conflict">
                    <span style={{ color: "#ff4d4f" }}>🔸 矛盾 ({factsSummary.conflicts})</span>
                  </Radio.Button>
                )}
                {factsSummary.unresolved > 0 && (
                  <Radio.Button value="unresolved">
                    <span style={{ color: "#1677ff" }}>📝 待审核 ({factsSummary.unresolved})</span>
                  </Radio.Button>
                )}
              </Radio.Group>
            </div>
          )}

          {uploadingFacts && (
            <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 12 } }}>
              <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
                <div style={{ flex: "0 0 320px" }}>
                  <Progress percent={100} status="active" showInfo={false} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Text strong>🔄 正在保存文件（保存后可点击「② 解析文档」继续）...</Text>
                  {uploadedFiles.length > 0 && (
                    <Space wrap style={{ marginTop: 4 }}>
                      {uploadedFiles.map((name, i) => (
                        <Tag key={i} color="processing">{name}</Tag>
                      ))}
                    </Space>
                  )}
                </div>
              </div>
            </Card>
          )}

          {/* 已上传的项目资料文档列表
              ✅ 2026-09-23 改版：默认折叠收起，点击标题展开/收起查看 ——
              全局事实页的主角是提取结果（左侧分类面板 + 右侧分类详情），
              资料列表降级为可展开的参考信息，避免长列表把事实内容挤到屏外 */}
          <Card
            size="small"
            title={
              <Space
                style={{ cursor: "pointer", userSelect: "none", width: "100%" }}
                onClick={() => setFactsDocsCollapsed((v) => !v)}
              >
                <span style={{ fontSize: 11, color: "#999" }}>
                  {factsDocsCollapsed ? "▶" : "▼"}
                </span>
                <FileTextOutlined />
                <Text strong>已上传的项目资料</Text>
                <Tag color="blue">{docList.length} 个文件</Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {factsDocsCollapsed ? "（点击展开查看）" : "（点击收起）"}
                </Text>
              </Space>
            }
            extra={
              <Space size={4}>
                {/* ✅ 全部重解析：补齐旧版截断内容 / 启用 OCR 后重扫扫描件 */}
                {docList.length > 0 && (
                  <Tooltip title="对所有文档（含已解析）重新解析，用于补齐旧版截断内容或在启用 OCR 后重扫扫描件">
                    <Button
                      size="small"
                      type="link"
                      disabled={generating || parsingDocs}
                      onClick={() => {
                        modal.confirm({
                          title: "重新解析全部文档",
                          content: `将对 ${docList.length} 个文档重新解析（含已解析的）。解析完成后需重新执行「③ AI 提取事实」才能更新全局事实。`,
                          okText: "开始重解析",
                          cancelText: "取消",
                          onOk: () => handleParseDocuments(true),
                        });
                      }}
                    >
                      <ReloadOutlined /> 全部重解析
                    </Button>
                  </Tooltip>
                )}
                <Button
                  size="small"
                  type="link"
                  onClick={() => loadDocuments()}
                  disabled={generating}
                >
                  <ReloadOutlined /> 刷新
                </Button>
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            {docList.length === 0 ? (
              <div style={{ textAlign: "center", padding: 16, color: "#999", fontSize: 13 }}>
                还没有上传资料文件。工作流：① 上传文件保存 → ② 解析文档 → ③ AI 提取事实
              </div>
            ) : factsDocsCollapsed ? (
              // ✅ 折叠态：不渲染文件列表，仅保留一行摘要（避免长列表挤出事实内容）
              <div style={{ textAlign: "center", padding: 8, color: "#999", fontSize: 12 }}>
                已收起 {docList.length} 个文件（含 {docList.filter((d) => isDocParsed(d)).length} 个已解析），点击标题展开查看
              </div>
            ) : (
              <List
                size="small"
                dataSource={docList}
                renderItem={(doc) => {
                  const ftypeIcon: Record<string, string> = {
                    docx: "📄", pdf: "📑", md: "📝", txt: "📃",
                    xlsx: "📊", xls: "📈", csv: "📉",
                    png: "🖼️", jpg: "🖼️", jpeg: "🖼️", bmp: "🖼️", tiff: "🖼️",
                  };
                  const icon = ftypeIcon[doc.file_type as string] || "📎";
                  const isParsed = isDocParsed(doc);
                  const isFailed = isDocFailed(doc);
                  return (
                    <List.Item
                      actions={[
                        ...((!isParsed) ? [
                          <Button
                            key="parse"
                            size="small"
                            type="link"
                            icon={<FileTextOutlined />}
                            disabled={parsingDocs}
                            onClick={() => handleParseDocument(doc.id, doc.file_name as string)}
                          >
                            {isFailed ? "重试解析" : "解析"}
                          </Button>,
                        ] : []),
                        ...(isParsed && doc.truncated ? [
                          <Tooltip
                            key="reparse"
                            title="该文档内容可能被旧版解析上限截断，重新解析可获取完整内容"
                          >
                            <Button
                              size="small"
                              type="link"
                              icon={<ReloadOutlined />}
                              disabled={parsingDocs}
                              onClick={() => handleParseDocument(doc.id, doc.file_name as string, true)}
                            >
                              重新解析
                            </Button>
                          </Tooltip>,
                        ] : []),
                        <Button
                          key="del"
                          size="small"
                          danger
                          type="link"
                          icon={<CloseCircleOutlined />}
                          onClick={() => handleDeleteDocument(doc.id, doc.file_name as string)}
                        >
                          删除
                        </Button>,
                      ]}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
                        <span style={{ fontSize: 18 }}>{icon}</span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{
                            fontWeight: 500,
                            fontSize: 14,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }} title={doc.file_name as string}>
                            {doc.file_name as string}
                          </div>
                          <div style={{ fontSize: 12, color: "#999" }}>
                            <Tag color="default" style={{ margin: "0 4px 0 0" }}>
                              {(doc.file_type as string | undefined)?.toUpperCase() || "未知格式"}
                            </Tag>
                            {isFailed ? (
                              <Tag color="red" style={{ margin: "0 4px 0 0" }}>解析失败，可重试</Tag>
                            ) : isParsed ? (
                              <Tag color="green" style={{ margin: "0 4px 0 0" }}>✓ 已解析 {doc.text_len || 0} 字</Tag>
                            ) : (
                              <Tag color="orange" style={{ margin: "0 4px 0 0" }}>⏳ 待解析</Tag>
                            )}
                            {isParsed && doc.truncated && (
                              <Tooltip title="内容达到旧版上限可能被截断，建议点「重新解析」获取完整内容">
                                <Tag color="volcano" style={{ margin: "0 4px 0 0" }}>⚠ 可能被截断</Tag>
                              </Tooltip>
                            )}
                            {doc.created_at as string | undefined}
                          </div>
                        </div>
                      </div>
                    </List.Item>
                  );
                }}
              />
            )}
          </Card>

          {/* ===== 全局事实分组列表（增强版：带模拟值/矛盾/置信度标记）=====
               ✅ 2026-09-23 改版：展示左侧分类面板选中的分类详情（项目资料下方），
               组合筛选器（模拟/矛盾/待审核）仍然生效 */}
          {facts.length > 0 && activeFactCategory ? (
            <>
              <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <Text strong>
                  {factCategoryEntries.find((e) => e.category === activeFactCategory)?.title
                    || activeFactCategory}
                </Text>
                <Tag color="blue">
                  {activeFactGroups.length} 组 / {activeFactGroups.reduce((n, g) => n + (g.items?.length || 0), 0)} 项
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  来自左侧「事实分类」面板选中项
                </Text>
              </div>
              <FactsGroupList
                groups={activeFactGroups}
                filter={factsFilter}
                onEditGroup={openFactEdit}
                onDeleteGroup={handleDeleteFactGroup}
                onEditItem={openFactItemEdit}
                onResolveItem={handleResolveFactItem}
                onResolveConflict={handleResolveConflictItem}
                onAckStaleOne={handleAckStaleOne}
                chapterTitles={factChapterTitles}
                factAttrTitles={factAttrTitles}
                sourceKindTitles={factSourceKindTitles}
              />
            </>
          ) : facts.length > 0 ? (
            <>
              <FactsGroupList
                groups={facts}
                filter={factsFilter}
                onEditGroup={openFactEdit}
                onDeleteGroup={handleDeleteFactGroup}
                onEditItem={openFactItemEdit}
                onResolveItem={handleResolveFactItem}
                onResolveConflict={handleResolveConflictItem}
                onAckStaleOne={handleAckStaleOne}
                chapterTitles={factChapterTitles}
                factAttrTitles={factAttrTitles}
                sourceKindTitles={factSourceKindTitles}
              />
              {/* ✅ 2026-10-06：消费后端 pagination（此前从不下发 limit/offset，
                  分组一多就整屏渲染）。总数不足一页时不渲染控件。 */}
              {(() => {
                const pg = buildFactsPagination(factsPagination, FACTS_PAGE_SIZE);
                if (!pg.show) return null;
                return (
                  <div style={{ marginTop: 12, textAlign: "center" }}>
                    <Pagination
                      current={pg.page}
                      pageSize={pg.pageSize}
                      total={pg.total}
                      size="small"
                      showSizeChanger={false}
                      onChange={(p: number) => { void loadFacts(p); }}
                    />
                  </div>
                );
              })()}
            </>
          ) : (
            // ✅ 优化：工具栏（AI 提取/上传/手动新增）始终渲染于列表上方，
            // 空状态内不再重复渲染同一组按钮
            <Empty
              description="暂无全局事实，可使用上方按钮提取或录入"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          )}
        </div>
      ),
    },
    // ================== Tab 4: 正文生成 ==================
    {
      key: "content",
      label: (() => {
        const { done, total } = outlineLeafCounts;
        return (
          <WorkflowTabLabel
            step={4}
            title="正文生成"
            badge={total > 0 ? `${done}/${total} 章` : ""}
            badgeColor={total > 0 ? (done >= total ? "green" : "blue") : "default"}
            hint="第四步：按目录逐章生成正文，支持一键全文 / 单章 / 补全生成，可设字数与并发模式；开启「全文一致性 Agent 修复」后正文生成结束会自动扫描并修复前后矛盾"
          />
        );
      })(),
      children: (
        <ContentGenerationTab
          generating={generating}
          running={generating && genType === "content"}
          taskPaused={taskPaused}
          taskStopping={taskStopping}
          shrinking={shrinking}
          hasTree={tree.length > 0}
          hasSelectedSection={!!selectedSection}
          // ✅ 2026-10-04：改走与后端压缩端点同口径的判据（>预算 130%）。
          //    旧实现按 >预算 判定 → 100%~130% 区间按钮可点、后端必 400。
          canShrink={canShrinkSection(selectedSection)}
          wordBudgetOption={wordBudgetOption}
          customWordBudget={customWordBudget}
          concurrencyOption={concurrencyOption}
          autoConsistencyRepair={autoConsistencyRepair}
          consistencySeverity={consistencySeverity}
          autoShrinkOver={autoShrinkOver}
          crSummary={crSummary}
          onGenerateAll={() => confirmAndGenerate("all")}
          onGenerateCurrent={handleGenerateCurrentSection}
          onGenerateMissing={handleGenerateMissing}
          onContinueSection={handleContinueCurrentSection}
          onShrinkSection={handleShrinkCurrentSection}
          onReset={handleResetContent}
          canReset={outlineLeafCounts.done > 0}
          onControl={handleControl}
          onWordBudgetChange={setWordBudgetOption}
          onCustomWordBudgetChange={(v) => setCustomWordBudget(v || 2000)}
          onConcurrencyChange={setConcurrencyOption}
          onAutoConsistencyChange={setAutoConsistencyRepair}
          onSeverityChange={setConsistencySeverity}
          onOpenConsistencyWorkbench={openConsistencyWorkbench}
          onAutoShrinkChange={setAutoShrinkOver}
          onNextStep={() => setActiveTab(NEXT_TAB.content)}
          // F-CONTENT-STANDARD(2026-09-26): 生成标准选择接线
          generationStandard={generationStandard}
          onGenerationStandardChange={setGenerationStandard}
          onSaveSchemeDefault={handleSaveSchemeDefaultStandard}
          savingSchemeDefault={savingStandardDefault}
          schemeDefault={scheme?.generation_standard}
        >

          <SectionContentCard
            section={selectedSection as TreeNode | null}
            treeEmpty={tree.length === 0}
            isEditing={isEditingSection}
            saving={savingSection}
            generating={generating}
            draftKey={draftKey}
            statusColor={statusColor}
            statusText={statusText}
            onEditStart={sectionEditStart}
            onDirtyChange={setEditDirty}
            onSave={saveSectionEdit}
            onCancel={sectionEditCancel}
            onRegenerate={regenerateCurrentSection}
            onChartFixed={sectionChartFixed}
            // F-CONTENT-STANDARD(2026-09-26): 章节级生成标准覆盖
            sectionStandard={selectedSection?.generation_standard || ""}
            schemeStandard={scheme?.generation_standard}
            onSectionStandardChange={handleSectionStandardChange}
          />
        </ContentGenerationTab>
      ),
    },
    // ================== Tab 5: 审核与预检 ==================
    {
      key: "review",
      label: (
        <WorkflowTabLabel
          step={5}
          title="审核与预检"
          hint="第五步：先跑「一键总检」拿到六维评分与可否交付结论，再按需下钻规范符合性检查 / 专家论证预检 / 导出预检 / 质量自检；全部通过后用「章节审核工作流」逐章评审留痕，最后去「导出」页生成文档"
        />
      ),
      children: (
        // ✅ 修复：审核与预检内容（两张大卡片 + 检查结果表格 + 历史记录）较长，
        //    容器必须可滚动，避免低视口下底部内容被裁剪
        <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
          {/* ===== ✅ 交付就绪度总检（六维加权评分 + 阻断项 + 放行结论）=====
               放在最上方：用户进入本页首先要的是"能不能交付"这一个答案，
               下面的分项检查是拿到答案后的下钻入口。 */}
          <ReadinessDashboard
            schemeId={id || ""}
            refreshKey={reviewTick}
            onContentFixed={() => { load(); setReviewTick((v) => v + 1); }}
          />

          {/* ===== ✅ 规范符合性检查（complianceApi.check）===== */}
          <Card
            size="small"
            title={
              <Space>
                <SafetyOutlined />
                <Text strong>规范符合性检查（AI 逐条判定）</Text>
                <Tag color="blue">{checklistText.split("\n").filter((s) => s.trim()).length} 项清单</Tag>
              </Space>
            }
            extra={
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={loadComplianceHistory}
                loading={historyLoading}
              >
                刷新历史
              </Button>
            }
            style={{ marginBottom: 12 }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              每行一条检查项，AI 会逐条扫描方案正文并给出命中/缺失、风险等级与修改建议（结果自动落库）
            </Text>
            <Input.TextArea
              rows={4}
              value={checklistText}
              onChange={(e) => { setChecklistText(e.target.value); setChecklistDirty(true); }}
              placeholder="每行一条检查项"
              style={{ marginTop: 8, fontFamily: "Consolas, monospace", fontSize: 12 }}
            />
            <Space style={{ marginTop: 8 }} wrap>
              <Button
                type="primary"
                icon={<SafetyOutlined />}
                loading={checkingCompliance}
                onClick={handleComplianceCheck}
              >
                开始合规检查
              </Button>
              {/* ✅ R106 FIX-5：运行中给出取消入口 */}
              {checkingCompliance && (
                <Button danger icon={<StopOutlined />} onClick={handleCancelComplianceCheck}>
                  取消检查
                </Button>
              )}
              <Button
                onClick={() => {
                  setChecklistText(
                    aiRules.length
                      ? aiRules.map((r) => r.title).join("\n")
                      : DEFAULT_COMPLIANCE_CHECKLIST.join("\n"));
                  setChecklistDirty(false);
                }}
                disabled={checkingCompliance}
              >
                恢复规则库清单
              </Button>
              {complianceResults.length > 0 && (
                <Button onClick={() => setComplianceResults([])}>清除本次结果</Button>
              )}
            </Space>

            {complianceResults.length > 0 && (
              <Table
                size="small"
                rowKey={(r, i) => `${r.rule_id || "r"}-${i}`}
                style={{ marginTop: 12 }}
                pagination={false}
                // ✅ 性能优化：虚拟滚动（pagination=false + 固定表体，
                // 全量渲染会把视口外行也建成 DOM）
                scroll={{ y: 320 }}
                virtual
                dataSource={complianceResults}
                columns={[
                  {
                    title: "判定", dataIndex: "hit", width: 80,
                    render: (hit) => (
                      <Tag color={hit === true ? "green" : hit === false ? "red" : "default"}>
                        {hit === true ? "命中" : hit === false ? "缺失" : "疑似"}
                      </Tag>
                    ),
                  },
                  {
                    title: "风险", dataIndex: "severity", width: 80,
                    // ✅ BUG 修复（2026-09-23）：旧实现只处理 high/medium，
                    //    AI 命中 block（阻断）时显示成蓝色与 low 无法区分，
                    //    且展示英文原词。现统一用规则库的严重度色板/中文标签。
                    render: (s: string) => (s
                      ? <Tag color={SEVERITY_COLOR[s as Severity] ?? "default"}>{SEVERITY_LABEL[s as Severity] ?? s}</Tag>
                      : "-"),
                  },
                  { title: "检查项", dataIndex: "item", ellipsis: true },
                  { title: "命中位置/证据", dataIndex: "evidence", ellipsis: true,
                    render: (v: string) => v || "—" },
                  { title: "修改建议", dataIndex: "suggestion", ellipsis: true,
                    render: (v: string) => v || "—" },
                ]}
              />
            )}

            {complianceHistory.length > 0 && (
              <Collapse
                ghost
                style={{ marginTop: 8 }}
                items={[{
                  key: "history",
                  label: <Text type="secondary">历史检查记录（{complianceHistory.length} 条）</Text>,
                  children: (
                    <Table
                      size="small"
                      rowKey="id"
                      pagination={{ pageSize: 10, size: "small" }}
                      dataSource={complianceHistory}
                      columns={[
                        { title: "时间", dataIndex: "created_at", width: 170,
                          render: (v: string) => (v || "").replace("T", " ").slice(0, 19) },
                        { title: "规则", dataIndex: "rule_id", width: 80,
                          render: (v: string) => v || "—" },
                        { title: "检查项", dataIndex: "item", ellipsis: true,
                          render: (v: string) => v || "—" },
                        { title: "风险", dataIndex: "severity", width: 80,
                          // ✅ BUG 修复（2026-09-23）：同上，历史表补 block/low 色与中文标签
                          render: (s: string) => s
                            ? <Tag color={SEVERITY_COLOR[s as Severity] ?? "default"}>{SEVERITY_LABEL[s as Severity] ?? s}</Tag>
                            : "—" },
                        { title: "建议", dataIndex: "suggestion", ellipsis: true,
                          render: (v: string) => v || "—" },
                      ]}
                    />
                  ),
                }]}
              />
            )}
          </Card>

          <Divider style={{ margin: "8px 0 16px" }} />

          {/* ===== ✅ 全文一致性审计（consistency_audit 表，§3.12.1）===== */}
          <Card
            size="small"
            title={
              <Space>
                <FileSearchOutlined />
                <Text strong>全文一致性审计（事实 vs 正文）</Text>
                {consistencyResult && (
                  <Tag color={consistencyResult.score >= 90 ? "green" : consistencyResult.score >= 70 ? "orange" : "red"}>
                    评分 {consistencyResult.score}
                  </Tag>
                )}
              </Space>
            }
            extra={
              <Space>
                <Button size="small" onClick={loadConsistencyLatest}>载入上次结果</Button>
                <Button
                  size="small"
                  type="primary"
                  icon={<FileSearchOutlined />}
                  loading={auditRunning}
                  onClick={handleConsistencyAudit}
                >
                  开始审计
                </Button>
                {/* ✅ R106 FIX-5：运行中给出取消入口 */}
                {auditRunning && (
                  <Button
                    size="small"
                    danger
                    icon={<StopOutlined />}
                    onClick={handleCancelConsistencyAudit}
                  >
                    取消
                  </Button>
                )}
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              将「全局事实」（唯一可信数据源）与正文逐项比对：数值 / 单位 / 时间 / 术语名称 / 章节间逻辑。需先完成「事实」页的全局事实提取。
              {consistencyResult?.created_at ? `（上次审计：${consistencyResult.created_at.replace("T", " ").slice(0, 19)}）` : ""}
            </Text>
            {auditRunning && (
              <div style={{ marginTop: 8 }}>
                <Progress percent={70} status="active" showInfo={false} size="small" />
                <Text type="secondary" style={{ fontSize: 12 }}>AI 正在逐项比对正文与项目事实，长方案约需 1-3 分钟…</Text>
              </div>
            )}
            {consistencyResult && !auditRunning && (
              (consistencyResult.issues.length === 0 ? (
                <div style={{ marginTop: 8 }}>
                  <Tag color="green">未发现不一致项</Tag>
                </div>
              ) : (
                <Table
                  size="small"
                  style={{ marginTop: 8 }}
                  rowKey={(_, i) => String(i)}
                  pagination={{ pageSize: 8, size: "small" }}
                  dataSource={consistencyResult.issues}
                  columns={[
                    {
                      title: "风险", dataIndex: "severity", width: 80,
                      render: (s: string) => (
                        <Tag color={s === "high" ? "red" : s === "medium" ? "orange" : "blue"}>{s || "-"}</Tag>
                      ),
                    },
                    { title: "维度", dataIndex: "dimension", width: 110, render: (v: string) => v || "—" },
                    { title: "所在章节", dataIndex: "section_title", width: 140, ellipsis: true,
                      render: (v: string) => v || "—" },
                    { title: "事实值", dataIndex: "fact", ellipsis: true,
                      render: (v: string) => v || "—" },
                    { title: "正文引用", dataIndex: "content_quote", ellipsis: true,
                      render: (v: string) => v || "—" },
                    { title: "修改建议", dataIndex: "suggestion", ellipsis: true,
                      render: (v: string) => v || "—" },
                  ]}
                />
              ))
            )}
          </Card>

          <Divider style={{ margin: "8px 0 16px" }} />

          <Card
            size="small"
            title={
              <Space>
                <AuditOutlined />
                <Text strong>专家论证预检（危大工程）</Text>
              </Space>
            }
            extra={
              <Space>
                <Button
                  icon={<AuditOutlined />}
                  loading={expertRunning}
                  disabled={expertRunning}
                  onClick={handleExpertReview}
                >
                  开始预检
                </Button>
                {/* ✅ R106 FIX-5：运行中给出取消入口 */}
                {expertRunning && (
                  <Button
                    size="small"
                    danger
                    icon={<StopOutlined />}
                    onClick={handleCancelExpertReview}
                  >
                    取消
                  </Button>
                )}
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              面向需专项论证的危大工程，按论证必要项清单检查方案就绪度并给出缺失项与补充建议。
              与「导出」页的「导出预检」（检查章节完整性）用途不同。
            </Text>
          </Card>
          {expertItems.length > 0 && (
            <Card title="论证必要项清单" size="small" style={{ marginBottom: 12 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {expertItems.map((item: string) => {
                  const ready = expertResult?.ready?.includes(item);
                  const missing = expertResult?.missing?.includes(item);
                  return (
                    <Tag
                      key={item}
                      color={ready ? "green" : missing ? "red" : "default"}
                      style={{ margin: 0 }}
                    >
                      {item}{ready ? " ✓" : missing ? " ✗" : ""}
                    </Tag>
                  );
                })}
              </div>
            </Card>
          )}
          {expertResult && (
            <Card title="预检结果" size="small">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="就绪度评分">
                  <Tag color={expertResult.score! >= 80 ? "green" : expertResult.score! >= 60 ? "orange" : "red"}>
                    {expertResult.score}
                  </Tag>
                </Descriptions.Item>
                {(expertResult.missing?.length ?? 0) > 0 && (
                  <Descriptions.Item label="缺失项">
                    {expertResult.missing!.join("、")}
                  </Descriptions.Item>
                )}
                {(expertResult.suggestions?.length ?? 0) > 0 && (
                  <Descriptions.Item label="补充建议">
                    {expertResult.suggestions!.join("；")}
                  </Descriptions.Item>
                )}
              </Descriptions>
            </Card>
          )}

          <Divider style={{ margin: "8px 0 16px" }} />

          {/* ✅ 功能归位：导出预检本质是"预检"，与规范符合性检查 / 专家论证预检同属
              「审核与预检」这一步，统一收敛到本页；「导出」页只负责生成文档。
              旧实现把它放在导出页，导致"两个预检分居两页"，用户不知道该在哪一层做检查。 */}
          <Card
            size="small"
            title={
              <Space>
                <SafetyOutlined />
                <Text strong>导出预检（章节完整性）</Text>
                {exportIssues.length > 0 && (
                  <Tag color="orange">{exportIssues.length} 个问题</Tag>
                )}
              </Space>
            }
            extra={
              <Space size={4}>
                {(exportStats || exportIssues.length > 0) && (
                  <Button
                    size="small"
                    type="link"
                    onClick={() => {
                      setExportStats(null);
                      setExportIssues([]);
                      setExportPreflight(null);
                      setPlaceholderReport(null);
                      setRerunPlan(null);
                    }}
                  >
                    清除结果
                  </Button>
                )}
                <Button
                  size="small"
                  type="primary"
                  icon={<SafetyOutlined />}
                  loading={checkingExport}
                  onClick={handleExportCheck}
                >
                  开始预检
                </Button>
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              检查空章节、孤立节点、章节字数达标度与图表完成率，确认无误后再到「导出」页生成 DOCX / PDF。
              本项与上方「规范符合性检查」（AI 逐条判定内容是否合规）用途不同。
            </Text>
            {exportIssues.length > 0 && (
              <Alert
                type="warning"
                message={`导出预检发现 ${exportIssues.length} 个问题`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12 }}>
                    {exportIssues.map((iss, idx) => (
                      <li key={idx}>
                        {/* 后端契约：预检 issue 一定携带英文 type 键（ExportIssueItem 未收录） */}
                        {EXPORT_ISSUE_LABEL[iss.type!] || iss.type}：
                        <Text strong>{describeExportIssue(iss)}</Text>
                      </li>
                    ))}
                  </ul>
                }
                showIcon
                style={{ marginTop: 12 }}
              />
            )}
            {/* ✅ 《待补充清单》（2026-09-24，人工补录兜底层）：按字段/按章节聚合
                展示正文中的【待补充】占位，点击章节跳转「正文生成」页定位补录；
                有补录数据时支持「重跑受影响章节」（只重跑可补齐的叶子章节） */}
            <PlaceholderReportPanel
              report={placeholderReport}
              onJumpToSection={handlePlaceholderJump}
              schemeId={id || undefined}
              rerunCount={rerunPlan?.rerunnable_count || 0}
              rerunning={rerunning}
              onRerunAffected={handleRerunAffected}
            />
            {exportStats && (
              <Card size="small" style={{ marginTop: 12 }} title="方案完整性统计">
                <Row gutter={[12, 8]}>
                  <Col span={6}>
                    <div style={{ fontSize: 11, color: "#888" }}>总字数</div>
                    <div style={{ fontSize: 16, fontWeight: 600 }}>{exportStats.total_words?.toLocaleString() || 0}</div>
                  </Col>
                  <Col span={6}>
                    <div style={{ fontSize: 11, color: "#888" }}>已生成章节</div>
                    <div style={{ fontSize: 16, fontWeight: 600 }}>
                      {exportStats.generated_count || 0}/{exportStats.section_count || 0}
                    </div>
                  </Col>
                  <Col span={6}>
                    <div style={{ fontSize: 11, color: "#888" }}>空章节占比</div>
                    <div style={{ fontSize: 16, fontWeight: 600, color: (exportStats.empty_ratio || 0) > 20 ? "#E8836B" : "#52c41a" }}>
                      {exportStats.empty_ratio || 0}%
                    </div>
                  </Col>
                  <Col span={6}>
                    <div style={{ fontSize: 11, color: "#888" }}>图表完成率</div>
                    <div style={{ fontSize: 16, fontWeight: 600, color: (exportStats.chart_total || 0) > 0 && (exportStats.chart_done || 0) < (exportStats.chart_total || 0) ? "#E8836B" : "#52c41a" }}>
                      {exportStats.chart_total ? `${Math.round((exportStats.chart_done || 0) / exportStats.chart_total * 100)}%` : "—"}
                    </div>
                  </Col>
                </Row>
                {(exportStats.empty_ratio || 0) > 0 || (exportStats.chart_total || 0) > (exportStats.chart_done || 0) ? (
                  <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                    💡 仍有空章节 / 未完成图表：可回「正文生成」页用「补全生成」，或在章节正文中手动补图后重新预检。
                  </div>
                ) : (
                  <div style={{ marginTop: 8, fontSize: 12, color: "#52c41a" }}>
                    ✓ 章节与图表均已就绪，可前往「导出」页生成文档。
                  </div>
                )}
              </Card>
            )}
            {/* ✅ G1：导出预检与就绪度总检共用同一份数据，结论直接在此呈现，
                用户不必在两个页面之间来回比对「到底能不能交付」 */}
            {exportPreflight && (
              <Card size="small" style={{ marginTop: 12 }}
                title="就绪度总检结论（与「审核与预检」页同一份规则与数据）">
                <Space size={12} wrap>
                  <span style={{ fontSize: 20, fontWeight: 700, color: exportPreflight.released ? "#52c41a" : "#E8836B" }}>
                    {exportPreflight.total ?? 0}
                    <span style={{ fontSize: 11, fontWeight: 400, color: "#999" }}> / 100</span>
                  </span>
                  <Tag
                    color={exportPreflight.released ? "green" : exportPreflight.stale ? "volcano" : "orange"}
                    style={{ marginRight: 0 }}
                  >
                    {exportPreflight.grade || "—"} 级{exportPreflight.released ? " · 建议放行" : " · 不建议放行"}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>{exportPreflight.verdict || "—"}</Text>
                  {exportPreflight.stale && <Tag color="volcano" style={{ marginRight: 0 }}>结论已过期</Tag>}
                </Space>
                <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                  {exportPreflight.stale
                    ? "⚠️ 本次总检之后正文 / 图表已发生变更，结论可能失效；请去「审核与预检」页重新执行一键总检，再回来导出。"
                    : "详细评分、问题清单与整改报告见「审核与预检」页；两处使用同一份规则与同一套数据。"}
                  {exportPreflight.rule_version ? ` 规则版本 ${exportPreflight.rule_version}` : ""}
                </div>
              </Card>
            )}
            {!exportStats && exportIssues.length === 0 && (
              <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                尚未预检。点击右上角「开始预检」检查章节完整性与图表完成率。
              </div>
            )}
          </Card>

          <Divider style={{ margin: "8px 0 16px" }} />

          {/* ✅ 接线 GET /schemes/{id}/sections/quality（原本是后端孤儿接口：
              能力已实现、前端从未调用 —— 典型的"信息算得出但传不到用户眼前"） */}
          <Card
            size="small"
            title={
              <Space>
                <CheckCircleOutlined />
                <Text strong>正文质量自检（交付前）</Text>
                {(qualityResult?.summary?.problem_sections || 0) > 0 && (
                  <Tag color="orange">{qualityResult!.summary!.problem_sections} 个章节待修</Tag>
                )}
              </Space>
            }
            extra={
              <Space size={4}>
                {qualityResult && (
                  <Button size="small" type="link" onClick={() => setQualityResult(null)}>
                    清除结果
                  </Button>
                )}
                <Button
                  size="small"
                  type="primary"
                  icon={<CheckCircleOutlined />}
                  loading={checkingQuality}
                  onClick={handleQualityCheck}
                >
                  开始自检
                </Button>
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              扫描全部已生成正文，检出两类不可交付问题：口语化 / AI 腔残留（正常生成落库前已清洗，
              命中说明手工编辑过）、引用已废止或被替代的标准编号（须替换为现行版本）。
            </Text>

            {qualityResult && (
              <Row gutter={[12, 8]} style={{ marginTop: 12 }}>
                <Col span={6}>
                  <div style={{ fontSize: 11, color: "#888" }}>已扫描章节</div>
                  <div style={{ fontSize: 16, fontWeight: 600 }}>
                    {qualityResult.summary?.checked ?? 0}
                  </div>
                </Col>
                <Col span={6}>
                  <div style={{ fontSize: 11, color: "#888" }}>问题章节</div>
                  <div style={{
                    fontSize: 16, fontWeight: 600,
                    color: (qualityResult.summary?.problem_sections || 0) > 0 ? "#E8836B" : "#52c41a",
                  }}>
                    {qualityResult.summary?.problem_sections ?? 0}
                  </div>
                </Col>
                <Col span={6}>
                  <div style={{ fontSize: 11, color: "#888" }}>口语化残留</div>
                  <div style={{
                    fontSize: 16, fontWeight: 600,
                    color: (qualityResult.summary?.colloquial || 0) > 0 ? "#E8836B" : "#52c41a",
                  }}>
                    {qualityResult.summary?.colloquial ?? 0}
                  </div>
                </Col>
                <Col span={6}>
                  <div style={{ fontSize: 11, color: "#888" }}>废止标准</div>
                  <div style={{
                    fontSize: 16, fontWeight: 600,
                    color: (qualityResult.summary?.abolished_standards || 0) > 0 ? "#E8836B" : "#52c41a",
                  }}>
                    {qualityResult.summary?.abolished_standards ?? 0}
                  </div>
                </Col>
              </Row>
            )}

            {qualityResult && (qualityResult.items?.length || 0) > 0 && (
              <Table
                size="small"
                rowKey={(r) => r.section_id!}
                style={{ marginTop: 12 }}
                pagination={false}
                // ✅ 性能优化：虚拟滚动（同上方：pagination=false + 固定表体）
                scroll={{ y: 260 }}
                virtual
                dataSource={qualityResult.items || []}
                columns={[
                  { title: "章节", dataIndex: "title", ellipsis: true },
                  {
                    title: "口语化 / AI 腔残留", dataIndex: "colloquial_hits", width: 220,
                    render: (hits: string[]) =>
                      hits?.length
                        ? hits.map((h) => <Tag key={h} color="orange" style={{ marginBottom: 2 }}>{h}</Tag>)
                        : "—",
                  },
                  {
                    title: "已废止 / 被替代标准", dataIndex: "abolished_standards", width: 220,
                    render: (hits: string[]) =>
                      hits?.length
                        ? hits.map((h) => <Tag key={h} color="red" style={{ marginBottom: 2 }}>{h}</Tag>)
                        : "—",
                  },
                ]}
              />
            )}

            {qualityResult && (qualityResult.items?.length || 0) === 0 && (
              <div style={{ marginTop: 12, fontSize: 12, color: "#52c41a" }}>
                ✓ 未检出口语化残留与废止标准，正文可直接交付。
              </div>
            )}

            {qualityResult && (
              <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                标准库版本 {qualityResult.standard_db_version || "—"}
                {qualityResult.standard_db_checked_at
                  ? `（核对于 ${(qualityResult.standard_db_checked_at || "").replace("T", " ").slice(0, 10)}）`
                  : ""}
              </div>
            )}

            {!qualityResult && (
              <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                尚未自检。点击右上角「开始自检」扫描全部已生成正文。
              </div>
            )}
          </Card>

          <Divider style={{ margin: "8px 0 16px" }} />

          {/* ===== ✅ 章节审核工作流（人工评审）=====
               机器检查（上方各卡片）之后是人工评审：逐章通过/驳回并留痕，
               方案级提交时后端会校验预检是否存在阻断项，有硬伤不放行评审。 */}
          <ReviewWorkflowPanel
            schemeId={id || ""}
            refreshKey={reviewTick}
            onChanged={load}
          />
        </div>
      ),
    },
    // ================== Tab 6: 导出文档 ==================
    {
      key: "export",
      // ✅ antd Tabs 默认只渲染 active 面板，exportForm 在非 active 时 DOM 被销毁
      // → Form.useForm() 实例发现无 Form 节点，控制台报 "useForm not connected" 警告。
      // forceRender 让该面板始终挂载，消除警告并改善 setFieldsValue 时序（导出预检回填无需等待 open）。
      forceRender: true,
      label: (
        <WorkflowTabLabel
          step={6}
          title="导出文档"
          hint="第六步：设置版式并生成 DOCX / PDF，或导出目录结构 JSON。章节完整性等检查请先在「审核与预检」页完成"
        />
      ),
      children: (
        // ✅ 修复：导出配置表单（基本设置/正文样式/各级标题样式 + 统计 + 缓存）较长，
        //    Form 根元素需可滚动，避免低视口下底部按钮（导出预检/导出 DOCX/PDF）被裁剪
        <Form
          form={exportForm}
          layout="vertical"
          className="scroll-area"
          style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
          initialValues={{
          font_name: "宋体", font_size: 12, show_page_number: true,
          show_title_page: false, show_toc: false, bidder_name: "",
          allow_pil_fallback: false,
          // ✅ 新增排版选项（默认值 = 标准专项方案版式）
          chapter_page_break: true, line_spacing: 1.15,
          page_number_style: "simple", toc_depth: 3,
          heading_styles: {
            "1": { font_name: "宋体", font_size: 14, bold: true },
            "2": { font_name: "宋体", font_size: 14, bold: true },
            "3": { font_name: "宋体", font_size: 14, bold: true },
            "4": { font_name: "宋体", font_size: 14, bold: true },
            "5": { font_name: "宋体", font_size: 12, bold: false },
            "6": { font_name: "宋体", font_size: 12, bold: false },
            "7": { font_name: "宋体", font_size: 12, bold: false },
          },
          // ✅ 增强 v7：封面项目信息表 + 页边距（缺省值与标准版式一致，向后兼容）
          margins: { left: 2.5, right: 2.5, top: 2.5, bottom: 2.5 },
          cover_info: { 工程名称: "", 编制单位: "", 方案编号: "", 版本: "" },
          // ✅ 容错与增强开关：默认值与后端 export.py 的 config.get 默认逐项一致
          //    （ai_image_auto_generate=True，其余 False），未改动任何既有行为。
          ai_image_auto_generate: true,
    // ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单四开关。
    //    默认全 false —— 不传时后端 scheme_forms 规范化为空 dict，
    //    产物与本轮之前逐字一致（向后兼容）。
    scheme_forms: {
      compilation_note: false,
      approval: false,
      expert_review: false,
      drawing_appendix: false,
    },
          chart_fail_placeholder: false,
          auto_rewrite_content: false,
          auto_fix_unclosed_fences: false,
          heading_border: false,
          // ✅ 2026-10-11（《文档生成规则》4.2 导出标准）：版式预设，
          //    缺省 "" = 不启用，产物与本轮之前逐字一致（向后兼容）。
          layout_preset: "",
        }}>
          {/* ✅ 导出格式预设库：同项目排版格式一键复用（对标 OpenBidKit exportFormatPresets） */}
          <Card size="small" style={{ marginBottom: 8 }} title="格式预设">
            <Space wrap>
              <Select
                style={{ minWidth: 200 }}
                placeholder="选择格式预设"
                value={selectedPresetId || undefined}
                onChange={(pid: string) => {
                  setSelectedPresetId(pid);
                  const p = exportPresets.find((x) => x.id === pid);
                  if (p) exportForm.setFieldsValue(p.config);
                }}
                options={exportPresets.map((p) => ({ value: p.id, label: (p.is_default ? "★ " : "") + p.name }))}
              />
              {/* ✅ R-1（2026-10-04）：4 个预设操作按钮统一由 presetOpLoading 单键控制
                  loading + disabled。此前无 loading，快速双击会并发 POST（尤其保存预设）。 */}
              <Button
                size="small"
                loading={presetOpLoading === "refresh"}
                disabled={!!presetOpLoading}
                onClick={handleRefreshPresets}
              >刷新</Button>
              <Button
                size="small"
                type="primary"
                loading={presetOpLoading === "save"}
                disabled={!!presetOpLoading}
                onClick={handleSavePreset}
              >保存当前为预设</Button>
              {selectedPresetId && (
                <>
                  <Button
                    size="small"
                    loading={presetOpLoading === "setDefault"}
                    disabled={!!presetOpLoading}
                    onClick={() => handleSetDefaultPreset(selectedPresetId)}
                  >设为默认</Button>
                  <Button
                    size="small"
                    danger
                    loading={presetOpLoading === "delete"}
                    disabled={!!presetOpLoading}
                    onClick={() => handleDeletePreset(selectedPresetId)}
                  >删除</Button>
                </>
              )}
            </Space>
          </Card>
          <Collapse
            defaultActiveKey={["basic", "body", "headings"]}
            size="small"
            style={{ marginBottom: 8 }}
            items={[
              {
                key: "basic",
                label: "基本设置",
                children: (
                  <>
                  <Row gutter={[12, 0]}>
                    {/* 封面页 + 目录页 同行 */}
                    <Col span={12}>
                      <Form.Item name="show_title_page" label="封面页" valuePropName="checked" style={{ marginBottom: 8 }}>
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="show_toc" label="目录页" valuePropName="checked" style={{ marginBottom: 8 }}>
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    {/* 页眉 + 页脚 同行 */}
                    <Col span={12}>
                      <Form.Item name="page_header" label="页眉" style={{ marginBottom: 8 }}>
                        <Input size="small" placeholder="如：XX项目深基坑专项方案" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="page_footer" label="页脚" style={{ marginBottom: 8 }}>
                        <Input size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="bidder_name" label="编制单位" style={{ marginBottom: 8 }}>
                        {/* ⚠️ 2026-10-01 定位切换：label 由「投标单位」改为「编制单位」——
                            专项施工方案的封面主体是编制/施工单位，不是投标人。
                            字段名 bidder_name 保持不变 —— 它是导出 config 的
                            历史契约（export.py 参数名 + 已存配置），改名字段会
                            让老方案的封面单位变成空白（AGENTS.md §3.2 禁止破坏契约）。
                            真正该补的是「编制/审核/批准人」三个岗位字段（见遗留项 L-0）。 */}
                        <Input size="small" placeholder="如：XX建设有限公司" />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item name="show_page_number" label="显示页码" valuePropName="checked" style={{ marginBottom: 8 }}>
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item
                        name="allow_pil_fallback"
                        label="PIL兜底渲染"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="默认关闭：图表仅用 mermaid 原生引擎渲染（前端预览同款/后端高清服务），失败图表默认整张跳过且不占图号（成稿不会出现红字报错段，可配合「渲染失败占位」开关定位失败图）。开启后失败图表改用内置 PIL 渲染器兜底（版式与预览略有差异）。总平面布置图/里程碑时间线始终使用内置渲染器，不受此开关影响。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    {/* ✅ 新增：章节分页 / 页码样式 / 目录层级 */}
                    <Col span={8}>
                      <Form.Item
                        name="chapter_page_break"
                        label="章节另起一页"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="开启后每个一级章节从新页开始（专项方案标准版式）。首个章节不会额外分页，封面与目录之后也不会多出空白页。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="page_number_style"
                        label="页码样式"
                        style={{ marginBottom: 8 }}
                        tooltip="需要「显示页码」开启后生效。Word 打开文档时会自动刷新页码域。"
                      >
                        <Select size="small" options={[
                          { label: "简单（- 1 -）", value: "simple" },
                          { label: "第 X 页 / 共 Y 页", value: "page_of_total" },
                        ]} />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="toc_depth"
                        label="目录收录层级"
                        style={{ marginBottom: 8 }}
                        tooltip="自动目录收录到第几级标题，默认 3 级（章 / 节 / 小节）。"
                      >
                        <Select size="small" options={[
                          { label: "1 级", value: 1 },
                          { label: "2 级", value: 2 },
                          { label: "3 级", value: 3 },
                          { label: "4 级", value: 4 },
                          { label: "5 级", value: 5 },
                        ]} />
                      </Form.Item>
                    </Col>
                  </Row>
                  {/* ✅ 缺口修复（2026-09-24）：后端 export.py 已支持 5 个容错/增强开关，
                      但前端表单从未下发 —— 用户无法关闭「导出时自动生成配图」、
                      无法开启「渲染失败仍占位」「正文自动改写」「未闭合代码块自动修复」
                      「标题边框」，只能被迫接受后端默认值。现补齐下发通道，
                      默认值与后端 config.get 默认逐项一致（见 export.py
                      2905 / 3025 / 3054 / 3079 / 3303 / 3305），旧行为不变。 */}
                  <Divider orientation="left" style={{ margin: "4px 0 8px" }}>容错与增强</Divider>
                  <Row gutter={[12, 0]}>
                    <Col span={8}>
                      <Form.Item
                        name="ai_image_auto_generate"
                        label="自动生成配图"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="导出时为正文中的「配图」占位自动生成插图（后端默认开启）。关闭则保留占位描述、不调用生图服务（可节省配额与时间）。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="chart_fail_placeholder"
                        label="渲染失败占位"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="开启后渲染失败的图表保留占位（红字图题「图 X-Y — 渲染失败」，图号照占）便于排查定位；默认关闭时失败图表整张跳过且图号回收，交付文档不出现红字。仅影响渲染失败降级方式，不影响正常图表。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="auto_rewrite_content"
                        label="正文自动改写"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="导出时对正文做规范化改写（清理残留标记/口语化表述）。默认关闭：导出内容以生成稿为准。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="auto_fix_unclosed_fences"
                        label="修复未闭合代码块"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="导出前尝试补齐正文里未闭合的代码围栏（AI 截断导致），避免图表/公式整段丢失。默认关闭。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item
                        name="heading_border"
                        label="标题边框"
                        valuePropName="checked"
                        style={{ marginBottom: 8 }}
                        tooltip="为各级标题添加边框（部分送审版式要求）。默认关闭。"
                      >
                        <Switch size="small" />
                      </Form.Item>
                    </Col>
                  </Row>
                  </>
                ),
              },
              {
                key: "cover",
                label: "封面与页面",
                children: (
                  <>
                    <Divider orientation="left" style={{ margin: "4px 0 8px" }}>页边距（cm）</Divider>
                    <Row gutter={[12, 0]}>
                      <Col span={6}>
                        <Form.Item name={["margins", "top"]} label="上" style={{ marginBottom: 8 }}>
                          <InputNumber size="small" min={1} max={5} step={0.1} style={{ width: "100%" }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name={["margins", "bottom"]} label="下" style={{ marginBottom: 8 }}>
                          <InputNumber size="small" min={1} max={5} step={0.1} style={{ width: "100%" }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name={["margins", "left"]} label="左" style={{ marginBottom: 8 }}>
                          <InputNumber size="small" min={1} max={5} step={0.1} style={{ width: "100%" }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name={["margins", "right"]} label="右" style={{ marginBottom: 8 }}>
                          <InputNumber size="small" min={1} max={5} step={0.1} style={{ width: "100%" }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Divider orientation="left" style={{ margin: "4px 0 8px" }}>封面信息（需开启「封面页」）</Divider>
                    <Row gutter={[12, 0]}>
                      <Col span={12}>
                        <Form.Item name={["cover_info", "工程名称"]} label="工程名称" style={{ marginBottom: 8 }}>
                          <Input size="small" placeholder="如：XX项目深基坑工程" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item name={["cover_info", "编制单位"]} label="编制单位" style={{ marginBottom: 8 }}>
                          <Input size="small" placeholder="如：XX建设集团有限公司" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item name={["cover_info", "方案编号"]} label="方案编号" style={{ marginBottom: 8 }}>
                          <Input size="small" placeholder="如：FA-2026-001" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item name={["cover_info", "版本"]} label="版本" style={{ marginBottom: 8 }}>
                          <Input size="small" placeholder="如：A版 / V1.0" />
                        </Form.Item>
                      </Col>
                    </Row>
                    {/* ✅ 2026-10-02（第二十三轮 · L-0）：专项施工方案法定前置表单。
                        依据住建部令第37号第十一条（审核/审查签字盖章）、
                        第十二条（专家不得少于 5 名）、第十三条（论证报告三选一结论），
                        以及建办质〔2018〕31号 第九条（计算书及相关施工图纸）。
                        全部默认关闭（向后兼容，开启后产物新增对应前置页）。 */}
                    <Divider orientation="left" style={{ margin: "4px 0 8px" }}>专项施工方案法定前置表单</Divider>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 8, fontSize: 12 }}
                      message="签字栏一律留空，由各方线下手签；系统不预填人名与证书编号。"
                      description="审批表依据住建部令第37号第十一条；专家论证报告仅在「超过一定规模的危大工程」时需要，论证结论为通过 / 修改后通过 / 不通过三选一。"
                    />
                    <Row gutter={[12, 0]}>
                      <Col span={12}>
                        <Form.Item
                          name={["scheme_forms", "compilation_note"]}
                          label="编制说明"
                          valuePropName="checked"
                          style={{ marginBottom: 8 }}
                        >
                          <Switch size="small" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          name={["scheme_forms", "approval"]}
                          label="专项施工方案审批表"
                          valuePropName="checked"
                          style={{ marginBottom: 8 }}
                        >
                          <Switch size="small" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          name={["scheme_forms", "expert_review"]}
                          label="专家论证报告"
                          valuePropName="checked"
                          style={{ marginBottom: 8 }}
                        >
                          <Switch size="small" />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          name={["scheme_forms", "drawing_appendix"]}
                          label="施工图纸附件清单"
                          valuePropName="checked"
                          style={{ marginBottom: 8 }}
                        >
                          <Switch size="small" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: "body",
                label: "正文样式",
                children: (
                  <Row gutter={[12, 0]}>
                    {/* 正文字体 + 正文字号 同行 */}
                    <Col span={12}>
                      <Form.Item name="font_name" label="正文字体" style={{ marginBottom: 8 }}>
                        <Select size="small" options={[
                          { label: "微软雅黑", value: "微软雅黑" },
                          { label: "宋体", value: "宋体" },
                          { label: "仿宋", value: "仿宋" },
                          { label: "黑体", value: "黑体" },
                        ]} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name="font_size" label="正文字号" style={{ marginBottom: 8 }}>
                        <Select size="small" options={[
                          { label: "小四 (12pt)", value: 12 },
                          { label: "五号 (10.5pt)", value: 10.5 },
                          { label: "四号 (14pt)", value: 14 },
                          { label: "小三 (15pt)", value: 15 },
                        ]} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name="line_spacing"
                        label="正文行距"
                        style={{ marginBottom: 8 }}
                        tooltip="正文段落行距，首行自动缩进 2 个字符（随字号自适应）。"
                      >
                        <Select size="small" options={[
                          { label: "单倍 (1.0)", value: 1 },
                          { label: "1.15 倍", value: 1.15 },
                          { label: "1.5 倍（推荐）", value: 1.5 },
                          { label: "1.75 倍", value: 1.75 },
                          { label: "双倍 (2.0)", value: 2 },
                        ]} />
                      </Form.Item>
                    </Col>
                    {/* ✅ 新增（2026-10-11 · 《文档生成规则》4.2 导出标准）：版式预设，
                        一键整体切换行距 + 四边距；已在「正文样式」里手动细调的
                        边距值不被预设强改（后端 _resolve_layout_preset 补缺不覆盖）。 */}
                    <Col span={12}>
                      <Form.Item
                        name="layout_preset"
                        label="版式预设"
                        style={{ marginBottom: 8 }}
                        tooltip="选择后整体应用配套的正文行距与页边距（如工程公文：上下 2.54cm / 左右 3.18cm / 1.5 倍行距）。默认「不启用」，产物版式与既往完全一致；你在「页边距」里显式改过的数值不会被预设覆盖。"
                      >
                        <Select size="small" allowClear options={[
                          { label: "不启用（保持标准版式）", value: "" },
                          { label: "工程公文（上下2.54 / 左右3.18 / 1.5倍）", value: "engineering" },
                        ]} />
                      </Form.Item>
                    </Col>
                  </Row>
                ),
              },
              {
                key: "headings",
                label: "各级标题样式",
                children: (
                  <>
                    {[1, 2, 3, 4, 5, 6, 7].map((lvl) => (
                      <div key={lvl} style={{
                        display: "flex", gap: 8, alignItems: "center", marginBottom: 4,
                      }}>
                        <Tag style={{ minWidth: 48, textAlign: "center", marginInlineEnd: 0 }}>{lvl}级标题</Tag>
                        <Form.Item
                          name={["heading_styles", String(lvl), "font_name"]}
                          style={{ flex: 1, marginBottom: 0 }}
                        >
                          <Select size="small" options={[
                            { label: "宋体", value: "宋体" },
                            { label: "微软雅黑", value: "微软雅黑" },
                            { label: "黑体", value: "黑体" },
                            { label: "仿宋", value: "仿宋" },
                          ]} />
                        </Form.Item>
                        <Form.Item
                          name={["heading_styles", String(lvl), "font_size"]}
                          style={{ width: 100, marginBottom: 0 }}
                        >
                          <Select size="small" options={[
                            { label: "三号 (16pt)", value: 16 },
                            { label: "小三 (15pt)", value: 15 },
                            { label: "四号 (14pt)", value: 14 },
                            { label: "小四 (12pt)", value: 12 },
                            { label: "五号 (10.5pt)", value: 10.5 },
                          ]} />
                        </Form.Item>
                        <Form.Item
                          name={["heading_styles", String(lvl), "bold"]}
                          style={{ width: 70, marginBottom: 0 }}
                          valuePropName="checked"
                        >
                          <Switch size="small" checkedChildren="加粗" unCheckedChildren="常规" />
                        </Form.Item>
                      </div>
                    ))}
                  </>
                ),
              },
            ]}
          />
          {exporting && exportPhase && (
            <Alert
              type="info"
              showIcon
              message={exportPhase}
              // ✅ D2：真实进度条 + 取消入口。
              //    旧实现只有一句静态文案，文档生成阶段（可达数十秒）零进度反馈。
              description={
                <div>
                  <Progress
                    percent={exportPct}
                    size="small"
                    status="active"
                    style={{ marginBottom: 6 }}
                  />
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span>
                      <Tag color="processing">
                        阶段 {EXPORT_STAGE_ORDER[exportStage] ?? 1}/3 ·{" "}
                        {EXPORT_STAGE_LABEL[exportStage]}
                      </Tag>
                      大文档导出可能需要 10-30 秒，图表渲染和文档构建在后台进行，请耐心等待。
                    </span>
                    <Button size="small" onClick={handleCancelExport}>
                      取消导出
                    </Button>
                  </div>
                </div>
              }
              style={{ marginBottom: 12 }}
            />
          )}
          {/* ✅ 2026-10-01：审核 / 预检不再作为导出的前置条件，此处只做非阻断提示。
              未完成预检、存在 high 问题、就绪度未放行均不影响导出按钮。 */}
          {exportGate.highIssueCount > 0 && (
            <Alert
              type="warning"
              showIcon
              message={exportGate.reason}
              description="审核与预检结论仅供参考，不限制导出；如用于正式交付，建议先整改上述问题。"
              action={<Button size="small" onClick={() => setActiveTab("review")}>前往审核与预检</Button>}
              style={{ marginBottom: 12 }}
            />
          )}
          <Space>
            <Button
              type="primary"
              icon={<ExportOutlined />}
              onClick={() => handleExport("docx")}
              loading={exporting}
              title="导出 DOCX（不受审核与预检状态限制）"
            >
              {exporting ? "导出中..." : "导出 DOCX"}
            </Button>
            <Button
              icon={<FilePdfOutlined />}
              onClick={() => handleExport("pdf")}
              loading={exporting}
              title="先生成 DOCX 再转换为 PDF（需安装 Microsoft Word 或 LibreOffice）"
            >
              导出 PDF
            </Button>
          </Space>

          {/* ✅ 功能归位：目录结构导出也是"导出"，从「目录生成」页收敛到本页，
              让导出类操作集中在一处（目录生成页专注编辑）。 */}
          <Card
            size="small"
            title={
              <Space>
                <ExportOutlined />
                <Text strong>目录结构导出</Text>
              </Space>
            }
            style={{ marginTop: 12 }}
            extra={
              <Button
                size="small"
                icon={<ExportOutlined />}
                onClick={handleExportTree}
                loading={exportingTree}
                disabled={generating || exporting || tree.length === 0}
              >
                导出目录 JSON
              </Button>
            }
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              导出按后端权威编号（如 1 / 1.1 / 1.1.1）整理的目录树 JSON，可直接给外部工具或存档使用，<b>不改动数据库</b>。
              与上方 DOCX / PDF 文档导出是两种产物：这里是"目录文件"，上方是"完整方案文档"。
            </Text>
          </Card>

          {/* ===== ✅ 导出缓存状态（exportApi.cacheStatus）===== */}
          <Card
            size="small"
            title="导出缓存"
            style={{ marginTop: 12 }}
            extra={
              <Button size="small" icon={<ReloadOutlined />} loading={cacheLoading} onClick={loadCacheStatus}>
                查询缓存
              </Button>
            }
          >
            {!cacheStatus ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                点击「查询缓存」查看该方案可复用的历史导出结果。内容与配置未变时会直接命中缓存，秒级返回。
              </Text>
            ) : (
              <>
                <Space style={{ marginBottom: 8 }} wrap>
                  <Tag color={cacheStatus.total > 0 ? "green" : "default"}>
                    有效缓存 {cacheStatus.total} 条
                  </Tag>
                  {cacheStatus.stale > 0 && (
                    <Tooltip title="结果文件已被保留策略（最多 5 份）清理、磁盘文件已丢失，或为导出失败遗留的孤儿产物。下次导出会自动清理这些记录，无需手工处理。">
                      <Tag color="orange">失效 {cacheStatus.stale} 条</Tag>
                    </Tooltip>
                  )}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    变更版本号或导出配置即视为未命中
                  </Text>
                </Space>
                {cacheStatus.items?.length > 0 ? (
                  <Table
                    size="small"
                    rowKey="id"
                    pagination={false}
                    dataSource={cacheStatus.items}
                    columns={[
                      { title: "状态", dataIndex: "exists", width: 72,
                        render: (ok: boolean) =>
                          // ✅ 逐行标注有效/失效：失效行（文件已被保留策略清理或
                          //    为导出失败遗留的孤儿产物）与有效行不再长得一样，
                          //    避免用户误以为任何一行都能秒级复用。
                          <Tag color={ok ? "green" : "default"}>{ok ? "有效" : "失效"}</Tag> },
                      { title: "生成时间", dataIndex: "created_at", width: 170,
                        render: (v: string) => (v || "").replace("T", " ").slice(0, 19) },
                      { title: "指纹", dataIndex: "content_fingerprint", width: 140,
                        render: (v: string) => (
                          <Tooltip title={v}>
                            <Text code style={{ fontSize: 11 }}>{(v || "").slice(0, 12)}…</Text>
                          </Tooltip>
                        ) },
                      { title: "产物路径", dataIndex: "result_path", ellipsis: true,
                        render: (v: string) => (
                          <Tooltip title={v}>
                            <Text style={{ fontSize: 11 }}>{(v || "").split(/[\\/]/).pop() || "—"}</Text>
                          </Tooltip>
                        ) },
                    ]}
                  />
                ) : (
                  <Text type="secondary" style={{ fontSize: 12 }}>暂无有效缓存</Text>
                )}
              </>
            )}
          </Card>
        </Form>
      ),
    },
  ];

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        // ✅ 直接填满 Content 层给的高度锚点（calc(100vh - 64px)）
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
      }}
    >
      {/* 顶部标题（单行紧凑版：方案名/状态标签与字数统计合并为一行，高度约为原两行布局的一半；
          方案名超长时省略号截断，字数统计始终可见） */}
      {scheme && (
        <div
          style={{
            marginBottom: 8,
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            gap: 12,
            minHeight: 0,
          }}
        >
          <Title
            level={4}
            style={{
              margin: 0,
              fontSize: 15,
              lineHeight: 1.3,
              flex: 1,
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {scheme.name}
            {scheme.type && <Tag color="blue" style={{ marginLeft: 8 }}>{scheme.type}</Tag>}
            <Tag color={scheme.status === "已完成" ? "green" : "default"}>{scheme.status}</Tag>
            {generating && genType === "content" && (
              <Tag color="processing">📝 正在生成正文...</Tag>
            )}
          </Title>
          <Text
            type="secondary"
            style={{ fontSize: 12, lineHeight: 1.3, flexShrink: 0, marginLeft: "auto" }}
          >
            目标字数：{scheme.word_budget || 0} · 当前字数：{scheme.word_count || 0}
          </Text>
        </div>
      )}

      {/* 生成进度区（页面唯一进度条）：目录/正文共用，左侧进度 + 右侧实时工作日志 */}
      {/* ✅ 全局事实提取有自己的专用进度卡（在事实 Tab 内），此处不再重复渲染空章节日志 */}
      {generating && genType !== "facts" && (
        <GenerationProgressCard
          genType={genType}
          progress={progress}
          progressMsg={progressMsg}
          sectionLogs={sectionLogs}
          outlinePhase={outlinePhase}
          outlineDoneTotal={outlineDoneTotal}
          outlineLogs={outlineLogs}
          stats={genStats}
        />
      )}

      {/* 主体：左侧导航面板 + 右侧 Tab 内容（flex:1 占满剩余高度，随进度区动态收缩） */}
      {/* 2026-09-23：import（解析提取）模块隐藏目录树——此时还没生成目录，章节树无意义；
          全局事实（facts）模块同样隐藏目录树，改为左侧「事实分类」面板（点击分类
          在右侧项目资料下方显示该分类提取结果） */}
      <div style={{ display: "flex", gap: 12, flex: 1, minHeight: 0 }}>
        {/* 左侧固定目录树（P0-5 memo 组件）—— 仅在有目录依赖的模块显示，import/facts 模块隐藏 */}
        {activeTab !== "import" && activeTab !== "facts" && (
        <SchemeTreePanel
          tree={tree}
          treeData={treeData}
          expandedKeys={expandedKeys}
          selectedSectionKey={selectedSection ? selectedSection.key : undefined}
          loading={loading}
          generating={generating}
          onRefresh={load}
          onToggleExpand={handleToggleExpand}
          onGoToOutline={handleGoToOutline}
          onSelectSection={handleTreeSelect}
          onExpand={handleTreeExpand}
          moveFlags={moveFlags}
          onMoveNode={moveNode}
          onRenameNode={renameNode}
          onDeleteNode={deleteNode}
          onAddChildNode={addChildNode}
          onAddSiblingNode={addSiblingNode}
          onSaveOutline={saveOutlineTree}
          renamingKey={renamingKey}
          renamingValue={renamingValue}
          setRenamingKey={setRenamingKey}
          setRenamingValue={setRenamingValue}
          scrollRef={leftTreeScrollRef}
        />
        )}

        {/* ✅ 全局事实 Tab 左侧面板：提取结果分类（替代目录树），点击 → 右侧项目资料下方展示该分类详情 */}
        {activeTab === "facts" && (
          <FactsCategoryPanel
            entries={factCategoryEntries}
            totalItems={factsSummary?.total || 0}
            selected={activeFactCategory}
            onSelect={setSelectedFactCategory}
          />
        )}

        {/* 右侧 Tab 内容（内部滚动：各 Tab 长内容不再撑高整体布局） */}
        <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", background: "#fff", border: "1px solid #f0f0f0", borderRadius: 8, padding: 16, overflow: "hidden" }}>
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={tabItems}
            className="swb-tabs-flex"
            style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
            tabBarStyle={{ flexShrink: 0, marginBottom: 4 }}
          />
        </div>
      </div>

      {/* ===== ✅ 全局事实新增 / 编辑 ===== */}
      <Modal
        title={factEditingId ? "编辑事实分组" : "新增事实分组"}
        open={factModalOpen}
        forceRender
        onOk={submitFact}
        confirmLoading={factSubmitting}
        okText={factEditingId ? "保存" : "新增"}
        cancelText="取消"
        onCancel={() => {
          // ✅ 修复：旧实现不重置 factEditingCategory —— 上次「编辑分组」的
          //    分类会残留，下次「新增分组」若用户不选分类，submitFact 会兜底
          //    使用旧分类，新分组被误继承归类。
          setFactModalOpen(false);
          setFactEditingId(null);
          setFactEditingCategory("");
        }}
        width={640}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="此处内容会作为全篇锚点，正文生成时用于保持工期、参数、设备型号等事实一致。建议用 Markdown 无序列表逐条书写。"
        />
        <Form form={factForm} layout="vertical">
          <Form.Item name="title" label="分组名称" rules={[{ required: true, message: "请输入分组名称" }]}>
            <Input placeholder="如：工期安排 / 技术路线与设备配置" />
          </Form.Item>
          <Form.Item
            name="category"
            label="事实分类"
            tooltip="用于事实清单归类与正文注入顺序；留空按「其他事实」处理"
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择分类（可选）"
              options={factCategoryOptions}
            />
          </Form.Item>
          <Form.Item name="content" label="事实内容" rules={[{ required: true, message: "请输入事实内容" }]}>
            <Input.TextArea
              rows={10}
              placeholder={"- 本方案项目总工期为 90 日历天\n- 挖掘机选用 PC200-8 型"}
              style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ===== ✅ 单条事实编辑（item_updates，不重建分组）===== */}
      <Modal
        title="编辑事实条目"
        open={factItemModalOpen}
        forceRender
        onOk={submitFactItem}
        confirmLoading={factItemSubmitting}
        okText="保存"
        cancelText="取消"
        onCancel={() => { setFactItemModalOpen(false); setFactItemEditing(null); }}
        width={560}
      >
        {factItemEditing?.groupTitle && (
          <div style={{ fontSize: 12, color: "#999", marginBottom: 8 }}>
            所属分组：{factItemEditing.groupTitle}
          </div>
        )}
        <Form form={factItemForm} layout="vertical">
          <Form.Item name="name" label="事实名称" rules={[{ required: true, message: "请输入事实名称" }]}>
            <Input placeholder="如：项目总工期" />
          </Form.Item>
          <Form.Item name="value" label="事实值" rules={[{ required: true, message: "请输入事实值" }]}>
            <Input.TextArea rows={3} placeholder="如：90 日历天" />
          </Form.Item>
          <Form.Item name="category" label="事实分类">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择分类（可选）"
              options={factCategoryOptions}
            />
          </Form.Item>
          <Form.Item
            name="is_simulated"
            label="模拟值"
            valuePropName="checked"
            tooltip="标记为模拟值后，该事实会回到「待审核」，未经人工确认不会注入正文"
          >
            <Switch checkedChildren="模拟值" unCheckedChildren="实际值" />
          </Form.Item>
          {/* ✅ R81：要求 vs 事实口径（与后端 value_kind 白名单同源） */}
          <Form.Item
            name="value_kind"
            label="事实口径"
            tooltip="本方案口径：本方案已采用的值，正文可直接引用；要求口径：招标/规范要求原文，正文引用时须写「满足/响应要求」，不得当作既定值"
          >
            <Select
              allowClear
              placeholder="未区分（历史行默认）"
              options={FACT_VALUE_KIND_OPTIONS}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ===== ✅ 目录库选择弹窗：套用到当前方案 ===== */}
      <Modal
        title="从目录库套用（仅显示已审核通过的目录）"
        open={libOpen}
        onCancel={() => setLibOpen(false)}
        footer={null}
        width={720}
      >
        <Space style={{ marginBottom: 12 }}>
          <Input
            placeholder="按名称搜索..."
            value={libKeyword}
            onChange={(e) => setLibKeyword(e.target.value)}
            allowClear
            style={{ width: 280 }}
          />
          <Button
            icon={<ReloadOutlined />}
            onClick={async () => {
              setLibLoading(true);
              try {
                const { data } = await outlineLibraryApi.list({
                  review_status: "已通过",
                  ...(libKeyword ? { keyword: libKeyword } : {}),
                });
                setLibItems(data.items || []);
                setLibError(
                  (data.items || []).length === 0
                    ? "没有匹配的目录库（可能是全部未审核通过）"
                    : ""
                );
              } catch (e: unknown) {
                const err = e as { message?: string };
                setLibError(err.message || "加载失败");
              } finally {
                setLibLoading(false);
              }
            }}
          >
            搜索
          </Button>
        </Space>

        {libError && (
          <Alert type="warning" showIcon message={libError} style={{ marginBottom: 12 }} />
        )}

        <Spin spinning={libLoading || libApplying}>
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {libItems.length === 0 && !libLoading ? (
              <Empty description="暂无可套用的目录库" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <List
                size="small"
                dataSource={libItems}
                renderItem={(lib) => (
                  <List.Item
                    actions={[
                      <Button
                        key="apply"
                        size="small"
                        type="primary"
                        icon={<AimOutlined />}
                        onClick={() => handleApplyLibrary(lib)}
                      >
                        套用
                      </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      title={
                        <Space wrap>
                          <span style={{ fontWeight: 500 }}>{lib.name}</span>
                          {lib.version && <Tag color="blue">{lib.version}</Tag>}
                          {lib.type && <Tag>{lib.type}</Tag>}
                          {(lib.ref_count || 0) > 0 && <Tag color="green">引用 {lib.ref_count}</Tag>}
                        </Space>
                      }
                      description={
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {lib.engineering_type || "通用"} · {lib.profession || "不限专业"}
                          {lib.source ? ` · 来源：${lib.source}` : ""}
                        </Text>
                      }
                    />
                  </List.Item>
                )}
              />
            )}
          </div>
        </Spin>
      </Modal>

      {/* ===== ✅ 上传识别结果存为目录库 ===== */}
      <Modal
        title="存为目录库"
        open={saveLibOpen}
        forceRender
        onOk={submitSaveAsLibrary}
        okText="入库（待审核）"
        cancelText="取消"
        onCancel={() => setSaveLibOpen(false)}
        width={520}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="入库后状态为「待审核」，需在「专项方案目录库」页审核通过后才能被其他方案套用。"
        />
        <Form form={saveLibForm} layout="vertical">
          <Form.Item name="name" label="目录库名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="如：深基坑标准目录" />
          </Form.Item>
        </Form>
        {lastUpload && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            来源文件：{lastUpload.name} · 共 {countOutline(lastUpload.outline)} 个章节
          </Text>
        )}
      </Modal>

      {/* ===== ✅ 上传解析：解析内容预览（前 N 字，不下载整份文件） ===== */}
      <Modal
        title={previewDoc ? `解析内容预览 · ${(previewDoc as { file_name?: string }).file_name}` : "解析内容预览"}
        open={!!previewDoc}
        onCancel={() => setPreviewDoc(null)}
        footer={null}
        width={760}
      >
        <Spin spinning={previewLoading}>
          {/* ✅ 解析可信度提示：区分「文档内容真的不完整」（warning，需行动）
              与「只是本弹窗没显示全」（info，完整内容仍在库里），二者互不覆盖 */}
          {!previewLoading && previewData?.is_parsed && (
            <div style={{ marginBottom: 12 }}>
              {previewNotices(previewData).map((n, i) => (
                <Alert
                  key={i}
                  type={n.kind}
                  showIcon
                  message={n.title}
                  description={n.detail}
                  style={{ marginBottom: 8 }}
                />
              ))}
            </div>
          )}
          {/* ✅ 四层存储解析质量概览 + 管线操作（修复 docPipelineApi/组件此前无调用点的断链） */}
          <div style={{ marginBottom: 12 }}>
            <DocumentPipelineSummary
              loading={pipelineLoading}
              status={pipelineStatus}
              error={pipelineError}
              onAction={handlePipelineAction}
              actionBusy={pipelineAction}
            />
          </div>
          {previewText ? (
            <div
              className="markdown-body"
              style={{ maxHeight: "60vh", overflowY: "auto", whiteSpace: "pre-wrap", fontSize: 13 }}
            >
              {previewText}
            </div>
          ) : (
            !previewLoading && <Empty description="该文档尚未解析，请先执行解析" />
          )}
        </Spin>
      </Modal>

      {/* ===== ✅ 提取项目：启动配置（模式 / 勾选项 / 强制重跑） ===== */}
      <Modal
        title="结构化提取配置"
        open={baConfigOpen}
        onOk={handleStartBaFromConfig}
        okText="开始提取"
        cancelText="取消"
        onCancel={() => setBaConfigOpen(false)}
        width={640}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
            <Text strong>提取范围：</Text>
            <Radio.Group
              value={baModeOpt}
              onChange={(e) => setBaModeOpt(e.target.value)}
              style={{ marginLeft: 8 }}
            >
              {/* 项数随后端 /items 定义动态生成（旧实现硬编码 17/18）；未加载时不报数 */}
              <Radio.Button value="key">必选项{baDefs.length ? `（${baDefs.filter(d => d.required).length} 项）` : ""}</Radio.Button>
              <Radio.Button value="full">全部{baDefs.length ? `（${baDefs.length} 项）` : ""}</Radio.Button>
              <Radio.Button value="custom">自定义勾选</Radio.Button>
            </Radio.Group>
          </div>
          {baModeOpt === "custom" && (
            <div style={{ maxHeight: 320, overflowY: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: 8 }}>
              {normalizeBaGroups(baGroups, baDefs).map((g) => (
                <div key={g.key} style={{ marginBottom: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{g.label}</Text>
                  <Checkbox.Group
                    style={{ display: "flex", flexDirection: "column", paddingLeft: 12 }}
                    value={baSelectedIds}
                    onChange={(vals) => setBaSelectedIds(vals as string[])}
                    options={g.items.map((d) => ({
                      label: (d.required ? "★ " : "") + (d.label || d.item_id),
                      value: d.item_id,
                    }))}
                  />
                </div>
              ))}
            </div>
          )}
          <div>
            <Checkbox
              checked={baForceRerun}
              onChange={(e) => setBaForceRerun(e.target.checked)}
            >
              强制重跑（清空本次所选解析项的已有结果后重新提取）
            </Checkbox>
          </div>
          <Alert
            type="info"
            showIcon
            message="提取结果为项目级共享：同项目下的多个方案共用一套提取结果；单项可稍后在列表行内单独重跑。"
          />
        </Space>
      </Modal>

      {/* ===== ✅ 提取项目：查看完整结果（与右侧阅读区共用渲染器） ===== */}
      <Modal
        title={baFullItem ? `${baFullItem.label || baFullItem.item_id} · 完整内容` : "完整内容"}
        open={!!baFullItem}
        onCancel={() => setBaFullItem(null)}
        footer={null}
        width={820}
      >
        {baFullItem && (
          <div style={{ maxHeight: "65vh", overflowY: "auto" }}>
            <BidAnalysisItemBody item={baFullItem} />
          </div>
        )}
      </Modal>

      {/* ===== ✅ 提取项目：人工校正（直接改写单项提取结果，source='manual'）=====
          AI 抽错关键参数（基坑深度、支护形式、工期）时，旧实现唯一出路是整项重跑
          （贵且不稳定），抽错的值会继续被下游 format_downstream_context 消费。
          此弹窗是唯一可靠出口：校正值落库后标记 source='manual'，下游优先使用。
          入口：「提取项目」Tab 与「上传解析」→「解析信息分类显示栏」右侧详情。 */}
      <Modal
        title={baEditItem ? `人工校正：${baEditItem.label || baEditItem.item_id}` : "人工校正"}
        open={!!baEditItem}
        onCancel={() => setBaEditItem(null)}
        onOk={handleBaSaveEdit}
        okText="保存校正"
        confirmLoading={baEditSaving}
        cancelText="取消"
        width={760}
      >
        {baEditItem && (
          <div>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="人工校正结果将标记为「人工校正」，下游目录/正文生成优先使用该校正值"
              description={
                baEditItem.output_type === "json"
                  ? "该项为 JSON 结构（项目级基本信息），请提交合法 JSON 对象/数组。"
                  : "该项为 Markdown 文本，可直接编辑。"
              }
            />
            <div style={{ marginBottom: 8 }}>
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={baEditSaving}
                onClick={handleBaClearEdit}
              >
                撤销校正并清空（回退到待提取）
              </Button>
            </div>
            <Input.TextArea
              value={baEditValue}
              onChange={(e) => setBaEditValue(e.target.value)}
              autoSize={{ minRows: 10, maxRows: 24 }}
              style={{ fontFamily: "monospace" }}
              placeholder={baEditItem.output_type === "json" ? "{ ... }" : "请输入校正后的内容"}
            />
          </div>
        )}
      </Modal>

      {/* ===== ✅ 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR §6）===== */}
      <Drawer
        title={
          <Space>
            <SyncOutlined />
            <span>全文一致性 Agent 修复工作台</span>
            {crSummary && (
              <Tag color={(crSummary.high ?? 0) > 0 ? "red" : (crSummary.total ?? 0) > 0 ? "orange" : "green"}>
                冲突 {crSummary.total ?? 0}
              </Tag>
            )}
          </Space>
        }
        open={crOpen}
        onClose={() => setCrOpen(false)}
        width={1000}
        styles={{ body: { padding: 16 } }}
      >
        <Spin spinning={crLoading}>
          {/* 概要 + 操作 */}
          <Card size="small" style={{ marginBottom: 12 }}>
            <Space size={16} wrap>
              <Text strong>{scheme?.name || "当前方案"}</Text>
              {crScanId && <Text type="secondary" style={{ fontSize: 12 }}>扫描号：{crScanId}</Text>}
              {crSummary && (
                <>
                  <Tag>冲突总数 {crSummary.total ?? 0}</Tag>
                  <Tag color="red">高 {crSummary.high ?? 0}</Tag>
                  <Tag color="orange">中 {crSummary.medium ?? 0}</Tag>
                  <Tag color="blue">低 {crSummary.low ?? 0}</Tag>
                  <Tag>待处理 {crSummary.pending ?? 0}</Tag>
                  <Tag>待人工 {crSummary.skipped ?? 0}</Tag>
                </>
              )}
            </Space>
            <div style={{ marginTop: 10 }}>
              <Space wrap>
                <Button
                  type="primary"
                  icon={<FileSearchOutlined />}
                  loading={crScanning}
                  onClick={handleCrScan}
                >
                  开始扫描
                </Button>
                {/* ✅ R168（FIX-10）：扫描/修复是 10 分钟级 AI 长任务，补取消入口
                    （口径与 R106 FIX-5 的三个预检取消按钮一致，运行中才出现） */}
                {crScanning && (
                  <Button danger onClick={handleCancelCrScan}>取消扫描</Button>
                )}
                <Button
                  type="primary"
                  ghost
                  icon={<SyncOutlined />}
                  loading={crRepairing}
                  disabled={crConflicts.length === 0}
                  onClick={() => handleCrRepair()}
                >
                  一键修复（{consistencySeverity === "high" ? "仅高危"
                    : consistencySeverity === "medium" ? "中及以上" : "全部"}）
                </Button>
                {crRepairing && (
                  <Button danger onClick={handleCancelCrRepair}>取消修复</Button>
                )}
                {/* ✅ 强制全量重修（2026-09-22）：默认跳过「已修复且成果仍在」的冲突，
                    勾选后本批全部重新调用 AI 修复 */}
                <Checkbox
                  checked={forceFullRepair}
                  onChange={(e) => setForceFullRepair(e.target.checked)}
                >
                  强制全量重修
                </Checkbox>
                <Button
                  disabled={!(crRepair?.items || []).some((i) => i.status === "repaired")}
                  onClick={() => handleCrConfirm(crRepairItemIds("repaired"), [])}
                >
                  全部接受
                </Button>
                <Button
                  danger
                  disabled={!(crRepair?.items || []).some((i) => i.status === "repaired")}
                  onClick={() => handleCrConfirm([], crRepairItemIds("repaired"))}
                >
                  全部拒绝
                </Button>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => { loadCrConflicts(); loadCrHistory(); }}
                >
                  刷新
                </Button>
              </Space>
              <div style={{ marginTop: 8, fontSize: 12, color: "#999" }}>
                💡 一次修复是按「章节」成稿的：逐条<Text strong>拒绝</Text>某条冲突会将该章节<Text strong>整体恢复</Text>为修复前原文（同章节其他已修复项一并回退）。
                需要保留部分修改时，请先「全部拒绝」后用更低的修复等级重新「一键修复」。
              </div>
              <div style={{ marginTop: 4, fontSize: 12, color: "#999" }}>
                💡 默认会跳过「已修复且修复成果仍在正文里」的冲突以节省 AI 调用；
                勾选「强制全量重修」后本批对所有命中的冲突重新调用 AI 修复。
              </div>
            </div>
          </Card>

          {/* 冲突清单 */}
          <Card size="small" title="冲突清单" style={{ marginBottom: 12 }}>
            {crConflicts.length === 0 ? (
              <Empty description="暂无冲突清单，请先点击「开始扫描」" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Table
                size="small"
                rowKey="id"
                pagination={{ pageSize: 8, size: "small" }}
                // ✅ 性能优化：虚拟滚动（固定 260px 表体，避免视口外行全量建 DOM）
                scroll={{ y: 260 }}
                virtual
                dataSource={crConflicts}
                expandable={{
                  expandedRowRender: (r) => (
                    <div style={{ fontSize: 12 }}>
                      {(r.occurrences || []).map((o, i: number) => (
                        <div key={i} style={{ marginBottom: 4 }}>
                          <Tag>{o.section_title || o.section_id}</Tag>
                          <span>{o.text || o.value}</span>
                        </div>
                      ))}
                      {r.repair_instruction && (
                        <div style={{ marginTop: 6, color: "#1677ff" }}>修复指令：{r.repair_instruction}</div>
                      )}
                      {r.reason && <div style={{ marginTop: 2, color: "#999" }}>仲裁理由：{r.reason}</div>}
                    </div>
                  ),
                }}
                columns={[
                  { title: "ID", dataIndex: "id", width: 70 },
                  {
                    title: "类型", dataIndex: "conflict_type", width: 100,
                    render: (v: string) => CR_TYPE_LABEL[v] || v || "—",
                  },
                  {
                    title: "等级", dataIndex: "severity", width: 70,
                    render: (s: string) => (
                      <Tag color={s === "high" ? "red" : s === "medium" ? "orange" : "blue"}>{s || "-"}</Tag>
                    ),
                  },
                  { title: "主题", dataIndex: "topic", width: 150, ellipsis: true },
                  {
                    title: "涉及章节", dataIndex: "occurrences", width: 160, ellipsis: true,
                    render: (occs) =>
                      Array.from(new Set((occs || []).map((o: { section_title?: string; section_id?: string }) => o.section_title || o.section_id)))
                        .join(" / ") || "—",
                  },
                  {
                    title: "当前值", dataIndex: "occurrences", width: 150, ellipsis: true,
                    render: (occs) =>
                      Array.from(new Set((occs || []).map((o: { value?: string }) => o.value).filter(Boolean))).join(" / ") || "—",
                  },
                  {
                    title: "权威值", dataIndex: "authoritative_value", width: 130, ellipsis: true,
                    render: (v: string) => v || "—（待人工）",
                  },
                  {
                    title: "状态", dataIndex: "status", width: 100,
                    render: (s: string) => <Tag>{CR_STATUS_LABEL[s] || s || "—"}</Tag>,
                  },
                ]}
              />
            )}
          </Card>

          {/* 修复预览（逐条确认） */}
          <Card
            size="small"
            title={
              <Space>
                <span>修复预览</span>
                {crRepair?.repair_id && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    批次 {crRepair.repair_id} · {CR_REPAIR_STATUS[crRepair.status!] || crRepair.status}
                  </Text>
                )}
              </Space>
            }
            style={{ marginBottom: 12 }}
          >
            {!(crRepair?.items || []).length ? (
              <Empty description="暂无修复结果，点击「一键修复」后在此逐条确认" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Collapse
                items={(crRepair!.items || []).map((it, idx: number) => ({
                  key: `${it.conflict_id}-${idx}`,
                  label: (
                    <Space wrap size={6}>
                      <Tag>{it.conflict_id || "—"}</Tag>
                      <Text style={{ fontSize: 12 }}>{it.section_title || it.section_id || "（无章节）"}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>{it.topic}</Text>
                      <Tag color={it.status === "repaired" ? "green" : it.status === "skipped" ? "orange" : "red"}>
                        {CR_ITEM_STATUS[it.status!] || it.status}
                      </Tag>
                    </Space>
                  ),
                  children: (
                    <div style={{ fontSize: 12 }}>
                      <div style={{ marginBottom: 6 }}>
                        <Text type="secondary">修复前：</Text>
                        <div style={{
                          background: "rgba(255,77,79,0.06)", padding: "4px 8px", borderRadius: 4,
                          whiteSpace: "pre-wrap", maxHeight: 120, overflow: "auto",
                        }}>
                          {it.before || "—"}
                        </div>
                      </div>
                      <div style={{ marginBottom: 6 }}>
                        <Text type="secondary">修复后：</Text>
                        <div style={{
                          background: "rgba(82,196,26,0.06)", padding: "4px 8px", borderRadius: 4,
                          whiteSpace: "pre-wrap", maxHeight: 120, overflow: "auto",
                        }}>
                          {it.after || "—"}
                        </div>
                      </div>
                      {(it.problems || []).length > 0 && (
                        <div style={{ color: "#cf1322", marginBottom: 6 }}>
                          问题：{(it.problems || []).join("；")}
                        </div>
                      )}
                      {it.status === "repaired" && (
                        <Space>
                          <Button size="small" type="primary" onClick={() => handleCrConfirm([it.conflict_id!], [])}>
                            接受
                          </Button>
                          <Button size="small" danger onClick={() => handleCrConfirm([], [it.conflict_id!])}>
                            拒绝（恢复原文）
                          </Button>
                        </Space>
                      )}
                    </div>
                  ),
                }))}
              />
            )}
          </Card>

          {/* 修复记录（可回滚） */}
          <Card size="small" title="修复记录（可一键回滚）">
            {crHistory.length === 0 ? (
              <Empty description="暂无修复记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Table
                size="small"
                rowKey="id"
                pagination={false}
                dataSource={crHistory}
                columns={[
                  {
                    title: "时间", dataIndex: "created_at", width: 160,
                    render: (v: string) => (v || "").replace("T", " ").slice(0, 19),
                  },
                  { title: "批次", dataIndex: "id", width: 160, ellipsis: true },
                  { title: "冲突", dataIndex: "total_conflicts", width: 60 },
                  { title: "已修复", dataIndex: "repaired", width: 70 },
                  { title: "失败", dataIndex: "failed", width: 60 },
                  {
                    title: "状态", dataIndex: "status", width: 110,
                    render: (s: string) => <Tag>{CR_REPAIR_STATUS[s] || s}</Tag>,
                  },
                  {
                    title: "操作", width: 90,
                    render: (_, r) => (
                      <Button size="small" danger onClick={() => handleCrRollback(r)}>回滚</Button>
                    ),
                  },
                ]}
              />
            )}
          </Card>
        </Spin>
      </Drawer>
    </div>
  );
}
