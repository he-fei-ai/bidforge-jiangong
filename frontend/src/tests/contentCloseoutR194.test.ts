/// <reference types="vite/client" />
/**
 * 正文生成模块承接收口 · R194 前端护栏（2026-11-09）。
 *
 * 收口面（配套后端护栏 tests/test_content_closeout_r194_20261109.py）：
 *   FE-1  接管轮询韧性：单次网络失败不再静默弃管（5 次连续失败 → 未知态），
 *         循环耗尽（400×3s）同样按未知态收尾 —— generating 不再永久卡死（C-1）。
 *   FE-2  断线重挂 + 手动接管回填 activeTaskIdRef：重挂窗口内停止/暂停按钮
 *         恢复控制可达性（C-4）。
 *   FE-3  stopped 终态消费 standard_summary / fact_autofix_summary（C-5，
 *         后端 _sse_content_payloads 刻意下发、前端此前静默丢弃）。
 *   FE-4  批处理器头部顺序对齐注释：flushNow → stop（C-10，旧序 flushNow
 *         恒刷空 Map，「排空残留」从未发生）。
 *   FE-5  刷新接管终态 + 手动接管终态消费 content_result（C-9 三路口径统一）。
 *
 * 判据实现沿用本仓既有范式（importParseR191.test.ts）：
 * Vite import.meta.glob(..., { query: "?raw" }) 读源码文本，不引 node:fs。
 */
import { describe, it, expect } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"] || "";
const RECOVERY = RAW["pages/schemeWorkbench/useUnknownTaskRecovery.tsx"] || "";
const BATCHER = RAW["utils/sseBatcher.ts"] || "";

/** 截取「刷新接管」轮询循环源码段（以 R194 注释为锚；循环体约 6k 字符）。 */
function takeoverLoopSrc(): string {
  const i = PAGE.indexOf("R194（FIX-12 · C-1 接管轮询韧性）");
  expect(i).toBeGreaterThan(0);
  const j = PAGE.indexOf("检测到后台", i);
  return PAGE.slice(i, j > 0 ? j : i + 8000);
}

describe("R194 前端护栏 · 接管轮询韧性（FE-1 / C-1）", () => {
  it("单次失败不再 catch{return} 弃管：有连续失败计数与未知态收尾", () => {
    const src = takeoverLoopSrc();
    expect(src).toContain("pollFailStreak");
    expect(src).toContain("pollFailStreak >= 5");
    expect(src).toContain("markUnknownTask");
    // 旧形态必须消失：catch 内裸 return（无任何收尾）
    expect(src).not.toMatch(/catch \{\s*return;\s*\}/);
  });

  it("循环耗尽（400×3s）有收尾：resetTakeover + markUnknownTask", () => {
    const src = takeoverLoopSrc();
    const iExhaust = src.indexOf("接管轮询超限（400×3s）未获得任务终态");
    expect(iExhaust).toBeGreaterThan(0);
    const before = src.slice(0, iExhaust);
    expect(before).toContain("resetTakeover()");
  });
});

describe("R194 前端护栏 · activeTaskIdRef 回填（FE-2 / C-4）", () => {
  it("断线重挂路径回填 ref（仅 ref 为空时、退出只清自己写的值）", () => {
    expect(PAGE).toContain("R194（FIX-13 · C-4 控制按钮可达性）");
    expect(PAGE).toContain("if (!activeTaskIdRef.current) {");
    expect(PAGE).toContain("_reattachBackfilled");
    expect(PAGE).toContain("if (_reattachBackfilled && activeTaskIdRef.current === taskId)");
  });

  it("手动接管路径回填 ref：hook 依赖注入 + 回填 + 退出清理", () => {
    expect(RECOVERY).toContain("activeTaskIdRef: MutableRefObject<string>");
    expect(RECOVERY).toContain("activeTaskIdRef.current = reattachTaskId");
    expect(RECOVERY).toContain("didBackfill && activeTaskIdRef.current === reattachTaskId");
    // 页面调用点注入 ref
    expect(PAGE).toContain("activeTaskIdRef,");
  });
});

describe("R194 前端护栏 · stopped 摘要消费（FE-3 / C-5）", () => {
  it("stopped 分支消费 standard_summary / fact_autofix_summary", () => {
    const i = PAGE.indexOf("evt.event === \"stopped\"");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 2200);
    expect(seg).toContain("R194（FIX-14 · C-5 stopped 摘要消费）");
    expect(seg).toContain("standardSummaryHint(evt.standard_summary)");
    expect(seg).toContain("factAutofixHint(evt.fact_autofix_summary)");
  });
});

describe("R194 前端护栏 · 批处理器顺序（FE-4 / C-10）", () => {
  it("头部顺序为 flushNow → stop（与尾部 finally 一致）", () => {
    const i = PAGE.indexOf("R194（FIX-15 · C-10 顺序对齐）");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 300);
    const iFlush = seg.indexOf("sseBatcherRef.current?.flushNow()");
    const iStop = seg.indexOf("sseBatcherRef.current?.stop()");
    expect(iFlush).toBeGreaterThan(-1);
    expect(iStop).toBeGreaterThan(iFlush);
  });

  it("stop() 清空 pending 的既有语义不被改变（顺序修复而非语义修复）", () => {
    expect(BATCHER).toContain("pending.clear()");
  });
});

describe("R194 前端护栏 · content_result 接管消费（FE-5 / C-9）", () => {
  it("刷新接管终态消费 content_result（失败明细 + 进度摘要）", () => {
    const i = PAGE.indexOf("R194（FIX-16 · C-9 三路口径统一）");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 1200);
    expect(seg).toContain("contentResultFailedSections(st.content_result)");
    expect(seg).toContain("contentResultSummary(st.content_result)");
    expect(seg).toContain("mergeFailedSections(_ck.sections)");
  });

  it("手动接管终态经 onContentResult 回调消费（hook 不持有日志区状态）", () => {
    expect(RECOVERY).toContain("onContentResult?: (fin: TaskTerminalInfo) => void");
    expect(RECOVERY).toContain('ut.genType === "content"');
    expect(RECOVERY).toContain("onContentResult?.(fin)");
    // 页面回调与断线重挂路径同一对纯函数
    expect(PAGE).toContain("contentResultFailedSections(fin.content_result)");
    expect(PAGE).toContain("contentResultSummary(fin.content_result)");
  });
});
