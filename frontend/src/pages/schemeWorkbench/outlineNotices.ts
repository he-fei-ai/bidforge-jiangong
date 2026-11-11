/**
 * R213（2026-11-10）· 目录生成模块「加法式后端信号」的**唯一前端文案出口**。
 *
 * 背景：后端 R209/R210 逐步加了 `clamped_depth` / `clamped_nodes` /
 * `unmounted_count` / `level1_truncated` / `level1_total` 五个「不阻断但必须可见」
 * 的信号，前端要么零消费、要么只在在线 completed 一处内联手写 ⇒ 停止 / 断线
 * 重挂 / 目录库保存等路径下，用户把「被裁剪、被截断、有不可达章节」的目录当
 * 完整成果。这里把判据与文案收成纯函数，供所有消费点复用并由 vitest 直测。
 */

/** 后端各响应里与本模块相关的加法式键（未出现 = 0/无信号）。 */
export interface OutlineNoticePayload {
  level1_truncated?: unknown;
  level1_total?: unknown;
  clamped_depth?: unknown;
  clamped_nodes?: unknown;
  unmounted_count?: unknown;
}

function positive(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** 一级目录被上限截断（SSE 终态 / checkpoint 重挂共用）。 */
export function outlineTruncationNotice(payload: OutlineNoticePayload | null | undefined): string | null {
  const dropped = positive(payload?.level1_truncated);
  if (!dropped) return null;
  const total = positive(payload?.level1_total);
  const kept = total > dropped ? total - dropped : 0;
  return `模型返回的一级目录过多（${total || "?"} 章），已截断保留前 ${kept || "若干"} 章（丢弃 ${dropped} 章），请检查目录是否完整`;
}

/** save-outline 把超过三级的节点折叠进父章节说明（R209 后端回传）。 */
export function outlineClampNotice(payload: OutlineNoticePayload | null | undefined): string | null {
  const nodes = positive(payload?.clamped_nodes);
  if (!nodes) return null;
  const depth = positive(payload?.clamped_depth);
  return `目录提交深度 ${depth || "超过 3"} 级，超出三级上限：${nodes} 个深层节点已折叠进父章节说明，请检查层级`;
}

/** list_sections 检出成环 / 悬挂的不可达章节（R210 后端回传）。 */
export function outlineUnmountedNotice(payload: OutlineNoticePayload | null | undefined): string | null {
  const count = positive(payload?.unmounted_count);
  if (!count) return null;
  return `检出 ${count} 个不可达章节（父子关系成环或悬挂）：目录树无法显示，导出也会缺章，建议重建目录或恢复快照`;
}
