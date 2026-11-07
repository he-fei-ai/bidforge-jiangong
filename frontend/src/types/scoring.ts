// ✅ R179 M1（2026-11-05）：评分项 → 目录覆盖校验前后端契约类型。
//    与后端 backend/app/routers/scoring.py 的响应形状保持一致（加法式，不改既有类型）。

export interface ScoringItemRow {
  id: string;
  source_doc_id: string;
  source_name: string;
  item_no: string;
  item_name: string;
  score: number | null;
  score_raw: string;
  criteria: string;
  is_primary: number;
  page_ref: string;
  page_num: number | null;
  source_quote: string;
}

export interface ScoringExtractRun {
  run_id: string;
  // empty = 本次解析返回 0 条但已保留上一次活跃快照（与「真的没有评分项」的 success 区分）
  status: "running" | "success" | "partial" | "failed" | "empty";
  docs_total: number;
  docs_ok: number;
  failed_sources: FailedSource[];
  items_count: number;
  error: string;
  updated_at: string;
}

export interface FailedSource {
  kind?: string;
  doc_id: string;
  file_name: string;
  reason: string;
}

export interface ScoringSuggestion {
  title: string;
  level: number;
  parent_id: string | null;
  parent_title: string;
}

export interface MatchedSection {
  section_id: string;
  title: string;
  level: number;
  confidence: number;
  primary: boolean;
}

export interface ScoringCoverageItem {
  id: string;
  item_no: string;
  item_name: string;
  score: number | null;
  score_raw: string;
  score_band: "high" | "mid" | "low";
  is_primary: boolean;
  page_ref: string;
  page_num: number | null;
  source_doc_id: string;
  source_quote: string;
  criteria: string;
  status: "mapped" | "missing";
  confidence: number | null;
  method: "exact" | "contains" | "keyword" | "criteria" | null;
  section_id: string | null;
  section_title: string | null;
  matched_sections: MatchedSection[];
  also_section_ids: string[];
  needs_review: boolean;
  suggestion: ScoringSuggestion | null;
  reason: string;
}

export interface ScoringMissingItem {
  item_id: string;
  item_no: string;
  name: string;
  score: number | null;
  score_raw: string;
  suggestion: ScoringSuggestion | null;
  page_num: number | null;
  page_ref: string;
  reason: string;
}

export interface ScoringExtraNode {
  section_id: string;
  title: string;
  level: number;
  path: string;
  parent_id: string;
  status: "extra";
  reason: string;
}

export interface ScoringOverloadedSection {
  section_id: string;
  title: string;
  matched_item_count: number;
}

export interface ScoringSummary {
  total_items: number;
  mapped: number;
  missing: number;
  extra: number;
  unmatched: number;
  coverage_rate: number | null;
  undetermined_items: number;
}

export interface ScoringCoverageResponse {
  scheme_id: string;
  project_id: string;
  status: "ok" | "partial" | "undetermined";
  undetermined_reason: "no_extraction" | "extraction_failed" | "empty_items" | null;
  extraction: ScoringExtractRun | null;
  summary: ScoringSummary;
  coverage_rate: number | null;
  items: ScoringCoverageItem[];
  missing: ScoringMissingItem[];
  extra: ScoringExtraNode[];
  unmatched: FailedSource[];
  overloaded_sections: ScoringOverloadedSection[];
  diagnostics: {
    run_status: string;
    thresholds: Record<string, number>;
  };
}

export interface ScoringExtractResponse {
  project_id: string;
  run_id: string;
  status: "success" | "partial" | "failed" | "empty";
  items: ScoringItemRow[];
  items_count: number;
  docs_total: number;
  docs_ok: number;
  failed_sources: FailedSource[];
  error: string;
}

export interface ScoringItemsListResponse {
  project_id: string;
  items: ScoringItemRow[];
  run: ScoringExtractRun | null;
  undetermined: boolean;
}
