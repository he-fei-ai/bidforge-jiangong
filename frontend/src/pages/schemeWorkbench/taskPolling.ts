import { facts_result as GeneratedFactsResult, type facts_result } from "../../types/checkpointSchema";
import { isTerminalTaskStatus } from "../../utils/contentEvents";
import { applyFactsCompletedEvent } from "./factsShared.ts";

/** GET /sse/task/{id} 终态响应（含 checkpoint 回传的 outline_result / content_result） */
export interface TaskTerminalInfo {
  status: string;
  message?: string;
  progress?: number;
  outline_result?: {
    outline?: unknown[];
    // ✅ R79 对齐单一事实源 `backend/app/services/checkpoint_schema.py`
    //    （原声明 list[dict] 是文档失真，实收值是章标题字符串数组）；
    //    本文件另有本地副本（未 import 生成的 types/checkpointSchema.ts），
    //    两处必须同口径，否则前端读法会把字符串当对象遍历。
    failed_chapters?: string[];
    failed_count?: number;
    /**
     * ✅ 契约补齐（2026-09-21）：后端 `_CHECKPOINT_KINDS.outline_generation`
     * 白名单含 `review`，断线重挂后目录的审核/修复结果此前在前端无处安放。
     */
    review?: unknown;
    /**
     * ✅ 契约补齐：checkpoint 记录的是**终态事件类型**（completed/stopped/error），
     * 前端此前只能靠 `status` 推断，无法区分「用户主动停止保留的部分成果」
     * 与「AI 异常后保留的部分成果」，两者的补救建议不同。
     */
    event?: string;
    /** partial=true 表示成果不完整（停止 / 失败兜底），需提示用户手动补齐 */
    partial?: boolean;
  };
  /** ✅ G12-6：正文生成 checkpoint（后端 `_CHECKPOINT_KINDS.content_generation` 白名单） */
  content_result?: {
    event?: string;
    message?: string;
    done?: number;
    total?: number;
    failed_count?: number;
    failed_sections?: Array<{ section_id?: string; title?: string; reason?: string }>;
    words?: number;
    word_count?: number;
    run_words?: number;
    over_count?: number;
  };
  /**
   * ✅ 契约补齐（2026-09-27）：全局事实提取 checkpoint
   * （后端 `_CHECKPOINT_KINDS.facts_generation` 白名单，kind=`facts_result`）。
   *
   * 此前后端无此通道（既不写 checkpoint 也不在白名单里），断线重挂只能拿到
   * status+message，segment_stats / cross_conflicts 全丢 —— 用户看到「已完成」
   * 却不知道哪几段失败、是否存在跨段矛盾。
   *
   * ✅ R89（2026-10-17）：类型改为**直接引用生成的契约**（`checkpointSchema.ts`
   * 由 `backend/app/services/checkpoint_schema.py` 生成），不再在本文件内联
   * 第三份手抄。旧实现内联副本与生成器产出可能分叉，而分叉方向正是本仓反复
   * 记账的「同一判据多副本」—— `facts_result` 能活到今天就是因为既不在生成器
   * 源里、也没有 parity 断言（内容/目录两族都有）。
   * `cross_conflicts` 仍是 `unknown[]`：跨段矛盾的具体形态由 facts_extractor
   * 决定，前端 applyFactsCompletedEvent 只按数组消费。
   */
  facts_result?: GeneratedFactsResult;
  [k: string]: unknown;
}

export async function pollTaskUntilTerminalImpl(
  taskId: string,
  fetchStatus: (taskId: string) => Promise<TaskTerminalInfo>,
  onPending?: (data: TaskTerminalInfo) => void,
  opts: {
    intervalMs?: number;
    maxPolls?: number;
    stuckThreshold?: number;
    netFailTolerance?: number;
  } = {},
): Promise<TaskTerminalInfo | null> {
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
          return { ...data, status: "completed", message: data.message };
        }
      } else {
        stuckAtFull = 0;
      }
    } catch (e: unknown) {
      // ✅ 修复：区分「任务确实不存在（404，不可恢复）」与「瞬时网络失败」。
      // 旧实现对任何一次请求失败都直接放弃重挂，一次抖动/502 就导致兜底失效。
      const err = e as { response?: { status?: number } } | null | undefined;
      if (err?.response?.status === 404) return null;
      netFailCount++;
      if (netFailCount >= netFailTolerance) {
        console.warn(`pollTaskUntilTerminal: task ${taskId} 连续 ${netFailCount} 次网络失败，放弃重挂`);
        return null;
      }
    }
  }
  return null;
}

