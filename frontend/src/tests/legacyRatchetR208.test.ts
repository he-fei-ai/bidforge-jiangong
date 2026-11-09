/**
 * R208 · 遗留项收口前端护栏（2026-11-09）。
 *
 * 行为级：断线重挂摘要 words / word_count 双键兼容；
 * 源码棘轮：本轮前后端契约收口的**使用点**必须留在源码（只锁定义会假绿，
 * 见 M11 教训）。全部断言使用代码专属短语，避免被注释文本命中。
 */
import { describe, it, expect } from "vitest";
import { contentResultSummary } from "../utils/contentEvents";
import apiSrc from "../api/index.ts?raw";
import panelSrc from "../pages/schemeWorkbench/tabs/ScoringCoveragePanel.tsx?raw";
import bidHookSrc from "../pages/schemeWorkbench/useBidAnalysis.tsx?raw";
import bidTypesSrc from "../utils/bidAnalysis.ts?raw";
import logListSrc from "../pages/schemeWorkbench/SectionLogList.tsx?raw";
import contentEventsSrc from "../utils/contentEvents.ts?raw";
import wbTypesSrc from "../pages/schemeWorkbench/workbenchTypes.ts?raw";
import autoConfirmSrc from "../pages/schemeWorkbench/outlineAutoConfirm.ts?raw";
import promptPageSrc from "../pages/PromptEditorPage.tsx?raw";
import recoverySrc from "../pages/schemeWorkbench/useUnknownTaskRecovery.tsx?raw";
import factsHookSrc from "../pages/schemeWorkbench/useFactsGeneration.ts?raw";

describe("R208 · contentResultSummary 双键兼容", () => {
  it("words 存在时优先（在线/checkpoint 主口径不变）", () => {
    expect(contentResultSummary({ done: 3, total: 3, words: 1200, word_count: 9999 }).words).toBe(1200);
  });

  it("仅 word_count 时回退（旧 checkpoint / 降级路径不再显示 0 字）", () => {
    expect(contentResultSummary({ word_count: 321 }).words).toBe(321);
  });

  it("两键皆缺 / 非对象一律 0，不抛异常", () => {
    expect(contentResultSummary({}).words).toBe(0);
    expect(contentResultSummary({ words: "1200" }).words).toBe(0);
    expect(contentResultSummary(null).words).toBe(0);
  });
});

describe("R208 · 长任务取消与 SSE 建连 race", () => {
  it("评分项抽取 API 透传 signal", () => {
    expect(apiSrc).toContain("signal?: AbortSignal,");
    expect(apiSrc).toContain("{ timeout: 300000, signal }");
  });

  it("sseGetStream 内层 abort 一律让 race 决出", () => {
    expect(apiSrc).toContain('err.name = options?.signal?.aborted ? "AbortError" : "TimeoutError";');
    expect(apiSrc).not.toContain("if (!options?.signal?.aborted) {\n              reject(new Error(timeoutMsg));");
  });

  it("抽取面板有独立世代 + 卸载清理 + finally 门禁", () => {
    expect(panelSrc).toContain("const seq = ++extractSeqRef.current;");
    expect(panelSrc).toContain("scoringApi.extract(projectId, undefined, ac.signal)");
    expect(panelSrc).toContain("extractSeqRef.current += 1;");
    expect(panelSrc).toContain("if (seq === extractSeqRef.current) setExtracting(false);");
  });
});

describe("R208 · 零消费字段接线", () => {
  it("多标段未选标段：类型声明 + 使用点告警", () => {
    expect(bidTypesSrc).toContain("multi_section_unselected?: boolean;");
    expect(bidHookSrc).toContain("if (evt.multi_section_unselected) {");
    expect(bidHookSrc).toContain("未选择标段");
  });

  it("字数偏差三键：类型、事件、日志项、渲染四点俱全", () => {
    expect(contentEventsSrc).toContain("word_within_band?: boolean;");
    expect(wbTypesSrc).toContain("word_deviation?: number;");
    expect(logListSrc).toContain("Math.round(item.word_ratio * 100)");
    expect(logListSrc).toContain("容差外");
    expect(logListSrc).toContain("容差内");
  });

  it("自动确认判定异常 blocker 有中文标签", () => {
    expect(autoConfirmSrc).toContain('evaluate_failed: "自动确认判定异常"');
  });
});

describe("R208 · 错误提示与死分支清理", () => {
  it("PromptEditorPage 两处 catch 消费服务端 detail", () => {
    expect(promptPageSrc).toContain('promptErrorText(e, "加载提示词失败")');
    expect(promptPageSrc).toContain('promptErrorText(e, "同步后刷新列表失败")');
    expect(promptPageSrc).not.toContain(".catch(() => {});");
  });

  it("未知任务恢复错误提示不再吞 error.message", () => {
    expect(recoverySrc).toContain("err?.message ||");
  });

  it("事实链永不触发的 connecting 死分支已删除", () => {
    expect(factsHookSrc).not.toContain('evt.event === "progress" || evt.event === "connecting"');
  });
});
