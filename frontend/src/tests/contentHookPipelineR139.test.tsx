/// <reference types="vite/client" />
// @vitest-environment jsdom
/**
 * R139（2026-10-28）· 正文生成 hook 链路缺陷回归护栏。
 *
 * 审计背景：正文生成前端存在**两条 SSE 消费链路** —— 页面内联版
 * （SchemeWorkbenchPage.tsx 的 handleGenerateContent）与 hook 版
 * （hooks/useContentGeneration.ts）。页面是线上主路径，hook 目前只被测试引用，
 * 但它一旦被启用就会带着三处缺陷上线：
 *
 *   FIX-1 断线重挂是**无上限**递归 setTimeout（页面版 pollTaskUntilTerminal 有
 *         maxPolls=200 兜底，hook 版完全没有）⇒ 后台任务永不终态时 await 永不
 *         返回 ⇒ setGenerating(false) 永不执行，界面永久「生成中」且不可取消。
 *   FIX-2 handleSectionDone 未装配 chart_yield（类型里定义了字段、后端一直在
 *         下发三键）⇒ 日志区「配图 N 张（已删 M 张）」在这条链路永久丢失
 *         （与 quality_issues 同族的静默断链）。
 *   FIX-3 未下发 regenerate_requirement（页面版 SchemeWorkbenchPage.tsx:6341 有）
 *         ⇒ 用户「重新生成时填写的额外要求」被静默丢弃。
 *
 * 本文件按 A（断线重连收敛）/ B（图表闭环字段）/ C（重新生成要求）三组验证，
 * 并含页面↔hook 的**字段级对称锚**（此前 parity 测试只比事件集合，不比字段）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";
import { useContentGeneration } from "../hooks/useContentGeneration";
import { normalizeChartYield } from "../utils/contentEvents";
import { pollTaskUntilTerminalImpl } from "../utils/taskPolling";

afterEach(cleanup);

// ---------------------------------------------------------------------------
// 源码读取（与 contentTerminalParity 同机制：Vite ?raw，不依赖 node:fs）
// ---------------------------------------------------------------------------
const RAW_SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.startsWith("../") ? k.slice(3) : k, v]),
);
const HOOK = RAW_SOURCES["hooks/useContentGeneration.ts"] || "";
const PAGE = RAW_SOURCES["pages/SchemeWorkbenchPage.tsx"] || "";

// ---------------------------------------------------------------------------
// 依赖桩
// ---------------------------------------------------------------------------
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

async function* once(...items: any[]) {
  for (const i of items) yield i;
}

function makeOpts(stream: AsyncIterable<any>, over: Record<string, unknown> = {}) {
  return {
    schemeId: "s1",
    concurrencyOption: "balanced",
    resolveWordBudgetOverride: () => undefined,
    autoConsistencyRepair: true,
    consistencySeverity: "high",
    autoShrinkOver: false,
    forceFullRepair: false,
    generationStandard: "inherit" as any,
    load: vi.fn().mockResolvedValue(undefined),
    getActiveTaskId: () => "t1",
    setActiveTab: vi.fn(),
    msg: {
      success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn(),
    },
    sseFetch: (async function* () {
      yield* stream;
    }) as any,
    ...over,
  } as any;
}

// ---------------------------------------------------------------------------
// A 组 · 断线重连必须有界（FIX-1）
// ---------------------------------------------------------------------------
describe("A 组 · 断线重连收敛到单一出口", () => {
  it("A1: 前提校验 —— 两个源码都真的取到了（为空会让源码锚常绿）", () => {
    expect(HOOK.length).toBeGreaterThan(500);
    expect(PAGE.length).toBeGreaterThan(10000);
    expect(HOOK).toContain("/sse/generate-content/");
  });

  it("A2: hook 复用 pollTaskUntilTerminalImpl，且旧的无上限递归形态已消失（FIX-1）", () => {
    // 断言**调用形态**（`await pollTaskUntilTerminalImpl(`）而不仅是符号出现：
    // 只 import 不调用时旧递归仍可存活，那样的护栏等于没锁。
    expect(HOOK).toContain("await pollTaskUntilTerminalImpl(");
    // 旧实现的确切形态：new Promise(() => { ... setTimeout(poll, 3000) ... })
    expect(HOOK).not.toContain("setTimeout(poll, 3000)");
    // 页面与 hook 共用同一判据（单一事实源，不得各写一份）
    expect(PAGE).toContain("pollTaskUntilTerminalImpl");
  });

  it("A3: 复用的出口有 maxPolls 硬上限 —— 恒 running 的任务必然有界返回 null", async () => {
    vi.useFakeTimers();
    try {
      const fetchStatus = vi.fn(async () => ({ status: "running" } as any));
      const p = pollTaskUntilTerminalImpl("t1", fetchStatus, undefined, {
        intervalMs: 3000, maxPolls: 5,
      });
      await vi.advanceTimersByTimeAsync(3000 * 6);
      const res = await p;
      expect(res).toBeNull();
      expect(fetchStatus).toHaveBeenCalledTimes(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("A4: 404 立即放弃（不耗满 maxPolls），与页面版同语义", async () => {
    vi.useFakeTimers();
    try {
      const fetchStatus = vi.fn(async () => {
        const e: any = new Error("not found");
        e.response = { status: 404 };
        throw e;
      });
      const p = pollTaskUntilTerminalImpl("t1", fetchStatus, undefined, {
        intervalMs: 3000, maxPolls: 50,
      });
      await vi.advanceTimersByTimeAsync(3000 * 2);
      expect(await p).toBeNull();
      expect(fetchStatus).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// B 组 · 图表产出闭环字段（FIX-2）
// ---------------------------------------------------------------------------
describe("B 组 · section_done 的图表闭环字段", () => {
  it("B1: hook 与页面都装配 chart_yield（字段级对称，不再只比事件集合）", () => {
    expect(HOOK).toContain("chart_yield:");
    expect(HOOK).toContain("normalizeChartYield(");
    expect(PAGE).toContain("chart_yield: normalizeChartYield({");
  });

  it("B2: 真实事件 → 日志项 chart_yield 与归一出口深相等（FIX-2 行为实证）", async () => {
    const dropped = [{ type: "mermaid", reason: "per_section_limit" }];
    const opts = makeOpts(once(
      { task_id: "t1", event: "section_start", section_id: "s1", title: "章一", index: 1, total: 1 },
      {
        task_id: "t1", event: "section_done", section_id: "s1", title: "章一",
        word_count: 1000, word_budget: 1000, word_status: "normal",
        chart_count: 2, charts_dropped_count: 1, charts_dropped: dropped,
      },
      { task_id: "t1", event: "completed", word_count: 1000, failed_count: 0 },
    ));
    const { result } = renderHook(() => useContentGeneration(opts));
    await act(async () => {
      await result.current.generate();
    });
    const item = result.current.sectionLogs.find((x) => x.section_id === "s1");
    expect(item?.status).toBe("success");
    expect(item?.chart_yield).toEqual(normalizeChartYield({
      chart_count: 2, charts_dropped_count: 1, charts_dropped: dropped,
    }));
    expect(item?.chart_yield?.charts_dropped_count).toBe(1);
  });

  it("B3: 后端未下发三键时归一为 0/[]，绝不 undefined（旧版后端兼容）", async () => {
    const opts = makeOpts(once(
      { task_id: "t1", event: "section_start", section_id: "s1", title: "章一", index: 1, total: 1 },
      { task_id: "t1", event: "section_done", section_id: "s1", title: "章一", word_count: 10 },
      { task_id: "t1", event: "completed", word_count: 10, failed_count: 0 },
    ));
    const { result } = renderHook(() => useContentGeneration(opts));
    await act(async () => {
      await result.current.generate();
    });
    const item = result.current.sectionLogs.find((x) => x.section_id === "s1");
    expect(item?.chart_yield).toEqual({
      chart_count: 0, charts_dropped_count: 0, charts_dropped: [],
    });
  });
});

// ---------------------------------------------------------------------------
// C 组 · 重新生成要求（FIX-3）
// ---------------------------------------------------------------------------
describe("C 组 · regenerate_requirement 下发", () => {
  /** 捕获请求体的 sseFetch：只回一个终态事件，让 generate 尽快收口。 */
  function captureBody(captured: any[]) {
    return (async function* (_url: string, body: any) {
      captured.push(body);
      yield { task_id: "t1", event: "completed", word_count: 0, failed_count: 0 };
    }) as any;
  }

  it("C1: 用户填写的要求随请求体下发（去首尾空白），与页面同口径（FIX-3）", async () => {
    const captured: any[] = [];
    const opts = makeOpts(once(), { sseFetch: captureBody(captured) });
    const { result } = renderHook(() => useContentGeneration(opts));
    await act(async () => {
      await result.current.generate({ regenerate_requirement: "  增加表格与流程图  " });
    });
    expect(captured.length).toBe(1);
    expect(captured[0].regenerate_requirement).toBe("增加表格与流程图");
    // 页面内联版同一判据（.trim() 后下发），防两链路再次漂移
    expect(PAGE).toContain("body.regenerate_requirement = extra.regenerate_requirement.trim()");
  });

  it("C2: 空 / 纯空白不下发（缺省请求体与引入本项前逐字节一致）", async () => {
    const captured: any[] = [];
    const opts = makeOpts(once(), { sseFetch: captureBody(captured) });
    const { result } = renderHook(() => useContentGeneration(opts));
    await act(async () => {
      await result.current.generate({ regenerate_requirement: "   " });
    });
    expect(captured[0]).not.toHaveProperty("regenerate_requirement");
  });
});
