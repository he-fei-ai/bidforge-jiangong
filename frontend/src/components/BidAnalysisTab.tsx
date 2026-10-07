/**
 * 「提取项目」Tab · 主体（组件级抽取）。
 *
 * 原先整个 Tab 的内联 JSX（前置条件提示 / 进度汇总 / 操作区 / 多标段检测结果 /
 * 分组任务列表 / 结果阅读区 / 下一步）都写死在 8000 行的 SchemeWorkbenchPage 里，
 * 无法单测。抽成受控组件后：
 *   - 数据与状态由页面通过 props 传入，组件只负责渲染与回调；
 *   - 缺失判定 / 汇总重算 / 13 分组统一走 utils/bidAnalysis（与后端同口径），
 *     不再前端硬编码分组表与「content === '未提取到'」的简化判定；
 *   - 展示为「分类栏 + 固定 N 项任务列表 + 内容区」：每行含全局序号、任务名称、
 *     所属分类、状态、摘要；点击分类可筛选该分类下解析项；窄屏（<768）列表与
 *     内容区改单栏互切（点行进详情、「返回列表」回退）；
 *   - 详情区附「来源位置」溯源（后端确定性反查的文档/行号/出处）与「来源方案」
 *     （结果项目级共享，逐行只能关联到发起提取的方案）；
 *   - 解析项定义加载失败时展示错误态 + 重试入口（不再永久停在「加载中」）；
 *   - 交互正确性由 tests/bidAnalysisTab.test.tsx 钉住。
 */
