/**
 * 审核与预检模块的类型契约。
 *
 * 此前该模块的全部状态都是 `any`（complianceResults: any[]、qualityResult: any、
 * exportIssues: any[] ...），前后端字段一旦不一致只能在运行期"点了没反应"才被发现。
 * 这里把后端 `audit_rules.py` / `preflight_engine.py` / `audit_scoring.py` /
 * `review.py` 的输出结构显式建模，前端据此获得编译期校验。
 */

/** 严重度：block 为交付阻断项（命中即不允许标记可交付） */
export type Severity = "block" | "high" | "medium" | "low";

/** 六个评分维度 */
export type DimensionKey =
  | "completeness"
  | "compliance"
  | "safety"
  | "consistency"
  | "traceability"
  | "deliverability";

/** 检查方式：program=程序化规则（离线秒级）；ai=AI 语义判定 */
export type CheckMode = "program" | "ai";

/** 规则定义（后端 audit_rules.AuditRule 的镜像） */
export interface AuditRule {
  rule_id: string;
  dimension: DimensionKey | string;
  title: string;
  detail: string;
  severity: Severity;
  mode: CheckMode;
  basis: string;
  keywords: string[];
  deprecated: boolean;
}

/** 规则维度定义（含权重） */
export interface AuditDimension {
  key: DimensionKey | string;
  label: string;
  weight: number;
  desc: string;
  rules: string[];
}

/** 单条预检发现 */
export interface PreflightFinding {
  rule_id: string;
  dimension: DimensionKey | string;
  severity: Severity;
  title: string;
  detail: string;
  evidence: string[];
  section_id: string;
  section_title: string;
  suggestion: string;
  basis: string;
  mode: CheckMode;
  // ✅ 缺口修复（2026-09-24）：后端 export.export_issues_to_findings 产出的
  //    导出预检类发现会带 section_ids / count / source 三个字段（同类问题
  //    聚合为一条），前端此前未声明 → 只能当 any 透传，"涉及 N 处"无法展示。
  /** 同类问题涉及的章节 id（聚合项，单条为空） */
  section_ids?: string[];
  /** 聚合条数 */
  count?: number;
  /** 来源（如 export_check / preflight / ai_compliance …） */
  source?: string;
  /**
   * ✅ 自动修复能力（后端 `services/review_autofix.py` 标注）。
   * `mode`：`auto` = 纯程序化确定性修复（不调 AI）/ `ai` = 定位后调 AI 改写 /
   * `manual` = 不支持自动修复，此时 `reason` 给出用户可执行的下一步。
   * 前端**必须**据此决定按钮状态，不得自行按 rule_id 猜测。
   */
  autofix?: AutoFixCapability;
}

/** 自动修复能力声明 */
export interface AutoFixCapability {
  mode: "auto" | "ai" | "manual";
  /** auto / ai 为 true；manual 为 false */
  fixable: boolean;
  /** manual 时给用户可执行的替代路径（AI 模式下为空） */
  reason: string;
}

/** 矛盾位置（章节 + 行号 + 句子 + 原文上下文） */
export interface AutoFixTarget {
  section_id: string;
  section_title: string;
  /** 命中的取值 / 术语 / 编号（section 型为空） */
  value: string;
  /** 1-based 行号 */
  line: number;
  /** ✅ 句子级定位：命中句在段落内的序号（1-based；section 型为 0） */
  sentence_idx: number;
  /** 所在段落的句子总数 */
  sentence_total: number;
  matched: string;
  context: string;
  why: string;
}

/** 定位预览结果（POST /autofix/plan） */
export interface AutoFixPlanResult {
  ok: boolean;
  fixable: boolean;
  mode: "auto" | "ai" | "manual";
  reason: string;
  finding: PreflightFinding;
  targets: AutoFixTarget[];
  max_sections?: number;
}

/** 单章修复结果 */
export interface AutoFixItem {
  section_id: string;
  section_title: string;
  status: "repaired" | "failed";
  targets: AutoFixTarget[];
  before: string;
  after: string;
  problems: string[];
}

