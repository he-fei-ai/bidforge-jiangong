/**
 * findRecentOutlineTaskImpl（自 SchemeWorkbenchPage 提取的模块级实现）回归测试
 *
 * 背景（2026-09-23 · 「30s 连接超时后目录任务孤儿运行」）：
 * SSE 在收到首个事件（含 task_id 的 connecting）之前建连失败/超时时，
 * 前端 taskId 为空串，旧实现直接跳过 pollTaskUntilTerminal 重挂 ——
 * 但后端任务可能已注册并继续生成（AI 照常计费），成果只躺在 checkpoint。
 * findRecentOutlineTaskImpl 按方案检索最近的 running/paused 目录任务
 * 供重挂接。
 *
 * 反例回归：
 * - 只认 outline_generation 类型（不误挂正文/事实任务）；
 * - 终态任务（completed/failed/stopped）不重挂；
 * - updated_at 超出时间窗（旧任务）不重挂；
 * - updated_at 脏值/缺字段保守接受（宁可重挂不漏挂）；
 * - 检索请求本身失败静默返回 null（保持旧行为，仅提示）。
 */
import { describe, expect, it } from "vitest";
import { findRecentOutlineTaskImpl } from "../pages/SchemeWorkbenchPage";

const NOW = Date.parse("2026-09-23T20:00:00Z");

function mkTask(over: Record<string, any> = {}) {
  return {
    id: "t1",
    task_type: "outline_generation",
    status: "running",
    updated_at: "2026-09-23T19:58:00Z",
    ...over,
  };
}

describe("findRecentOutlineTaskImpl（超时后按方案检索可重挂任务）", () => {
  it("命中：最近的 running 目录任务返回其 id", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({ tasks: [mkTask()] }),
      { nowMs: NOW },
    );
    expect(got).toBe("t1");
  });

  it("只认 outline_generation：不误挂正文/事实任务", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({
        tasks: [
          mkTask({ id: "c1", task_type: "content_generation" }),
          mkTask({ id: "f1", task_type: "facts_generation" }),
        ],
      }),
      { nowMs: NOW },
    );
    expect(got).toBeNull();
  });

  it("终态任务不重挂", async () => {
    for (const status of ["completed", "failed", "stopped"]) {
      const got = await findRecentOutlineTaskImpl(
        async () => ({ tasks: [mkTask({ status })] }),
        { nowMs: NOW },
      );
      expect(got, `status=${status} 不应重挂`).toBeNull();
    }
  });

  it("updated_at 超出时间窗（陈旧任务）不重挂", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({ tasks: [mkTask({ updated_at: "2026-09-23T10:00:00Z" })] }),
      { nowMs: NOW, windowMs: 5 * 60_000 },
    );
    expect(got).toBeNull();
  });

  it("paused 任务可重挂（用户暂停后断线的场景）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({ tasks: [mkTask({ status: "paused" })] }),
      { nowMs: NOW },
    );
    expect(got).toBe("t1");
  });

  it("updated_at 脏值/缺字段保守接受（宁可重挂不漏挂）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({ tasks: [mkTask({ updated_at: "not-a-date" })] }),
      { nowMs: NOW },
    );
    expect(got).toBe("t1");
  });

  it("多条任务时按列表顺序取第一条可重挂者（后端按 created_at DESC 排序）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => ({
        tasks: [
          mkTask({ id: "new", status: "failed" }),
          mkTask({ id: "old" }),
        ],
      }),
      { nowMs: NOW },
    );
    expect(got).toBe("old");
  });

  it("检索请求失败静默返回 null（保持旧行为：仅提示不重挂）", async () => {
    const got = await findRecentOutlineTaskImpl(
      async () => {
        throw new Error("network down");
      },
      { nowMs: NOW },
    );
    expect(got).toBeNull();
  });
});
