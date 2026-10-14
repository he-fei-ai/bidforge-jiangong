// @vitest-environment jsdom
/**
 * 正文生成「降级完成（degraded）」终态接线 · 组件级交互测试（2026-10-10）。
 *
 * 背景（跨模块数据传递断点）：
 * 后端 2026-10-07（P0-3）为「一致性修复静默失败」引入了加法式终态 ``degraded``：
 * 一致性修复有失败项时，SSE 终态事件名由 ``completed`` 变为 ``degraded``，
 * 任务 status 也写成 ``degraded``（``finish_task(task_id, "degraded")``）。
 * 但整个前端没有一处认领这个状态：
 *
 *   BUG-C1  SSE 终态分支（页面 / hook 两处都只判 ``completed``）
 *           ⇒ 降级完成不 break、不 load()、不给任何提示：正文停在旧内容，
 *              成稿含未修复缺陷而用户零感知。
 *   BUG-C2  页面 pollTaskUntilTerminal 终态白名单三态
 *           ⇒ degraded 既不匹配终态、也不匹配「progress 满但 running」兜底
 *              卡满 200 次轮询（≈10 分钟）后返回 null。
 *   BUG-C3  useContentGeneration 断线重挂是**无上限**递归 setTimeout，
 *           同样的三态白名单 ⇒ await 永不返回（比 C2 更严重）。
 *   BUG-C4  页面「接管后台任务」轮询三态白名单，同样漏判。
 *
 * 修法：终态集合下沉为 utils/contentEvents 的单一事实源
 * （TERMINAL_TASK_STATUSES / isTerminalTaskStatus / isTerminalContentEvent /
 * degradedConsistencyHint），四处调用点一律改由它判定。
 *
 * 本文件按 A（纯函数）/ B（轮询）/ C（hook 级 SSE 交互）/ D（A-B 反向）四组验证。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, waitFor, cleanup, act } from "@testing-library/react";
import React from "react";
import {
  TERMINAL_TASK_STATUSES,
  CONTENT_TERMINAL_EVENTS,
  isTerminalTaskStatus,
  isTerminalContentEvent,
  degradedConsistencyHint,
} from "../utils/contentEvents";

afterEach(cleanup);

// ⚠️ vi.mock 会被提升到 import 之前，工厂里引用的变量必须用 vi.hoisted 声明，
//    否则工厂执行时拿到的是 undefined（本文件首版就是这么翻的）。
const { apiStatusMock } = vi.hoisted(() => ({ apiStatusMock: vi.fn() }));
vi.mock("../api", () => ({
  tasksApi: {
    status: (...args: unknown[]) => (apiStatusMock as any)(...args),
    control: vi.fn().mockResolvedValue({ data: { ok: true } }),
  },
  sectionsApi: {
    resetContent: vi.fn().mockResolvedValue({ data: { cleared: 0 } }),
  },
}));

// ---------------------------------------------------------------------------
// A 组 · 终态判定纯函数
// ---------------------------------------------------------------------------

describe("终态集合单一事实源", () => {
  it("A1: degraded 属于任务终态且属于正文终态事件", () => {
    expect(TERMINAL_TASK_STATUSES).toContain("degraded");
    expect(TERMINAL_TASK_STATUSES).toContain("completed");
    expect(CONTENT_TERMINAL_EVENTS).toContain("degraded");
    expect(CONTENT_TERMINAL_EVENTS).toContain("completed");
  });

  it("A2: isTerminalTaskStatus 覆盖四态，并对其余值保守返回 false", () => {
    for (const s of ["completed", "degraded", "failed", "stopped"]) {
      expect(isTerminalTaskStatus(s)).toBe(true);
    }
    for (const s of ["running", "paused", "pending", "", undefined, null, 123]) {
      expect(isTerminalTaskStatus(s)).toBe(false);
    }
  });

  it("A3: isTerminalContentEvent 只认 completed / degraded", () => {
    expect(isTerminalContentEvent({ event: "degraded" })).toBe(true);
    expect(isTerminalContentEvent({ event: "completed" })).toBe(true);
    expect(isTerminalContentEvent({ event: "stopped" })).toBe(false);
    expect(isTerminalContentEvent({ event: "section_done" })).toBe(false);
    expect(isTerminalContentEvent(null)).toBe(false);
    expect(isTerminalContentEvent(undefined)).toBe(false);
  });

  it("A4: degradedConsistencyHint 只在 degraded 且确有失败时给出文案", () => {
    expect(degradedConsistencyHint({ event: "degraded", consistency_failed: 2 }))
      .toContain("一致性修复 2 处失败");
    // 兜底：顶层字段缺失时读 consistency_summary.failed
    expect(degradedConsistencyHint(
      { event: "degraded", consistency_summary: { failed: 5 } }))
      .toContain("5 处失败");
    // 非降级 / 无失败信息 → 空串（零噪音）
    expect(degradedConsistencyHint({ event: "completed", consistency_failed: 2 })).toBe("");
    expect(degradedConsistencyHint({ event: "degraded" })).toBe("");
    expect(degradedConsistencyHint({ event: "degraded", consistency_failed: 0 })).toBe("");
    expect(degradedConsistencyHint(null)).toBe("");
  });
});

// ---------------------------------------------------------------------------
// B 组 · 轮询必须认 degraded（BUG-C2）
// ---------------------------------------------------------------------------

describe("pollTaskUntilTerminalImpl 认领 degraded", () => {
  it("B1: status=degraded 必须在第一次轮询就收尾（不得跑满 maxPolls）", async () => {
    const { pollTaskUntilTerminalImpl } = await import("../pages/SchemeWorkbenchPage");
    let calls = 0;
    const fetchStatus = vi.fn(async () => {
      calls += 1;
      return { status: "degraded", progress: 1, message: "降级完成" } as any;
    });
    const res = await pollTaskUntilTerminalImpl(
      "task-1", fetchStatus, undefined, { intervalMs: 0 });
    expect(res?.status).toBe("degraded");
    expect(calls).toBe(1);
  });

  it("B2: 回归护栏 —— running + progress 满仍走 stuck 兜底为 completed", async () => {
    const { pollTaskUntilTerminalImpl } = await import("../pages/SchemeWorkbenchPage");
    const fetchStatus = vi.fn(async () => ({ status: "running", progress: 1 } as any));
    const res = await pollTaskUntilTerminalImpl(
      "task-1", fetchStatus, undefined, { intervalMs: 0, stuckThreshold: 2 });
    expect(res?.status).toBe("completed");
  });
});

// ---------------------------------------------------------------------------
// C 组 · hook 级 SSE 交互（BUG-C1 / BUG-C3）
// ---------------------------------------------------------------------------

function makeHookOpts(stream: AsyncIterable<any>, over: Record<string, unknown> = {}) {
  return {
    schemeId: "s1",
    concurrencyOption: "balanced",
    resolveWordBudgetOverride: () => undefined,
    autoConsistencyRepair: true,
    consistencySeverity: "high",
    autoShrinkOver: false,
    forceFullRepair: false,
    generationStandard: "inherit" as any,
    load: vi.fn(),
    getActiveTaskId: () => "t1",
    setActiveTab: vi.fn(),
    msg: {
      success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn(),
    },
    sseFetch: (async function* (_url?: string, _body?: unknown) {
      yield* stream;
    }) as any,
    ...over,
  } as any;
}

async function* once(...items: any[]) {
  for (const i of items) yield i;
}

/** 渲染 hook 并**真正触发一次生成**（renderHook 只挂载，不调用 generate）。 */
async function runGenerate(opts: any) {
  const { useContentGeneration } = await import("../hooks/useContentGeneration");
  const { result } = renderHook(() => useContentGeneration(opts));
  await act(async () => {
    await (result.current.generate as any)();
  });
  return result;
}

