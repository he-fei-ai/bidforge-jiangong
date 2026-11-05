import { factsApi } from "../../api";
import {
  type FactChapterStat,
  type FactChapterStatBlock,
  type FactCrossConflict,
  type FactGroupLike,
  type FactItemLike,
  type FactSegmentStats,
  type FactStats,
  type FactsSummary,
} from "../../types/facts";
import { FactsChapterCoveragePanel } from "./FactsChapterCoveragePanel.tsx";
import { FactsDiagnosticsPanel } from "./FactsDiagnosticsPanel.tsx";
import { Collapse, Pagination, Tag } from "antd";

export function hasFactsFilterMatch(
  groups: FactGroupLike[] | null | undefined,
  filter: string,
): boolean {
  if (filter === "all") return true;
  return (groups || []).some((g) =>
    (g?.items || []).some((it) =>
      filter === "simulated"
        ? it.is_simulated
        : filter === "conflict"
          ? it.has_conflict
          : !it.is_resolved,
    ),
  );
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
export function buildBatchResolveResultCopy(
  result:
    | {
        changed?: unknown;
        skipped?: unknown;
        skipped_safety_count?: unknown;
        skipped_safety?: unknown;
      }
    | null
    | undefined,
): {
  tone: "success" | "warning";
  text: string;
  /** ✅ 2026-10-06：被安全闸门拦下的事实**名称**列表。
   *  后端 `batch-resolve` 一直逐条回传 `skipped_safety[]`，但前端只读计数
   *  `skipped_safety_count` —— 用户看到「5 项未放行」却不知道是哪 5 项，
   *  只能自己逐条翻找。返回名称列表供结果提示直接列出（前 6 条 + 省略号）。 */
  blockedNames: string[];
} {
  const changed = Number(result?.changed) || 0;
  const skipped = Number(result?.skipped) || 0;
  const safety = Number(result?.skipped_safety_count) || 0;
  const rawSkippedSafety: unknown = result?.skipped_safety;
  const blockedNames: string[] = Array.isArray(rawSkippedSafety)
    ? rawSkippedSafety
        .map((it: { name?: unknown; title?: unknown }) =>
          String(it?.name || it?.title || "").trim(),
        )
        .filter(Boolean)
    : [];
  if (safety > 0) {
    const shown = blockedNames.slice(0, 6);
    const detail = shown.length
      ? `（${shown.join("、")}${
          blockedNames.length > shown.length ? ` 等 ${blockedNames.length} 条` : ""
        }）`
      : "";
    return {
      tone: "warning",
      text: `已确认 ${changed} 项；${safety} 项模拟值或安全关键事实未放行${detail}，请逐条核对裁决`,
      blockedNames,
    };
  }
  return {
    tone: "success",
    text: `已确认 ${changed} 项${skipped ? `，${skipped} 项原本已确认` : ""}，可注入事实已就绪`,
    blockedNames: [],
  };
}

/** 九大章节完整性与危大诊断只读面板（组件级测试锁定空态/覆盖率/缺参）。 */

export function buildBatchResolveCopy(
  stats:
    | {
        unresolved?: number;
        simulated?: number;
        conflicts?: number;
      }
    | null
    | undefined,
): {
  headline: string;
  confirmType: "warning" | "confirm";
  simulatedNote: string;
  conflictNote: string;
} {
  const unresolved = Number(stats?.unresolved) || 0;
  const simulated = Number(stats?.simulated) || 0;
  const conflicts = Number(stats?.conflicts) || 0;
  return {
    headline: `当前方案共有 ${unresolved} 项事实未确认，是否全部确认为“已核实”？`,
    confirmType: simulated > 0 ? "warning" : "confirm",
    simulatedNote:
      simulated > 0
        ? `⚠️ 其中含模拟值 ${simulated} 项（AI 推断的占位数据），确认后会被直接注入正文生成，请先核对真实值。`
        : "",
    conflictNote:
      conflicts > 0
        ? `另有 ${conflicts} 项存在矛盾（多源取值不一致），批量确认不处理矛盾，建议逐条裁决。`
        : "",
  };
}

/** 事实提取进度卡：进度 + 分段级实时日志（最新在上） */

export interface FactsAdjustOperation {
  op: string;
  fact_id?: string;
  name?: string;
  value?: string;
  // 后端回传字段是开放的（不同 op 带不同载荷），保留索引签名以兼容
  // api/index.ts 里 factsApi.adjust 的 Array<Record<string, unknown>> 契约。
  [key: string]: unknown;
}

export interface FactsAdjustPlanShape {
  summary?: string;
  operations: FactsAdjustOperation[];
  /**
   * ✅ 2026-10-08（全局事实模块收口 · F3 前端接线）：后端 `POST /global-facts/adjust`
   * 新增加法式字段 —— 被安全校验丢弃的操作及理由。旧后端不返回该键，
   * `normalizeFactsAdjustPlan` 一律归一为 `[]`，界面行为与收口前逐字一致。
   */
  ignored?: FactsAdjustIgnoredItem[];
}

/** 被丢弃操作的单条记录（与后端 `_validate_adjust_ops._ignore` 的记录形状对齐） */
export interface FactsAdjustIgnoredItem {
  op?: string;
  reason?: string;
  fact_id?: string;
  name?: string;
}

/**
 * 丢弃理由的**封闭枚举**（必须与后端 `global_facts._validate_adjust_ops` 逐项一致）。
 *
 * ✅ 2026-10-08：这是「展示层映射」而非「判据副本」—— 判定发生在后端，前端只把
 * 已知 reason 翻成中文；未知 reason 原样回显（后端将来加枚举值时，前端不会
 * 静默显示空白，而是显示英文键名，能立刻被发现）。parity 由
 * `frontend/src/tests/factsAdjust.test.tsx` 与后端护栏双向锁死。
 */
export const FACTS_ADJUST_IGNORE_REASONS = [
  "op_not_object",
  "unknown_fact_id",
  "no_effective_change",
  "missing_name_or_value",
  "duplicate_fact_key",
  "unknown_op",
] as const;

export const FACTS_ADJUST_IGNORE_REASON_LABELS: Record<string, string> = {
  op_not_object: "返回项不是合法操作对象",
  unknown_fact_id: "该事实不在当前方案可见范围（可能属于其它方案或已删除）",
  no_effective_change: "这一条没有任何实际变更",
  missing_name_or_value: "缺少名称或取值",
  duplicate_fact_key: "同名事实已存在，未重复新增",
  unknown_op: "未知的操作类型",
};

/** 丢弃理由 → 中文标签；未知/缺失一律回退原文，绝不返回空串 */
export function factsAdjustIgnoreText(item: FactsAdjustIgnoredItem): string {
  const reason = String(item?.reason || "").trim();
  const label = FACTS_ADJUST_IGNORE_REASON_LABELS[reason] || reason || "未知原因";
  const who = String(item?.name || "").trim() || String(item?.fact_id || "").trim();
  const op = String(item?.op || "").trim() || "unknown";
  return who ? `${op} · ${who}：${label}` : `${op}：${label}`;
}

/**
 * 防御式归一后端调整计划响应。
 *
 * ⚠️ 事件循环里的任何异常都会中断整个交互（本函数在 setState 路径上），
 * 因此缺键 / 类型不符 / 旧版后端一律回退空数组与空串，绝不抛异常。
 */
export function normalizeFactsAdjustPlan(data: unknown): FactsAdjustPlanShape {
  const d = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  const ops = Array.isArray(d.operations) ? d.operations : [];
  const ign = Array.isArray(d.ignored) ? d.ignored : [];
  return {
    summary: typeof d.summary === "string" ? d.summary : "",
    operations: ops.filter((o) => o && typeof o === "object") as FactsAdjustOperation[],
    ignored: ign.filter((i) => i && typeof i === "object") as FactsAdjustIgnoredItem[],
  };
}

/** 未纳入计划的操作数（后端 `ignored_count` 的前端兜底口径） */
export function factsAdjustIgnoredCount(
  plan?: { ignored?: FactsAdjustIgnoredItem[] } | null,
): number {
  return Array.isArray(plan?.ignored) ? plan!.ignored.length : 0;
}

/** 确认弹窗与「应用」按钮展示的操作明细上限（两处同口径） */
export const FACTS_ADJUST_PREVIEW_LIMIT = 20;

/** 操作明细的单行渲染：`op · 名称 值` */
export function formatFactsAdjustOperation(op: FactsAdjustOperation, i: number): string {
  const label = op.name || op.fact_id || `第 ${i + 1} 条`;
  const value = op.value ? ` ${op.value}` : "";
  return `${op.op} · ${label}${value}`;
}

export function buildFactsSummary(stats: unknown): FactsSummary {
  // ✅ any 清理（2026-11-02）：入参改 `unknown` —— 本函数仍是「把后端可能为脏/
  // 缺席的计数归一成 number」的**防御层**，输入本就不可信，故不收紧为具体接口
  // （收紧会迫使既有断言与全部测试夹具处理可空性，把防御逻辑挪到调用方，与本
  // 函数存在的目的相反）。归一化后的**读出侧**类型见 `types/facts.ts::FactsSummary`，
  // 页面状态 `factsSummary` 已按该类型声明。运行期行为与 any 版本逐字一致。
  if (!stats || typeof stats !== "object") return null!;
  const s = stats as Partial<FactStats>;
  const simulated = Number(s.simulated) || 0;
  const unresolved = Number(s.unresolved) || 0;
  const conflicts = Number(s.conflicts) || 0;
  const stale = Number(s.stale) || 0;
  return {
    ...s,
    simulated,
    unresolved,
    conflicts,
    stale,
    total: Number(s.total) || 0,
    // ⚠️ 口径：任一门控类计数 > 0 即告警（与后端 `_FACTS_INJECT_WHERE` 四条件
    //    对齐）—— stale 必须计入，否则误显示「全部就绪」并放行正文生成。
    has_warnings: simulated > 0 || unresolved > 0 || conflicts > 0 || stale > 0,
  };
}

/**
 * 逐章事实统计（消费后端 `stats.by_chapter`）。
 *
 * ✅ 2026-10-06 新增消费：后端每次 `GET /global-facts` 都下发
 * `stats.by_chapter`（九章各自的事实条数 + 字段覆盖率 + 缺失字段 +
 * by_fact_attr / by_source_kind 分布），此前前端**整体丢弃**。
 * 这类信息是「哪些章节事实不足」的**唯一**量化依据 —— 用户只能靠
 * `FactsDiagnosticsPanel` 的字段覆盖率（口径不同：那个查章节必填字段，
 * 这个查实际落了多少条事实），无法回答「我这套方案的九章事实均衡吗」。
 *
 * 返回按 `order` 升序的渲染就绪列表；`count === 0` 的章节也保留
 * （「这一章一条事实都没有」本身就是要展示的结论）。
 */
export type FactChapterStatRow = {
  order: number;
  key: string;
  title: string;
  count: number;
  coverage: number;
  missingCount: number;
  missingFields: string[];
  shared: number;
};

export function buildFactChapterStats(
  stats: { by_chapter?: FactChapterStatBlock } | null | undefined,
  titles?: Record<string, string>,
): FactChapterStatRow[] {
  const bc = stats?.by_chapter;
  /**
   * ✅ R89（2026-10-17）· 【P0 前后端契约断裂】形状读错，本面板自引入起永久空转。
   *
   * 后端 `list_facts` 的 `stats.by_chapter` 是**对象**
   * `{chapters, totals, by_fact_attr, by_source_kind, shared_count}`
   * （`global_facts._chapter_stats_for_items`）。旧实现
   * `Array.isArray(stats?.by_chapter) ? … : []` 对对象恒判 false ⇒ rows 恒 `[]`
   * ⇒ `FactsChapterCoveragePanel` **永远**渲染「暂无章节统计」。
   * 致命之处在于 `types/facts.ts` 当时也把该字段声明成数组 —— 契约与实现
   * **同错**，`tsc` 因此不可能报错（详见 FactChapterStatBlock 的订正说明）。
   *
   * 现按真实形状读取，并**额外容错裸数组**（防御旧后端/未来扁平化）；
   * 契约类型已同步订正为对象，数组分支只是运行时兜底。
   */
  const rows: FactChapterStat[] = Array.isArray(bc)
    ? (bc as unknown as FactChapterStat[])
    : Array.isArray(bc?.chapters)
      ? bc.chapters
      : [];
  return rows
    .map((c) => {
      const key = String(c?.key || "");
      const missing = Array.isArray(c?.missing_fields) ? c.missing_fields : [];
      return {
        order: Number(c?.order) || 0,
        key,
        title: String(c?.title || "") || factChapterTitle(key, titles),
        count: Number(c?.count) || 0,
        coverage: Number(c?.coverage) || 0,
        missingCount: missing.length,
        missingFields: missing.map((f: unknown) => String(f)),
        // ✅ R89：per-chapter 共性计数由后端聚合下发（旧的顶层合计语义错位 ⇒ 恒 0）。
        shared: Number(c?.shared_count) || 0,
      };
    })
    .sort((a: FactChapterStatRow, b: FactChapterStatRow) => a.order - b.order);
}

/** 分页参数归一（消费后端 `pagination`）。total_groups 为 0 时不渲染分页控件。 */
export function buildFactsPagination(
  pagination:
    | {
        total_groups?: unknown;
        offset?: unknown;
      }
    | null
    | undefined,
  pageSize: number,
): {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  show: boolean;
} {
  const total = Number(pagination?.total_groups) || 0;
  const limit = Math.max(1, Number(pageSize) || 20);
  const totalPages = Math.max(1, Math.ceil(total / limit));
  /**
   * ✅ R89（2026-10-17）· 【P1 分页页码可产出小数 / 非有限值】
   * 旧实现 `(offset) / limit + 1` 未取整：offset 不是 limit 整数倍时
   * （offset=25 / limit=20 ⇒ 2.25）得到**小数页码**并原样交给 antd
   * `Pagination current` —— 该组件按整数页取值，小数会让「当前页高亮」
   * 落到没有数据的页；更糟的是 `total > limit` 时 `show=true`，用户能点
   * 到一个不存在的页。offset 来自后端回传而非本地计算（未来任何按
   * limit 变更/多标签页切换都可能给出非整倍数），属**不可依赖调用方自律**
   * 的输入。
   * 同时 `Number(undefined) / limit` 得 NaN，`Math.min/max` 对 NaN
   * **恒返回 NaN** ⇒ page 变成 NaN，比小数更难排查。
   */
  const rawOffset = Number(pagination?.offset);
  const offset = Number.isFinite(rawOffset) && rawOffset > 0 ? Math.floor(rawOffset) : 0;
  const page = Math.min(Math.max(1, Math.floor(offset / limit) + 1), totalPages);
  return { page, pageSize: limit, total, totalPages, show: total > limit };
}

/** 每页分组数（可由设置覆盖；后端 limit=0 表示不分页） */
export const FACTS_PAGE_SIZE = 20;

/**
 * 九章事实分布（消费 `stats.by_chapter`）—— 只读面板。
 *
 * ✅ 2026-10-06 新增：后端每次列表请求都下发 by_chapter，此前整体丢弃。
 * 与 :func:`FactsDiagnosticsPanel` 的区别（两者不可互相替代）：
 *   · DiagnosticsPanel 查「章节**必填字段**覆盖率」（该抽到什么字段）
 *   · 本面板查「章节**实际落了多少条事实**」（抽得够不够、均衡吗）
 */

export function applyFactsCompletedEvent(evt: unknown): {
  segmentStats: FactSegmentStats | null;
  crossConflicts: FactCrossConflict[];
} {
  const e = (evt && typeof evt === "object" ? evt : {}) as Record<string, unknown>;
  const ss = e.segment_stats;
  return {
    segmentStats: ss && typeof ss === "object" ? (ss as FactSegmentStats) : null,
    crossConflicts: Array.isArray(e.cross_conflicts)
      ? (e.cross_conflicts as FactCrossConflict[])
      : [],
  };
}

// ============================================================
// 目录树纯操作函数（模块级导出，行为由 outlineTab.test 钉住）
// ============================================================
/** 扁平化工具：按当前树顺序收集所有节点 key（用于 /reorder 全量重排） */

export const FACT_CATEGORY_OPTIONS: { value: string; label: string }[] = [
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

/** ✅ R81（2026-10-15）：要求 vs 事实口径选项（后端 value_kind 白名单同源）。
 *
 *  - `fact`：本方案已采用的确定性口径（默认，正文可直接引用）；
 *  - `requirement`：招标文件/规范的要求原文口径（无法安全转写时），
 *    正文引用须写「满足/响应要求」，不得当作本方案既定值；
 *  - 空串 = 未区分（历史行缺省），不渲染徽标、注入无前缀。
 *
 * 与后端 `facts_extractor.VALUE_KIND_ALLOWED` / `normalize_value_kind`
 * 的值域逐字一致；徽标渲染见条目行的「要求口径」Tag。
 */
export const FACT_VALUE_KIND_OPTIONS: { value: string; label: string }[] = [
  { value: "fact", label: "本方案口径（默认）" },
  { value: "requirement", label: "要求口径（招标/规范原文）" },
];

/** 口径徽标文案（空串返回空——与徽标渲染「空值零渲染」纪律一致）。 */
export function factValueKindLabel(vk?: string | null): string {
  if (vk === "requirement") return "要求口径";
  if (vk === "fact") return "本方案口径";
  return "";
}

/** ✅ 2026-09-24：九大章节中文名（建办质〔2018〕31号）。
 *
 * ✅ 2026-10-06 降级为**离线兜底**：权威来源是 `GET /global-facts/category-map`
 * （后端 `facts_classification.category_map_payload()`，源自
 * `scheme_classification.NINE_CHAPTERS`）。此前本表是唯一来源 ——
 * 也就是后端增删章节后前端会静默显示旧标题。后端端点虽早已存在，
 * 前端却从未调用（`factsApi.categoryMap` 只被自己的单测引用）。
 * 现由 `loadFactCategoryMap` 在运行时覆盖；本表仅在端点不可用时兜底。
 * parity 护栏：backend/tests/test_global_facts_closeout_20261006.py。 */
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

/** ✅ 2026-10-06：事实属性中文名。权威来源同为 /category-map 的 fact_attr_titles；
 *  下方 FALLBACK_FACT_ATTR_TITLES 仅在端点不可用时兜底（此前是 4 个内联三元）。 */
export const FALLBACK_FACT_ATTR_TITLES: Record<string, string> = {
  quantitative: "定量事实",
  qualitative: "定性事实",
  relation: "关系事实",
  norm: "规范事实",
};

/** ✅ 2026-10-06：数据来源类型中文名。此前后端逐条下发 source_kind、
 *  统计里也有 by_source_kind，但**界面零展示** —— 用户无法分辨一条事实
 *  来自招标文件 / 图纸 / 现场勘察 / 总体方案 / 人工手册，而这正是判断
 *  「AI 编造 vs 资料原文」的关键依据。 */
export const FALLBACK_SOURCE_KIND_TITLES: Record<string, string> = {
  bid_doc: "项目文件解析提取",
  drawing: "施工图设计文件提取",
  survey: "勘察报告提取",
  overall_plan: "施工组织设计提取",
  manual: "用户补充录入",
};

/** 由后端事实条目的 chapter 键取中文标题；空值/未识别的键返回空串（不渲染徽标）。
 *  titles 缺省时用离线兜底表；运行时由 /category-map 覆盖。 */
export function factChapterTitle(chapter?: string | null, titles?: Record<string, string>): string {
  const table = titles && Object.keys(titles).length ? titles : FACT_CHAPTER_TITLES;
  return (chapter && table[chapter]) || "";
}

/** 事实属性键 → 中文名。
 *
 * ✅ 2026-10-06 未识别键**返回空串**（与 factChapterTitle 同口径）。
 * 最初实现回落原键（便于排障），实测会在条目卡上渲染出「🗂 nope」这类噪声 ——
 * 三套枚举里只有属性/来源会这样显示，行为不一致反而更难判断是数据问题还是
 * 界面问题。统一为「不渲染」后，未识别值只会在原始 JSON 里出现。 */
export function factAttrTitle(attr?: string | null, titles?: Record<string, string>): string {
  if (!attr) return "";
  const table = titles && Object.keys(titles).length ? titles : FALLBACK_FACT_ATTR_TITLES;
  return table[attr] || "";
}

/** 数据来源类型键 → 中文名；未识别键同 factChapterTitle 返回空串。 */
export function factSourceKindTitle(kind?: string | null, titles?: Record<string, string>): string {
  if (!kind) return "";
  const table = titles && Object.keys(titles).length ? titles : FALLBACK_SOURCE_KIND_TITLES;
  return table[kind] || "";
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
export function applyFactsFilter<T extends FactGroupLike>(
  groups: T[] | null | undefined,
  filter: string,
): (T & { filteredItems: FactItemLike[] })[] {
  const out: (T & { filteredItems: FactItemLike[] })[] = [];
  for (const g of groups || []) {
    let items: FactItemLike[] = g.items || [];
    if (filter === "simulated") items = items.filter((it) => it.is_simulated);
    else if (filter === "conflict") items = items.filter((it) => it.has_conflict);
    else if (filter === "unresolved") items = items.filter((it) => !it.is_resolved);
    if (filter !== "all" && items.length === 0) continue;
    out.push({ ...g, filteredItems: items });
  }
  return out;
}

// ============================================================
// ✅ R75 · G-D（2026-10-13）事实诊断「单侧失败不丢另一侧成果」：
//    loadFactsDiagnostics 旧实现用 Promise.all 拉「章节覆盖度」与「危大阈值」
//    两个**互相独立**的诊断 —— 任一 reject 即整体 reject，另一侧已到达的
//    成果连带丢弃、两个面板一起空白。生产实证形态：danger-check 对空方案
//    会 400（后端 P0 修复注释里记录过），此时 chapters 明明 200 返回，
//    界面却两个面板都空 + 只有一条「加载事实诊断失败」。
//    现改 Promise.allSettled 独立落定；合并判定抽成纯函数供组件级测试
//    （沿用 applyFactsFilter 惯例，API 调用留在页面层）。
// ============================================================
/**
 * ✅ BUG-06 前端半边（R176 · 2026-11-04）：事实列表首个请求的页码裁决。
 *
 * 切方案时 `/scheme/:id` 是同一路由组件，组件不卸载 → `factsPageRef` 保留旧页码，
 * 新方案首屏带着旧 `offset` 去请求；越界后即便后端已做 clamp，看到的仍是
 * 「新方案事实全没了」的假象。根因必须在**首个请求**就落回第 1 页（offset=0）。
 *
 * 判据：本次请求的方案 ≠ 上一次已请求的方案 ⇒ 强制第 1 页；
 * 同方案翻页仍走 override → ref 的原顺序（零回归）。
 */
export function resolveFactsLoadPage(input: {
  schemeId: string;
  lastSchemeId: string;
  pageOverride?: number;
  refPage: number;
}): number {
  if ((input.schemeId || "") !== (input.lastSchemeId || "")) return 1;
  return Math.max(1, Number(input.pageOverride) || input.refPage);
}

/**
 * ✅ 诊断报告落板的**唯一判定源**（R176/R177 · 2026-11-04）。
 *
 * 规则：`undefined`（请求失败/键缺席）与 `null`（成功但无数据）一律视为
 * 「无法判定」⇒ 返回 `null`，面板据此渲染「无法判定 / 无数据」。
 *
 * ⚠️ 语义说明（勿当 bug 改回）：`mergeFactsDiagnosticsSettled` 对 fulfilled
 * 但 `data` 为 `undefined` 的响应同样**不设置**对应键（与失败同形）。这是
 * **有意**的：成功但无数据 = 无法判定，而不是「沿用上一次结论」。任何一侧
 * 都不得再写 `if (merged.x !== undefined) set(...)` 式的条件赋值。
 */
function diagnosticReportOrNull<T>(value: T | null | undefined): T | null {
  return value === undefined ? null : (value ?? null);
}

/** 危大/超规模报告落板（与章节覆盖度同源，仅按侧取键）。 */
export function resolveFactsDangerSettled<T>(merged: { danger?: T | null }): T | null {
  return diagnosticReportOrNull(merged.danger);
}

/** 章节覆盖度报告落板（与危大侧同源；刷新失败同样清空旧覆盖率）。 */
export function resolveFactsChapterSettled<T>(merged: { chapters?: T | null }): T | null {
  return diagnosticReportOrNull(merged.chapters);
}

export function mergeFactsDiagnosticsSettled<TChapters = unknown, TDanger = unknown>(
  chaptersRes: PromiseSettledResult<{ data?: TChapters }>,
  dangerRes: PromiseSettledResult<{ data?: TDanger }>,
): {
  chapters?: TChapters | null;
  danger?: TDanger | null;
  warning: string | null;
  aborted: boolean;
} {
  const isAbort = (e: unknown) =>
    (e as { name?: string } | null | undefined)?.name === "AbortError";
  const out: {
    chapters?: TChapters | null;
    danger?: TDanger | null;
    warning: string | null;
    aborted: boolean;
  } = { warning: null, aborted: false };
  const failed: unknown[] = [];
  if (chaptersRes.status === "fulfilled") out.chapters = chaptersRes.value?.data;
  else failed.push(chaptersRes.reason);
  if (dangerRes.status === "fulfilled") out.danger = dangerRes.value?.data;
  else failed.push(dangerRes.reason);
  const realFailures = failed.filter((e) => !isAbort(e));
  // 双侧皆失败且均/含 AbortError（非真实错误）：整个请求作废，静默返回（与旧行为一致）
  if (failed.length === 2 && failed.some(isAbort) && realFailures.length === 0) {
    return { warning: null, aborted: true };
  }
  if (realFailures.length > 0) {
    const e = realFailures[0] as { response?: { data?: { detail?: string } }; message?: string };
    out.warning = e?.response?.data?.detail || e?.message || "加载事实诊断失败";
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
  groups: FactGroupLike[] | null | undefined,
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
export function filterFactsGroupsByCategory<T extends FactGroupLike>(
  groups: T[] | null | undefined,
  category: string,
): T[] {
  if (!category) return groups || [];
  return (groups || []).filter((g) => ((g?.category || "other").trim() || "other") === category);
}

/**
 * 左侧「事实分类」面板（与目录树面板同宽同风格，仅全局事实 Tab 显示）。
 * 纯展示组件：选中态与点击全部回调化。
 */
