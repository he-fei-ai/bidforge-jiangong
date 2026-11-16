// @vitest-environment jsdom
/**
 * R252 D-3 · 提示词占位符删除告警 · 组件级行为护栏（2026-11-16）。
 *
 * 背景：`facts_completeness_rules` 等参数化提示词含 `{label}` / `{missing_note}`
 * 占位符。用户在编辑器误删占位符后保存，后端 `.format` 因缺占位符 fail-soft
 * 回退出厂骨架，定制静默失效 —— 此前 UI 只显示「变量变化」红 Tag，未说明后果。
 *
 * 锁定：
 *   1. baseline 有 `{label}`、当前无 → Alert 出现占位符删除告警文案（含后果说明）；
 *   2. 仅新增变量（baseline 空、当前有 `{x}`）→ 不出现删除告警（仍为 info 提示）；
 *   3. 无变化 → 不渲染变量 diff Alert。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { PromptVariableAnalysis } from "../pages/PromptEditorPage";
import { computePromptVariablesFallback } from "../utils/perf/computeWorker";

// jsdom 下 Worker 不真跑（组件 useEffect 走 computePromptVariables 异步回包，
// 早于其 3s 超时降级会被默认 1s waitFor 判负）。这里把异步入口直连主线程
// 纯函数，行为与生产降级路径一致，测试不再依赖 Worker 时序。
vi.mock("../utils/perf/computeWorker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/perf/computeWorker")>();
  return {
    ...actual,
    computePromptVariables: (req: Parameters<typeof computePromptVariablesFallback>[0]) =>
      Promise.resolve(computePromptVariablesFallback(req)),
  };
});

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

describe("R252 D-3 · 占位符删除告警", () => {
  it("删除占位符时出现后果告警文案", async () => {
    render(
      <PromptVariableAnalysis
        content={"校验规则：字段完整性必须覆盖全部章节。"}
        baseline={"校验规则：{label} 覆盖 {missing_note}。"}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText(/回退到出厂骨架/)).toBeTruthy();
    });
    // 红 Tag 仍在（既有形态保留）
    expect(screen.getByText("-label")).toBeTruthy();
    expect(screen.getByText("-missing_note")).toBeTruthy();
  });

  it("仅新增变量时不出现删除告警", async () => {
    // 变量名须 ≥2 字符（与后端 _VARIABLE_PATTERN / extractPromptVariables 对齐）
    render(<PromptVariableAnalysis content={"规则：{xx} 必填。"} baseline={"规则：必填。"} />);
    await waitFor(() => {
      expect(screen.getByText("+xx")).toBeTruthy();
    });
    expect(screen.queryByText(/回退到出厂骨架/)).toBeNull();
  });

  it("无变量变化时不渲染 diff Alert", async () => {
    render(<PromptVariableAnalysis content={"规则：{xx}。"} baseline={"规则：{xx}。"} />);
    await waitFor(() => {
      expect(screen.queryByText("相对默认模板的变量变化：")).toBeNull();
    });
  });
});