/** 修复执行结果（POST /autofix/apply） */
export interface AutoFixResult {
  ok: boolean;
  status: "repaired" | "failed" | "unsupported" | "not_located";
  mode: "auto" | "ai" | "manual";
  rule_id: string;
  reason?: string;
  targets: AutoFixTarget[];
  items: AutoFixItem[];
  /** 回滚凭据（成功修复时非空） */
  snapshot_id: string;
  repair_id?: string;
  stats?: { repaired: number; failed: number; skipped: number };
}

/** 批量暂存中的单条结果（POST /autofix/stage 的 items 元素） */
export interface AutoFixStageItem {
  rule_id: string;
  section_id: string;
  section_title: string;
  mode: "auto" | "ai" | "manual";
  status: "repaired" | "failed" | "unsupported" | "not_located";
  reason?: string;
  targets: AutoFixTarget[];
  /** 该问题改写前的章节正文（截断预览） */
  before: string;
  /** 该问题改写后的章节正文（链式合并视角；截断预览） */
  after: string;
  problems: string[];
  /** 同章内链式顺序（0-based），供「接受前缀」直接取末条合并结果 */
  chain_index: number;
  sentence_idx: number;
  sentence_total: number;
}

/** 只读收集结果（POST /autofix/collect） */
export interface AutoFixCollectResult {
  scheme_id: string;
  scope: "all_blocking" | "auto_fixable" | "all";
  total: number;
  items: PreflightFinding[];
  content_fingerprint?: string;
  stale?: boolean;
}

/** 批量暂存结果（POST /autofix/stage） */
export interface AutoFixStageResult {
  batch_id: string;
  items: AutoFixStageItem[];
  stats: { repaired: number; failed: number; skipped: number };
  status: "pending_confirm" | "empty";
  reason?: string;
}

/** 确认结果（POST /autofix/confirm） */
export interface AutoFixConfirmResult {
  status: "confirmed" | "rejected";
  accepted: number;
  repaired_sections: number;
  snapshot_id: string;
  batch_id: string;
  skipped?: Array<{ rule_id: string; status: number; detail: string }>;
}

/** 维度得分 */
export interface DimensionScoreItem {
  key: DimensionKey | string;
  label: string;
  weight: number;
  score: number;
  penalty: number;
  issue_count: number;
  block_count: number;
}

/** 严重度计数 */
export interface SeverityCounts {
  block: number;
  high: number;
  medium: number;
  low: number;
  total: number;
}

/** 客观性统计（字数 / 章节 / 图表） */
export interface PreflightStats {
  section_count: number;
  leaf_count: number;
  generated_count: number;
  total_words: number;
  word_budget: number;
  empty_ratio: number;
  chart_total: number;
  chart_done: number;
  standard_db_version?: string;
  standard_db_checked_at?: string;
  /** ✅ G1：本次总检里来自导出预检（export_check）的命中数（仅 /overview 返回） */
  export_issue_count?: number;
}

/** 就绪度总览（一键总检的返回体） */
export interface ReadinessOverview {
  scheme_id: string;
  scheme_name: string;
  /** 综合评分 0-100 */
  total: number;
  /** A / B / C / D */
  grade: string;
  verdict: string;
  /** 是否建议放行交付 */
  released: boolean;
  blocked: boolean;
  blockers: Array<{
    rule_id: string;
    title: string;
    detail: string;
    suggestion: string;
    section_title: string;
  }>;
  weakest: string;
  rule_version: string;
  dimensions: DimensionScoreItem[];
  counts: SeverityCounts;
  findings: PreflightFinding[];
  stats: PreflightStats;
  /** 本次评分纳入的数据来源 */
  sources: string[];
  created_at?: string;
  // ✅ G2/G3（2026-09-21）：总检的并发锁 + 幂等缓存 + 结论时效
  /** true = 命中服务端缓存（同内容指纹下 TTL 内重复请求，未重复落库） */
  cached?: boolean;
  /** true = 本次结论对应的正文 / 图表已发生变化，结论已失效 */
  stale?: boolean;
  /** 本次结论对应的内容指纹（后端用于判定结论是否过期） */
  content_fingerprint?: string;
  // ✅ 缺口修复（2026-09-24）：后端 audit_scoring.ReadinessResult.as_dict 会返回
  //    unknown_dimension_count（AI/程序化发现里出现了六维之外的维度 key，
  //    一律计入 deliverability）。>0 说明规则库与评分口径已漂移，属必须暴露的
  //    数据质量信号，此前前端类型未声明、UI 也无从提示。
  unknown_dimension_count?: number;
}

