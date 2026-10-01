/**
 * 「提取项目」(bidAnalysis) Tab 的纯函数层。
 *
 * 为什么单独抽出来：页面（SchemeWorkbenchPage）此前内联了「缺失判定 / 汇总重算 /
 * 13 分组」三套逻辑，其中缺失判定与后端不一致（只认 `content === "未提取到"`，
 * 不 trim、不认 json 项的「全字段没有提及」），会出现「后端汇总判缺失、前端行内
 * 却是绿色 ✓」的前后端分叉；分组则是把后端 GROUPS 硬编码复制了一份。
 *
 * 判定口径（与后端 `bid_analysis_service.is_missing_result` 严格对齐）：
 *   - 空 / 全空白                              → 缺失
 *   - markdown 项内容恰为「未提取到」           → 缺失
 *   - json 项所有字段都是「没有提及」/空        → 缺失
 */

export type BaItemDef = {
  item_id: string;
  label?: string;
  required?: number;
  output_type?: string;
  group?: string;
  sort_order?: number;
};

export type BaStoredItem = {
  item_id: string;
  status?: string;
  content?: string;
  error?: string;
  /** 结果来源：'ai'（AI 提取，默认）| 'manual'（人工校正） */
  source?: string;
  /** 来源位置 JSON（后端提取成功后确定性反查的出处列表；旧库/未匹配时为空） */
  evidence?: string;
  /** 发起本次提取的方案名（/results 附加；结果本体项目级共享） */
  scheme_name?: string;
} & Record<string, any>;

export type BaGroup = {
  /** 分组键（后端 group 字段，如 project_info） */
  key: string;
  /** 分组中文名（后端 GROUPS.label） */
  label: string;
  items: BaItemDef[];
};

export type BaSummary = {
  total: number;
  success: number;
  /** 完成且有有效内容的项数（后端 /results 的 success_valid；旧字段缺省为 success） */
  success_valid?: number;
  errors: number;
  running: number;
  pending: number;
  /** 人工校正的项数（source='manual'） */
  manual_count?: number;
  missing_required: string[];
  all_required_done: boolean;
};

/**
 * SSE `text_stats` 事件载荷：本轮提取规模（项数 × 切段数 ≈ 模型调用次数）。
 * 超长文档此前对用户完全不可见，只能等十几分钟后从账单里发现额度被烧光。
 */
export type BaTextStats = {
  total_chars: number;
  segment_count: number;
  item_count: number;
  est_model_calls: number;
  chunk_size?: number;
  // ===== 提取依据完整性（2026-09-26 起由后端 text_stats / completed 事件透传）=====
  // 截断信号此前只停在后端（parse_truncated 已持久化、_combine_doc_texts 的报告
  // 分支也早写好但从未被调用）—— 用户全程看不到「提取依据不完整」，
  // 会拿着残缺的招标文件往下走。字段与后端 _combine_doc_texts_report 逐字对齐，
  // 全部可选：旧后端 / 缓存的旧事件不带这些字段时前端按「未知」处理，不误报。
  /** 任一参与提取的文档被截断，或预算不足导致有文档整份未被纳入 */
  source_truncated?: boolean;
  /** 被截断的文档清单（沿用后端 details 形状：name / category / chars） */
  truncated_docs?: Array<{ name?: string; category?: string; chars?: number }>;
  /** 纳入提取的文档数 */
  used_doc_count?: number;
  /** 项目文档总数 */
  input_doc_count?: number;
  /** 未纳入的文档数（预算耗尽被整份跳过，或正文为空） */
  dropped_doc_count?: number;
};

/** 解析项「无有效内容」标记（与后端 MARKDOWN_MISSING_RESULT 一致） */
export const BA_MISSING_MARKER = "未提取到";

/** json 项中视为「该字段没有提及」的取值（与后端 _JSON_EMPTY_VALUES 一致） */
const JSON_EMPTY_VALUES = new Set(["", "没有提及", "未提取到", "n/a", "无", "null", "none"]);

