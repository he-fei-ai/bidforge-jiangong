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
  return parts;
}

export interface FixStats {
  formulas?: number;
  block_formulas?: number;
  replacement_chars?: number;
  control_chars?: number;
  gbk_mojibake?: number;
  latin1_mojibake?: number;
  cyrillic?: number;
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
  return parts;
}
