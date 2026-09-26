/**
 * 方案工作台 · 正文生成事件纯函数（从 SchemeWorkbenchPage.tsx 抽离）
 *
 * 承载 SSE 事件 → 章节日志状态机的纯逻辑部分（零副作用、返回新数组、
 * 不直接触碰 React 状态），页面只做「调用 + 落 ref + setState」。
 * 抽离动机：该状态机原先内联在 8000 行巨型组件里重复 4 处且无测试。
 */

/**
 * F-CONTENT-STANDARD(2026-09-26)：生成标准校验报告（后端 standard_report）。
 *
 * 与后端 `app/services/content_standard.py::standard_report` 的返回结构
 * 前端侧子集建模：只声明前端真正消费的字段，其余（stats 等）忽略。
 */
export type StandardReportIssue = {
  type?: string;      // 英文问题码（如 fuzzy_expression / value_conflict）
  severity?: "error" | "warning";
  message?: string;
  excerpt?: string;
};

export type StandardReport = {
  standard?: "precise" | "fuzzy";
  passed?: boolean;
  error_count?: number;
  warning_count?: number;
  issues?: StandardReportIssue[];
  stats?: { placeholders?: number; fuzzy_hits?: number; checked_facts?: number };
};

export type SectionLogItem = {
  section_id: string;
  title: string;
  status: "running" | "success" | "failed" | "skipped";
  word_count?: number;
  word_budget?: number;
  word_status?: string; // under / normal / over（字数达标情况）
  /** ✅ 续写失败信号（后端 section_done 下发）：续写调用失败/产出被丢弃且最终字数不达标 */
  continue_failed?: boolean;
  /** ✅ 质量告警（后端 section_done 下发 quality_issues）：本章正文的程序化质检问题 */
  quality_issues?: unknown[];
  /**
   * F-CONTENT-STANDARD(2026-09-26)：本章实际生效的生成标准
   * （section_start / section_done 均下发，用于日志行的模式徽标）。
   */
  generation_standard?: "precise" | "fuzzy";
  /**
   * F-CONTENT-STANDARD(2026-09-26)：本章生成标准校验报告（section_done 下发）。
   * 咨询性产物 —— 只提示不阻断；含 issues 明细供 Popover 展示。
   */
  standard_report?: StandardReport | null;
  index?: number;       // 第几章（后端 section_start 下发）
  total?: number;       // 总章数
  reason?: string;
  time: number; // epoch ms
  duration?: number; // 耗时(ms)
  /** ✅ 进度增强：章节内阶段（后端 section_stage 下发） */
  stage?: string;       // context / draft / continue / persist
  stage_label?: string; // 阶段中文名
};

/**
 * ✅ 进度增强：正文生成运行统计（后端 stats / ping 事件下发）。
 * 用于展示预计剩余时间、已耗时、累计字数、并发度，以及
 * 「当前正在生成哪些章节、各自处于哪个阶段」。
 */
export type GenStats = {
  elapsed_ms?: number;
  eta_ms?: number | null;
  done?: number;
  total?: number;
  failed?: number;
  words?: number;
  /** ✅ 二次增强：已完成章节的平均实测耗时（后端 stats.avg_section_ms） */
  avg_section_ms?: number | null;
  concurrency?: number;
  phase?: string;
  phase_label?: string;
  progress?: number;
  /** 目录生成专用：是否走「一级 → 逐章二三级」分步链路（true 时 done/total 为章节维度） */
  stepwise?: boolean;
  /** 目录生成专用：已生成目录节点数 */
  nodes?: number;
  running?: {
    section_id: string;
    title: string;
    index?: number;
    stage?: string;
    stage_label?: string;
    elapsed_ms?: number;
  }[];
};

/**
 * 按 section_id 插入或替换一条章节日志，替换项保持在数组尾部
 * （与旧实现的 `[...filter, item]` 语义逐字节一致，日志列表倒序渲染 → 尾部 = 最上面）。
 */
export function upsertSectionLog(
  list: SectionLogItem[],
  item: SectionLogItem
): SectionLogItem[] {
  return [...list.filter((x) => x.section_id !== item.section_id), item];
}

/**
 * 终态收尾：把仍处于「进行中」的日志项收尾，避免终态后残留转圈图标。
 * status 用 skipped 表达"未完成"，与 failed（真失败）区分开。
 * 无 running 项时原样返回（引用相等 → 调用方跳过 setState）。
 */
