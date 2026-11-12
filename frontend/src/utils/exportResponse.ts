/**
 * 导出响应头解析纯函数（T3 · 2026-10-03 从 SchemeWorkbenchPage.handleExport 内联逻辑提取）。
 *
 * 目的：导出响应头（文件名 / 图表渲染统计 / 自动修复统计）的解析此前内联在
 * 9800+ 行页面组件里无法单测；提取为纯函数后可独立验证（tests/exportResponse.test.ts），
 * 组件侧只保留 msg 展示。解析失败一律返回 null/兜底名，不阻断下载（与原行为一致）。
 */

/** 大小写不敏感地取响应头（axios 对自定义头一般保留原始大小写，这里兜底双查）。 */
export function headerValue(headers: unknown, name: string): string | undefined {
  if (!headers || typeof headers !== "object") return undefined;
  const lower = name.toLowerCase();
  for (const k of Object.keys(headers as Record<string, unknown>)) {
    if (k.toLowerCase() === lower) {
      const v = (headers as Record<string, unknown>)[k];
      return typeof v === "string" ? v : undefined;
    }
  }
  return undefined;
}

/**
 * 解析 X-Export-Filename → 下载文件名。
 * 头缺失 / 百分号解码失败时沿用兜底名（`${方案名}.${format}`），不阻断下载。
 */
export function resolveExportFilename(headers: unknown, fallback: string): string {
  const raw = headerValue(headers, "x-export-filename");
  if (!raw) return fallback;
  try {
    return decodeURIComponent(raw) || fallback;
  } catch {
    return fallback;
  }
}

export interface ChartRenderStats {
  fe?: number;
  backend_ok?: number;
  failed?: number;
  /**
   * ✅ 2026-10-06（D1 加固）：**跨章借图**次数 —— 该章节没有自己的图表登记时，
   * 导出借用了别章的同类图。借图本身是既定契约（三章共用一张 labor 图是合理
   * 场景），但此前**零信号**：成稿里 B 章可能出现 A 章的流程图而用户毫不知情。
   * 后端现把它计入 X-Chart-Render-Stats，此处展示为提示（不阻断导出）。
   */
  fallback_borrowed?: number;
  /** 跨章借图明细（最多 20 条），用于提示文案与问题定位。 */
  fallback_borrowed_details?: Array<{
    section_id: string;
    chart_type: string;
    borrowed_from: string;
  }>;
  /**
   * ✅ D-23（R176 · 2026-11-04）：甘特图超过单图上限（30 项任务）被截断的
   * 任务总数。旧实现只在后端日志留 WARNING —— 用户看到「完整」的横道图，
   * 却不知道成稿里少了若干任务。0/缺省时不展示。
   */
  gantt_truncated?: number;
  /**
   * ✅ R179（G-06）：**无本地渲染兜底**的类型（mindmap/sequence/class/state/er/journey
   * 等只能走 Mermaid HTTP Service）渲染失败的张数。旧实现只累加 `failed`，用户
   * 无法区分「图表语法坏了」与「本机 PIL 根本不会画」，会被泛化提示误导去开启
   * 「允许 PIL 兜底」（对这些类型无效）。0/缺省时不展示。
   */
  no_pil_fallback?: number;
  /** 上述失败涉及的类型明细（后端去重后给出，最多展示前几个）。 */
  no_pil_fallback_types?: string[];
  /**
   * ✅ R181（2026-11-06）：**无 HTTP 兜底**的 chart-json 甘特图渲染失败张数。
   * 甘特图 JSON 载荷只走本地 PIL，不经过 Mermaid HTTP 服务；失败时与
   * `no_pil_fallback`（本地兜底缺失）是相反的缺口。0/缺省时不展示。
   */
  no_http_fallback?: number;
  /** 上述失败涉及的类型明细（后端去重后给出）。 */
  no_http_fallback_types?: string[];
  /**
   * ✅ R221（2026-11-11 · 报告 §6.1 短期建议②）：本次导出中**HTTP 失败后
   * 由本地 PIL 兜底出图**的张数。此前 `backend_ok` 把 HTTP 原生、JSON 原生、
   * PIL 兜底混成一个数字，运维无法回答「哪些图用了兜底」。缓存命中不计入
   * （那不是本次渲染动作）。0/缺省时不展示。
   */
  pil_fallback?: number;
  /** 本次导出中由 Mermaid HTTP 渲染服务原生出图的张数（同样不含缓存命中）。 */
  http_ok?: number;
  /**
   * ✅ R221：本可本地兜底、却因导出开关 `allow_pil_fallback=false` 被关闭而
   * 丢图的张数。与 `no_pil_fallback` 语义相反 —— 这类图只要开启开关就能出图，
   * 必须单独提示可行动建议，不能混进「开启也没用」的告警里。
   */
  pil_fallback_disabled?: number;
  /** 上述失败涉及的类型明细（后端去重后给出）。 */
  pil_fallback_disabled_types?: string[];
  /**
   * ✅ R222（2026-11-12 · 日志驱动）：**交付侧实际落图**统计。
   * 预渲染的 `backend_ok` 只说明「渲染器产出了 PNG」，不等于「图进了成稿」；
   * 正文每处图表围栏记一个 expected，成功插图记 inserted，跳过/插入失败记
   * dropped。后台日志实证存在「成稿缺图、failed=0、还写了缓存」的导出，
   * 这三项是与预渲染统计互补的单一事实源。0/缺省时不展示。
   */
  chart_expected?: number;
  chart_inserted?: number;
  chart_dropped?: number;
  /** 同图重复引用的去重跳过次数（有意设计，不计入缺图）。 */
  chart_duplicate_skipped?: number;
  /** 缺图明细（最多 20 条），reason 为 no_code / render_or_probe_failed / insert_failed。 */
  chart_dropped_details?: Array<{ section_id: string; chart_type: string; reason: string }>;
  /** 缓存命中标识（复用上次成果，本次未重新渲染）。 */
  cached?: boolean;
  /** ✅ R222：命中时回带的**上次导出**图表统计（旁车加法字段，旧后端无此键）。 */
  cached_stats?: ChartRenderStats;
  /** 上次导出的成稿本身存在缺图（cached_stats.chart_dropped > 0）。 */
  cached_degraded?: boolean;
}

