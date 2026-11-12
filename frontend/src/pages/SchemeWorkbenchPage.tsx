import { useEffect, useState, useCallback, useRef, useMemo, memo } from "react";
import {
  App,
  Card,
  Button,
  Tree,
  Progress,
  Space,
  Tabs,
  Input,
  InputNumber,
  Radio,
  Checkbox,
  Empty,
  Tag,
  Modal,
  Form,
  Select,
  Typography,
  Descriptions,
  Collapse,
  Upload,
  Divider,
  Tooltip,
  List,
  Switch,
  Alert,
  Spin,
  Table,
  Row,
  Col,
  Drawer,
  Popover,
  Pagination,
} from "antd";
// ✅ any 清理专项（2026-11-02）：编辑树/展示树的构建函数返回 antd TreeDataNode
//    （拖拽回调的 onDrop 事件类型随 useOutlineActions 迁至 hook 内引用）
import type { TreeDataNode } from "antd";
import {
  PlayCircleOutlined,
  PauseOutlined,
  StopOutlined,
  FileTextOutlined,
  FilePdfOutlined,
  ExportOutlined,
  AuditOutlined,
  GlobalOutlined,
  FolderOpenOutlined,
  UploadOutlined,
  AppstoreOutlined,
  SwapOutlined,
  ReloadOutlined,
  CaretRightOutlined,
  EditOutlined,
  FontSizeOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  ExclamationCircleOutlined,
  WarningOutlined,
  SafetyOutlined,
  AimOutlined,
  InfoCircleOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  DeleteOutlined,
  PlusOutlined,
  MinusCircleOutlined,
  FileSearchOutlined,
  SyncOutlined,
  ClockCircleOutlined,
  HourglassOutlined,
  ThunderboltOutlined,
  PauseCircleOutlined,
  BarChartOutlined,
} from "@ant-design/icons";
import { useParams } from "react-router-dom";
import {
  sectionsApi,
  schemesApi,
  factsApi,
  complianceApi,
  sseFetch,
  sseGetStream,
  tasksApi,
  consistencyRepairApi,
  bidAnalysisApi,
  docPipelineApi,
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
  type DocumentPipelineStatusLike,
  type PipelineActionKey,
} from "../components/DocumentPipelineSummary";
import BidAnalysisTab, { BidAnalysisItemBody } from "../components/BidAnalysisTab";
// ✅ 2026-09-24 B2/T2：import 子 Tab 标签栏与「18 项扁平清单」抽为受控组件（可单测）
import ImportSubTabBar from "../components/ImportSubTabBar";
import BaItemFlatList from "../components/BaItemFlatList";
import {
  recomputeBaSummary,
  normalizeBaGroups,
  findFirstDoneBaItem,
  type BaItemDef,
  type BaStoredItem,
  type BaTextStats,
  type BaGroup,
  type BaSummary,
} from "../utils/bidAnalysis";
import {
  buildUploadNotice,
  computeDocStats,
  NEXT_TAB,
  pickInitialTab,
  selectPlacedExportCharts,
  findDefaultExportPreset,
  canShrinkSection,
  type WorkflowTabKey,
  type DocRecord,
  type PreviewError,
} from "../utils/workflowDerived";
import {
  UPLOAD_FILE_ACCEPT,
  splitByUploadTotalQuota,
  splitByUploadFileCount,
  partitionUploadFiles,
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_FILES_FALLBACK,
  resolveMaxUploadBytes,
  formatUploadLimitMb,
} from "../utils/uploadAccept";
// ✅ 2026-09-26 解析可信度提示：把后端的「文档被截断 / 预算耗尽被跳过 / 编码降级」
//    翻译成用户可见的告警。集中在 utils 的纯函数里，避免页面各处拼文案而漂移。
import { sourceNotice, previewNotices } from "../utils/parseSourceNotice";
// ✅ R86 L1：目录写入检出占位/泛化标题时的可操作提示（后端 create/update/
//    save-outline/upload 四条入口加法式回传 `generic_residual` 明细）。
//    文案集中在 utils 纯函数里（vitest 可直接覆盖），页面只负责 msg.warning。
import { genericAutofixNotice, genericResidualNotice } from "../utils/genericResidual";
import { emptyTopChaptersNotice } from "../utils/emptyTopChapters";
// ✅ R87 FIX-1：章节编号**合法性判据**（与后端 `numbering.stored_id_parts` 同口径）。
//    此前本页面自持一份「逐段 parseInt 挑数字」的松过滤，会把脏编号抢救成
//    看似合法的章号，而后端 fail-closed ⇒ 目录树与导出成稿编号不一致。
import { storedIdParts } from "../utils/outlineNumbering";
import ContentGenerationTab from "../components/ContentGenerationTab";
import {
  upsertSectionLog,
  finalizeRunningLogsIn,
  mergeFailedSectionsInto,
  contentResultFailedSections,
  contentResultSummary,
  normalizeQualityIssues,
  normalizeChartYield,
  chartDropTexts,
  isTerminalTaskStatus,
  isTerminalContentEvent,
  degradedConsistencyHint,
  // ✅ R175 BUG-3：进度单调护栏（心跳/单章阶段/终态统一走同一纯函数）
  nextProgress,
  // ✅ R77b（2026-10-15）：事实冲突定点修复终态提示（与标准校验提示同纪律）
  factAutofixHint,
  type SectionLogItem,
  type GenStats,
  type StandardReport,
} from "../utils/contentEvents";
// F-CONTENT-STANDARD(2026-09-26): 生成标准纯函数层（请求体映射 / 标签 / 报告判定）
import {
  buildStandardRequestFields,
  standardSummaryHint,
  sectionStandardOptionLabel,
  normalizeStandard,
  STANDARD_LABELS,
  issueLabel,
  reportHasIssues,
  reportIssueCount,
  type GenerationStandardChoice,
} from "../utils/contentStandard";
import { useSchemeLiveTask } from "../hooks/useSchemeLiveTask";
import { useAntdMessageHub } from "../utils/activityCenter";
// ✅ 2026-09-25：SSE 批量器抽到 utils/sseBatcher.ts（TaskStatusBar 也要复用，
//  工具函数不再寄生在 9000+ 行的页面文件里）
import { createSseBatcher } from "../utils/sseBatcher";
import { appendBoundedLog } from "../utils/perf/logBuffer";
import SectionContentCard, { countPlainTextWords } from "../components/SectionContentCard";
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
  FactItemLike,
  FactCrossConflict,
} from "../types/facts";
import { deriveExportGate } from "../utils/exportCharts";
const { Title, Text } = Typography;

// ✅ 2026-11-04 拆分（G1）：页面级契约类型抽到 schemeWorkbench/workbenchTypes.ts
//    （纯类型搬运，运行时零变化；SSE 事件 / REST 响应字段与搬运前逐字一致）。
import type {
  OutlineNodeInput,
  SseBaseEvent,
  ContentGenEvent,
  ContextDegradedEvent,
  SchemeRecord,
  WorkbenchSection,
  MultiSectionHint,
  FactsSegmentStats,
  QualityCheckResult,
  BaEditItemLike,
  OutlineLibraryItem,
  ComplianceHistoryItem,
  TaskListItem,
  ExportIssueGate,
} from "./schemeWorkbench/workbenchTypes";

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
import { ReviewTab } from "./schemeWorkbench/tabs/ReviewTab.tsx";
import { ExportTab } from "./schemeWorkbench/tabs/ExportTab.tsx";
import type { NumberingCheckInfo } from "../utils/exportResponse";
import { FactsTab } from "./schemeWorkbench/tabs/FactsTab.tsx";
import { OutlineTab } from "./schemeWorkbench/tabs/OutlineTab.tsx";
import { ImportTab, ImportSidePanel } from "./schemeWorkbench/tabs/ImportTab.tsx";
import { OutlineGateList } from "./schemeWorkbench/OutlineGateList.tsx";
import { OutlineNodeBudgetPanel } from "./schemeWorkbench/OutlineNodeBudgetPanel.tsx";
import { OutlineTreeActions } from "./schemeWorkbench/OutlineTreeActions.tsx";
import { SchemeTreePanel } from "./schemeWorkbench/SchemeTreePanel.tsx";
import { SectionLogList } from "./schemeWorkbench/SectionLogList.tsx";
import { TaskControlButtons } from "./schemeWorkbench/TaskControlButtons.tsx";
import {
  EXPORT_ISSUE_LABEL,
  EXPORT_STAGE_LABEL,
  EXPORT_STAGE_ORDER,
  TASK_TYPE_LABEL,
  describeExportIssue,
  statusColor,
  statusText,
} from "./schemeWorkbench/exportShared.ts";
import {
  FACTS_PAGE_SIZE,
  FACT_CATEGORY_OPTIONS,
  FACT_CHAPTER_TITLES,
  FACT_VALUE_KIND_OPTIONS,
  FALLBACK_FACT_ATTR_TITLES,
  FALLBACK_SOURCE_KIND_TITLES,
  applyFactsFilter,
  buildBatchAckStaleResultCopy,
  buildBatchResolveCopy,
  buildBatchResolveResultCopy,
  buildFactsCategoryEntriesFromStats,
  buildFactsPagination,
  buildFactsSummary,
  factAttrTitle,
  factChapterTitle,
  factSourceKindTitle,
  factValueKindLabel,
  filterFactsGroupsByCategory,
  hasFactsFilterMatch,
  isCurrentSchemeRequest,
  mergeFactsDiagnosticsSettled,
  resolveFactsChapterSettled,
  resolveFactsDangerSettled,
  resolveFactsLoadPage,
  resolveSelectedFactCategory,
} from "./schemeWorkbench/factsShared.ts";
import { type FactsLogItem, type OutlineLogItem } from "./schemeWorkbench/logTypes.ts";
import { formatOutlineTitle } from "./schemeWorkbench/outlineNumbering.ts";
import {
  buildTree,
  collectAllKeys,
  countGeneratedLeaves,
  countOutline,
  findFirstContentDescendant,
  findSectionById,
  flattenTreeKeys,
  hasAnyContent,
  hasUnsavedLocalNodes,
  mergeTreeContents,
  moveSiblingInTree,
  scrollToNodeInContainer,
  treeFingerprint,
  updateNodeFields,
  type TreeNode,
} from "./schemeWorkbench/outlineTreeModel.ts";
import {
  CR_ITEM_STATUS,
  CR_REPAIR_STATUS,
  CR_STATUS_LABEL,
  CR_TYPE_LABEL,
  DEFAULT_COMPLIANCE_CHECKLIST,
} from "./schemeWorkbench/reviewShared.ts";
import { pickStats, statsEqual } from "./schemeWorkbench/statsFormat.ts";
import {
  findRecentOutlineTaskImpl,
  pollTaskUntilTerminalImpl,
  reattachTaskImpl,
  resolveUnknownTaskNext,
  type TaskTerminalInfo,
} from "./schemeWorkbench/taskPolling.ts";
import { useExportPresets } from "./schemeWorkbench/useExportPresets.tsx";
import { useDocumentWorkflow } from "./schemeWorkbench/useDocumentWorkflow.tsx";
import { useBidAnalysis } from "./schemeWorkbench/useBidAnalysis.tsx";
import { useReviewAudit } from "./schemeWorkbench/useReviewAudit.tsx";
import { useOutlineTreeEdit } from "./schemeWorkbench/useOutlineTreeEdit.tsx";
import { useOutlineActions } from "./schemeWorkbench/useOutlineActions.tsx";
import { useFactsCrud } from "./schemeWorkbench/useFactsCrud.ts";
import { useSectionEditing } from "./schemeWorkbench/useSectionEditing.tsx";
import { useExportCheck } from "./schemeWorkbench/useExportCheck.ts";
import { useExportRun } from "./schemeWorkbench/useExportRun.ts";
import { useUnknownTaskRecovery } from "./schemeWorkbench/useUnknownTaskRecovery.tsx";
import { useFactsGeneration } from "./schemeWorkbench/useFactsGeneration.ts";
import { useOutlineGeneration } from "./schemeWorkbench/useOutlineGeneration.tsx";
import { UnknownTaskBanner } from "./schemeWorkbench/UnknownTaskBanner.tsx";
import { useOutlineEditTreeRender } from "./schemeWorkbench/useOutlineEditTreeRender.tsx";
import { useOutlineViewTree } from "./schemeWorkbench/useOutlineViewTree.tsx";
import { useOutlineGate } from "./schemeWorkbench/useOutlineGate.tsx";
import { useOutlineAutoConfirm } from "./schemeWorkbench/useOutlineAutoConfirm.tsx";
import { useWorkbenchDataLoaders } from "./schemeWorkbench/useWorkbenchDataLoaders.ts";
// ✅ any 清理专项（2026-11-02）：R-P1 拆分 hook 已定稿的域契约 —— 页面 state
//    类型与 hook deps 对齐（setter 传入前受 hook props 类型约束）
import type {
  ComplianceResultItem,
  CrConflictItem,
  CrRepairHistoryItem,
  CrRepairResult,
  CrSummary,
  ExpertReviewResult,
  SelectedSectionLike,
} from "./schemeWorkbench/useReviewAudit";
import type {
  ExportCacheStatus,
  ExportIssueItem,
  ExportPresetItem,
  ExportPreflight,
  ExportRerunPlan,
  ExportStats,
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
export {
  GenerationProgressCard,
  OUTLINE_LOG_RENDER_MAX,
  type GenerationProgressProps,
} from "./schemeWorkbench/GenerationProgressCard.tsx";
export { OutlineGateList } from "./schemeWorkbench/OutlineGateList.tsx";
export {
  OutlineNodeBudgetPanel,
  WORD_BUDGET_MAX,
  WORD_BUDGET_MIN,
} from "./schemeWorkbench/OutlineNodeBudgetPanel.tsx";
export { OutlineTreeActions } from "./schemeWorkbench/OutlineTreeActions.tsx";
export { SchemeTreePanel, type SchemeTreePanelProps } from "./schemeWorkbench/SchemeTreePanel.tsx";
export {
  SECTION_LOG_BG,
  SECTION_LOG_ICON,
  SECTION_LOG_RENDER_MAX,
  SectionLogList,
} from "./schemeWorkbench/SectionLogList.tsx";
export { TaskControlButtons } from "./schemeWorkbench/TaskControlButtons.tsx";
export {
  EXPORT_ISSUE_LABEL,
  EXPORT_STAGE_LABEL,
  EXPORT_STAGE_ORDER,
  TASK_TYPE_LABEL,
  deepMergeExportConfig,
  describeExportIssue,
  statusColor,
  statusText,
} from "./schemeWorkbench/exportShared.ts";
export {
  FACTS_ADJUST_IGNORE_REASONS,
  FACTS_ADJUST_IGNORE_REASON_LABELS,
  FACTS_ADJUST_PREVIEW_LIMIT,
  FACTS_PAGE_SIZE,
  FACT_CATEGORY_OPTIONS,
  FACT_CHAPTER_TITLES,
  FACT_VALUE_KIND_OPTIONS,
  FALLBACK_FACT_ATTR_TITLES,
  FALLBACK_SOURCE_KIND_TITLES,
  applyFactsCompletedEvent,
  applyFactsFilter,
  buildBatchAckStaleResultCopy,
  buildBatchResolveCopy,
  buildBatchResolveResultCopy,
  buildFactChapterStats,
  buildFactsCategoryEntriesFromStats,
  buildFactsPagination,
  buildFactsSummary,
  factAttrTitle,
  factChapterTitle,
  factSourceKindTitle,
  factValueKindLabel,
  factsAdjustIgnoreText,
  factsAdjustIgnoredCount,
  filterFactsGroupsByCategory,
  formatFactsAdjustOperation,
  hasFactsFilterMatch,
  isCurrentSchemeRequest,
  mergeFactsDiagnosticsSettled,
  normalizeFactsAdjustPlan,
  resolveFactsChapterSettled,
  resolveFactsDangerSettled,
  resolveFactsLoadPage,
  resolveSelectedFactCategory,
  type FactChapterStatRow,
  type FactsAdjustIgnoredItem,
  type FactsAdjustOperation,
  type FactsAdjustPlanShape,
  type FactsCategoryEntry,
} from "./schemeWorkbench/factsShared.ts";
export { type FactsLogItem, type OutlineLogItem } from "./schemeWorkbench/logTypes.ts";
export {
  CN_NUMBERS,
  LEADING_NUMBER_CJK,
  LEADING_NUMBER_RE,
  LEADING_NUMBER_SEP,
  PURE_NUMBER_TITLE_RE,
  YEAR_PREFIX_RE,
  formatOutlineTitle,
  stripOutlineNumbering,
} from "./schemeWorkbench/outlineNumbering.ts";
export {
  STAGE_COLOR,
  UNSAVED_KEY_PREFIXES,
  _PURE_NUMERIC_ID_RE,
  buildPartialOutlineHint,
  collectAllKeys,
  countGeneratedLeaves,
  countOutline,
  describeReorganizeReport,
  findFirstContentDescendant,
  findSectionById,
  flattenTreeKeys,
  hasAnyContent,
  hasDeepOutlineNodes,
  hasUnsavedLocalNodes,
  isUnsavedLocalKey,
  moveSiblingInTree,
  outlineToTreeNode,
  outlineTreeDepth,
  renumberOutline,
  renumberTreeLocally,
  scrollToNodeInContainer,
  summarizeUploadOutlineResult,
  treeFingerprint,
  treeNodesWithContent,
  treeToOutline,
  updateNodeFields,
  mergeTreeContents,
  type PartialOutlineHint,
  type TreeNode,
  type UploadOutlineSummary,
} from "./schemeWorkbench/outlineTreeModel.ts";
export {
  CR_ITEM_STATUS,
  CR_REPAIR_STATUS,
  CR_STATUS_LABEL,
  CR_TYPE_LABEL,
  DEFAULT_COMPLIANCE_CHECKLIST,
  summarizeOutlineQuality,
} from "./schemeWorkbench/reviewShared.ts";
export { fmtDuration, pickStats, statsEqual } from "./schemeWorkbench/statsFormat.ts";
export {
  findRecentOutlineTaskImpl,
  pollTaskUntilTerminalImpl,
  reattachTaskImpl,
  resolveUnknownTaskNext,
  type TaskTerminalInfo,
  type UnknownTaskInfo,
} from "./schemeWorkbench/taskPolling.ts";

// ✅ G12：审核依据解析迁至 reviewShared.ts（逐字搬运），页面保留同名再导出。
import { summarizeOutlineReviewBasis } from "./schemeWorkbench/reviewShared.ts";
export { summarizeOutlineReviewBasis } from "./schemeWorkbench/reviewShared.ts";

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
  reconnecting,
}: {
  step: number;
  title: string;
  badge?: string;
  badgeColor?: string;
  hint?: string;
  reconnecting?: boolean;
}) {
  return (
    <Space
      size={3}
      style={{ lineHeight: 1.3 }}
      title={hint ? `${title}（${hint}）` : title}
    >
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
      <span className="swb-tab-title" style={{ fontSize: 13 }}>
        {title}
      </span>
      {badge && (
        <Tag
          color={badgeColor || "blue"}
          style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: "16px", padding: "0 4px" }}
        >
          {badge}
        </Tag>
      )}
      {reconnecting && (
        <Tag
          color="warning"
          style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: "16px", padding: "0 4px" }}
        >
          重连中
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