describe("useContentGeneration · 终态事件接线", () => {
  it("C1: degraded 终态必须收口——刷新正文 + 发出降级告警（BUG-C1）", async () => {
    const opts = makeHookOpts(once(
      { task_id: "t1", event: "progress", progress: 0.5, message: "已完成 1/2" },
      {
        task_id: "t1", event: "degraded", progress: 1, word_count: 8888,
        failed_count: 0, consistency_failed: 3,
        message: "正文8888字完成（一致性修复 3 处失败）",
        consistency_summary: { total: 5, repaired: 2, failed: 3 },
      },
    ));
    const result = await runGenerate(opts);
    expect(opts.load).toHaveBeenCalledTimes(1);
    expect(opts.msg.warning).toHaveBeenCalledTimes(1);
    expect(String(opts.msg.warning.mock.calls[0][0])).toContain("一致性修复 3 处失败");
    // 降级完成绝不能报 success
    expect(opts.msg.success).not.toHaveBeenCalled();
    expect(result.current.progress).toBe(1);
    expect(result.current.progressMsg).toContain("降级完成");
  });

  it("C2: completed 终态保持原语义（success + 刷新），不得被修复破坏", async () => {
    const opts = makeHookOpts(once(
      { task_id: "t1", event: "completed", progress: 1, word_count: 100, failed_count: 0 },
    ));
    await runGenerate(opts);
    expect(opts.load).toHaveBeenCalledTimes(1);
    expect(opts.msg.success).toHaveBeenCalledTimes(1);
    expect(opts.msg.warning).not.toHaveBeenCalled();
  });

  it("C3: 章节失败口径不被修复打乱（failed_count>0 → warning「N 章失败」）", async () => {
    const opts = makeHookOpts(once(
      { task_id: "t1", event: "completed", progress: 1, failed_count: 2, message: "done" },
    ));
    await runGenerate(opts);
    const warned = String(opts.msg.warning.mock.calls[0][0]);
    expect(warned).toContain("2 章失败");
    expect(opts.msg.success).not.toHaveBeenCalled();
  });

  it("C4: 断线重挂轮询遇到 status=degraded 必须返回并刷新（BUG-C3）", async () => {
    apiStatusMock.mockResolvedValue({
      data: { status: "degraded", progress: 1, content_result: { done: 2, total: 2, words: 100 } },
    });
    // ⚠️ 必须**先给出一个带 task_id 的事件**再抛 —— 否则 catch 分支里
    //    taskId 仍是空串，`if (taskId)` 不成立，根本不会进入重挂轮询
    //    （这正是真实断连的常见形态：连接已注册但正文事件一条没到）。
    const opts = makeHookOpts((async function* () {
      yield { task_id: "t1", event: "progress", progress: 0.1, message: "连接中" };
      throw Object.assign(new Error("SSE 中断"), { name: "NetworkError" });
    })());
    const result = await runGenerate(opts);
    await waitFor(() => expect(opts.load).toHaveBeenCalled(), { timeout: 5000 });
    expect(opts.msg.warning.mock.calls.some(
      (c: unknown[]) => String(c[0]).includes("降级完成"))).toBe(true);
    expect(result.current.generating).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// D 组 · A-B 反向锚：证明上面的断言真的能抓住「退回旧实现」
// ---------------------------------------------------------------------------

describe("A/B 反向锚", () => {
  it("D1: 若终态集合退回三态（去掉 degraded），B1 必然失败", () => {
    // 复刻修法前的旧白名单写法
    const legacy = (s: string) => ["completed", "failed", "stopped"].includes(s);
    expect(legacy("degraded")).toBe(false);
    expect(isTerminalTaskStatus("degraded")).toBe(true);
  });

  it("D2: 若 SSE 终态改回只认 completed，degraded 事件会被漏掉", () => {
    const legacyMatch = (evt: any) => evt?.event === "completed";
    const ev = { event: "degraded" };
    expect(legacyMatch(ev)).toBe(false);
    expect(isTerminalContentEvent(ev)).toBe(true);
  });
});