import React, { useState, memo, useMemo } from "react";
import { Alert, Button, Card, Progress, Space, Tag, Tooltip, Typography } from "antd";
import {
  AimOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  CopyOutlined,
  EditOutlined,
  EyeOutlined,
  GlobalOutlined,
  LeftOutlined,
  LoadingOutlined,
  ReloadOutlined,
  RightOutlined,
  SettingOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import MarkdownRenderer from "./MarkdownRenderer";
import { useBreakpoint } from "../utils/ui";
import { bidAnalysisApi } from "../api";
import {
  isMissingBaResult,
  mergeBaItem,
  normalizeBaGroups,
  parseBaEvidence,
  projectInfoFieldLabel,
  type BaGroup,
  type BaItemDef,
  type BaStoredItem,
  type BaSummary,
  type BaTextStats,
} from "../utils/bidAnalysis";
import { sourceNotice } from "../utils/parseSourceNotice";

const { Text } = Typography;

/** 动态口径文案：解析项总数 / 必选 / 可选（来源后端 /items 的 items，不再前端硬编码） */
export function baScopeText(defs: BaItemDef[]): string {
  const total = defs.length;
  const required = defs.filter(d => d.required).length;
  if (!total) return "结构化提取";
  return `${total} 项结构化提取（${required} 必选 + ${total - required} 可选）`;
}

export type BidAnalysisTabProps = {
  /** 解析项定义（后端 /bid-analysis/items 的 items，当前为固定 18 项） */
  defs: BaItemDef[];
  /** 后端 /bid-analysis/items 的 groups（13 分组，唯一权威源） */
  groups: any[];
  /** 已存储的解析项结果（后端 /bid-analysis/results 的 items） */
  items: BaStoredItem[];
  summary: BaSummary | null;
  running: boolean;
  /** 0-1 */
  progress: number;
  progressMsg: string;
  /** 已解析文档数（0 → 前置条件未满足） */
  parsedDocCount: number;
  selectedItem: any | null;
  sectionChecking: boolean;
  sectionCheckResult: any | null;
  /** 本轮提取规模（SSE `text_stats`）：项数 × 切段数 ≈ 模型调用次数 */
  textStats?: BaTextStats | null;
  /** 解析项定义（/items）加载失败原因；非空且列表为空时展示错误态 + 重试 */
  defsError?: string | null;
  /** 定义加载失败后的重试入口（不传则只展示错误文案） */
  onRetryDefs?: () => void;
  onStart: () => void;
  onStop: () => void;
  onOpenConfig: () => void;
  onCheckSections: () => void;
  onRefresh: () => void;
  onSelectItem: (item: any) => void;
  onRerunItem: (def: BaItemDef) => void;
  onOpenFullView: (item: any) => void;
  /** 人工校正当前选中项的提取结果（打开校正弹窗） */
  onEditItem?: (item: any) => void;
  onDismissSectionResult: () => void;
  onGoImport: () => void;
  onGoOutline: () => void;
  onGoFacts: () => void;
  /** 方案 id（用于「智能分类」调用 /api/v1/bid-analysis/classify） */
  schemeId?: string;
  /** 项目 id（分类接口可选，缺省时后端按 scheme_id 反查） */
  projectId?: string;
  /**
   * 2026-09-23 内容面板模式：外部（SchemeWorkbenchPage 的 extract 子 Tab）已把
   * 「18 项结构化提取」清单放到左侧面板，右侧只保留进度/操作栏 + 详情阅读区。
   * 为 true 时跳过内部「分类栏 + 任务列表」，避免左右两侧重复展示同一份清单。
   */
  contentOnly?: boolean;
};

/**
 * 行内摘要（单行）：内容压平为一行前 60 字 / 失败原因 / 运行中与待执行提示。
 * 口径与后端缺失判定一致（isMissingBaResult），不重复造判据。
 */
export function baRowSummaryText(
  stored: BaStoredItem | undefined,
  absent: boolean,
): string {
  const status = stored?.status || "idle";
  if (status === "running") return "提取中…";
  if (status === "error") return String(stored?.error || "本轮提取失败").slice(0, 60);
  if (status === "success") {
    if (absent) return "已完成，但未提取到有效内容";
    const oneLine = String(stored?.content || "")
      .replace(/[#>*`|_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return oneLine ? oneLine.slice(0, 60) : "无内容";
  }
  return "待执行（尚无提取结果）";
}

/** 单个解析项内容渲染（右侧阅读区与「查看完整」Modal 共用） */
export function BidAnalysisItemBody({ item }: { item: any }) {
  if (!item) return null;
  const content: string = item.content || "";
  // 失败项的错误原因必须可见：旧实现只在「无内容」时展示 error，
  // 而在途项保留了上一轮内容（便于对照），一旦 status=error 就有内容 ——
  // 结果用户看到的是旧内容 + 绿色/红色标签，完全不知道这一轮为什么失败。
  const hasError = !!(item?.error && String(item.error).trim());
  const banner = hasError ? (
    <Alert
      type="error"
      showIcon
      style={{ marginBottom: content ? 10 : 0 }}
      message="该项本轮提取失败"
      description={
        <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {String(item.error)}
          {content ? "（下方仍展示上一轮内容，便于对照）" : ""}
        </span>
      }
    />
  ) : null;

  if (!content) {
    if (banner) return banner;
    return <div style={{ color: "#999", padding: 20 }}>（暂无内容）</div>;
  }
  const isJson =
    item.output_type === "json" ||
    (() => {
      try {
        JSON.parse(content);
        return true;
      } catch {
        return false;
      }
    })();

  if (isJson) {
    let obj: any = null;
    try {
      obj = JSON.parse(content);
    } catch {
      obj = null;
    }
    if (!obj || typeof obj !== "object") {
      return (
        <>
          {banner}
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "monospace" }}>{content}</pre>
        </>
      );
    }
    // 项目级基本信息存的是英文键 JSON（键名即 prompt 模板键，不可改）——
    // 左列展示中文名，避免用户看到 project_name / contractor 这类英文键。
    const isProjectInfo = item.item_id === "projectBasicInfo";
    return (
      <div>
        {banner}
        {Object.entries(obj).map(([k, v]) => (
          <div key={k} style={{ marginBottom: 2, display: "flex", gap: 8, fontSize: 13, lineHeight: 1.4 }}>
            <div style={{ minWidth: 160, color: "#666", flexShrink: 0 }}>
              {isProjectInfo ? projectInfoFieldLabel(k) : k}
            </div>
            <div style={{ flex: 1, color: v === "没有提及" ? "#bbb" : "#333", wordBreak: "break-word" }}>
              {v === "没有提及" || v === "" ? <Text type="secondary">（无）</Text> : String(v)}
            </div>
          </div>
        ))}
      </div>
    );
  }

  // Markdown 项：走统一渲染器（与正文/事实等模块一致，避免直接暴露 ## / ** 源码）
  return (
    <>
      {banner}
      <MarkdownRenderer content={content} />
    </>
  );
}

/**
 * 「来源位置」列表：展示后端提取成功后确定性反查的出处（文档/标题/行号/摘录）。
 * 无证据（旧数据 / 未匹配到 / 人工校正后清空）时整块不渲染，不留空壳。
 * 导出供 ParseResultCategoryPanel（上传解析 Tab 分类栏）复用，两处口径一致。
 */
export function BidAnalysisEvidenceList({ item }: { item: any }) {
  const list = parseBaEvidence(item?.evidence);
  if (!list.length) return null;
  return (
    <div
      className="ba-evidence-list"
      style={{ marginTop: 6, borderTop: "1px dashed #e5e5e5", paddingTop: 4 }}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        来源位置（由提取结果反查原文自动匹配，共 {list.length} 处；人工校正后清空）
      </Text>
      {list.map((e, i) => (
        <div
          key={i}
          className="ba-evidence-item"
          style={{ marginTop: 6, fontSize: 12, color: "#555", lineHeight: 1.7 }}
        >
          <Space size={6} wrap>
            <Tag style={{ marginInlineEnd: 0 }}>{e.doc || "原文"}</Tag>
            {e.field && <Tag color="blue" style={{ marginInlineEnd: 0 }}>{e.field}</Tag>}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {e.heading ? `${e.heading} › ` : ""}第 {e.line ?? "?"} 行
            </Text>
          </Space>
          <div style={{ color: "#888", paddingLeft: 2, wordBreak: "break-word" }}>
            「{e.quote}」
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * 九大章节 key → 中文标题（**兜底**用）。
 *
 * ✅ 2026-10-06：本表曾被 ClassificationPanel 当作唯一标题来源，而它是
 * backend `scheme_classification.NINE_CHAPTERS` 的一份手抄副本。现改为兜底：
 * 优先读响应自带的 `ch.title`（backend 已随每一章下发），本表仅在
 * `title` 缺失时兜底，避免 backend 增删章节后前端标题静默失真。
 * parity 护栏见 backend/tests/test_classification_parity_20261001.py。
 */
const CHAPTER_LABELS: Record<string, string> = {
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

/**
 * 单章字段覆盖率（0~1）。
 *
 * ✅ BUG 修复（2026-10-06）：原实现读 `ch.completeness`，而 backend
 * `validate_chapter_fields` 逐章下发的键是 **`field_coverage`**
 * （`completeness` 只存在于顶层汇总）。取值恒为 undefined →
 * `Math.round(undefined * 100)` = NaN → **九大章节每一条进度条恒显示 0%**，
 * 且 `status="active"` 永不转 success —— 字段完整性面板整体失效、
 * 零测试覆盖（此前只有顶层「总体 X%」是对的，因为它读顶层同名键）。
 *
 * 兼容：若响应确实带 `completeness`（旧缓存 / 未来契约变更）仍按其取值，
 * 两键都缺失才回落 0。
 */
export function chapterFieldCoverage(ch: any): number {
  const raw = ch?.field_coverage ?? ch?.completeness;
  const n = Number(raw);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

/** 章节中文标题：优先响应自带 title，其次兜底表，最后退回 key。 */
export function chapterTitleOf(ch: any, key: string): string {
  return ch?.title || CHAPTER_LABELS[key] || key;
}

/**
 * 危大工程自动分类 + 九大章节字段完整性面板（#11）。
 * data：/api/v1/bid-analysis/classify 返回体（含 classification / chapter_completeness）。
 */
export function ClassificationPanel({ data }: { data: any }) {
  if (!data) return null;
  if (data.disabled) {
    return (
      <Alert type="info" showIcon banner style={{ marginBottom: 4 }}
        message="智能分类已关闭" description={data.reason} />
    );
  }
  if (data.error) {
    return (
      <Alert type="error" showIcon banner style={{ marginBottom: 4 }}
        message="智能分类失败" description={data.error} />
    );
  }
  const cls = data.classification || {};
  const completeness = data.chapter_completeness || {};
  const chapters: Record<string, any> = completeness.chapters || {};
  const chapterKeys = Object.keys(chapters);

  return (
    <Card size="small" style={{ marginBottom: 4 }}
      title={<span><AimOutlined /> 危大工程分类与九大章节字段完整性</span>}>
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        <div>
          <Text strong>识别大类：</Text>
          {(cls.category_names || []).join("、") || "—"}
          {(cls.sub_names || []).length > 0 && (
            <span style={{ marginLeft: 8 }}><Text type="secondary">子类：</Text>{cls.sub_names.join("、")}</span>
          )}
        </div>
        <div>
          <Text strong>危大级别：</Text>
          {cls.is_hazardous ? <Tag color="red">危大工程</Tag> : null}
          {cls.is_oversize ? <Tag color="volcano">超过一定规模的危大工程</Tag> : null}
          {!cls.is_hazardous && !cls.is_oversize && <span>—</span>}
        </div>
        {(cls.standards_keys || []).length > 0 && (
          <div>
            <Text strong>适用规范类别：</Text>
            {cls.standards_keys.map((k: string) => <Tag key={k}>{k}</Tag>)}
          </div>
        )}
        <div style={{ marginTop: 4 }}>
          <Text strong>九大章节字段完整性：</Text>
          <Text type="secondary">
            {chapterKeys.length ? `总体 ${Math.round((completeness.completeness || 0) * 100)}%` : "（暂无提取结果）"}
          </Text>
        </div>
        {chapterKeys.map((key) => {
          const ch = chapters[key] || {};
          const pct = Math.round(chapterFieldCoverage(ch) * 100);
          const missing = ch.missing_fields || [];
          return (
            <div key={key}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span>{chapterTitleOf(ch, key)}</span>
                <span style={{ color: pct >= 100 ? "#52c41a" : "#fa8c16" }}>{pct}%</span>
              </div>
              <Progress percent={pct} size="small" status={pct >= 100 ? "success" : "active"} />
              {missing.length > 0 && (
                <div style={{ fontSize: 11, color: "#999" }}>
                  缺失：{missing.slice(0, 6).join("、")}{missing.length > 6 ? "…" : ""}
                </div>
              )}
            </div>
          );
        })}
      </Space>
    </Card>
  );
}

function BidAnalysisTab({
  defs,
  groups,
  items,
  summary,
  running,
  progress,
  progressMsg,
  parsedDocCount,
  selectedItem,
  sectionChecking,
  sectionCheckResult,
  textStats,
  defsError,
  onRetryDefs,
  onStart,
  onStop,
  onOpenConfig,
  onCheckSections,
  onRefresh,
  onSelectItem,
  onRerunItem,
  onOpenFullView,
  onEditItem,
  onDismissSectionResult,
  onGoImport,
  onGoOutline,
  onGoFacts,
  contentOnly,
  schemeId,
  projectId,
}: BidAnalysisTabProps) {
  const { isSmall, isDesktop } = useBreakpoint();
  // 分类筛选（"all" 或后端分组键）与窄屏列表/详情互切、复制反馈：
  // 纯展示层局部状态，不上抛页面（页面只需关心 selectedItem）。
  const [catFilter, setCatFilter] = useState<string>("all");
  const [detailOpen, setDetailOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  // ✅ 修复（2026-09-23，重复触发）：单项「重新提取」的幂等守卫。
  // 旧实现仅靠 disabled={running}，而 running 由父组件异步 setState，
  // 从点击到 running=true 之间的窗口内双击/连点会把同一项的 AI 重跑
  // 提交两次（重复烧额度，且后一次覆盖前一次结果，用户看到的值不可预测）。
  // 用 ref 做同步标志（不触发重渲），1.5s 冷却自清理，无回调依赖。
  const rerunGuard = React.useRef<Set<string>>(new Set());
  const rerunLockMs = 1500;

  // ✅ 修复（2026-09-23，卸载后定时器仍在跑）：统一登记本组件挂起的定时器，
  //    卸载时全部清理。本组件有两处 window.setTimeout（重跑冷却自清理、
  //    复制反馈复位），旧实现不清理 → 组件卸载后回调照旧执行：复制反馈会在
  //    已卸载组件上 setState，单测环境拆除后回调直接抛错
  //    （vitest「Uncaught Exception in processTimers」）。
  const timersRef = React.useRef<number[]>([]);
  const safeTimeout = (fn: () => void, ms: number) => {
    const t = window.setTimeout(() => {
      timersRef.current = timersRef.current.filter((x) => x !== t);
      fn();
    }, ms);
    timersRef.current.push(t);
    return t;
  };
  React.useEffect(() => () => {
    timersRef.current.forEach((t) => window.clearTimeout(t));
    timersRef.current = [];
  }, []);

  // ✅ 2026-09-24（#11 前端面板）：危大工程自动分类结果（九大章节字段完整性）。
  //    由「智能分类」按钮触发（调用 /api/v1/bid-analysis/classify），本地持有，不阻断提取。
  const [classifyResult, setClassifyResult] = React.useState<any>(null);
  const [classifying, setClassifying] = React.useState(false);
  const handleClassify = async () => {
    if (!schemeId) return;
    setClassifying(true);
    try {
      const res = await bidAnalysisApi.classify(schemeId, projectId);
      const data = res?.data ?? res;
      if (data?.disabled) {
        setClassifyResult({ disabled: true, reason: data.reason });
      } else if (data?.ok) {
        setClassifyResult(data);
      } else {
        setClassifyResult({ error: "分类失败" });
      }
    } catch (e: any) {
      setClassifyResult({ error: String(e?.message || e) });
    } finally {
      setClassifying(false);
    }
  };

  const handleRerunItem = (def: BaItemDef) => {
    const key = def.item_id;
    if (rerunGuard.current.has(key)) return;
    rerunGuard.current.add(key);
    safeTimeout(() => { rerunGuard.current.delete(key); }, rerunLockMs);
    onRerunItem(def);
  };

  // 13 分组：优先后端 groups，缺失时按定义项的 group 推导
  // ✅ 性能优化：以下派生数据按依赖记忆化，避免每次渲染（含父组件每帧重渲）重复
  //    做 O(n) 重算（结构化提取项可达数百条）。原始实现每次渲染都重建 groupList /
  //    flatRows / catEntries / visibleRows，是「提取项目」Tab 随父组件每帧重渲时的卡顿来源。
  const groupList: BaGroup[] = useMemo(() => normalizeBaGroups(groups, defs), [groups, defs]);
  const storedMap = useMemo(() => {
    const m = new Map<string, BaStoredItem>();
    for (const it of items) m.set(it.item_id, it);
    return m;
  }, [items]);

  // 扁平任务列表：全局序号按后端 sort_order 与分组顺序确定，与分类筛选无关
  const { flatRows, catEntries, itemCount } = useMemo(() => {
    let seqCursor = 0;
    const fr = groupList.flatMap(g =>
      g.items.map(def => ({ def, groupKey: g.key, groupLabel: g.label, seq: (seqCursor += 1) })),
    );
    // 分类栏条目：全部 + 各分组（带项数）
    const ce = [
      { key: "all", label: "全部", count: fr.length },
      ...groupList.map(g => ({ key: g.key, label: g.label, count: g.items.length })),
    ];
    return { flatRows: fr, catEntries: ce, itemCount: fr.length };
  }, [groupList]);
  const visibleRows = useMemo(() =>
    catFilter === "all" ? flatRows : flatRows.filter(r => r.groupKey === catFilter),
    [flatRows, catFilter]);

  // 选中项：分类归属 / 序号 / 上下切换均基于当前筛选后的行列表
  const selectedRow = useMemo(() =>
    selectedItem ? flatRows.find(r => r.def.item_id === selectedItem.item_id) || null : null,
    [flatRows, selectedItem]);
  const selIdx = useMemo(() =>
    selectedItem ? visibleRows.findIndex(r => r.def.item_id === selectedItem.item_id) : -1,
    [visibleRows, selectedItem]);

  const selectRow = (row: (typeof flatRows)[number]) => {
    onSelectItem(mergeBaItem(row.def, storedMap.get(row.def.item_id)));
    if (isSmall) setDetailOpen(true); // 窄屏点行即进入详情
  };
  const goAdjacent = (delta: number) => {
    if (!visibleRows.length || selIdx < 0) return;
    selectRow(visibleRows[(selIdx + delta + visibleRows.length) % visibleRows.length]);
  };
  /** 复制原始解析结果：Clipboard API 不可用（http / 内网）时回退 textarea+execCommand */
  const handleCopyContent = async () => {
    const text = String(selectedItem?.content || "");
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        throw new Error("clipboard unavailable");
      }
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* 均不可用时由用户手动选中复制 */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    // ✅ 用 safeTimeout：卸载时会被清理，避免在已拆卸的组件上 setState
    safeTimeout(() => setCopied(false), 1500);
  };

  // 行内 / 详情区共用的状态标签（口径与后端一致，不重复造判据）
  const renderStatusNode = (def: BaItemDef, stored?: BaStoredItem) => {
    const status = stored?.status || "idle";
    const content = stored?.content || "";
    const absent = status === "success" && isMissingBaResult(content, def.output_type);
    const isManual = stored?.source === "manual";
    if (status === "running") {
      return (
        <Tag color="blue">
          <LoadingOutlined /> 运行中
        </Tag>
      );
    }
    if (status === "error") {
      return (
        <Tooltip title={stored?.error || "该项提取失败"}>
          <Tag color="red">✗ 失败</Tag>
        </Tooltip>
      );
    }
    if (status === "success") {
      if (isManual) return <Tag color="purple">✎ 人工 {content.length}字</Tag>;
      return absent ? <Tag color="volcano">⚠ 无内容</Tag> : <Tag color="green">✓ {content.length}字</Tag>;
    }
    return <Tag color="default">待执行</Tag>;
  };
  // 「完成且有有效内容」的项数：优先后端 success_valid（/results 权威汇总），
  // 运行中前端重算时也带该字段；两者都缺（老后端）才回退 success。
  const successValid = summary
    ? (summary.success_valid ?? summary.success ?? 0)
    : 0;
  const sectionCount =
    sectionCheckResult?.total_declared ||
    sectionCheckResult?.detected_count ||
    (sectionCheckResult?.sections || []).length;

  /** 分类栏：桌面竖向侧栏 / 窄屏横向 chips；点击任一分类筛选该分类下解析项 */
  const renderCatBar = (vertical: boolean) => (
    <div
      className="ba-cat-bar"
      style={
        vertical
          ? {
              width: 150, flexShrink: 0, border: "1px solid #f0f0f0",
              borderRadius: 6, padding: "6px 0", overflowY: "auto" as const,
            }
          : {
              display: "flex", gap: 8, overflowX: "auto" as const,
              whiteSpace: "nowrap", padding: "2px 0", flexShrink: 0,
            }
      }
    >
      {catEntries.map(c => {
        const active = catFilter === c.key;
        return (
          <div
            key={c.key}
            className="ba-cat-item"
            data-group={c.key}
            role="button"
            tabIndex={0}
            onKeyDown={e => { if (e.key === "Enter") setCatFilter(c.key); }}
            onClick={() => setCatFilter(c.key)}
            style={{
              cursor: "pointer", fontSize: 12, flexShrink: 0,
              padding: vertical ? "7px 12px" : "3px 10px",
              borderRadius: vertical ? 0 : 14,
              background: active ? (vertical ? "#e6f4ff" : "#1677ff") : "transparent",
              color: active && !vertical ? "#fff" : active ? (vertical ? "#1677ff" : "#fff") : "#555",
              fontWeight: active ? 600 : 400,
              border: vertical ? "none" : `1px solid ${active ? "#1677ff" : "#d9d9d9"}`,
              display: "flex", alignItems: "center", gap: 6,
              justifyContent: vertical ? "space-between" : "center",
            }}
          >
            <span>{c.label}</span>
            <span style={{ color: active && !vertical ? "#fff" : "#999", fontSize: 11 }}>{c.count}</span>
          </div>
        );
      })}
    </div>
  );

  return (
    <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
      {/* ===== 前置条件：本步依赖「解析提取」产出的纯文本 =====
          2026-09-23 晚：改为 banner 紧凑模式（~32px vs 原 120px），省 ~88px。 */}
      {parsedDocCount === 0 && (
        <Alert
          banner
          style={{ marginBottom: 4 }}
          type="warning"
          showIcon
          message={
            <Space size={4}>
              前置条件未满足：至少解析一份文档后才能执行结构化提取
              <Button size="small" type="primary" onClick={onGoImport}>
                前往解析提取
              </Button>
            </Space>
          }
        />
      )}

      {/* ===== 进度 / 汇总 =====
          2026-09-23 晚：Card body padding 从 8px 16px → 4px 12px；Tag 行全部 flexWrap 单行展示，
          进度条和状态提示用 margin 4 压缩，整体从 ~80px 省到 ~40px。 */}
      {(summary || running) && (
        <Card size="small" style={{ marginBottom: 4 }} styles={{ body: { padding: "4px 12px" } }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Text strong style={{ fontSize: 13 }}>提取进度：</Text>
            {summary && (
              <>
                <span>
                  共 <Text strong>{summary.total}</Text> 项
                </span>
                <Tooltip title="完成项中「有有效内容」的数量（排除「未提取到」/ json 全空）—— 下游目录生成的放行依据">
                  <Tag color="green">✓ 有效 {successValid}</Tag>
                </Tooltip>
                {(summary.success || 0) > successValid && (
                  <Tooltip title="已完成但内容为空标记（「未提取到」/ json 全「没有提及」），不计入有效成果">
                    <Tag color="orange">⚠ 完成无内容 {(summary.success || 0) - successValid}</Tag>
                  </Tooltip>
                )}
                {(summary.manual_count || 0) > 0 && (
                  <Tooltip title="由人工校正确认的结果（source='manual'），优先级高于 AI 输出">
                    <Tag color="purple">✎ 人工校正 {summary.manual_count}</Tag>
                  </Tooltip>
                )}
                {summary.errors > 0 && <Tag color="red">✗ 失败 {summary.errors}</Tag>}
                {summary.running > 0 && <Tag color="blue">⏳ 运行中 {summary.running}</Tag>}
                {!summary.all_required_done && (summary.missing_required?.length || 0) > 0 && (
                  <Tooltip title={`必选项缺失：${(summary.missing_required || []).join("、")}`}>
                    <Tag color="orange">⚠ 必选项缺失 {summary.missing_required.length}</Tag>
                  </Tooltip>
                )}
                {summary.all_required_done && (
                  <Tag color="green">
                    <CheckCircleOutlined /> 全部就绪
                  </Tag>
                )}
              </>
            )}
            {running && (
              <Progress percent={Math.round(progress * 100)} size="small" style={{ width: 140 }} />
            )}
          </div>
          {progressMsg && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              状态：{progressMsg}
            </Text>
          )}
          {/* 提取规模：本轮会烧多少模型调用。旧实现用户全程不可见，
              超长文档要等十几分钟后从账单里才发现额度被烧光。 */}
          {textStats && (textStats.total_chars || textStats.est_model_calls) > 0 && (
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
              提取规模：{textStats.total_chars.toLocaleString()} 字
              {textStats.chunk_size ? ` ÷ 每段 ${textStats.chunk_size.toLocaleString()} 字` : ""}
              = <Text strong>{textStats.segment_count}</Text> 段
              × <Text strong>{textStats.item_count}</Text> 项
              ≈ <Text strong>{textStats.est_model_calls.toLocaleString()}</Text> 次模型调用
            </Text>
          )}
          {/* ✅ 2026-09-26 提取依据完整性：任一文档被截断、或预算不足导致有文档
              整份未被纳入时，所有提取项的可信度都受影响——必须在结果区显性提示，
              而不是只停在后端日志里（此前用户全程看不到「提取依据不完整」）。 */}
          {(() => {
            const n = textStats ? sourceNotice(textStats) : null;
            return n ? (
              <Alert
                type="warning"
                showIcon
                message={n.title}
                description={n.detail}
                style={{ marginTop: 4 }}
              />
            ) : null;
          })()}
        </Card>
      )}

      {/* ===== 操作区：配置 + 启动 / 停止 ===== */}
      <Card size="small" style={{ marginBottom: 4 }}>
        <Space wrap>
          <Text strong>{baScopeText(defs)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            仅对当前方案已解析的文档运行
          </Text>
          <div style={{ flex: 1 }} />
          {!running ? (
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              disabled={parsedDocCount === 0}
              onClick={onStart}
            >
              开始提取
            </Button>
          ) : (
            <Button danger icon={<StopOutlined />} onClick={onStop}>
              停止
            </Button>
          )}
          <Button icon={<SettingOutlined />} disabled={running} onClick={onOpenConfig}>
            配置
          </Button>
          {/* 多标段检测：确定性规则秒级返回，不消耗 AI 额度 */}
          <Tooltip title="对已解析纯文本运行规则检测，判断项目资料是否疑似包含多个标段">
            <Button
              icon={<AimOutlined />}
              loading={sectionChecking}
              disabled={running || parsedDocCount === 0}
              onClick={onCheckSections}
            >
              多标段检测
            </Button>
          </Tooltip>
          {/* ✅ 修复（2026-09-23）：提取进行中禁止刷新 —— onRefresh 会拉取
              /bid-analysis/results 并整体替换 baItems/baSummary，把 UI 上正在
              进行的任务快照覆盖成旧值（进度回退、已完成项变回待提取）；
              与同卡片「配置」「多标段检测」的 disabled={running} 对齐。 */}
          <Button icon={<ReloadOutlined />} disabled={running} onClick={onRefresh}>
            刷新
          </Button>
          {/* ✅ 2026-09-24（#11）：危大工程自动分类 + 九大章节字段完整性校验。
              确定性规则 + 提取结果差集分析，不消耗额外 AI 额度；结果以面板展示。 */}
          <Tooltip title={schemeId ? "按方案名称/资料自动识别危大工程类别，并校验九大章节字段完整性" : "缺少方案 id，无法分类"}>
            <Button
              icon={<AimOutlined />}
              loading={classifying}
              disabled={running || !schemeId}
              onClick={handleClassify}
            >
              智能分类
            </Button>
          </Tooltip>
        </Space>
      </Card>

      {/* ===== 多标段检测结果明细（未命中也如实告知，避免「点了没反应」） ===== */}
      {sectionCheckResult && !sectionCheckResult.has_multiple && (
        <Alert
          type="info"
          showIcon
          closable
          style={{ marginBottom: 4 }}
          message="多标段检测：未发现多标段特征"
          description={
            sectionCheckResult.source === "no_documents" || sectionCheckResult.source === "empty_text"
              ? "暂无已解析文档文本，请先完成「解析提取」"
              : "已基于当前方案全部已解析文档规则扫描，可继续「目录生成」"
          }
          onClose={onDismissSectionResult}
        />
      )}
      {sectionCheckResult?.has_multiple && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 4 }}
          message={`检测到疑似多标段（约 ${sectionCount} 个）`}
          description={
            <div>
              {(sectionCheckResult.sections || []).length > 0 && (
                <div style={{ marginBottom: 4, fontSize: 12 }}>
                  识别到的标段标识：{(sectionCheckResult.sections || []).join("、")}
                </div>
              )}
              建议按标段拆分项目资料后分别建方案；继续生成将把多个标段内容混淆进同一份方案。
            </div>
          }
        />
      )}

      {/* ===== 危大工程分类与九大章节字段完整性（点击「智能分类」后展示） ===== */}
      {classifyResult && <ClassificationPanel data={classifyResult} />}

      {/* ===== 主体：分类栏 + 解析项任务列表 + 内容区 =====
          桌面（>=1200）：左侧竖向分类栏 + 列表 + 内容区三栏；
          窄屏（<768）：分类横向 chips，列表与详情单栏互切；
          平板（768~1200）：顶部 chips + 列表/内容两栏。
          contentOnly=true：外部已把「18 项结构化提取」清单放到左侧面板，
            此处仅保留结果阅读区（详情），避免重复展示。 */}
      <div style={{ display: "flex", gap: 8, minHeight: 200, alignItems: "stretch" }}>
        {!contentOnly && (<>
        {/* 分类栏（桌面）：左侧竖向栏，点击任一分类筛选该分类下解析项（“全部”回退） */}
        {isDesktop && renderCatBar(true)}

        {/* 中栏（窄屏为唯一栏）：分类 chips（非桌面时置顶）+ 任务列表 */}
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {!isDesktop && renderCatBar(false)}
          {(!isSmall || !detailOpen) && (
          <div className="ba-task-pane" style={{ flex: 1, minWidth: 0, border: "1px solid #f0f0f0", borderRadius: 6, overflow: "hidden" }}>
          <div style={{ padding: "4px 8px", background: "#fafafa", borderBottom: "1px solid #f0f0f0", fontSize: 13, fontWeight: 500 }}>
            解析项任务列表（{itemCount} 项，点击任一查看完整内容）
            {catFilter !== "all" && (
              <Text type="secondary" style={{ fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
                当前分类：{catEntries.find(c => c.key === catFilter)?.label}（{visibleRows.length} 项）
              </Text>
            )}
          </div>
          {itemCount === 0 ? (
            defsError ? (
              /* 定义加载失败：不能永久停在「加载中」，给出原因与重试入口 */
              <Alert
                style={{ margin: 8 }}
                type="error"
                showIcon
                message="解析项定义加载失败"
                description={
                  <span style={{ wordBreak: "break-word" }}>
                    {defsError}。列表与提取均依赖后端 /bid-analysis/items，修复后点击重试。
                  </span>
                }
                action={
                  onRetryDefs ? (
                    <Button size="small" icon={<ReloadOutlined />} onClick={onRetryDefs}>
                      重试
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <div style={{ padding: 12, textAlign: "center", color: "#999" }}>解析项定义加载中…</div>
            )
          ) : (
            visibleRows.map(row => {
              const { def } = row;
              const stored = storedMap.get(def.item_id);
              const absent = stored?.status === "success"
                && isMissingBaResult(stored?.content || "", def.output_type);
              const isSelected = selectedItem?.item_id === def.item_id;
              return (
                <div
                  key={def.item_id}
                  className="ba-item-row"
                  data-item-id={def.item_id}
                  style={{
                    padding: "6px 12px",
                    borderTop: "1px solid #f5f5f5",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                    background: isSelected ? "#e6f4ff" : "transparent",
                  }}
                  onClick={() => selectRow(row)}
                >
                  {/* 全局序号：与分类筛选无关，固定按后端 sort_order 排定 */}
                  <span
                    className="ba-item-seq"
                    style={{ width: 24, flexShrink: 0, textAlign: "right", color: "#999", fontSize: 12 }}
                  >
                    {row.seq}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                      {def.required ? <span style={{ color: "#ff4d4f", flexShrink: 0 }}>*</span> : null}
                      <span
                        style={{
                          fontSize: 13, fontWeight: def.required ? 600 : 400,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        }}
                      >
                        {def.label}
                      </span>
                      {/* 所属分类：不再依赖分组表头，每行自带 */}
                      <Tag
                        className="ba-row-cat-tag"
                        style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: "16px", padding: "0 4px", flexShrink: 0 }}
                      >
                        {row.groupLabel}
                      </Tag>
                    </div>
                    <div
                      className="ba-row-summary"
                      style={{ fontSize: 12, color: "#999", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    >
                      {baRowSummaryText(stored, absent)}
                    </div>
                  </div>
                  {renderStatusNode(def, stored)}
                  <Tooltip title="重新提取该项（仅覆盖该项结果，不影响其它解析项）">
                    <Button
                      size="small"
                      type="link"
                      icon={<ReloadOutlined />}
                      disabled={running}
                      aria-label={`重新提取 ${def.label || def.item_id}`}
                      onClick={e => {
                        e.stopPropagation();
                        handleRerunItem(def);
                      }}
                    />
                  </Tooltip>
                </div>
              );
            })
          )}
          </div>
          )}
        </div>
        </>)}

        {/* 右栏（窄屏为详情单栏，带「返回列表」）：结果阅读区
            contentOnly=true 时始终显示详情面板（不依赖 isSmall/detailOpen，因为外部已用左侧面板选项） */}
        {(contentOnly || !isSmall || detailOpen) && (
        <div className="ba-detail-pane" style={{ flex: contentOnly ? 1 : 1.2, minWidth: 0, border: "1px solid #f0f0f0", borderRadius: 6, overflow: "hidden", display: "flex", flexDirection: "column" }}>
          <div style={{ padding: "4px 8px", background: "#fafafa", borderBottom: "1px solid #f0f0f0", fontSize: 13, fontWeight: 500, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", minWidth: 0 }}>
              {selectedItem ? (
                <>
                  {selectedRow && <Text type="secondary" style={{ fontSize: 12 }}>#{selectedRow.seq}</Text>}
                  <span>{selectedItem.label || selectedItem.item_id}</span>
                  {/* 分类归属 + 状态 + 来源时间：详情头部把「这条结果从哪来」说清楚 */}
                  {selectedRow && <Tag className="ba-detail-cat-tag">{selectedRow.groupLabel}</Tag>}
                  {renderStatusNode(selectedRow?.def || { item_id: selectedItem.item_id, output_type: selectedItem.output_type }, storedMap.get(selectedItem.item_id))}
                  {storedMap.get(selectedItem.item_id)?.updated_at && (
                    <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                      更新于 {String(storedMap.get(selectedItem.item_id)!.updated_at)}
                    </Text>
                  )}
                  {/* 关联项目口径：结果项目级共享，逐行唯一能展示的关联是发起提取的方案 */}
                  {storedMap.get(selectedItem.item_id)?.scheme_name && (
                    <Tooltip title="提取结果按项目共享：同项目下多个方案共用同一份结果；此处展示发起本次提取的方案">
                      <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                        来源方案：{String(storedMap.get(selectedItem.item_id)!.scheme_name)}（项目级共享）
                      </Text>
                    </Tooltip>
                  )}
                </>
              ) : (
                "结果阅读区（从列表选择解析项）"
              )}
            </span>
            {selectedItem && (
              <Space size={8} wrap>
                {isSmall && (
                  <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => setDetailOpen(false)}>
                    返回列表
                  </Button>
                )}
                <Tooltip title="上一解析项">
                  <Button size="small" aria-label="上一解析项" icon={<LeftOutlined />} disabled={visibleRows.length < 2} onClick={() => goAdjacent(-1)} />
                </Tooltip>
                <Tooltip title="下一解析项">
                  <Button size="small" aria-label="下一解析项" icon={<RightOutlined />} disabled={visibleRows.length < 2} onClick={() => goAdjacent(1)} />
                </Tooltip>
                {selectedItem.content && (
                  <Tooltip title="复制该项原始解析结果（文本 / JSON 原文）">
                    <Button size="small" icon={<CopyOutlined />} onClick={handleCopyContent}>
                      {copied ? "已复制" : "复制"}
                    </Button>
                  </Tooltip>
                )}
                {onEditItem && selectedItem.content && (
                  <Tooltip title="人工校正：直接修改该项提取结果（AI 抽错关键参数时的唯一可靠出口）">
                    <Button
                      size="small"
                      icon={<EditOutlined />}
                      disabled={running}
                      onClick={() => onEditItem(selectedItem)}
                    >
                      人工校正
                    </Button>
                  </Tooltip>
                )}
                {selectedItem.content && (
                  <Button size="small" icon={<EyeOutlined />} onClick={() => onOpenFullView(selectedItem)}>
                    查看完整
                  </Button>
                )}
              </Space>
            )}
          </div>
          <div className="ba-item-body" style={{ flex: 1, overflow: "auto", padding: 4 }}>
            {selectedItem ? (
              <>
                <BidAnalysisItemBody item={selectedItem} />
                {/* 来源位置：有反查到的出处才展示（旧数据/未匹配/人工校正后无此块） */}
                <BidAnalysisEvidenceList item={selectedItem} />
              </>
            ) : (
              <div style={{ color: "#999", textAlign: "center", padding: 40 }}>
                从左侧选择一个解析项查看其完整提取结果
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      {/* ===== 下一步 ===== */}
      <Space style={{ marginTop: 16 }} wrap>
        <Button
          type="primary"
          icon={<ArrowRightOutlined />}
          onClick={onGoOutline}
          disabled={!summary?.all_required_done && !(successValid > 0)}
        >
          下一步：目录生成
        </Button>
        <Button icon={<GlobalOutlined />} onClick={onGoFacts}>
          去提取全局事实
        </Button>
        {summary && !summary.all_required_done && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            必选项未全部完成，目录生成质量可能受限
          </Text>
        )}
        {summary && (summary.success || 0) > 0 && successValid === 0 && (
          <Text type="warning" style={{ fontSize: 12 }}>
            已有完成项但内容均为空标记（「未提取到」），目录生成不会用到任何提取成果
          </Text>
        )}
      </Space>
    </div>
  );
}

// ✅ 性能优化：默认导出用 memo 包裹，父组件（方案工作台）在生成期每帧重渲时，
//    只要传入 props 引用不变，本组件即跳过整棵渲染（含数百条解析项列表）。
export default memo(BidAnalysisTab);
