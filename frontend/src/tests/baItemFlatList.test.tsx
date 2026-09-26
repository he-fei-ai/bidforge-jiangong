// @vitest-environment jsdom
/**
 * BaItemFlatList 组件级交互测试（import · extract 子 Tab 左面板 18 项清单）。
 *
 * ✅ 2026-09-24 B2 回归护栏：状态值域必须与后端 bid_analysis_items
 *    （idle/running/success/error）及 isMissingBaResult 口径严格对齐。
 *    旧内联实现的两类 BUG：
 *      - 失败态判 "failed"（后端写 "error"）→ 失败项永不显示红 ✗；
 *      - success 即绿 ✓，把「未提取到」/ json 全「没有提及」当成已完成。
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import React from "react";
import BaItemFlatList from "../components/BaItemFlatList";
import type { BaItemDef, BaStoredItem } from "../utils/bidAnalysis";

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

const def = (over: Partial<BaItemDef>): BaItemDef => ({
  item_id: "it1",
  label: "项目基本信息",
  required: 1,
  output_type: "markdown",
  ...over,
});

const stored = (over: Partial<BaStoredItem>): BaStoredItem => ({
  item_id: "it1",
  ...over,
});

function setup(props: Partial<React.ComponentProps<typeof BaItemFlatList>> = {}) {
  const onSelectItem = vi.fn();
  const defs = props.defs ?? [def({})];
  const utils = render(
    <BaItemFlatList
      defs={defs}
      items={props.items ?? []}
      loading={props.loading ?? false}
      defsError={props.defsError ?? null}
      selectedItemId={props.selectedItemId ?? null}
      onSelectItem={props.onSelectItem ?? onSelectItem}
    />
  );
  return { ...utils, onSelectItem, defs };
}

/** 按标签定位某一行（返回行容器 div） */
function rowByText(container: HTMLElement, label: string): HTMLElement {
  const rows = Array.from(container.querySelectorAll('[data-testid="ba-flat-item"]'));
  const el = rows.find(d => (d.textContent || "").includes(label));
  if (!el) throw new Error(`未找到包含「${label}」的清单行`);
  return el as HTMLElement;
}