/** 解析 X-Chart-Render-Stats；头缺失或 JSON 非法返回 null。 */
export function parseChartRenderStats(headers: unknown): ChartRenderStats | null {
  const raw = headerValue(headers, "x-chart-render-stats");
  if (!raw) return null;
  try {
    const stats = JSON.parse(raw) as ChartRenderStats;
    return typeof stats === "object" && stats !== null ? stats : null;
  } catch {
    return null;
  }
}

/** 图表渲染统计 → 展示片段（空数组表示无可展示项）。 */
export function chartRenderStatsParts(stats: ChartRenderStats): string[] {
  const parts: string[] = [];
  if (stats.fe) parts.push(`前端图 ${stats.fe} 张`);
  if (stats.backend_ok) parts.push(`后端原生 ${stats.backend_ok} 张`);
  if (stats.failed) parts.push(`失败 ${stats.failed} 张`);
  if (stats.fallback_borrowed) {
    parts.push(`跨章借图 ${stats.fallback_borrowed} 张`);
  }
  // ✅ R221：`backend_ok` 仍是总数，这里只**追加**细分项（旧 stats 无此字段时
  //    不追加，既有 `chartRenderStatsParts` 的精确形状断言逐字不受影响）。
  if (stats.pil_fallback) parts.push(`PIL 兜底 ${stats.pil_fallback} 张`);
  // ✅ R222：仅当**成稿真的缺图**才追加，正常导出不新增噪声；
  //    旧 stats 无此字段时不追加，既有精确形状断言逐字不受影响。
  if (stats.chart_expected && stats.chart_dropped) {
    parts.push(
      `成稿落图 ${Number(stats.chart_inserted || 0)}/${Number(stats.chart_expected)} 张（缺 ${stats.chart_dropped} 张）`,
    );
  }
  return parts;
}

const CHART_DROP_REASON_TEXT: Record<string, string> = {
  no_code: "无登记图表代码",
  render_or_probe_failed: "渲染失败或图片损坏",
  insert_failed: "图片插入失败",
};

/**
 * ✅ R222：成稿缺图告警（数据丢失级）。返回 null 表示无需提示。
 *
 * 与「渲染失败」类告警不同：这里说的是**已经交付的文档里少了几张图**，
 * 必须明确告知「本次已标记降级且不写缓存」，并给出可行动建议。
 */
export function chartDeliveryWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.chart_dropped || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  const expected = Number(stats?.chart_expected || 0);
  const inserted = Number(stats?.chart_inserted || 0);
  const details = Array.isArray(stats?.chart_dropped_details) ? stats.chart_dropped_details : [];
  const reasons = Array.from(
    new Set(
      details
        .map((d) => (d && typeof d.reason === "string" ? CHART_DROP_REASON_TEXT[d.reason] : ""))
        .filter((t): t is string => Boolean(t)),
    ),
  );
  const reasonText = reasons.length ? `（${reasons.join("、")}）` : "";
  return `成稿有 ${n} 张图表未插入${reasonText}：正文共 ${expected} 处图表围栏，实际落图 ${inserted} 张。本次导出已标记「降级」且本次不写入缓存，建议检查图表代码或登记后重新导出`;
}

/**
 * ✅ R222：缓存命中时，若**上次导出**的成稿本身缺图，必须提示用户重新导出。
 *
 * 坏缓存守卫只拦「本次降级」；历史已落盘的缺图产物仍会被命中，旧实现只回
 * `{"cached":true}` → 用户以为复用的是完整成稿。旧旁车无统计时返回 null。
 */
