/**
 * 正文生成 · 生成标准（精准内容 / 模糊内容）纯函数层
 *
 * 承载「UI 选择 → SSE 请求体」的唯一映射口径。
 *
 * ✅ 缺口修复（2026-09-26 · F1「选项未生效、两选项无差异」）：
 *    此前页面虽持有 `generationStandard` state 并渲染了 Radio，但
 *    `handleGenerateContent` / `useContentGeneration` 只读 `extra.task_standard`，
 *    而**所有调用方都不传** → 请求体永远不含标准字段，
 *    后端一律走默认 precise。结果：用户切「模糊内容」后生成结果与精准**完全一致**。
 *    现把映射收敛到本文件纯函数，页面与 hook 共用，杜绝两处各写一遍再次漂移。
 *
 * 口径与后端 `app/services/content_standard.py::resolve_effective_standard` 对齐：
 *   precise / fuzzy  → 传 task_standard + override_section_standard=true（强覆盖）
 *   inherit（按章节设置）→ **不传值** + override_section_standard=false（逐章回落）
 */

/** 任务级三选一：按章节设置 / 精准内容 / 模糊内容 */
export type GenerationStandardChoice = "inherit" | "precise" | "fuzzy";

/** 后端值域（与后端 PRECISE / FUZZY 常量一致） */
export type GenerationStandard = "precise" | "fuzzy";

/** 面向用户的中文名（与后端 STANDARD_LABELS 保持一致） */
export const STANDARD_LABELS: Record<GenerationStandard, string> = {
  precise: "精准内容",
  fuzzy: "模糊内容",
};

/** 是否合法标准值（脏数据/旧库空串一律视为未设置） */
export function normalizeStandard(v: unknown): GenerationStandard | "" {
  return v === "precise" || v === "fuzzy" ? v : "";
}

/**
 * 把 UI 三选一映射为 SSE 请求体字段。
 *
 * - `inherit`（按章节设置）：返回 `{}` —— **不能**返回 override=false，
 *   保持与「未传任何新字段」的旧客户端逐字节一致（NFR-3 向后兼容）。
 * - `precise` / `fuzzy`：返回强覆盖两字段。
 */
export function buildStandardRequestFields(choice: GenerationStandardChoice | undefined): {
  task_standard?: GenerationStandard;
  override_section_standard?: boolean;
} {
  const std = choice === "precise" || choice === "fuzzy" ? choice : "";
  if (!std) return {};
  return { task_standard: std, override_section_standard: true };
}

/**
 * 章节级下拉选项文案：`inherit` 需要把方案默认翻译出来 */
export function sectionStandardOptionLabel(choice: string, schemeStandard: unknown): string {
  if (choice === "precise") return STANDARD_LABELS.precise;
  if (choice === "fuzzy") return STANDARD_LABELS.fuzzy;
  const std = normalizeStandard(schemeStandard) || "precise";
  return `沿用方案（${STANDARD_LABELS[std]}）`;
}

/**
 * 章节实际生效标准：章节为空时回落方案默认，再回落精准（与后端
 * `resolve_effective_standard` 的 ②③ 步同口径）。用于卡片上「本章将按 X 生成」的提示。
 */
export function effectiveSectionStandard(
  sectionStandard: unknown,
  schemeStandard: unknown,
): GenerationStandard {
  return normalizeStandard(sectionStandard) || normalizeStandard(schemeStandard) || "precise";
}

/** 校验报告里的问题类型 → 中文标签（前端展示用，后端只给 type 英文码） */
export const STANDARD_ISSUE_LABELS: Record<string, string> = {
  fuzzy_expression: "模糊表述",
  fact_value_missing: "事实未引用",
  value_mismatch: "数值不一致",
  value_conflict: "数值与事实冲突",
  model_conflict: "型号与事实冲突",
  /**
   * ✅ R211（2026-11-10 · P1）：未闭合围栏「吞正文」告警。
   * 后端 `content_standard.FENCE_SWALLOWED_CODE` 用的是 **`code`** 键（R181
   * 统一后的载荷形状），而本表的既有条目都是 `type` 键 —— 前端只读 `type` 时
   * 该告警的标签为空串 ⇒ 日志 Popover 里出现一个**无文字的色块**，用户看不到
   * 「N 行疑似正文被困在代码块内」，R179 FIX-3 / R181 两轮修复的用户可见效果
   * 被前端断链吃掉。
   */
  fence_swallowed_prose: "未闭合围栏吞正文",
};

/**
 * 报告问题条目的展示文案（缺失类型时原样回退，避免空白）。
 *
 * ✅ R211：**双键兜底** —— 后端 issue 的键名有两种：
 *   · `type`：既有校验问题（模糊表述 / 数值不一致 …）；
 *   · `code`：R181 统一的围栏吞正文告警。
 * 只读 `type` 会让后者渲染成空标签（见 STANDARD_ISSUE_LABELS 注释）。
 */
export function issueLabel(
  issue: { type?: string; code?: string } | null | undefined,
): string {
  const t = issue?.code || issue?.type || "";
  return STANDARD_ISSUE_LABELS[t] || t;
}

/** 校验报告的消费子集（按本文件实际读取字段推导；兼容 null/标量等脏输入） */
type ReportIssueCounts = {
  error_count?: number;
  warning_count?: number;
  issues?: unknown[];
};

/** 终态事件 standard_summary 载荷（按本文件实际读取字段推导） */
type StandardSummaryPayload = {
  issue_sections?: number;
  total_issues?: number;
  precise?: number;
  fuzzy?: number;
};

/** 报告是否含问题（error 或 warning 都算，供告警 Tag 显隐） */
export function reportHasIssues(report: unknown): boolean {
  if (!report || typeof report !== "object") return false;
  const r = report as ReportIssueCounts;
  const n = (r.error_count || 0) + (r.warning_count || 0);
  if (n > 0) return true;
  return Array.isArray(r.issues) && r.issues.length > 0;
}

/** 报告问题总数（告警 Tag 上的数字） */
export function reportIssueCount(report: unknown): number {
  if (!report || typeof report !== "object") return 0;
  const r = report as ReportIssueCounts;
  const n = (r.error_count || 0) + (r.warning_count || 0);
  return n || (Array.isArray(r.issues) ? r.issues.length : 0);
}

/**
 * 终态汇总 → 追加到完成消息的提示语。
 * 有问题才说话；无问题返回空串（调用方不追加，避免"一切正常"式噪音）。
 */
export function standardSummaryHint(summary: unknown): string {
  if (!summary || typeof summary !== "object") return "";
  const s = summary as StandardSummaryPayload;
  const issueSections = Number(s.issue_sections || 0);
  const total = Number(s.total_issues || 0);
  if (issueSections <= 0 || total <= 0) return "";
  const precise = Number(s.precise || 0);
  const fuzzy = Number(s.fuzzy || 0);
  const parts: string[] = [];
  if (precise) parts.push(`精准 ${precise} 章`);
  if (fuzzy) parts.push(`模糊 ${fuzzy} 章`);
  const modeTxt = parts.length ? `（${parts.join(" / ")}）` : "";
  return `生成标准校验：${issueSections} 章存在 ${total} 处问题${modeTxt}`;
}
