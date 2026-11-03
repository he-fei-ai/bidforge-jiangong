

export const statusColor: Record<string, string> = {
  empty: "default", pending: "processing", generated: "success",
  expanded: "blue", reviewed: "green",
};

export const statusText: Record<string, string> = {
  empty: "待生成", pending: "生成中", generated: "已生成",
  expanded: "已扩写", reviewed: "已审核",
};

/** 导出预检问题类型 → 中文标签（后端 export_check 会返回多种类型，需分别展示）
 *  ⚠️ 必须与后端 `routers/export.py::_EXPORT_ISSUE_RULE_MAP` 的 **15 类** 逐键相等 ——
 *  缺标签时界面会回退显示英文 type（`EXPORT_ISSUE_LABEL[iss.type] || iss.type`），
 *  用户看到 `global_facts_blocked：…` 之类裸代号；多余死键同样有害（后端已不产出
 *  却留一条永远不可达的翻译）。parity 双向由后端
 *  `tests/test_charts_r13_and_types_parity_20261004.py::TestExportIssueLabelParity`
 *  读本文件锁定（✅ R168 补反向棘轮：后端新增类型而漏改此处、或后端摘除类型而本处
 *  残留死键，均失败）。 */
export const EXPORT_ISSUE_LABEL: Record<string, string> = {
  empty_section: "空章节",
  orphan_node: "孤立节点",
  status_inconsistent: "状态不一致",
  low_word_count: "字数偏少",
  chart_failed: "图表异常",
  chart_ungenerated: "图表未生成",
  duplicate_section_number: "章节编号重复",
  section_number_mismatch: "章节编号漂移",
  bid_analysis_incomplete: "项目提取未完成",
  review_pending: "章节待审核",
  review_rejected: "章节审核未通过",
  review_missing: "缺少章节审核记录",
  global_facts_blocked: "全局事实待确认",
  stale_cross_reference: "交叉引用已失效",
  body_subheading_namespace_conflict: "正文子标题撞号",
};

// ✅ any 清理（2026-11-02）：导出预检问题单条契约（与 useExportPresets.ExportIssueItem
//    同形；只声明本模块消费的键，其余字段由调用方持有）
export interface ExportIssue {
  title?: string;
  detail?: string;
  chart_type?: string;
}

/** 预检问题的一句话描述（title 缺失时回退 detail / chart_type） */
export function describeExportIssue(iss: ExportIssue | null | undefined): string {
  if (iss?.title) return iss.title;
  if (iss?.detail) return iss.detail;
  if (iss?.chart_type) return `图表类型 ${iss.chart_type}`;
  return "—";
}

/**
 * 导出阶段标签（D2 加固 · 2026-10-06）。
 *
 * 独立成模块级常量（而非内联三元）：既让 `exportStage` 有真实消费点
 * （避免「声明了 state 却没人读」的死状态债），也便于单测直接断言
 * 「阶段 → 用户可读文案 + 序号」的映射不会漂移。
 */
export const EXPORT_STAGE_LABEL: Record<"idle" | "charts" | "docx" | "pdf", string> = {
  idle: "空闲",
  charts: "渲染图表",
  docx: "生成文档",
  pdf: "生成 PDF",
};

/** 阶段在总流程中的序号（1 起，idle 不参与）。 */
export const EXPORT_STAGE_ORDER: Record<string, number> = { charts: 1, docx: 2, pdf: 3 };

/** 导出配置深合并（2026-10-06 · P0「折叠面板配置被静默丢弃」修复的配套纯函数）。
 *
 * `validateFields()` 只返回**已挂载**表单项的值，而导出配置表单的「封面与
 * 页面」面板默认折叠（defaultActiveKey=["basic","body","headings"]），其中
 * 12 项 —— `margins`(4) / `cover_info`(4) / `scheme_forms`(4)，即**页边距、
 * 封面项目信息表、专项施工方案四张法定前置表单**—— 未挂载即不进返回值，
 * 后端静默回落到自己的默认值（"界面拨了开关、导出毫无变化"）。
 *
 * 本函数把整份 form store（`getFieldsValue(true)`，antd `preserve` 默认 true
 * 保证折叠不丢值）与校验结果深合并：**只让 `validated` 覆盖同名叶子值**，
 * 既保留 `validateFields` 的校验语义，又不丢折叠面板里的配置。
 *
 * 独立成模块级纯函数（而非内联在 handleExport）便于单测直接断言嵌套语义。
 */
export function deepMergeExportConfig(store: unknown, validated: unknown): Record<string, unknown> {
  const base = (store && typeof store === "object" ? store : {}) as Record<string, unknown> | unknown[];
  const patch = (validated && typeof validated === "object" ? validated : {}) as Record<string, unknown>;
  const out = (Array.isArray(base) ? [...base] : { ...base }) as Record<string, unknown>;
  for (const key of Object.keys(patch)) {
    const pv = patch[key];
    if (pv && typeof pv === "object" && !Array.isArray(pv)) {
      out[key] = deepMergeExportConfig(out[key], pv);
    } else if (pv !== undefined) {
      out[key] = pv;
    }
  }
  return out;
}

/** 后台任务类型中文标签（断线重挂接提示用） */
export const TASK_TYPE_LABEL: Record<string, string> = {
  outline_generation: "目录生成",
  content_generation: "正文生成",
  facts_generation: "全局事实提取",
};

/**
 * ✅ 工作流 Tab 标签：步骤序号 + 标题 + 状态徽标。
 * 把「提取项目 → 目录生成 → 全局事实 → 正文生成 → 审核与预检 → 导出」的工序顺序
 * 直接画在导航栏上，用户一眼能看到当前在哪一步、下一步该做什么。
 */