export function cachedChartDegradedWarning(stats: ChartRenderStats): string | null {
  if (!stats?.cached || !stats.cached_stats) return null;
  const prev = stats.cached_stats;
  const n = Number(prev.chart_dropped || 0);
  if (!stats.cached_degraded && (!Number.isFinite(n) || n <= 0)) return null;
  return `本次复用了上次导出的成果，而该成稿当初有 ${n} 张图表未插入（正文 ${Number(prev.chart_expected || 0)} 处，实际落图 ${Number(prev.chart_inserted || 0)} 张）。建议重新导出以生成完整成稿`;
}

/**
 * 跨章借图的告警文案（D1）。返回 null 表示无需提示。
 *
 * 与 `chartRenderStatsParts` 的 info 提示分层：借图属于"内容可能与章节不符"
 * 的风险（而非性能统计），因此走 warning 且措辞点明「请回查该章节图表登记」。
 */
export function fallbackBorrowWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.fallback_borrowed || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  return `${n} 张图表使用了跨章借用（该章节没有自己的图表登记，借用了同类图），成稿图文可能与章节内容不符，请回查对应章节`;
}

/**
 * 甘特图截断告警文案（D-23）。返回 null 表示无需提示。
 *
 * 后端单张甘特图最多渲染 30 项任务（`_GANTT_MAX_TASKS`），超出部分不画。
 * 与 `chartRenderStatsParts` 的 info 分层：这里属于「成稿内容不完整」，
 * 走 warning 并告诉用户可执行动作（拆分/精简任务列表后重新导出）。
 */
export function ganttTruncatedWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.gantt_truncated || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  return `甘特图任务超出单图上限（每图最多 30 项），成稿中共有 ${n} 项任务未绘制；请精简任务列表或拆分为多张甘特图后重新导出`;
}

/**
 * 无本地渲染兜底告警文案（R179 · G-06）。返回 null 表示无需提示。
 *
 * 这些图表类型（mindmap/sequence/class/state/er/journey）PIL 本地渲染器不支持，
 * 只能依赖 Mermaid HTTP Service。失败时「开启允许 PIL 兜底」没有意义，必须
 * 明确告知用户去检查图表服务，避免给无效操作建议。
 */
export function noPilFallbackWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.no_pil_fallback || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  const types = Array.isArray(stats?.no_pil_fallback_types)
    ? stats.no_pil_fallback_types.filter((t): t is string => typeof t === "string").slice(0, 5)
    : [];
  const detail = types.length ? `（${types.join("、")}）` : "";
  // ✅ R221：后端判据已由「类型不在白名单」改为「该载荷无本地解析器」，
  //    告警覆盖 architecture/labor/layout 的 Mermaid 语法载荷，措辞同步放宽。
  return `${n} 张图表依赖 Mermaid HTTP 渲染服务失败${detail}，这些类型或载荷形态没有本地渲染兜底，开启「允许 PIL 兜底」也无法补救；请确认图表服务可用，或改用受支持的图表类型 / JSON 数据载荷后重试`;
}

/**
 * 无 HTTP 出口告警文案（R181）。返回 null 表示无需提示。
 *
 * JSON 数据载荷只在本地渲染，不经过 HTTP 图表服务。失败时旧实现只并入泛化
 * `failed`，用户会被「检查图表服务」类建议误导；这里明确区分「该形态没有 HTTP 兜底」。
 *
 * ✅ R221：后端判据由「仅甘特图」扩到全部 7 类 chart-json，文案同步去掉
 * 「N 张甘特图」的硬编码（否则 flowchart/labor 等失败时会张冠李戴）。
 */
export function noHttpFallbackWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.no_http_fallback || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  const types = Array.isArray(stats?.no_http_fallback_types)
    ? stats.no_http_fallback_types.filter((t): t is string => typeof t === "string").slice(0, 5)
    : [];
  const detail = types.length ? `（${types.join("、")}）` : "";
  return `${n} 张图表使用 JSON 数据格式，本地渲染失败${detail}；该形态没有 HTTP 渲染兜底，请检查图表数据（任务 / 日期 / 节点）或改用受支持的 Mermaid 语法后重试`;
}

/**
 * ✅ R221：「PIL 兜底开关被关闭」告警。返回 null 表示无需提示。
 *
 * 与 `noPilFallbackWarning` 恰好相反：这批图本地渲染器**本来能画**，只是
 * 用户在导出设置里关掉了开关（或旧预设库带了 false）。旧实现把它并进泛化
 * `failed`，提示「开启允许 PIL 兜底」虽有方向但混在别的失败里；更糟的是
 * 判据修正前它可能误进 no_pil_fallback，得到「开启也无法补救」的错误结论。
 */
