import { useEffect, useState, useCallback, useRef, useMemo, memo } from "react";
import {
  App, Card, Button, Tree, Progress, Space, Tabs, Input, InputNumber,
  Radio, Checkbox,
  Empty, Tag, Modal, Form, Select, Typography, Descriptions,
  Collapse, Upload, Divider, Tooltip, List, Switch, Alert, Spin, Table,
  Row, Col, Drawer, Popover,
} from "antd";
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
} from "@ant-design/icons";
import { useParams } from "react-router-dom";
import {
sectionsApi, schemesApi, factsApi, exportApi, complianceApi,
sseFetch, sseGetStream, tasksApi, uploadOutlineApi, outlineLibraryApi,
chartsApi, consistencyRepairApi, bidAnalysisApi, docPipelineApi,
} from "../api";
import MarkdownRenderer from "../components/MarkdownRenderer";
import UploadParseTab from "../components/UploadParseTab";
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
} from "../utils/bidAnalysis";
import {
  buildUploadNotice, computeDocStats, isDocParsed, isDocFailed, NEXT_TAB, pickInitialTab,
  selectPlacedExportCharts, findDefaultExportPreset,
  type WorkflowTabKey,
} from "../utils/workflowDerived";
import { UPLOAD_FILE_ACCEPT } from "../utils/uploadAccept";
import ContentGenerationTab from "../components/ContentGenerationTab";
import {
  upsertSectionLog, finalizeRunningLogsIn, mergeFailedSectionsInto,
  contentResultFailedSections, contentResultSummary,
  type SectionLogItem, type GenStats,
} from "../utils/contentEvents";
// F-CONTENT-STANDARD(2026-09-26): 生成标准纯函数层（请求体映射 / 标签 / 报告判定）
import {
  buildStandardRequestFields, standardSummaryHint,
  sectionStandardOptionLabel, normalizeStandard,
  STANDARD_LABELS, issueLabel, reportHasIssues, reportIssueCount,
  type GenerationStandardChoice,
} from "../utils/contentStandard";
import { useSchemeLiveTask } from "../hooks/useSchemeLiveTask";
import { hookAntdMessage } from "../utils/activityCenter";
// ✅ 2026-09-25：SSE 批量器抽到 utils/sseBatcher.ts（TaskStatusBar 也要复用，
//  工具函数不再寄生在 9000+ 行的页面文件里）
import { createSseBatcher } from "../utils/sseBatcher";
import SectionContentCard, { countPlainTextWords } from "../components/SectionContentCard";
import ReadinessDashboard from "../components/review/ReadinessDashboard";
// ✅ 《待补充清单》面板（2026-09-24，人工补录兜底层）：占位符按字段/按章节聚合
import PlaceholderReportPanel from "../components/PlaceholderReportPanel";
import ReviewWorkflowPanel from "../components/review/ReviewWorkflowPanel";
import type { AuditRule, Severity } from "../types/audit";
import { SEVERITY_COLOR, SEVERITY_LABEL } from "../types/audit";
import {
  deriveExportGate,
  renderChartsForExport,
  type ExportChartItem,
  type ExportChartImage,
} from "../utils/exportCharts";
const { Title, Text } = Typography;

// ============================================================
// ✅ P0-6 计时器隔离：实时耗时只让本行组件每秒自更新（React.memo），
//    消除生成期间整页 1Hz 重渲（旧实现：父组件 setNowTick 驱动全页重渲）
// ============================================================
const ElapsedTimer = memo(function ElapsedTimer({ startTime }: { startTime: number }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((v) => v + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <Text type="secondary" style={{ fontSize: 11, marginLeft: 4, flexShrink: 0 }}>
      {Math.max(0, Math.floor((Date.now() - startTime) / 1000))}s
    </Text>
  );
});

// ============================================================
// 目录标题编号（对齐后端 heading_standard.py 的编号原则）
//   L1: 第X章   L2: N   L3: N.M   L4: N.M.K   L5: （X）
// 编号直接由节点 id（点分路径，如 "1" / "1.1" / "1.1.1"）推导，
// 与后端 renumber_outline / HeadingNumberingGeneratorV2 保持一致，
// 保证目录树与正文/导出编号始终同步。展示时套用、存储仍保留裸标题。
// ============================================================
/** 目录系统硬性上限：三级（与后端 outline_utils.MAX_OUTLINE_DEPTH 对齐） */
const MAX_OUTLINE_DEPTH = 3;

const CN_NUMBERS: string[] = [
  "", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
  "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
  "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十",
];

// ✅ 与后端 strip_outline_numbering 同源：剥离标题开头已嵌入的编号前缀，
// 修复存量数据 "第一章 第一章 工程概况" / "2.1 2.1 相关法律法规" 双重编号
const LEADING_NUMBER_RE =
  /^\s*(?:第[一二三四五六七八九十百千零0-9]+[章节]|[（(][一二三四五六七八九十0-9]+[)）]|[0-9]+[)）]|[0-9]+(?:\.[0-9]+){0,7}[、.．，,：: \-—]+|[一二三四五六七八九十百千零]+[、.．，,：: \-—]+)[、.．，,：: \-—]*/;

// 整条标题本身就是一个编号（"1" / "1.1" / "1.2.3"）时必须原样返回，
// 不能走剥离流程 —— 否则 "." 同时属于分隔符字符类，会只剥半截
//（"1.1" → "1"、"1.2.3" → "3"），与后端 numbering.strip_outline_numbering
// 的 _PURE_NUMBER_TITLE_RE 短路契约不一致：同一节点前后端展示会不同，
// 叠加位置编号后即产生编号漂移 / 双重编号。
const PURE_NUMBER_TITLE_RE = /^[0-9]+(?:[.．][0-9]+)*$/;

export function stripOutlineNumbering(title: string): string {
  if (!title) return title;
  const trimmed = title.trim();
  if (PURE_NUMBER_TITLE_RE.test(trimmed)) return trimmed;
  const stripped = title.replace(LEADING_NUMBER_RE, "").trim();
  return stripped || trimmed;
}

export function formatOutlineTitle(
  id: string | undefined,
  level: number,
  title: string,
): string {
  if (!title || !title.trim()) return title || "";
  const bareTitle = stripOutlineNumbering(title);
  const parts = String(id || "")
    .split(".")
    .map((p) => parseInt(p, 10))
    .filter((n) => !Number.isNaN(n));
  if (!parts.length) return bareTitle;

  let number = "";
  let sep = " ";
  if (level === 1) {
    number = `第${CN_NUMBERS[parts[0]] || parts[0]}章`;
  } else if (level === 2) {
    // L2: "1"（tail 不含章号，直接第一个 part）
    number = String(parts[parts.length - 1]);
  } else if (level === 3) {
    // L3: "1.1"（tail 最后两个 part）
    number = parts.slice(-2).join(".");
  } else if (level === 4) {
    // L4: "1.1.1"（tail 最后三个 part）
    number = parts.slice(-3).join(".");
  } else if (level === 5) {
    // L5: "1.1.1.1"（十进制延续，tail 最后四个）+ 、分隔
    number = parts.slice(-4).join(".");
    sep = "、";
  } else if (level === 6) {
    // L6: "1）"（括号数字，只用最后一个 part）+ 、分隔
    number = `${parts[parts.length - 1]}）`;
    sep = "、";
  } else if (level === 7) {
    // L7: "a" / "b"（小写字母，只用最后一个 part）+ 、分隔
    const n = parts[parts.length - 1] || 1;
    const alphaLetters = "abcdefghijklmnopqrstuvwxyz";
    const idx = Math.max(0, Math.min(n - 1, alphaLetters.length - 1));
    number = alphaLetters[idx];
    sep = "、";
  } else {
    return bareTitle;
  }
  return `${number}${sep}${bareTitle}`;
}

export type TreeNode = {
  key: string;
  title: string;
  level: number;
  status: string;
  word_count: number;
  word_budget: number;
  word_status?: string;
  content?: string;
  description?: string;
  /** 后端权威编号（点分路径，如 "1" / "1.1" / "1.1.1"） */
  outlineId?: string;
  /** F-CONTENT-STANDARD(2026-09-26)：章节级生成标准覆盖（""=沿用方案级） */
  generation_standard?: string;
  /** F-CONTENT-STANDARD(2026-09-26)：最近一次生成实际使用的标准（只读，供复查） */
  last_generation_standard?: string;
  children?: TreeNode[];
};

// SectionLogItem / GenStats 类型已抽离至 utils/contentEvents.ts（与纯函数同源，
// 供组件级测试独立引用），页面统一从该处导入，避免类型定义双份漂移。

/** 阶段徽标配色（与后端 _STAGE_MODEL 的阶段标识对应） */
const STAGE_COLOR: Record<string, string> = {
  context: "default",
  draft: "processing",
  continue: "warning",
  persist: "cyan",
};