/** json 项是否「整体无有效信息」（所有字段都空 / 没有提及）；解析失败不判缺失 */
function isAllEmptyJson(raw: string): boolean {
  let text = raw;
  if (text.startsWith("```")) {
    text = text.replace(/^```[A-Za-z0-9_-]*\s*/, "").replace(/\s*```$/, "").trim();
  }
  let data: any;
  try {
    data = JSON.parse(text);
  } catch {
    return false;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  const values = Object.values(data);
  if (!values.length) return false;
  return values.every(v => {
    if (v && typeof v === "object") {
      return Array.isArray(v) ? v.length === 0 : Object.keys(v).length === 0;
    }
    const s = v === null || v === undefined ? "" : String(v).trim();
    return !s || JSON_EMPTY_VALUES.has(s.toLowerCase());
  });
}

/** 解析项是否「整体无结果」（必选项缺失判定 / 行内「无内容」标签的唯一口径） */
export function isMissingBaResult(content?: string, outputType = "markdown"): boolean {
  const raw = (content || "").trim();
  if (!raw) return true;
  if (outputType === "json") return isAllEmptyJson(raw);
  if (outputType === "markdown") return raw === BA_MISSING_MARKER;
  return false;
}

/**
 * 运行中实时重算汇总（后端 /results 的权威汇总只在加载/结束时写入）。
 * 口径与后端 list_analysis_results 完全一致：total 取定义项数，必选项缺失
 * 同时检查 status 与内容有效性。
 */
export function recomputeBaSummary(
  items: BaStoredItem[] = [],
  defs: BaItemDef[] = [],
): BaSummary {
  const total = defs.length || items.length || 0;
  const byId = new Map(items.map(i => [i.item_id, i]));
  let success = 0;
  let success_valid = 0;
  let manual_count = 0;
  let errors = 0;
  let running = 0;
  const missing_required: string[] = [];

  for (const d of defs) {
    const it = byId.get(d.item_id);
    const status = it?.status || "idle";
    if (status === "success") {
      success++;
      // 「完成」≠「有内容」：content='未提取到' / json 全「没有提及」不算有效成果。
      // 与后端 _compute_results_summary 的 success_valid 同口径 —— 前端「下一步：
      // 目录生成」的放行条件用它，避免带着一份空项目信息进入下游。
      if (!isMissingBaResult(it?.content, d.output_type || "markdown")) {
        success_valid++;
      }
    } else if (status === "error") {
      errors++;
    } else if (status === "running") {
      running++;
    }
    if (it?.source === "manual") manual_count++;

    if (d.required) {
      if (
        status !== "success" ||
        isMissingBaResult(it?.content, d.output_type || "markdown")
      ) {
        missing_required.push(d.label || d.item_id);
      }
    }
  }

  return {
    total,
    success,
    success_valid,
    manual_count,
    errors,
    running,
    pending: total - success - errors - running,
    missing_required,
    all_required_done: missing_required.length === 0,
  };
}

/**
 * 归一化 13 分组：优先用后端 `/bid-analysis/items` 返回的 groups（唯一权威源），
 * 缺失时按定义项的 group 字段**推导**（不再前端硬编码分组表）。
 */
export function normalizeBaGroups(
  groups: any[] = [],
  defs: BaItemDef[] = [],
): BaGroup[] {
  const byId = new Map(defs.map(d => [d.item_id, d]));

  if (groups?.length) {
    return groups.map((g: any) => {
      const key = g.group || g.key || "";
      const rawItems: any[] = g.items?.length
        ? g.items
        : defs.filter(d => d.group === key);
      return {
        key,
        label: g.label || key,
        items: rawItems.map((d: any) => byId.get(d.item_id) || d),
      };
    });
  }

  // 回退：按 defs.group 首次出现顺序推导（label 缺失时退化为分组键）
  const order: string[] = [];
  const bucket = new Map<string, BaItemDef[]>();
  for (const d of defs) {
    const key = d.group || "other";
    if (!bucket.has(key)) {
      bucket.set(key, []);
      order.push(key);
    }
    bucket.get(key)!.push(d);
  }
  return order.map(key => ({ key, label: key, items: bucket.get(key)! }));
}

/** 合并「解析项定义」与「已存结果」，供右侧阅读区 / 完整查看使用 */
export function mergeBaItem(def?: BaItemDef, stored?: BaStoredItem): any {
  if (!def && !stored) return null;
  return {
    ...(def || {}),
    ...(stored || {}),
    item_id: (stored?.item_id || def?.item_id) as string,
    content: stored?.content || "",
  };
}

/**
 * 找出第一个「已完成且有内容」的解析项（按定义顺序）。
 * 供 Tab 切入时自动填充右侧阅读区（此前注释声称默认显示第一个 success 项，实际没有实现）。
 */
export function findFirstDoneBaItem(
  items: BaStoredItem[] = [],
  defs: BaItemDef[] = [],
): any {
  const byId = new Map(items.map(i => [i.item_id, i]));
  for (const d of defs) {
    const s = byId.get(d.item_id);
    if (s?.status === "success" && s.content) return mergeBaItem(d, s);
  }
  return null;
}

/**
 * 「项目级基本信息」(projectBasicInfo) 的 JSON 字段键 → 中文展示名。
 *
 * 为什么只做展示层映射：该解析项落库的是**英文键** JSON（键名 = prompt 模板键，
 * 见 backend/app/services/bid_analysis_service.py 的 37 字段模板）。改键名会破坏
 * 已落库数据、`is_missing_result` 判定与下游 format_downstream_context，因此键名
 * 保持英文，仅在前端渲染时翻译为中文；未命中的键回退显示原始键（不丢字段）。
 *
 * 顺序与后端模板一致，便于对照排查。展示名去掉了模板里给模型看的括号提示
 * （如「工程类型（房建/市政/…）」→「工程类型」）。
 */
export const PROJECT_INFO_FIELD_LABELS: Record<string, string> = {
  project_name: "项目名称",
  project_number: "项目编号",
  project_alias: "项目简称",
  construction_unit: "建设单位",
  contractor: "施工单位",
  supervision_unit: "监理单位",
  design_unit: "设计单位",
  survey_unit: "勘察单位",
  project_location: "工程地点",
  administrative_region: "行政区划",
  surrounding_roads: "周边道路",
  engineering_type: "工程类型",
  total_building_area: "总建筑面积",
  land_area: "占地面积",
  total_cost: "总造价/合同额",
  contract_duration: "合同工期",
  planned_duration: "计划总工期",
  start_date: "开工日期",
  completion_date: "竣工日期",
  milestones: "里程碑节点",
  project_status: "项目状态",
  quality_goal: "质量目标",
  safety_goal: "安全目标",
  schedule_goal: "工期目标",
  civility_goal: "文明施工目标",
  green_goal: "绿色施工目标",
  structure_form: "结构形式",
  floors: "层数",
  building_height: "建筑高度",
  foundation_pit_depth: "基坑深度",
  span: "跨度",
  seismic_grade: "抗震等级",
  project_manager: "项目经理",
  technical_director: "技术负责人",
  production_manager: "生产经理",
  safety_director: "安全总监",
  chief_supervision_engineer: "总监理工程师",
};

/** 项目级基本信息字段中文名（未登记键回退原始键，保证不丢字段） */
export function projectInfoFieldLabel(key: string): string {
  return PROJECT_INFO_FIELD_LABELS[key] || key;
}

/**
 * 单条来源位置证据（与后端 bid_analysis_service.build_evidence 的条目结构对齐）：
 * doc=文档名，line=原文行号，heading=标题路径，quote=原文摘录，
 * match=命中的结果句片段，field=json 项的字段路径（markdown 项无）。
 */
export type BaEvidenceEntry = {
  doc?: string;
  line?: number;
  heading?: string;
  quote?: string;
  match?: string;
  field?: string;
};

/** 解析落库的 evidence JSON（非法/缺省一律回退空数组，不阻断渲染） */
export function parseBaEvidence(raw?: string): BaEvidenceEntry[] {
  if (!raw) return [];
  try {
    const data = JSON.parse(raw);
    return Array.isArray(data) ? (data as BaEvidenceEntry[]) : [];
  } catch {
    return [];
  }
}