// ✅ 2026-11-04 拆分（G2）：FactsGroupList 抽到 schemeWorkbench/FactsGroupList.tsx，
//    对外导出名保持不变（页面与既有组件级测试仍从本文件 import）。
import { FactsGroupList } from "./schemeWorkbench/FactsGroupList";
export { FactsGroupList };
// ✅ 2026-11-04 拆分（G4d）：全文一致性修复工作台抽屉抽到独立面板组件。
import { ComplianceRepairDrawer } from "./schemeWorkbench/ComplianceRepairDrawer";
// ✅ 2026-11-04 拆分（G4a-c）：工作台弹窗集合（事实/目录库/预览/提取配置）抽到独立面板组件。
import { outlineTruncationNotice, outlineUnmountedNotice } from "./schemeWorkbench/outlineNotices";
import { WorkbenchModals } from "./schemeWorkbench/WorkbenchModals";

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
  // ✅ 可感知重连态：SSE 断线后正在重新挂接后台任务时，进度卡 / 标签徽标 / 控制按钮
  //    统一进入「重连中」，而不是只有一行 progressMsg 在变。
  const [sseReconnecting, setSseReconnecting] = useState(false);
  const [genType, setGenType] = useState("");
  // ✅ F3 暂停状态可见：当前活跃任务是否已暂停（暂停/恢复按钮互斥 + 「已暂停」Tag）
  const [taskPaused, setTaskPaused] = useState(false);
  // ✅ R106 FIX-4：「停止中」态。stop 指令 accepted ≠ 已停止（后端协作式退出，
  //    要等在飞 AI 调用被 cancel / 段边界检查点命中），按钮保持置灰直至任务落
  //    终态。旧实现发完指令立刻乐观报「已停止」，用户以为还能接着点。
  const [taskStopping, setTaskStopping] = useState(false);
  // ✅ R227（修复10）：任务终态信号（completed/degraded/stopped/failed）—— 正文
  //    生成 stopped 终态 progress 保留真实值（<1），GenerationProgressCard 的
  //    `hasFailed` 需区分「终态但 progress<1」与「仍在跑」，故显式记录终态。
  const [taskTerminal, setTaskTerminal] = useState(false);
  // ✅ R174 F2（2026-11-03）：未知任务状态机（state + ref + 处理器）已收口到
  //    schemeWorkbench/useUnknownTaskRecovery.tsx（G10 拆分，见其注释）。
  //    ref 仍是守卫唯一事实源，页面通过 hasUnknownTask() 只读判定。
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
  const [factItemEditing, setFactItemEditing] = useState<
    (FactItemLike & { groupId?: string; groupTitle?: string }) | null
  >(null);
  const [factItemSubmitting, setFactItemSubmitting] = useState(false);
  const [factItemForm] = Form.useForm();
  // 全局事实 Tab 筛选：全部 / 模拟值 / 矛盾 / 待审核
  const [factsFilter, setFactsFilter] = useState<string>("all");
  // ✅ 缺值模式（对齐 OpenBidKit 全局事实三模式）：
  // fabricate（智能补全，模拟值标记）/ omit（不杜撰，剔除模拟值）/ placeholder（留待填写）
  const [missingValueMode, setMissingValueMode] = useState<"fabricate" | "omit" | "placeholder">(
    "fabricate",
  );
  // 全局事实提取进度日志（阶段/分段级，实时滚动显示）
  const [factsLogs, setFactsLogs] = useState<FactsLogItem[]>([]);
  const factsLogsRef = useRef<FactsLogItem[]>([]);
  // 最近一次提取的分段统计（用于展示"哪些段失败、为什么失败"）
  const [factsSegmentStats, setFactsSegmentStats] = useState<FactsSegmentStats | null>(null);
  // ✅ SSE completed 事件的跨段矛盾清单（旧版前端未消费，2026-09-20 接线）
  const [factsCrossConflicts, setFactsCrossConflicts] = useState<FactCrossConflict[]>([]);
  // ✅ 事实分类白名单：初始为本地 FACT_CATEGORY_OPTIONS，挂载后由 GET /global-facts/categories 覆盖
  const [factCategoryOptions, setFactCategoryOptions] =
    useState<{ value: string; label: string }[]>(FACT_CATEGORY_OPTIONS);
  // ✅ 2026-10-06：九大章节标题 / 事实属性名 / 来源类型名 —— 权威来源是
  //    GET /global-facts/category-map（此前该端点从未被调用，三套枚举各自
  //    硬编码或干脆不展示）。初值为本地兜底表，拉取成功后覆盖。
  const [factChapterTitles, setFactChapterTitles] =
    useState<Record<string, string>>(FACT_CHAPTER_TITLES);
  const [factAttrTitles, setFactAttrTitles] =
    useState<Record<string, string>>(FALLBACK_FACT_ATTR_TITLES);
  const [factSourceKindTitles, setFactSourceKindTitles] = useState<Record<string, string>>(
    FALLBACK_SOURCE_KIND_TITLES,
  );
  // ✅ 2026-10-06：分页状态（消费后端 pagination；此前从不下发 limit/offset）
  const [factsPage, setFactsPage] = useState(1);
  // ✅ R166：loadFacts 的默认页码走 ref 镜像 —— 旧实现把 factsPage 放进
  //    loadFacts 的 useCallback deps，分页 setState → loadFacts 身份变化 →
  //    挂载 effect 整体重跑（目录全量 + 合规历史 + 专家项 3-4 个无关请求
  //    随每次翻页重发）。ref 镜像后 loadFacts 引用稳定，分页只发事实列表请求。
  const factsPageRef = useRef(1);
  // ✅ BUG-06 前端半边（R176 · 2026-11-04）：记录「上一次真正发起事实列表请求的方案」。
  //    切方案时 `factsPageRef` 里的旧页码若被沿用，新方案首屏会带着旧 offset 去取，
  //    越界后即便后端 clamp 也只是补救；根因必须在**首个请求**就落回第 1 页。
  //    这里用 ref 而非 state：与 factsPageRef 同理，避免进 loadFacts 依赖引发重挂载。
  const factsPageSchemeRef = useRef("");
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
  // ✅ R193（L-07 · R189 遗留落地）：数据源从「当前页 20 组聚合」切换为后端
  //    stats.by_category 全量统计（后端注释明确「分页仅影响 groups，统计基于
  //    全量」）。>20 组方案面板分类不再缺行、计数徽标与实际一致；排序同源
  //    （后端 _cat_order）保证面板首项必在当前页分组头部，默认选中不悬空。
  const factCategoryEntries = useMemo(
    () =>
      buildFactsCategoryEntriesFromStats(
        factsSummary,
        (cat) => factCategoryOptions.find((o) => o.value === cat)?.label || "",
      ),
    [factsSummary, factCategoryOptions],
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
  // ✅ B1-1（R176 · 2026-11-04）：预检读取失败（503 + read_degraded）时的显式状态 ——
  //    旧实现失败后 issues/stats 双双清空，页面只剩「尚未预检」，用户不知道
  //    「有没有问题」；现单独标记「预检不完整」，与「确实无问题」明确区分。
  const [exportCheckIncomplete, setExportCheckIncomplete] = useState(false);
  // ✅ R183-补（2026-11-07）：严格模式编号结构自检结论 —— `checked=false` 表示
  //    「自检没跑成」，导出页必须与「检查通过」区分显示（后端 X-Numbering-Check）。
  const [numberingCheck, setNumberingCheck] = useState<NumberingCheckInfo | null>(null);
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
  const exportGate = useMemo(
    () =>
      deriveExportGate({
        hasPreflight: exportStats !== null,
        issues: exportIssues,
        readiness: exportPreflight,
      }),
    [exportIssues, exportPreflight, exportStats],
  );
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
    fetchExportPresets,
    handleRefreshPresets,
    handleSavePreset,
    handleSetDefaultPreset,
    handleDeletePreset,
    loadCacheStatus,
  } = useExportPresets({
    id,
    exportForm,
    presetOpLoading,
    setPresetOpLoading,
    selectedPresetId,
    cacheAbortRef,
    setCacheLoading,
    currentSchemeIdRef,
    presetListAbortRef,
    setExportPresets,
    setSelectedPresetId,
    setExportIssues,
    setExportStats,
    setExportPreflight,
    setPlaceholderReport,
    setRerunPlan,
    setCacheStatus,
    msg,
    modal,
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
  const setChecklistDirty = (v: boolean) => {
    checklistDirtyRef.current = v;
    setChecklistDirtyState(v);
  };
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
  // 预览失败状态位：区分「超时 / 请求失败 / 确实未解析」，避免失败被空态「尚未解析」盖掉
  const [previewError, setPreviewError] = useState<PreviewError | null>(null);
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
  const [sectionCheckResult, setSectionCheckResult] = useState<Record<string, unknown> | null>(
    null,
  );
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
  // ✅ R179 N1（2026-11-05）：目录自动确认（默认关 = 完全现状人工闸门）。
  //    开关存后端 governance_settings kv；delay 为后端只读倒计时（运维环境变量）。
  const [outlineAutoConfirmEnabled, setOutlineAutoConfirmEnabled] = useState(false);
  const [outlineAutoConfirmDelayMs, setOutlineAutoConfirmDelayMs] = useState(8000);
  // ✅ 2026-09-23「上传解析 + 提取项目」合并：import 内部子 Tab（docs = 文档解析 / extract = 项目提取）
  //    顶层 Tab 数量从 7 降到 6，bidAnalysis 不再是独立 Tab key，而是 import 下的子 Tab。
  const [importSubTab, setImportSubTab] = useState<"docs" | "extract">("docs");
  // ✅ F2 导航竞态守卫：镜像当前 Tab 到 ref，异步回调（SSE 完成等）里的自动跳转
  //   仅在用户仍停留在「触发跳转的那个 Tab」时才执行，避免把已手动切走的用户拽回。
  const activeTabRef = useRef<string>("content");
  useEffect(() => {
    activeTabRef.current = activeTab;
  }, [activeTab]);
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
  const draftKey = selectedSection && id ? `scheme_draft_${id}_${selectedSection.key}` : null;
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
  const [lastUpload, setLastUpload] = useState<{
    id: string;
    name: string;
    outline: OutlineNodeInput[];
  } | null>(null);
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
  // ✅ R179 M1（2026-11-05）：「新增建议章节」后待定位的新章节 id —— load 回调
  //    拿到新树后据此选中定位（方案 §1.7：新增成功后自动定位，只定位不生成正文）。
  const pendingLocateSectionIdRef = useRef<string>("");
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
  // ✅ 文档解析内容预览的取消控制器：预览可能等满 60s，卸载/切方案时中断在途请求
  const previewAbortRef = useRef<AbortController | null>(null);
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
      // ✅ R213（FIX-12）：后端 list_sections 回传 unmounted_count（R210），
      //    前端此前零类型零消费 ⇒ 目录比库里少几章却毫无提示（只有后端 WARNING）。
      const _unmounted = outlineUnmountedNotice(data);
      if (_unmounted) msg.warning(_unmounted);
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
        // ✅ R223（2026-11-12 · P3 · FE-9）：树里找不到时必须清空选中 —— 旧实现
        //    落空时什么都不做 ⇒ 新增章节（key=local_ 前缀）保存后重建的树里该
        //    key 不存在（落库换成 DB 主键），selectedSection 残留旧 local_ 对象：
        //    预算面板继续显示幽灵节点，预算编辑回调 findSectionById 落空后静默
        //    return（点击零反馈）。
        else setSelectedSection(null);
      }
      // ✅ R179 M1：新增建议章节后定位到新节点（只定位，不生成正文）
      if (pendingLocateSectionIdRef.current) {
        const created = findSectionById(newTree, pendingLocateSectionIdRef.current);
        if (created) setSelectedSection(created);
        pendingLocateSectionIdRef.current = "";
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      if (requestedSchemeId === currentSchemeIdRef.current) msg.error(err.message || "加载失败");
    }
    if (requestedSchemeId === currentSchemeIdRef.current) setLoading(false);
  }, [id]);

  // ✅ R179 M1（2026-11-05）：评分项缺失项「新增建议章节」——只新增结构节点，
  //    不生成正文、不调用 AI；创建后刷新目录、自动定位新节点，并交由面板重新
  //    计算覆盖率。
  const handleCreateSuggestedSection = useCallback(
    async (title: string, parentId: string | null, level: number) => {
      if (!id) throw new Error("方案未加载");
      const { data } = await sectionsApi.create(id, {
        title,
        parent_id: parentId || "",
        level: Math.min(Math.max(level, 1), 3),
        sort_order: 9999,
      });
      pendingLocateSectionIdRef.current = String(data?.id || "");
      await load();
    },
    [id, load],
  );

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
  const baActive =
    baRunning || (!!liveBa && (liveBa.status === "running" || liveBa.status === "paused"));
  const baShownProgress = baRunning ? baProgress : liveBa?.progress || 0;
  const baShownMsg = baRunning ? baProgressMsg : liveBa?.message || "";
  baDefsRef.current = baDefs;

  // ✅ P0-7 轮询瘦身：生成期间的轻量目录刷新（不拉 content 大字段、不覆盖
  //    正在编辑的 selectedSection），增量由 SSE 事件即时推送，轮询仅作状态兜底
  const refreshTreeLight = useCallback(async () => {
    if (!id) return;
    try {
      const { data } = await sectionsApi.list(id, { include_content: false });
      const newTree = buildTree(data.tree || []);
      // ✅ R215（2026-11-11 · P2）：轻量轮询整树替换丢 content —— 轻量接口不返回
      //    content，字数/状态变化后指纹必不同 → 整树替换为无 content 节点，用户点
      //    其他章节时正文卡「已生成 N 字」与「（章节尚未生成…）」占位同卡矛盾。
      //    按旧树 key 回填 content（纯函数 mergeTreeContents，新树有值以新树为准）。
      setTree((prev) => {
        if (hasUnsavedLocalNodes(prev)) return prev;
        const merged = mergeTreeContents(prev, newTree);
        return treeFingerprint(prev) === treeFingerprint(merged) ? prev : merged;
      });
      const allKeys = collectAllKeys(newTree);
      setExpandedKeys((prev) => (prev.length === 0 ? allKeys : prev));
    } catch {
      // 生成中轮询失败静默：SSE 与断线兜底（pollTaskUntilTerminal）继续工作
    }
  }, [id]);

  const loadFactsDiagnostics = useCallback(
    async (options?: { signal?: AbortSignal }) => {
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
        // ✅ 刷新失败不得沿用旧结论（R176/R177 · 2026-11-04）：两侧一律经
        //    factsShared 的同一判定源落板 —— 失败/成功但无数据均清空为 null，
        //    面板落「无法判定 / 无数据」，不得保留上一次的覆盖率或危大结论。
        setFactsChapterReport(resolveFactsChapterSettled(merged));
        setFactsDangerReport(resolveFactsDangerSettled(merged));
        if (merged.warning && requestedSchemeId === currentSchemeIdRef.current) {
          msg.warning(merged.warning);
        }
      } finally {
        if (requestedSchemeId === currentSchemeIdRef.current) setFactsDiagnosticsLoading(false);
      }
    },
    [id, msg],
  );

  const loadFacts = useCallback(
    async (pageOverride?: number) => {
      if (!id) return;
      const requestedSchemeId = id;
      factsListAbortRef.current?.abort();
      const ac = new AbortController();
      factsListAbortRef.current = ac;
      // ✅ 2026-10-06：真正使用后端分页（此前从不传 limit/offset，
      //   pagination 每次下发却被丢弃 —— 分组一多就整屏渲染，且首屏白等全量）。
      // ✅ BUG-06 前端半边（R176 · 2026-11-04）：切方案后首个请求必须回第 1 页。
      //    判据在 resolveFactsLoadPage（方案变化 ⇒ 1，同方案翻页零回归）；
      //    此处立刻登记「本次请求的方案」，避免连点/并发再次沿用旧页。
      const wantPage = resolveFactsLoadPage({
        schemeId: id,
        lastSchemeId: factsPageSchemeRef.current,
        pageOverride,
        refPage: factsPageRef.current,
      });
      factsPageSchemeRef.current = id;
      const offset = (wantPage - 1) * FACTS_PAGE_SIZE;
      try {
        const { data } = await factsApi.list(requestedSchemeId, {
          signal: ac.signal,
          limit: FACTS_PAGE_SIZE,
          offset,
        });
        if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal))
          return;
        setFacts(data.groups || []);
        setFactsSummary(buildFactsSummary(data.stats));
        setFactsPagination(data.pagination || null);
        setFactsPage(wantPage);
        factsPageRef.current = wantPage;
        // ✅ R189（2026-11-08）：把本次列表请求的 signal 透传给诊断链。
        //    此前 loadFactsDiagnostics 支持 options.signal，却没有任何调用方
        //    传入 → 切方案/再次 loadFacts 时旧诊断请求继续在飞，
        //    mergeFactsDiagnosticsSettled 的 aborted 分支永远是死代码。
        // ✅ R193（D-11）：诊断与页码无关（章节/危大检查基于文档面），仅首屏
        //    /刷新（offset=0）拉取——旧实现每次翻页都重发 2 个诊断请求
        //    （danger-check 走 AI 参数核对链路时开销可观）。
        if (offset === 0) void loadFactsDiagnostics({ signal: ac.signal });
        // ✅ R189（调用链闭合）：后端 GET /global-facts/cross-conflicts 自 R175
        //    就把已落库的多来源冲突暴露出来，前端却从未调用 —— 页面一刷新
        //    「跨段矛盾」横幅只能靠 SSE 内存态，冲突记录仍在 DB 却不可见。
        //    这里在列表刷新后回读；**仅在本地为空时补齐**，避免覆盖 SSE 本轮
        //    刚下发的实时结果（两者同源，实时态优先）。失败静默：诊断数据
        //    不该让事实列表整体加载失败。
        void (async () => {
          try {
            // 独立 async 块：即使测试替身/旧版 api 层没有该方法，同步 TypeError
            // 也只在这里被吞掉，绝不能冒泡进 loadFacts 的 catch → 误报「加载失败」。
            const { data } = await factsApi.crossConflicts(requestedSchemeId, { signal: ac.signal });
            if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal))
              return;
            const conflicts = Array.isArray(data?.conflicts) ? data.conflicts : [];
            if (conflicts.length) setFactsCrossConflicts((prev) => (prev.length ? prev : conflicts));
          } catch (e: unknown) {
            if ((e as { name?: string })?.name === "AbortError" || ac.signal.aborted) return;
            console.warn("回读已落库跨段矛盾失败（不影响事实列表）:", e);
          }
        })();
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
    },
    [id, msg, loadFactsDiagnostics],
  );

  loadFactsRef.current = loadFacts;

  // ✅ R45 · D5：AI 调整事实的状态机已抽到模块级 FactsAdjustPanel，
  // 页面只保留后端契约接线（见下方 <FactsAdjustPanel /> 处的 preview/apply）。

  // ✅ 2026-11-04 拆分：三个只读加载器迁至 schemeWorkbench/useWorkbenchDataLoaders.ts
  const { loadDocuments, loadBaMeta, loadBaResults } = useWorkbenchDataLoaders({
    id,
    schemeProjectId,
    msg,
    currentSchemeIdRef,
    factsDocsListAbortRef,
    setDocList,
    setBaDefs,
    setBaGroups,
    setBaDefsError,
    setBaItems,
    setBaSummary,
    setBaLoading,
    setBaError,
    baDefsRef,
  });

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
    } catch {
      /* ignore */
    }
    setHistoryLoading(false);
  }, [id]);

  // ===== ✅ 全局事实：手工增删改查回调簇 =====
  //     G5 拆分：handler 迁至 schemeWorkbench/useFactsCrud.ts（函数体逐字搬运），
  //     页面仍持有弹窗/表单 state 与 loadFacts，经 deps 注入。
  const {
    openFactCreate,
    openFactEdit,
    submitFact,
    openFactItemEdit,
    submitFactItem,
    handleDeleteFactGroup,
    handleResolveFactItem,
    handleResolveConflictItem,
    handleClearAllFacts,
  } = useFactsCrud({
    id,
    msg,
    modal,
    loadFacts,
    factEditingId,
    setFactEditingId,
    factEditingCategory,
    setFactEditingCategory,
    factForm,
    setFactModalOpen,
    setFactSubmitting,
    factItemEditing,
    setFactItemEditing,
    factItemForm,
    setFactItemModalOpen,
    setFactItemSubmitting,
    setFactsFilter,
    setFactsSegmentStats,
    setFactsCrossConflicts,
  });

  // ✅ 2026-11-11：expertItems 回包的卸载守卫用独立 ref/effect 维护，
  //    刻意不改动下方 factsListAbortRef 的 cleanup 字面量 —— R193 D-01
  //    源码棘轮锁的就是那段 cleanup 的逐字形态（锁接线，不是锁注释）。
  const expertItemsAliveRef = useRef(true);
  useEffect(() => {
    expertItemsAliveRef.current = true;
    return () => {
      expertItemsAliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    load();
    loadFacts();
    loadComplianceHistory();
    complianceApi
      .expertItems()
      .then(({ data }) => {
        if (expertItemsAliveRef.current) setExpertItems(data.items || []);
      })
      .catch(() => {});
    // ✅ R193 修复（D-01 · 与 R191 factsDocsListAbortRef 同构漏面）：事实列表
    //    在飞请求的中止必须收进本 effect 的 cleanup（React 保证 cleanup 先于
    //    下一轮 body 执行）——「先杀上一轮、再发新一轮」。旧实现把 abort 放在
    //    [id] 切换 effect 里：同一次 commit 内本 effect 先执行（loadFacts 的
    //    同步前缀已创建新 AbortController 并发出新方案请求），切换 effect 随后
    //    abort 的恰是新方案刚发出的请求且无重发路径 → catch 吞掉 AbortError、
    //    finally 的 isCurrentSchemeRequest（新 id vs 新 id）通过 → factsReady
    //    误置 true → 事实 Tab 永久空态（分组/分页/诊断/矛盾横幅全空，且无自愈）。
    return () => {
      factsListAbortRef.current?.abort();
    };
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
    return () => {
      alive = false;
    };
  }, []);

  // ✅ R179 N1（2026-11-05）：读取目录自动确认开关（kv，默认 false）。
  //    失败/旧后端一律保持默认关 —— 绝不因读不到配置而改变人工路径行为。
  useEffect(() => {
    let alive = true;
    systemApi
      .getOutlineAutoConfirm()
      .then(({ data }) => {
        if (!alive) return;
        setOutlineAutoConfirmEnabled(!!data?.enabled);
        const d = Number(data?.delay_ms);
        if (Number.isFinite(d) && d > 0) setOutlineAutoConfirmDelayMs(d);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // ✅ 加载后端审核规则库，用规则标题覆盖前端默认清单（消除与后端/提示词的分叉）。
  //    用户一旦手工编辑过清单（checklistDirty），后续不再覆盖，保留自定义能力。
  useEffect(() => {
    if (!id) return;
    let alive = true;
    complianceApi
      .rules()
      .then(({ data }) => {
        if (!alive) return;
        const rules: AuditRule[] = (data.items || []).filter((r: AuditRule) => r.mode === "ai");
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
      .catch(() => {
        /* 规则加载失败时沿用前端内置默认清单 */
      });
    return () => {
      alive = false;
    };
  }, [id]);

  // ✅ 每次切入「审核与预检」Tab 触发子组件刷新（总检历史 / 审核清单）
  useEffect(() => {
    if (activeTab === "review") setReviewTick((t) => t + 1);
  }, [activeTab]);

  // ✅ 进入工作台即拉取资料文档列表（按 scheme id 查询，不依赖 project_id 是否已加载）
  //    ✅ R191 修复（切方案竞态）：本 effect 声明在「切方案清理」effect **之前**，
  //    React 按声明序执行 body —— id 变化时这里先发出新方案请求
  //    （factsDocsListAbortRef 已被覆盖为新请求），随后清理 effect 的
  //    factsDocsListAbortRef.abort() 会**误杀新方案刚发出的请求**且无重发
  //    → 新方案页面残留上一个方案的文档列表（行内解析/删除还携带旧方案
  //    docId）。现把中止收进本 effect 的 cleanup：React 保证 cleanup 先于
  //    下一轮所有 effect body 执行，中止的永远是「上一轮」请求。
  useEffect(() => {
    if (id) loadDocuments();
    return () => {
      factsDocsListAbortRef.current?.abort();
    };
  }, [id, loadDocuments]);

  // 「上传解析」/「提取项目」模块的一次性元数据加载（分类选项 + 解析项定义）
  useEffect(() => {
    if (!id) return;
    let alive = true;
    factsApi
      .categoryOptions()
      .then(({ data }) => {
        if (alive) setCategoryOptions(data.options || []);
      })
      .catch(() => {
        /* 选项加载失败时分类降级为只读 Tag */
      });
    // ✅ 事实分类单一事实源（后端 CATEGORY_TITLES），失败时静默保留本地兜底
    factsApi
      .categories()
      .then(({ data }) => {
        if (alive && Array.isArray(data.categories) && data.categories.length)
          setFactCategoryOptions(data.categories);
      })
      .catch(() => {
        /* 拉取失败时沿用 FACT_CATEGORY_OPTIONS */
      });
    // ✅ 2026-10-06：消费此前**从未被调用**的 /global-facts/category-map。
    //    该端点由后端 facts_classification.category_map_payload() 生成，
    //    是九大章节标题 / 事实属性名 / 来源类型名的唯一权威来源；前端此前
    //    各自硬编码（章节标题 2 份副本 + 事实属性 4 个内联三元 + 来源类型
    //    干脆不展示）→ 后端增删枚举后界面静默失真。
    //    失败时静默沿用本地兜底表（不阻断页面）。
    factsApi
      .categoryMap()
      .then(({ data }) => {
        if (!alive) return;
        const chapters = Array.isArray(data?.chapters) ? data.chapters : [];
        if (chapters.length) {
          const m: Record<string, string> = {};
          chapters.forEach((c) => {
            if (c?.key && c?.title) m[c.key] = c.title;
          });
          if (Object.keys(m).length) setFactChapterTitles(m);
        }
        if (data?.fact_attr_titles && Object.keys(data.fact_attr_titles).length) {
          setFactAttrTitles(data.fact_attr_titles);
        }
        if (data?.source_kind_titles && Object.keys(data.source_kind_titles).length) {
          setFactSourceKindTitles(data.source_kind_titles);
        }
      })
      .catch(() => {
        /* 拉取失败时沿用本地兜底表 */
      });
    loadBaMeta();
    return () => {
      alive = false;
    };
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
      previewAbortRef.current?.abort();
      // ✅ R191/R193：factsDocsListAbortRef 与 factsListAbortRef 的中止均已收进
      //    各自「列表加载」effect 的 cleanup（先于本 effect body 执行）——在
      //    这里 abort 会误杀本 commit 内新方案刚发出的列表请求且无重发。
      baSseAbortRef.current?.abort();
      exportCheckAbortRef.current?.abort();
      cacheAbortRef.current?.abort();
      exportAbortRef.current?.abort();
      // ✅ R197（F1）：审核与预检域三 ref —— 旧实现切方案不中止，旧方案 1-3 分钟
      //    的 AI 检查返回后直接把结果落到新方案页面。CR 扫描/修复两 ref 在
      //    useReviewAudit 内部，由其 [id] effect 收口。
      expertAbortRef.current?.abort();
      checkAbortRef.current?.abort();
      auditAbortRef.current?.abort();
      // ✅ R207：页面自持的一致性审计历史 GET 也须中止（旧结果落到新方案页）
      consistencyLatestAbortRef.current?.abort();
      sseBatcherRef.current?.stop();
      exportBatcherRef.current?.stop();
      exportingRef.current = false;
      setExporting(false);
      setExportPhase("");
      setCheckingExport(false);
      setCacheLoading(false);
      // ✅ R193 修复（D-02 · 生成运行态跨方案残留）：旧实现切换时只 abort SSE
      //    与 ++genSeqRef —— 被中止协程 finally 的世代校验（genSeq 不匹配）会
      //    跳过状态复位 ⇒ generating/genType/taskPaused/taskStopping/
      //    activeTaskIdRef 全部残留上一方案：新方案永久卡「正在提取全局事实」
      //    幽灵进度卡、全部事实操作按钮 disabled、停止按钮指向旧方案任务 id，
      //    且因入口全禁用用户无自愈途径（目录/正文链同灶，同因同修）。
      //    切换是同步点：++genSeq 已让旧协程不再复位，这里显式复位是唯一权威
      //    时机；若新方案确有运行中任务，接管轮询会重新置位。
      setGenerating(false);
      setGenType("");
      setTaskPaused(false);
      setTaskStopping(false);
      activeTaskIdRef.current = "";
      // ✅ R215（2026-11-11 · P1）：正文选中章与生成摘要跨方案复位 —— 旧实现在
      //    D-02 复位块漏掉 selectedSection / crSummary，而 load() 只在旧 section
      //    key 于新树中找得到时才刷新（找不到时什么都不做）⇒ 方案 A 的章节对象
      //    原样残留：正文卡显示 A 方案正文与字数、章节级生成标准 Select 以新
      //    方案 id + 旧 section key 调 update（404）、「重写/续写本章」把 A 的
      //    section_id 发给 B 的 SSE 链（select_target_leaves 查无此章 → 假成功
      //    completed）、最重者在残留内容点「AI 自动修复」会以 A 的 section_id
      //    触发跨方案写库（fix-mermaid 按全局 section_id 定位）。切换是同步点，
      //    这里显式清空是唯一权威时机；selectedSectionRef 渲染时自动跟随。
      setSelectedSection(null);
      setCrSummary(null);
      // ✅ R223（2026-11-12 · P2 · FE-3/FE-4）：目录树与未知态跨方案复位 ——
      //    ① setTree([])：切到 B 后、load() 返回前左侧树仍是 A 的目录；若 B 的
      //      load 失败（catch 仅 toast），A 的树**永久残留**在 B 页面，此时
      //      「保存目录」会把 A 的整棵树写进 B（跨方案整表重建）——与 R215
      //      selectedSection 跨方案写库同族，树是最后一块漏清的页面态。
      //    ② clearUnknownTask()：未知态是页面级状态且含任务归属，A 的未知任务
      //      横幅残留到 B 页会被「重新接管」触发跨方案接管（FE-3 接管入口已有
      //      归属校验兜底，此处是显示层复位——横幅不该出现在异方案页面）。
      //    切换是同步点：这里显式清空是唯一权威时机（R193 D-02 同判据）。
      setTree([]);
      clearUnknownTask();
    }
    initialTabDecidedRef.current = false;
    // ✅ R191 修复（切方案列表残留）：新方案列表请求是异步的，成功回写前
    //    docList 仍是上一个方案的数据——先清空占位，避免「新方案页面闪现
    //    旧方案文档」与请求失败时永久残留。
    setDocList([]);
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
    // ✅ R216（2026-11-11 · P3 · D-5）：切方案复位清单补 failedOutlineChapters ——
    //    消费点（useOutlineEditTreeRender / useOutlineViewTree）用标题子串互相
    //    匹配上橙色（node.title.includes(t) || t.includes(node.title)），
    //    「施工准备」这类通用章名跨方案必然撞车 ⇒ 旧方案的失败章标题
    //    在新方案树上误高亮。同块内 factsLogs 等态都已清，此态独漏。
    setFailedOutlineChapters([]);
    // ✅ R189（2026-11-08 · 切方案态残留三件套）：
    //    ① factsPagination 不清 → 新方案首屏可能短暂沿用旧方案的 total/pageSize；
    //    ② factsPageSchemeRef 不清 → resolveFactsLoadPage 在「切走再切回」时
    //       把旧方案 id 当成"同方案"，沿用切走前的非首页 offset；
    //    ③ factsDiagnosticsLoading 不清 → 旧诊断请求 finally 里的
    //       `requestedSchemeId === currentSchemeIdRef.current` 判据不成立，
    //       永远没人把 loading 置回 false，新方案诊断面板一直转圈。
    setFactsPagination(null);
    factsPageSchemeRef.current = "";
    setFactsDiagnosticsLoading(false);
    // ✅ BUG-06 前端半边（R176 · 2026-11-04）：页码回第 1 页，与
    //    resolveFactsLoadPage 的「新方案首请求 offset=0」共同保证不再带旧页码。
    factsPageRef.current = 1;
    setFactsPage(1);
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
      previewAbortRef.current?.abort();
      presetListAbortRef.current?.abort();
      exportCheckAbortRef.current?.abort();
      cacheAbortRef.current?.abort();
      exportAbortRef.current?.abort();
      // ✅ R197（F1）：审核与预检域三 ref 卸载兜底（CR 两 ref 由 hook 卸载
      //    cleanup 收口）—— 离开方案页后 AI 不再继续计费、不再向已卸载组件 setState
      expertAbortRef.current?.abort();
      checkAbortRef.current?.abort();
      auditAbortRef.current?.abort();
      consistencyLatestAbortRef.current?.abort();
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

  // ============================================================
  // ✅ R174 F2（2026-11-03）：重挂放弃后的显式可恢复未知态
  // 旧缺陷：pollTaskUntilTerminal 200×3s 耗尽 return null → 三条 SSE 链对
  // hit.final==null 无任何处理，finally 复位 generating —— 进度卡消失、页面
  // 静默回空闲，但后端任务仍在跑（AI 继续计费），用户重新点生成即并发/重复计费。
  // 现约定：
  //   ① 进入未知态时必须有明确提示（markUnknownTask 的 warning + 页面横幅）；
  //   ② 至少一个手动恢复入口（UnknownTaskBanner 的「重新接管任务」按钮）；
  //   ③ 未知态下重新生成必须经 guardUnknownTaskGeneration 二次确认
  //     （先查一次任务态：仍在跑 → 自动接管；已终态/不存在 → 放行）。
  // 恢复入口为用户手动触发，不做无限轮询刷爆后端。
  // ============================================================
  const UNKNOWN_TASK_TYPE_LABEL: Record<string, string> = {
    outline: "目录生成",
    content: "正文生成",
    facts: "全局事实提取",
  };

  // ✅ G10：未知态状态机迁至 useUnknownTaskRecovery.tsx；hasUnknownTask() 替代原 ref 直读。
  // ✅ R216（2026-11-11 · P2 · D-1）：未知态手动接管 outline 终态的恢复回调 ——
  //    经 ref 间接引用：handleOutlineReady / openOutlineGate 在 :2170+ 才由
  //    hook 解构（R194 FIX-16 同款考量：用「声明在前且稳定」的通道，避免
  //    TDZ 与陈旧闭包）；真实实现见 :2277 后的 ref 赋值处。
  const outlineRestoreFnRef = useRef<(fin: TaskTerminalInfo) => void>(() => {});
  // ✅ R224（F17）：facts 终态消费函数（真实实现在 useFactsGeneration 内，
  //    声明在本 ref 之后 —— 经 ref 间接注入，规避 TDZ；与 outlineRestoreFnRef
  //    同模式）。
  const factsTerminalFnRef = useRef<((fin: TaskTerminalInfo) => void) | null>(null);
  const {
    unknownTask,
    setUnknownTask,
    markUnknownTask,
    clearUnknownTask,
    handleReattachUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
  } = useUnknownTaskRecovery({
    id,
    msg,
    modal,
    generating,
    genSeqRef,
    activeTaskIdRef,
    setGenerating,
    setGenType,
    setTaskPaused,
    setProgress,
    setProgressMsg,
    load,
    pollTaskUntilTerminal,
    // ✅ R194（FIX-16）：content 任务手动接管终态消费 content_result —— 与断线
    //    重挂路径（handleGenerateContent 的 hit.final 分支）同一对纯函数同口径。
    //    用 mergeFailedSectionsIntoRef（:1019，声明在前且 useCallback 稳定），
    //    不用 :2255 的薄包装（声明在后，此处引用会 TDZ）。
    onContentResult: (fin) => {
      const _ck = contentResultFailedSections(fin.content_result);
      if (_ck.sections.length) {
        mergeFailedSectionsIntoRef(_ck.sections);
        msg.warning(`后台正文生成有 ${_ck.failedCount} 章失败，已在下方日志列出明细（可逐章重试）`);
      }
      const _sum = contentResultSummary(fin.content_result);
      if (_sum.total > 0) {
        const _overMsg = _sum.overCount > 0 ? `，${_sum.overCount} 章超字数` : "";
        // ✅ R199：断线重挂摘要透出配图数（此前图表产出明细重挂后整体丢失）
        const _chartMsg = _sum.charts > 0 ? `，配图 ${_sum.charts} 张` : "";
        msg.info(`后台正文生成：已完成 ${_sum.done}/${_sum.total} 章，共 ${_sum.words} 字${_overMsg}${_chartMsg}`);
      }
    },
    // ✅ R216（2026-11-11 · P2 · D-1）：outline 任务手动接管终态消费 outline_result
    //    —— 目录生成不写 sections（必须经一级闸门确认才入库），completed 只
    //    msg.success+load() 刷不出任何东西 ⇒ checkpoint 里的目录成果凭空丢失，
    //    用户只能重新生成（重复计费）。镜像 onContentResult 注入。
    onOutlineResult: (fin) => outlineRestoreFnRef.current(fin),
    // ✅ R224（F17）：facts 任务手动接管终态消费 facts_result —— segment_stats /
    //    cross_conflicts 此前只在断线重挂（useFactsGeneration reattach 分支）恢复，
    //    手动接管与刷新接管两路缺口（content=R194 FE-16 / outline=R216 D-1 同族）。
    onFactsResult: (fin) => factsTerminalFnRef.current?.(fin),
    unknownTaskTypeLabel: UNKNOWN_TASK_TYPE_LABEL,
  });

  // ✅ 页面加载/刷新后：检测后台遗留的进行中任务并重新挂接（轮询到终态后自动刷新）
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const { data } = await tasksApi.list(id);
        const allRunning = (data.tasks || []).filter(
          (t: TaskListItem) => t.status === "running" || t.status === "paused",
        );
        // ✅ R224（F3）：僵尸判定移到 active 选取**之后**且阈值 0.95 → 0.99 ——
        //   一致性/字数批调度阶段在 0.95~0.98 属合法长时间运行（R217 FIX-10/11
        //   确立；后端 R224 起 DB progress 运行期封顶 0.98），0.95 会把刷新期间
        //   的合法阶段误判成「已完成」且**不接管**（停止/暂停按钮消失、真终态后
        //   无人刷新）；且旧实现先于 active 选取 return，一条高进度行会挡住同
        //   方案真正在跑任务的接管。0.99 与 taskPolling.stuckAtFull 兜底同阈值，
        //   且只对「将要接管的 active」判僵尸。
        const active = allRunning.find(Boolean);
        if (!active || cancelled) return;
        if ((active.progress || 0) >= 0.99) {
          console.warn("resume: 发现 progress≥99% 但 status 仍 running 的僵尸任务，直接 load 刷新");
          msg.success("检测到后台任务已完成，正在刷新");
          load();
          return;
        }
        msg.info(
          `检测到后台「${TASK_TYPE_LABEL[active.task_type] || active.task_type}」任务仍在进行（${Math.round((active.progress || 0) * 100)}%），完成后自动刷新`,
          6,
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
        // ✅ R194（FIX-12 · C-1 接管轮询韧性）：单次网络失败即静默弃管（旧实现
        //    catch 块内直接 return）—— generating=true 永久残留（全部生成按钮
        //    disabled、进度冻结、「完成后自动刷新」失效），与 R174 F2「绝不静默
        //    回空闲」纪律冲突（该纪律只覆盖了 pollTaskUntilTerminal 调用方，漏了
        //    这条独立轮询循环）。现对齐 taskPolling 的 5 次连续失败容错：单次失败
        //    继续轮询；连续失败按未知态收尾（markUnknownTask 横幅 + 手动重接入口）。
        let pollFailStreak = 0;
        for (let i = 0; i < 400; i++) {
          await new Promise((r) => setTimeout(r, 3000));
          if (cancelled) return;
          try {
            const { data: st } = await tasksApi.status(active.id);
            pollFailStreak = 0;
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
              // ✅ R194（FIX-16 · C-9 三路口径统一）：content 任务的终态此前只走
              //    「msg + load()」，checkpoint 白名单里的 content_result（失败明细 /
              //    进度摘要）在接管终态下无人消费 —— 断线重挂路径（handleGenerate
              //    Content 的 hit.final 分支）与在线 completed 分支都能看到「N 章失败
              //    / 共 X 字」，刷新接管后 sectionLogs 为空、失败明细永久不可见。
              //    现与重挂路径同口径消费（同一对纯函数，无第三副本）。
              if (active.task_type === "content_generation") {
                const _ck = contentResultFailedSections(st.content_result);
                if (_ck.sections.length) {
                  mergeFailedSections(_ck.sections);
                  msg.warning(`后台正文生成有 ${_ck.failedCount} 章失败，已在下方日志列出明细（可逐章重试）`);
                }
                const _sum = contentResultSummary(st.content_result);
                if (_sum.total > 0) {
                  const _overMsg = _sum.overCount > 0 ? `，${_sum.overCount} 章超字数` : "";
                  // ✅ R199：断线重挂摘要透出配图数
                  const _chartMsg = _sum.charts > 0 ? `，配图 ${_sum.charts} 张` : "";
                  msg.info(
                    `后台正文生成：已完成 ${_sum.done}/${_sum.total} 章，共 ${_sum.words} 字${_overMsg}${_chartMsg}`,
                  );
                }
              }
              // ✅ R224（F17b）：facts 任务的刷新接管终态消费 facts_result ——
              //    segment_stats / cross_conflicts 此前只在断线重挂恢复；与
              //    content（R194 FE-16）/ outline（R216 D-3）同批补齐三路矩阵。
              if (active.task_type === "facts_generation") {
                factsTerminalFnRef.current?.(st);
              }
              const ckpt = st.outline_result;
              if (
                active.task_type === "outline_generation" &&
                ckpt?.outline &&
                Array.isArray(ckpt.outline) &&
                ckpt.outline.length
              ) {
                // ✅ 刷新页面后恢复后台跑完的目录成果（与在线 completed 走同一闸门）
                setTree(buildTree(ckpt.outline));
                // ✅ R216（2026-11-11 · P2 · D-3）：刷新接管补消费截断/复用信号 ——
                //    R213 FIX-14 收口了 completed/stopped/断线重挂三条路径，
                //    刷新接管是漏网的第 4 条（checkpoint 白名单带 level1_* /
                //    resumed_chapters，此前此处零感知）。
                const _takeoverTruncation = outlineTruncationNotice(ckpt);
                if (_takeoverTruncation) {
                  msg.warning(_takeoverTruncation);
                  pushOutlineLog(1, `⚠️ 一级目录超上限（接管）：${_takeoverTruncation}`);
                }
                if (typeof ckpt.resumed_chapters === "number" && ckpt.resumed_chapters > 0) {
                  msg.info(`已复用上次中断生成的 ${ckpt.resumed_chapters} 章（跳过对应 AI 调用）`);
                  pushOutlineLog(1, `♻️ 复用上次成果 ${ckpt.resumed_chapters} 章`);
                }
                if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                  setFailedOutlineChapters(ckpt.failed_chapters);
                  msg.warning(
                    `后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`,
                  );
                } else {
                  msg.success("后台目录生成已完成，请确认保存");
                }
                // ✅ R74（G1）：接管恢复路径与在线 completed 同口径呈现审核依据
                //    （checkpoint 白名单含 review，此前无处安放）。
                summarizeOutlineReviewBasis(ckpt.review).logs.forEach((line) =>
                  pushOutlineLog(1, line),
                );
                if (outlineAutoConfirmEnabled) {
                  handleOutlineReady(ckpt.outline, ckpt.auto_confirm);
                } else {
                  openOutlineGate(ckpt.outline);
                }
              } else if (st.status === "completed" && active.task_type !== "facts_generation") {
                // facts_generation 的 completed/degraded 已由 factsTerminalFnRef
                // 消费（统计恢复 + loadFacts，R224 F17b），不走通用分支防双提示。
                msg.success("后台任务已完成，正在刷新");
                load();
              } else if (st.status === "degraded" && active.task_type !== "facts_generation") {
                // facts_generation 的 degraded 已由 factsTerminalFnRef 消费（R224 F17b）
                // ✅ 2026-10-10：降级完成同样要刷新（正文已落库），但必须告警
                // ✅ R89 S2：目录检查点写失败也会降级 —— 成果只经 SSE 送出一次，
                //    刷新后 checkpoint_json 为空、无法恢复，因此文案同时覆盖两种成因。
                msg.warning(
                  "后台任务降级完成：成稿可能含未修复缺陷，或成果未能写入检查点（刷新后可能无法恢复，建议立即保存）",
                );
                load();
              } else if (st.status === "failed") {
                msg.error(`后台任务失败：${st.message || ""}`);
              } else if (st.status === "stopped") {
                // ✅ R89 S4：接管路径此前对 stopped 无分支 —— 用户主动停止后台
                // 目录生成后刷新页面，这里静默 return，进度条与状态停在接管前。
                msg.info(`后台任务已停止${st.message ? `：${st.message}` : ""}`);
                // ✅ R224（F17）：stopped 前已逐章落库的正文不 load() 永不显示
                //   （断线重挂 :3016 与手动接管 :178 都 load）—— 三路恢复矩阵补
                //   齐最后一条。outline 例外：checkpoint 成果恢复不落 sections，
                //   load() 会把刚恢复的树预览冲掉（R223 FE-1 同纪律）。
                if (active.task_type !== "outline_generation") {
                  load();
                }
              }
              return;
            }
            // ✅ 进度已满但 status 没更新：3 次连续就兜底
            if ((st.progress || 0) >= 0.99) {
              stuckAtFull++;
              if (stuckAtFull >= 3) {
                console.warn(
                  `resume: task ${active.id} stuck at 100% for ${stuckAtFull} polls, treating as completed`,
                );
                resetTakeover();
                msg.success("后台任务已完成，正在刷新");
                load();
                return;
              }
            } else {
              stuckAtFull = 0;
            }
          } catch {
            pollFailStreak++;
            if (pollFailStreak >= 5) {
              resetTakeover();
              // ✅ R223（FE-2）：标记前校验世代与取消 —— 切方案后（cancelled 由
              //    effect cleanup 置位 / genSeq 被 bump）旧方案任务不得被标到新页面。
              if (!cancelled && genSeqRef.current === takeoverSeq) {
                markUnknownTask({
                  taskId: active.id,
                  genType: gt || genTypeMap[active.task_type] || "content",
                  schemeId: id,
                  reason: "接管轮询连续网络失败（5 次），任务状态未知",
                });
              }
              return;
            }
            // 单次/少量失败：继续轮询（后端重启、网络抖动均可自愈）
          }
        }
        // ✅ R194（FIX-12）：循环耗尽（400×3s ≈ 20 分钟无终态）—— 旧实现直接
        //    落出 for 循环无任何收尾，同样把 generating 永久卡死。长正文任务
        //    （30+ 章轻松超过 20 分钟）每次都必然触发。现按未知态收尾。
        // ✅ R223（FE-2）：同上，标记前校验世代与取消。
        resetTakeover();
        if (!cancelled && genSeqRef.current === takeoverSeq) {
          markUnknownTask({
            taskId: active.id,
            genType: gt || genTypeMap[active.task_type] || "content",
            schemeId: id,
            reason: "接管轮询超限（400×3s）未获得任务终态",
          });
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
          // ✅ R217（2026-11-11 · P2）：页面级「切换章节 → 放弃更改」是
          //    **第二个**放弃入口，R211 FIX-3 只接了卡片内的「取消/放弃」按钮。
          //    旧实现仅清内存态，localStorage 草稿仍在 ⇒ 点回原章「手动编辑」
          //    又弹「发现未保存的草稿」（R211 症状在另一路径复活）。
          //    注意此刻 selectedSection 已指向新章，草稿 key 必须按 prevKey
          //    （旧章）构造；格式与 :1040 的 draftKey 逐字同源（护栏锁同源）。
          if (id && prevKey) {
            try {
              localStorage.removeItem(`scheme_draft_${id}_${prevKey}`);
            } catch {
              /* ignore */
            }
          }
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
      // ✅ R205（前后端载荷对齐）：事实链 warning 只带 warnings 数组不带 message
      //    （sse_handlers generate_facts），旧实现恒弹兜底文案「存在需人工核对的
      //    提示」+ 事实链本地日志另出一条 ⇒ 双提示且真实告警内容不可见。
      const _warnParts = Array.isArray(evt.warnings)
        ? (evt.warnings as unknown[])
            .map((w) => (typeof w === "string" ? w : (JSON.stringify(w) ?? String(w))))
            .filter((w): w is string => Boolean(w))
        : [];
      msg.warning(evt.message || (_warnParts.length ? _warnParts.join("；") : "存在需人工核对的提示"));
      return false;
    }
    // ✅ R190（FIX-4）：上下文降级可见信号（D-39）。此前该事件无 message 键，
    //    通用分支 pushOutlineLog("") 直接整条丢弃 ⇒ 知识库读取失败、装配段截断
    //    用户全程不可见。现按 field/stage/reason/字符数拼提示（不中断流，仅告警）。
    if (evt.event === "context_degraded") {
      const d = evt as ContextDegradedEvent;
      const what =
        d.reason === "segment_failed"
          ? "装配段失败（整段上下文缺席）"
          : "内容被截断";
      const span =
        typeof d.original_chars === "number" && typeof d.kept_chars === "number"
          ? ` ${d.original_chars}→${d.kept_chars} 字`
          : "";
      msg.warning(`上下文降级：${d.field || "未知字段"}（${d.stage || "生成阶段"}）${what}${span}，生成质量可能受影响`);
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
      // ✅ R205（同名不同 payload）：正文 build_error_payload 与 stopped/completed
      //    同带标准校验与事实冲突修复汇总，旧 error 分支只读 message → 异常终态
      //    下「已执行但未成功」的两类留痕完全不可见。纯函数，零闭包依赖。
      const _errStdHint = standardSummaryHint(evt.standard_summary);
      if (_errStdHint) msg.warning(_errStdHint);
      const _errFaHint = factAutofixHint(evt.fact_autofix_summary);
      if (_errFaHint) msg.info(_errFaHint);
      // ✅ 修复：error 后中断 SSE 连接（旧实现仅 break 未 abort，后端任务继续运行，
      // 用户此时再次点生成会造成新旧两个任务并发写同一方案）
      abortControllerRef.current?.abort();
      return true;
    }
    return false;
  }, []);

  /** SSE 统一推送进度日志（供 onSseEvent、doGenerateContent 等多处复用） */
  const pushOutlineLog = (p: number, m: string, done?: number, total?: number) => {
    if (!m) return;
    const list = outlineLogsRef.current;
    if (list.length && list[list.length - 1].message === m) return;
    // ✅ 修复：短方案一次性直出链路的 progress 事件内嵌统计快照（done=0/total=0，
    //    无章节维度），原样写入会渲染出无意义的「0/0 章」。仅长方案分步链路
    //    （total>0）才记录章节计数。
    const hasChapterDim = typeof total === "number" && total > 0;
    // ✅ 2026-11-11 性能：写入端有界（渲染层的 OUTLINE_LOG_RENDER_MAX 只截断 DOM，
    //    旧实现数据源仍无限追加并每次 O(n) 复制）。语义保持不变：新数组引用 + 最近 N 条。
    outlineLogsRef.current = appendBoundedLog(list, {
      progress: p,
      message: m,
      done: hasChapterDim ? done : undefined,
      total: hasChapterDim ? total : undefined,
      time: Date.now(),
    });
    setOutlineLogs(outlineLogsRef.current);
  };

  // ✅ 2026-11-04 拆分（G4 续）：目录写入提示与确认闸门迁至
  //    schemeWorkbench/useOutlineGate.tsx（函数体逐字搬运）。
  const {
    saveRequirements,
    notifyClearedContent,
    notifyGenericResidual,
    openOutlineGate,
    doSaveGate,
  } = useOutlineGate({
    id,
    scheme,
    msg,
    modal,
    requirements,
    treeRef,
    hasContentRef,
    load,
  });

  // ✅ R179 N1（2026-11-05）：目录自动确认编排。
  //    开关关闭（默认）时 `handleOutlineReady` 逐字委托 `openOutlineGate`，
  //    人工路径行为与旧版完全一致（有等价性用例锁定）。
  const {
    handleOutlineReady,
    cancelAuto: cancelOutlineAutoConfirm,
    modal: outlineAutoConfirmModal,
    banner: outlineAutoConfirmBanner,
  } = useOutlineAutoConfirm({
    id,
    enabled: outlineAutoConfirmEnabled,
    delayMs: outlineAutoConfirmDelayMs,
    hasExistingContent: () => hasContentRef.current || hasAnyContent(treeRef.current),
    openOutlineGate,
    saveAuto: async (outline, snapshotBefore) => {
      if (!id) return false;
      try {
        const { data } = await sectionsApi.saveOutline(id, {
          outline,
          source: "ai",
          lock_roots: true,
          snapshot_before: snapshotBefore,
        });
        msg.success(`目录已自动确认保存（${outline.length} 个一级章节已锁定）`);
        // ✅ R188（L9）：后端 R179 N1 / R187 早已回传 snapshot_id 与 roots_locked，
        //    此前前端零消费 → 用户不知道「已创建可回退快照」，也不知道一级未锁定。
        if (data?.snapshot_id) {
          msg.info(
            `已创建可回退快照（${String(data.snapshot_id).slice(0, 8)}），可在「目录版本」面板一键回退`,
          );
        }
        if (data?.roots_locked === false) {
          msg.warning("本次保存未锁定一级章节，后续生成可能改动顶层结构");
        }
        notifyClearedContent(data);
        notifyGenericResidual(data);
        pushOutlineLog(1, "⚡ 自动确认已生效（保存前已创建可回退的目录快照）");
        load();
        return true;
      } catch (saveErr: unknown) {
        const err = saveErr as { message?: string };
        msg.error(`自动确认保存失败：${err?.message || "未知错误"}，已切回人工确认`);
        return false;
      }
    },
    onSwitchToManual: async () => {
      try {
        await systemApi.updateOutlineAutoConfirm({ enabled: false });
        setOutlineAutoConfirmEnabled(false);
        msg.info("已切回人工确认（目录自动确认已关闭）");
      } catch (e: unknown) {
        const err = e as { message?: string };
        msg.error(`切回人工失败：${err?.message || "未知错误"}，请到 AI 配置页关闭开关`);
      }
    },
    msg,
  });

  // ✅ G12：目录生成主链迁至 useOutlineGeneration.tsx（函数体逐字搬运）。
  const { handleGenerateOutline } = useOutlineGeneration({
    id,
    msg,
    modal,
    tree,
    setTree,
    hasContentRef,
    openOutlineGate,
    autoConfirmEnabled: outlineAutoConfirmEnabled,
    handleOutlineReady,
    notifyClearedContent,
    notifyGenericResidual,
    load,
    pushOutlineLog,
    onSseEvent,
    summarizeOutlineReviewBasis,
    setActiveTab,
    setGenerating,
    setGenType,
    setProgress,
    setProgressMsg,
    setGenStats,
    setOutlineLogs,
    outlineLogsRef,
    setFailedOutlineChapters,
    genSeqRef,
    activeTaskIdRef,
    abortControllerRef,
    sseBatcherRef,
    markUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
    pollTaskUntilTerminal,
  });

  // ✅ R216（2026-11-11 · P2 · D-1）：未知态手动接管 outline 终态恢复（真实实现，
  //    经 outlineRestoreFnRef 间接注入 useUnknownTaskRecovery）。与刷新接管路径
  //    （:1939 outline 分支）和断线重挂路径（useOutlineGeneration reattach 分支）
  //    消费同一组信号：树恢复 + 截断/复用信号 + 失败章高亮 + 审核依据 +
  //    一级目录闸门/自动确认。⚠️ 三处消费矩阵此前不对齐（未知态接管完全不消费
  //    outline_result ⇒ completed 只弹「已完成」而目录丢失，用户被迫重新生成
  //    重复计费），前端护栏成对锁定（outlineR216.test.ts）。
  outlineRestoreFnRef.current = (fin) => {
    const ckpt = fin.outline_result;
    if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
      // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），收窄一次供闭包复用
      const restoredOutline = ckpt.outline as OutlineNodeInput[];
      // ✅ 刷新页面后恢复后台跑完的目录成果（与在线 completed 走同一闸门）
      setTree(buildTree(restoredOutline));
      const _takeoverTruncation = outlineTruncationNotice(ckpt);
      if (_takeoverTruncation) {
        msg.warning(_takeoverTruncation);
        pushOutlineLog(1, `⚠️ 一级目录超上限（接管）：${_takeoverTruncation}`);
      }
      if (typeof ckpt.resumed_chapters === "number" && ckpt.resumed_chapters > 0) {
        msg.info(`已复用上次中断生成的 ${ckpt.resumed_chapters} 章（跳过对应 AI 调用）`);
        pushOutlineLog(1, `♻️ 复用上次成果 ${ckpt.resumed_chapters} 章`);
      }
      if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
        setFailedOutlineChapters(ckpt.failed_chapters);
        msg.warning(
          `后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`,
        );
      } else {
        msg.success("后台目录生成已完成，请确认保存");
      }
      summarizeOutlineReviewBasis(ckpt.review).logs.forEach((line) => pushOutlineLog(1, line));
      if (outlineAutoConfirmEnabled) {
        handleOutlineReady(restoredOutline, ckpt.auto_confirm);
      } else {
        openOutlineGate(restoredOutline);
      }
    } else if (fin.status === "failed") {
      // 无可恢复成果的失败：与刷新接管 failed 分支同文案口径
      msg.error(fin.message || "后台任务失败");
    } else if (fin.status === "stopped") {
      msg.info(`后台任务已停止${fin.message ? `：${fin.message}` : ""}`);
      load();
    } else if (fin.status === "completed" || fin.status === "degraded") {
      // 无 checkpoint 成果（旧后端 / 检查点写失败）：与刷新接管 else 分支同口径
      msg.success("后台任务已完成，正在刷新");
      load();
    }
  };

  /**
   * ✅ R66（2026-10-11）四档字数控制：档位数值表（与后端真源
   * content_utils.WORD_BUDGET_TIERS 一致，前端仅用于预估展示）。
   */
  const WORD_BUDGET_TIER_VALUES: Record<string, number> = {
    brief: 800,
    standard: 1200,
    detailed: 2000,
    ultra: 2500,
  };

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
    // ✅ R174 F2：未知态下重新生成必须先查询一次任务状态（防并发/重复计费）。
    //    覆盖所有正文生成入口（预估弹窗 / 重写 / 续写 / 补全的最终汇合点）。
    if (hasUnknownTask()) {
      guardUnknownTaskGeneration("正文生成", () => void handleGenerateContent(extra));
      return;
    }
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = ""; // R106 FIX-8 · 正文链
    // 创建新的 AbortController，取消上一次未完成的 SSE
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：每次生成任务创建独立的 rAF 批处理器。先把上一次任务的
    //    残留批处理排空并 stop，再启用新实例，确保 stop() 的 stopped 标记
    //    不会污染本次任务的 schedule() 调用。
    //    ✅ R194（FIX-15 · C-10 顺序对齐）：stop() 内部先 pending.clear() 再置
    //    stopped —— 旧序「stop(); flushNow();」的 flushNow 恒刷空 Map，「排空
    //    残留」从未发生，上一任务最后一帧的更新被静默丢弃；与本函数尾部
    //    （finally）的正确顺序 flushNow→stop 自相矛盾。现对齐。
    sseBatcherRef.current?.flushNow();
    sseBatcherRef.current?.stop();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    // ✅ R227（修复10）：新一轮生成开始时复位终态信号（上一任务可能 stopped）。
    setTaskTerminal(false);
    setSseReconnecting(false);
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
    // ✅ R194（FIX-13）：本协程是否执行过重挂回填（退出时只清自己写的 ref）
    let _reattachBackfilled = false;
    try {
      for await (const raw of sseFetch(`/sse/generate-content/${id}`, body, {
        signal: ac.signal,
      })) {
        // 后端 SSE 契约：正文生成链事件，一次性断言（字段见 ContentGenEvent）
        const evt = raw as ContentGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (onSseEvent(evt)) {
          // ✅ 二次增强：任务终止（error）后收尾残留的「进行中」日志项，不再转圈
          finalizeRunningLogs("生成失败（任务已终止）", "failed");
          break;
        }

        // ✅ R205（后端推·前端零消费）：字数调整阶段四事件此前无任何分支，
        //    长任务期间进度文案冻结在上一阶段，用户不知道正在扩写/压缩。
        if (
          evt.event === "word_adjust_start" ||
          evt.event === "word_adjust_batch_start" ||
          evt.event === "word_adjust_batch_done" ||
          evt.event === "word_adjust_done"
        ) {
          const wa = evt as Record<string, unknown>;
          let waMsg = "";
          if (evt.event === "word_adjust_start") {
            waMsg = `字数调整中：不足 ${Number(wa.under) || 0} 章 / 超出 ${Number(wa.over) || 0} 章，共 ${Number(wa.batch_count) || 0} 批`;
          } else if (evt.event === "word_adjust_batch_start") {
            waMsg = `字数调整 ${Number(wa.batch) || 0}/${Number(wa.batch_total) || 0} 批进行中`;
          } else if (evt.event === "word_adjust_batch_done") {
            waMsg = `字数调整 ${Number(wa.done) || 0}/${Number(wa.total) || 0} 章完成`;
          } else {
            waMsg = `字数调整完成（计划 ${Number(wa.planned) || 0} 章，已调整 ${Number(wa.adjusted) || 0} 章）`;
          }
          // ✅ 2026-11-11 性能：逐批事件改为 rAF 合帧。终态分支（:2727）会先
          //    flushNow 再写最终文案，故不会把旧批次文案回写覆盖终态。
          batcher.schedule("progressMsg", () => setProgressMsg(waMsg));
          // 批次事件只更新文案，避免逐批刷屏日志（首/末事件写日志留痕）
          if (evt.event === "word_adjust_start" || evt.event === "word_adjust_done") {
            pushOutlineLog(typeof evt.progress === "number" ? evt.progress : 0, waMsg);
          }
          continue;
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
            // ✅ R208：R66 起后端随 section_done 下发偏差/达成率/容差判定，
            //    此前前端零声明零消费，日志区无法追溯字数是否落在容差带。
            word_deviation: evt.word_deviation,
            word_ratio: evt.word_ratio,
            word_within_band: evt.word_within_band,
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
            // ✅ R215（2026-11-11 · P2）：体积闸门降级握手（R181）—— 后端超限时
            //    只截 content 并附 truncated 系列键（注释明言「前端可据 truncated=true
            //    提示并重新拉取」），前端此前照单全收 ⇒ 预览显示被砍尾的正文而字数
            //    Tag 仍是全量值，且截断态驻留到终态 load()。现在不落截断内容（保留
            //    prev，完整内容以终态 load() 回读为准）并提示一次。
            const _degraded = evt.truncated === true;
            if (_degraded) {
              msg.warning(`「${evt.title || evt.section_id}」正文超长，预览已截断显示（完整内容生成结束后自动刷新）`);
            }
            setSelectedSection((prev) =>
              prev
                ? {
                    ...prev,
                    content: !_degraded && typeof evt.content === "string" ? evt.content : prev.content,
                    // ✅ R227（修复9）：`||` 把合法的 0 当假值 —— 空章节（word_count=0 /
                    //    word_budget=0）会被 prev 覆盖成旧值。改用 `??`（仅 null/undefined
                    //    回退），0 字空章不再被上一事件覆盖。
                    word_count: evt.word_count ?? prev.word_count,
                    word_budget: evt.word_budget ?? prev.word_budget,
                    status: "generated",
                  }
                : prev,
            );
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
          // ✅ R175 BUG-3：单章阶段事件自带加权进度（后端 section_stage.progress）。
          //    旧实现只更新阶段文案、丢弃进度，单章数分钟内进度只能靠 10s 心跳，
          //    观感上「卡住」。现与心跳走同一单调护栏，单章推进即可见且不回退。
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress((prev) => nextProgress(prev, p)));
          }
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ 进度增强：运行统计（含心跳通道周期推送的 ping）——单章 AI 调用期间
          //    唯一的实时通道，保证已耗时 / ETA / 累计字数 / 进行中章节持续刷新。
          // ✅ 性能优化：rAF 批处理 + 相同值去重，避免高频事件引发多余渲染。
          const statsSnapshot = pickStats(evt);
          batcher.schedule("genStats", () =>
            setGenStats((prev) => (statsEqual(prev, statsSnapshot) ? prev : statsSnapshot)),
          );
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            // ✅ R175 BUG-3：心跳是 raw 进度快照（存在 running 移除与 done 自增
            //    之间的窗口），必须与目录链 :2650 同模式加单调守卫，否则进度条回退。
            batcher.schedule("progress", () => setProgress((prev) => nextProgress(prev, p)));
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
          batcher.schedule("progressMsg", () =>
            setProgressMsg(evt.message || "正在扫描全文一致性..."),
          );
        } else if (evt.event === "consistency_scan_done") {
          batcher.schedule("progress", () => {
            setProgress(0.98);
            setProgressMsg(
              `一致性扫描完成：发现 ${evt.summary?.total ?? 0} 处冲突，正在定向修复...`,
            );
          });
        } else if (evt.event === "consistency_repair_progress") {
          if (typeof evt.progress === "number") {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
          }
          batcher.schedule("progressMsg", () =>
            setProgressMsg(evt.message || "正在定向修复冲突..."),
          );
        } else if (evt.event === "consistency_repair_done") {
          // ✅ R217（2026-11-11 · P2）：此处写死 1.0 是「阶段性事件提前封顶」——
          //    后端在这个事件之后还有跨章节字数批调度（word_adjust_*）才发
          //    completed，进度模型是 0.98 → 1.0。旧实现让用户看到进度条 100%
          //    却还有数分钟的「字数调整 1/3 批进行中」。改走单调护栏并保持
          //    阶段值 0.98，终态 completed 分支仍是唯一封顶点。
          batcher.schedule("progress", () => {
            setProgress((prev) => nextProgress(prev, 0.98));
            setProgressMsg(
              `一致性修复完成：已修复 ${evt.repaired ?? 0} 处（待你确认），跳过 ${evt.skipped ?? 0} 处（待人工），失败 ${evt.failed ?? 0} 处`,
            );
          });
          // ✅ G11：生成链路里的自动一致性修复改写了正文 → 审核 / 总检结论失效
          if ((evt.repaired ?? 0) > 0) setReviewTick((t) => t + 1);
        } else if (evt.event === "consistency_skipped") {
          // ✅ R217：与 consistency_repair_done 同族 —— 跳过一致性检查后
          //    仍可能进入字数批调度，不得提前封顶（唯一封顶点是 completed）。
          batcher.schedule("progress", () => {
            setProgress((prev) => nextProgress(prev, 0.98));
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
          // ✅ R227（修复10）：终态信号（completed/degraded）置位。
          setTaskTerminal(true);
          // ✅ 性能优化：终态事件立即 flush，确保 UI 与后续 load() 前的最后一次渲染一致
          batcher.flushNow();
          setProgress(1);
          // ✅ 优先采用后端统计的失败章节数（done_ids 口径，含未进入日志的失败）
          const failedCount =
            typeof evt.failed_count === "number"
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
          // ✅ R224（F23）：在线 completed 消费 run_words / over_count —— 后端
          //    R-1 口径键（本次落库字数/超字章数）此前只有类型声明零读取点，
          //    「在线横幅 vs 重挂摘要」口径在线侧实际仍无消费（键产出了但前端没有）。
          if (typeof evt.run_words === "number" && evt.run_words > 0) {
            const _runOver = typeof evt.over_count === "number" ? evt.over_count : 0;
            msg.info(`本次落库 ${evt.run_words} 字${_runOver > 0 ? `（${_runOver} 章超字数）` : ""}`);
          }
          const _stdHint = standardSummaryHint(evt.standard_summary);
          if (_stdHint) {
            msg.warning(_stdHint);
            setProgressMsg(`${doneMsg} | ${_stdHint}`);
          }
          // ✅ R215（2026-11-11 · P3）：顺序校验失守提示 —— 后端把「实际派发序 vs
          //    目录 DFS 序逐位比对」（R105 G1 的核心留痕信号）随 completed 下发，
          //    ok=false 时只打后端 WARNING，前端零消费 ⇒ 「章节推进序错乱」类
          //    反馈无从在界面取证。失守才说话（与 _stdHint 同纪律）。
          if (evt.order_check && evt.order_check.ok === false) {
            msg.warning("本次生成的章节完成顺序与目录顺序不一致（并发派发所致），章节内容不受影响");
          }
          // ✅ R77b（2026-10-15）：事实冲突定点修复汇总提示（与标准校验提示
          //    独立成条；无冲突返回空串 → 不追加，不制造噪音）
          const _faHint = factAutofixHint(evt.fact_autofix_summary);
          if (_faHint) {
            msg.info(_faHint);
            setProgressMsg(`${doneMsg} | ${_faHint}`);
          }
          // ✅ 全文一致性 Agent 修复结果摘要（后端随 completed 一并下发）
          if (evt.consistency_summary) {
            const cs = evt.consistency_summary;
            setCrSummary(cs);
            if (cs.scan_id) setCrScanId(cs.scan_id);
            if ((cs.total ?? 0) > 0) {
              msg.info(
                `全文一致性 Agent 修复：发现 ${cs.total} 处冲突，已修复 ${cs.repaired ?? 0} 处，` +
                  `${cs.failed ?? 0} 处失败 —— 可在「正文生成 → 修复工作台」逐条确认或回滚`,
              );
            }
          }
          load();
          break;
        } else if (evt.event === "stopped") {
          // ✅ R227（修复10）：终态信号（stopped）置位 —— progress 保留真实值（<1），
          //    有失败时进度条仍需显示 exception（对齐 completed 分支）。
          setTaskTerminal(true);
          // ✅ 性能优化：终态事件立即 flush，确保停止时的最终进度立即展示
          batcher.flushNow();
          // ✅ 二次增强：停止时把未完成章节收尾为"已停止"（不再转圈），
          //    并保留停止时的真实进度（后端 stopped 事件携带 progress）。
          //    ✅ R175 BUG-3：终态进度同样过单调护栏，避免收尾事件把进度拉回。
          if (typeof evt.progress === "number") {
            setProgress((prev) => nextProgress(prev, evt.progress));
          }
          // ✅ 缺口修复（2026-09-24）：后端 stopped 事件现已随事件下发
          //    failed_sections / failed_count（此前只落 checkpoint，在线路径拿不到）。
          //    与 completed 分支走同一合并入口，停止后立即可见失败明细。
          mergeFailedSections(evt.failed_sections);
          // ✅ R194（FIX-14 · C-5 stopped 摘要消费）：后端 stopped 载荷刻意携带
          //    standard_summary / fact_autofix_summary（_sse_content_payloads 注释
          //    明言「用户中途停止也要看得到修复成效」，R77b 三拼装点字段一致），
          //    前端 stopped 分支此前不消费 —— 同一事件族 completed/degraded 看
          //    得到「标准校验 N 章 / 事实修复 M 处」，stopped 看不到。现补齐，
          //    与 completed 分支同口径（无问题不追加、不制造噪音）。
          const _stoppedStdHint = standardSummaryHint(evt.standard_summary);
          if (_stoppedStdHint) {
            msg.warning(_stoppedStdHint);
          }
          const _stoppedFaHint = factAutofixHint(evt.fact_autofix_summary);
          if (_stoppedFaHint) {
            msg.info(_stoppedFaHint);
          }
          finalizeRunningLogs("已停止，未生成完成", "skipped");
          const stoppedFailed = typeof evt.failed_count === "number" ? evt.failed_count : 0;
          msg.info(
            stoppedFailed > 0 ? `已停止（${stoppedFailed} 章失败，详见下方日志）` : "已停止",
          );
          load();
          break;
        } else {
          // ✅ 修复：章节级事件（section_start/done/error、consistency_*）不携带
          // progress 字段，旧实现 `evt.progress || 0` 会把进度条打回 0 造成跳变。
          // 仅在事件真正携带进度/消息时才更新。
          if (evt.progress !== undefined) {
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress((prev) => nextProgress(prev, p)));
          }
          if (evt.message) {
            batcher.schedule("progressMsg", () => setProgressMsg(evt.message!));
          }
          // ✅ 进度增强：progress 事件随附的权威统计（ETA / 累计字数 / 进行中章节）
          if (evt.stats) {
            const statsSnapshot = pickStats(evt.stats);
            batcher.schedule("genStats", () =>
              setGenStats((prev) => (statsEqual(prev, statsSnapshot) ? prev : statsSnapshot)),
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
        // ✅ 可感知重连态：进入重挂先置位，进度卡 / 徽标 / 控制按钮同步进入「重连中」。
        setSseReconnecting(true);
        let hit: Awaited<ReturnType<typeof reattachTaskImpl>> = null;
        try {
          hit = await reattachTaskImpl(
            async () => (await tasksApi.list(id)).data,
            pollTaskUntilTerminal,
            {
              taskId,
              typeFilter: "content_generation",
              onReattach: () =>
                setProgressMsg(
                  "连接中断，正在重新挂接后台任务（后台任务可能仍在运行，请勿重复发起）...",
                ),
            },
          );
        } finally {
          // ✅ R217（2026-11-11 · P1）：旧协程不得清掉新方案的重连态
          if (genSeqRef.current === mySeq) setSseReconnecting(false);
        }
        // ✅ R217（2026-11-11 · P1）：重挂轮询 Promise **不受** abortController
        //    控制（pollTaskUntilTerminalImpl 无 signal 参数，最长 200×3s）。
        //    切方案复位块只 abort SSE controller，已进入轮询的旧协程仍会带着
        //    A 方案结果回来写当前页面的 msg / 失败日志 / load() ⇒ 跨方案串台。
        //    目录链 useOutlineGeneration 已有 `hit && genSeqRef.current === mySeq`
        //    同款世代守卫，正文链同构漏改；此处统一按世代判定是否消费结果。
        //    （数据层不串库：load() 内已有 requestedSchemeId 守卫，本项只管 UI。）
        const reattachStillCurrent = genSeqRef.current === mySeq;
        // ✅ R174 F2 / 本轮加固：轮询耗尽/连续网络失败（final=null）→ 未知态；
        //    连「可接管任务」都没检索到时同样无法确认后台是否仍在跑（AI 可能照常计费），
        //    一并进入持久可见的未知态，绝不静默回空闲。
        if (!hit && reattachStillCurrent) {
          markUnknownTask({
            taskId,
            genType: "content",
            schemeId: id,
            reason: "SSE 连接中断后未能检索到可接管的正文生成任务，无法确认后台是否仍在运行",
          });
        } else if (hit && !hit.final && reattachStillCurrent) {
          markUnknownTask({
            taskId: hit.taskId,
            genType: "content",
            schemeId: id,
            reason: "重挂轮询超限（200×3s）或连续网络失败，未获得任务终态",
          });
        }
        if (hit && reattachStillCurrent) {
          taskId = hit.taskId;
          // ✅ R194（FIX-13 · C-4 控制按钮可达性）：重挂期间回填 activeTaskIdRef。
          //    旧实现 taskId 只赋局部变量 —— 重挂轮询窗口（200×3s ≈ 10 分钟）内
          //    停止/暂停按钮可见（generating=true）但 handleControl 读到空 id，
          //    永远提示「任务正在建立连接，尚未取得任务标识」，用户明知任务在跑
          //    却无法停止（R106 FIX-8 清空 ref 是为防旧指令打到旧任务，重挂拿到
          //    **同一任务**的 id 后理应回填）。仅在 ref 为空时回填（SSE 阶段已由
          //    onSseEvent 写入真实 id 的场景不动），退出时只清自己写入的值。
          if (!activeTaskIdRef.current) {
            activeTaskIdRef.current = taskId;
            _reattachBackfilled = true;
          }
          const fin = hit.final;
          // ✅ R227（修复10）：断线重挂命中已终态任务（hit.final 仅终态才存在），
          //    显式置位终态信号 —— 重挂 stopped 任务的进度条 progress<1，须同样
          //    在「有失败」时显示 exception（对齐在线 stopped/completed 分支）。
          setTaskTerminal(true);
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
            // ✅ R199：断线重挂摘要透出配图数
            const chartMsg = summary.charts > 0 ? `，配图 ${summary.charts} 张` : "";
            msg.info(
              `后台正文生成：已完成 ${summary.done}/${summary.total} 章，` +
                `共 ${summary.words} 字${overMsg}${chartMsg}`,
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
      setSseReconnecting(false);
      setGenType("");
      setTaskPaused(false);
      // ✅ R194（FIX-13）：只清本路径回填的 ref（正常 SSE 流的 ref 由 onSseEvent
      //    写入、新任务启动时 R106 FIX-8 清空，此处不得越位）。
      if (_reattachBackfilled && activeTaskIdRef.current === taskId) {
        activeTaskIdRef.current = "";
      }
    }
    // ✅ 性能优化：任务结束或异常退出后，排空残余批处理并停止，
    //    避免旧任务的 schedule() 闭包在新任务已开始后才触发，污染 UI 状态。
    batcher.flushNow();
    batcher.stop();
    if (sseBatcherRef.current === batcher) sseBatcherRef.current = null;
  };

  // ✅ 2026-11-04 拆分（G7 姊妹组）：正文编辑动作迁至 schemeWorkbench/useSectionEditing.tsx
  //    （函数体逐字搬运，shrinking 同名返回，页面引用零改动）。
  const {
    saveSectionEdit,
    handleSectionStandardChange,
    handleSaveSchemeDefaultStandard,
    regenerateCurrentSection,
    handleShrinkCurrentSection,
    handleResetContent,
    confirmAndGenerate,
    shrinking,
  } = useSectionEditing({
    id,
    msg,
    modal,
    selectedSection,
    setSelectedSection,
    tree,
    setTree,
    generating,
    scheme,
    setScheme,
    generationStandard,
    concurrencyOption,
    wordBudgetOption,
    customWordBudget,
    forceFullRepair,
    setForceFullRepair,
    draftKey,
    setSavingSection,
    setIsEditingSection,
    setEditDirty,
    setReviewTick,
    setSavingStandardDefault,
    load,
    resolveWordBudgetTier,
    resolveWordBudgetOverride,
    wordBudgetTierValues: WORD_BUDGET_TIER_VALUES,
    handleGenerateContent,
  });

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

  // ✅ 2026-11-04 拆分（G11）：全局事实提取主链迁至
  //    schemeWorkbench/useFactsGeneration.ts（函数体逐字搬运）。
  const { handleGenerateFacts, handleRetryFailedSegments, consumeFactsTerminal } = useFactsGeneration({
    id,
    msg,
    modal,
    onSseEvent,
    loadFacts,
    markUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
    pollTaskUntilTerminal,
    activeTaskIdRef,
    abortControllerRef,
    sseBatcherRef,
    genSeqRef,
    setGenerating,
    setGenType,
    setProgress,
    setProgressMsg,
    factsLogsRef,
    setFactsLogs,
    setFactsSegmentStats,
    setFactsCrossConflicts,
    missingValueMode,
    // ✅ R193（D-08）：跨方案污染守卫——提取/重试的点击闭包可能落后于方案切换。
    currentSchemeIdRef,
    docList,
    factsSummary,
  });
  // ✅ R224（F17）：facts 终态消费注入 ref（真实实现在上方 hook 内，经 ref
  //    间接引用规避 TDZ —— 与 outlineRestoreFnRef.current 同模式）。
  factsTerminalFnRef.current = consumeFactsTerminal;

  // R-P1 拆分：上传/解析/预览/四层管线/分类/批量确认/过期解除/删除文档域处理器
  // 迁至 useDocumentWorkflow（调用点保持在原位；批确认/过期解除/删除段原在
  // 「提取项目」块之后，随域合并进同一 hook，见该文件头注释）。
  const {
    handleUploadDocuments,
    handleParseDocuments,
    handleParseDocument,
    handlePreviewDocument,
    handleCancelPreview,
    handlePipelineAction,
    handleCategoryChange,
    handleBatchResolve,
    handleAckStaleAll,
    handleAckStaleOne,
    handleDeleteDocument,
  } = useDocumentWorkflow({
    id,
    schemeProjectId,
    msg,
    modal,
    uploadingFacts,
    setUploadingFacts,
    setUploadedFiles,
    maxUploadBytes,
    maxUploadTotalBytes,
    maxUploadFiles,
    uploadAbortRef,
    parseAbortRef,
    previewAbortRef,
    parsingDocs,
    setParsingDocs,
    parsingDocId,
    setParsingDocId,
    docList,
    loadDocuments,
    setPreviewDoc,
    setPreviewText,
    setPreviewData,
    setPreviewLoading,
    setPreviewError,
    previewDoc,
    setPipelineStatus,
    setPipelineError,
    setPipelineLoading,
    pipelineAction,
    setPipelineAction,
    setDocList,
    loadFacts,
    factsSummary,
    factsListAbortRef,
    currentSchemeIdRef,
  });

  // ============================================================
  // 「提取项目」(bidAnalysis) 处理器
  // ============================================================

  // R-P1 拆分：「提取项目」域处理器（SSE/停止/多标段/校正/配置启动）迁至 useBidAnalysis
  const {
    runBaSse,
    handleStopBa,
    handleCheckSections,
    handleRerunItem,
    openBaEdit,
    handleBaSaveEdit,
    handleBaClearEdit,
    handleStartBaFromConfig,
  } = useBidAnalysis({
    id,
    schemeProjectId,
    msg,
    baSseAbortRef,
    setBaRunning,
    setBaProgress,
    setBaProgressMsg,
    setBaTextStats,
    setBaItems,
    baItems,
    baDefsRef,
    setBaSummary,
    setSelectedBaItem,
    loadBaResults,
    autoJumpFrom,
    baStoppingRef,
    setBaStopping,
    sectionChecking,
    setSectionChecking,
    setSectionCheckResult,
    baEditItem,
    setBaEditItem,
    baEditValue,
    setBaEditValue,
    setBaEditSaving,
    setBaConfigOpen,
    baModeOpt,
    baSelectedIds,
    baForceRerun,
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
  // R-P1 拆分：目录操作回调（保存/预算/导出/目录库/清除/上传识别/拖拽/编号提示）
  // 迁至 useOutlineActions；以下 state/ref 因是 hook deps（立即求值，避 TDZ）上移至此。
  // ✅ 修复（2026-09-20 深度审查）：导出入口重入保护。
  //    loading={exporting} 只能在重渲染后禁用按钮，React 状态更新异步批处理，
  //    同一帧内的双击 / 连点（包括 DOCX 与 PDF 两个按钮互点）仍会并发进入导出流程。
  //    用 ref 同步标志做函数级防护，确保任何时刻只有一个导出在进行。
  const exportingRef = useRef(false);
  const [exportingTree, setExportingTree] = useState(false);
  const [libError, setLibError] = useState("");
  // ✅ 编辑增强：拖拽/上移下移后实时高亮"编号已更新"提示
  const [reorderHint, setReorderHint] = useState<string | null>(null);
  const reorderHintTimer = useRef<number | null>(null);

  const {
    saveOutlineTree,
    doSaveOutlineTree,
    handleBudgetChange,
    handleExportTree,
    openLibraryPicker,
    handleApplyLibrary,
    handleSaveAsLibrary,
    submitSaveAsLibrary,
    clearAllOutline,
    handleUploadParseOutline,
    onOutlineTreeDrop,
    flashReorderHint,
  } = useOutlineActions({
    id,
    msg,
    modal,
    load,
    scheme,
    tree,
    treeRef,
    setTree,
    setSelectedSection,
    setExpandedKeys,
    setActiveTab,
    setReviewTick,
    setMultiSectionHint,
    outlineImportReorganize,
    lastUpload,
    setLastUpload,
    exportingRef,
    exportingTree,
    setExportingTree,
    setLibOpen,
    setLibLoading,
    setLibItems,
    setLibError,
    setLibApplying,
    setSaveLibOpen,
    saveLibForm,
    setReorderHint,
    reorderHintTimer,
    notifyClearedContent,
    notifyGenericResidual,
  });

  // R-P1 拆分：目录树编辑回调（新增/同级/删除/重命名/上下移）迁至
  // useOutlineTreeEdit；flashReorderHint 由 useOutlineActions 提供，经 deps 传入。
  const { addChildNode, addSiblingNode, deleteNode, renameNode, moveNode, findParentKey } =
    useOutlineTreeEdit({
      id,
      msg,
      modal,
      load,
      treeRef,
      expandedKeysRef,
      editTreeScrollRef,
      leftTreeScrollRef,
      setTree,
      setSelectedSection,
      setRenamingKey,
      setRenamingValue,
      setExpandedKeys,
      flashReorderHint,
    });

  // ✅ 2026-11-04 拆分（G6）：编辑树渲染派生（moveFlags / convertToEditTree /
  //    outlineEditTreeData）迁至 schemeWorkbench/useOutlineEditTreeRender.tsx，
  //    useMemo/useCallback 形态与依赖数组原样保留。
  const { moveFlags, outlineEditTreeData } = useOutlineEditTreeRender({
    tree,
    moveNode,
    renameNode,
    failedOutlineChapters,
    renamingKey,
    renamingValue,
    setRenamingKey,
    setRenamingValue,
  });

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
        if (action === "pause") {
          setTaskPaused(true);
          msg.success("已暂停");
        } else if (action === "resume") {
          setTaskPaused(false);
          setTaskStopping(false);
          msg.success("已恢复");
        } else {
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
  // ✅ R207：页面自持的「载入上次一致性审计结果」GET（useReviewAudit 五 ref
  //    收口范围之外），切方案/卸载必须一并中止。
  const consistencyLatestAbortRef = useRef<AbortController | null>(null);
  const [expertRunning, setExpertRunning] = useState(false);

  // R-P1 拆分：专家论证 / 合规检查 / 一致性审计（含 auditRunning、consistencyResult
  // 两个 useState）/ CR 修复工作台处理器迁至 useReviewAudit（调用点在原位置）。
  const {
    handleExpertReview,
    handleCancelExpertReview,
    handleComplianceCheck,
    handleCancelComplianceCheck,
    auditRunning,
    consistencyResult,
    setConsistencyResult,
    handleConsistencyAudit,
    handleCancelConsistencyAudit,
    loadCrConflicts,
    loadCrHistory,
    openConsistencyWorkbench,
    handleCrScan,
    handleCancelCrScan,
    handleCrRepair,
    handleCancelCrRepair,
    crRepairItemIds,
    handleCrConfirm,
    handleCrRollback,
    crConfirming,
  } = useReviewAudit({
    id,
    msg,
    modal,
    expertAbortRef,
    expertRunning,
    setExpertRunning,
    setExpertResult,
    checkAbortRef,
    setCheckingCompliance,
    checklistText,
    checklistDirty,
    aiRules,
    setComplianceResults,
    loadComplianceHistory,
    auditAbortRef,
    consistencySeverity,
    forceFullRepair,
    crScanning,
    setCrScanning,
    crRepairing,
    setCrRepairing,
    crScanId,
    setCrRepair,
    crRepair,
    setCrConflicts,
    setCrSummary,
    setCrScanId,
    setCrHistory,
    setCrOpen,
    setCrLoading,
    selectedSectionRef,
    setSelectedSection,
    load,
    setReviewTick,
  });

  const loadConsistencyLatest = async () => {
    if (!id) return;
    const sid = id;
    const controller = new AbortController();
    consistencyLatestAbortRef.current?.abort();
    consistencyLatestAbortRef.current = controller;
    try {
      const { data } = await complianceApi.consistencyLatest(id, controller.signal);
      // ✅ R207：审核域唯一完全无落地守卫的读请求 —— 切方案后旧方案的
      //    「载入上次结果」会把 score/issues 写进新方案页（R197 五 ref 收口
      //    只覆盖 hook 内 AI 长请求，页面自持的这个 GET 被遗漏）。
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      if (data?.exists) {
        setConsistencyResult({
          score: data.score,
          issues: data.issues || [],
          created_at: data.created_at,
          // ✅ R181（遗留问题 6）：沿用后端评分口径说明
          score_rule: data.score_rule,
          score_note: data.score_note,
          // ✅ R212（2026-11-10 · P1-3）：**补搬覆盖度字段**。后端 /latest 的
          //    ``counts_json`` 里本就带 sections / batches / checked_chars /
          //    total_chars（覆盖度），但此前只搬 score/issues ⇒ 「载入上次结果」
          //    后卡片**没有任何覆盖度提示**，用户把「只查了 3 批 / 6000 字」的
          //    分数当成全书结论（POST 路径存了 counts 却零渲染，同型）。
          //    现与 POST 路径**逐字同字段**，前端渲染一处即可。
          counts: data.counts,
          checked_chars: data.checked_chars ?? data.counts?.checked_chars,
          total_chars: data.total_chars ?? data.counts?.total_chars,
        });
      }
    } catch {
      // 历史结果加载失败不阻塞页面
    }
  };

  // ✅ 2026-11-04 拆分（G9）：导出主链（handleExport / handleCancelExport）迁至
  //    schemeWorkbench/useExportRun.ts（函数体逐字搬运，exportPctRef 随迁）。
  const { handleExport, handleCancelExport } = useExportRun({
    id,
    scheme,
    msg,
    exportForm,
    modal,
    exportingRef,
    exportSeqRef,
    exportAbortRef,
    exportBatcherRef,
    currentSchemeIdRef,
    setExporting,
    setExportStage,
    setExportPct,
    setExportPhase,
    loadCacheStatus,
    setNumberingCheck,
  });

  // ✅ 2026-11-04 拆分（G7）：导出预检 + 质量自检迁至 schemeWorkbench/useExportCheck.ts
  //    （函数体逐字搬运；导出主链 handleExport 仍在页面）。
  const { handleExportCheck, handleQualityCheck } = useExportCheck({
    id,
    msg,
    currentSchemeIdRef,
    exportCheckAbortRef,
    checkingExportRef,
    setCheckingExport,
    setExportIssues,
    setExportCheckIncomplete,
    setExportStats,
    setExportPreflight,
    setPlaceholderReport,
    setRerunPlan,
    checkingQuality,
    setCheckingQuality,
    setQualityResult,
  });

  // ✅ 2026-11-04 拆分（G6 续）：展示树派生与树交互回调迁至
  //    schemeWorkbench/useOutlineViewTree.tsx（useMemo/useCallback 形态原样保留）。
  const {
    treeData,
    handleTreeSelect,
    handleTreeExpand,
    handlePlaceholderJump,
    handleRerunAffected,
    handleToggleExpand,
    handleGoToOutline,
  } = useOutlineViewTree({
    id,
    msg,
    tree,
    failedOutlineChapters,
    expandedKeys,
    setExpandedKeys,
    setSelectedSection,
    setActiveTab,
    findParentKey,
    generating,
    rerunPlan,
    setRerunning,
    handleGenerateContent,
    handleExportCheck,
  });

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
    : progress >= 1
      ? "已完成"
      : progress >= 0.93
        ? "审核校对"
        : progress >= 0.3
          ? "生成二三级目录"
          : progress >= 0.1
            ? "生成一级目录"
            : "准备中";

  // ================== ✅ 2026-09-25 性能治理：稳定回调 + 派生数据收敛 ==================
  // 背景：本页面是 9000+ 行单体组件（132 个 useState），任一 state 变更都会重跑整个
  // 函数体并重建 tabItems（6 个 Tab 的全部子树 JSX）。下方两个子组件 BidAnalysisTab
  // 与 SectionContentCard 已经 memo 化（前者含 7 个 useMemo，后者把草稿 state 下沉到
  // 组件内），但父级传入的是内联箭头函数 —— Object.is 比较必然全败，memo 被彻底击穿，
  // 退化为裸函数组件（等于每帧全量重渲 922 行 + 275 行的子树）。这里把内联回调收敛
  // 为 useCallback（依赖全是稳定 setter / 既有稳定回调 / ref），让 memo 真正生效。
  const baOpenConfig = useCallback(() => setBaConfigOpen(true), []);
  const baRefreshResults = useCallback(() => {
    loadBaResults();
  }, [loadBaResults]);
  const baRefreshAll = useCallback(() => {
    loadDocuments();
    loadBaResults();
  }, [loadDocuments, loadBaResults]);
  const baDismissSectionCheck = useCallback(() => setSectionCheckResult(null), []);
  const baGoImport = useCallback(() => {
    setActiveTab("import");
    setImportSubTab("docs");
  }, []);
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
          : { ...n, children: n.children ? patchNode(n.children) : undefined },
      );
    setSelectedSection((prev) =>
      prev && prev.key === sec.key
        ? { ...prev, content: newContent, ...(hasWc ? { word_count: wordCount } : {}) }
        : prev,
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
    () =>
      baSummary ?? (baItems.length || baDefs.length ? recomputeBaSummary(baItems, baDefs) : null),
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
          badge={docList.length ? `${docStats.parsedCount}/${docList.length}` : ""}
          badgeColor={docList.length > 0 && docStats.allParsed ? "green" : "default"}
          hint="第一步：上传项目资料并解析为纯文本，随后在同页内完成 AI 结构化提取（供目录/正文引用）"
        />
      ),
      // 2026-09-23：import 模块左面板（25vw）随子 Tab 切换内容：
      //   docs → 解析分类列表（list-only 变体）
      //   extract → 解析分类（复用同一套 groups/items，与 BidAnalysisTab 同源）
      // 右侧「信息显示窗口」已移到 docs 子 Tab 的上传区下方（detail-only），
      // 与左侧分类栏通过 sharedParseState 联动，避免同一分类在两侧同时展示造成的视觉重复。
      children: (
        <ImportTab
          importSubTab={importSubTab}
          setImportSubTab={setImportSubTab}
          docList={docList}
          docStats={docStats}
          maxUploadBytes={maxUploadBytes}
          categoryOptions={categoryOptions}
          generating={generating}
          uploadingFacts={uploadingFacts}
          uploadedFiles={uploadedFiles}
          parsingDocs={parsingDocs}
          parsingDocId={parsingDocId}
          handleUploadDocuments={handleUploadDocuments}
          // ✅ R203（R191 遗留承接）：解析中「停止解析」——中断在途批量/单份解析
          onStopParse={() => {
            parseAbortRef.current?.abort();
          }}
          handleParseDocuments={handleParseDocuments}
          loadDocuments={loadDocuments}
          handleParseDocument={handleParseDocument}
          handlePreviewDocument={handlePreviewDocument}
          handleDeleteDocument={handleDeleteDocument}
          handleCategoryChange={handleCategoryChange}
          setActiveTab={setActiveTab}
          baSummary={baSummary}
          baSummaryDerived={baSummaryDerived}
          baDefs={baDefs}
          baGroups={baGroups}
          baItems={baItems}
          selectedBaItem={selectedBaItem}
          setSelectedBaItem={setSelectedBaItem}
          setBaFullItem={setBaFullItem}
          openBaEdit={openBaEdit}
          baLoading={baLoading}
          baError={baError}
          baDefsError={baDefsError}
          sharedParseState={sharedParseState}
          baRefreshResults={baRefreshResults}
          baActive={baActive}
          id={id}
          scheme={scheme}
          baShownProgress={baShownProgress}
          baShownMsg={baShownMsg}
          sectionChecking={sectionChecking}
          sectionCheckResult={sectionCheckResult}
          baTextStats={baTextStats}
          loadBaMeta={loadBaMeta}
          baOpenConfig={baOpenConfig}
          handleStopBa={handleStopBa}
          baStopping={baStopping}
          handleCheckSections={handleCheckSections}
          baRefreshAll={baRefreshAll}
          handleRerunItem={handleRerunItem}
          baDismissSectionCheck={baDismissSectionCheck}
          baGoImport={baGoImport}
          baGoOutline={baGoOutline}
          baGoFacts={baGoFacts}
        />
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
        <OutlineTab
          autoConfirmNotice={outlineAutoConfirmBanner}
          requirements={requirements}
          setRequirements={setRequirements}
          saveRequirements={saveRequirements}
          multiSectionHint={multiSectionHint}
          generating={generating}
          genType={genType}
          handleGenerateOutline={handleGenerateOutline}
          maxUploadBytes={maxUploadBytes}
          handleUploadParseOutline={handleUploadParseOutline}
          outlineImportReorganize={outlineImportReorganize}
          setOutlineImportReorganize={setOutlineImportReorganize}
          openLibraryPicker={openLibraryPicker}
          lastUpload={lastUpload}
          handleSaveAsLibrary={handleSaveAsLibrary}
          tree={tree}
          addChildNode={addChildNode}
          saveOutlineTree={saveOutlineTree}
          clearAllOutline={clearAllOutline}
          taskPaused={taskPaused}
          taskStopping={taskStopping}
          handleControl={handleControl}
          selectedSection={selectedSection}
          setSelectedSection={setSelectedSection}
          moveFlags={moveFlags}
          moveNode={moveNode}
          deleteNode={deleteNode}
          setActiveTab={setActiveTab}
          reorderHint={reorderHint}
          setReorderHint={setReorderHint}
          handleBudgetChange={handleBudgetChange}
          editTreeScrollRef={editTreeScrollRef}
          outlineEditTreeData={outlineEditTreeData}
          expandedKeys={expandedKeys}
          setExpandedKeys={setExpandedKeys}
          onOutlineTreeDrop={onOutlineTreeDrop}
          msg={msg}
          schemeId={id || ""}
          projectId={schemeProjectId || ""}
          onCreateSuggestedSection={handleCreateSuggestedSection}
          onOutlineRestored={load}
        />
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
          badgeColor={
            factsSummary?.has_warnings ? "orange" : factsSummary?.total ? "green" : "default"
          }
          hint={
            factsSummary?.has_warnings
              ? `待确认模拟值 ${factsSummary.simulated || 0} 项 · 待审核 ${factsSummary.unresolved || 0} 项 · 矛盾 ${factsSummary.conflicts || 0} 项${factsSummary.stale > 0 ? ` · 来源过期 ${factsSummary.stale} 项` : ""}`
              : "第三步：AI 从已解析资料提取全篇统一的事实变量，供正文生成保持一致"
          }
        />
      ),
      children: (
        <FactsTab
          id={id}
          facts={facts}
          factsSummary={factsSummary}
          factsCrossConflicts={factsCrossConflicts}
          factsSegmentStats={factsSegmentStats}
          factsFilter={factsFilter}
          setFactsFilter={setFactsFilter}
          factChapterTitles={factChapterTitles}
          factAttrTitles={factAttrTitles}
          factSourceKindTitles={factSourceKindTitles}
          factCategoryEntries={factCategoryEntries}
          activeFactCategory={activeFactCategory}
          activeFactGroups={activeFactGroups}
          factsPagination={factsPagination}
          docList={docList}
          docStats={docStats}
          uploadedFiles={uploadedFiles}
          factsDocsCollapsed={factsDocsCollapsed}
          setFactsDocsCollapsed={setFactsDocsCollapsed}
          generating={generating}
          genType={genType}
          progress={progress}
          progressMsg={progressMsg}
          missingValueMode={missingValueMode}
          setMissingValueMode={setMissingValueMode}
          factsLogs={factsLogs}
          uploadingFacts={uploadingFacts}
          parsingDocs={parsingDocs}
          parsingDocId={parsingDocId}
          taskPaused={taskPaused}
          taskStopping={taskStopping}
          factsChapterReport={factsChapterReport}
          factsDangerReport={factsDangerReport}
          factsDiagnosticsLoading={factsDiagnosticsLoading}
          setActiveTab={setActiveTab}
          msg={msg}
          modal={modal}
          handleAckStaleAll={handleAckStaleAll}
          handleBatchResolve={handleBatchResolve}
          handleUploadDocuments={handleUploadDocuments}
          handleParseDocuments={handleParseDocuments}
          handleParseDocument={handleParseDocument}
          handleDeleteDocument={handleDeleteDocument}
          handleGenerateFacts={handleGenerateFacts}
          handleRetryFailedSegments={handleRetryFailedSegments}
          openFactCreate={openFactCreate}
          handleClearAllFacts={handleClearAllFacts}
          openFactEdit={openFactEdit}
          handleDeleteFactGroup={handleDeleteFactGroup}
          openFactItemEdit={openFactItemEdit}
          handleResolveFactItem={handleResolveFactItem}
          handleResolveConflictItem={handleResolveConflictItem}
          handleAckStaleOne={handleAckStaleOne}
          handleControl={handleControl}
          loadFactsDiagnostics={loadFactsDiagnostics}
          loadFacts={loadFacts}
          loadDocuments={loadDocuments}
        />
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
            reconnecting={sseReconnecting}
          />
        );
      })(),
      children: (
        <ContentGenerationTab
          generating={generating}
          running={generating && genType === "content"}
          reconnecting={sseReconnecting}
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
        <ReviewTab
          id={id}
          projectId={schemeProjectId}
          load={load}
          reviewTick={reviewTick}
          setReviewTick={setReviewTick}
          checklistText={checklistText}
          setChecklistText={setChecklistText}
          setChecklistDirty={setChecklistDirty}
          aiRules={aiRules}
          checkingCompliance={checkingCompliance}
          complianceResults={complianceResults}
          setComplianceResults={setComplianceResults}
          complianceHistory={complianceHistory}
          historyLoading={historyLoading}
          loadComplianceHistory={loadComplianceHistory}
          handleComplianceCheck={handleComplianceCheck}
          handleCancelComplianceCheck={handleCancelComplianceCheck}
          consistencyResult={consistencyResult}
          auditRunning={auditRunning}
          loadConsistencyLatest={loadConsistencyLatest}
          handleConsistencyAudit={handleConsistencyAudit}
          handleCancelConsistencyAudit={handleCancelConsistencyAudit}
          expertResult={expertResult}
          expertItems={expertItems}
          expertRunning={expertRunning}
          handleExpertReview={handleExpertReview}
          handleCancelExpertReview={handleCancelExpertReview}
          exportIssues={exportIssues}
          setExportIssues={setExportIssues}
          exportStats={exportStats}
          setExportStats={setExportStats}
          exportPreflight={exportPreflight}
          setExportPreflight={setExportPreflight}
          placeholderReport={placeholderReport}
          setPlaceholderReport={setPlaceholderReport}
          rerunPlan={rerunPlan}
          setRerunPlan={setRerunPlan}
          checkingExport={checkingExport}
          exportCheckIncomplete={exportCheckIncomplete}
          setExportCheckIncomplete={setExportCheckIncomplete}
          handleExportCheck={handleExportCheck}
          handlePlaceholderJump={handlePlaceholderJump}
          rerunning={rerunning}
          handleRerunAffected={handleRerunAffected}
          qualityResult={qualityResult}
          setQualityResult={setQualityResult}
          checkingQuality={checkingQuality}
          handleQualityCheck={handleQualityCheck}
        />
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
        <ExportTab
          schemeId={id}
          exportForm={exportForm}
          exporting={exporting}
          exportPhase={exportPhase}
          exportPct={exportPct}
          exportStage={exportStage}
          exportGate={exportGate}
          numberingCheck={numberingCheck}
          handleExport={handleExport}
          handleCancelExport={handleCancelExport}
          exportPresets={exportPresets}
          selectedPresetId={selectedPresetId}
          setSelectedPresetId={setSelectedPresetId}
          presetOpLoading={presetOpLoading}
          handleRefreshPresets={handleRefreshPresets}
          handleSavePreset={handleSavePreset}
          handleSetDefaultPreset={handleSetDefaultPreset}
          handleDeletePreset={handleDeletePreset}
          tree={tree}
          generating={generating}
          exportingTree={exportingTree}
          handleExportTree={handleExportTree}
          cacheStatus={cacheStatus}
          cacheLoading={cacheLoading}
          loadCacheStatus={loadCacheStatus}
          setActiveTab={setActiveTab}
        />
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
            {scheme.type && (
              <Tag color="blue" style={{ marginLeft: 8 }}>
                {scheme.type}
              </Tag>
            )}
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

      {/* ✅ R174 F2：未知态横幅（「暂时隐藏」只收起横幅，守卫 ref 仍在）。 */}
      <UnknownTaskBanner
        unknownTask={unknownTask}
        generating={generating}
        typeLabel={UNKNOWN_TASK_TYPE_LABEL}
        onReattach={handleReattachUnknownTask}
        onDismiss={() => setUnknownTask(null)}
      />

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
          reconnecting={sseReconnecting}
          terminal={taskTerminal}
        />
      )}

      {/* 主体：左侧导航面板 + 右侧 Tab 内容（flex:1 占满剩余高度，随进度区动态收缩） */}
      {/* 2026-09-23：import（解析提取）模块隐藏目录树——此时还没生成目录，章节树无意义；
          全局事实（facts）模块同样隐藏目录树，改为左侧「事实分类」面板（点击分类
          在右侧项目资料下方显示该分类提取结果） */}
      <div className="swb-workbench-main" style={{ display: "flex", gap: 12, flex: 1, minHeight: 0 }}>
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

        {/* 2026-11-04：解析提取 Tab 的左侧分类 / 提取清单面板提升到页面级左栏，
            与目录树 / 事实分类面板同级，顶部与右侧步骤 Tab 栏齐平；右侧步骤 Tab
            （解析提取 / 目录生成 …）随之右移，与子 Tab「文档解析」左对齐。 */}
        {activeTab === "import" && (
          <ImportSidePanel
            importSubTab={importSubTab}
            baGroups={baGroups}
            baItems={baItems}
            baDefs={baDefs}
            baDefsError={baDefsError}
            baLoading={baLoading}
            baError={baError}
            selectedBaItem={selectedBaItem}
            sharedParseState={sharedParseState}
            baRefreshResults={baRefreshResults}
            onSelectItem={setSelectedBaItem}
            onEditItem={openBaEdit}
            onOpenFullView={setBaFullItem}
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
        <div
          style={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            background: "var(--bp-bg-container)",
            border: "1px solid var(--bp-border)",
            borderRadius: 8,
            padding: 16,
            overflow: "hidden",
          }}
        >
          <Tabs
            activeKey={activeTab}
            onChange={(key) => {
              // ✅ R179 N1：切换页面立即取消自动确认倒计时（无计时器时为空操作）
              cancelOutlineAutoConfirm("切换页面");
              setActiveTab(key);
            }}
            items={tabItems}
            className="swb-tabs-flex"
            style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
            tabBarStyle={{ flexShrink: 0, marginBottom: 4 }}
          />
        </div>
      </div>

      {/* ✅ R179 N1：自动确认倒计时 Modal（开关关闭时 open=false，不渲染任何内容） */}
      {outlineAutoConfirmModal}

      {/* ===== ✅ 全局事实新增 / 编辑 ===== */}
      <WorkbenchModals
        factEditingId={factEditingId}
        factModalOpen={factModalOpen}
        factSubmitting={factSubmitting}
        setFactModalOpen={setFactModalOpen}
        setFactEditingId={setFactEditingId}
        setFactEditingCategory={setFactEditingCategory}
        submitFact={submitFact}
        factForm={factForm}
        factCategoryOptions={factCategoryOptions}
        factItemModalOpen={factItemModalOpen}
        factItemSubmitting={factItemSubmitting}
        factItemEditing={factItemEditing}
        setFactItemModalOpen={setFactItemModalOpen}
        setFactItemEditing={setFactItemEditing}
        submitFactItem={submitFactItem}
        factItemForm={factItemForm}
        libOpen={libOpen}
        setLibOpen={setLibOpen}
        libKeyword={libKeyword}
        setLibKeyword={setLibKeyword}
        libLoading={libLoading}
        setLibLoading={setLibLoading}
        libItems={libItems}
        setLibItems={setLibItems}
        libError={libError}
        setLibError={setLibError}
        libApplying={libApplying}
        onApplyLibrary={handleApplyLibrary}
        previewDoc={previewDoc}
        onCancelPreview={handleCancelPreview}
        previewLoading={previewLoading}
        previewData={previewData}
        previewError={previewError}
        previewText={previewText}
        pipelineLoading={pipelineLoading}
        pipelineStatus={pipelineStatus}
        pipelineError={pipelineError}
        pipelineAction={pipelineAction}
        onPipelineAction={handlePipelineAction}
        baConfigOpen={baConfigOpen}
        setBaConfigOpen={setBaConfigOpen}
        onStartBaFromConfig={handleStartBaFromConfig}
        baModeOpt={baModeOpt}
        setBaModeOpt={setBaModeOpt}
        baDefs={baDefs}
        baGroups={baGroups}
        baSelectedIds={baSelectedIds}
        setBaSelectedIds={setBaSelectedIds}
        baForceRerun={baForceRerun}
        setBaForceRerun={setBaForceRerun}
        baFullItem={baFullItem}
        setBaFullItem={setBaFullItem}
        baEditItem={baEditItem}
        setBaEditItem={setBaEditItem}
        baEditSaving={baEditSaving}
        baEditValue={baEditValue}
        setBaEditValue={setBaEditValue}
        onBaSaveEdit={handleBaSaveEdit}
        onBaClearEdit={handleBaClearEdit}
      />

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
          <Form.Item
            name="name"
            label="目录库名称"
            rules={[{ required: true, message: "请输入名称" }]}
          >
            <Input placeholder="如：深基坑标准目录" />
          </Form.Item>
        </Form>
        {lastUpload && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            来源文件：{lastUpload.name} · 共 {countOutline(lastUpload.outline)} 个章节
          </Text>
        )}
      </Modal>

      {/* ===== ✅ 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR §6）===== */}
      {/* ===== ✅ 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR §6）===== */}
      <ComplianceRepairDrawer
        open={crOpen}
        onClose={() => setCrOpen(false)}
        schemeName={scheme?.name}
        loading={crLoading}
        summary={crSummary}
        scanId={crScanId}
        scanning={crScanning}
        repairing={crRepairing}
        conflicts={crConflicts}
        repair={crRepair}
        history={crHistory}
        consistencySeverity={consistencySeverity}
        forceFullRepair={forceFullRepair}
        confirming={crConfirming}
        onForceFullRepairChange={setForceFullRepair}
        onScan={handleCrScan}
        onCancelScan={handleCancelCrScan}
        onRepair={() => handleCrRepair()}
        onCancelRepair={handleCancelCrRepair}
        repairItemIds={crRepairItemIds}
        onConfirm={handleCrConfirm}
        onReload={() => {
          loadCrConflicts();
          loadCrHistory();
        }}
        onRollback={handleCrRollback}
      />
    </div>
  );
}
