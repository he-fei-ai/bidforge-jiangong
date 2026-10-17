/**
 * 全局事实模块前后端契约类型。
 *
 * ✅ 2026-10-06 新增。此前 `frontend/src/types/` 下**没有任何事实相关类型**，
 * `factsApi` 的 23 个方法全部返回 `Promise<AxiosResponse<any>>`、页面里
 * `facts: any[]` / `factsSummary: any` / `ch: any` —— 整个特性是 `any`。
 *
 * 缺失的代价是**已实证**的：`ClassificationPanel` 读 `ch.completeness`
 * 而后端下发 `field_coverage`，TypeScript 完全无法报错，九大章节进度条
 * 恒显示 0%（该组件此前零测试覆盖）。有了本文件，同类字段名漂移在
 * `tsc --noEmit` 阶段即暴露。
 *
 * 字段名与后端逐字对应：
 *   - routers/global_facts.py（list_facts / list_facts_by_chapters / …）
 *   - services/facts_extractor.py（format_for_frontend → SSE completed 事件）
 *   - services/facts_classification.py（category_map_payload）
 */

// =========================================================================
// 分类映射（GET /api/v1/global-facts/category-map）
// =========================================================================

/** 九大章节（建办质〔2018〕31号），键与 scheme_classification.NINE_CHAPTERS[].key 一致 */
export type FactChapterKey =
  | "overview"
  | "basis"
  | "plan"
  | "technique"
  | "safety"
  | "personnel"
  | "acceptance"
  | "emergency"
  | "calc_drawings";

export interface FactChapterDef {
  order: number;
  key: FactChapterKey;
  title: string;
}

/** 事实属性（正交维度之一） */
export type FactAttrKey = "quantitative" | "qualitative" | "relation" | "norm";

/** 数据来源类型（正交维度之一） */
export type FactSourceKindKey = "bid_doc" | "drawing" | "survey" | "overall_plan" | "manual";

export interface FactCategoryMap {
  chapters: FactChapterDef[];
  category_to_chapter: Record<string, FactChapterKey | "">;
  fact_attr_titles: Record<FactAttrKey, string>;
  source_kind_titles: Record<FactSourceKindKey, string>;
}

// =========================================================================
// 事实条目（GET /api/v1/global-facts → groups[].items[]）
// =========================================================================

export interface FactConflictCandidate {
  value: unknown;
  source?: string;
  confidence?: number;
  /** ✅ 候选值本身的模拟值语义（落库 / 前端回传 / 裁决重算共用） */
  is_simulated?: boolean;
}

export interface FactItem {
  /** 行 id；分组编辑时也接受 group_id（后端两种模式都支持） */
  fact_id: string;
  name: string;
  value: string;
  /** 单位（后端 _display_value 已把单位并入 value，此列单独保留） */
  value_unit?: string;
  category: string;
  fact_type?: string;
  fact_key?: string;
  scope?: "scheme" | "project";
  source?: string;
  source_ref?: unknown;
  confidence?: number;
  /** AI 编造值（⚠️ 模拟值）—— 注入门控 has is_simulated=0 */
  is_simulated: boolean;
  /** 人工已确认 —— 注入门控 has is_resolved=1 */
  is_resolved: boolean;
  /** 存在多来源矛盾待裁决 */
  has_conflict: boolean;
  conflict_values?: FactConflictCandidate[];
  conflict_keys?: string;
  /** 来源资料已变化，停止注入正文与导出 */
  is_stale: boolean;
  is_safety_critical?: boolean;
  /** 九大章节归属（后端派生，键见 FactChapterKey） */
  chapter?: FactChapterKey | "";
  /** ✅ 后端同时下发 chapter_title；前端此前自行硬编码映射（已改为消费本字段） */
  chapter_title?: string;
  fact_attr?: FactAttrKey | "";
  source_kind?: FactSourceKindKey | "";
  is_shared?: boolean;
  shared_chapters?: string[];
  /** 出处可追溯链（此前后端逐条下发、前端刷新后全部丢弃） */
  page_ref?: number | null;
  evidence_kind?: string;
  zone_type?: string;
  norm_group?: string;
  chunk_hash?: string;
  /** ✅ R80：要求 vs 事实口径（''=未区分/fact=本方案口径/requirement=招标
   *  要求原文口径，正文引用须写「满足/响应要求」）。空值不渲染徽标。 */
  value_kind?: "" | "fact" | "requirement";
}

export interface FactGroup {
  id: string;
  title: string;
  category: string;
  content: string;
  items: FactItem[];
  /** applyFactsFilter 产出的过滤后条目（不落库） */
  filteredItems?: FactItem[];
}

