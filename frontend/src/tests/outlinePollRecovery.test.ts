// @vitest-environment jsdom
/**
 * 目录生成「断线重挂成果恢复」轮询逻辑测试（2026-09-20 补齐）。
 *
 * pollTaskUntilTerminalImpl（自 SchemeWorkbenchPage 提取的模块级实现）：
 *   1. 终态直通：completed/stopped 响应原样返回（含 checkpoint 回传的
 *      outline_result，供上层 fin.outline_result 消费分支恢复目录成果）；
 *   2. stuckAtFull 兜底：progress≥0.99 且 status=running 连续 3 次 → 判定
 *      completed，且必须保留全量字段（✅ BUG C 回归：旧实现丢 outline_result，
 *      未入库目录「看起来丢了」）；
 *   3. 进度未满时计数重置，不提前兜底；
 *   4. 瞬时网络失败容忍（成功一次即重置计数）；404 立即放弃；连续失败达
 *      阈值放弃重挂；
 *   5. onPending 只在中途轮询回调（终态不刷"后台继续"提示）。
 */
import { describe, it, expect, vi } from "vitest";
import {
  pollTaskUntilTerminalImpl,
  type TaskTerminalInfo,
} from "../pages/SchemeWorkbenchPage";

const OUTLINE_RESULT = {
  outline: [{ title: "第一章 工程概况", children: [] }],
  failed_chapters: ["第三章"],
  failed_count: 1,
};

function seq(...items: Array<TaskTerminalInfo | Error>) {
  let i = 0;
  return vi.fn(async () => {
    const it = items[i++];
    if (it instanceof Error) throw it;
    return it;
  });
}

const netErr = (status: number) => {
  const e: any = new Error(`HTTP ${status}`);
  e.response = { status };
  return e;
};

const FAST = { intervalMs: 0, maxPolls: 50 };

describe("pollTaskUntilTerminalImpl（断线重挂轮询）", () => {
  it("终态直通：completed 响应原样返回（含 outline_result），不触发 onPending", async () => {
    const onPending = vi.fn();
    const fetchStatus = seq({
      status: "completed", message: "目录生成完成", progress: 1,
      outline_result: OUTLINE_RESULT,
    });
    const fin = await pollTaskUntilTerminalImpl("t1", fetchStatus, onPending, FAST);
    expect(fin?.status).toBe("completed");
    expect(fin?.outline_result).toEqual(OUTLINE_RESULT);
    expect(onPending).not.toHaveBeenCalled();
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("stopped 终态同样带回 checkpoint 成果（本轮后端修复的 stopped 携带契约）", async () => {
    const fin = await pollTaskUntilTerminalImpl(
      "t2",
      seq({ status: "stopped", message: "用户已停止", progress: 0.6, outline_result: OUTLINE_RESULT }),
      undefined,
      FAST,
    );
    expect(fin?.status).toBe("stopped");
    expect(fin?.outline_result?.outline).toHaveLength(1);
  });

  it("BUG C 回归：stuckAtFull 兜底判定 completed 必须保留 outline_result 等全量字段", async () => {
    const stuck: TaskTerminalInfo = {
      status: "running", progress: 1, message: "目录生成完成",
      outline_result: OUTLINE_RESULT,
    };
    const fetchStatus = seq(stuck, stuck, stuck);
    const fin = await pollTaskUntilTerminalImpl("t3", fetchStatus, undefined, FAST);
    expect(fin?.status).toBe("completed");
    expect(fin?.message).toBe("目录生成完成");
    expect(fin?.outline_result).toEqual(OUTLINE_RESULT);
    expect(fetchStatus).toHaveBeenCalledTimes(3);
  });

  it("进度未满 / 非 running 时 stuckAtFull 重置，不提前兜底", async () => {
    const fetchStatus = seq(
      { status: "running", progress: 1 },
      { status: "running", progress: 1 },
      { status: "running", progress: 0.5 },   // ← 计数清零
      { status: "running", progress: 1 },
      { status: "running", progress: 1 },
      { status: "completed", message: "ok" }, // ← 正常终态出口
    );
    const fin = await pollTaskUntilTerminalImpl("t4", fetchStatus, undefined, {
      ...FAST, maxPolls: 10,
    });
    expect(fin?.status).toBe("completed");
    expect(fetchStatus).toHaveBeenCalledTimes(6);
  });

  it("瞬时网络失败容忍：成功一次即重置计数（502 × 4 夹杂成功 → 最终拿到终态）", async () => {
    const fetchStatus = seq(
      netErr(502), netErr(502),
      { status: "running", progress: 0.3 },
      netErr(502), netErr(502),
      { status: "stopped", message: "用户已停止" },
    );
    const fin = await pollTaskUntilTerminalImpl("t5", fetchStatus, undefined, FAST);
    expect(fin?.status).toBe("stopped");
  });

  it("404（任务确实不存在）立即放弃重挂", async () => {
    const fetchStatus = seq(netErr(404), { status: "completed" });
    const fin = await pollTaskUntilTerminalImpl("t6", fetchStatus, undefined, FAST);
    expect(fin).toBeNull();
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("连续网络失败达容错上限（默认 5 次）放弃重挂", async () => {
    const fetchStatus = vi.fn(async () => { throw netErr(500); });
    const fin = await pollTaskUntilTerminalImpl("t7", fetchStatus, undefined, {
      ...FAST, netFailTolerance: 5,
    });
    expect(fin).toBeNull();
    expect(fetchStatus).toHaveBeenCalledTimes(5);
  });

  it("onPending 每次中途轮询都回调（进度提示保持实时）", async () => {
    const onPending = vi.fn();
    const fetchStatus = seq(
      { status: "running", progress: 0.2, message: "第1章" },
      { status: "running", progress: 0.4, message: "第2章" },
      { status: "completed", message: "done" },
    );
    await pollTaskUntilTerminalImpl("t8", fetchStatus, onPending, FAST);
    expect(onPending).toHaveBeenCalledTimes(2);
    expect(onPending.mock.calls[1][0].message).toBe("第2章");
  });
});
