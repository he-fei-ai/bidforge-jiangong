/**
 * SSE 事件高频 setState 批量器（rAF 合帧）
 * ===========================================
 * 背景：正文/目录/事实生成时，单个 SSE 事件往往在同一微任务里触发
 *      setSectionLogs + setProgress + setProgressMsg + setGenStats 共 4+ 次
 *      setState；后端心跳 ping 又是 100ms 级。React 18 虽在同 tick 内自动
 *      批处理，但跨 await 边界（每次 read() 都是一次 await）就退化为每次
 *      事件一次独立 render。这里把写入延迟到下一帧、按 key 只保留最新值，
 *      既保留最终语义，又把 render 频率压到 ≤ 1 次 / 帧。
 *
 * 从 SchemeWorkbenchPage.tsx 抽出（2026-09-25）：常驻的活动状态栏
 * （TaskStatusBar）同样存在「每条快照一次 setState」的问题，需要一个与页面
 * 解耦的公共实现。此前工具函数寄生在 9000+ 行的页面文件里，跨组件复用会
 * 造成反向依赖（小组件 import 巨型页面），故收敛到 utils/。
 *
 * 使用方式：
 *   const batcher = createSseBatcher();
 *   batcher.schedule("progress", () => setProgress(p));
 *   // 收尾时保证最终态落地
 *   batcher.flushNow();
 *   // 卸载时清掉未刷任务，防止 React 已卸载后 setState
 *   batcher.stop();
 */
export type SseBatcher = {
  schedule: (key: string, fn: () => void) => void;
  /** 组件卸载时调用：丢弃所有未刷任务，并 cancel 已排的 rAF */
  stop: () => void;
  /** 立即把已排任务刷下去（不排新帧）。用于 completed/stopped/error 收尾时
   *  保证日志/进度写到位，用户看到最终态 */
  flushNow: () => void;
};

export function createSseBatcher(): SseBatcher {
  const pending = new Map<string, () => void>();
  let rafId: number | null = null;
  let stopped = false;
  function flush() {
    rafId = null;
    const tasks = Array.from(pending.values());
    pending.clear();
    // 逐个 apply；某一项抛错不能阻断其他项刷新
    for (const t of tasks) {
      try { t(); } catch { /* 单条失败静默 */ }
    }
  }
  function schedule(key: string, fn: () => void) {
    if (stopped) return;
    pending.set(key, fn);
    if (rafId === null) {
      rafId = typeof requestAnimationFrame === "function"
        ? requestAnimationFrame(() => flush())
        : 0; // 非浏览器环境降级：同步 flush
      if (rafId === 0) flush();
    }
  }
  return {
    schedule,
    stop() {
      stopped = true;
      pending.clear();
      if (rafId !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
      rafId = null;
    },
    flushNow() {
      if (rafId !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafId);
      }
      rafId = null;
      flush();
    },
  };
}