export function pilFallbackDisabledWarning(stats: ChartRenderStats): string | null {
  const n = Number(stats?.pil_fallback_disabled || 0);
  if (!Number.isFinite(n) || n <= 0) return null;
  const types = Array.isArray(stats?.pil_fallback_disabled_types)
    ? stats.pil_fallback_disabled_types.filter((t): t is string => typeof t === "string").slice(0, 5)
    : [];
  const detail = types.length ? `（${types.join("、")}）` : "";
  return `${n} 张图表${detail}因「PIL兜底渲染」开关被关闭而未能出图；这些图本地渲染器本可绘制，请在导出设置中开启「PIL兜底渲染」后重试`;
}

/**
 * ✅ R221：导出预检的图表渲染就绪度（后端 `/export/check` 的加法式响应字段）。
 * `http_service_available` 是**三态**：true/false/null(尚未探测)，不能当布尔用。
 */
export interface ChartRenderReadiness {
  http_service_available?: boolean | null;
  all_backends_unavailable?: boolean;
  syntax_payload_count?: number;
  unrenderable_syntax_count?: number;
  unrenderable_syntax_types?: string[];
  pil_fallback_default?: boolean;
}

/**
 * 预检就绪度 → 用户可见提示。返回 null 表示无需提示。
 *
 * 旧预检只看图表状态（done/generated），「5 张图全部完成」却仍可能导出后
 * 整张消失。这里把「HTTP 服务不可用 + 存在语法载荷」提前暴露，尤其点名
 * 「本地渲染器画不了」的那批 —— 它们改默认值也救不回来。
 */
export function chartRenderReadinessHint(r: ChartRenderReadiness | null | undefined): string | null {
  if (!r) return null;
  const down = r.all_backends_unavailable === true || r.http_service_available === false;
  if (!down) return null;
  const syntax = Number(r.syntax_payload_count || 0);
  if (!Number.isFinite(syntax) || syntax <= 0) return null;
  const unrenderable = Number(r.unrenderable_syntax_count || 0);
  const types = Array.isArray(r.unrenderable_syntax_types)
    ? r.unrenderable_syntax_types.filter((t): t is string => typeof t === "string").slice(0, 4)
    : [];
  const detail = types.length ? `（${types.join("、")}）` : "";
  const base = `图表 HTTP 渲染服务不可用，本方案有 ${syntax} 张 Mermaid 语法图表，导出将依赖前端预览渲染或本地 PIL 兜底`;
  if (!Number.isFinite(unrenderable) || unrenderable <= 0) {
    return r.pil_fallback_default
      ? `${base}；「PIL兜底渲染」默认已开启，多数图表仍可出图`
      : `${base}；建议开启「PIL兜底渲染」`;
  }
  return `${base}；其中 ${unrenderable} 张${detail}本地渲染器无法绘制，若前端预览渲染失败，这些图会在成稿中缺失，建议改用受支持的图表类型或部署图表渲染服务`;
}

export interface FixStats {
  formulas?: number;
  block_formulas?: number;
  replacement_chars?: number;
  control_chars?: number;
  gbk_mojibake?: number;
  latin1_mojibake?: number;
  cyrillic?: number;
  /**
   * ✅ 2026-10-06（后端加法式扩展）：单个内容块渲染抛异常时后端不再让整份导出
   * 失败，而是回滚该块已写入的文档元素与图号/表号，降级为纯文本保留文字。
   * 计数经 X-Fix-Stats 透出，使「成稿里少了一块版式」对用户可见
   * （此前这类降级不存在，降级的等价物是整次导出 500）。
   */
  block_render_failed?: number;
  /** 超宽/超长 GFM 表格被截断的次数（成稿可交付但该表内容不完整）。 */
  table_truncated?: number;
  /** ✅ R195：「项目关键事实」附录渲染失败次数（产物缺附录，本次不写缓存）。 */
  facts_appendix_dropped?: number;
  /** ✅ R195：数据来源/知识库补充附录渲染失败次数（产物缺附录，本次不写缓存）。 */
  sources_appendix_dropped?: number;
  /** ✅ R195：超长段落被截断的次数（成稿该段不完整，需回查源文）。 */
  paragraph_truncated?: number;
}

/** 解析 X-Fix-Stats；头缺失或 JSON 非法返回 null。 */
export function parseFixStats(headers: unknown): FixStats | null {
  const raw = headerValue(headers, "x-fix-stats");
  if (!raw) return null;
  try {
    const fs = JSON.parse(raw) as FixStats;
    return typeof fs === "object" && fs !== null ? fs : null;
  } catch {
    return null;
  }
}

