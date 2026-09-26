// @vitest-environment jsdom
/**
 * ImportSubTabBar 组件级交互测试（import 顶层 Tab 的 docs/extract 子标签栏）。
 *
 * ✅ 2026-09-24 T2：该区域此前内联在 9000+ 行的 SchemeWorkbenchPage 中，零测试。
 *    覆盖：受控激活态、点击回调、提取徽标（无 summary 不显；有 summary 显
 *    success/total，必选项全完成绿色否则蓝色）。
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import ImportSubTabBar from "../components/ImportSubTabBar";
import type { BaSummary } from "../utils/bidAnalysis";

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

function setup(props: Partial<React.ComponentProps<typeof ImportSubTabBar>> = {}) {
  const onChange = vi.fn();
  const utils = render(
    <ImportSubTabBar
      activeKey={props.activeKey ?? "docs"}
      onChange={props.onChange ?? onChange}
      summary={props.summary}
    />
  );
  return { ...utils, onChange };
}

const summary = (over: Partial<BaSummary>): BaSummary => ({
  total: 18,
  success: 3,
  success_valid: 3,
  errors: 0,
  running: 0,
  pending: 15,
  missing_required: [],
  all_required_done: false,
  ...over,
} as BaSummary);

describe("ImportSubTabBar", () => {
  it("渲染两个子标签：文档解析 / 项目提取", () => {
    const { container } = setup();
    const tabs = Array.from(container.querySelectorAll(".ant-tabs-tab"));
    expect(tabs).toHaveLength(2);
    expect(tabs[0].textContent).toContain("文档解析");
    expect(tabs[1].textContent).toContain("项目提取");
  });

  it("受控激活态：activeKey=docs/extract 对应标签带 active class", () => {
    const docs = setup({ activeKey: "docs" });
    const tabsDocs = docs.container.querySelectorAll(".ant-tabs-tab");
    expect(tabsDocs[0].className).toContain("ant-tabs-tab-active");
    expect(tabsDocs[1].className).not.toContain("ant-tabs-tab-active");

    const extract = setup({ activeKey: "extract" });
    const tabsEx = extract.container.querySelectorAll(".ant-tabs-tab");
    expect(tabsEx[1].className).toContain("ant-tabs-tab-active");
    expect(tabsEx[0].className).not.toContain("ant-tabs-tab-active");
  });

  it("点击标签回调 onChange：docs ↔ extract", () => {
    // 受控组件：用 React state 回写 activeKey（antd 点击当前已激活标签不触发 onChange）
    const onChange = vi.fn();
    function Harness() {
      const [k, setK] = React.useState<"docs" | "extract">("docs");
      return (
        <ImportSubTabBar
          activeKey={k}
          onChange={(next) => { onChange(next); setK(next); }}
        />
      );
    }
    const { container } = render(<Harness />);
    const getTabs = () => container.querySelectorAll(".ant-tabs-tab");
    fireEvent.click(getTabs()[1]);
    expect(onChange).toHaveBeenLastCalledWith("extract");
    fireEvent.click(getTabs()[0]);
    expect(onChange).toHaveBeenLastCalledWith("docs");
  });

  it("无 summary：不显徽标（标签文本不含 n/total）", () => {
    const { container } = setup({ summary: null });
    expect(container.textContent).not.toContain("/");
    expect(container.querySelectorAll(".ant-tag")).toHaveLength(0);
  });

  it("有 summary：显示 success/total 徽标；必选项未齐 → 蓝色", () => {
    const { container } = setup({
      summary: summary({ success: 3, all_required_done: false, missing_required: ["建设规模"] }),
    });
    const tag = container.querySelector(".ant-tag");
    expect(tag).not.toBeNull();
    expect(tag!.textContent).toBe("3/18");
    expect(tag!.className).toContain("ant-tag-blue");
  });

  it("必选项全完成：徽标变绿", () => {
    const { container } = setup({
      summary: summary({ success: 18, success_valid: 18, all_required_done: true, pending: 0 }),
    });
    const tag = container.querySelector(".ant-tag");
    expect(tag!.textContent).toBe("18/18");
    expect(tag!.className).toContain("ant-tag-green");
  });
});
