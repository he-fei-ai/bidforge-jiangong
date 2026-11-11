import { type GenStats } from "../../utils/contentEvents";
import { GenerationProgressCard } from "./GenerationProgressCard.tsx";
import { SchemeTreePanel } from "./SchemeTreePanel.tsx";

export function fmtDuration(ms?: number | null): string {
  if (!ms || ms <= 0) return "0秒";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return s ? `${m}分${s}秒` : `${m}分钟`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}小时${m % 60}分` : `${h}小时`;
}

/**
 * ✅ 进度增强：从 SSE 事件载荷中提取运行统计。
 * 三种来源共用同一口径：stats 事件、心跳 ping 事件、progress 事件内嵌的 stats。
 */
export function pickStats(src: unknown): GenStats {
  // ✅ any 清理（2026-11-02）：入参收窄为 unknown，字段经 Partial<GenStats> 投影直读
  const s = (src && typeof src === "object" ? src : {}) as Partial<GenStats>;
  return {
    elapsed_ms: s?.elapsed_ms,
    eta_ms: s?.eta_ms,
    done: s?.done,
    // R175 BUG-4：已处理章节数（成功 + 失败），与终态 checkpoint 同口径
    processed: s?.processed,
    total: s?.total,
    failed: s?.failed,
    words: s?.words,
    avg_section_ms: s?.avg_section_ms,
    concurrency: s?.concurrency,
    phase: s?.phase,
    phase_label: s?.phase_label,
    progress: s?.progress,
    stepwise: s?.stepwise,
    nodes: s?.nodes,
    running: s?.running,
  };
}

/**
 * ✅ 性能优化：判断两个 GenStats 载荷是否语义相等（浅比较所有字段）。
 * 背景：后端 stats/ping 事件每 100–1000ms 推送一次，若载荷与当前状态相同
 *      仍调用 setGenStats 会触发整页（含 GenerationProgressCard、树、Tab）
 *      一次无意义重渲。此函数配合 setState(prev => equalStats ? prev : next)
 *      跳过值不变时的 setState，消除高频 ping 带来的抖动。
 */
export function statsEqual(a: GenStats | undefined, b: GenStats): boolean {
  if (!a) return false;
  return (
    a.elapsed_ms === b.elapsed_ms &&
    a.eta_ms === b.eta_ms &&
    a.done === b.done &&
    a.processed === b.processed &&
    a.total === b.total &&
    a.failed === b.failed &&
    a.words === b.words &&
    a.avg_section_ms === b.avg_section_ms &&
    a.concurrency === b.concurrency &&
    a.phase === b.phase &&
    a.phase_label === b.phase_label &&
    a.progress === b.progress &&
    a.stepwise === b.stepwise &&
    a.nodes === b.nodes &&
    runningEqual(a.running, b.running)
  );
}

/** ✅ R217（2026-11-11 · P2）：running 是数组，JSON 反序列化后每次都是新
 *  引用 —— 旧实现按引用比较恒 false，心跳去重（本节注释声称消除的
 *  「高频 ping 抖动」）从未生效。按内容比较，元素为扁平统计对象。 */
function runningEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((v, i) => JSON.stringify(v) === JSON.stringify(b[i]));
}

// ✅ P0-5 组件拆分：目录树辅助纯函数（原组件内定义，无闭包依赖，移模块级供 SchemeTreePanel 复用）
