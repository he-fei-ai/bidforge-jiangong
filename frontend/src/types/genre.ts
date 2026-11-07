/**
 * 文种标签契约（R179 Phase A 后端 `GenreTag.to_dict()` 的前端镜像）。
 *
 * 字段名逐字对应 `backend/app/services/scheme_classification.py::GenreTag.to_dict`。
 * 三态口径：``hazard`` / ``oversize`` 只能是 yes / no / undetermined ——
 * 缺参数时后端输出 undetermined，前端不得把它显示成「非危大」。
 */

export type TriStateValue = "yes" | "no" | "undetermined";

export interface GenreRuleTrace {
  layer: number;
  rule_id: string;
  sub_id?: string;
  conclusion?: TriStateValue;
  oversize_conclusion?: TriStateValue;
  evidence?: string[];
  oversize_evidence?: string[];
  missing_params?: string[];
  /** 第三层（或 LLM 建议）记录可能带额外字段，保持兼容而不丢展示。 */
  text?: string;
  clause_ref?: string;
  source?: string;
}

export interface GenreTag {
  scheme_name: string;
  category_ids: string[];
  category_names: string[];
  sub_ids: string[];
  sub_names: string[];
  hazard: TriStateValue;
  oversize: TriStateValue;
  missing_params: string[];
  template_key: string;
  labels: string[];
  needs_calc: boolean | null;
  expert_review_required: boolean | null;
  /** rules = 规则引擎；llm_suggested = 规则零命中后的 LLM 类别建议 */
  source: string;
  rule_trace: GenreRuleTrace[];
  layer3: GenreRuleTrace[];
}

/** GET/POST /projects/{project_id}/schemes/{scheme_id}/genre* 的响应体 */
export interface GenreResponse {
  enabled: boolean;
  genre: GenreTag | null;
}