/**
 * ✅ BUG 修复（2026-09-23 · 「30s 连接超时后目录任务孤儿运行」）：
 * SSE 在收到首个事件（含 task_id 的 connecting）之前建连失败/超时时，
 * 前端 taskId 为空串，旧实现直接跳过 pollTaskUntilTerminal 重挂 ——
 * 但后端 StreamingResponse 可能已返回、任务已注册并继续生成（AI 照常
 * 计费），成果只躺在 checkpoint 里。此处按方案检索最近的
 * outline_generation 任务，若仍在运行且 updated_at 在时间窗内
 * （默认 5 分钟，覆盖 30s 建连超时 + 装配/排队耗时），返回其 id 供重挂。
 * 提取为模块级实现（fetchTasks 注入），可单测。
 */
export async function findRecentOutlineTaskImpl(
  fetchTasks: () => Promise<{ tasks?: Array<Record<string, unknown>> }>,
  opts: { typeFilter?: string; windowMs?: number; nowMs?: number } = {},
): Promise<string | null> {
  const typeFilter = opts.typeFilter ?? "outline_generation";
  const windowMs = opts.windowMs ?? 5 * 60_000;
  const nowMs = opts.nowMs ?? Date.now();
  try {
    const { tasks } = await fetchTasks();
    for (const t of tasks || []) {
      if (t.task_type !== typeFilter) continue;
      if (t.status !== "running" && t.status !== "paused") continue;
      // updated_at 解析失败（脏值/缺字段）时保守接受，避免漏挂
      const ts = Date.parse(String(t.updated_at || ""));
      if (!Number.isNaN(ts) && nowMs - ts > windowMs) continue;
      return String(t.id || "");
    }
  } catch {
    // 检索失败（后端确实不可达）：静默返回 null，由调用方保持旧行为（仅提示）
  }
  return null;
}

/**
 * SSE 断线重挂统一入口（2026-10-14 · 三链收口）。
 *
 * 目录 / 正文 / 事实三条 SSE 链在错误分支里做完全相同的三件事：
 *   ① taskId 为空（30s 建连超时 / 网络抖动 / 网关重连时首个携带 task_id 的事件
 *      尚未到达）→ 按方案检索最近的同类型在跑任务（后端任务已注册并继续计费，
 *      成果只躺在 checkpoint）；
 *   ② 命中 → 先回调 onReattach（UI 提示「正在重新挂接」，必须在轮询开始前，
 *      否则用户在中断刻度上多等一个轮询周期却无任何反馈）；
 *   ③ 轮询至终态并返回 { taskId, final }。
 * 检索不到返回 null，调用方维持旧行为（仅提示，不乱挂）。
 *
 * 终态后的成果恢复（outline_result / content_result / facts_result）各链语义
 * 不同，仍由调用方处理 —— 本 helper 只收敛「检索 → 重挂 → 轮询」三段，
 * 消除三条链各自手写这三步的双写分叉（正是 P1「断线不重挂」缺陷的温床）。
 *
 * 提取为模块级实现（fetchTasks / pollTerminal 注入），可单测。
 */
export async function reattachTaskImpl(
  fetchTasks: () => Promise<{ tasks?: Array<Record<string, unknown>> }>,
  pollTerminal: (taskId: string) => Promise<TaskTerminalInfo | null>,
  opts: {
    taskId?: string;
    typeFilter?: string;
    nowMs?: number;
    onReattach?: (taskId: string) => void;
  } = {},
): Promise<{ taskId: string; final: TaskTerminalInfo | null } | null> {
  let tid = opts.taskId || "";
  if (!tid) {
    tid =
      (await findRecentOutlineTaskImpl(fetchTasks, {
        typeFilter: opts.typeFilter,
        nowMs: opts.nowMs,
      })) || "";
  }
  if (!tid) return null;
  opts.onReattach?.(tid);
  return { taskId: tid, final: await pollTerminal(tid) };
}

// ============================================================
// 一级目录确认闸门（对齐 OpenBidKit 的 outline-selection 阶段）：
// AI 生成目录后不自动入库，先由用户勾选确认一级章节；确认后保存并
// 将一级章节 locked=1（与 OpenBidKit 的 allowRootChanges=false 语义一致）
// ============================================================