/** 预检历史（分数趋势） */
export interface PreflightRunItem {
  id: string;
  total: number;
  grade: string;
  verdict: string;
  released: number;
  blocked: number;
  counts: SeverityCounts;
  rule_version: string;
  created_at: string;
  /** ✅ G3：本条历史是否已过期（正文 / 图表在其之后被修改） */
  stale?: boolean;
  /** ✅ G5：所属项目（跨方案的项目级审计追溯） */
  project_id?: string;
}

/** 审核状态机 */
export type ReviewStatus = "" | "pending" | "reviewing" | "approved" | "rejected";

export interface ReviewSummary {
  /** 编译状态（草稿/目录已确认）—— 2026-09-17 与审核状态解耦后语义收窄 */
  scheme_status: string;
  /** 人工审核状态（pending/reviewing/approved/rejected）—— 独立列，不再占用 scheme_status */
  review_status: string;
  review_status_label?: string;
  scheme_name: string;
  counts: Record<string, number>;
  labels: Record<string, string>;
  total_sections: number;
  generated_sections: number;
  approved_sections: number;
  /** 通过率口径：approved / total */
  progress: number;
  /** 已进入审核流的章节数（含审核中/通过/驳回，后端 2026-09-21 补字段） */
  reviewed_sections?: number;
  /** 已纳入审核流的比例（reviewed / total） */
  reviewed_progress?: number;
  /** 已通过的比例（approved / total，与 progress 同口径的显式别名） */
  approved_progress?: number;
  reviewed_all: boolean;
  records: ReviewRecord[];
}

export interface ReviewRecord {
  id: string;
  section_id: string;
  section_title: string;
  from_status: string;
  to_status: string;
  reviewer: string;
  comment: string;
  created_at: string;
  from_label?: string;
  to_label?: string;
}

export interface ReviewChecklistItem {
  id: string;
  title: string;
  level: number;
  parent_id: string;
  word_count: number;
  review_status: ReviewStatus;
  review_status_label: string;
  last_reviewer: string;
  last_comment: string;
  last_reviewed_at: string;
}

export const SEVERITY_LABEL: Record<Severity, string> = {
  block: "阻断",
  high: "严重",
  medium: "一般",
  low: "提示",
};

export const SEVERITY_COLOR: Record<Severity, string> = {
  block: "volcano",
  high: "red",
  medium: "orange",
  low: "blue",
};

/** 严重度排序权重（降序） */
export const SEVERITY_WEIGHT: Record<string, number> = {
  block: 3,
  high: 2,
  medium: 1,
  low: 0,
};

export const REVIEW_STATUS_COLOR: Record<ReviewStatus, string> = {
  "": "default",
  pending: "default",
  reviewing: "processing",
  approved: "success",
  rejected: "error",
};

export const GRADE_COLOR: Record<string, string> = {
  A: "#52c41a",
  B: "#1677ff",
  C: "#fa8c16",
  D: "#ff4d4f",
};

export const SCORE_COLOR: Record<DimensionKey | string, string> = {
  completeness: "#1677ff",
  compliance: "#722ed1",
  safety: "#fa541c",
  consistency: "#13c2c2",
  traceability: "#2f54eb",
  deliverability: "#52c41a",
};