/** ✅ 进度增强：毫秒 → 人类可读时长（与后端 _fmt_duration 同口径） */
function fmtDuration(ms?: number | null): string {
  if (!ms || ms <= 0) return "0秒";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s ? `${m}分${s}秒` : `${m}分钟`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}小时${m % 60}分` : `${h}小时`;
}

/**
 * ✅ 进度增强：从 SSE 事件载荷中提取运行统计。
 * 三种来源共用同一口径：stats 事件、心跳 ping 事件、progress 事件内嵌的 stats。
 */
function pickStats(src: any): GenStats {
  return {
    elapsed_ms: src?.elapsed_ms,
    eta_ms: src?.eta_ms,
    done: src?.done,
    total: src?.total,
    failed: src?.failed,
    words: src?.words,
    avg_section_ms: src?.avg_section_ms,
    concurrency: src?.concurrency,
    phase: src?.phase,
    phase_label: src?.phase_label,
    progress: src?.progress,
    stepwise: src?.stepwise,
    nodes: src?.nodes,
    running: src?.running,
  };
}

/**
 * ✅ 性能优化：判断两个 GenStats 载荷是否语义相等（浅比较所有字段）。
 * 背景：后端 stats/ping 事件每 100–1000ms 推送一次，若载荷与当前状态相同
 *      仍调用 setGenStats 会触发整页（含 GenerationProgressCard、树、Tab）
 *      一次无意义重渲。此函数配合 setState(prev => equalStats ? prev : next)
 *      跳过值不变时的 setState，消除高频 ping 带来的抖动。
 */
export function statsEqual(a: GenStats | undefined, b: GenStats): boolean {
  if (!a) return false;
  return (
    a.elapsed_ms === b.elapsed_ms &&
    a.eta_ms === b.eta_ms &&
    a.done === b.done &&
    a.total === b.total &&
    a.failed === b.failed &&
    a.words === b.words &&
    a.avg_section_ms === b.avg_section_ms &&
    a.concurrency === b.concurrency &&
    a.phase === b.phase &&
    a.phase_label === b.phase_label &&
    a.progress === b.progress &&
    a.stepwise === b.stepwise &&
    a.nodes === b.nodes &&
    a.running === b.running
  );
}

// ✅ P0-5 组件拆分：目录树辅助纯函数（原组件内定义，无闭包依赖，移模块级供 SchemeTreePanel 复用）
export function findFirstContentDescendant(node: TreeNode): TreeNode | null {
  // ✅ BUG修复：同时检查 word_count>0，避免有字数但content为空的数据不一致节点被跳过
  if (node.content || (node.word_count || 0) > 0) return node;
  for (const child of node.children || []) {
    const found = findFirstContentDescendant(child);
    if (found) return found;
  }
  return null;
}

export function collectAllKeys(nodes: TreeNode[]): string[] {
  const keys: string[] = [];
  function walk(n: TreeNode) {
    keys.push(n.key);
    n.children?.forEach(walk);
  }
  nodes.forEach(walk);
  return keys;
}

/**
 * ✅ 性能优化：目录树的轻量指纹，用于「内容未变则跳过 setState」的相等性守卫。
 * 背景：正文生成期间每 3s 轮询一次轻量接口刷新目录树，即使数据完全没变也会产生
 * 全新树对象 → setTree 拿到新引用 → treeData / outlineEditTreeData /
 * countGeneratedLeaves / collectAllKeys 全部重算 + 整页重渲（每 3s 一次无效渲染）。
 * 对齐既有 statsEqual 的守卫思路：指纹相同就返回旧引用，React 直接跳过重渲。
 *
 * 只取影响渲染与用户可感知的字段（遍历顺序 / 字数 / 状态），刻意不含 content
 * 大字段（轻量轮询本就不返回它）；用短字符串拼接而非 JSON.stringify，
 * 避免每 3s 序列化整棵树。
 */
export function treeFingerprint(nodes: TreeNode[]): string {
  let out = "";
  function walk(ns: TreeNode[]) {
    for (const n of ns) {
      out += `${n.key}:${n.word_count}:${n.status};`;
      if (n.children?.length) walk(n.children);
    }
  }
  walk(nodes || []);
  return out;
}

/**
 * ✅ 编辑增强：把指定 key 的树节点滚动到可视区域中央（平滑滚动）。
 * 兼容"落库后 load 重建 DOM"的异步场景——若首次查询未命中节点元素，
 * 通过 requestAnimationFrame 重试若干次，直到节点出现在树中再滚动。
 * 仅做视图定位，不改动任何状态。
 */
function scrollToNodeInContainer(container: HTMLElement | null, key: React.Key, tries = 10) {
  if (!container || tries <= 0) return;
  const sel = `[data-key="${CSS.escape(String(key))}"]`;
  const el = (container.querySelector(`.ant-tree-node-content-wrapper${sel}`) ||
    container.querySelector(sel)) as HTMLElement | null;
  if (el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  requestAnimationFrame(() => scrollToNodeInContainer(container, key, tries - 1));
}

/** 目录树中是否已有任意章节正文（用于"重新生成目录"的覆盖风险确认） */
export function hasAnyContent(nodes: TreeNode[]): boolean {
  for (const n of nodes) {
    if (n.content || (n.word_count || 0) > 0) return true;
    if (n.children?.length && hasAnyContent(n.children)) return true;
  }
  return false;
}

/** 目录树中已有正文的叶子章节数（用于上传替换目录时提示影响范围） */
export function treeNodesWithContent(nodes: TreeNode[]): number {
  let count = 0;
  const walk = (ns: TreeNode[]) => {
    for (const n of ns) {
      if (n.content || (n.word_count || 0) > 0) count++;
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes || []);
  return count;
}

/**
 * ✅ 计算目录树的最大层级（迭代实现 + 节点数保护）。
 * 用于拖拽/新增时判断是否会产出超过三级上限的节点——旧实现对此无校验，
 * 深层节点只能在点「保存目录」时被后端静默裁剪，用户看到节点"消失"。
 */
export function outlineTreeDepth(nodes: TreeNode[]): number {
  let max = 0;
  let guard = 0;
  const stack: { node: TreeNode; level: number }[] = (nodes || []).map((n) => ({ node: n, level: 1 }));
  while (stack.length && guard++ < 50000) {
    const { node, level } = stack.pop()!;
    if (level > max) max = level;
    for (const c of node.children || []) stack.push({ node: c, level: level + 1 });
  }
  return max;
}

/** 就地更新树中指定节点的字段（压缩完成后刷新字数/正文用） */
export function updateNodeFields(
  nodes: TreeNode[],
  key: string,
  fields: Partial<TreeNode>
): TreeNode[] {
  return nodes.map((n) => {
    if (n.key === key) return { ...n, ...fields };
    if (n.children?.length) {
      return { ...n, children: updateNodeFields(n.children, key, fields) };
    }
    return n;
  });
}

/** 统计目录树节点总数（含所有层级；纯函数，供导入识别与导出复用） */
export function countOutline(outline: any[], depth = 0): number {
  if (depth > 50 || !outline?.length) return 0;
  return outline.reduce(
    (acc, n) => acc + 1 + countOutline(n?.children || [], depth + 1), 0);
}

/** 把后端「整理为标准结构」报告折算成人话（匹配/补全/补充章节统计） */
export function describeReorganizeReport(report: any): string {
  if (!report || typeof report !== "object") return "";
  const matched = Number(report.matched_chapters) || 0;
  const standard = Number(report.standard_chapters) || 0;
  const skeleton = Number(report.kept_standard_skeleton) || 0;
  const extras = Number(report.appended_extras) || 0;
  const tpl = report.template ? `（模板 ${report.template}）` : "";
  const bits = [`匹配 ${matched}/${standard} 章`];
  if (skeleton > 0) bits.push(`补全空骨架 ${skeleton} 章`);
  if (extras > 0) bits.push(`${extras} 章归入「补充章节」`);
  return `已按标准章节骨架整理${tpl}：${bits.join("，")}`;
}

export type UploadOutlineSummary = {
  /** 是否得到可应用的目录（outline 非空）——空结果时绝不替换用户当前目录树 */
  ok: boolean;
  level: "success" | "warning";
  /** 主提示文案（成功=识别完成统计，失败=后端给出的原因） */
  message: string;
  /** 附加告警（内容不完整/截断/归位统计），逐条如实告知用户 */
  notices: string[];
  outline: any[];
  nodeCount: number;
  reorganized: boolean;
};

/**
 * 「导入目录（智能识别）」结果诊断（纯函数，可单测）。
 *
 * ✅ 补齐数据传递链路（2026-09-20）：后端 parse_outline 早已回传
 *    empty_text / warning / parse_warnings / parse_truncated /
 *    reorganized / reorganize_report，但旧前端只读 outline / file_name，
 *    导致：① 扫描件无 OCR 返回空目录时仍误报「识别完成：共 0 个章节」
 *    并清空用户当前目录树；② 「整理为标准结构」归位统计对用户不可见。
 *    这里把「响应 → 用户可读提示」的折算收口成纯函数，供交互链路复用。
 */
export function summarizeUploadOutlineResult(data: any): UploadOutlineSummary {
  const d = data || {};
  const outline: any[] = Array.isArray(d.outline) ? d.outline : [];
  const nodeCount = countOutline(outline);
  const reorganized = Boolean(d.reorganized);
  const notices: string[] = [];

  if (Array.isArray(d.parse_warnings)) {
    for (const w of d.parse_warnings) {
      if (typeof w === "string" && w.trim()) notices.push(w.trim());
      else if (w && typeof w === "object" && w.message) notices.push(String(w.message));
    }
  }
  if (d.reorganize_report) {
    const desc = describeReorganizeReport(d.reorganize_report);
    if (desc) notices.push(desc);
  }

  // 空结果：以「不替换目录树」为前提，如实回传后端给出的失败原因
  if (nodeCount === 0) {
    const warn = typeof d.warning === "string" && d.warning.trim()
      ? d.warning.trim()
      : "未识别到目录结构，请检查文件是否为扫描件（需 OCR）或格式是否受支持";
    return { ok: false, level: "warning", message: warn, notices, outline: [], nodeCount: 0, reorganized };
  }

  // 非空但原文被截断（raw_text_truncated / parse_truncated）：仍可用，补充告警
  if (typeof d.warning === "string" && d.warning.trim()) notices.unshift(d.warning.trim());
  const name = d.file_name || "文件";
  const suffix = reorganized ? "，已按标准章节骨架整理" : "";
  return {
    ok: true,
    level: "success",
    message: `识别完成：${name}，共 ${nodeCount} 个章节，可在下方调整后保存${suffix}`,
    notices,
    outline,
    nodeCount,
    reorganized,
  };
}

// ============================================================
// 全局事实 Tab · 模块级可测单元（2026-09-21 拆出，行为由 factsTab.test 钉住）
// ============================================================
/** 筛选命中判定：当前 filter 在 groups 里是否存在可展示条目（悬空回退唯一事实源） */
export function hasFactsFilterMatch(groups: any[], filter: string): boolean {
  if (filter === "all") return true;
  return (groups || []).some((g: any) =>
    (g?.items || []).some((it: any) =>
      filter === "simulated" ? it.is_simulated
        : filter === "conflict" ? it.has_conflict
          : !it.is_resolved));
}

/** 批量确认文案：修复旧文案把「模拟值 ∩ 未确认」交集重复计数（unresolved+simulated） */
/** 异步响应是否仍属于当前方案。AbortSignal + 路由 ID 双重守卫，防跨方案污染。 */
export function isCurrentSchemeRequest(
  requestedSchemeId: string,
  currentSchemeId: string,
  signal?: AbortSignal,
): boolean {
  return !!requestedSchemeId && requestedSchemeId === currentSchemeId && !signal?.aborted;
}

/** 批量确认结果文案：安全跳过时不得误报“事实注入已就绪”。 */
export function buildBatchResolveResultCopy(result: any | null | undefined): {
  tone: "success" | "warning";
  text: string;
} {
  const changed = Number(result?.changed) || 0;
  const skipped = Number(result?.skipped) || 0;
  const safety = Number(result?.skipped_safety_count) || 0;
  if (safety > 0) {
    return {
      tone: "warning",
      text: `已确认 ${changed} 项；${safety} 项模拟值或安全关键事实未放行，请逐条核对裁决`,
    };
  }
  return {
    tone: "success",
    text: `已确认 ${changed} 项${skipped ? `，${skipped} 项原本已确认` : ""}，可注入事实已就绪`,
  };
}

/** 九大章节完整性与危大诊断只读面板（组件级测试锁定空态/覆盖率/缺参）。 */
export function FactsDiagnosticsPanel({ loading, chapterReport, dangerReport, onRefresh }: {
  loading: boolean;
  chapterReport: any | null;
  dangerReport: any | null;
  onRefresh: () => void;
}) {
  const chapters = chapterReport?.chapters || [];
  const classification = dangerReport?.classification || {};
  const missing = dangerReport?.missing_params || [];
  return (
    <Card
      size="small"
      title="九大章节完整性 · 危大诊断"
      extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>刷新诊断</Button>}
      style={{ marginBottom: 12 }}
    >
      {!chapterReport && !dangerReport ? (
        <Text type="secondary">暂无诊断数据</Text>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space wrap>
            <Tag color={classification.is_hazardous ? "red" : "default"}>
              {classification.is_hazardous ? "危大工程" : "未识别为危大工程"}
            </Tag>
            <Tag color={classification.is_oversize ? "volcano" : "default"}>
              {classification.is_oversize ? "超过一定规模" : "未超过一定规模"}
            </Tag>
            {missing.map((p: string) => <Tag key={p} color="orange">缺参：{p}</Tag>)}
          </Space>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 6 }}>
            {chapters.map((ch: any) => (
              <Tooltip key={ch.key} title={`缺失字段：${(ch.missing_fields || []).join("、") || "无"}`}>
                <div>
                  <Text style={{ fontSize: 12 }}>{ch.order}. {ch.title}</Text>
                  <Progress percent={Math.round((ch.coverage || 0) * 100)} size="small" />
                </div>
              </Tooltip>
            ))}
          </div>
        </Space>
      )}
    </Card>
  );
}

export function buildBatchResolveCopy(stats: {
  unresolved?: number; simulated?: number; conflicts?: number;
} | null | undefined): {
  headline: string; confirmType: "warning" | "confirm";
  simulatedNote: string; conflictNote: string;
} {
  const unresolved = Number(stats?.unresolved) || 0;
  const simulated = Number(stats?.simulated) || 0;
  const conflicts = Number(stats?.conflicts) || 0;
  return {
    headline: `当前方案共有 ${unresolved} 项事实未确认，是否全部确认为“已核实”？`,
    confirmType: simulated > 0 ? "warning" : "confirm",
    simulatedNote: simulated > 0
      ? `⚠️ 其中含模拟值 ${simulated} 项（AI 推断的占位数据），确认后会被直接注入正文生成，请先核对真实值。`
      : "",
    conflictNote: conflicts > 0
      ? `另有 ${conflicts} 项存在矛盾（多源取值不一致），批量确认不处理矛盾，建议逐条裁决。`
      : "",
  };
}

/** 事实提取进度卡：进度 + 分段级实时日志（最新在上） */
export function FactsExtractProgressCard({ progress, progressMsg, logs }: {
  progress: number;
  progressMsg: string;
  logs: Array<{ progress: number; message: string; time?: number; [k: string]: unknown }>;
}) {
  const list = (logs || []).slice().reverse();
  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Space direction="vertical" style={{ width: "100%" }} size={6}>
        <Text strong>📚 正在提取全局事实</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>{progressMsg}</Text>
        <Progress percent={Math.round((progress || 0) * 100)} size="small" />
        <div style={{ maxHeight: 180, overflow: "auto" }}>
          {list.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>正在连接 AI，准备提取...</Text>
          ) : (
            list.map((l, i) => (
              <div key={i} style={{ fontSize: 12, lineHeight: "20px" }}>
                <Text type="secondary">{Math.round((l.progress || 0) * 100)}%</Text>{" "}
                <span>{l.message}</span>
              </div>
            ))
          )}
        </div>
        {list.length > 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>共 {list.length} 条进度记录</Text>
        )}
      </Space>
    </Card>
  );
}

/** 分段失败告警：部分失败 warning / 全部失败 error，明细最多 6 条 */
export function FactsSegmentFailuresAlert({ stats }: {
  stats: { ok?: number; failed?: number; total?: number; failed_details?: Array<{ index?: number; heading?: string; reason?: string }> } | null;
}) {
  const s = stats || {};
  const failed = Number(s.failed) || 0;
  const total = Number(s.total) || 0;
  const allFailed = failed > 0 && failed === total;
  const details = Array.isArray(s.failed_details) ? s.failed_details : [];
  return (
    <Alert
      type={allFailed ? "error" : "warning"}
      showIcon
      style={{ marginBottom: 12 }}
      message={allFailed
        ? `本次提取全部 ${total} 段失败，未提取到事实`
        : `本次提取有 ${failed}/${total} 段失败，结果可能不完整`}
      description={(
        <div style={{ fontSize: 12 }}>
          {details.slice(0, 6).map((d, i) => (
            <div key={i}>· 第 {d.index ?? i + 1} 段（{d.heading || "未命名"}）：{d.reason || "未知原因"}</div>
          ))}
          {details.length > 6 && <div>· ……其余 {details.length - 6} 条明细略</div>}
          {details.length === 0 && (
            <div>未获取到失败明细，建议稍后点击「③ AI 提取事实」重新提取。</div>
          )}
          <div>常见原因：模型超时/限流、网络抖动、单段内容过长。</div>
        </div>
      )}
    />
  );
}

// ============================================================
// 全局事实 · 模块级可测纯函数（2026-09-21 拆出，行为由 factsTab.test 钉住）
// ============================================================
/**
 * 把后端 list 接口返回的 stats 收敛为前端事实 Tab 使用的 summary 对象。
 *
 * has_warnings = 模拟值 / 未确认 / 矛盾 任一 > 0，驱动「全部就绪」入口与徽标颜色。
 * ✅ 抽纯函数（2026-09-21）：loadFacts 此前内联算 has_warnings，与切方案重置
 *    setFactsSummary(null) 分散两处，口径漂移难以测试。
 */
export function buildFactsSummary(stats: any): any {
  if (!stats || typeof stats !== "object") return null;
  const simulated = Number(stats.simulated) || 0;
  const unresolved = Number(stats.unresolved) || 0;
  const conflicts = Number(stats.conflicts) || 0;
  return {
    ...stats,
    simulated,
    unresolved,
    conflicts,
    total: Number(stats.total) || 0,
    has_warnings: simulated > 0 || unresolved > 0 || conflicts > 0,
  };
}

/**
 * SSE completed 事件的统一收口：segment_stats / cross_conflicts 缺字段即清空。
 *
 * ✅ BUG 修复（2026-09-21）：增量提取「全部跳过（all_skipped）」的 completed
 * 事件不带 segment_stats，旧实现 `if (evt.segment_stats) setFactsSegmentStats(...)`
 * 只在有字段时才写 → 上一次提取的失败告警会残留，误导用户以为本次也失败；
 * 跨段矛盾横幅同理。现缺字段一律清空，避免跨次 / 跨方案残留。
 */
export function applyFactsCompletedEvent(evt: any): {
  segmentStats: any | null;
  crossConflicts: any[];
} {
  const ss = evt?.segment_stats;
  return {
    segmentStats: ss && typeof ss === "object" ? ss : null,
    crossConflicts: Array.isArray(evt?.cross_conflicts) ? evt.cross_conflicts : [],
  };
}

// ============================================================
// 目录树纯操作函数（模块级导出，行为由 outlineTab.test 钉住）
// ============================================================
/** 扁平化工具：按当前树顺序收集所有节点 key（用于 /reorder 全量重排） */
export function flattenTreeKeys(nodes: TreeNode[]): string[] {
  const keys: string[] = [];
  const walk = (ns: TreeNode[]) => {
    ns.forEach((n) => {
      keys.push(n.key);
      if (n.children?.length) walk(n.children);
    });
  };
  walk(nodes);
  return keys;
}

/**
 * ✅ 目录编辑增强：同级上移/下移。
 * 在当前层（含递归子层）定位 key 所在兄弟数组，与相邻 sibling 交换位置。
 * - dir=-1 上移；dir=1 下移。
 * - 已在同层首/尾（无相邻 sibling）时返回 null（调用方据此禁用按钮，不触发移动）。
 * 仅交换兄弟顺序、不改层级与父子关系，符合"上下移动章节"语义。
 */
export function moveSiblingInTree(
  nodes: TreeNode[],
  key: string,
  dir: -1 | 1,
): TreeNode[] | null {
  const idx = nodes.findIndex((n) => n.key === key);
  if (idx !== -1) {
    if (idx + dir < 0 || idx + dir >= nodes.length) return null;
    const copy = [...nodes];
    [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]];
    return copy;
  }
  for (const n of nodes) {
    if (n.children && n.children.length) {
      const res = moveSiblingInTree(n.children, key, dir);
      if (res) {
        return nodes.map((x) => (x.key === n.key ? { ...x, children: res } : x));
      }
    }
  }
  return null;
}

/** 检测目录树中是否存在超过 3 级的深层节点（后端落库时会被裁剪合并） */
export function hasDeepOutlineNodes(nodes: TreeNode[], depth = 1): boolean {
  if (depth > MAX_OUTLINE_DEPTH) return true;
  return nodes.some((n) => (n.children?.length || 0) > 0 && hasDeepOutlineNodes(n.children!, depth + 1));
}

/**
 * ✅ 单一口径：未落库「本地临时节点」的 key 前缀全集。
 *
 * 目录树里存在两类尚未写入 sections 表的临时 key：
 *   · `local_`  —— 目录树中手工「新增一级章节 / 新增子章节 / 新增同级」产生的节点；
 *   · `upload_` —— 「导入目录（智能识别）」用识别结果**替换本地树**时构造的节点
 *                   （outlineToTreeNode，见下方模块级实现）。
 *
 * ✅ BUG 根因修复（2026-09-21）：旧实现只认 `local_` 一种前缀，于是
 * 「导入目录」替换后的整棵临时树被各处误判为「已落库章节」，四处口径同时漂移：
 *   1. hasUnsavedLocalNodes 漏检 → 顶部橙色 Tag 与左侧 Alert 都不显示，
 *      用户不知道整棵树都还没保存；
 *   2. deleteNode 误调 `DELETE /sections/{upload_...}` → 后端 404，
 *      异常中断在 `setTree(removeNode(...))` 之前 → **本地节点根本删不掉**；
 *   3. renameNode 误调 `PATCH /sections/{upload_...}` → 404，
 *      被 `.catch(() => {})` 吞掉 → 本地改名看似成功、服务端无任何记录；
 *   4. moveNode 因 `hasUnsavedLocal=false` 走「立即 /reorder」分支，
 *      `executemany UPDATE ... WHERE id=?` 对不存在的 upload_ id 静默 0 行，
 *      却弹出「编号已实时更新（已同步到服务端）」——**假成功**，刷新后回到旧顺序。
 * 统一收口到 isUnsavedLocalKey，四处调用点共用同一判定，禁止再各写一份前缀判断。
 */
export const UNSAVED_KEY_PREFIXES = ["local_", "upload_"] as const;

/** 判断某节点 key 是否属于「尚未落库的本地临时节点」（见 UNSAVED_KEY_PREFIXES 说明） */
export function isUnsavedLocalKey(key: string): boolean {
  const k = String(key || "");
  return UNSAVED_KEY_PREFIXES.some((p) => k.startsWith(p));
}

/**
 * ✅ B11 修复：全树递归检测未保存的本地临时节点（见 UNSAVED_KEY_PREFIXES）。
 * 旧实现只查根层（tree.some），嵌套在二/三级下的新增节点会被漏检 ——
 * moveNode/拖拽随即使 /reorder 立即持久化，未落库的新增章节被丢失。
 * 现同时覆盖 local_（手工新增）与 upload_（导入识别替换）两类临时 key。
 */
export function hasUnsavedLocalNodes(nodes: TreeNode[]): boolean {
  for (const n of nodes || []) {
    if (isUnsavedLocalKey(n.key)) return true;
    if (n.children?.length && hasUnsavedLocalNodes(n.children)) return true;
  }
  return false;
}

/**
 * 目录树 → 提交给 /save-outline 的 outline 结构（纯函数，可单测）。
 *
 * 关键约定：`id` 直接取节点 key —— 已落库章节是 DB 主键（UUID），
 * 临时节点是 local_/upload_ 前缀 key。后端 `_save_outline_to_db` 先按
 * `__original_id` 判定 is_new，再统一 renumber，故此处不做编号改写。
 */
export function treeToOutline(nodes: TreeNode[]): any[] {
  return nodes.map((n, i) => ({
    id: n.key,
    title: n.title,
    level: n.level,
    sort_order: i,
    word_budget: n.word_budget,
    description: n.description || "",
    children: n.children && n.children.length ? treeToOutline(n.children) : [],
  }));
}

/**
 * 按位置重排 outline 的展示编号（纯函数，可单测）。
 *
 * 只写前端展示字段 `outlineId`（点分路径）与 `sort_order`，**不改 `id`**：
 * `id` 是「已落库章节的 DB 主键 / 临时节点 key」的载体，被后端用于
 * 智能保留正文；编号由后端 `renumber_outline` 落库时统一生成。
 */
export function renumberOutline(outline: any[], parentKey = ""): any[] {
  return outline.map((node, idx) => {
    const newKey = parentKey ? `${parentKey}.${idx + 1}` : String(idx + 1);
    return {
      ...node,
      sort_order: idx,
      outlineId: newKey,
      children: node.children ? renumberOutline(node.children, newKey) : [],
    };
  });
}

/**
 * 「导入目录（智能识别）」结果 → 本地临时树（纯函数，可单测）。
 *
 * ✅ 编号由「位置」推导（与后端 renumber_outline 同口径），而不是直接用
 * n.id —— 识别结果里的 id 常为 "n1"/任意值，直接当编号用会让目录树完全不
 * 显示编号（保存前后观感不一致）。
 *
 * key 用 `upload_` 前缀标记「尚未落库」：保存目录后由后端换成 DB 主键。
 * 该前缀必须被 isUnsavedLocalKey 识别（见 UNSAVED_KEY_PREFIXES 说明）。
 */
export function outlineToTreeNode(outline: any[], parentPath = "", parentLevel = 0): TreeNode[] {
  return (outline || []).map((n, i) => {
    const level = n.level || parentLevel + 1;
    const outlineId = parentPath ? `${parentPath}.${i + 1}` : String(i + 1);
    const hasChildren = Array.isArray(n.children) && n.children.length > 0;
    return {
      key: n.id || `upload_${Date.now()}_${i}`,
      title: n.title,
      level,
      status: "empty",
      word_count: 0,
      word_budget: n.word_budget || 1500,
      description: n.description || "",
      outlineId,
      children: hasChildren ? outlineToTreeNode(n.children, outlineId, level) : [],
    };
  });
}

/**
 * 本地重算展示用编号（outlineId/level）：拖拽/移动后即时刷新，
 * 仅改展示字段，不动 key 与正文数据。
 */
export function renumberTreeLocally(nodes: TreeNode[], parentPath = "", parentLevel = 0): TreeNode[] {
  return nodes.map((n, i) => {
    const level = parentLevel + 1;
    const outlineId = parentPath ? `${parentPath}.${i + 1}` : String(i + 1);
    return {
      ...n,
      level,
      outlineId,
      children: n.children?.length ? renumberTreeLocally(n.children, outlineId, level) : [],
    };
  });
}

/**
 * 目录树卡片右上操作区（2026-09-21 拆出，行为由 outlineTab.test 钉住）：
 * 「下一步 / 新增子章节 / 上移 / 下移 / 删除」全部回调化，禁用口径内聚：
 * 未选中 → 四个操作键禁用；同层首/尾（moveFlags）→ 对应方向禁用。
 */
/**
 * 断线重挂后「部分成果」弹窗文案（模块级导出，行为由 outlineTab.test 钉住）。
 *
 * ✅ 口径补齐（2026-09-21）：旧实现 failed / stopped 两个分支各自手拼文案，
 * 且只说「已生成 N 章的目录尚未保存」——**不告诉用户为什么中断**。
 * 现在 checkpoint 白名单已回传 `event` / `partial`（见后端
 * `_CHECKPOINT_KINDS.outline_generation`），可据此区分两种语义：
 *   · event=stopped —— 用户主动停止 / 客户端断线兜底：成果本身可信，
 *     建议按需补齐后保存；
 *   · event=error —— AI 调用异常后落库的兜底成果：内容可能不完整，
 *     建议先补齐再考虑重试整章。
 * 收口成纯函数，避免两个消费分支（takeover / SSE 中断重挂）文案再次漂移。
 */
export type PartialOutlineHint = {
  title: string;
  content: string;
  okText: string;
  cancelText: string;
  /** 弹窗视觉基调：warning = 异常中断，info = 用户主动停止 */
  tone: "warning" | "info";
};

export function buildPartialOutlineHint(opts: {
  event?: string;
  partial?: boolean;
  nodeCount?: number;
}): PartialOutlineHint {
  const isErr = opts.event === "error" || opts.event === "failed";
  const n = Math.max(0, Number(opts.nodeCount) || 0);
  const prefix = `检测到已生成 ${n} 个章节节点尚未保存。`;
  const cause = isErr
    ? "本次生成因 AI 服务异常中断，成果可能不完整：建议先按需补齐缺失章节，再考虑重试整章。"
    : "本次生成由用户主动停止或连接中断，已生成部分结果可直接使用：按需补齐后保存即可。";
  return {
    title: isErr ? "目录生成中断，但有部分成果" : "目录生成已停止，但有部分成果",
    content: `${prefix}${cause}`,
    okText: "保存已生成部分",
    cancelText: "不保存",
    tone: isErr ? "warning" : "info",
  };
}

export function OutlineTreeActions({
  selected, moveFlags, showNext, onAddChild, onMove, onDelete, onNextStep,
}: {
  selected: TreeNode | null;
  moveFlags: Record<string, { up: boolean; down: boolean }>;
  showNext: boolean;
  onAddChild: () => void;
  onMove: (dir: -1 | 1) => void;
  onDelete: () => void;
  onNextStep: () => void;
}) {
  return (
    <Space size={4}>
      {showNext && (
        <Button
          size="small"
          type="link"
          icon={<ArrowRightOutlined />}
          onClick={onNextStep}
        >
          下一步：全局事实
        </Button>
      )}
      <Tooltip title="新增子章节到选中节点">
        <Button
          size="small"
          icon={<CaretRightOutlined />}
          disabled={!selected}
          onClick={onAddChild}
        >
          新增子章节
        </Button>
      </Tooltip>
      <Tooltip title="上移选中章节">
        <Button
          size="small"
          icon={<ArrowUpOutlined />}
          disabled={!selected || !moveFlags[selected.key]?.up}
          onClick={() => onMove(-1)}
        >
          上移
        </Button>
      </Tooltip>
      <Tooltip title="下移选中章节">
        <Button
          size="small"
          icon={<ArrowDownOutlined />}
          disabled={!selected || !moveFlags[selected.key]?.down}
          onClick={() => onMove(1)}
        >
          下移
        </Button>
      </Tooltip>
      <Tooltip title="删除选中节点">
        <Button
          size="small"
          danger
          icon={<StopOutlined />}
          disabled={!selected}
          onClick={onDelete}
        >
          删除
        </Button>
      </Tooltip>
    </Space>
  );
}

/** 字数预算的合法区间（与后端 sectionsApi.update 的整数语义一致，超限会被服务端拒绝） */
export const WORD_BUDGET_MIN = 50;
export const WORD_BUDGET_MAX = 50000;

/**
 * 选中章节的「字数预算」编辑面板（模块级导出，行为由 outlineTab.test 钉住）。
 *
 * ✅ 功能缺口补齐（2026-09-21）：字数预算此前在整条前端链路中**只有读取、没有写入** ——
 *   · 目录树行内只显示只读的 `(L1 · 1500字)`；
 *   · 正文页只显示 `x字 / y字`，预算超了只能点「字数压缩」；
 *   · 压缩器「只缩不扩」，正文偏少时没有任何入口调低预算。
 * 用户无法在正文生成**前**按需规划每章篇幅，只能等生成完再补救。
 * 现把预算编辑收敛到「目录生成」Tab 的选中章节面板：失焦 / 回车即回抛，
 * 落库由父组件统一处理（临时节点 local_/upload_ 仅改本地，等「保存目录」）。
 *
 * 校验口径（防脏数据写库）：非正数 → 回退原值不提交；越界 → 收敛到
 * [WORD_BUDGET_MIN, WORD_BUDGET_MAX]；与原值相同 → 不触发回调（避免无谓写库）。
 */
export const OutlineNodeBudgetPanel = memo(function OutlineNodeBudgetPanel({
  node,
  onBudgetChange,
}: {
  node: TreeNode;
  onBudgetChange: (key: string, budget: number) => void;
}) {
  const current = node.word_budget || 1500;
  const [draft, setDraft] = useState<number>(current);
  // 选中章节切换 / 预算被外部改动（如 load 刷新）时同步草稿
  useEffect(() => {
    setDraft(current);
  }, [node.key, current]);

  const commit = () => {
    const n = Math.round(Number(draft));
    // 空值 / 非数字 / 非正数：回退原值，不静默提交脏数据
    if (!Number.isFinite(n) || n <= 0) {
      setDraft(current);
      return;
    }
    const clamped = Math.min(WORD_BUDGET_MAX, Math.max(WORD_BUDGET_MIN, n));
    setDraft(clamped);
    if (clamped !== current) onBudgetChange(node.key, clamped);
  };

  const overBudget = (node.word_count || 0) > current;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        padding: "6px 10px",
        marginBottom: 8,
        background: "#fafafa",
        border: "1px solid #f0f0f0",
        borderRadius: 4,
      }}
    >
      <Text strong style={{ fontSize: 12 }}>📏 字数预算</Text>
      <Text ellipsis style={{ fontSize: 12, fontWeight: 600, maxWidth: 240 }}>
        {formatOutlineTitle(node.outlineId || node.key, node.level, node.title)}
      </Text>
      <Text type="secondary" style={{ fontSize: 11 }}>L{node.level}</Text>
      {/* addonAfter 已废弃，改用 Space.Compact + Space.Addon（antd v5.29+） */}
      <Space.Compact size="small" style={{ width: 116 }}>
        <InputNumber
          // ⚠️ 不设 min/max：antd 会在 blur 时自行把越界值收敛到 min/max 并触发
          // 一轮额外的 onChange —— 与下方 commit 的受控夹取互相打架（测试里出现
          // 「输入 10 → 期望 50 却 0 次回调 / 输入 0 → 被夹成 50」的竞态）。
          // 夹取口径统一收口在 commit（[WORD_BUDGET_MIN, WORD_BUDGET_MAX]），
          // 秒回弹、幂等、可单测。
          step={100}
          value={draft}
          onChange={(v) => setDraft(v as number)}
          onBlur={commit}
          onPressEnter={commit}
          style={{ width: "100%" }}
          aria-label="字数预算"
        />
        <Space.Addon>字</Space.Addon>
      </Space.Compact>
      <Text type="secondary" style={{ fontSize: 11 }}>
        已写 {(node.word_count || 0).toLocaleString()} 字
      </Text>
      {overBudget ? (
        <Tag color="warning" style={{ margin: 0, fontSize: 11 }}>
          超预算，可在正文页压缩
        </Tag>
      ) : (
        <Tag style={{ margin: 0, fontSize: 11 }}>生成正文时按此目标控制篇幅</Tag>
      )}
    </div>
  );
});

// ============================================================
// SSE 断线兜底轮询（2026-09-20 提取为模块级可注入实现）：
// 原为组件内闭包（依赖 tasksApi/setProgress），无法在测试中驱动其
// 终态判定/兜底分支；提取后组件侧仅剩 setState 胶水，断线重挂契约
// （含 stuckAtFull 兜底保留 outline_result）可直接行为单测。
// ============================================================
/** GET /sse/task/{id} 终态响应（含 checkpoint 回传的 outline_result / content_result） */
export interface TaskTerminalInfo {
  status: string;
  message?: string;
  progress?: number;
  outline_result?: {
    outline?: unknown[];
    failed_chapters?: unknown[];
    failed_count?: number;
    /**
     * ✅ 契约补齐（2026-09-21）：后端 `_CHECKPOINT_KINDS.outline_generation`
     * 白名单含 `review`，断线重挂后目录的审核/修复结果此前在前端无处安放。
     */
    review?: unknown;
    /**
     * ✅ 契约补齐：checkpoint 记录的是**终态事件类型**（completed/stopped/error），
     * 前端此前只能靠 `status` 推断，无法区分「用户主动停止保留的部分成果」
     * 与「AI 异常后保留的部分成果」，两者的补救建议不同。
     */
    event?: string;
    /** partial=true 表示成果不完整（停止 / 失败兜底），需提示用户手动补齐 */
    partial?: boolean;
  };
  /** ✅ G12-6：正文生成 checkpoint（后端 `_CHECKPOINT_KINDS.content_generation` 白名单） */
  content_result?: {
    event?: string;
    message?: string;
    done?: number;
    total?: number;
    failed_count?: number;
    failed_sections?: Array<{ section_id?: string; title?: string; reason?: string }>;
    words?: number;
    word_count?: number;
    run_words?: number;
    over_count?: number;
  };
  [k: string]: unknown;
}

export async function pollTaskUntilTerminalImpl(
  taskId: string,
  fetchStatus: (taskId: string) => Promise<TaskTerminalInfo>,
  onPending?: (data: TaskTerminalInfo) => void,
  opts: {
    intervalMs?: number;
    maxPolls?: number;
    stuckThreshold?: number;
    netFailTolerance?: number;
  } = {},
): Promise<TaskTerminalInfo | null> {
  const intervalMs = opts.intervalMs ?? 3000;
  const maxPolls = opts.maxPolls ?? 200;
  const stuckThreshold = opts.stuckThreshold ?? 3;
  const netFailTolerance = opts.netFailTolerance ?? 5;
  let stuckAtFull = 0; // progress==1.0 但 status 仍 running 的连续次数
  let netFailCount = 0; // 连续网络失败计数
  for (let i = 0; i < maxPolls; i++) {
    await new Promise((r) => setTimeout(r, intervalMs));
    try {
      const data = await fetchStatus(taskId);
      netFailCount = 0; // ✅ 成功一次即重置，瞬时网络抖动不应终止重挂
      if (["completed", "failed", "stopped"].includes(data.status)) return data;
      onPending?.(data);
      // ✅ 进度已满但 status 没更新：连续达阈值就兜底为 completed
      if ((data.progress || 0) >= 0.99 && data.status === "running") {
        stuckAtFull++;
        if (stuckAtFull >= stuckThreshold) {
          console.warn(`pollTaskUntilTerminal: task ${taskId} stuck at progress=100% for ${stuckAtFull} polls, treating as completed`);
          // ✅ BUG 修复（2026-09-20）：兜底判定 completed 时必须保留全量字段
          //   （尤其 outline_result）—— 后台成果已落 checkpoint，丢字段会让
          //   调用方（fin.outline_result 消费分支）误判无成果直接 load()。
          return { ...data, status: "completed", message: data.message };
        }
      } else {
        stuckAtFull = 0;
      }
    } catch (e: any) {
      // ✅ 修复：区分「任务确实不存在（404，不可恢复）」与「瞬时网络失败」。
      // 旧实现对任何一次请求失败都直接放弃重挂，一次抖动/502 就导致兜底失效。
      if (e?.response?.status === 404) return null;
      netFailCount++;
      if (netFailCount >= netFailTolerance) {
        console.warn(`pollTaskUntilTerminal: task ${taskId} 连续 ${netFailCount} 次网络失败，放弃重挂`);
        return null;
      }
    }
  }
  return null;
}

/**
 * ✅ BUG 修复（2026-09-23 · 「30s 连接超时后目录任务孤儿运行」）：
 * SSE 在收到首个事件（含 task_id 的 connecting）之前建连失败/超时时，
 * 前端 taskId 为空串，旧实现直接跳过 pollTaskUntilTerminal 重挂 ——
 * 但后端 StreamingResponse 可能已返回、任务已注册并继续生成（AI 照常
 * 计费），成果只躺在 checkpoint 里。此处按方案检索最近的
 * outline_generation 任务，若仍在运行且 updated_at 在时间窗内
 * （默认 5 分钟，覆盖 30s 建连超时 + 装配/排队耗时），返回其 id 供重挂。
 * 提取为模块级实现（fetchTasks 注入），可单测。
 */
export async function findRecentOutlineTaskImpl(
  fetchTasks: () => Promise<{ tasks?: Array<Record<string, any>> }>,
  opts: { typeFilter?: string; windowMs?: number; nowMs?: number } = {},
): Promise<string | null> {
  const typeFilter = opts.typeFilter ?? "outline_generation";
  const windowMs = opts.windowMs ?? 5 * 60_000;
  const nowMs = opts.nowMs ?? Date.now();
  try {
    const { tasks } = await fetchTasks();
    for (const t of tasks || []) {
      if (t.task_type !== typeFilter) continue;
      if (t.status !== "running" && t.status !== "paused") continue;
      // updated_at 解析失败（脏值/缺字段）时保守接受，避免漏挂
      const ts = Date.parse(t.updated_at || "");
      if (!Number.isNaN(ts) && nowMs - ts > windowMs) continue;
      return String(t.id || "");
    }
  } catch {
    // 检索失败（后端确实不可达）：静默返回 null，由调用方保持旧行为（仅提示）
  }
  return null;
}

// ============================================================
// 一级目录确认闸门（对齐 OpenBidKit 的 outline-selection 阶段）：
// AI 生成目录后不自动入库，先由用户勾选确认一级章节；确认后保存并
// 将一级章节 locked=1（与 OpenBidKit 的 allowRootChanges=false 语义一致）
// ============================================================
export function OutlineGateList({
  outline,
  selectedRef,
}: {
  outline: any[];
  selectedRef: { current: number[] };
}) {  const [checked, setChecked] = useState<string[]>(
    outline.map((_: any, i: number) => String(i))
  );
  // 同步勾选结果到 ref（modal.confirm onOk 读取，避免闭包陈旧值）
  useEffect(() => {
    selectedRef.current = checked.map(Number);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked]);
  const toggle = (i: number, on: boolean) => {
    setChecked((prev) =>
      on ? [...prev, String(i)] : prev.filter((x) => x !== String(i))
    );
  };
  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <Text type="secondary">
          AI 已生成 {outline.length} 个一级章节。取消勾选不需要的章节（其全部子级一并排除）；
          确认保存后一级目录将被锁定。取消勾选过少可能导致方案覆盖不全，请谨慎操作。
        </Text>
      </div>
      <div
        style={{
          maxHeight: 320,
          overflow: "auto",
          border: "1px solid #f0f0f0",
          borderRadius: 6,
          padding: "4px 12px",
        }}
      >
        {outline.map((n: any, i: number) => (
          <div key={i} style={{ padding: "4px 0" }}>
            <Checkbox
              checked={checked.includes(String(i))}
              onChange={(e) => toggle(i, e.target.checked)}
            >
              {n.title || `（未命名第 ${i + 1} 章）`}
            </Checkbox>
            {n.description ? (
              <Text
                type="secondary"
                style={{ fontSize: 12, marginLeft: 8, whiteSpace: "normal" }}
              >
                {String(n.description).slice(0, 60)}
              </Text>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export function findSectionById(nodes: TreeNode[], key: string): TreeNode | null {
  for (const n of nodes) {
    if (n.key === key) return n;
    const found = findSectionById(n.children || [], key);
    if (found) return found;
  }
  return null;
}

// ✅ P0-5 组件拆分：章节日志列表独立 memo 组件——sectionLogs 更新只重渲日志区，
//    不再带动整页其余部分重渲；行按 section_id 稳定 key，避免整表重建
// ✅ 实时日志面板的最大渲染行数：仅限制渲染，数据源保持全量。
// 长方案（120+ 章）逐章多次事件的日志可达上千条，而该滚动区高度固定，
// 全量渲染会把视口外的条目也建成 DOM；与下方 OUTLINE_LOG_RENDER_MAX 同口径。
const SECTION_LOG_RENDER_MAX = 200;

// ✅ 性能优化：状态图标与行底色与 items 无关，提到模块级只构造一次
// （原实现在组件内每次渲染重建两个对象字面量）
const SECTION_LOG_ICON: Record<string, React.ReactNode> = {
  running: <LoadingOutlined style={{ color: "#1677ff" }} />,
  success: <CheckCircleOutlined style={{ color: "#52c41a" }} />,
  failed: <CloseCircleOutlined style={{ color: "#ff4d4f" }} />,
  skipped: <MinusCircleOutlined style={{ color: "#faad14" }} />,
};
const SECTION_LOG_BG: Record<string, string> = {
  running: "rgba(22,119,255,0.06)",
  success: "rgba(82,196,26,0.06)",
  failed: "rgba(255,77,79,0.06)",
  skipped: "rgba(250,173,20,0.06)",
};

const SectionLogList = memo(function SectionLogList({ items }: { items: SectionLogItem[] }) {
  // ✅ 性能优化：`[...items].reverse()` 原直接写在 JSX 里 —— 长方案日志条目
  // 持续增长，每次条目变化都全量复制 + 反转 + 全量建 DOM。改为按 items 记忆化
  // 并限制渲染行数（数据源 items 本身不动，完成统计等在 GenerationProgressCard
  // 内基于完整历史计算，不受影响）。
  const visibleItems = useMemo(
    () => (items.length > SECTION_LOG_RENDER_MAX
      ? items.slice(-SECTION_LOG_RENDER_MAX).reverse()
      : [...items].reverse()),
    [items],
  );
  return (
    <List
      size="small"
      dataSource={visibleItems}
      renderItem={(item) => (
        <List.Item key={item.section_id || item.title} style={{ padding: "4px 8px", background: SECTION_LOG_BG[item.status], borderRadius: 4, marginBottom: 4 }}>
          <div style={{ width: "100%" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {SECTION_LOG_ICON[item.status]}
              {typeof item.index === "number" && item.total && (
                <Tag style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                  {item.index}/{item.total}
                </Tag>
              )}
              <Text
                strong
                ellipsis
                style={{ flex: 1, fontSize: 13 }}
                title={item.title}
              >
                {item.title}
              </Text>
              {/* ✅ 进度增强：进行中章节显示当前阶段（AI 生成中 / 续写扩充中 …），
                  让用户知道这一章卡在哪一步，而不是只有一个转圈图标 */}
              {item.status === "running" && item.stage_label && (
                <Tag
                  color={STAGE_COLOR[item.stage || "draft"] || "processing"}
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {item.stage_label}
                </Tag>
              )}
              {item.status === "success" && (
                <Tag
                  color={
                    item.word_status === "under"
                      ? "orange"
                      : item.word_status === "over"
                      ? "red"
                      : "success"
                  }
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {item.word_count}字 / {item.word_budget}
                  {item.word_status === "under"
                    ? "（偏少）"
                    : item.word_status === "over"
                    ? "（超字，可压缩）"
                    : ""}
                </Tag>
              )}
              {/* ✅ 续写失败信号：发生过续写调用失败/产出被丢弃且最终仍不达标，
                  单独提示用户「可重试或手工续写」，与「写不满」区分 */}
              {item.status === "success" && item.continue_failed && (
                <Tooltip title="续写调用失败或产出被丢弃，最终字数仍偏少——可对该章「续写本章」或重试补足">
                  <Tag color="warning" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                    续写未完成
                  </Tag>
                </Tooltip>
              )}
              {/* ✅ 质量告警（后端 section_done.quality_issues）：本章正文存在程序化
                  质检问题（如未闭合代码块、章节重复标题等），即时提示可复查 */}
              {item.status === "success" && Array.isArray(item.quality_issues) && item.quality_issues.length > 0 && (
                <Tooltip title={item.quality_issues.map((q: any) => String(q?.message || q?.detail || q)).join("；")}>
                  <Tag color="gold" style={{ margin: 0, fontSize: 11, flexShrink: 0 }}>
                    质量告警 {item.quality_issues.length}
                  </Tag>
                </Tooltip>
              )}
              {/* ✅ F-CONTENT-STANDARD(2026-09-26)：模式徽标 + 校验告警。
                  模式徽标回答「这章是按哪种标准写的」，告警 Popover 回答
                  「哪里不符合、怎么改」—— 两问缺一，用户就只能靠通读正文自查。 */}
              {item.generation_standard && (
                <Tag
                  color={item.generation_standard === "precise" ? "blue" : "cyan"}
                  style={{ margin: 0, fontSize: 11, flexShrink: 0 }}
                >
                  {STANDARD_LABELS[item.generation_standard] || item.generation_standard}
                </Tag>
              )}
              {item.status === "success" && reportHasIssues(item.standard_report) && (
                <Popover
                  placement="leftTop"
                  title="生成标准校验（咨询性，不阻断生成）"
                  content={
                    <div style={{ maxWidth: 420, maxHeight: 300, overflowY: "auto" }}>
                      {(item.standard_report?.issues || []).map((it: any, i: number) => (
                        <div key={i} style={{ marginBottom: 6, fontSize: 12 }}>
                          <Tag
                            color={it?.severity === "error" ? "red" : "orange"}
                            style={{ marginRight: 4, fontSize: 11 }}
                          >
                            {issueLabel(it)}
                          </Tag>
                          <span>{it?.message || "（无描述）"}</span>
                          {it?.excerpt && (
                            <div style={{ color: "#999", fontSize: 11, marginTop: 2 }}>
                              原文：{it.excerpt}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  }
                >
                  <Tag color="volcano" style={{ margin: 0, fontSize: 11, flexShrink: 0, cursor: "help" }}>
                    标准告警 {reportIssueCount(item.standard_report)}
                  </Tag>
                </Popover>
              )}
              {/* 生成中 → 实时耗时；已完成 → 总耗时 */}
              {item.status === "running" ? (
                <ElapsedTimer startTime={item.time} />
              ) : (
                typeof item.duration === "number" && (
                  <Text type="secondary" style={{ fontSize: 11, marginLeft: 4, flexShrink: 0 }}>
                    {item.duration < 1000
                      ? `${item.duration}ms`
                      : `${(item.duration / 1000).toFixed(1)}s`}
                  </Text>
                )
              )}
              {item.status === "failed" && (
                <ExclamationCircleOutlined style={{ color: "#ff4d4f", fontSize: 13 }} />
              )}
            </div>
            {item.status === "failed" && item.reason && (
              <div
                style={{
                  marginTop: 2,
                  padding: "4px 8px",
                  background: "rgba(255,77,79,0.08)",
                  borderLeft: "2px solid #ff4d4f",
                  borderRadius: 2,
                  fontSize: 12,
                  color: "#cf1322",
                  wordBreak: "break-all",
                }}
              >
                {item.reason}
              </div>
            )}
          </div>
        </List.Item>
      )}
    />
  );
});

// ✅ P0-5 组件拆分：生成进度卡（Progress + 统计 + 双日志）独立 memo 组件——
//    生成期间高频更新的只有本卡与日志，抽离后整页其余部分不再随之重渲
export type GenerationProgressProps = {
  genType: string;
  progress: number;
  progressMsg: string;
  sectionLogs: SectionLogItem[];
  outlinePhase: string;
  outlineDoneTotal: OutlineLogItem | null;
  outlineLogs: OutlineLogItem[];
  /** ✅ 进度增强：后端下发的运行统计（已耗时 / ETA / 累计字数 / 并发 / 进行中章节） */
  stats?: GenStats;
};

/** ✅ 实时日志面板的最大渲染行数：仅限制渲染，数据源保持全量。
 *  长方案分步生成（数十章 × 多阶段）日志可达上千条，而这个滚动区仅 280px 高，
 *  全量渲染会造成严重掉帧；截断到最近若干条对用户可见内容无实质影响。 */
const OUTLINE_LOG_RENDER_MAX = 200;

export const GenerationProgressCard = memo(function GenerationProgressCard(p: GenerationProgressProps) {
  const {
    genType, progress, progressMsg, sectionLogs,
    outlinePhase, outlineDoneTotal, outlineLogs, stats,
  } = p;
  // ✅ 性能优化：原实现把 `[...outlineLogs].reverse()` 写在 JSX 里 —— SSE 期间
  //    progress 与日志每条更新都触发重渲，也就意味着每次都全量复制一遍整个数组，
  //    同时把不断增加的日志全部渲染成 List.Item。改为按 outlineLogs 记忆化，
  //    并对渲染行数设上限（数据源 outlineLogs 本身不动，其它派生产物如
  //    outlineDoneTotal 仍需读取完整历史，行为保持不变）。
  const outlineLogItems = useMemo(
    () => (outlineLogs.length > OUTLINE_LOG_RENDER_MAX
      ? outlineLogs.slice(-OUTLINE_LOG_RENDER_MAX).reverse()
      : [...outlineLogs].reverse()),
    [outlineLogs],
  );
  const isContent = genType === "content";
  const isOutline = genType === "outline";
  // ✅ 性能优化：正文生成期间 sectionLogs 随每条 SSE 更新，原实现的 3 次独立
  //    filter 等同把整份日志遍历 3 遍（长方案可达数百章）。合并为单遍扫描 +
  //    按 sectionLogs 记忆化，非本字段变化（如 progress）不再重算。
  const { runningLogs, successCount, failedCount } = useMemo(() => {
    const running: SectionLogItem[] = [];
    let success = 0;
    let failed = 0;
    for (const l of sectionLogs) {
      if (l.status === "running") running.push(l);
      else if (l.status === "success") success += 1;
      else if (l.status === "failed") failed += 1;
    }
    return { runningLogs: running, successCount: success, failedCount: failed };
  }, [sectionLogs]);
  // ✅ 进度增强：优先采用后端权威统计（含未进入日志的失败章），缺失时回退本地计数
  const doneCount = typeof stats?.done === "number" ? stats.done : successCount + failedCount;
  const totalCount = stats?.total ?? sectionLogs[0]?.total ?? 0;
  const elapsedMs = stats?.elapsed_ms ?? null;
  const etaMs = stats?.eta_ms ?? null;
  const words = stats?.words ?? 0;
  const concurrency = stats?.concurrency ?? 0;
  const nodes = stats?.nodes ?? 0;
  const avgSectionMs = stats?.avg_section_ms ?? null;
  // ✅ 二次增强：失败数取「后端统计 / 本地日志」的较大值 —— 强制重写失败等场景
  //    章节可能不在日志里（保留旧正文、状态未改），只看日志会把失败数显示为 0，
  //    与完成提示的「N 章失败」自相矛盾。目录与正文模式同口径。
  const failedShown = Math.max(
    failedCount,
    typeof stats?.failed === "number" ? stats.failed : 0,
  );
  // 目录生成分步链路才有章节维度（done/total = 已生成子目录的章数 / 总章数）
  const outlineStepwise = isOutline && !!stats?.stepwise;
  const hasFailed = failedShown > 0 && progress >= 1;
  return (
    <Card
      size="small"
      style={{ marginBottom: 12, flexShrink: 0 }}
      styles={{ body: { padding: 12 } }}
    >
      <div style={{ display: "flex", gap: 16 }}>
        {/* ---- 左侧：任务标题 + 横向进度条 + 运行统计 + 当前工作内容 ---- */}
        <div style={{ flex: "0 0 420px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <Text strong style={{ fontSize: 13 }}>
              {genType === "outline" ? "📂 正在生成目录"
                : isContent ? "📝 正在生成正文" : "⏳ 正在处理"}
            </Text>
            {isContent && totalCount > 0 && (
              <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                已完成 {doneCount}/{totalCount}
              </Text>
            )}
            {outlineStepwise && totalCount > 0 && (
              <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                已完成 {doneCount}/{totalCount} 章
              </Text>
            )}
          </div>
          <Progress
            percent={Math.round(progress * 100)}
            status={hasFailed ? "exception" : "active"}
          />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <Text style={{ fontSize: 12, color: "#666", flex: 1, minWidth: 0 }} ellipsis title={progressMsg}>
              {progressMsg}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
              {Math.round(progress * 100)}%
            </Text>
          </div>

          {/* ✅ 进度增强：运行态指标（已耗时 / 预计剩余 / 累计字数 / 并发档位 / 目录节点数）
              目录生成同样展示已耗时与预计剩余 —— 审核+修复最长 180s，没有时间反馈时
              用户无法判断是「在跑」还是「卡死」。 */}
          {(isContent || isOutline) && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {elapsedMs ? (
                <Tag icon={<ClockCircleOutlined />} style={{ margin: 0 }}>
                  已耗时 {fmtDuration(elapsedMs)}
                </Tag>
              ) : null}
              {etaMs ? (
                <Tag icon={<HourglassOutlined />} color="blue" style={{ margin: 0 }}>
                  预计剩余 {fmtDuration(etaMs)}
                </Tag>
              ) : null}
              {isContent && words > 0 ? (
                <Tag icon={<FileTextOutlined />} style={{ margin: 0 }}>
                  已生成 {words.toLocaleString()} 字
                </Tag>
              ) : null}
              {/* ✅ 二次增强：平均单章耗时 —— 用户据此判断「还要等多久」「是不是变慢了」 */}
              {isContent && avgSectionMs ? (
                <Tag style={{ margin: 0 }} title="已完成章节的平均实测耗时">
                  平均单章 {fmtDuration(avgSectionMs)}
                </Tag>
              ) : null}
              {(isContent || isOutline) && failedShown > 0 ? (
                <Tag color="error" style={{ margin: 0 }}>
                  失败 {failedShown}
                </Tag>
              ) : null}
              {isOutline && nodes > 0 ? (
                <Tag icon={<FileTextOutlined />} style={{ margin: 0 }}>
                  目录 {nodes} 个节点
                </Tag>
              ) : null}
              {isContent && concurrency > 0 ? (
                <Tag icon={<ThunderboltOutlined />} style={{ margin: 0 }}>
                  并发 {concurrency}
                </Tag>
              ) : null}
            </div>
          )}

          {/* ✅ 进度增强：当前正在生成的章节（章节名 + 阶段），让用户知道"卡在哪一步" */}
          {isContent && runningLogs.length > 0 && (
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
              {runningLogs.slice(0, 4).map((l) => (
                <Tag
                  key={l.section_id}
                  color={STAGE_COLOR[l.stage || "draft"] || "processing"}
                  style={{ margin: 0, maxWidth: 200, overflow: "hidden" }}
                  title={`${l.title}${l.stage_label ? " · " + l.stage_label : ""}`}
                >
                  <span style={{ fontSize: 11 }}>
                    {typeof l.index === "number" ? `${l.index}. ` : ""}
                    {l.title}
                    {l.stage_label ? ` · ${l.stage_label}` : ""}
                  </span>
                </Tag>
              ))}
              {runningLogs.length > 4 && (
                <Tag style={{ margin: 0, fontSize: 11 }}>
                  +{runningLogs.length - 4}
                </Tag>
              )}
            </div>
          )}

          {/* 统计色块：正文=章节统计；目录=阶段 + 已完成章节 */}
          {genType === "outline" ? (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Tag color="processing" style={{ margin: 0 }}>
                <LoadingOutlined /> {outlinePhase}
              </Tag>
              {/* 后端 stats 未到时（连接初期/旧任务）回退到日志推断的章节计数 */}
              {!outlineStepwise && outlineDoneTotal && (
                <Tag color="blue" style={{ margin: 0 }}>
                  章节 {outlineDoneTotal.done}/{outlineDoneTotal.total}
                </Tag>
              )}
            </div>
          ) : (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Tag color="processing" style={{ margin: 0 }}>
                <LoadingOutlined /> {runningLogs.length} 进行中
              </Tag>
              <Tag color="success" style={{ margin: 0 }}>
                <CheckCircleOutlined /> {successCount} 成功
              </Tag>
              <Tag color="error" style={{ margin: 0 }}>
                <CloseCircleOutlined /> {failedShown} 失败
              </Tag>
            </div>
          )}
        </div>

        {/* ---- 右侧：正文生成章节滚动日志 ---- */}
        {genType === "content" && (
        <div style={{ flex: 1, minHeight: 0, maxHeight: 280, overflow: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: "6px 10px", background: "#fafafa" }}>
          {sectionLogs.length === 0 ? (
            <div style={{ textAlign: "center", padding: 12, color: "#999" }}>
              正在连接 AI，等待章节生成...
            </div>
          ) : (
            <SectionLogList items={sectionLogs} />
          )}
        </div>
        )}

        {/* ---- 右侧：目录生成实时日志 ---- */}
        {genType === "outline" && (
        <div style={{ flex: 1, minHeight: 0, maxHeight: 280, overflow: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: "6px 10px", background: "#fafafa" }}>
          {outlineLogs.length === 0 ? (
            <div style={{ textAlign: "center", padding: 12, color: "#999" }}>
              正在连接 AI，准备生成目录...
            </div>
          ) : (
            <List
              size="small"
              dataSource={outlineLogItems}
              // ✅ 性能优化：反转后若沿用默认的 index key，每新增一条日志都会让
              //    所有行的 key 顺移一位 → 全量 reconcile。改为按内容派生稳定 key。
              rowKey={(item: OutlineLogItem) => `${item.time}-${item.message}`}
              renderItem={(item) => (
                <List.Item
                  style={{
                    padding: "4px 8px",
                    borderRadius: 4,
                    marginBottom: 4,
                    background: "rgba(22,119,255,0.05)",
                  }}
                >
                  <div style={{ width: "100%", display: "flex", alignItems: "center", gap: 8 }}>
                    <Tag color="blue" style={{ margin: 0, minWidth: 46, textAlign: "center" }}>
                      {Math.round(item.progress * 100)}%
                    </Tag>
                    <Text style={{ flex: 1, fontSize: 13 }} ellipsis title={item.message}>
                      {item.message}
                    </Text>
                    {/* 仅分步链路（total>0）有章节维度，防历史日志渲染「0/0 章」 */}
                    {typeof item.done === "number" && typeof item.total === "number" && item.total > 0 && (
                      <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                        {item.done}/{item.total} 章
                      </Text>
                    )}
                  </div>
                </List.Item>
              )}
            />
          )}
        </div>
        )}
      </div>
    </Card>
  );
});

// ✅ P0-5 组件拆分：左侧目录树面板独立 memo 组件——生成期间进度/日志更新
//    不再带动树与右侧 Tab 重渲；回调均以稳定引用传入
type SchemeTreePanelProps = {
  tree: TreeNode[];
  treeData: any[];
  expandedKeys: React.Key[];
  selectedSectionKey?: React.Key;
  loading: boolean;
  generating: boolean;
  onRefresh: () => void;
  onToggleExpand: () => void;
  /** ✅ 导航职责：目录"生成/管理"统一在「目录生成」Tab，左侧面板只负责跳转，避免两处重复入口 */
  onGoToOutline: () => void;
  onSelectSection: (key: React.Key) => void;
  onExpand: (keys: React.Key[]) => void;
  /** ✅ 编辑增强：左侧树也支持上移/下移/重命名/删除，复用与右侧编辑树同一套逻辑 */
  moveFlags: Record<string, { up: boolean; down: boolean }>;
  onMoveNode: (key: string, dir: -1 | 1) => void;
  onRenameNode: (key: string, title: string) => void;
  onDeleteNode: (key: string) => void;
  onAddChildNode: (key: string) => void;
  onAddSiblingNode: (key: string) => void;
  /** ✅ 编辑增强：左侧树顶部"有未保存修改"提示中的「保存目录」动作 */
  onSaveOutline: () => void;
  /** 行内重命名编辑态（与右侧编辑树共用同一份状态） */
  renamingKey: string | null;
  renamingValue: string;
  setRenamingKey: (k: string | null) => void;
  setRenamingValue: (v: string) => void;
  /** 左侧树滚动容器 ref（由父组件持有，编辑树移动时同步定位用） */
  scrollRef: React.RefObject<HTMLDivElement>;
};

const SchemeTreePanel = memo(function SchemeTreePanel(p: SchemeTreePanelProps) {
  const {
    tree, treeData, expandedKeys, selectedSectionKey, loading,
    generating,
    onRefresh, onToggleExpand, onGoToOutline, onSelectSection, onExpand,
    moveFlags, onMoveNode, onRenameNode, onDeleteNode,
    onAddChildNode, onAddSiblingNode, onSaveOutline,
    renamingKey, renamingValue, setRenamingKey, setRenamingValue, scrollRef,
  } = p;
  // ✅ 编辑增强：左侧树滚动容器（ref 由父组件持有，编辑树移动时同步定位）
  const handleMoveNode = (key: string, dir: -1 | 1) => {
    onMoveNode(key, dir);
    scrollToNodeInContainer(scrollRef.current, key);
  };
  return (
    <div
      style={{
        width: 300,
        flexShrink: 0,
        background: "#fafafa",
        border: "1px solid #f0f0f0",
        borderRadius: 8,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          padding: "10px 14px",
          background: "#fff",
          borderBottom: "1px solid #f0f0f0",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Space size={6}>
          <FolderOpenOutlined style={{ color: "#1677ff" }} />
          <Text strong style={{ fontSize: 13 }}>目录树</Text>
          {tree.length > 0 && (
            <Tag color="blue" style={{ margin: 0 }}>{collectAllKeys(tree).length}</Tag>
          )}
        </Space>
        <Space size={4}>
          <Tooltip title="刷新">
            <Button size="small" type="text" icon={<ReloadOutlined />} onClick={onRefresh} />
          </Tooltip>
          <Tooltip title="展开/收起">
            <Button size="small" type="text" icon={<SwapOutlined />} onClick={onToggleExpand} />
          </Tooltip>
        </Space>
      </div>

      {/* ✅ 编辑增强：左侧树新增 local_ 临时节点后，顶部高亮提醒需「保存目录」，
          右侧工具栏已有橙色 Tag，这里在导航面板内也给出可一键保存的入口 */}
      {hasUnsavedLocalNodes(tree) && (
        <Alert
          type="warning"
          showIcon
          banner
          message="有未保存的新增章节"
          description="新增的章节尚未同步到服务端，请点击「保存目录」。"
          action={
            <Button size="small" type="primary" onClick={onSaveOutline}>
              保存目录
            </Button>
          }
          style={{ borderRadius: 0 }}
        />
      )}

      {/* 树（本面板只做"章节导航"，目录的生成/编辑统一在「目录生成」Tab；
          但为编辑便利，行内提供上移/下移按钮，复用与右侧编辑树同一套 moveNode 逻辑） */}
      <div ref={scrollRef} className="scroll-area" style={{ flex: 1, overflow: "auto", padding: "8px 4px", minHeight: 0 }}>
        {/* ✅ 编辑增强：左侧树行内操作按钮默认隐藏，悬浮行时淡入，
            小屏/窄面板下显著省空间（悬浮即"折叠/展开"） */}
        <style>{`
          .swb-tree-row .swb-act { opacity: 0; transition: opacity .15s ease; }
          .swb-tree-row:hover .swb-act { opacity: 1; }
          .swb-tree-row:hover { background: rgba(0,0,0,.03); }
        `}</style>
        {loading ? (
          <div style={{ textAlign: "center", padding: 40, color: "#999" }}>加载中...</div>
        ) : tree.length > 0 ? (
          <Tree
            treeData={treeData}
            expandedKeys={expandedKeys}
            onExpand={onExpand}
            selectedKeys={selectedSectionKey ? [selectedSectionKey] : []}
            onSelect={(keys) => {
              if (keys.length > 0) onSelectSection(keys[0]);
            }}
            titleRender={(node: any) => {
              // ✅ 编辑增强：行内重命名（与右侧编辑树共用 renamingKey 状态）
              if (renamingKey === node.key) {
                return (
                  <Input
                    size="small"
                    autoFocus
                    value={renamingValue}
                    style={{ width: "100%" }}
                    onChange={(e) => setRenamingValue(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onBlur={() => {
                      onRenameNode(node.key, renamingValue);
                      setRenamingKey(null);
                    }}
                    onPressEnter={() => {
                      onRenameNode(node.key, renamingValue);
                      setRenamingKey(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setRenamingKey(null);
                      e.stopPropagation();
                    }}
                  />
                );
              }
              return (
                <div className="swb-tree-row" style={{ display: "flex", alignItems: "center", gap: 4, width: "100%" }}>
                  <span
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {node.title}
                  </span>
                  <Space
                    className="swb-act"
                    size={2}
                    align="center"
                    style={{ flexShrink: 0, marginLeft: "auto" }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Tooltip title="新增子章节">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<CaretRightOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onAddChildNode(node.key);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="新增同级章节">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<PlusOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onAddSiblingNode(node.key);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="重命名">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<EditOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          setRenamingValue(typeof node.title === "string" ? node.title : "");
                          setRenamingKey(node.key);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title={!moveFlags[node.key]?.up ? "已是最顶部" : "上移"}>
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        disabled={!moveFlags[node.key]?.up}
                        icon={<ArrowUpOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleMoveNode(node.key, -1);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title={!moveFlags[node.key]?.down ? "已是最底部" : "下移"}>
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        disabled={!moveFlags[node.key]?.down}
                        icon={<ArrowDownOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleMoveNode(node.key, 1);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="删除（含所有子章节）">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        danger
                        icon={<DeleteOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteNode(node.key);
                        }}
                      />
                    </Tooltip>
                  </Space>
                </div>
              );
            }}
            showLine={{ showLeafIcon: false }}
            blockNode
          />
        ) : (
          <Empty description="暂无目录" image={Empty.PRESENTED_IMAGE_SIMPLE}>
            <Button
              size="small"
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={generating}
              onClick={onGoToOutline}
            >
              前往「目录生成」
            </Button>
          </Empty>
        )}
      </div>
    </div>
  );
});

/** 全局事实提取的进度日志条目（阶段/分段级） */
type FactsLogItem = {
  progress: number;   // 0~1
  message: string;
  time: number;       // epoch ms
};

/** 目录生成的进度日志条目（阶段/章节级，实时显示工作内容） */
type OutlineLogItem = {
  progress: number;   // 0~1
  message: string;
  done?: number;      // 已完成章节数（长方案分步生成时下发）
  total?: number;     // 总章节数
  time: number;       // epoch ms
};

const statusColor: Record<string, string> = {
  empty: "default", pending: "processing", generated: "success",
  expanded: "blue", reviewed: "green",
};

const statusText: Record<string, string> = {
  empty: "待生成", pending: "生成中", generated: "已生成",
  expanded: "已扩写", reviewed: "已审核",
};

/** 导出预检问题类型 → 中文标签（后端 export_check 会返回多种类型，需分别展示） */
const EXPORT_ISSUE_LABEL: Record<string, string> = {
  empty_section: "空章节",
  orphan_node: "孤立节点",
  status_inconsistent: "状态不一致",
  low_word_count: "字数偏少",
  chart_failed: "图表异常",
  duplicate_section_number: "章节编号重复",
  duplicate_section_title: "章节标题重复",
};

/** 预检问题的一句话描述（title 缺失时回退 detail / chart_type） */
function describeExportIssue(iss: any): string {
  if (iss?.title) return iss.title;
  if (iss?.detail) return iss.detail;
  if (iss?.chart_type) return `图表类型 ${iss.chart_type}`;
  return "—";
}

/** 后台任务类型中文标签（断线重挂接提示用） */
const TASK_TYPE_LABEL: Record<string, string> = {
  outline_generation: "目录生成",
  content_generation: "正文生成",
  facts_generation: "全局事实提取",
};

/**
 * ✅ 工作流 Tab 标签：步骤序号 + 标题 + 状态徽标。
 * 把「提取项目 → 目录生成 → 全局事实 → 正文生成 → 审核与预检 → 导出」的工序顺序
 * 直接画在导航栏上，用户一眼能看到当前在哪一步、下一步该做什么。
 */
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
function countGeneratedLeaves(nodes: TreeNode[]): { done: number; total: number } {
  let done = 0;
  let total = 0;
  const walk = (n: TreeNode) => {
    if (!n.children || n.children.length === 0) {
      total += 1;
      if ((n.word_count || 0) > 0 || (n.content || "").trim()) done += 1;
    } else {
      n.children.forEach(walk);
    }
  };
  nodes.forEach(walk);
  return { done, total };
}

/** 规范符合性检查默认清单（每行一条，前端发送为 string[]，后端交给 AI 逐条判定） */
const DEFAULT_COMPLIANCE_CHECKLIST = [
  "是否引用了现行有效的规范、标准与设计文件",
  "工程概况与周边环境描述是否完整（地质、水文、邻近建构筑物）",
  "施工工艺流程是否覆盖全部工序且有可执行步骤",
  "关键工序是否给出设计计算书与验算依据",
  "材料与构配件规格、强度等级是否明确",
  "安全保证措施是否覆盖主要风险源且有针对性",
  "应急处置措施是否含组织机构、物资、演练要求",
  "人员分工与岗位职责是否明确到岗到人",
  "施工进度计划与劳动力配置是否匹配",
  "验收要求与检验批划分是否符合规范",
];

// ===== 全文一致性 Agent 修复：状态/类型中文标签（与后端 conflict_type 对齐）=====
const CR_TYPE_LABEL: Record<string, string> = {
  numeric: "数值",
  param: "参数",
  person: "人名",
  model: "型号",
  timeline: "时间口径",
  commitment: "承诺口径",
  duplication: "章节重复",
  facts: "与事实不符",
  design: "与设计不符",
  standard: "与规范不符",
};
const CR_STATUS_LABEL: Record<string, string> = {
  pending: "待修复",
  repaired: "已修复待确认",
  accepted: "已接受",
  skipped: "待人工确认",
  failed: "修复失败",
};
const CR_ITEM_STATUS: Record<string, string> = {
  repaired: "已修复",
  skipped: "跳过",
  failed: "失败",
};
const CR_REPAIR_STATUS: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  partially_confirmed: "部分确认",
  rejected: "已拒绝",
  rolled_back: "已回滚",
};

/** 全局事实分类选项（与后端 CATEGORY_TITLES 对齐，用于手工录入/编辑时归类） */
const FACT_CATEGORY_OPTIONS: { value: string; label: string }[] = [
  { value: "basic", label: "基本信息" },
  { value: "basis", label: "编制依据" },
  { value: "personnel", label: "人员角色" },
  { value: "labor", label: "劳动力配置" },
  { value: "schedule", label: "工期安排" },
  { value: "equipment", label: "主要设备配置" },
  { value: "machinery", label: "机械统计" },
  { value: "material_mgmt", label: "材料管理" },
  { value: "tech_param", label: "技术参数" },
  { value: "scale", label: "工程规模与地质参数" },
  { value: "risk", label: "风险与危大工程" },
  { value: "safety_critical", label: "安全关键参数" },
  { value: "deployment", label: "施工部署" },
  { value: "temporary", label: "临时工程" },
  { value: "process", label: "施工流程" },
  { value: "execution", label: "工程做法" },
  { value: "monitoring", label: "监测方案" },
  { value: "quality", label: "质量标准" },
  { value: "acceptance", label: "验收要求" },
  { value: "commitment", label: "服务承诺" },
  { value: "environment", label: "环保与文明施工" },
  { value: "emergency", label: "应急处置" },
  { value: "other", label: "其他事实" },
];

/** ✅ 2026-09-24：九大章节中文名（建办质〔2018〕31号）—— 与后端 category-map
 * 端点返回一致；前端以此给每条事实展示「章节徽标」，并支持按章节筛选。
 * 仅在 /categories 拉取的本地回退：后端 chapters 数据仍是权威来源。 */
export const FACT_CHAPTER_TITLES: Record<string, string> = {
  overview: "工程概况",
  basis: "编制依据",
  plan: "施工计划",
  technique: "施工工艺技术",
  safety: "施工安全保证措施",
  personnel: "施工管理及作业人员配备和分工",
  acceptance: "验收要求",
  emergency: "应急处置措施",
  calc_drawings: "计算书及相关施工图纸",
};

/** 由后端事实条目的 chapter 键取中文标题；空值/未识别的键返回空串（不渲染徽标） */
export function factChapterTitle(chapter?: string | null): string {
  return (chapter && FACT_CHAPTER_TITLES[chapter]) || "";
}

// ============================================================
// ✅ 全局事实分组列表（可测试组件，2026-09-20）：
//    旧实现把「筛选 + 分组 Collapse + 条目卡片 + 矛盾裁决」全部内联在
//    tabItems JSX 里，无法组件级测试；且分组级「编辑/删除」全断链 ——
//    openFactEdit 死代码、factsApi.delete 无调用点。现抽出组件，
//    数据操作全部回调化（API 调用留在页面层），纯筛选逻辑另导
//    applyFactsFilter 供自动回退 effect / 组件 / 测试三方共用。
// ============================================================

/** 按筛选器返回带 filteredItems 的分组（all 不过滤但同样回填，保证消费口径统一） */
export function applyFactsFilter(groups: any[], filter: string): any[] {
  const out: any[] = [];
  for (const g of groups || []) {
    let items: any[] = g.items || [];
    if (filter === "simulated") items = items.filter((it: any) => it.is_simulated);
    else if (filter === "conflict") items = items.filter((it: any) => it.has_conflict);
    else if (filter === "unresolved") items = items.filter((it: any) => !it.is_resolved);
    if (filter !== "all" && items.length === 0) continue;
    out.push({ ...g, filteredItems: items });
  }
  return out;
}

// ============================================================
// ✅ 全局事实左侧分类面板（2026-09-23）：目录树在事实页不再显示，
//    左栏改为「提取结果分类」，点击分类 → 右侧「项目资料」下方
//    仅展示该分类的分组事实。数据结构与交互对齐 ParseResultCategoryPanel。
// ============================================================

/** 单个分类的聚合条目（左侧面板数据源） */
export type FactsCategoryEntry = {
  category: string;
  title: string;
  groupCount: number;
  itemCount: number;
  simulated: number;
  conflicts: number;
  unresolved: number;
};

/**
 * 把分组列表按 category 聚合为分类清单（纯函数，可单测）。
 *
 * - label 优先取 labelOf(category)（页面注入 factCategoryOptions 的中文名），
 *   取不到回退 category 原文；
 * - 分组顺序保持后端稳定排序（按 category），不做二次重排；
 * - groups 为空/null 时安全返回 []。
 */
export function buildFactsCategoryEntries(
  groups: any[],
  labelOf?: (cat: string) => string,
): FactsCategoryEntry[] {
  const out: FactsCategoryEntry[] = [];
  const byCat = new Map<string, FactsCategoryEntry>();
  for (const g of groups || []) {
    const cat = (g?.category || "other").trim() || "other";
    let e = byCat.get(cat);
    if (!e) {
      e = {
        category: cat,
        title: (labelOf?.(cat) || "").trim() || cat,
        groupCount: 0,
        itemCount: 0,
        simulated: 0,
        conflicts: 0,
        unresolved: 0,
      };
      byCat.set(cat, e);
      out.push(e);
    }
    e.groupCount += 1;
    for (const it of g.items || []) {
      e.itemCount += 1;
      if (it.is_simulated) e.simulated += 1;
      if (it.has_conflict) e.conflicts += 1;
      if (!it.is_resolved) e.unresolved += 1;
    }
  }
  return out;
}

/**
 * 选中分类的悬空回退（纯函数，可单测）：
 * - selected 仍在清单中 → 原样保留；
 * - 悬空（分组被删除/清空/筛选变化）→ 回退第一个分类；
 * - 无任何分类（尚未提取）→ null（右侧显示空态）。
 */
export function resolveSelectedFactCategory(
  entries: FactsCategoryEntry[],
  selected: string,
): string | null {
  if (!entries || entries.length === 0) return null;
  if (selected && entries.some((e) => e.category === selected)) return selected;
  return entries[0].category;
}

/** 取某分类下的分组（保持原顺序；纯函数，可单测） */
export function filterFactsGroupsByCategory(groups: any[], category: string): any[] {
  if (!category) return groups || [];
  return (groups || []).filter(
    (g) => ((g?.category || "other").trim() || "other") === category,
  );
}

/**
 * 左侧「事实分类」面板（与目录树面板同宽同风格，仅全局事实 Tab 显示）。
 * 纯展示组件：选中态与点击全部回调化。
 */
export function FactsCategoryPanel({
  entries,
  totalItems,
  selected,
  onSelect,
}: {
  entries: FactsCategoryEntry[];
  totalItems: number;
  selected: string | null;
  onSelect: (category: string) => void;
}) {
  return (
    <div
      style={{
        width: 300,
        flexShrink: 0,
        background: "#fafafa",
        border: "1px solid #f0f0f0",
        borderRadius: 8,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          padding: "10px 14px",
          background: "#fff",
          borderBottom: "1px solid #f0f0f0",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Space size={6}>
          <AppstoreOutlined style={{ color: "#1677ff" }} />
          <Text strong style={{ fontSize: 13 }}>事实分类</Text>
        </Space>
        {totalItems > 0 && (
          <Tag color="blue" style={{ margin: 0 }}>{totalItems} 项</Tag>
        )}
      </div>
      <div className="scroll-area" style={{ flex: 1, overflow: "auto", padding: "8px 8px", minHeight: 0 }}>
        {entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: "24px 8px", color: "#999", fontSize: 13 }}>
            暂无提取结果
          </div>
        ) : (
          entries.map((e) => {
            const active = e.category === selected;
            return (
              <div
                key={e.category}
                onClick={() => onSelect(e.category)}
                style={{
                  padding: "8px 10px",
                  borderRadius: 6,
                  marginBottom: 4,
                  cursor: "pointer",
                  background: active ? "#e6f4ff" : "transparent",
                  border: active ? "1px solid #91caff" : "1px solid transparent",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{
                    flex: 1, minWidth: 0, fontWeight: active ? 600 : 400,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }} title={e.title}>
                    {e.title}
                  </span>
                  <Tag style={{ margin: 0 }}>{e.itemCount} 项</Tag>
                </div>
                {(e.simulated > 0 || e.conflicts > 0 || e.unresolved > 0) && (
                  <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
                    {e.simulated > 0 && <Tag color="orange" style={{ margin: 0 }}>⚠️{e.simulated}</Tag>}
                    {e.conflicts > 0 && <Tag color="red" style={{ margin: 0 }}>🔸{e.conflicts}</Tag>}
                    {e.unresolved > 0 && <Tag color="blue" style={{ margin: 0 }}>📝{e.unresolved}</Tag>}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/** 事实分组列表：条目级 编辑/确认/矛盾裁决 + 分组级 编辑/删除，全部回调化 */
// ✅ 性能优化：memo 化。本组件是模块级函数组件（非页面内），旧实现无任何记忆化：
// 渲染频率 = 整页渲染频率（正文生成期间 ≤1 次/帧），而每次渲染都要跑
// applyFactsFilter + 每组 3 次 filter 全量扫描 + 为 antd Collapse 重建全部条目。
export const FactsGroupList = memo(function FactsGroupList({
  groups,
  filter,
  onEditGroup,
  onDeleteGroup,
  onEditItem,
  onResolveItem,
  onResolveConflict,
}: {
  groups: any[];
  filter: string;
  onEditGroup: (group: any) => void;
  onDeleteGroup: (group: any) => void;
  onEditItem: (group: any, item: any) => void;
  onResolveItem: (item: any) => void;
  onResolveConflict: (item: any, value: string) => void;
}) {
  // ✅ 性能优化：按输入记忆化过滤结果（原实现每次渲染无条件重算）
  const visible = useMemo(() => applyFactsFilter(groups, filter), [groups, filter]);
  return (
    <Collapse
      items={visible.map((g: any) => {
        const allItems = g.items || [];
        const items = g.filteredItems || allItems;
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
              {items.length > 0 && items.some((it: any) => it.name || it.value) ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {items.map((it: any, idx: number) => (
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
                            {factChapterTitle(it.chapter) && (
                              <Tooltip title={`九大章节归属：${factChapterTitle(it.chapter)}`}>
                                <Tag color="geekblue" style={{ margin: 0 }}>
                                  📖 {factChapterTitle(it.chapter)}
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
                            {it.fact_attr && (
                              <span style={{ fontSize: 11, color: "#888" }}>
                                {it.fact_attr === "quantitative" && "定量"}
                                {it.fact_attr === "qualitative" && "定性"}
                                {it.fact_attr === "relation" && "关系"}
                                {it.fact_attr === "norm" && "规范"}
                              </span>
                            )}
                            {it.is_stale && (
                              <Tooltip title="来源资料已重新解析或删除，本条旧事实已停止注入正文与导出；请重新提取或编辑核对">
                                <Tag color="volcano" style={{ margin: 0 }}>来源已变化</Tag>
                              </Tooltip>
                            )}
                            {it.is_simulated && (
                              <Tooltip title="AI 模拟生成，需人工确认">
                                <Tag color="orange" style={{ margin: 0 }}>
                                  ⚠️ 模拟值
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
                                  {(it.conflict_values || []).map((cv: any, ci: number) => (
                                    <div key={ci}>
                                      {cv.value} ({cv.source}, 置信度 {(cv.confidence * 100).toFixed(0)}%)
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
                            <Tooltip title={`置信度 ${(it.confidence * 100).toFixed(0)}%`}>
                              <div style={{
                                width: 50, height: 6, borderRadius: 3,
                                background: it.confidence >= 0.9 ? "#52c41a" :
                                  it.confidence >= 0.7 ? "#1677ff" :
                                  it.confidence >= 0.4 ? "#fa8c16" : "#ff4d4f",
                                opacity: 0.8,
                              }} />
                            </Tooltip>
                            {/* 来源 */}
                            {it.source && (
                              <span style={{ fontSize: 11, color: "#999" }}>
                                📎 {it.source}
                                {it.source_ref && ` · ${it.source_ref.slice(0, 30)}`}
                              </span>
                            )}
                          </div>

                          {/* 矛盾候选值列表 + 一键选值 */}
                          {it.has_conflict && it.conflict_values?.length > 0 && (
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
                                    onClick={() => onResolveConflict(it, it.value)}
                                  >
                                    保留当前值
                                  </Button>
                                </div>
                                {it.conflict_values.map((cv: any, ci: number) => (
                                  <div key={ci} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                    <Tag color="orange" style={{ margin: 0 }}>{cv.value}</Tag>
                                    <span style={{ fontSize: 11, color: "#999" }}>来自 {cv.source}</span>
                                    <Button
                                      size="small"
                                      type="link"
                                      style={{ padding: 0, height: "auto" }}
                                      onClick={() => onResolveConflict(it, cv.value)}
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
  const msg = hookAntdMessage(_antdMsg, "方案工作台");
  const [scheme, setScheme] = useState<any>(null);
  const [tree, setTree] = useState<TreeNode[]>([]);
  // 长方案分步目录生成时，子目录生成失败的章节标题列表（用于高亮提示）
  const [failedOutlineChapters, setFailedOutlineChapters] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  // 编制要求 / 评审要点（对齐 OpenBidKit score-planning 的「评分大项→目录分支」）：
  // 存于 schemes.config_json.requirements；生成目录时逐条注入（缺失即不合格），
  // 审核阶段做覆盖检查并自动修复补齐
  const [requirements, setRequirements] = useState("");
  // 导入目录时检测到的多标段提示（对齐 OpenBidKit bidSectionDetector）
  const [multiSectionHint, setMultiSectionHint] = useState<any>(null);
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
  const [selectedSection, setSelectedSection] = useState<any>(null);
  const [facts, setFacts] = useState<any[]>([]);
  const [factsSummary, setFactsSummary] = useState<any>(null);
  // ✅ facts 加载已落定（成功或失败都置 true）：起始 Tab 判定以它为「可以决策」
  //    信号 —— 旧实现只看 factsSummary===null，加载失败时永远停在等待态。
  const [factsReady, setFactsReady] = useState(false);
  const [docList, setDocList] = useState<any[]>([]);
  // ===== ✅ 接线 factsApi.create / factsApi.update（手工新增与修改事实）=====
  const [factModalOpen, setFactModalOpen] = useState(false);
  const [factEditingId, setFactEditingId] = useState<string | null>(null);
  const [factEditingCategory, setFactEditingCategory] = useState<string>("");
  const [factSubmitting, setFactSubmitting] = useState(false);
  const [factForm] = Form.useForm();
  // ===== 单条事实编辑（item_updates：改名称/值/分类/模拟值标记，不重建分组）=====
  const [factItemModalOpen, setFactItemModalOpen] = useState(false);
  const [factItemEditing, setFactItemEditing] = useState<any>(null);
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
  const [factsSegmentStats, setFactsSegmentStats] = useState<any>(null);
  // ✅ SSE completed 事件的跨段矛盾清单（旧版前端未消费，2026-09-20 接线）
  const [factsCrossConflicts, setFactsCrossConflicts] = useState<any[]>([]);
  // ✅ 事实分类白名单：初始为本地 FACT_CATEGORY_OPTIONS，挂载后由 GET /global-facts/categories 覆盖
  const [factCategoryOptions, setFactCategoryOptions] = useState<{ value: string; label: string }[]>(FACT_CATEGORY_OPTIONS);
  // ✅ 左侧分类面板（2026-09-23）：全局事实 Tab 选中分类（悬空时由
  //    resolveSelectedFactCategory 回退）+ 项目资料卡片折叠态（默认收起）
  const [selectedFactCategory, setSelectedFactCategory] = useState<string>("");
  const [factsDocsCollapsed, setFactsDocsCollapsed] = useState(true);
  // 九大章节字段完整性与危大阈值诊断（后端确定性计算，零 AI）。
  const [factsChapterReport, setFactsChapterReport] = useState<any>(null);
  const [factsDangerReport, setFactsDangerReport] = useState<any>(null);
  const [factsDiagnosticsLoading, setFactsDiagnosticsLoading] = useState(false);
  // 自然语言调整：先预览操作计划，用户二次确认后才 apply=true。
  const [factsAdjusting, setFactsAdjusting] = useState(false);
  const [factsAdjustPlan, setFactsAdjustPlan] = useState<any>(null);
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
  // ✅ 导出预检独立 loading（旧实现复用 exporting，点预检不显示加载态、还会误亮导出按钮）
  const [checkingExport, setCheckingExport] = useState(false);
  const [exportIssues, setExportIssues] = useState<any[]>([]);
  const [exportStats, setExportStats] = useState<any>(null);
  // ✅ G1：就绪度总检摘要（由导出预检接口一并返回，不再需要来回切换页面）
  const [exportPreflight, setExportPreflight] = useState<any>(null);
  // ✅ 《待补充清单》（2026-09-24）：导出预检返回的占位符聚合（含按字段/按章节）
  const [placeholderReport, setPlaceholderReport] = useState<any>(null);
  // ✅ 重跑计划（治 F 层第 3 条）：哪些章节的占位字段已有补录数据、可自动重跑
  const [rerunPlan, setRerunPlan] = useState<any>(null);
  const [rerunning, setRerunning] = useState(false);
  const [exportPhase, setExportPhase] = useState<string>("");
  // 导出按钮与预检卡片共用同一结论，避免“审核页提示阻断、导出页仍可直接下载”。
  const exportGate = useMemo(() => deriveExportGate({
    hasPreflight: exportStats !== null,
    issues: exportIssues,
    readiness: exportPreflight,
  }), [exportIssues, exportPreflight, exportStats]);
  // ===== ✅ 导出格式预设库（对标 OpenBidKit exportFormatPresets）=====
  const [exportPresets, setExportPresets] = useState<any[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  // 导出域四类请求各自独立取消：预设读取、导出预检、缓存查询、文档导出。
  const presetListAbortRef = useRef<AbortController | null>(null);
  const exportCheckAbortRef = useRef<AbortController | null>(null);
  const cacheAbortRef = useRef<AbortController | null>(null);
  const fetchExportPresets = useCallback(async (sid: string) => {
    presetListAbortRef.current?.abort();
    const controller = new AbortController();
    presetListAbortRef.current = controller;
    try {
      const { data } = await exportApi.presets.list(sid, controller.signal);
      if (
        controller.signal.aborted
        || currentSchemeIdRef.current !== sid
        || presetListAbortRef.current !== controller
      ) return;
      const presets = data.presets || [];
      setExportPresets(presets);
      const defaultPreset = findDefaultExportPreset(presets);
      if (defaultPreset) {
        setSelectedPresetId(defaultPreset.id);
        exportForm.setFieldsValue(defaultPreset.config || {});
      } else {
        setSelectedPresetId("");
      }
    } catch {
      // 非致命：预设缺失不影响导出；切方案产生的取消不提示错误。
    } finally {
      if (presetListAbortRef.current === controller) presetListAbortRef.current = null;
    }
  }, [exportForm]);
  useEffect(() => {
    exportForm.resetFields();
    setExportPresets([]);
    setSelectedPresetId("");
    setExportIssues([]);
    setExportStats(null);
    setExportPreflight(null);
    setPlaceholderReport(null);
    setRerunPlan(null);
    setCacheStatus(null);
    if (id) fetchExportPresets(id);
  }, [id, fetchExportPresets]);
  const handleSavePreset = async () => {
    // ✅ BUG 修复：id 来自 useParams（string | undefined），旧实现未判空，
    //    直接把 undefined 传给 API（类型报错 + 请求 /schemes/undefined/...）
    if (!id) return;
    const sid = id;
    const config = exportForm.getFieldsValue(true);
    const name = (window.prompt("请输入格式预设名称：") || "").trim();
    if (!name) return;
    try {
      await exportApi.presets.create(sid, name, config);
      if (currentSchemeIdRef.current !== sid) return;
      msg.success("已保存为格式预设");
      await fetchExportPresets(sid);
    } catch (e: any) {
      if (currentSchemeIdRef.current === sid) msg.error(e?.message || "保存预设失败");
    }
  };
  const handleSetDefaultPreset = async (pid: string) => {
    if (!id) return;
    const sid = id;
    try {
      await exportApi.presets.setDefault(sid, pid);
      if (currentSchemeIdRef.current !== sid) return;
      msg.success("已设为项目默认预设");
      await fetchExportPresets(sid);
    } catch (e: any) {
      if (currentSchemeIdRef.current === sid) msg.error(e?.message || "操作失败");
    }
  };
  const handleDeletePreset = (pid: string) => {
    modal.confirm({
      title: "确认删除该格式预设？",
      okText: "删除", okButtonProps: { danger: true },
      onOk: async () => {
        if (!id) return;
        const sid = id;
        try {
          await exportApi.presets.remove(sid, pid);
          if (currentSchemeIdRef.current !== sid) return;
          msg.success("已删除预设");
          if (selectedPresetId === pid) setSelectedPresetId("");
          await fetchExportPresets(sid);
        } catch (e: any) {
          if (currentSchemeIdRef.current === sid) msg.error(e?.message || "删除失败");
        }
      },
    });
  };
  // ===== ✅ 接线 sectionsApi.quality（正文质量自检：口语化/AI 腔残留 + 废止标准）=====
  //  后端早有该能力但前端从未调用（孤儿接口），属于"后端算得出、用户看不见"的
  //  信息断链；交付前自检的三个预检项在此补齐最后一块。
  const [checkingQuality, setCheckingQuality] = useState(false);
  const [qualityResult, setQualityResult] = useState<any>(null);
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
  const [autoShrinkOver, setAutoShrinkOver] = useState(false);
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
  const [crConflicts, setCrConflicts] = useState<any[]>([]);
  const [crSummary, setCrSummary] = useState<any>(null);
  const [crScanId, setCrScanId] = useState<string>("");
  const [crRepair, setCrRepair] = useState<any>(null);
  const [crHistory, setCrHistory] = useState<any[]>([]);
  // ===== ✅ 接线 exportApi.cacheStatus（历史导出缓存可见）=====
  const [cacheStatus, setCacheStatus] = useState<{ items: any[]; total: number; stale: number } | null>(null);
  const [cacheLoading, setCacheLoading] = useState(false);
  const [expertItems, setExpertItems] = useState<string[]>([]);
  const [expertResult, setExpertResult] = useState<any>(null);
  // ===== ✅ 接线 complianceApi.check / complianceApi.results（规范符合性检查 + 历史记录）=====
  const [checklistText, setChecklistText] = useState(DEFAULT_COMPLIANCE_CHECKLIST.join("\n"));
  const [checkingCompliance, setCheckingCompliance] = useState(false);
  const [complianceResults, setComplianceResults] = useState<any[]>([]);
  const [complianceHistory, setComplianceHistory] = useState<any[]>([]);
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
  // 正在解析的单份文档 id（列表行内按钮 loading / 全列表禁用依据）
  const [parsingDocId, setParsingDocId] = useState<string | null>(null);
  // 文件分类下拉选项（后端 /global-facts/documents/category-options）
  const [categoryOptions, setCategoryOptions] = useState<string[]>([]);
  // 解析内容预览弹窗
  const [previewDoc, setPreviewDoc] = useState<any>(null);
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
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
  const [baGroups, setBaGroups] = useState<any[]>([]);
  const [baItems, setBaItems] = useState<BaStoredItem[]>([]);
  const [baSummary, setBaSummary] = useState<any>(null);
  const [selectedBaItem, setSelectedBaItem] = useState<any>(null);
  // 2026-09-23 需求拆分：左侧列表 & 右侧详情分别渲染 ParseResultCategoryPanel，
  // 通过共享对象串联展开/激活状态，避免"点击左侧分类，右侧详情不同步"的割裂。
  const sharedParseState = useRef<ParsePanelSharedState>({
    expanded: {},
    activeKey: null,
  }).current;
  const [sectionChecking, setSectionChecking] = useState(false);
  const [sectionCheckResult, setSectionCheckResult] = useState<any>(null);
  // 配置弹窗（mode = key 必选 / full 全部 / custom 自定义勾选）
  const [baConfigOpen, setBaConfigOpen] = useState(false);
  const [baModeOpt, setBaModeOpt] = useState<"key" | "full" | "custom">("key");
  const [baSelectedIds, setBaSelectedIds] = useState<string[]>([]);
  const [baForceRerun, setBaForceRerun] = useState(false);
  // 「查看完整」弹窗 + 本地 SSE 运行态（进度优先取本地事件，轮询作兜底）
  const [baFullItem, setBaFullItem] = useState<any>(null);
  // ✅ 人工校正闭环（2026-09-23 复原）：AI 抽错关键参数时直接改写单项结果（source='manual'）。
  //   后端 PUT/DELETE /results/{item_id} 已实现；组件 BidAnalysisTab 与「解析信息分类
  //   显示栏」均有「人工校正」入口（onEditItem 真值才渲染）。此前页面从未传 onEditItem
  //   → 按钮在生产环境从不渲染，这里补齐 openBaEdit / handleBaSaveEdit / handleBaClearEdit。
  const [baEditItem, setBaEditItem] = useState<any>(null);
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
  const [libItems, setLibItems] = useState<any[]>([]);
  const [libLoading, setLibLoading] = useState(false);
  const [libApplying, setLibApplying] = useState(false);
  const [libKeyword, setLibKeyword] = useState("");
  /** 最近一次「导入目录（智能识别）」的 upload 记录，用于「存为目录库」 */
  const [lastUpload, setLastUpload] = useState<{ id: string; name: string; outline: any[] } | null>(null);
  // ✅ 接线后端 reorganize 能力：导入识别时是否先按标准章节骨架归位
  const [outlineImportReorganize, setOutlineImportReorganize] = useState(false);
  const [saveLibOpen, setSaveLibOpen] = useState(false);
  const [saveLibForm] = Form.useForm();

  const activeTaskIdRef = useRef<string>("");
  // ✅ 生成世代计数：重入生成（如双击）时旧 SSE 协程被 abort 后其收尾代码
  // 会在新任务启动后才执行，若无世代校验，旧协程的 finally 会把新任务的
  // generating 复位为 false → 暂停/停止按钮消失，运行中的任务无法停止。
  const genSeqRef = useRef(0);
  // 生成中的刷新定时器
  const refreshTimerRef = useRef<number | null>(null);
  // ✅ 修复 stale closure：用 ref 保存 selectedSection 最新值，避免 load 回调读到旧值
  const selectedSectionRef = useRef<any>(null);
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
  const currentSchemeIdRef = useRef(id || "");
  currentSchemeIdRef.current = id || "";
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
        const fresh = findSectionById(newTree, currentSelected.key);
        if (fresh) setSelectedSection(fresh);
      }
    } catch (e: any) {
      if (requestedSchemeId === currentSchemeIdRef.current) msg.error(e.message || "加载失败");
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

  const loadFactsDiagnostics = useCallback(async () => {
    if (!id) return;
    const requestedSchemeId = id;
    setFactsDiagnosticsLoading(true);
    try {
      const [chapters, danger] = await Promise.all([
        factsApi.chapters(requestedSchemeId),
        factsApi.dangerCheck({}, requestedSchemeId),
      ]);
      if (requestedSchemeId !== currentSchemeIdRef.current) return;
      setFactsChapterReport(chapters.data);
      setFactsDangerReport(danger.data);
    } catch (e: any) {
      if (requestedSchemeId === currentSchemeIdRef.current) {
        msg.warning(e?.response?.data?.detail || e?.message || "加载事实诊断失败");
      }
    } finally {
      if (requestedSchemeId === currentSchemeIdRef.current) setFactsDiagnosticsLoading(false);
    }
  }, [id, msg]);

  const loadFacts = useCallback(async () => {
    if (!id) return;
    const requestedSchemeId = id;
    factsListAbortRef.current?.abort();
    const ac = new AbortController();
    factsListAbortRef.current = ac;
    try {
      const { data } = await factsApi.list(requestedSchemeId, { signal: ac.signal });
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal)) return;
      setFacts(data.groups || []);
      setFactsSummary(buildFactsSummary(data.stats));
      void loadFactsDiagnostics();
    } catch (e: any) {
      if (e?.name === "AbortError" || ac.signal.aborted) return;
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) return;
      console.warn("加载全局事实失败:", e?.message || e);
      msg.warning(e?.message || "加载全局事实失败，请稍后重试");
    } finally {
      if (isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) {
        setFactsReady(true);
      }
      if (factsListAbortRef.current === ac) factsListAbortRef.current = null;
    }
  }, [id, msg, loadFactsDiagnostics]);

  loadFactsRef.current = loadFacts;

  const handlePreviewFactsAdjust = async () => {
    if (!id || factsAdjusting) return;
    const instruction = (window.prompt("请输入事实调整要求（仅生成预览，不会立即修改）：") || "").trim();
    if (!instruction) return;
    setFactsAdjusting(true);
    try {
      const { data } = await factsApi.adjust({ instruction, scheme_id: id, apply: false });
      if (id !== currentSchemeIdRef.current) return;
      setFactsAdjustPlan({ ...data, instruction });
    } catch (e: any) {
      msg.error(e?.response?.data?.detail || e?.message || "生成调整计划失败");
    } finally {
      setFactsAdjusting(false);
    }
  };

  const applyFactsAdjustPlan = () => {
    if (!id || !factsAdjustPlan?.operations?.length) return;
    modal.confirm({
      title: "确认应用事实调整",
      content: (
        <div>
          <div>{factsAdjustPlan.summary || `将执行 ${factsAdjustPlan.operations.length} 项调整`}</div>
          <ul>{factsAdjustPlan.operations.slice(0, 20).map((op: any, i: number) => (
            <li key={`${op.op}-${op.fact_id || i}`}>{op.op} · {op.name || op.fact_id} {op.value || ""}</li>
          ))}</ul>
        </div>
      ),
      okText: "确认应用", cancelText: "取消", okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await factsApi.adjust({
            instruction: factsAdjustPlan.instruction || factsAdjustPlan.summary || "按已确认计划调整",
            scheme_id: id,
            apply: true,
            // 应用用户刚确认的预览计划，禁止后端再次调用 AI 生成另一份计划。
            operations: factsAdjustPlan.operations,
          });
          setFactsAdjustPlan(null);
          msg.success("事实调整已应用");
          await loadFacts();
        } catch (e: any) {
          msg.error(e?.response?.data?.detail || e?.message || "应用调整失败");
          throw e;
        }
      },
    });
  };

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
    } catch (e: any) {
      if (e?.name === "AbortError" || ac.signal.aborted) return;
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) return;
      msg.warning(e?.message || "加载已上传资料列表失败");
    } finally {
      if (factsDocsListAbortRef.current === ac) factsDocsListAbortRef.current = null;
    }
  }, [id, scheme?.project_id, msg]);

  // =====「提取项目」(bidAnalysis) 数据加载 =====
  /** 解析项定义 + 分组（后端 /bid-analysis/items 为唯一权威源） */
  const loadBaMeta = useCallback(async () => {
    try {
      const { data } = await bidAnalysisApi.items();
      const defs: BaItemDef[] = data.items || [];
      setBaDefs(defs);
      setBaGroups(normalizeBaGroups(data.groups || [], defs));
      setBaDefsError(null);
    } catch (e: any) {
      console.warn("加载解析项定义失败:", e?.message || e);
      // 不再只留 console.warn：旧实现失败后组件永久停在「定义加载中…」，
      // 用户既不知道出错也没有重试入口（2026-09-23 遗留项修复）。
      setBaDefsError(String(e?.response?.data?.detail || e?.message || "网络异常"));
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
    } catch (e: any) {
      console.warn("加载结构化提取结果失败:", e?.message || e);
      setBaError(e?.response?.data?.detail || e?.message || "加载解析结果失败");
    } finally {
      setBaLoading(false);
    }
  }, [id, scheme?.project_id]);

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

  function openFactEdit(group: any) {
    setFactEditingId(group.id);
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
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "保存失败");
    } finally {
      setFactSubmitting(false);
    }
  }

  // ===== ✅ 单条事实编辑（走 PATCH item_updates，不重建整个分组）=====
  function openFactItemEdit(group: any, item: any) {
    setFactItemEditing({ ...item, groupId: group?.id, groupTitle: group?.title });
    factItemForm.setFieldsValue({
      name: item.name,
      value: item.value,
      category: item.category || group?.category || undefined,
      is_simulated: !!item.is_simulated,
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
        }],
      }, id);
      msg.success("事实已更新");
      setFactItemModalOpen(false);
      setFactItemEditing(null);
      factItemForm.resetFields();
      await loadFacts();
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "保存失败");
    } finally {
      setFactItemSubmitting(false);
    }
  }

  // ===== ✅ 断链修复：分组删除 / 单条确认 / 矛盾裁决 / 一键清空 =====
  //    此前 factsApi.delete / clearAll 已封装但前端无任何调用点，
  //    openFactEdit 为死代码；分组级删不掉、也不能一键清空。
  const handleDeleteFactGroup = (group: any) => {
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
          await factsApi.delete(group.id, id);
          msg.success("事实分组已删除");
          await loadFacts();
        } catch (e: any) {
          msg.error(e?.message || "删除失败");
        }
      },
    });
  };

  const handleResolveFactItem = async (item: any) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolve(item.fact_id, id);
      msg.success("已确认");
      // ✅ 等待刷新完成（2026-09-21）：旧实现未 await，用户紧接着点「一键清空」/
      //    切 Tab 时会读到旧 stats，徽标 /「全部就绪」入口短暂错乱。
      await loadFacts();
    } catch (e: any) {
      msg.error(e?.message || "确认失败");
    }
  };

  const handleResolveConflictItem = async (item: any, value: string) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolveConflict(item.fact_id, value, id);
      msg.success("矛盾已裁决");
      await loadFacts();
    } catch (e: any) {
      msg.error(e?.message || "裁决失败");
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
        } catch (e: any) {
          msg.error(e?.message || "清空失败");
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
      setSelectedBaItem((prev: any) => {
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
    setFactsAdjustPlan(null);
    setFactModalOpen(false);
    setFactItemModalOpen(false);
    setSelectedFactCategory("");
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
          (t: any) => t.status === "running" || t.status === "paused"
        );
        // ✅ 跳过 progress 已满的僵尸（后端没来得及标 completed），直接 load 刷新
        const stuckFull = allRunning.find((t: any) => (t.progress || 0) >= 0.95);
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
            if (["completed", "failed", "stopped"].includes(st.status)) {
              resetTakeover();
              const ckpt = (st as any).outline_result;
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
                openOutlineGate(ckpt.outline);
              } else if (st.status === "completed") {
                msg.success("后台任务已完成，正在刷新");
                load();
              } else if (st.status === "failed") {
                msg.error(`后台任务失败：${st.message || ""}`);
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

  function buildTree(nodes: any[]): TreeNode[] {
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
        key: n.id,
        title: n.title,
        level: n.level,
        status: n.status,
        word_count: n.word_count || 0,
        word_budget: n.word_budget || 0,
        content: n.content,
        description: n.description || "",
        outlineId,
        children: n.children ? buildTree(n.children) : [],
      };
    });
  }

  // ✅ 性能优化：onSseEvent 被每条 SSE 事件调用一次，若每次渲染重建会破坏上层
  //    对它的引用稳定性（未来 memo 化子组件依赖此引用时也会失效）。
  //    此函数只依赖 ref 与 msg，可安全 useCallback([])
  const onSseEvent = useCallback((evt: any): boolean => {
    if (evt.task_id) activeTaskIdRef.current = evt.task_id;
    if (evt.event === "error") {
      msg.error(evt.message || "生成失败");
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
      let cfg: any = {};
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
    } catch (e: any) {
      msg.error(e?.message || "保存编制要求失败");
    }
  }

  /** 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
   * 生成完成后不自动入库，由用户勾选确认一级章节后保存并锁定（locked=1） */
  function openOutlineGate(outline: any[]) {
    if (!id) return;
    const roots = (outline || []).filter((n: any) => n && typeof n === "object");
    if (!roots.length) {
      msg.warning("AI 返回空目录，未保存");
      return;
    }
    const selectedRef: { current: number[] } = { current: roots.map((_: any, i: number) => i) };
    modal.confirm({
      title: "🔍 一级目录确认（生成后入库前的最后一道闸门）",
      width: 660,
      content: <OutlineGateList outline={roots} selectedRef={selectedRef} />,
      okText: "确认并保存目录",
      cancelText: "暂不入库（手动编辑）",
      onOk: async () => {
        const filtered = roots.filter((_: any, i: number) => selectedRef.current.includes(i));
        if (!filtered.length) {
          msg.error("至少保留一个一级章节");
          return Promise.reject(new Error("empty"));
        }
        try {
          await sectionsApi.saveOutline(id, {
            outline: filtered,
            source: "ai",
            lock_roots: true,
          });
          msg.success(`目录已确认保存（${filtered.length} 个一级章节已锁定）`);
        } catch (saveErr: any) {
          msg.error(`目录已生成但保存失败：${saveErr?.message || "未知错误"}，请点击「保存目录」重试`);
        }
        load();
      },
      onCancel: () => {
        // 目录已显示在目录树预览中，未入库 —— 用户可手动编辑后点「保存目录」
        msg.info("目录未入库：可在目录树中手动调整后点击「保存目录」");
      },
    });
  }

  const doGenerateOutline = async () => {
    if (!id) return;
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
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
      for await (const evt of sseFetch(`/sse/generate-outline/${id}`, undefined, { signal: ac.signal })) {
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
                  await sectionsApi.saveOutline(id, { outline: evt.outline, source: "ai" });
                  msg.success(`已保存 ${partialCount} 个章节节点`);
                  load();
                } catch (saveErr: any) {
                  msg.error(saveErr?.message || "保存失败");
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
            // ✅ 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
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
                  await sectionsApi.saveOutline(id, { outline: evt.outline, source: "ai" });
                  msg.success(`已保存 ${partialCount} 个章节的目录`);
                  load();
                } catch (saveErr: any) {
                  msg.error(saveErr?.message || "保存失败");
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
    } catch (e: any) {
      if (e.name !== "AbortError") {
        msg.warning(e.message || "SSE 连接中断");
        // ✅ BUG 修复（2026-09-23 · 「30s 连接超时后任务孤儿运行」）：
        //    建连阶段（首个事件到达前）超时/失败时 taskId 尚未产生，旧实现
        //    直接跳过重挂 —— 但后端任务可能已注册并继续生成（AI 照常计费），
        //    成果只躺在 checkpoint。此处按方案检索最近仍在运行的
        //    outline_generation 任务重挂（检索不到才维持旧行为：仅提示）。
        let attachTaskId = taskId;
        if (!attachTaskId) {
          attachTaskId =
            (await findRecentOutlineTaskImpl(async () =>
              (await tasksApi.list(id)).data,
            )) || "";
        }
        if (attachTaskId) {
          setProgressMsg("连接中断，正在重新挂接后台任务...");
          const fin: any = await pollTaskUntilTerminal(attachTaskId);
          if (fin?.status === "completed") {
            // ✅ 成果恢复：后台完成的目录只存在于任务 checkpoint 中，
            // 仅 load() 刷不到任何东西（目录需用户闸门确认才入库）。
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              setTree(buildTree(ckpt.outline));
              if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                setFailedOutlineChapters(ckpt.failed_chapters);
                msg.warning(`后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`);
              } else {
                msg.success("目录生成已在后台完成，请确认保存");
              }
              openOutlineGate(ckpt.outline);
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
              setTree(buildTree(failCkpt.outline));
              const partialCount = countOutline(failCkpt.outline);
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
                    await sectionsApi.saveOutline(id, { outline: failCkpt.outline, source: "ai" });
                    msg.success(`已保存 ${partialCount} 个章节节点`);
                    load();
                  } catch (saveErr: any) {
                    msg.error(saveErr?.message || "保存失败");
                  }
                },
              });
            }
          } else if (fin?.status === "stopped") {
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              setTree(buildTree(ckpt.outline));
              const partialCount = countOutline(ckpt.outline);
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
                    await sectionsApi.saveOutline(id, { outline: ckpt.outline, source: "ai" });
                    msg.success(`已保存 ${partialCount} 个章节的目录`);
                    load();
                  } catch (saveErr: any) {
                    msg.error(saveErr?.message || "保存失败");
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

  /** 计算实际传递给后端的字数预算覆盖值（未选则不覆盖） */
  function resolveWordBudgetOverride(): number | undefined {
    if (wordBudgetOption === "default") return undefined;
    if (wordBudgetOption === "custom") return customWordBudget;
    return parseInt(wordBudgetOption, 10);
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
      const budget = wbOverride || l.word_budget || 1500;
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
      const { data } = await sectionsApi.update(id, selectedSection.key, {
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
        word_count: data.word_count || content.length,
        status: content.trim() ? "generated" : "empty",
      };
      setSelectedSection(updatedSection);
      // 同步更新 tree 中对应节点
      const updateTreeNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === selectedSection.key
            ? { ...n, content: content, word_count: data.word_count || content.length,
                status: content.trim() ? "generated" : "empty" }
            : { ...n, children: n.children ? updateTreeNode(n.children) : undefined }
        );
      setTree((prev) => updateTreeNode(prev));
    } catch (e: any) {
      msg.error(e.message || "保存失败");
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
    const sectionKey = selectedSection.key;
    try {
      await sectionsApi.update(id, sectionKey, { generation_standard: v });
      const patchNode = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) =>
          n.key === sectionKey
            ? { ...n, generation_standard: v }
            : { ...n, children: n.children ? patchNode(n.children) : undefined }
        );
      setTree((prevTree) => patchNode(prevTree));
      setSelectedSection((s: TreeNode | null) => (s && s.key === sectionKey ? { ...s, generation_standard: v } : s));
      msg.success(v ? `本章生成标准已设为「${STANDARD_LABELS[v as "precise" | "fuzzy"]}」` : "本章已改为沿用方案默认");
    } catch (e: any) {
      // 受控 Select 未改 state → 自动回滚到旧值
      msg.error(e?.message || "生成标准保存失败");
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
      const { data } = await schemesApi.update(scheme.project_id, id, {
        generation_standard: std,
      });
      setScheme((s: any) => (s ? { ...s, generation_standard: data?.generation_standard || std } : s));
      msg.success(`已存为方案默认：${STANDARD_LABELS[std as "precise" | "fuzzy"]}`);
    } catch (e: any) {
      msg.error(e?.message || "保存方案默认失败");
    } finally {
      setSavingStandardDefault(false);
    }
  }

  /** 重写当前章节（带二次确认） */
  function regenerateCurrentSection() {
    if (!selectedSection) return;
    const hasContent = selectedSection.word_count > 0 || selectedSection.content;
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
    const budget = selectedSection.word_budget || 1500;
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
          const { data } = await sectionsApi.shrink(id, selectedSection.key);
          // 就地更新选中章节与树（避免整页 load 闪烁）
          setSelectedSection(
            (prev: TreeNode | null) =>
              prev && prev.key === selectedSection.key
                ? { ...prev, content: data.content, word_count: data.word_count, word_status: data.word_status }
                : prev
          );
          setTree((prev) =>
            updateNodeFields(prev, selectedSection.key, {
              content: data.content,
              word_count: data.word_count,
              word_status: data.word_status,
            })
          );
          msg.success(
            `压缩完成：${data.before} → ${data.word_count} 字（${data.rounds_used} 轮，${data.stop_reason}）`
          );
        } catch (e: any) {
          msg.error(e?.message || "压缩失败");
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
        } catch (e: any) {
          msg.error(e?.message || "重置失败");
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
   * ✅ 二次增强：合并后端 completed 事件下发的失败章节明细（failed_sections）。
   *   失败章节可能压根没进日志（异常路径 / 保留旧正文未改状态），
   *   只有后端知道"是哪几章、为什么失败"，这里补进日志供用户定位与重试。
   */
  const mergeFailedSections = (list: any) => {
    const next = mergeFailedSectionsInto(sectionLogsRef.current, list);
    if (next === sectionLogsRef.current) return; // 无失败明细时纯函数原样返回 → 跳过 setState
    sectionLogsRef.current = next;
    setSectionLogs([...next]);
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
    const body: any = {};
    const wbOverride = resolveWordBudgetOverride();
    if (wbOverride) body.word_budget_override = wbOverride;
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
      for await (const evt of sseFetch(`/sse/generate-content/${id}`, body, { signal: ac.signal })) {
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
            // ✅ 缺口修复（2026-09-24）：后端 section_done 一并下发本章程序化
            //    质检问题（quality_issues），此前前端完全丢弃 —— 只有重新拉整篇
            //    正文才可能发现。现落到日志项，日志区直接提示。
            quality_issues: Array.isArray(evt.quality_issues) ? evt.quality_issues : undefined,
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
            setSelectedSection((prev: TreeNode | null) => prev ? {
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

        if (evt.event === "completed") {
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
          if (failedCount > 0) {
            msg.warning(doneMsg + `（${failedCount} 章失败）`);
            setProgressMsg(doneMsg + `（${failedCount} 章失败）`);
          } else {
            msg.success(doneMsg);
            setProgressMsg(doneMsg);
          }
          // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：生成标准校验汇总提示
          //    （仅在「有问题章数 > 0」时追加，无问题不制造噪音）
          const _stdHint = standardSummaryHint(evt.standard_summary);
          if (_stdHint) { msg.warning(_stdHint); setProgressMsg(`${doneMsg}　|　${_stdHint}`); }
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
            batcher.schedule("progressMsg", () => setProgressMsg(evt.message));
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
    } catch (e: any) {
      if (e.name !== "AbortError") {
        msg.warning(e.message || "SSE 连接中断");
        // ✅ 二次增强：连接中断时先收尾「进行中」日志项（后台任务可能仍在跑，
        //    但本页已无法再收到 section_stage —— 残留转圈会误导用户）。
        finalizeRunningLogs("连接中断，等待后台任务收尾", "skipped");
        if (taskId) {
          setProgressMsg("连接中断，正在重新挂接后台任务...");
          const fin = await pollTaskUntilTerminal(taskId);
          // ✅ G12-6（2026-09-20）：消费断线重挂回传的 content_result 失败明细。
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
      for await (const evt of sseFetch(`/sse/generate-facts/${id}`, { missing_value_mode: missingValueMode }, { signal: ac.signal })) {
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
        }
      }
    } catch (e: any) {
      if (e.name !== "AbortError") {
        msg.warning(e.message || "SSE 连接中断");
        if (taskId) {
          setProgressMsg("连接中断，正在重新挂接后台任务...");
          const fin = await pollTaskUntilTerminal(taskId);
          if (fin?.status === "completed") {
            msg.success("全局事实提取已在后台完成");
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
    const parsedCount = docList.filter((d: any) => isDocParsed(d)).length;
    if (parsedCount === 0) {
      const failed = docList.filter((d: any) => isDocFailed(d)).length;
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

  // ✅ 分步工作流 ①：上传保存（仅存文件，不解析不提取）
  const handleUploadDocuments = async (files: File[]) => {
    if (!id || files.length === 0) return;
    if (uploadingFacts) return;
    uploadAbortRef.current?.abort();
    const ac = new AbortController();
    uploadAbortRef.current = ac;

    setUploadingFacts(true);
    setUploadedFiles(files.map((f) => f.name));
    try {
      const { data } = await factsApi.uploadDocuments(files, id, { signal: ac.signal });
      if (ac.signal.aborted) return;
      const savedCount = data.saved_count ?? 0;
      // ✅ 统一口径（2026-09-21）：提示文案唯一入口收敛到
      //    workflowDerived.buildUploadNotice（六类拒绝原因全覆盖：
      //    unsupported/oversize/empty/signature_invalid/too_many/quota_exceeded）。
      //    旧实现在此手写拼装，漏掉累计体积超限（quota_exceeded/quota_files）——
      //    「已保存 3 个文件，其余为什么没了」在界面上完全无解释。
      const notice = buildUploadNotice(data);
      if (savedCount === 0 && notice.details.length > 0) {
        console.warn("上传被拒明细:", notice.details);
      }
      if (notice.tone === "error") msg.error(notice.text);
      else msg.success(notice.text);
      loadDocuments();
    } catch (e: any) {
      if (e?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(e.message || "上传保存失败");
    } finally {
      if (uploadAbortRef.current === ac) {
        uploadAbortRef.current = null;
        setUploadingFacts(false);
        setUploadedFiles([]);
      }
    }
  };

  // ✅ 分步工作流 ②：批量解析所有待解析文档
  //    force=true 时为「全部重解析」：连同已解析文档一起重新解析，用于
  //    补齐旧版截断内容 / 启用 OCR 后重扫此前失败的扫描件。
  const handleParseDocuments = async (force = false) => {
    if (!id) return;
    // ✅ 防重入：单份解析（OCR 可能数分钟）进行中时不得叠加批量解析，
    //    否则同一文档被两路并发解析，四层产物与版本列互相覆盖。
    //    （组件侧已禁用入口，此处是页面层防御，避免其他调用点绕过）
    if (parsingDocs || parsingDocId) return;
    // ✅ 2026-09-24 B4：与后端 parse-all 选取口径（parsed_markdown 为空，含 failed）
    //    及 UploadParseTab 按钮统一到 computeDocStats.actionableCount；
    //    旧实现用 !text_len 自行计数，与组件按钮的 pendingCount（排除 failed）口径不一致。
    const docStats = computeDocStats(docList);
    if (!force && docStats.actionableCount === 0) {
      // ✅ BUG 修复（2026-09-25，前端早退阻断后端自修复）：actionableCount 为 0
      // 只说明"没有待解析/失败的文档"，**不等于状态列都正确**——存量文档可能
      // parse_status 为 null/pending 而正文早已存在，此时统计条与列表都把它显示
      // 成"已解析"、按钮本应禁用，但后端 parse_status 列永远停在错误值。
      // 旧实现在此直接 return，后端的 parse_status 自修复永远不可达。
      // 现仅当全部状态都确认为 success 时才早退，否则放行请求触发后端修正。
      const hasStaleStatus = docList.some((d) => d.parse_status !== "success");
      if (!hasStaleStatus) {
        msg.info("所有文档均已解析");
        return;
      }
    }
    if (force && docList.length === 0) {
      msg.info("还没有上传资料文件");
      return;
    }
    setParsingDocs(true);
    // ✅ 与上传/SSE 同口径：解析挂 AbortController，卸载/切方案时中断
    parseAbortRef.current?.abort();
    const ac = new AbortController();
    parseAbortRef.current = ac;
    try {
      const { data } = await factsApi.parseAllDocuments(
        id, scheme?.project_id, force, { signal: ac.signal });
      if (ac.signal.aborted) return;
      const truncatedCount = data.truncated_count || 0;
      const reconciledCount = data.reconciled_count || 0;
      if (data.failed_count > 0) {
        const details = (data.failed || [])
          .map((f: any) => `${f.file_name}：${f.reason}`)
          .join("；");
        msg.warning(`解析完成：成功 ${data.parsed} 个，失败 ${data.failed_count} 个。${details}`);
      } else if (truncatedCount > 0) {
        const names = (data.truncated || []).map((f: any) => f.file_name).join("、");
        msg.warning(
          `解析完成：成功 ${data.parsed} 个文档；其中 ${truncatedCount} 个内容超长被截断（${names}），建议拆分后重新上传`);
      } else if (data.parsed === 0 && reconciledCount > 0) {
        // ✅ 2026-09-25：本轮只修正了陈旧的 parse_status（存量文档正文早已存在，
        // 但状态列停在 pending/null）→ 给用户明确解释，否则"什么都没发生"。
        msg.success(`已修正 ${reconciledCount} 份文档的解析状态`);
      } else {
        msg.success(
          `${force ? "重新" : ""}解析完成：成功 ${data.parsed} 个文档`
          + (reconciledCount > 0 ? `，并修正 ${reconciledCount} 份陈旧状态` : ""));
      }
      loadDocuments();
    } catch (e: any) {
      if (e?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(e.message || "解析失败");
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocs(false);
      }
    }
  };

  // ✅ 分步工作流 ②：解析单份文档（列表行内按钮）
  // force=true 时强制重解析（用于补齐早期被截断的文档内容）
  // ✅ 单文档解析防重入：parsingDocId 非空时忽略新的单份解析请求
  const handleParseDocument = async (docId: string, fileName: string, force = false) => {
    if (parsingDocId || parsingDocs) return;
    setParsingDocId(docId);
    parseAbortRef.current?.abort();
    const ac = new AbortController();
    parseAbortRef.current = ac;
    try {
      const { data } = await factsApi.parseDocument(docId, force, { signal: ac.signal });
      if (ac.signal.aborted) return;
      if (data.already_parsed) {
        // ✅ 2026-09-25：后端在短路返回时自修复了陈旧 parse_status
        //    （正文已存在但状态仍是 pending）→ 明确告知，避免用户误以为
        //    「点了解析却没反应」。
        msg.info(data.reconciled
          ? `「${fileName}」已有解析结果，已修正其解析状态`
          : `「${fileName}」已解析过`);
      } else {
        msg.success(`「${fileName}」${force ? "重新" : ""}解析完成（${data.text_len} 字）`);
      }
      loadDocuments();
    } catch (e: any) {
      if (e?.name === "AbortError" || ac.signal.aborted) return;
      msg.error(e.message || "解析失败");
      // ✅ 失败/超时后补一次列表刷新：后端可能已落库，避免 UI 与后端不同步
      loadDocuments();
    } finally {
      if (parseAbortRef.current === ac) {
        parseAbortRef.current = null;
        setParsingDocId(null);
      }
    }
  };

  /** 查看解析内容预览（前 N 字，不下载整份文件） */
  const handlePreviewDocument = async (doc: any) => {
    if (!doc?.id) return;
    setPreviewDoc(doc);
    setPreviewText("");
    setPreviewLoading(true);
    // 重置四层质量概览，随后异步拉取（不阻断正文预览）
    setPipelineStatus(null);
    setPipelineError(null);
    setPipelineLoading(true);
    try {
      const { data } = await factsApi.previewDocument(doc.id);
      if (!data.is_parsed) {
        msg.warning(data.message || "该文档尚未解析");
      }
      setPreviewText(data.preview || "");
    } catch (e: any) {
      msg.error(e.message || "获取预览失败");
    } finally {
      setPreviewLoading(false);
    }
    loadPipelineStatus(doc.id);
  };

  /** 拉取四层解析质量概览（status + completeness 合并；失败非阻断，仅降级为告警） */
  const loadPipelineStatus = async (docId: string) => {
    if (!docId) return;
    setPipelineLoading(true);
    setPipelineError(null);
    try {
      const { data } = await docPipelineApi.status(docId);
      // status 已含 completeness 快照；quality_score 缺省时再取一次报告补全
      setPipelineStatus(data);
    } catch (e: any) {
      setPipelineError(e?.message || "解析质量信息读取失败");
    } finally {
      setPipelineLoading(false);
    }
  };

  /** 刷新完整性校验并回填质量分 */
  const refreshPipelineQuality = async (docId: string) => {
    try {
      const { data } = await docPipelineApi.completeness(docId, true);
      const q = typeof data?.quality_score === "number" ? data.quality_score : undefined;
      setPipelineStatus((prev) => (prev ? { ...prev, quality_score: q, completeness: data?.completeness ?? prev.completeness } : prev));
    } catch {
      /* 质量刷新失败不额外打扰（正文/状态仍在） */
    }
  };

  /** 预览弹窗内的四层管线动作：调用对应 API → 反馈 → 重新拉取状态 */
  const handlePipelineAction = async (action: PipelineActionKey) => {
    const docId = previewDoc?.id;
    if (!docId || pipelineAction) return;
    setPipelineAction(action);
    try {
      if (action === "reparse") {
        const { data } = await docPipelineApi.reparse(docId, "force");
        msg.success(data?.skipped ? "文件未变更，无需重解析" : "已重新解析并刷新四层产物");
      } else if (action === "syncExtractions") {
        const { data } = await docPipelineApi.syncExtractions(docId);
        msg.success(`提取层已物化（${(data?.written_types || []).length} 类）`);
      } else if (action === "crossCheck") {
        const { data } = await docPipelineApi.crossCheck(docId);
        msg.success(`交叉校验完成（冲突 ${data?.stored_conflicts ?? 0}）`);
      } else if (action === "refreshQuality") {
        await refreshPipelineQuality(docId);
        msg.success("质量分已刷新");
      }
      // 动作可能改变四层产物 → 重新拉取权威状态
      if (action !== "refreshQuality") await loadPipelineStatus(docId);
    } catch (e: any) {
      msg.error(e?.message || "操作失败");
    } finally {
      setPipelineAction(null);
    }
  };


  /** 修改文档分类（招标文件 / 合同文件 / ...，影响提取优先级） */
  const handleCategoryChange = async (docId: string, category: string) => {
    try {
      await factsApi.updateDocumentCategory(docId, category);
      setDocList((prev: any[]) =>
        prev.map((d) => (d.id === docId ? { ...d, doc_category: category } : d)));
      msg.success("分类已更新");
    } catch (e: any) {
      // ✅ 2026-09-24 T3：更新失败（超时/网络中断但服务端可能已落库等）时，
      //    重新拉取服务端真值，避免受控 Select 与后端分类发生漂移。
      msg.error(e.message || "分类更新失败");
      loadDocuments();
    }
  };

  // ============================================================
  // 「提取项目」(bidAnalysis) 处理器
  // ============================================================

  /**
   * 启动/单项重跑结构化提取（SSE）。
   * 事件契约（backend routers/bid_analysis.py）：
   *   item_update {item_id,status,progress,content?,error?} / heartbeat / completed / error / cancelled
   */
  const runBaSse = useCallback(async (opts: {
    mode: "key" | "full" | "custom" | "item";
    selectedItemIds?: string[];
    forceRerun?: boolean;
  }) => {
    if (!id || baSseAbortRef.current) return;
    const ac = new AbortController();
    baSseAbortRef.current = ac;
    setBaRunning(true);
    setBaProgress(0);
    setBaProgressMsg("正在连接 AI...");
    // 清空上一轮提取规模（避免「本轮尚未切段却显示上轮调用数」的误导）
    setBaTextStats(null);
    try {
      const { path, params } = bidAnalysisApi.startSse(id, scheme?.project_id, opts);
      for await (const evt of sseGetStream(path, { params, signal: ac.signal })) {
        if (ac.signal.aborted) break;
        const t = evt?.type;
        if (t === "item_update") {
          setBaItems((prev) => {
            const idx = prev.findIndex((i) => i.item_id === evt.item_id);
            const patch: BaStoredItem = { item_id: evt.item_id, status: evt.status };
            if (evt.content) patch.content = evt.content;
            if (evt.error) patch.error = evt.error;
            if (idx >= 0) {
              const cp = [...prev];
              cp[idx] = { ...cp[idx], ...patch };
              return cp;
            }
            return [...prev, patch];
          });
          if (typeof evt.progress === "number") setBaProgress(evt.progress);
        } else if (t === "heartbeat") {
          if (typeof evt.progress === "number") setBaProgress(evt.progress);
          if (evt.message) setBaProgressMsg(evt.message);
        } else if (t === "text_stats") {
          // 提取规模（切段后立即推送）：超长文档的真实消耗对用户必须可见
          setBaTextStats({
            total_chars: evt.total_chars ?? 0,
            segment_count: evt.segment_count ?? 0,
            item_count: evt.item_count ?? 0,
            est_model_calls: evt.est_model_calls ?? 0,
            chunk_size: evt.chunk_size ?? 0,
          });
        } else if (t === "completed") {
          const r = evt.result || {};
          if (r.ok) {
            msg.success(`结构化提取完成（${r.completed || 0}/${r.total || 0} 项）`);
            // ✅ 自动跳目录生成收紧为「key/full 且必选项齐备」（停止/报错/单项重跑不跳）
            if (opts.mode === "key" || opts.mode === "full") autoJumpFrom("import" as WorkflowTabKey, "outline");
          } else {
            const missing = (r.missing_required || []).join("、");
            msg.warning(`提取完成，但必选项未全部完成${missing ? `：${missing}` : ""}`);
          }
        } else if (t === "error") {
          msg.error(evt.error || "结构化提取失败");
        } else if (t === "cancelled") {
          msg.info(evt.message || "任务已取消");
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError" && !ac.signal.aborted) {
        msg.error(e.message || "结构化提取连接中断");
      }
    } finally {
      if (baSseAbortRef.current === ac) baSseAbortRef.current = null;
      setBaRunning(false);
      // 收尾以服务端权威结果为准（item_update 只是乐观视图）
      loadBaResults();
    }
  }, [id, scheme?.project_id, loadBaResults, msg]);

  /** 停止：四类任务共用 task_registry 控制链路（POST /sse/task/{id}/control） */
  const handleStopBa = useCallback(async () => {
    if (!id) return;
    try {
      const { data } = await tasksApi.list(id, 10);
      const t = (data.tasks || []).find(
        (x: any) => x.task_type === "bid_analysis"
          && (x.status === "running" || x.status === "paused"));
      if (!t) {
        msg.info("没有正在运行的结构化提取任务");
        return;
      }
      await tasksApi.control(t.id, "stop");
      msg.success("已发送停止指令，在途解析项将在收口后落终态");
    } catch (e: any) {
      msg.error(e.message || "停止失败");
    }
  }, [id, msg]);

  /** 多标段检测（规则检测，非 AI，秒级） */
  const handleCheckSections = useCallback(async () => {
    if (!id || sectionChecking) return;
    setSectionChecking(true);
    try {
      const { data } = await bidAnalysisApi.checkSections(id, scheme?.project_id);
      setSectionCheckResult(data);
    } catch (e: any) {
      msg.error(e.message || "多标段检测失败");
    } finally {
      setSectionChecking(false);
    }
  }, [id, scheme?.project_id, sectionChecking, msg]);

  /** 单项重跑：mode=item 严格按勾选、不补全必选项、forceRerun=false（直接覆盖该项） */
  const handleRerunItem = useCallback((def: BaItemDef) => {
    runBaSse({ mode: "item", selectedItemIds: [def.item_id], forceRerun: false });
  }, [runBaSse]);

  // ===== ✅ 人工校正闭环（2026-09-23 复原，供「提取项目」与「解析信息分类显示栏」共用）=====
  /** 打开校正弹窗（载入当前项内容，json/markdown 均可编辑） */
  const openBaEdit = useCallback((item: any) => {
    if (!item) return;
    setBaEditItem(item);
    setBaEditValue(item?.content || "");
  }, []);

  /** 保存校正（PUT /results/{item_id}，后端返回权威 item + summary）
   *  AI 抽错关键参数时唯一可靠出口；校正值标记 source='manual'，下游优先使用。 */
  const handleBaSaveEdit = useCallback(async () => {
    const item = baEditItem;
    if (!item || !id) return;
    const content = (baEditValue ?? "").trim();
    if (!content) {
      msg.warning("内容不能为空（如需清空请点「撤销校正」）");
      return;
    }
    // ✅ 前端预校验 json 项（与后端 422 对齐，避免一次往返）
    if (item.output_type === "json") {
      try {
        const parsed = JSON.parse(content);
        if (typeof parsed === "string") {
          msg.error("项目级基本信息为 JSON 对象/数组，不能是裸字符串");
          return;
        }
      } catch {
        msg.error("项目级基本信息为 JSON 结构，请提交合法 JSON");
        return;
      }
    }
    setBaEditSaving(true);
    try {
      const { data } = await bidAnalysisApi.updateResult(
        item.item_id, id, content, scheme?.project_id);
      msg.success("已保存人工校正结果（优先于 AI 输出，下游目录/正文将使用该校正值）");
      // 以后端权威返回更新（item + summary），避免乐观更新与 DB 漂移
      if (data?.item) {
        const updated = { ...item, ...data.item };
        setBaItems((prev: BaStoredItem[]) => {
          const idx = prev.findIndex((i) => i.item_id === item.item_id);
          if (idx < 0) return [...prev, updated];
          const cp = [...prev]; cp[idx] = { ...cp[idx], ...updated }; return cp;
        });
        setSelectedBaItem((prev: any) =>
          prev && prev.item_id === item.item_id ? { ...prev, ...updated } : prev);
      }
      if (data?.summary) setBaSummary(data.summary);
      else setBaSummary(recomputeBaSummary(baItems, baDefsRef.current));
      setBaEditItem(null);
    } catch (e: any) {
      msg.error(e?.response?.data?.detail || e.message || "保存失败");
    } finally {
      setBaEditSaving(false);
    }
  }, [baEditItem, baEditValue, id, scheme?.project_id, baItems, msg]);

  /** 撤销校正（DELETE /results/{item_id}，回到 idle + source='ai'）
   *  不删行本身 —— 行是「解析项槽位」，删掉会破坏 sort_order 展示顺序与 upsert 语义。 */
  const handleBaClearEdit = useCallback(async () => {
    const item = baEditItem;
    if (!item || !id) return;
    setBaEditSaving(true);
    try {
      const { data } = await bidAnalysisApi.clearResult(
        item.item_id, id, scheme?.project_id);
      msg.success("已清空该项结果（回到待提取状态）");
      if (data?.item) {
        const updated = { ...item, ...data.item };
        setBaItems((prev: BaStoredItem[]) => {
          const idx = prev.findIndex((i) => i.item_id === item.item_id);
          if (idx < 0) return [...prev, updated];
          const cp = [...prev]; cp[idx] = { ...cp[idx], ...updated }; return cp;
        });
        setSelectedBaItem((prev: any) =>
          prev && prev.item_id === item.item_id ? { ...prev, ...updated } : prev);
      }
      if (data?.summary) setBaSummary(data.summary);
      else setBaSummary(recomputeBaSummary(baItems, baDefsRef.current));
      setBaEditItem(null);
    } catch (e: any) {
      msg.error(e?.response?.data?.detail || e.message || "清空失败");
    } finally {
      setBaEditSaving(false);
    }
  }, [baEditItem, id, scheme?.project_id, baItems, msg]);

  /** 配置弹窗确认 → 按模式启动 */
  const handleStartBaFromConfig = useCallback(() => {
    const selected = baModeOpt === "custom" ? baSelectedIds : undefined;
    if (baModeOpt === "custom" && (!selected || selected.length === 0)) {
      msg.warning("自定义模式至少勾选一个解析项");
      return;
    }
    setBaConfigOpen(false);
    runBaSse({
      mode: baModeOpt,
      selectedItemIds: selected,
      forceRerun: baForceRerun,
    });
  }, [baModeOpt, baSelectedIds, baForceRerun, runBaSse, msg]);

  /** 批量确认所有未确认的事实 */
  const handleBatchResolve = async () => {
    if (!id) return;
    const unresolved = factsSummary?.unresolved || 0;
    const simulated = factsSummary?.simulated || 0;
    if (unresolved === 0) {
      msg.info("没有待确认的事实");
      return;
    }
    // ✅ 文案统一走 buildBatchResolveCopy：修复旧文案把「模拟值 ∩ 未确认」
    //    交集重复计数（unresolved + simulated），且空态判定误伤已确认的模拟值。
    const copy = buildBatchResolveCopy({
      unresolved, simulated, conflicts: factsSummary?.conflicts || 0,
    });
    modal.confirm({
      title: "批量确认所有事实",
      content: (
        <div>
          <div>{copy.headline}</div>
          {copy.simulatedNote && (
            <div style={{ marginTop: 6 }}>{copy.simulatedNote}</div>
          )}
          {copy.conflictNote && (
            <div style={{ marginTop: 6, color: "#d46b08" }}>{copy.conflictNote}</div>
          )}
        </div>
      ),
      okText: "全部确认",
      cancelText: "取消",
      type: copy.confirmType,
      onOk: async () => {
        try {
          const { data } = await factsApi.batchResolve(id);
          const copy = buildBatchResolveResultCopy(data);
          if (copy.tone === "warning") msg.warning(copy.text);
          else msg.success(copy.text);
          await loadFacts();
        } catch (e: any) {
          msg.error(e.message || "确认失败");
        }
      },
    });
  };

  /** 删除已上传的项目资料文档 */
  const handleDeleteDocument = async (docId: string, fileName: string) => {
    modal.confirm({
      title: "确认删除文件",
      content: `删除后，从「${fileName}」中提取的全局事实不会自动清除，但后续 AI 生成将不再引用该文件。确定删除？`,
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await factsApi.deleteDocument(docId);
          msg.success("文件已删除");
          loadDocuments();
        } catch (e: any) {
          msg.error(e.message || "删除失败");
        }
      },
    });
  };

  // ============================================================
  // 目录生成 Tab 的辅助函数
  // （treeToOutline / renumberOutline / outlineToTreeNode 已提升至
  //   模块级导出，行为由 outlineTab / outlineTreeLogic 测试钉住）
  // ============================================================

  /** 目录树：在指定节点下新增一个子节点 */
  const addChildNode = useCallback((parentKey?: string) => {
    const curTree = treeRef.current;
    const curExpanded = expandedKeysRef.current;
    const newId = `local_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newLevel = parentKey
      ? (findSectionById(curTree, parentKey)?.level || 0) + 1
      : 1;
    // ✅ BUG 修复：目录系统硬性限三级（后端 MAX_OUTLINE_DEPTH=3），在三级章节下
    // 添加的四级节点保存时会被静默裁剪并入父章节描述——用户以为加成功了，
    // 保存后节点"消失"。这里在添加入口直接拦截并说明。
    if (newLevel > MAX_OUTLINE_DEPTH) {
      msg.warning(`目录最多支持 ${MAX_OUTLINE_DEPTH} 级，无法在三级章节下继续添加子章节`);
      return;
    }
    const newNode: TreeNode = {
      key: newId,
      title: "新章节",
      level: newLevel,
      status: "empty",
      word_count: 0,
      word_budget: 1500,
      children: [],
    };
    if (!parentKey) {
      setTree([...curTree, newNode]);
    } else {
      setTree(curTree.map((n) => insertIntoNode(n, parentKey, newNode)));
    }
    // 自动选中新节点
    setSelectedSection(newNode);
    // 自动展开父节点
    if (parentKey && !curExpanded.includes(parentKey)) {
      setExpandedKeys([...curExpanded, parentKey]);
    }
  }, [msg, id]);

  /** 目录树：在指定节点同级（其后）新增一个节点；未指定 key 时追加为根级章节 */
  const addSiblingNode = useCallback((key?: string) => {
    const curTree = treeRef.current;
    const curExpanded = expandedKeysRef.current;
    if (!key) {
      addChildNode(); // 无参照节点：复用"追加到根"
      return;
    }
    const ref = findSectionById(curTree, key);
    if (!ref) return;
    const newId = `local_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newLevel = ref.level || 1;
    const newNode: TreeNode = {
      key: newId,
      title: "新章节",
      level: newLevel,
      status: "empty",
      word_count: 0,
      word_budget: 1500,
      children: [],
    };
    // 插入到参照节点之后（同父层级），保留其余结构
    const insertAfter = (nodes: TreeNode[]): TreeNode[] => {
      const out: TreeNode[] = [];
      for (const n of nodes) {
        out.push(n);
        if (n.key === key) {
          out.push(newNode);
        } else if (n.children && n.children.length) {
          out[out.length - 1] = { ...n, children: insertAfter(n.children) };
        }
      }
      return out;
    };
    setTree(insertAfter(curTree));
    setSelectedSection(newNode);
    // 自动展开父节点（确保新同级节点可见）
    const parentKey = findParentKey(curTree, key);
    if (parentKey && !curExpanded.includes(parentKey)) {
      setExpandedKeys([...curExpanded, parentKey]);
    }
  }, [addChildNode, id]);

  /** 查找某节点的父节点 key（用于新增同级后确保父级展开） */
  function findParentKey(nodes: TreeNode[], key: string, parent = ""): string {
    for (const n of nodes) {
      if (n.key === key) return parent;
      if (n.children && n.children.length) {
        const r = findParentKey(n.children, key, n.key);
        if (r !== "") return r;
      }
    }
    return "";
  }

  function insertIntoNode(node: TreeNode, parentKey: string, newNode: TreeNode): TreeNode {
    if (node.key === parentKey) {
      return { ...node, children: [...(node.children || []), newNode] };
    }
    if (node.children && node.children.length) {
      return {
        ...node,
        children: node.children.map((c) => insertIntoNode(c, parentKey, newNode)),
      };
    }
    return node;
  }

  /** 目录树：删除一个节点 */
  const deleteNode = useCallback((key: string) => {
    const target = findSectionById(treeRef.current, key);
    if (!target) return;
    // 确认删除
    modal.confirm({
      title: "确认删除章节",
      content: `「${target.title}」及其所有子章节将被删除，确定继续？`,
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const cur = findSectionById(treeRef.current, key);
          // ✅ BUG 修复：判据改用 isUnsavedLocalKey（覆盖 local_ + upload_）。
          // 旧实现只认 local_，「导入目录」替换后的 upload_ 节点会被当成已落库
          // 章节，误调 DELETE /sections/{upload_...} → 后端 404，异常抛出后
          // setTree(removeNode(...)) 执行不到 → **本地节点永远删不掉**。
          if (cur && !isUnsavedLocalKey(cur.key) && id) {
            await sectionsApi.delete(id, cur.key);
          }
          setTree(removeNode(treeRef.current, key));
          setSelectedSection(null);
          msg.success("已删除");
        } catch (e: any) {
          msg.error(e.message || "删除失败");
        }
      },
    });
  }, [modal, id, msg]);

  function removeNode(nodes: TreeNode[], key: string): TreeNode[] {
    return nodes
      .filter((n) => n.key !== key)
      .map((n) => ({
        ...n,
        children: n.children ? removeNode(n.children, key) : [],
      }));
  }

  /** 目录树：重命名 */
  const renameNode = useCallback((key: string, newTitle: string) => {
    const trimmed = newTitle.trim();
    if (!trimmed) {
      msg.warning("章节标题不能为空");
      return;
    }
    const node = findSectionById(treeRef.current, key);
    // ✅ 等值短路：用户把旧标题清空/回车即触发（失焦/回车两条路径都调本函数），
    //    标题未变就直接退出，既避免冗余本地重渲，也不再 PATCH /sections/{id}
    //    发送「未改即提交」的空变更。node.title 是后端原始文本（buildTree 直取
    //    n.title，formatOutlineTitle 仅用于渲染展示），这里 .trim() 对齐用户输入。
    if (node && node.title.trim() === trimmed) return;
    setTree(treeRef.current.map((n) => renameInNode(n, key, trimmed)));
    // ✅ 判据改用 isUnsavedLocalKey：upload_（导入识别）节点同样尚未落库，
    // 误调 PATCH /sections/{upload_...} 会 404。
    // ✅ BUG 修复（B12 真正落地）：旧实现 `.catch(() => {})` 完全吞掉写库失败，
    // 用户看到本地标题已改就以为已保存；实际服务端仍是旧标题 —— 若此后不点
    // 「保存目录」直接生成正文，正文里会出现旧章节名。现失败即明示并给出补救动作。
    if (node && !isUnsavedLocalKey(key) && id) {
      sectionsApi
        .update(id, key, { title: trimmed })
        .catch((err: any) => {
          msg.warning(
            `本地已改名为「${trimmed}」，但服务端保存失败（${err?.message || "未知错误"}）——` +
              `请点击「保存目录」持久化，否则刷新后仍是旧标题`
          );
          flashReorderHint("⚠️ 改名未同步到服务端，请点击「保存目录」持久化");
        });
    }
  }, [msg, id]);

  function renameInNode(node: TreeNode, key: string, title: string): TreeNode {
    if (node.key === key) return { ...node, title };
    if (node.children) {
      return {
        ...node,
        children: node.children.map((c) => renameInNode(c, key, title)),
      };
    }
    return node;
  }

  // （flattenTreeKeys / moveSiblingInTree 已提升至模块级导出，供测试与页面共用）

  /**
   * 目录树：上移/下移章节（dir=-1 上 / +1 下）。
   * 本地树立即交换顺序给出视觉反馈；若当前没有「未保存的本地新增节点(local_)」，
   * 则立即调用 /reorder 持久化（重排 sort_order 并回写章节编号），随后刷新树，
   * 保证正文生成读取的「当前章节编号」与展示一致。
   * 若存在未保存新增节点，则仅本地交换、依赖「保存目录」统一落库（与拖拽一致，
   * 避免 /reorder 只重排 DB 节点而丢失未落库的新增章节）。
   */
  const moveNode = useCallback((key: string, dir: -1 | 1) => {
    const newTree = moveSiblingInTree(treeRef.current, key, dir);
    if (!newTree) return;
    const hasUnsavedLocal = hasUnsavedLocalNodes(treeRef.current);
    // ✅ 编辑增强：移动后滚动定位到目标节点（展开状态天然保留——load 不清空
    //    expandedKeys，且被移动节点原本可见、其祖先必然已展开）。
    const scrollToTarget = () => {
      // ✅ 编辑增强：编辑树与左侧导航树同步滚动定位到目标节点
      scrollToNodeInContainer(editTreeScrollRef.current, key);
      scrollToNodeInContainer(leftTreeScrollRef.current, key);
    };
    if (!hasUnsavedLocal && id) {
      // 无未保存新增节点：立即持久化（/reorder 重排 + 回写编号），load 后编号自然正确
      const order = flattenTreeKeys(newTree);
      setTree(renumberTreeLocally(newTree)); // 即时刷新编号展示
      flashReorderHint("章节已上/下移，编号已实时更新（已同步到服务端）");
      sectionsApi
        .reorder(id, { order })
        .then(() => load().then(() => setTimeout(scrollToTarget, 60)))
        .catch(() => {
          // ✅ 后端 /reorder 失败降级为"仅本地调整"：当前章节顺序只在前端可见，
          // 刷新后会回到 DB 原有顺序；明确告知用户需要手动点「保存目录」才能持久化。
          msg.warning("移动已在本地生效，服务端同步失败，请点击「保存目录」完成持久化");
          flashReorderHint("⚠️ 服务端同步失败，本地顺序尚未持久化，请点击「保存目录」");
        });
    } else {
      // 存在未保存新增节点：仅本地交换 + 本地重算编号，依赖「保存目录」统一落库
      setTree(renumberTreeLocally(newTree));
      flashReorderHint();
      setTimeout(scrollToTarget, 60);
    }
    // ✅ 依赖说明：flashReorderHint 为函数声明（每渲染新建但仅用 setState+ref，
    //    捕获旧引用语义等价），故不列入依赖，以保证本回调引用稳定。
  }, [msg, id, load, scrollToNodeInContainer]);

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
      // ✅ G10：目录结构变更会改变预检的章节集合 → 刷新审核 / 就绪度工作台
      setReviewTick((t) => t + 1);
      await load();
    } catch (e: any) {
      msg.error(e.message || "保存目录失败");
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
    setSelectedSection((prev: TreeNode | null) =>
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
      .catch((err: any) => {
        msg.warning(
          `本地已把字数预算改为 ${budget} 字，但服务端保存失败（${err?.message || "未知错误"}）——` +
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
    } catch (e: any) {
      msg.error(e.message || "导出目录树失败");
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
    } catch (e: any) {
      setLibError(e.message || "加载目录库失败");
    } finally {
      setLibLoading(false);
    }
  }

  /** 套用某个目录库到当前方案：apply 取 outline_json → 写入 sections */
  function handleApplyLibrary(lib: any) {
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
          const { data } = await outlineLibraryApi.applyAndSave(lib.id, { scheme_id: id });
          msg.success(`已套用「${lib.name}」，共 ${data.count || 0} 个章节`);
          setLibOpen(false);
          setSelectedSection(null);
          await load();
        } catch (e: any) {
          msg.error(e.message || "套用目录库失败");
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
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.message || "存入目录库失败");
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
          await sectionsApi.saveOutline(id, { outline: [], source: "手动清除" });
          msg.success("所有目录已清除");
          setTree([]);
          setSelectedSection(null);
          setExpandedKeys([]);
          await load();
        } catch (e: any) {
          msg.error(e.message || "清除失败");
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
      const { data } = await uploadOutlineApi.parse(file, {
        scheme_name: scheme?.name || "",
        reorganize: outlineImportReorganize,
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
    } catch (e: any) {
      msg.error(e.message || "文件识别失败");
    } finally {
      hide();
    }
  };

  // （outlineToTreeNode 已提升至模块级导出，行为由 outlineTab 测试钉住）

  // （renumberTreeLocally 已提升至模块级导出）

  /** 目录树拖拽排序 */
  function onOutlineTreeDrop(info: any) {
    const dropKey = info.node.key;
    const dragKey = info.dragNode.key;
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
  const [renamingKey, setRenamingKey] = useState<string | null>(null);
  const [renamingValue, setRenamingValue] = useState<string>("");

  // ✅ 编辑增强：拖拽/上移下移后实时高亮"编号已更新"提示
  const [reorderHint, setReorderHint] = useState<string | null>(null);
  const reorderHintTimer = useRef<number | null>(null);
  // ✅ 编辑增强：编辑树 / 左侧导航树滚动容器 ref，用于移动后滚动定位到目标节点
  const editTreeScrollRef = useRef<HTMLDivElement>(null);
  const leftTreeScrollRef = useRef<HTMLDivElement>(null);
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
  const convertToEditTree = useCallback((node: TreeNode): any => {
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

  const handleControl = async (action: "pause" | "resume" | "stop") => {
    const tid = activeTaskIdRef.current;
    if (!tid) {
      msg.warning("没有活跃的任务");
      return;
    }
    try {
      const { data } = await tasksApi.control(tid, action);
      if (data.ok) {
        // ✅ F3 暂停状态可见：控制成功后同步本地 paused 态（按钮互斥 + Tag）
        if (action === "pause") { setTaskPaused(true); msg.success("已暂停"); }
        else if (action === "resume") { setTaskPaused(false); msg.success("已恢复"); }
        else { setTaskPaused(false); msg.success("已停止"); }
      }
    } catch (e: any) {
      msg.error(e.message || "控制失败");
    }
  };

  const handleExpertReview = async () => {
    if (!id) return;
    try {
      const { data } = await complianceApi.expertReview({ scheme_id: id });
      setExpertResult(data);
      msg.success("预检完成");
    } catch (e: any) {
      msg.error(e.message || "预检失败");
    }
  };

  /** ✅ 接线 exportApi.cacheStatus：查询仍有效的导出缓存（后端已过滤僵尸行） */
  const loadCacheStatus = async () => {
    if (!id) return;
    const sid = id;
    cacheAbortRef.current?.abort();
    const controller = new AbortController();
    cacheAbortRef.current = controller;
    setCacheLoading(true);
    try {
      const { data } = await exportApi.cacheStatus(sid, controller.signal);
      if (
        controller.signal.aborted
        || currentSchemeIdRef.current !== sid
        || cacheAbortRef.current !== controller
      ) return;
      setCacheStatus(data);
    } catch (e: any) {
      const canceled = e?.name === "AbortError" || e?.code === "ERR_CANCELED";
      if (!canceled && currentSchemeIdRef.current === sid) {
        msg.error(e.message || "读取导出缓存状态失败");
      }
    } finally {
      if (cacheAbortRef.current === controller) {
        cacheAbortRef.current = null;
        setCacheLoading(false);
      }
    }
  };

  // ================== ✅ 规范符合性检查（complianceApi.check）==================
  /** 把多行文本切成检查清单并提交 AI 逐条判定 */
  const handleComplianceCheck = async () => {
    if (!id) return;
    const checklist = checklistText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (checklist.length === 0) {
      msg.warning("请至少输入一条检查项");
      return;
    }
    setCheckingCompliance(true);
    try {
      // ✅ 清单未被手工改动时携带 rule_ids 提交 —— 让落库的 rule_id 指向规则注册表，
      //    历史结果才能跨版本比对并归并进六维评分（否则 AI 自造的 "R1" 无语义）
      const useRules = !checklistDirty && aiRules.length > 0
        && checklist.length === aiRules.length;
      const payload = useRules
        ? { scheme_id: id, rule_ids: aiRules.map((r) => r.rule_id) }
        : { scheme_id: id, checklist };
      const { data } = await complianceApi.check(payload);
      const results = data.results || [];
      setComplianceResults(results);
      const miss = results.filter((r: any) => r.hit === false).length;
      const high = results.filter((r: any) => r.severity === "high").length;
      msg.success(`检查完成：${results.length} 项，疑似缺失 ${miss} 项，高风险 ${high} 项`);
      // 结果已落库，刷新历史记录
      loadComplianceHistory();
    } catch (e: any) {
      msg.error(e.message || "合规检查失败（请确认已配置可用的 AI 供应商）");
    } finally {
      setCheckingCompliance(false);
    }
  };

  // ===== ✅ 全文一致性审计（§3.12.1）：事实 vs 正文比对，0-100 评分 + 不一致项 =====
  const [auditRunning, setAuditRunning] = useState(false);
  const [consistencyResult, setConsistencyResult] = useState<{ score: number; issues: any[]; created_at?: string } | null>(null);

  const handleConsistencyAudit = async () => {
    if (!id) return;
    setAuditRunning(true);
    setConsistencyResult(null);
    try {
      const { data } = await complianceApi.consistencyAudit(id);
      setConsistencyResult({ score: data.score, issues: data.issues || [] });
      const high = (data.issues || []).filter((i: any) => i.severity === "high").length;
      msg.success(`一致性审计完成：评分 ${data.score}，不一致项 ${data.issues?.length || 0}（高危 ${high}）`);
    } catch (e: any) {
      msg.error(e.message || "一致性审计失败");
    } finally {
      setAuditRunning(false);
    }
  };

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

  // ===== ✅ 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR）=====
  const loadCrConflicts = async (scanId?: string) => {
    if (!id) return;
    try {
      const { data } = await consistencyRepairApi.conflicts(id, scanId);
      if (data?.exists) {
        setCrConflicts(data.conflicts || []);
        setCrSummary(data.summary || null);
        setCrScanId(data.scan_id || scanId || "");
      } else {
        setCrConflicts([]);
        setCrSummary(null);
      }
    } catch (e: any) {
      msg.warning(e.message || "加载冲突清单失败");
    }
  };

  const loadCrHistory = async () => {
    if (!id) return;
    try {
      const { data } = await consistencyRepairApi.repairs(id, 10);
      setCrHistory(data.items || []);
    } catch {
      // 历史修复记录加载失败不阻塞工作台
    }
  };

  const openConsistencyWorkbench = () => {
    setCrOpen(true);
    setCrLoading(true);
    Promise.all([loadCrConflicts(), loadCrHistory()]).finally(() => setCrLoading(false));
  };

  const handleCrScan = async () => {
    if (!id || crScanning) return;
    setCrScanning(true);
    setCrRepair(null);
    try {
      const { data } = await consistencyRepairApi.scan(id, {
        scope: "full",
        include_global_facts: true,
        include_project_docs: true,
        include_design_docs: true,
      });
      setCrConflicts(data.conflicts || []);
      setCrSummary(data.summary || null);
      setCrScanId(data.scan_id || "");
      const s = data.summary || {};
      msg.success(`扫描完成：发现 ${s.total ?? 0} 处冲突（高 ${s.high ?? 0} / 中 ${s.medium ?? 0} / 低 ${s.low ?? 0}）`);
    } catch (e: any) {
      msg.error(e.message || "全文一致性扫描失败");
    } finally {
      setCrScanning(false);
    }
  };

  const handleCrRepair = async (opts?: { conflictIds?: string[]; severity?: string }) => {
    if (!id || crRepairing) return;
    setCrRepairing(true);
    try {
      const { data } = await consistencyRepairApi.repair(id, {
        scan_id: crScanId || undefined,
        mode: "auto",
        severity_threshold: opts?.severity || consistencySeverity,
        conflict_ids: opts?.conflictIds,
        // ✅ 强制全量重修：true 时不跳过「已修复且修复成果仍在正文里」的冲突
        force_full_repair: forceFullRepair,
      });
      setCrRepair(data);
      await loadCrConflicts();
      await loadCrHistory();
      // 若当前选中章节被修复，同步预览正文
      const curKey = selectedSectionRef.current?.key;
      if (curKey) {
        const hit = (data.items || []).find(
          (x: any) => x.section_id === curKey && x.status === "repaired" && x.after);
        if (hit?.after) {
          setSelectedSection((prev: any) => (prev && prev.key === curKey
            ? { ...prev, content: hit.after, word_count: hit.after.length, status: "generated" }
            : prev));
        }
      }
      load();
      msg.success(`修复完成：成功 ${data.repaired ?? 0} 处，失败 ${data.failed ?? 0} 处，跳过 ${data.skipped ?? 0} 处`);
      // ✅ G11（2026-09-21）：修复改写了正文 → 审核结论与总检结论都已失效。
      // 旧实现只报"修复完成"，不提示重跑总检：用户以为分数没变即可交付，
      // 实际按旧结论放行会把新写进去的问题一起带出。
      if ((data.repaired ?? 0) > 0) {
        setReviewTick((t) => t + 1);
        msg.warning("正文已被修复改写，审核结论已失效；请重新送审并重新执行一键总检");
      }
    } catch (e: any) {
      msg.error(e.message || "全文一致性修复失败");
    } finally {
      setCrRepairing(false);
    }
  };

  const crRepairItemIds = (status: string) =>
    (crRepair?.items || [])
      .filter((i: any) => i.status === status && i.conflict_id)
      .map((i: any) => i.conflict_id as string);

  const handleCrConfirm = async (accepted: string[], rejected: string[]) => {
    if (!id || !crRepair?.repair_id) return;
    try {
      const { data } = await consistencyRepairApi.confirm(id, {
        repair_id: crRepair.repair_id, accepted, rejected,
      });
      msg.success(`已确认：接受 ${accepted.length} 条，拒绝 ${rejected.length} 条`);
      await loadCrConflicts();
      await loadCrHistory();
      load();
      setCrRepair((prev: any) => (prev ? { ...prev, status: data.status } : prev));
      // ✅ G11：拒绝回滚会把章节正文恢复为修复前版本 → 审核结论失效
      if (accepted.length + rejected.length > 0) {
        setReviewTick((t) => t + 1);
        if (rejected.length > 0) {
          msg.warning("已拒绝部分修复并恢复原文，相关章节审核结论已失效，需重新送审并重新总检");
        }
      }
    } catch (e: any) {
      msg.error(e.message || "确认修复结果失败");
    }
  };

  const handleCrRollback = (repair: any) => {
    if (!id) return;
    modal.confirm({
      title: "回滚本次修复？",
      content: `将把本次修复涉及的章节恢复为修复前正文（快照 ${repair.snapshot_id || "—"}）。该操作会生成一个可再次撤销的快照。`,
      okText: "确认回滚",
      cancelText: "取消",
      onOk: async () => {
        try {
          await consistencyRepairApi.rollback(id, { repair_id: repair.id });
          msg.success("已回滚到修复前版本");
          // ✅ G11：回滚改写了正文 → 审核结论失效，需要重新送审 + 重跑总检
          setReviewTick((t) => t + 1);
          msg.warning("正文已回滚到修复前版本，相关章节审核结论已失效，需重新送审并重新总检");
          await loadCrConflicts();
          await loadCrHistory();
          load();
        } catch (e: any) {
          msg.error(e.message || "回滚失败");
        }
      },
    });
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
    if (!exportGate.allowed) {
      msg.warning(exportGate.reason);
      return;
    }
    const sid = id;
    const controller = new AbortController();
    const exportSeq = ++exportSeqRef.current;
    exportAbortRef.current?.abort();
    exportAbortRef.current = controller;
    exportingRef.current = true;
    exportPctRef.current = -1;
    setExporting(true);
    setExportPhase("正在渲染图表...");
    try {
      const config = await exportForm.validateFields();
      // ✅ 统一渲染轨：用与预览一致的 mermaid.js 把图表渲染成 PNG 随导出提交，
      // 后端优先采用（未命中/失败的图表由后端渲染轨兜底），导出与预览所见即所得
      let chartImages: ExportChartImage[] = [];
      try {
        const { data } = await chartsApi.list(sid, controller.signal);
        if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
        const items: ExportChartItem[] = selectPlacedExportCharts(data.items || [])
          .map((p: any) => ({ chart_type: p.chart_type, mermaid_code: p.code }));
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
            exportBatcherRef.current?.schedule("phase", () => setExportPhase(text));
          }
        }, controller.signal);
        if (chartImages.length > 0) {
          msg.info(`已按预览样式渲染 ${chartImages.length}/${items.length} 张图表`, 3);
        }
      } catch (e) {
        if (controller.signal.aborted) throw e;
        chartImages = []; // 前端渲染失败不阻塞导出，图表走后端渲染轨兜底
      }
      // ✅ 收尾前把图表渲染进度的最后一帧刷下去，保证用户看到最终进度文本
      exportBatcherRef.current?.flushNow();
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
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
      //    后端回传的 X-Export-Filename（百分号编码，这里解码还原）。
      const nameRaw = (resp.headers?.["x-export-filename"]
        || resp.headers?.["X-Export-Filename"]) as string | undefined;
      let downloadName = `${scheme?.name || "方案"}.${format}`;
      if (nameRaw) {
        try {
          downloadName = decodeURIComponent(nameRaw);
        } catch { /* 解码异常时沿用兜底名，不阻断下载 */ }
      }
      a.download = downloadName;
      // ✅ 修复：① Firefox 要求 <a> 挂在文档中才会触发下载；
      //          ② click() 后同步 revokeObjectURL 会撤销尚未开始的下载
      //            （Safari / Firefox 上表现为"点了没反应"），改为延迟回收。
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      msg.success(format === "pdf" ? "PDF 导出成功" : "导出成功");
      if (currentSchemeIdRef.current === sid) await loadCacheStatus();
      if (
        controller.signal.aborted
        || exportSeqRef.current !== exportSeq
        || currentSchemeIdRef.current !== sid
      ) return;
      // 展示图表渲染轨统计（前端 mermaid.js / 后端 mermaid 原生 / 失败）
      const statsRaw = (resp.headers?.["x-chart-render-stats"] || resp.headers?.["X-Chart-Render-Stats"]) as string | undefined;
      if (statsRaw) {
        try {
          const stats = JSON.parse(statsRaw) as { fe?: number; backend_ok?: number; failed?: number };
          const parts: string[] = [];
          if (stats.fe) parts.push(`前端图 ${stats.fe} 张`);
          if (stats.backend_ok) parts.push(`后端原生 ${stats.backend_ok} 张`);
          if (stats.failed) parts.push(`失败 ${stats.failed} 张`);
          if (parts.length) msg.info(`图表渲染：${parts.join(" / ")}`, 5);
          if (stats.failed && stats.failed > 0) {
            msg.warning(`${stats.failed} 张图表渲染失败，建议检查图表代码或开启「允许 PIL 兜底」后重试`);
          }
        } catch { /* 统计头解析失败不影响导出结果展示 */ }
      }
      // 展示公式/乱码自动修复统计（导出时后端已内建修复：公式转 Word 数学排版、清理乱码符号）
      const fixRaw = (resp.headers?.["x-fix-stats"] || resp.headers?.["X-Fix-Stats"]) as string | undefined;
      if (fixRaw) {
        try {
          const fs = JSON.parse(fixRaw) as {
            formulas?: number; block_formulas?: number; replacement_chars?: number;
            control_chars?: number; gbk_mojibake?: number; latin1_mojibake?: number; cyrillic?: number;
          };
          const parts: string[] = [];
          if (fs.formulas) parts.push(`公式 ${fs.formulas} 处`);
          if (fs.replacement_chars) parts.push(`替换字符 ${fs.replacement_chars} 处`);
          if (fs.control_chars) parts.push(`控制字符 ${fs.control_chars} 处`);
          if (fs.gbk_mojibake) parts.push(`GBK 乱码 ${fs.gbk_mojibake} 处`);
          if (fs.latin1_mojibake) parts.push(`编码乱码 ${fs.latin1_mojibake} 处`);
          if (fs.cyrillic) parts.push(`西里尔误植 ${fs.cyrillic} 处`);
          if (parts.length) msg.success(`导出前已自动修复：${parts.join(" / ")}`, 6);
        } catch { /* 统计头解析失败不影响导出结果展示 */ }
      }
    } catch (e: any) {
      const canceled = e?.name === "AbortError" || e?.code === "ERR_CANCELED";
      if (!canceled && exportSeqRef.current === exportSeq && currentSchemeIdRef.current === sid) {
        msg.error(e.message || "导出失败");
      }
    } finally {
      if (exportSeqRef.current === exportSeq && exportAbortRef.current === controller) {
        exportAbortRef.current = null;
        exportingRef.current = false;
        setExporting(false);
        setExportPhase("");
      }
    }
  };

  const handleExportCheck = async () => {
    if (!id) return;
    const sid = id;
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
      const ps: any = data.preflight_summary || null;
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
    } catch (e: any) {
      const canceled = e?.name === "AbortError" || e?.code === "ERR_CANCELED";
      if (!canceled && currentSchemeIdRef.current === sid) msg.error(e.message || "预检失败");
    } finally {
      if (exportCheckAbortRef.current === controller) {
        exportCheckAbortRef.current = null;
        setCheckingExport(false);
      }
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
    if (!id) return;
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
    } catch (e: any) {
      msg.error(e.message || "质量自检失败");
    } finally {
      setCheckingQuality(false);
    }
  };

  // ✅ 性能优化：与 convertToEditTree 同款 useCallback 化，并修正下方 treeData 的
  //    陈旧闭包问题 —— 旧 deps 只有 [tree]，但闭包内读了 failedOutlineChapters，
  //    导致「失败章高亮」在只改 failedOutlineChapters 时不刷新（当前被「setTree 与
  //    setFailedOutlineChapters 总同批次提交」掩盖，新增单点改动路径就会暴露）。
  const convertToAntTree = useCallback((node: TreeNode, depth = 0): any => {
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
    } catch (e: any) {
      msg.error(e?.message || "重跑中断，可稍后重新预检查看进度");
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
  const sectionChartFixed = useCallback((newContent: string) => {
    const sec = selectedSectionRef.current;
    if (!sec) return;
    const wc = newContent.length;
    const patchNode = (nodes: TreeNode[]): TreeNode[] =>
      nodes.map((n) =>
        n.key === sec.key
          ? { ...n, content: newContent, word_count: wc }
          : { ...n, children: n.children ? patchNode(n.children) : undefined }
      );
    setSelectedSection((prev: any) =>
      prev && prev.key === sec.key ? { ...prev, content: newContent, word_count: wc } : prev
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
                <Text strong>{importSubTab === "docs" ? "解析信息分类" : "18 项结构化提取"}</Text>
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
                        docs={docList}
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
              message={`检测到疑似多标段招标文件（约 ${multiSectionHint.total_declared || multiSectionHint.detected_count} 个标段）`}
              description="当前导入的招标文件可能包含多个标段 / 标包。建议按标段拆分后分别生成方案，避免目录与正文互相混淆。"
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
                if (file.size > 30 * 1024 * 1024) {
                  msg.error("文件过大，请上传 30MB 以内的文件");
                  return false;
                }
                handleUploadParseOutline(file);
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
                selected={selectedSection}
                moveFlags={moveFlags}
                showNext={tree.length > 0}
                onAddChild={() => selectedSection && addChildNode(selectedSection.key)}
                onMove={(dir) => selectedSection && moveNode(selectedSection.key, dir)}
                onDelete={() => selectedSection && deleteNode(selectedSection.key)}
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
                node={selectedSection}
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
                  selectedKeys={selectedSection ? [selectedSection.key] : []}
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
            ? `待确认模拟值 ${factsSummary.simulated || 0} 项 · 待审核 ${factsSummary.unresolved || 0} 项 · 矛盾 ${factsSummary.conflicts || 0} 项`
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
                  {factsSummary.by_category.map((c: any) => (
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
              {factsSummary.simulated_ratio > 0.3 && (
                <Alert
                  style={{ marginTop: 8 }}
                  type="warning"
                  showIcon
                  message={`模拟值占比 ${(factsSummary.simulated_ratio * 100).toFixed(0)}% 超过 30% 阈值，建议补充更多项目资料以减少 AI 模拟`}
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
                      {factsCrossConflicts.slice(0, 8).map((cf: any, idx: number) => {
                        const sevColor = cf.severity === "high" ? "red" : cf.severity === "medium" ? "orange" : "blue";
                        return (
                          <span key={cf.rule_id || idx} style={{ fontSize: 12 }}>
                            <Tag color={sevColor}>{cf.severity || "low"}</Tag>
                            <Text strong>{cf.side_a?.name}</Text>：
                            <Text delete type="danger">{cf.side_a?.value}</Text>
                            {" ↔ "}
                            <Text type="warning">{cf.side_b?.value}</Text>
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
                  const files = fileList.map((f) => (f as any).originFileObj || f) as File[];
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
            <Button
              icon={<ThunderboltOutlined />}
              onClick={handlePreviewFactsAdjust}
              loading={factsAdjusting}
              disabled={generating || uploadingFacts || parsingDocs || (factsSummary?.total || 0) === 0}
            >
              AI 调整事实
            </Button>
            {factsAdjustPlan?.operations?.length > 0 && (
              <Button danger onClick={applyFactsAdjustPlan}>应用调整计划</Button>
            )}
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
            />
          )}

          <FactsDiagnosticsPanel
            loading={factsDiagnosticsLoading}
            chapterReport={factsChapterReport}
            dangerReport={factsDangerReport}
            onRefresh={loadFactsDiagnostics}
          />

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
                已收起 {docList.length} 个文件（含 {docList.filter((d: any) => isDocParsed(d)).length} 个已解析），点击标题展开查看
              </div>
            ) : (
              <List
                size="small"
                dataSource={docList}
                renderItem={(doc: any) => {
                  const ftypeIcon: Record<string, string> = {
                    docx: "📄", pdf: "📑", md: "📝", txt: "📃",
                    xlsx: "📊", xls: "📈", csv: "📉",
                    png: "🖼️", jpg: "🖼️", jpeg: "🖼️", bmp: "🖼️", tiff: "🖼️",
                  };
                  const icon = ftypeIcon[doc.file_type] || "📎";
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
                            onClick={() => handleParseDocument(doc.id, doc.file_name)}
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
                              onClick={() => handleParseDocument(doc.id, doc.file_name, true)}
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
                          onClick={() => handleDeleteDocument(doc.id, doc.file_name)}
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
                          }} title={doc.file_name}>
                            {doc.file_name}
                          </div>
                          <div style={{ fontSize: 12, color: "#999" }}>
                            <Tag color="default" style={{ margin: "0 4px 0 0" }}>
                              {doc.file_type?.toUpperCase() || "未知格式"}
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
                            {doc.created_at}
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
                  {activeFactGroups.length} 组 / {activeFactGroups.reduce((n: number, g: any) => n + (g.items?.length || 0), 0)} 项
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
              />
            </>
          ) : facts.length > 0 ? (
            <FactsGroupList
              groups={facts}
              filter={factsFilter}
              onEditGroup={openFactEdit}
              onDeleteGroup={handleDeleteFactGroup}
              onEditItem={openFactItemEdit}
              onResolveItem={handleResolveFactItem}
              onResolveConflict={handleResolveConflictItem}
            />
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
          shrinking={shrinking}
          hasTree={tree.length > 0}
          hasSelectedSection={!!selectedSection}
          canShrink={!!selectedSection && !!selectedSection.content &&
            (selectedSection.word_count || 0) > (selectedSection.word_budget || 1500)}
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
            section={selectedSection}
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
          <ReadinessDashboard schemeId={id || ""} refreshKey={reviewTick} />

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
                rowKey={(r: any, i) => `${r.rule_id || "r"}-${i}`}
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
                    render: (hit: any) => (
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
              <Button icon={<AuditOutlined />} onClick={handleExpertReview}>
                开始预检
              </Button>
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
                  <Tag color={expertResult.score >= 80 ? "green" : expertResult.score >= 60 ? "orange" : "red"}>
                    {expertResult.score}
                  </Tag>
                </Descriptions.Item>
                {expertResult.missing?.length > 0 && (
                  <Descriptions.Item label="缺失项">
                    {expertResult.missing.join("、")}
                  </Descriptions.Item>
                )}
                {expertResult.suggestions?.length > 0 && (
                  <Descriptions.Item label="补充建议">
                    {expertResult.suggestions.join("；")}
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
                        {EXPORT_ISSUE_LABEL[iss.type] || iss.type}：
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
                  <Tag color="orange">{qualityResult.summary.problem_sections} 个章节待修</Tag>
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
                rowKey={(r: any) => r.section_id}
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
          chart_fail_placeholder: false,
          auto_rewrite_content: false,
          auto_fix_unclosed_fences: false,
          heading_border: false,
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
              <Button size="small" onClick={() => { if (id) fetchExportPresets(id); }}>刷新</Button>
              <Button size="small" type="primary" onClick={handleSavePreset}>保存当前为预设</Button>
              {selectedPresetId && (
                <>
                  <Button size="small" onClick={() => handleSetDefaultPreset(selectedPresetId)}>设为默认</Button>
                  <Button size="small" danger onClick={() => handleDeletePreset(selectedPresetId)}>删除</Button>
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
                      <Form.Item name="bidder_name" label="投标单位" style={{ marginBottom: 8 }}>
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
                        tooltip="默认关闭：图表仅用 mermaid 原生引擎渲染（前端预览同款/后端高清服务），失败时文档中以红字占位提示重新导出，保证渲染质量不降级。开启后失败图表改用内置 PIL 渲染器兜底（版式与预览略有差异）。总平面布置图/里程碑时间线始终使用内置渲染器，不受此开关影响。"
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
                        tooltip="开启后图表渲染失败的章节会插入占位段（含图表代码）而非整段跳过，便于人工补图。默认关闭。"
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
              description="大文档导出可能需要 10-30 秒，图表渲染和文档构建在后台进行，请耐心等待。"
              style={{ marginBottom: 12 }}
            />
          )}
          <Alert
            type={exportGate.allowed ? "success" : "warning"}
            showIcon
            message={exportGate.reason}
            description={exportGate.allowed
              ? `当前方案已完成导出预检，就绪度总检评分 ${exportPreflight?.total ?? "—"}，结论未过期且已放行。`
              : "请先到「审核与预检」完成当前方案的导出预检与就绪度总检；存在 high 问题、结论过期或未放行时不会生成文档。"}
            action={<Button size="small" onClick={() => setActiveTab("review")}>前往审核与预检</Button>}
            style={{ marginBottom: 12 }}
          />
          <Space>
            <Button
              type="primary"
              icon={<ExportOutlined />}
              onClick={() => handleExport("docx")}
              loading={exporting}
              disabled={!exportGate.allowed}
              title={exportGate.reason}
            >
              {exporting ? "导出中..." : "导出 DOCX"}
            </Button>
            <Button
              icon={<FilePdfOutlined />}
              onClick={() => handleExport("pdf")}
              loading={exporting}
              disabled={!exportGate.allowed}
              title={exportGate.allowed ? "先生成 DOCX 再转换为 PDF（需安装 Microsoft Word 或 LibreOffice）" : exportGate.reason}
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
                    <Tooltip title="结果文件已被保留策略（最多 5 份）清理，或磁盘文件已丢失。下次导出会自动清理这些记录。">
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
              } catch (e: any) {
                setLibError(e.message || "加载失败");
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
                renderItem={(lib: any) => (
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
        title={previewDoc ? `解析内容预览 · ${previewDoc.file_name}` : "解析内容预览"}
        open={!!previewDoc}
        onCancel={() => setPreviewDoc(null)}
        footer={null}
        width={760}
      >
        <Spin spinning={previewLoading}>
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
                {/* ✅ 强制全量重修（2026-09-22）：默认跳过「已修复且成果仍在」的冲突，
                    勾选后本批全部重新调用 AI 修复 */}
                <Checkbox
                  checked={forceFullRepair}
                  onChange={(e) => setForceFullRepair(e.target.checked)}
                >
                  强制全量重修
                </Checkbox>
                <Button
                  disabled={!(crRepair?.items || []).some((i: any) => i.status === "repaired")}
                  onClick={() => handleCrConfirm(crRepairItemIds("repaired"), [])}
                >
                  全部接受
                </Button>
                <Button
                  danger
                  disabled={!(crRepair?.items || []).some((i: any) => i.status === "repaired")}
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
                  expandedRowRender: (r: any) => (
                    <div style={{ fontSize: 12 }}>
                      {(r.occurrences || []).map((o: any, i: number) => (
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
                    render: (occs: any[]) =>
                      Array.from(new Set((occs || []).map((o) => o.section_title || o.section_id)))
                        .join(" / ") || "—",
                  },
                  {
                    title: "当前值", dataIndex: "occurrences", width: 150, ellipsis: true,
                    render: (occs: any[]) =>
                      Array.from(new Set((occs || []).map((o) => o.value).filter(Boolean))).join(" / ") || "—",
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
                    批次 {crRepair.repair_id} · {CR_REPAIR_STATUS[crRepair.status] || crRepair.status}
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
                items={(crRepair.items || []).map((it: any, idx: number) => ({
                  key: `${it.conflict_id}-${idx}`,
                  label: (
                    <Space wrap size={6}>
                      <Tag>{it.conflict_id || "—"}</Tag>
                      <Text style={{ fontSize: 12 }}>{it.section_title || it.section_id || "（无章节）"}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>{it.topic}</Text>
                      <Tag color={it.status === "repaired" ? "green" : it.status === "skipped" ? "orange" : "red"}>
                        {CR_ITEM_STATUS[it.status] || it.status}
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
                          <Button size="small" type="primary" onClick={() => handleCrConfirm([it.conflict_id], [])}>
                            接受
                          </Button>
                          <Button size="small" danger onClick={() => handleCrConfirm([], [it.conflict_id])}>
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
                    render: (_: any, r: any) => (
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
