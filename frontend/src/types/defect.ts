/**
 * 统一缺陷账本契约（R179 Phase C）。
 *
 * 字段名逐字对应 `backend/app/services/defect_ledger.py` 与
 * `backend/app/routers/defects.py`。三态口径：``passed`` 只统计 verdict=yes；
 * ``undetermined`` 必须单列展示，绝不能并入「通过」。
 */

/** 检查结论：yes 通过 / no 不通过 / undetermined 无法判定 / pending 尚未检查 */
export type DefectVerdict = "yes" | "no" | "undetermined" | "pending";

/** 缺陷状态：open 待处理 / suggested 已有建议 / applied 已执行 / dismissed 已驳回 */
export type DefectStatus = "open" | "suggested" | "applied" | "dismissed";

/** free = 可自动执行（apply）；protected = 命中禁改白名单，只能人工裁决 */
export type FixPolicy = "free" | "protected";

/** 人工裁决动作（POST /defects/{id}/decision） */
export type DefectDecision = "accept" | "reject" | "defer" | "manual";

export interface DefectItem {
  id: string;
  project_id: string;
  scheme_id: string;
  source_kind: string;
  source_run_id: string;
  source_table: string;
  rule_id: string;
  dimension: string;
  severity: string;
  title: string;
  detail: string;
  section_id: string;
  section_title: string;
  before_text: string;
  after_text: string;
  suggestion: string;
  fix_policy: FixPolicy;
  /** 命中的禁改白名单类别（后端 JSON 解析后下发） */
  policy_categories: string[];
  status: DefectStatus | string;
  verdict: DefectVerdict | string;
  verdict_reason: string;
  fingerprint: string;
  created_at: string;
  updated_at: string;
  /** ✅ R227（D2）：指向的章节已被删除（保留账本留痕，显示层标「章节已删除」）。
   * 后端 `_serialize` 经 `SELECT *` 透传；缺省 0 = 未失效（旧响应无此键）。 */
  stale?: number;
}

export interface DefectSummary {
  total: number;
  passed: number;
  failed: number;
  /** 检查器异常 / 未注册 —— 永不计入 passed */
  undetermined: number;
  pending: number;
  /** ✅ R227（D2）：指向已删章节的失效账本行数（加法式，不影响上方计数语义） */
  stale?: number;
  counts: Record<string, number>;
  pass_rule: string;
}

export interface DefectListResponse {
  items: DefectItem[];
  total: number;
}

export interface DefectDiffRow {
  type: "equal" | "delete" | "insert" | "replace";
  before: string;
  after: string;
}

/** GET /defects/{id}/diff → 全文并排比对（不截断） */
export interface DefectDiff {
  before_text: string;
  after_text: string;
  before_chars: number;
  after_chars: number;
  truncated: boolean;
  rows: DefectDiffRow[];
  unified: string;
}

export interface DefectSyncSource {
  source: string;
  source_run_id?: string;
  scanned: number;
  created: number;
  updated: number;
  skipped: boolean;
}

export interface DefectSyncResult {
  scheme_id: string;
  sources: DefectSyncSource[];
  total: number;
}
