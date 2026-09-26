export interface PromptVariable {
  name: string;
  dunder: boolean;
}

export interface PromptVariableDiff {
  added: string[];
  removed: string[];
}

export interface PromptItem {
  key: string;
  category: string;
  label: string;
  content: string;
  default_content: string;
  variables: string[];
  modified: boolean;
  content_hash: string;
  updated_at: string;
  audit_count: number;
}

export interface PromptListResponse {
  items: PromptItem[];
}

export interface PromptMutationResponse {
  ok: boolean;
  content: string;
  content_hash: string;
  reset?: boolean;
  variables?: string[];
  added_variables: string[];
  removed_variables: string[];
}

export interface PromptAuditLog {
  id: string;
  prompt_key: string;
  action: "update" | "reset" | "rollback" | string;
  /** 动作中文标签（后端 PROMPT_AUDIT_ACTIONS 提供） */
  action_label?: string;
  before_hash: string;
  after_hash: string;
  variables_before: string[];
  variables_after: string[];
  client_ip: string;
  created_at: string;
  /**
   * ✅ G2 版本回滚（2026-09-24）：结构化变更摘要（字数变化 + 变量增删）。
   * 历史行（引入快照前写入）无快照 → 空数组。
   */
  changes?: { field: string; label: string; before: unknown; after: unknown }[];
  /** 是否可回滚（只有带「变更前」正文快照的记录才为 true） */
  rollbackable?: boolean;
}

export interface PromptAuditLogsResponse {
  items: PromptAuditLog[];
  total: number;
  limit: number;
  offset: number;
  /** 动作 → 中文标签映射（供前端筛选/展示） */
  actions?: { value: string; label: string }[];
}

/** ✅ G2 版本回滚：POST /api/v1/prompts/{key}/rollback 响应 */
export interface PromptRollbackResponse {
  ok: boolean;
  content: string;
  content_hash: string;
  variables?: string[];
  added_variables: string[];
  removed_variables: string[];
  /** 回滚来源的审计行 id（可据此追溯「从哪个版本回滚回来的」） */
  rollback_from_audit?: string;
}