/** 自动修复统计 → 展示片段（空数组表示无可展示项）。 */
export function fixStatsParts(fs: FixStats): string[] {
  const parts: string[] = [];
  if (fs.formulas) parts.push(`公式 ${fs.formulas} 处`);
  if (fs.replacement_chars) parts.push(`替换字符 ${fs.replacement_chars} 处`);
  if (fs.control_chars) parts.push(`控制字符 ${fs.control_chars} 处`);
  if (fs.gbk_mojibake) parts.push(`GBK 乱码 ${fs.gbk_mojibake} 处`);
  if (fs.latin1_mojibake) parts.push(`编码乱码 ${fs.latin1_mojibake} 处`);
  if (fs.cyrillic) parts.push(`西里尔误植 ${fs.cyrillic} 处`);
  if (fs.block_render_failed) {
    parts.push(`内容块降级为纯文本 ${fs.block_render_failed} 处（版式已降级，文字保留）`);
  }
  if (fs.table_truncated) parts.push(`超宽/超长表格截断 ${fs.table_truncated} 处`);
  if (fs.facts_appendix_dropped) parts.push(`「项目关键事实」附录渲染失败 ${fs.facts_appendix_dropped} 处`);
  if (fs.sources_appendix_dropped) parts.push(`数据来源附录渲染失败 ${fs.sources_appendix_dropped} 处`);
  if (fs.paragraph_truncated) parts.push(`超长段落被截断 ${fs.paragraph_truncated} 处（成稿该段不完整，请回查源文）`);
  return parts;
}

// =========================================================================
// ✅ R175 BUG-7（2026-11-03）：导出失败错误体解析 + 降级信号识别
//
// 审计 R175《导出文档模块审计》BUG-7：导出走 `responseType:"blob"`，后端
// 4xx/5xx 的真实原因在 `err.response.data`（Blob）里，`handleExport` 的
// catch 只看 `err.message` → 用户只看到「Request failed with status code N」
// （如 409 编号不一致、500 渲染异常的真实文案被吞）。另有后端新增的
// `X-Export-Degraded: empty-sections` 信号前端**完全未读取**，成稿缺章
// 对用户零提示。
//
// 说明：`api/index.ts` 的响应拦截器已有一层 Blob 解包（R14/R104 修复），
// 本层是**导出链路的防御性兜底**：拦截器被绕过（自定义实例/非 JSON 体）
// 或解析失败时仍能取回真实 detail；同时补齐降级头识别。
// =========================================================================

/** 后端降级信号（`X-Export-Degraded`，逗号分隔；未知信号也保留给用户提示）。 */
export interface ExportDegradedInfo {
  /** 原始信号列表（小写，去空） */
  signals: string[];
  /** 面向用户的中文提示；无法翻译的信号退化为通用降级文案 */
  message: string;
}

/** 已知降级信号 → 中文提示（未知信号走通用文案，不静默）。 */
const EXPORT_DEGRADED_TEXT: Record<string, string> = {
  "empty-sections": "部分章节为空，导出内容可能不完整（后端已降级导出，本次不写入缓存）",
  // ✅ B1-2（R176 · 2026-11-04）：全局事实读取失败导致的降级（本次不写缓存）
  "global-facts-query-failed":
    "全局事实读取失败，成稿可能缺少「项目关键事实」（后端已降级导出，本次不写入缓存），建议稍后重新导出",
  // ✅ R179（2026-11-05）：导出出口闸门检测到终稿仍有无法自动消除的格式残留
  "content-residue":
    "成稿仍存在无法自动消除的格式残留（后端已降级导出，本次不写入缓存），请检查后重新导出",
  // ✅ R195（2026-11-09）：事实/补充附录渲染失败 → 产物缺附录（本次不写缓存）
  "appendix-dropped":
    "「项目关键事实」或数据来源附录渲染失败，成稿缺少对应附录（后端已降级导出，本次不写入缓存），建议修复数据源后重新导出",
  // ✅ R195（2026-11-09）：章节级渲染失败被 fail-soft 跳过 → 产物缺章
  "section-dropped":
    "部分章节渲染失败被跳过，成稿缺少该章内容（后端已降级导出，本次不写入缓存），请检查对应章节内容后重新导出",
  // ✅ R222（2026-11-12）：交付侧落图数 < 正文图表围栏数（成稿确实缺图）。
  "charts-dropped":
    "成稿有图表未插入（渲染成功却没进文档、或有图表围栏但无登记代码），后端已降级导出且本次不写入缓存，请检查图表代码或登记后重新导出",
};

/**
 * 解析 `X-Export-Degraded`。头缺失/空值返回 null（旧行为零提示不变）；
 * 命中的信号至少产出一条中文提示，未知信号也保留并给出通用降级文案。
 */
export function parseExportDegraded(headers: unknown): ExportDegradedInfo | null {
  const raw = headerValue(headers, "x-export-degraded");
  if (!raw) return null;
  const signals = raw
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  if (!signals.length) return null;
  const texts = signals.map((s) => EXPORT_DEGRADED_TEXT[s]).filter((t): t is string => Boolean(t));
  // 已知信号给出精确文案；未知信号也必须可见（不得因「不认识」而静默吞掉）
  const unknown = signals.filter((s) => !EXPORT_DEGRADED_TEXT[s]);
  const parts = [...texts];
  if (unknown.length) {
    parts.push(
      texts.length
        ? `导出已降级（其他信号：${unknown.join(" / ")}）`
        : `导出已降级（${unknown.join(" / ")}），成稿内容可能不完整`,
    );
  }
  return { signals, message: parts.join("；") };
}

