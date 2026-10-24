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
  return parts;
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
  /**
   * ✅ R117 FIX-10（2026-10-20）：同章内**逐字重复**的正文块被折叠的次数。
   * 后端已删掉重复副本，用户若在成稿里仍看到同一节内容两遍，说明命中了
   * 旧导出缓存（`_EXPORTER_VERSION` 已升至 25，重新导出即可）。
   */
  repeated_blocks?: number;
  /**
   * ✅ R117 FIX-11（2026-10-20）：同章内「前 60 字相同、后文分歧」的**近似重复**
   * 正文块数。后端**刻意不改写**成稿（两侧各有独有内容，删任一侧即丢正文），
   * 所以这一项必须让用户看到——它是「回源头重新生成该章」的唯一指路牌。
   */
  near_duplicate_blocks?: number;
  /**
   * ✅ R128（2026-10-22）：正文「图 X-Y / 表 X-Y」引用升级为可点击 REF 域
   * 的处数。只对真实存在的题注书签包域（未登记编号保持纯文本）；
   * Word/WPS 打开后更新域即可随编号刷新/点击跳转。
   */
  cross_ref_fields?: number;
  /**
   * ✅ R129（2026-10-22 · 引入易标式导出机制）：清除了 Word 2010+ 专有
   * 命名空间声明的 XML 部件数（w14/wp14/wps/wpg 等 —— WPS/LibreOffice
   * 兼容性收口，产物只保留 OOXML Transitional 基础命名空间）。
   */
  ns_parts_sanitized?: number;
  /** ✅ R129：导出后校验检出的 Markdown 语法残留段落数（渲染链路漏洞）。 */
  postcheck_markdown_residue?: number;
  /** ✅ R129：导出后校验检出的 HTML 标签残留段落数。 */
  postcheck_html_residue?: number;
  /** ✅ R129：导出后校验检出的 HTML 实体残留段落数。 */
  postcheck_entity_residue?: number;
  /** ✅ R129：导出后校验检出的占位符残留段落数。 */
  postcheck_placeholder_residue?: number;
  /** ✅ R129：公式 OMML 转换失败后降级为图片嵌入的处数。 */
  formula_image_fallback?: number;
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
  if (fs.repeated_blocks) {
    parts.push(`同章逐字重复正文块折叠 ${fs.repeated_blocks} 处（成稿仅保留首次）`);
  }
  if (fs.cross_ref_fields) {
    parts.push(`正文交叉引用升级为可点击 REF 域 ${fs.cross_ref_fields} 处（更新域后随编号刷新）`);
  }
  if (fs.ns_parts_sanitized) {
    parts.push(`已清除 Word 2010+ 专有命名空间 ${fs.ns_parts_sanitized} 个部件（WPS/LibreOffice 兼容）`);
  }
  if (fs.formula_image_fallback) {
    parts.push(`公式降级为图片嵌入 ${fs.formula_image_fallback} 处（OMML 转换失败的三级降级）`);
  }
  return parts;
}

/**
 * 导出统计里「**成稿仍有问题、后端刻意没有替你改**」的告警片段（R117 FIX-11）。
 *
 * 为什么必须与 `fixStatsParts` 分开：那边每一条都是「已自动修复」的事实，
 * 混进 `msg.success("导出前已自动修复：…")` 会让「近似重复**未改动**」读起来
 * 像已经处理过 —— 这正是 R115/R116 反复收口的那类谎报。故此处单独走 warning，
 * 并给出唯一可执行动作：回源文该章重新生成。
 */
export function fixStatsWarnings(fs: FixStats): string[] {
  const warns: string[] = [];
  if (fs.near_duplicate_blocks) {
    warns.push(
      `${fs.near_duplicate_blocks} 处正文在同一章内近似重复（前半逐字相同、后半各写各的）：` +
        "成稿未作改动——两侧各有独有内容，删任一侧都会丢正文，请回源文该章重新生成",
    );
  }
  // ✅ R129（2026-10-22）：导出后校验检出的残留 —— 与近似重复同级：
  // 后端「只体检不篡改」，成稿带着残留交付，用户是唯一能回源头修的人。
  const residueTotal =
    (fs.postcheck_markdown_residue ?? 0) +
    (fs.postcheck_html_residue ?? 0) +
    (fs.postcheck_entity_residue ?? 0) +
    (fs.postcheck_placeholder_residue ?? 0);
  if (residueTotal > 0) {
    const detail: string[] = [];
    if (fs.postcheck_markdown_residue) detail.push(`Markdown 残留 ${fs.postcheck_markdown_residue} 处`);
    if (fs.postcheck_html_residue) detail.push(`HTML 标签残留 ${fs.postcheck_html_residue} 处`);
    if (fs.postcheck_entity_residue) detail.push(`HTML 实体残留 ${fs.postcheck_entity_residue} 处`);
    if (fs.postcheck_placeholder_residue) detail.push(`占位符残留 ${fs.postcheck_placeholder_residue} 处`);
    warns.push(`导出后校验检出：${detail.join("、")}——请回源文对应章节修正后重新导出`);
  }
  return warns;
}
