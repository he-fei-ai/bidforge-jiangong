/**
 * 方案工作台 · 任务轮询到终态的纯实现（从 SchemeWorkbenchPage.tsx 下沉，R121）。
 *
 * 抽离动机（R120 遗留 L5）：`pollTaskUntilTerminalImpl` 本就是「可注入依赖、可单测」
 * 的模块级纯函数，此前留在 12669 行巨型页面里，导致 `contentDegradedTerminal` 的
 * B1/B2 只能 `await import("../pages/SchemeWorkbenchPage")` 才能测它 —— 全量并发
 * （8-worker 冷 jsdom + 超大模块编译）下这一下动态 import 撞单测 60s 墙钟产生**假红**
 * （单独无并发复跑 12/12 全绿，非回归）。下沉到本叶子后，测试直接静态引本模块，
 * 整个测试文件不再牵入页面。
 *
 * 行为逐字保持：终态判定仍走单一事实源 `isTerminalTaskStatus`（含 degraded），
 * stuck-at-full 兜底保留全量字段（spread），404 与瞬时网络失败分级处理不变。
 *
 * 类型用最小结构约束 `PollableTaskTerminal`（status/progress/message + 索引签名），
 * 并以泛型 `T` 保留调用方的具体终态类型（页面侧仍返回其 `TaskTerminalInfo` 全量字段），
 * 因此对本函数唯一的行为消费者（status / progress 分支）零影响、零类型分叉、零新增依赖。
 */
import { isTerminalTaskStatus } from "./contentEvents";

/** 轮询可见的最小终态字段；结构上兼容 pages/types 各处 TaskTerminalInfo。 */
export interface PollableTaskTerminal {
  status: string;
  progress?: number;
  message?: string;
  [k: string]: unknown;
}

export async function pollTaskUntilTerminalImpl<T extends PollableTaskTerminal>(
  taskId: string,
  fetchStatus: (taskId: string) => Promise<T>,
  onPending?: (data: T) => void,
  opts: {
    intervalMs?: number;
    maxPolls?: number;
    stuckThreshold?: number;
    netFailTolerance?: number;
  } = {},
): Promise<T | null> {
  const intervalMs = opts.intervalMs ?? 3000;
  const maxPolls = opts.maxPolls ?? 200;
  const stuckThreshold = opts.stuckThreshold ?? 3;
  const netFailTolerance = opts.netFailTolerance ?? 5;
  let stuckAtFull = 0; // progress==1.0 但 status 仍 running 的连续次数
  let netFailCount = 0; // 连续网络失败计数
  for (let i = 0; i < maxPolls; i++) {
    await new Promise((r) => setTimeout(r, intervalMs));
    try {
      const data = await fetchStatus(taskId);
      netFailCount = 0; // ✅ 成功一次即重置，瞬时网络抖动不应终止重挂
      // ✅ 2026-10-10：终态集合改由 isTerminalTaskStatus 单一事实源判定。
      //    旧实现内联 `completed/failed/stopped`，把后端 2026-10-07 引入的
      //    加法式终态 degraded 漏掉 ⇒ 降级完成时既不匹配终态、也不匹配下方
      //    「progress 满但 running」兜底，页面卡在「正在重新挂接…」200×3s。
      if (isTerminalTaskStatus(data.status)) return data;
      onPending?.(data);
      // ✅ 进度已满但 status 没更新：连续达阈值就兜底为 completed
      if ((data.progress || 0) >= 0.99 && !isTerminalTaskStatus(data.status)) {
        stuckAtFull++;
        if (stuckAtFull >= stuckThreshold) {
          console.warn(`pollTaskUntilTerminal: task ${taskId} stuck at progress=100% for ${stuckAtFull} polls, treating as completed`);
          // ✅ BUG 修复（2026-09-20）：兜底判定 completed 时必须保留全量字段
          //   （尤其 outline_result）—— 后台成果已落 checkpoint，丢字段会让
          //   调用方（fin.outline_result 消费分支）误判无成果直接 load()。
          return { ...data, status: "completed", message: data.message } as T;
        }
      } else {
        stuckAtFull = 0;
      }
    } catch (e: any) {
      // ✅ 修复：区分「任务确实不存在（404，不可恢复）」与「瞬时网络失败」。
      // 旧实现对任何一次请求失败都直接放弃重挂，一次抖动/502 就导致兜底失效。
      if (e?.response?.status === 404) return null;
      netFailCount++;
      if (netFailCount >= netFailTolerance) {
        console.warn(`pollTaskUntilTerminal: task ${taskId} 连续 ${netFailCount} 次网络失败，放弃重挂`);
        return null;
      }
    }
  }
  return null;
}
