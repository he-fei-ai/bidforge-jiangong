// AUTO-GENERATED — DO NOT EDIT
// Source: backend/app/services/checkpoint_schema.py
// Regenerate: python tools/generate_checkpoint_schema.py

export interface outline_result {
  event?: string;
  partial?: boolean;
  outline?: unknown[];
  review?: Record<string, unknown>;
  failed_chapters?: string[];
  failed_count?: number;
}

export interface content_result {
  event?: string;
  message?: string;
  done?: number;
  total?: number;
  failed_count?: number;
  failed_sections?: unknown[];
  words?: number;
  word_count?: number;
  run_words?: number;
  over_count?: number;
  word_audit?: Record<string, unknown>;
  fact_autofix_summary?: Record<string, unknown>;
}

export interface facts_result {
  event?: string;
  message?: string;
  segment_stats?: Record<string, unknown>;
  cross_conflicts?: unknown[];
  warnings?: string[];
  group_count?: number;
  total_items?: number;
}

export interface TaskTerminalInfo {
  status: string;
  message?: string;
  progress?: number;
  outline_result?: outline_result;
  content_result?: content_result;
  facts_result?: facts_result;
  [k: string]: unknown;
}