// -------------------------------------------------------------------------
// ✅ R89（2026-10-17）组件层的**最小结构**约束
// -------------------------------------------------------------------------
/**
 * 为什么不是直接用 `FactItem`：
 *
 * `FactItem` 要求 `category` 等全部必填字段，而组件（及其既有 20+ 处测试夹具）
 * 按「只声明被断言的字段」构造对象。强行收紧会把测试夹具与断言一起改写 ——
 * 本仓纪律明确指出：**判据过严会逼后人把护栏改松，比判据缺失更糟**。
 *
 * 这里改为「声明组件真正读取的键 + 索引签名放行其余」：
 *   · 读取点获得类型（字段改名/类型漂移可见）；
 *   · 未声明的透传键不被阻断（不制造纯 churn）。
 * 形状正确性由 pytest 侧的跨语言 parity 护栏
 * （`test_global_facts_frontend_parity_r89_20261017.py`）兜住。
 *
 * ⚠️ **刻意不加索引签名**：带索引签名的目标类型会要求源类型（`FactItem` /
 * `FactGroup`）也有索引签名，否则 `FactGroup[] → FactGroupLike[]` 不可赋值 ——
 * 那等于反向要求严格契约也变松。未声明但被读取的键由 `tsc` 直接报出来，
 * 逐个补进下表即可（这正是本文件该有的收敛方向）。
 */
export interface FactItemLike {
  id?: string | number;
  fact_id?: string;
  name?: string;
  value?: string;
  category?: string;
  is_simulated?: boolean;
  is_resolved?: boolean;
  has_conflict?: boolean;
  is_stale?: boolean;
  is_safety_critical?: boolean;
  is_shared?: boolean;
  scope?: string;
  chapter?: string;
  chapter_title?: string;
  fact_attr?: string;
  source_kind?: string;
  value_kind?: string;
  confidence?: number;
  source?: string;
  conflict_values?: FactConflictCandidate[];
}

export interface FactGroupLike {
  id?: string;
  title?: string;
  category?: string;
  content?: string;
  items?: FactItemLike[];
  filteredItems?: FactItemLike[];
}

// =========================================================================
// 统计（GET /api/v1/global-facts → stats）
// =========================================================================

export interface FactCategoryStat {
  category: string;
  title: string;
  items: number;
  groups: number;
  simulated: number;
  conflicts: number;
  unresolved: number;
  safety_critical?: number;
}

export interface FactChapterStat {
  order: number;
  key: FactChapterKey;
  title: string;
  count: number;
  coverage: number;
  missing_fields: string[];
  total_fields?: number;
  covered_fields?: string[];
  fields?: string[];
  items?: FactItem[];
  /**
   * ✅ R89（2026-10-17）：**逐章**共性事实条数（is_shared=1 且 chapter 命中本章）。
   * 顶层 `FactChapterStatBlock.shared_count` 是**全案合计**，两者语义不同、不可混用
   * —— 旧前端读章条目上的该键（不存在）⇒「共性 N」列自引入起恒不显示。
   * 未分类事实的共性只计入顶层合计（无法归到任何一章）。
   */
  shared_count?: number;
}

/**
 * ✅ R89（2026-10-17）：`stats.by_chapter` 的**真实形状是对象而非数组**。
 *
 * ⚠️ 本文件此前把它声明为 `FactChapterStat[]`，前端实现照此写成
 * `Array.isArray(stats.by_chapter) ? … : []` —— **契约与实现同错**，
 * 于是 `tsc --noEmit` 不可能报错，`FactsChapterCoveragePanel` 自 2026-10-06
 * 引入起永久显示「暂无章节统计」，而数据逐请求就在同一份 `stats` 里下发。
 * 该缺陷能长期存活还因为该面板**零组件用例**。
 *
 * 权威出处：`backend/app/routers/global_facts.py::_chapter_stats_for_items`。
 * 判据纪律：类型契约必须**独立核对后端源码**，不能由前端实现反推。
 */
export interface FactChapterStatBlock {
  chapters: FactChapterStat[];
  totals: {
    facts: number;
    uncategorized: number;
    total_fields: number;
    covered_fields: number;
    coverage: number;
    missing_chapters: number;
  };
  by_fact_attr: Record<string, number>;
  by_source_kind: Record<string, number>;
  /** 全案共性事实条数（合计，非逐章） */
  shared_count: number;
}

export interface FactStats {
  total: number;
  simulated: number;
  unresolved: number;
  conflicts: number;
  stale: number;
  safety_critical?: number;
  project_shared?: number;
  simulated_ratio: number;
  by_category: FactCategoryStat[];
  /** ✅ 逐章维度统计（对象包裹，见 FactChapterStatBlock 的形状订正说明） */
  by_chapter?: FactChapterStatBlock;
  /**
   * ✅ R89（2026-10-17）：**前端派生**字段（`buildFactsSummary` 计算），后端
   * 不下发。声明在此以便消费侧 `factsSummary?.has_warnings` 有编译期依据 ——
   * 此前它只存在于实现里、类型上完全失明。
   */
  has_warnings?: boolean;
}