/**
 * ✅ R205（导出→前端零消费字段）：后端 DOCX/PDF 在缺图降级时挂
 * `X-Cache-Status: degraded`（export.py:5885/6516），此前前端生产代码
 * 只解析 `X-Export-Degraded`，缓存降级信号完全不可见。
 */
export interface CacheStatusInfo {
  status: string;
  degraded: boolean;
  message: string;
}

/** 解析 `X-Cache-Status`；头缺失返回 null，仅 degraded 产出中文提示。 */
export function parseCacheStatus(headers: unknown): CacheStatusInfo | null {
  const raw = headerValue(headers, "x-cache-status");
  if (!raw) return null;
  const status = raw.trim().toLowerCase();
  if (!status) return null;
  const degraded = status === "degraded";
  return {
    status,
    degraded,
    message: degraded ? "本次导出缓存降级：成稿可能缺少图表，建议检查后重新导出" : "",
  };
}

/** 全局事实读取状态（`X-Global-Facts-Status` 回传的百分号编码 JSON）。 */
export interface GlobalFactsStatus {
  ok: boolean;
  code: string;
  count?: number;
}

/**
 * ✅ B1-2（R176 · 2026-11-04）：解析 `X-Global-Facts-Status`。
 *
 * 后端在 `_query_global_facts()` 异常时降级为「空事实」并把状态写进该响应头
 * （`export.py:_global_facts_status_headers`），但 R176 审计发现前端全仓零消费 ——
 * 用户拿到缺少「项目关键事实」的成稿却没有任何提示。头缺失/解码失败/JSON 非法
 * 返回 null（旧行为零提示不变，绝不阻断下载）。
 */
export function parseGlobalFactsStatus(headers: unknown): GlobalFactsStatus | null {
  const raw = headerValue(headers, "x-global-facts-status");
  if (!raw) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(decoded);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.code !== "string" || !obj.code) return null;
  return {
    ok: obj.ok === true,
    code: obj.code,
    count: typeof obj.count === "number" ? obj.count : undefined,
  };
}

/** 全局事实状态 → 用户可读告警；读取正常（ok）返回 null。 */
export function globalFactsStatusWarning(status: GlobalFactsStatus | null): string | null {
  if (!status || status.ok) return null;
  if (status.code === "query_failed") {
    return "全局事实读取失败（数据库连接异常），成稿可能缺少「项目关键事实」，建议稍后重新导出";
  }
  return `全局事实状态异常（${status.code}），成稿可能缺少「项目关键事实」`;
}

/** 导出失败解析结果。 */
export interface ExportErrorResolution {
  /** 用户/组件主动取消（AbortError / ERR_CANCELED）：调用方不得弹错误提示 */
  canceled: boolean;
  /** 展示文案；canceled 时保留原始消息（调用方不使用） */
  message: string;
  /** true 表示后端未给出可读错误体，message 为泛化兜底文案 */
  generic: boolean;
}

/** 无错误体时的泛化兜底文案（与旧 catch 的 `|| "导出失败"` 口径一致）。 */
const GENERIC_EXPORT_FAILURE = "导出失败";
/** axios 默认状态文案：没有真实 detail 时不能直接展示给用户。 */
const AXIOS_STATUS_ONLY_MESSAGE = /^Request failed with status code \d+$/;

/** 从对象体取非空字符串字段（detail / message）。 */
function stringField(obj: unknown, key: string): string | undefined {
  if (typeof obj !== "object" || obj === null) return undefined;
  const v = (obj as Record<string, unknown>)[key];
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

/**
 * Blob 错误体 → 可读文案：先读文本再按 JSON 解析 `detail`/`message`。
 * 非 JSON 体（二进制残片 / HTML 错误页）不返回文本，避免把乱码抛给用户。
 */
async function blobErrorDetail(data: unknown): Promise<string | undefined> {
  if (typeof Blob === "undefined" || !(data instanceof Blob)) return undefined;
  try {
    const text = (await data.text()).trim();
    if (!text.startsWith("{") && !text.startsWith("[")) return undefined;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return undefined;
    }
    return stringField(parsed, "detail") ?? stringField(parsed, "message");
  } catch {
    return undefined;
  }
}

/** 非 Blob 错误体 → 可读文案（字符串体仅在不像 HTML 时采用）。 */
function bodyErrorDetail(data: unknown): string | undefined {
  if (typeof data === "string") {
    const t = data.trim();
    return t && !t.startsWith("<") ? t : undefined;
  }
  return stringField(data, "detail") ?? stringField(data, "message");
}

/**
 * ✅ R175 BUG-7：把导出 catch 里的任意错误解析为可展示文案。
 *
 * 优先顺序：响应体（Blob→文本→JSON / 对象 detail|message / 纯文本）
 *   → 拦截器已解析的 `err.message`（排除 axios 裸状态码）
 *   → 泛化兜底「导出失败（HTTP N），后端未返回可读错误信息」。
 *
 * 取消（AbortError / ERR_CANCELED）与**无响应**的网络错误/超时保留原始消息，
 * 不新增误报（超时文案仍是 axios 的 timeout 提示）。
 */
