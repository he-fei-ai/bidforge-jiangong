// @vitest-environment jsdom
/**
 * SectionLogList 组件级测试（R201 · G4 遗留缺口收口）。
 *
 * 覆盖章节日志行的图表产出闭环展示：
 * 1. 有删图 → Tag「配图 N（已删 M）」；
 * 2. 无删图 → Tag「配图 N」（不带已删）；
 * 3. 零图零删 → 不占位（最常见的正常章节不打扰用户）；
 * 4. 降级信号（type=register / reason=apply_failed）→ 仍走同一 Tag 通道
 *    （文案枚举由 contentEvents.chartDropTexts 负责，此处锁「有删图即提示」）。
 */
import { describe, it, expect, beforeAll } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { SectionLogList } from "../pages/schemeWorkbench/SectionLogList";
import type { SectionLogItem } from "../utils/contentEvents";

// ✅ antd 栅格（responsiveObserver）在 jsdom 下需要 matchMedia 桩
beforeAll(() => {
  if (typeof window.matchMedia !== "function") {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }
});

const base = (over: Partial<SectionLogItem> = {}): SectionLogItem => ({
  section_id: "s1",
  title: "第一章 施工部署",
  status: "success",
  word_count: 800,
  word_budget: 900,
  word_status: "normal",
  time: Date.now(),
  ...over,
});

describe("SectionLogList 图表产出闭环（R201 G4）", () => {
  it("有删图时展示「配图 N（已删 M）」", () => {
    const { container } = render(
      <SectionLogList
        items={[
          base({
            chart_yield: {
              chart_count: 2,
              charts_dropped_count: 1,
              charts_dropped: [{ type: "flowchart", reason: "validation_failed" }],
            },
          }),
        ]}
      />,
    );
    const tags = Array.from(container.querySelectorAll(".ant-tag")).map((el) => el.textContent || "");
    expect(tags.some((t) => t.includes("配图 2（已删 1）"))).toBe(true);
  });

  it("无删图时展示「配图 N」且不出现「已删」", () => {
    const { container } = render(
      <SectionLogList
        items={[
          base({
            chart_yield: { chart_count: 3, charts_dropped_count: 0, charts_dropped: [] },
          }),
        ]}
      />,
    );
    const tags = Array.from(container.querySelectorAll(".ant-tag")).map((el) => el.textContent || "");
    expect(tags.some((t) => t.includes("配图 3"))).toBe(true);
    expect(tags.some((t) => t.includes("已删"))).toBe(false);
  });

  it("零图零删时完全不占位（普通章节不打扰用户）", () => {
    const { container } = render(
      <SectionLogList
        items={[
          base({
            chart_yield: { chart_count: 0, charts_dropped_count: 0, charts_dropped: [] },
          }),
        ]}
      />,
    );
    const tags = Array.from(container.querySelectorAll(".ant-tag")).map((el) => el.textContent || "");
    expect(tags.some((t) => t.includes("配图"))).toBe(false);
  });

  it("登记降级信号（register/apply_failed）同样走「已删 M」通道", () => {
    const { container } = render(
      <SectionLogList
        items={[
          base({
            chart_yield: {
              chart_count: 0,
              charts_dropped_count: 1,
              charts_dropped: [{ type: "register", reason: "apply_failed" }],
            },
          }),
        ]}
      />,
    );
    const tags = Array.from(container.querySelectorAll(".ant-tag")).map((el) => el.textContent || "");
    expect(tags.some((t) => t.includes("配图 0（已删 1）"))).toBe(true);
  });
});
