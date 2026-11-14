// @vitest-environment jsdom
/**
 * BaItemFlatList 组件级交互测试（「项目提取」子 Tab 左面板扁平清单）。
 *
 * 覆盖：渲染序号与标签、加载/空/错误态、五种状态标签映射
 * （running 蓝… / success 有效 绿✓ / success 空 橙「空」/ error 红✗ / idle 灰·）、
 * 必填徽标、点击回调合并 def+stored、选中态样式。
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

const defs: BaItemDef[] = [
  { item_id: "project_name", label: "项目名称", required: 1, output_type: "markdown" },
  { item_id: "build_scale", label: "建设规模", required: 1, output_type: "markdown" },
  { item_id: "invest", label: "投资估算", required: 0, output_type: "json" },
];

const stored = (item_id: string, over: Partial<BaStoredItem> = {}): BaStoredItem => ({
  item_id,
  status: "idle",
  ...over,
});

function rows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll('[data-testid="ba-flat-item"]'));
}

/** 状态标（…✓空✗·）与必填角标（必）并存，取状态标须按文本定位而非固定索引 */
function stateTag(container: HTMLElement): HTMLElement {
  const candidates = Array.from(container.querySelectorAll(".ant-tag")) as HTMLElement[];
  const tag = candidates.find((t) => ["…", "✓", "空", "✗", "·"].includes(t.textContent || ""));
  if (!tag) throw new Error("未找到状态标签");
  return tag;
}

function setup(props: Partial<React.ComponentProps<typeof BaItemFlatList>> = {}) {
  const onSelectItem = vi.fn();
  const utils = render(
    <BaItemFlatList
      defs={props.defs ?? defs}
      items={props.items ?? []}
      loading={props.loading ?? false}
      defsError={props.defsError ?? ""}
      selectedItemId={props.selectedItemId ?? null}
      onSelectItem={props.onSelectItem ?? onSelectItem}
    />,
  );
  return { ...utils, onSelectItem };
}

describe("BaItemFlatList", () => {
  it("渲染全部定义项：序号从 1 递增 + 标签（label 优先，无 label 回退 item_id）", () => {
    const { container } = setup();
    const list = rows(container);
    expect(list).toHaveLength(defs.length);
    expect(list[0].textContent).toContain("1.");
    expect(list[0].textContent).toContain("项目名称");
    expect(list[2].textContent).toContain("3.");
    expect(list[2].textContent).toContain("投资估算");
    // 无 label 回退 item_id
    const fallback = setup({ defs: [{ item_id: "no_label_item" }] });
    expect(rows(fallback.container)[0].textContent).toContain("no_label_item");
  });

  it("loading 且无定义：显示 Spin（不遮罩已有定义）", () => {
    const spin = setup({ defs: [], loading: true });
    expect(spin.container.querySelector(".ant-spin")).not.toBeNull();
    const hasDefs = setup({ defs, loading: true });
    expect(hasDefs.container.querySelector(".ant-spin")).toBeNull();
    expect(rows(hasDefs.container)).toHaveLength(defs.length);
  });

  it("defsError：显示错误 Alert，不渲染空态", () => {
    const { container } = setup({ defs: [], defsError: "定义接口超时" });
    const alert = container.querySelector(".ant-alert-error");
    expect(alert).not.toBeNull();
    expect(alert!.textContent).toContain("解析项定义加载失败");
    expect(alert!.textContent).toContain("定义接口超时");
  });

  it("无定义且非加载非错误：显示 Empty 空态", () => {
    const { container } = setup({ defs: [], loading: false });
    expect(container.querySelector(".ant-empty")).not.toBeNull();
    expect(container.textContent).toContain("暂无结构化提取项");
  });

  it("状态标签映射：running→蓝… / success 有效→绿✓ / success 空→橙空 / error→红✗ / idle→灰·", () => {
    const items: BaStoredItem[] = [
      stored("project_name", { status: "running" }),
      stored("build_scale", { status: "success", content: "有实际内容" }),
      // json 项：所有字段均「没有提及」→ 视为无有效内容（与后端 _JSON_EMPTY_VALUES 口径一致）
      stored("invest", { status: "success", content: "{\"投资估算\": \"没有提及\"}" }),
      stored("extra", { status: "error", error: "提取超时" }),
      stored("idle_one", { status: "idle" }),
    ];
    // 单独渲染每一项，逐项断言
    const running = setup({ defs: defs.slice(0, 1), items: [items[0]] });
    expect(stateTag(running.container).textContent).toBe("…");
    expect(stateTag(running.container).className).toContain("ant-tag-blue");

    const ok = setup({ defs: defs.slice(1, 2), items: [items[1]] });
    expect(stateTag(ok.container).textContent).toBe("✓");
    expect(stateTag(ok.container).className).toContain("ant-tag-green");

    const empty = setup({ defs: defs.slice(2, 3), items: [items[2]] });
    expect(stateTag(empty.container).textContent).toBe("空");
    expect(stateTag(empty.container).className).toContain("ant-tag-orange");

    const err = setup({ defs: [{ item_id: "extra" }], items: [items[3]] });
    expect(stateTag(err.container).textContent).toBe("✗");
    expect(stateTag(err.container).className).toContain("ant-tag-red");

    const idle = setup({ defs: [{ item_id: "idle_one" }], items: [items[4]] });
    const idleTag = stateTag(idle.container);
    expect(idleTag.textContent).toBe("·");
    expect(idleTag.className).not.toContain("ant-tag-blue");
    expect(idleTag.className).not.toContain("ant-tag-green");
    expect(idleTag.className).not.toContain("ant-tag-orange");
    expect(idleTag.className).not.toContain("ant-tag-red");
  });

  it("必填徽标：required=1 显示「必」，有效内容为绿、否则橙", () => {
    const noContent = setup({ defs: defs.slice(0, 1), items: [stored("project_name", { status: "success", content: "未提取到" })] });
    const mustTagNo = noContent.container.querySelector(".ant-tag-orange")!;
    expect(mustTagNo.textContent).toBe("必");

    const hasContent = setup({ defs: defs.slice(0, 1), items: [stored("project_name", { status: "success", content: "实际内容" })] });
    const mustTagYes = hasContent.container.querySelector(".ant-tag-green")!;
    expect(mustTagYes.textContent).toBe("必");
  });

  it("点击项：onSelectItem 收到 def+stored 合并对象（含 status/content/error）", () => {
    const it = stored("project_name", { status: "success", content: "合并内容", error: "" });
    const { onSelectItem, container } = setup({ items: [it] });
    fireEvent.click(rows(container)[0]);
    const merged = onSelectItem.mock.calls[0][0] as BaItemDef & BaStoredItem;
    expect(merged.item_id).toBe("project_name");
    expect(merged.label).toBe("项目名称");
    expect(merged.status).toBe("success");
    expect(merged.content).toBe("合并内容");
  });

  it("选中态：selectedItemId 命中的行使用主色边框", () => {
    const { container } = setup({ selectedItemId: "build_scale" });
    const list = rows(container);
    expect(list[0].style.border).not.toContain("var(--bp-primary)");
    expect(list[1].style.border).toContain("var(--bp-primary)");
    expect(list[2].style.border).not.toContain("var(--bp-primary)");
  });
});
