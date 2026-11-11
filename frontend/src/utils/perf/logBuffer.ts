/**
 * SSE 实时日志的「写入端」有界缓冲。
 *
 * 背景：日志列表在渲染层已有 SECTION_LOG_RENDER_MAX / OUTLINE_LOG_RENDER_MAX
 * 截断，但那只是 slice —— 数据源仍是无限追加的 `[...list, item]`，长方案
 * 上千条事件时每次 append 都是 O(n) 复制，且整份数组一直被 ref/state 持有。
 * 渲染层截断 ≠ 内存截断，故在写入侧统一收口。
 *
 * 保留「最近 N 条」与原有「相邻同文案去重」语义；返回新数组引用以便 React
 * 正常感知变化。刻意不改日志条目结构（time/progress/message 等字段逐字保留）。
 */
export const LIVE_LOG_MAX = 300;

export function appendBoundedLog<T>(list: readonly T[], item: T, max = LIVE_LOG_MAX): T[] {
  const limit = Number.isFinite(max) && max > 0 ? Math.floor(max) : LIVE_LOG_MAX;
  const base = list.length >= limit ? list.slice(list.length - limit + 1) : Array.from(list);
  base.push(item);
  return base;
}