describe("BaItemFlatList", () => {
  it("无记录（idle）：序号+标签+必选角标+灰色 · 状态标", () => {
    const { container } = setup();
    const row = rowByText(container, "项目基本信息");
    expect(row.textContent).toContain("1.");
    expect(row.textContent).toContain("必");
    // 默认 Tag（灰），不带任何颜色 class
    const stateTag = row.querySelectorAll(".ant-tag")[1];
    expect(stateTag.textContent).toBe("·");
    expect(stateTag.className).not.toContain("ant-tag-green");
    expect(stateTag.className).not.toContain("ant-tag-red");
  });

  it("running：蓝色 … 标签（进行中可见）", () => {
    const { container } = setup({
      items: [stored({ status: "running" })],
    });
    const tag = rowByText(container, "项目基本信息").querySelectorAll(".ant-tag")[1];
    expect(tag.textContent).toBe("…");
    expect(tag.className).toContain("ant-tag-blue");
  });

  it("success 且有有效内容：绿色 ✓，必选角标变绿", () => {
    const { container } = setup({
      items: [stored({ status: "success", content: "工程名称：某某产业园" })],
    });
    const row = rowByText(container, "项目基本信息");
    const tags = row.querySelectorAll(".ant-tag");
    // 必选角标（第 1 个）绿
    expect(tags[0].className).toContain("ant-tag-green");
    // 状态标（第 2 个）绿 ✓
    expect(tags[1].textContent).toBe("✓");
    expect(tags[1].className).toContain("ant-tag-green");
  });

  it("error：红色 ✗（修复 failed 值域 BUG 的核心回归）", () => {
    const { container } = setup({
      items: [stored({ status: "error", error: "模型超时" })],
    });
    const tag = rowByText(container, "项目基本信息").querySelectorAll(".ant-tag")[1];
    expect(tag.textContent).toBe("✗");
    expect(tag.className).toContain("ant-tag-red");
    // 必选项失败时角标保持橙色
    const row = rowByText(container, "项目基本信息");
    expect(row.querySelectorAll(".ant-tag")[0].className).toContain("ant-tag-orange");
  });

  it("success 但 markdown 内容为「未提取到」：橙色「空」而非绿色 ✓", () => {
    const { container } = setup({
      items: [stored({ status: "success", content: "  未提取到  " })],
    });
    const tag = rowByText(container, "项目基本信息").querySelectorAll(".ant-tag")[1];
    expect(tag.textContent).toBe("空");
    expect(tag.className).toContain("ant-tag-orange");
  });

  it("success 但 json 所有字段都是「没有提及」：橙色「空」", () => {
    const { container } = setup({
      defs: [def({ item_id: "pbi", output_type: "json" })],
      items: [stored({
        item_id: "pbi",
        status: "success",
        content: '{"project_name":"没有提及","project_number":"没有提及"}',
      })],
    });
    const tag = rowByText(container, "项目基本信息").querySelectorAll(".ant-tag")[1];
    expect(tag.textContent).toBe("空");
    expect(tag.className).toContain("ant-tag-orange");
  });

  it("success 且 json 至少一个字段有值：绿色 ✓（反例，防误判空）", () => {
    const { container } = setup({
      defs: [def({ item_id: "pbi2", output_type: "json" })],
      items: [stored({
        item_id: "pbi2",
        status: "success",
        content: '{"project_name":"某某大厦","project_number":"没有提及"}',
      })],
    });
    const tag = rowByText(container, "项目基本信息").querySelectorAll(".ant-tag")[1];
    expect(tag.textContent).toBe("✓");
    expect(tag.className).toContain("ant-tag-green");
  });

  it("点击行：onSelectItem 收到「定义 + 已存结果」合并对象", () => {
    const { onSelectItem, container } = setup({
      items: [stored({ status: "success", content: "工期 120 天", source: "ai" })],
    });
    fireEvent.click(rowByText(container, "项目基本信息"));
    expect(onSelectItem).toHaveBeenCalledTimes(1);
    const merged = onSelectItem.mock.calls[0][0];
    expect(merged.item_id).toBe("it1");
    expect(merged.label).toBe("项目基本信息");
    expect(merged.required).toBe(1);
    expect(merged.status).toBe("success");
    expect(merged.content).toBe("工期 120 天");
  });

  it("选中项高亮（蓝边 + 浅蓝底）；非选项保持默认", () => {
    const defs = [
      def({ item_id: "a", label: "第一项" }),
      def({ item_id: "b", label: "第二项", required: 0 }),
    ];
    const { container } = setup({ defs, selectedItemId: "b" });
    const selected = rowByText(container, "第二项");
    expect(selected.style.borderColor).toContain("22, 119, 255");
    expect(selected.style.background).toBe("rgb(230, 244, 255)");
    const other = rowByText(container, "第一项");
    expect(other.style.background).toBe("rgb(255, 255, 255)");
  });

  it("加载中（无定义）：显示 Spin；定义加载失败：显示 Alert；无定义：Empty", () => {
    const loading = setup({ loading: true, defs: [] });
    expect(loading.container.querySelector(".ant-spin")).not.toBeNull();

    const err = setup({ defs: [], defsError: "网络错误" });
    expect(err.container.textContent).toContain("解析项定义加载失败");
    expect(err.container.textContent).toContain("网络错误");

    const empty = setup({ defs: [] });
    expect(empty.container.textContent).toContain("暂无结构化提取项");
  });

  it("非必选项不渲染「必」角标", () => {
    const { container } = setup({
      defs: [def({ required: 0 })],
      items: [stored({ status: "success", content: "有内容" })],
    });
    const row = rowByText(container, "项目基本信息");
    expect(row.textContent).not.toContain("必");
    expect(row.querySelectorAll(".ant-tag")).toHaveLength(1);
  });
});