export function finalizeRunningLogsIn(
  list: SectionLogItem[],
  reason: string,
  status: "skipped" | "failed" = "skipped"
): SectionLogItem[] {
  if (!list.some((x) => x.status === "running")) return list;
  const now = Date.now();
  return list.map((x) =>
    x.status === "running"
      ? { ...x, status, reason, duration: now - x.time }
      : x
  );
}

/**
 * 合并后端 completed/error/stopped 事件下发的失败章节明细（failed_sections）。
 * 失败章节可能压根没进日志（异常路径 / 保留旧正文未改状态），
 * 只有后端知道"是哪几章、为什么失败"，补进日志供用户定位与重试。
 * 已存在的条目保留 index/total/time（不覆盖进度元数据）。
 */
export function mergeFailedSectionsInto(
  list: SectionLogItem[],
  failedSections: unknown
): SectionLogItem[] {
  if (!Array.isArray(failedSections) || failedSections.length === 0) return list;
  const map = new Map(list.map((x) => [x.section_id, x]));
  failedSections.forEach((f: any) => {
    if (!f?.section_id) return;
    const prev = map.get(f.section_id);
    map.set(f.section_id, {
      section_id: f.section_id,
      title: f.title || prev?.title || "未知章节",
      status: "failed",
      reason: f.reason || prev?.reason,
      index: prev?.index,
      total: prev?.total,
      time: prev?.time ?? Date.now(),
      duration: prev ? Date.now() - prev.time : undefined,
    });
  });
  return Array.from(map.values());
}

/**
 * ✅ G12-6（2026-09-20）：断线重挂时从任务终态的 content_result checkpoint
 * 还原失败章节明细。
 *
 * 背景（跨模块数据传递断点）：后端早已把「哪几章失败、为什么失败」落进
 * checkpoint（`_CHECKPOINT_KINDS` 的 content_generation 白名单 + GET /sse/task/{id}
 * 的 `_attach_checkpoint_result`），但前端此前**从未消费** `fin.content_result` ——
 * 断线或刷新后用户只看到「后台任务已停止」，逐章失败原因彻底丢失，只能凭目录树
 * 一章章猜。现与在线 completed 事件（`evt.failed_sections`）走同一合并入口。
 *
 * 防御式解析：checkpoint 缺失 / 字段类型不符一律返回空结果，绝不抛异常
 * （终态查询路径上任何异常都会让重挂流程静默失败，用户连「任务已停止」都看不到）。
 *
 * @returns sections = 有效失败明细；failedCount = 后端计数优先，缺失时按明细条数回退
 * （后端明细上限 50 条，被截断时以后端计数为准，故 failed_count 优先）
 */
export function contentResultFailedSections(
  contentResult: unknown,
): { sections: any[]; failedCount: number } {
  if (!contentResult || typeof contentResult !== "object") {
    return { sections: [], failedCount: 0 };
  }
  const cr = contentResult as {
    failed_sections?: unknown;
    failed_count?: unknown;
  };
  const sections = Array.isArray(cr.failed_sections)
    ? cr.failed_sections.filter(
        (x: unknown): x is { section_id: string } =>
          !!x && typeof x === "object" && !!(x as any).section_id,
      )
    : [];
  const declared = cr.failed_count;
  const failedCount = typeof declared === "number" && declared > 0
    ? declared
    : sections.length;
  return { sections, failedCount };
}

/**
 * ✅ 正文生成 checkpoint 摘要（2026-09-21 新增）。
 *
 * 从断线重挂回传的 `content_result` 中提取人类可读的进度摘要，
 * 供 `TaskTerminalInfo` 断线分支展示「生成了多少章、写了多少字、
 * 几章超字数」—— 旧实现只报「后台任务已停止」，用户无从判断完成度。
 *
 * 防御式解析：所有字段可选、类型不符一律回退默认值，绝不抛异常。
 */
export function contentResultSummary(
  contentResult: unknown,
): { done: number; total: number; words: number; overCount: number } {
  if (!contentResult || typeof contentResult !== "object") {
    return { done: 0, total: 0, words: 0, overCount: 0 };
  }
  const cr = contentResult as Record<string, unknown>;
  return {
    done: typeof cr.done === "number" ? cr.done : 0,
    total: typeof cr.total === "number" ? cr.total : 0,
    words: typeof cr.words === "number" ? cr.words : 0,
    overCount: typeof cr.over_count === "number" ? cr.over_count : 0,
  };
}