/**
 * ✅ R89：`buildFactsSummary` 的**归一化输出**。
 *
 * 刻意是 `Partial<FactStats>`：该函数本身就是「把后端可能为脏/缺席的计数
 * 归一成 number」的防御层，输入宽松、输出只保证被归一的那些键是数字。
 * 收紧成 `FactStats` 会强迫每个调用方与测试替身补齐全部维度 —— 那等于把
 * 防御逻辑挪到调用方，与本函数存在的目的相反。
 */
export type FactsSummary = Partial<FactStats> & {
  total: number;
  simulated: number;
  unresolved: number;
  conflicts: number;
  stale: number;
  has_warnings: boolean;
};

export interface FactPagination {
  limit: number;
  offset: number;
  total_groups: number;
  returned: number;
}

export interface FactListResponse {
  groups: FactGroup[];
  stats: FactStats;
  pagination: FactPagination;
}

// =========================================================================
// 章节视图（GET /api/v1/global-facts/chapters）
// =========================================================================

export interface FactChaptersResponse {
  chapters: FactChapterStat[];
  uncategorized: FactItem[];
  totals: {
    facts: number;
    fields: number;
    covered: number;
    uncategorized: number;
  };
  by_fact_attr: Record<string, number>;
  by_source_kind: Record<string, number>;
}

// =========================================================================
// 危大诊断（POST /api/v1/global-facts/danger-check）
// =========================================================================

export interface FactDangerClassification {
  category_ids: string[];
  category_names: string[];
  sub_ids: string[];
  sub_names: string[];
  is_hazardous: boolean;
  is_oversize: boolean;
  standards_keys: string[];
  thresholds?: unknown;
}

export interface FactDangerResponse {
  classification: FactDangerClassification;
  threshold_params: Record<string, number>;
  category_id?: string;
  missing_params?: string[];
}

// =========================================================================
// 分段诊断（SSE completed → segment_stats / cross_conflicts）
// =========================================================================

export interface FactSegmentFailure {
  index: number;
  heading: string;
  zone_type?: string;
  reason: string;
  preview?: string;
}

export interface FactSegmentStats {
  total: number;
  ok: number;
  skipped: number;
  failed: number;
  failed_details: FactSegmentFailure[];
  /**
   * ✅ 2026-10-06 新增：模型主动声明未能提取的段（`segment_failed=true`）。
   * 与 `failed` 语义不同 —— 无法与「合法无事实段」区分，故不计入 failed，
   * 但必须可见：这些段的指纹已记入进度表，普通重跑不会重试。
   */
  declared_failed?: number;
  declared_failed_details?: Omit<FactSegmentFailure, "reason">[];
}

export interface FactCrossConflictSide {
  name: string;
  value: unknown;
  source?: string;
  confidence?: number;
}

export interface FactCrossConflict {
  rule_id: string;
  severity: "high" | "medium" | "low";
  conflict_type: string;
  side_a: FactCrossConflictSide;
  side_b: FactCrossConflictSide;
  resolution_hint?: string;
  auto_resolvable: boolean;
}

/** SSE `completed` 事件载荷（services/facts_extractor.format_for_frontend） */
export interface FactsCompletedEvent {
  ok: boolean;
  groups: FactGroup[];
  total_items: number;
  simulated_count: number;
  conflict_count: number;
  cross_conflicts: FactCrossConflict[];
  segment_stats: FactSegmentStats;
  warnings: string[];
}

// =========================================================================
// 写操作返回契约
// =========================================================================

export interface FactResolveResponse {
  ok: boolean;
  /**
   * ⚠️ 形状随端点而异，**不要用严格相等比较**：
   *   - `PATCH /{id}/ack-stale`  → boolean（`true` / `false`）
   *   - `POST /ack-stale`、`POST /batch-resolve` → 影响行数 int
   * 判「本条本就不处于过期状态」请读 `idempotent`（加法式字段，仅 ack-stale 有）。
   */
  changed?: boolean | number;
  /** ✅ 加法式：本条本就不处于过期状态（幂等调用），非阻塞提示用 */
  idempotent?: boolean;
  skipped?: number;
  /** ✅ 后端逐条回传被安全闸门拦下的事实；前端此前只读计数，用户不知「是哪几条」 */
  skipped_safety?: FactItem[];
  skipped_safety_count?: number;
  safety_blocked?: boolean;
  gated?: number;
  is_resolved?: boolean;
  blocked_reason?: string;
  deleted?: number;
  updated_items?: number;
  applied?: { updated: number; deleted: number; added: number };
}
