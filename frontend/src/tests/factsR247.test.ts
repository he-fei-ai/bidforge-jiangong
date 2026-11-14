/**
 * R247（2026-11-14）全局事实模块收口 · 前端护栏
 *
 * FE-1（P2）刷新接管终态收窄 + 世代复核：
 *   旧实现 `if (active.task_type === "facts_generation") { factsTerminalFnRef.current?.(st); }`
 *   把**全部终态**喂给 consumeFactsTerminal —— failed/stopped 被当降级完成
 *   （consumeFactsTerminal 内 _isDegraded = status !== "completed" 恒真）：
 *     ① 双 toast（通用 failed/stopped 分支已提示，此处再 warning 一次）；
 *     ② applyFactsCompletedEvent({}) 用空对象覆盖 segmentStats / crossConflicts，
 *        把分段失败横幅清空 ⇒ 用户看不到哪几段提取失败。
 *   现与手动接管路径（useUnknownTaskRecovery 已按 completed|degraded 收窄）同口径，
 *   并补 cancelled / genSeq 复核（与 catch 分支同纪律）。
 *
 * A 组 源码棘轮（?raw）：四条件收窄必须出现在 consumeFactsTerminal 调用之前的窗口内。
 * B 组 对照成对：手动接管路径的 completed|degraded 收窄仍在（口径成对，防单边回退）。
 * C 组 自证：锚点真存在（防 vacuous pass）。
 */
import { describe, expect, it } from "vitest";

import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import recoverySrc from "../pages/schemeWorkbench/useUnknownTaskRecovery.tsx?raw";

// FE-1 刷新接管终态消费点：调用必须独占一行（子串稳定）
const CALL = "factsTerminalFnRef.current?.(st);";

/** 取「刷新接管终态」判定窗口：从 R247 收窄注释块到 consumeFactsTerminal 调用。 */
function takeoverWindow(): string {
  const callIdx = pageSrc.indexOf(CALL);
  expect(callIdx).toBeGreaterThan(0);
  // 窗口起点：本次改动的收窄注释块（唯一锚，紧邻四条件判定之前）
  const anchor = "R247（2026-11-14 · P2 · 刷新接管终态收窄";
  expect(pageSrc.split(anchor).length - 1).toBe(1);
  const start = pageSrc.lastIndexOf(anchor, callIdx);
  expect(start).toBeGreaterThan(0);
  // 终点含调用行本身（slice 终点互斥，需越过 CALL 长度）
  return pageSrc.slice(start, callIdx + CALL.length);
}

describe("FE-1 刷新接管终态收窄 + 世代复核（R247）", () => {
  it("仅 completed/degraded 才消费 facts 成果（failed/stopped 落通用分支）", () => {
    const win = takeoverWindow();
    expect(win).toContain('(st.status === "completed" || st.status === "degraded")');
  });

  it("终态消费前复核 cancelled（切方案 effect cleanup 置位后不得写入旧方案态）", () => {
    const win = takeoverWindow();
    expect(win).toContain("!cancelled");
  });

  it("终态消费前复核 genSeq 世代（旧方案 segmentStats 不得写入新页面态）", () => {
    const win = takeoverWindow();
    expect(win).toContain("genSeqRef.current === takeoverSeq");
  });

  it("三道闸门均在 consumeFactsTerminal 调用之前（顺序契约）", () => {
    const win = takeoverWindow();
    const callIdx = win.indexOf(CALL);
    expect(win.indexOf('(st.status === "completed" || st.status === "degraded")')).toBeLessThan(callIdx);
    expect(win.indexOf("!cancelled")).toBeLessThan(callIdx);
    expect(win.indexOf("genSeqRef.current === takeoverSeq")).toBeLessThan(callIdx);
  });
});

describe("B 组 · 与手动接管路径口径成对（R224 F17）", () => {
  it("useUnknownTaskRecovery 的 facts 终态仍按 completed|degraded 收窄", () => {
    const i = recoverySrc.indexOf('ut.genType === "facts"');
    expect(i).toBeGreaterThan(0);
    const line = recoverySrc.slice(i, recoverySrc.indexOf("\n", i));
    expect(line).toContain('(fin.status === "completed" || fin.status === "degraded")');
  });
});

describe("C 组 · 自证（防 vacuous pass）", () => {
  it("被锁的调用点与两份源码均真存在", () => {
    expect(pageSrc).toContain(CALL);
    expect(recoverySrc).toContain('ut.genType === "facts"');
  });
});