export async function resolveExportError(e: unknown): Promise<ExportErrorResolution> {
  const err = (typeof e === "object" && e !== null ? e : {}) as {
    name?: unknown;
    code?: unknown;
    message?: unknown;
    response?: { status?: unknown; data?: unknown };
  };
  const name = typeof err.name === "string" ? err.name : "";
  const code = typeof err.code === "string" ? err.code : "";
  const rawMessage = typeof err.message === "string" ? err.message.trim() : "";

  if (name === "AbortError" || code === "ERR_CANCELED") {
    return {
      canceled: true,
      message: rawMessage || "已取消导出",
      generic: false,
    };
  }

  const response = err.response;
  const hasResponse = typeof response === "object" && response !== null;
  const fromBody = hasResponse
    ? ((await blobErrorDetail(response?.data)) ?? bodyErrorDetail(response?.data))
    : undefined;
  if (fromBody) return { canceled: false, message: fromBody, generic: false };

  // 无响应 = 网络错误 / 超时：保留原始文案（timeout / Network Error），不误报
  if (!hasResponse) {
    return {
      canceled: false,
      message: rawMessage || GENERIC_EXPORT_FAILURE,
      generic: !rawMessage,
    };
  }

  // 有响应但无可读体：拦截器已翻译过的中文 detail 优先；裸状态码/空 → 泛化
  if (rawMessage && !AXIOS_STATUS_ONLY_MESSAGE.test(rawMessage)) {
    return { canceled: false, message: rawMessage, generic: false };
  }
  const status = typeof response?.status === "number" ? response.status : undefined;
  // ✅ R195（2026-11-09）：无错误体时按状态码给可行动文案。
  //   409（无 export_blocked_high_issues 体）= 生成中导出互斥；503 = PDF 转换锁等待超时。
  const statusFallback =
    status === 409
      ? "导出被拒绝：方案可能正在生成中（事实/目录/正文），请等待生成完成或先停止生成后再导出"
      : status === 503
        ? "导出服务繁忙：PDF 转换队列等待超时，请稍后重试；若连续出现请检查 Word 状态"
        : undefined;
  return {
    canceled: false,
    message: status
      ? (statusFallback ?? `${GENERIC_EXPORT_FAILURE}（HTTP ${status}），后端未返回可读错误信息`)
      : GENERIC_EXPORT_FAILURE,
    generic: true,
  };
}

// =========================================================================
// ✅ B-15（R182 · 2026-11-06）：high 级问题阻断导出 + 显式覆写
//
// 后端在导出前按 severity 拦截 high 级 issue，返回 409 + 结构化错误体
// （error=export_blocked_high_issues）。前端必须让用户**显式**选择「已知悉并继续」，
// 不得静默放行；覆写续行后后端回 `X-Export-Override: high-issues` 并在审核留痕
// 中记录本次覆写。覆写信号与 `X-Export-Degraded`（后端降级）语义分离，互不复用。
// =========================================================================

/** 阻断项（后端 high_issues 数组元素）。 */
export interface ExportBlockedIssue {
  type: string;
  rule_id: string;
  severity: string;
  count: number;
  title: string;
  /** ✅ R212（2026-11-10 · P3-4）：**可区分标题**（R207 A5 后端已产出：
   *  同 rule_id 的不同子类（待审 / 驳回 / 无记录）拼成「标题·子类」）。
   *    前端结构化解析此前把它丢了 —— 用户能看到区分文案，只是因为后端把
   *    display_title 拼进了 detail 字符串这一条**偶然**路径；任何按 issues[]
   *    逐条渲染的改动都会退回同名。 */
  display_title?: string;
}

/** 导出被阻断的结构化信息。 */
export interface ExportBlockedInfo {
  code: "export_blocked_high_issues";
  detail: string;
  issueCount: number;
  issueTotal: number;
  issues: ExportBlockedIssue[];
  overrideHint: string;
}

