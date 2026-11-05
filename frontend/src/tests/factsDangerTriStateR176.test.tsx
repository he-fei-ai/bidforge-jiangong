// @vitest-environment jsdom
/**
 * 危大诊断三态 · BUG-08（R176 · 2026-11-04）前端护栏。
 *
 * 缺陷形态：诊断面板把「danger-check 请求失败 / 分类为空」与「未识别为危大
 * 工程」塌缩成同一个布尔分支 —— 请求失败时输出**否定结论**，构成危大工程
 * 假阴性（用户据此认为无需按危大管控）。
 *
 * 本组用例锁三件事：
 *   A. 报告缺席（null）→ 必须「无法判定」，绝不得出现「未识别为危大」；
 *   B. 报告在场但 `is_hazardous` 非布尔（classification 为空/契约漂移）→ 同上；
 *   C. 后端确实给出布尔 → 才允许输出肯定/否定结论（三态不塌缩）。
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import {
  FactsDiagnosticsPanel,
  dangerTriState,
  oversizeTriState,
} from "../pages/schemeWorkbench/FactsDiagnosticsPanel";

afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Progress）会用到
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
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

const CHAPTERS = {
  chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5, missing_fields: [] }],
};

describe("BUG-08 · 危大判定三态", () => {
  it("纯函数：报告缺席 / 字段非布尔 → unknown；布尔才给结论", () => {
    expect(dangerTriState(null)).toBe("unknown");
    expect(dangerTriState(undefined)).toBe("unknown");
    expect(dangerTriState({})).toBe("unknown");
    expect(dangerTriState({ classification: {} })).toBe("unknown");
    expect(dangerTriState({ classification: { is_hazardous: undefined } })).toBe("unknown");
    expect(dangerTriState({ classification: { is_hazardous: true } })).toBe("hazardous");
    expect(dangerTriState({ classification: { is_hazardous: false } })).toBe("safe");
    expect(oversizeTriState({ classification: { is_hazardous: true } })).toBe("unknown");
  });

  it("核心场景：danger-check 失败（报告 null）、chapters 成功 → 显示无法判定", () => {
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={CHAPTERS}
        dangerReport={null}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("无法判定（危大分类数据不可用）")).toBeTruthy();
    expect(getByText("无法判定（超规模）")).toBeTruthy();
    // 假阴性防线：失败时绝不能出现肯定式否定结论
    expect(queryByText("未识别为危大工程")).toBeNull();
    expect(queryByText("未超过一定规模")).toBeNull();
  });

  it("分类为空（classification 无 is_hazardous）同样不得输出否定结论", () => {
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={CHAPTERS}
        dangerReport={{ classification: {} }}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("无法判定（危大分类数据不可用）")).toBeTruthy();
    expect(queryByText("未识别为危大工程")).toBeNull();
  });

  it("后端给出 false 时才允许显示「未识别为危大工程」", () => {
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={CHAPTERS}
        dangerReport={{ classification: { is_hazardous: false, is_oversize: false } }}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("未识别为危大工程")).toBeTruthy();
    expect(getByText("未超过一定规模")).toBeTruthy();
    expect(queryByText("无法判定（危大分类数据不可用）")).toBeNull();
  });

  it("真危大：肯定结论照常显示，三态互斥", () => {
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={CHAPTERS}
        dangerReport={{
          classification: { is_hazardous: true, is_oversize: false },
          missing_params: ["depth"],
        }}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("危大工程")).toBeTruthy();
    expect(getByText("未超过一定规模")).toBeTruthy();
    expect(getByText("缺参：depth")).toBeTruthy();
    expect(queryByText("无法判定（危大分类数据不可用）")).toBeNull();
    expect(queryByText("无法判定（超规模）")).toBeNull();
  });

  it("双侧皆无数据 → 中性空态（不输出任何结论）", () => {
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={null}
        dangerReport={null}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("暂无诊断数据")).toBeTruthy();
    expect(queryByText("未识别为危大工程")).toBeNull();
    expect(queryByText("危大工程")).toBeNull();
  });
});
