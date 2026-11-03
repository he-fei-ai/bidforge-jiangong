

// ✅ any 清理（2026-11-02）：目录质量载荷契约（按本函数实际消费字段推导，
//    与后端 outline_quality 结构对齐；未知字段一律 ? 修饰，不虚构）
type OutlineQualityPayload = {
  quality?: {
    name_coverage?: { evaluated?: unknown; missing?: unknown[] };
    continuity?: { ok?: unknown; issue_counts?: Record<string, number> };
    redundant_titles?: unknown[];
  };
};

export function summarizeOutlineQuality(review: unknown): {
  level: "warn" | "info" | "none";
  message: string;
  logs: string[];
} {
  const empty = { level: "none" as const, message: "", logs: [] as string[] };
  const q = (review as OutlineQualityPayload | null | undefined)?.quality;
  if (!q || typeof q !== "object") return empty;

  const logs: string[] = [];
  let warn = false;

  const missing: string[] = Array.isArray(q.name_coverage?.missing)
    ? q.name_coverage.missing.filter((m: unknown): m is string => typeof m === "string")
    : [];
  if (q.name_coverage?.evaluated && missing.length > 0) {
    warn = true;
    const head = missing.slice(0, 5).join("、");
    const more = missing.length > 5 ? ` 等 ${missing.length} 项` : "";
    logs.push(`⚠️ 方案名称涉及但目录未覆盖：${head}${more}`);
  }

  const cont = q.continuity;
  if (cont && cont.ok === false) {
    warn = true;
    const c: Record<string, number> = cont.issue_counts || {};
    // ⚠️ 这张表必须与后端 `outline_quality.CONTINUITY_ISSUE_LABELS` **同步**
    //    （唯一事实源，跨语言 parity 由
    //    tests/test_outline_title_fix_chain_r73b_20261012.py 锁定）。
    //    漏一类的后果是**静默的**：下面的 Object.keys(labels) 会把该类计数直接
    //    过滤掉，后端已判 ok=false 而界面零提示 —— R73b F5 实证 `generic_titles`
    //    （占位/泛化标题，正是用户报的「专项6」「总体实施策划（2）」）漏了三天。
    const labels: Record<string, string> = {
      level_gaps: "层级跳级",
      empty_parents: "有父无子",
      duplicate_titles: "同名章节",
      numbering_mismatch: "编号错位",
      dash_titles: "破折号长尾标题",
      empty_titles: "空标题节点",
      generic_titles: "占位/泛化标题",
    };
    const parts = Object.keys(labels)
      .filter((k) => (c[k] || 0) > 0)
      .map((k) => `${labels[k]} ${c[k]} 处`);
    // 兜底：后端新增了问题类而这张表还没跟上时，也必须把计数透出来（宁可显示
    // 英文裸代号，不可把已检出的问题咽掉）。
    const unlabeled = Object.keys(c).filter((k) => !(k in labels) && (c[k] || 0) > 0);
    if (unlabeled.length)
      parts.push(`其它问题 ${unlabeled.map((k) => `${k} ${c[k]} 处`).join("、")}`);
    if (parts.length) logs.push(`⚠️ 目录连续性问题：${parts.join("、")}`);
  }

  // ⚠️ 脏数据防御：后端只保证是数组，元素仍可能是 null/非对象（旧数据或第三方
  //    端点塞入），直接读 .title 会把整条提示抛在渲染路径上。
  const redundant: unknown[] = Array.isArray(q.redundant_titles) ? q.redundant_titles : [];
  if (redundant.length > 0) {
    const head = redundant
      .slice(0, 5)
      .map((r) => ((r as { title?: string } | null)?.title) || "")
      .filter(Boolean)
      .join("、");
    if (head) logs.push(`ℹ️ 疑似与方案名称无关的章节（未自动删除，请确认）：${head}`);
  }

  if (logs.length === 0) return empty;
  return { level: warn ? "warn" : "info", message: logs.join("；"), logs };
}

/**
 * 后端目录审核载荷（`review.review_mode` / `review.suggestions`）→ 面向用户的
 * 一句审核依据 + 逐条已执行动作（纯函数，可单测）。
 *
 * 背景（R73b 遗留 #1 / R74 G1）：`programmatic*` 与 `programmatic+titled/surgical`
 * 的区分此前只进后端日志，用户在界面看不到「这次结论的程序化程度」；
 * suggestions（已执行的补救动作说明）前端也一直没有消费方 —— 后端每次
 * 程序化出口都带了，前端静默丢。
 * 属呈现增强、不改任何判定：只进生成日志，不弹 toast（避免与 quality
 * 告警叠加成噪音）。
 *
 * review_mode 值域（唯一来源：后端 sse_handlers._review_and_fix_outline）：
 * - `programmatic`                    覆盖全过，跳过 AI 审核
 * - `programmatic+titled`             另含占位标题外科式重写
 * - `programmatic+surgical[+titled]`  另含外科式补齐缺失章节
 * - 不写 / 非 programmatic 开头        走了完整 AI 审核链路（含审核超时/失败
 *   回退场景，此时 suggestions 里会有「审核超时已跳过」等如实文案）
 */

export const DEFAULT_COMPLIANCE_CHECKLIST = [
  "是否引用了现行有效的规范、标准与设计文件",
  "工程概况与周边环境描述是否完整（地质、水文、邻近建构筑物）",
  "施工工艺流程是否覆盖全部工序且有可执行步骤",
  "关键工序是否给出设计计算书与验算依据",
  "材料与构配件规格、强度等级是否明确",
  "安全保证措施是否覆盖主要风险源且有针对性",
  "应急处置措施是否含组织机构、物资、演练要求",
  "人员分工与岗位职责是否明确到岗到人",
  "施工进度计划与劳动力配置是否匹配",
  "验收要求与检验批划分是否符合规范",
];

// ===== 全文一致性 Agent 修复：状态/类型中文标签（与后端 conflict_type 对齐）=====
export const CR_TYPE_LABEL: Record<string, string> = {
  numeric: "数值",
  param: "参数",
  person: "人名",
  model: "型号",
  timeline: "时间口径",
  commitment: "承诺口径",
  duplication: "章节重复",
  facts: "与事实不符",
  design: "与设计不符",
  standard: "与规范不符",
};
export const CR_STATUS_LABEL: Record<string, string> = {
  pending: "待修复",
  repaired: "已修复待确认",
  accepted: "已接受",
  skipped: "待人工确认",
  failed: "修复失败",
};
export const CR_ITEM_STATUS: Record<string, string> = {
  repaired: "已修复",
  skipped: "跳过",
  failed: "失败",
};
export const CR_REPAIR_STATUS: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  partially_confirmed: "部分确认",
  rejected: "已拒绝",
  rolled_back: "已回滚",
};

/** 全局事实分类选项（与后端 CATEGORY_TITLES 对齐，用于手工录入/编辑时归类） */