/** 把 Blob / 对象错误体统一读成 JSON 对象；非 JSON 返回 null。 */
async function readJsonBody(data: unknown): Promise<Record<string, unknown> | null> {
  let parsed: unknown = data;
  if (typeof Blob !== "undefined" && data instanceof Blob) {
    try {
      const text = (await data.text()).trim();
      if (!text.startsWith("{")) return null;
      parsed = JSON.parse(text);
    } catch {
      return null;
    }
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  return parsed as Record<string, unknown>;
}

/**
 * 解析「high 级问题阻断导出」错误体；非该错误（网络错误 / 其它 4xx）返回 null。
 * 返回 null 时调用方继续走 `resolveExportError` 的既有文案路径。
 */
export async function parseExportBlock(error: unknown): Promise<ExportBlockedInfo | null> {
  const err = (typeof error === "object" && error !== null ? error : {}) as {
    response?: { data?: unknown };
  };
  const obj = await readJsonBody(err.response?.data);
  if (!obj || obj.error !== "export_blocked_high_issues") return null;
  const rawIssues = Array.isArray(obj.high_issues) ? obj.high_issues : [];
  const issues: ExportBlockedIssue[] = rawIssues.map((it) => {
    const rec = (typeof it === "object" && it !== null ? it : {}) as Record<string, unknown>;
    return {
      type: typeof rec.type === "string" ? rec.type : "",
      rule_id: typeof rec.rule_id === "string" ? rec.rule_id : "",
      severity: typeof rec.severity === "string" ? rec.severity : "high",
      count: typeof rec.count === "number" ? rec.count : 1,
      title: typeof rec.title === "string" ? rec.title : "",
      // ✅ R212（P3-4）：保留后端产出的可区分标题（此前被丢弃）
      display_title: typeof rec.display_title === "string" ? rec.display_title : undefined,
    };
  });
  const issueCount =
    typeof obj.high_issue_count === "number" ? obj.high_issue_count : issues.length;
  const issueTotal =
    typeof obj.high_issue_total === "number"
      ? obj.high_issue_total
      : issues.reduce((sum, it) => sum + it.count, 0);
  return {
    code: "export_blocked_high_issues",
    detail:
      stringField(obj, "detail") ??
      `导出已被阻断：存在 ${issueCount} 类高风险问题（共 ${issueTotal} 处），请先修复或确认已知悉后继续`,
    issueCount,
    issueTotal,
    issues,
    overrideHint: stringField(obj, "override_hint") ?? "",
  };
}

/** 解析 `X-Export-Override`；头缺失返回 null（未覆写时零提示不变）。 */
export function parseExportOverride(headers: unknown): string | null {
  const raw = headerValue(headers, "x-export-override");
  if (!raw) return null;
  const signal = raw.trim().toLowerCase();
  return signal || null;
}

// =========================================================================
// ✅ R183-补（2026-11-07）：严格模式「编号结构自检未执行」必须对用户可见
//
// 后端严格模式本应在导出前做两道校验：正文编号（validate_scheme_numbering_
// consistency）+ 结构编号（只读复用 sections._outline_structure_report）。
// 后者崩溃或符号不可用时，后端**不阻断导出**（自检崩了不能让用户导不出），
// 只在 200 响应头 `X-Numbering-Check` 里回传 `checked=false` + 原因。
// 若前端不消费该头，界面就只剩「导出成功 / 导出失败」二态 ——
// 「没检查」与「检查通过」长得一模一样，正是本仓要消除的隐性缺陷。
//
// 注意：该头**只在严格模式**下发（非严格模式返回 `{}` → 无头）。因此
// 「头缺失」= 本来就没承诺检查，**不得**据此发提示；只有头明确说
// `checked=false` 才提示。这样既满足「没检查必须显性」，也不误报。
// =========================================================================

/** `X-Numbering-Check` 回传的编号结构自检结论（后端百分号编码 JSON）。 */
export interface NumberingCheckInfo {
  /** 自检是否真的执行了。false = 没跑成（崩溃 / 符号不可用），不是「结构一致」。 */
  checked: boolean;
  /** 结构是否一致；未执行时为 null（不得当成 true）。 */
  consistent: boolean | null;
  /** 存储编号与树位置不一致的章节数；未执行时为 null。 */
  mismatched?: number | null;
  /** 未执行原因（checked=false 时后端给出）。 */
  reason?: string;
}

/**
 * 解析 `X-Numbering-Check`；头缺失 / 百分号解码失败 / JSON 非法 /
 * 缺 `checked` 布尔字段一律返回 null，不抛错、不影响下载。
 */
export function parseNumberingCheck(headers: unknown): NumberingCheckInfo | null {
  const raw = headerValue(headers, "x-numbering-check");
  if (!raw) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(decoded);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.checked !== "boolean") return null;
  return {
    checked: obj.checked,
    consistent: obj.consistent === true ? true : obj.consistent === false ? false : null,
    mismatched: typeof obj.mismatched === "number" ? obj.mismatched : null,
    reason: typeof obj.reason === "string" && obj.reason ? obj.reason : undefined,
  };
}

/**
 * 「编号结构自检未执行」告警文案；自检已执行（`checked=true`）返回 null。
 *
 * 只在 `checked === false` 时提示：严格模式声明了结构闸门，闸门没跑成
 * 就不能让界面停留在「导出成功 = 一切正常」。非严格模式后端不发该头，
 * 这里同样返回 null —— 不把「本来就没承诺检查」误报成异常。
 */
export function numberingCheckWarning(info: NumberingCheckInfo | null): string | null {
  if (!info || info.checked) return null;
  const reason = info.reason ? `：${info.reason}` : "";
  return `编号结构自检未执行${reason}。本次导出已成功，但严格模式的编号结构校验未生效，结论不代表「结构一致」，建议稍后重试或检查服务日志`;
}
