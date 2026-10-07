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
  /** ✅ 逐章维度统计（此前后端每次下发、前端整体丢弃） */
  by_chapter?: FactChapterStat[];
}

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
