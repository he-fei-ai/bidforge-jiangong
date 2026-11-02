// @vitest-environment jsdom
/**
 * R145（2026-10-30）可拖拽面板护栏测试
 *
 * 覆盖用户需求验证点：
 *  A. 拖拽改宽/改高（Pointer Events 增量 → flex-basis 更新）
 *  B. 持久化（拖拽结束写 LocalStorage；重挂载后按存储比例还原）
 *  C. 防坍塌（min/max px 夹紧；max 优先于 minSecond 时取交集下限）
 *  D. 窄屏降级（disabled → 不渲染手柄、堆叠单栏、不读不写持久化）
 *  E. 重置布局（清空持久化 + 回默认比例）
 *  F. 嵌套防冒泡（内层手柄拖拽不干扰外层）
 *  G. 手柄语义（role=separator / aria-orientation / 光标轴类）
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { Tree, type TreeDataNode } from "antd";
import { SplitPane, ResizeHandle } from "../components/resizable/SplitPane";
import {
  RESIZE_STORAGE_PREFIX,
  ResizableLayoutProvider,
  useResizableLayoutReset,
  useResizableResetSignal,
} from "../components/resizable/resizableLayout";

// jsdom 无布局引擎：用可控制的伪尺寸模拟容器与首面板。
// getBoundingClientRect 从当前 flex-basis 推导实际 px —— 键盘/拖拽多步操作时
// rect 跟随最新布局（单次拖拽的既有用例读到的初始值不变，因 flex-basis 未变）。
function mockRects(container: HTMLElement, first: HTMLElement, total: number, firstSize: number) {
  Object.defineProperty(container, "clientWidth", { configurable: true, value: total });
  Object.defineProperty(container, "clientHeight", { configurable: true, value: total });
  Object.defineProperty(first, "getBoundingClientRect", {
    configurable: true,
    value: () => {
      const basisPct = parseFloat(first.style.flexBasis);
      const px = Number.isFinite(basisPct) && basisPct > 0 ? (basisPct / 100) * total : firstSize;
      return { width: px, height: px } as DOMRect;
    },
  });
}

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
});

describe("SplitPane 拖拽与持久化", () => {
  it("A. 横向拖拽：增量 100px → flex-basis 按新比例更新", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-a"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-a"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    // 默认 30% → 模拟容器 1000px、首面板 300px
    mockRects(group, first, 1000, 300);
    expect((first as HTMLElement).style.flexBasis).toBe("30.0000%");

    const handle = screen.getByTestId("resize-handle-t-a");
    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 400, clientY: 0 }); // +100px → 400/1000 = 40%
    fireEvent.pointerUp(handle, { clientX: 400, clientY: 0 });
    expect(first.style.flexBasis).toBe("40.0000%");
  });

  it("B. 持久化：拖拽结束写入 LocalStorage，重挂载按存储比例还原", () => {
    const { unmount } = render(
      <SplitPane
        direction="vertical"
        storageKey="t-b"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>上</div>}
        second={<div>下</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-b"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-b");
    fireEvent.pointerDown(handle, { button: 0, clientX: 0, clientY: 300 });
    fireEvent.pointerMove(handle, { clientX: 0, clientY: 500 }); // 500/1000 = 50%
    fireEvent.pointerUp(handle, { clientX: 0, clientY: 500 });
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-b")).toBe("0.5");
    unmount();

    // 重挂载：无 jsdom 布局，直接断言存储被读取（初始 flex-basis 来自存储值）
    render(
      <SplitPane
        direction="vertical"
        storageKey="t-b"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>上</div>}
        second={<div>下</div>}
      />,
    );
    const first2 = (document.querySelector('[data-storage-key="t-b"]') as HTMLElement)
      .firstElementChild as HTMLElement;
    expect(first2.style.flexBasis).toBe("50.0000%");
  });

  it("C. 防坍塌：拖过头被 max 夹紧（1000px 容器 max=450 → 45%）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-c"
        defaultRatio={0.3}
        min={100}
        max={450}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-c"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-c");
    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 900, clientY: 0 }); // 想拖到 90%
    fireEvent.pointerUp(handle, { clientX: 900, clientY: 0 });
    expect(first.style.flexBasis).toBe("45.0000%");
    // 持久化的是夹紧后的值
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-c")).toBe("0.45");
  });

  it("C2. 极限拖小被 min 夹紧（min=100 → 10%）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-c2"
        defaultRatio={0.5}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-c2"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 500);
    const handle = screen.getByTestId("resize-handle-t-c2");
    fireEvent.pointerDown(handle, { button: 0, clientX: 500, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: -900, clientY: 0 }); // 想拖到 -40%
    fireEvent.pointerUp(handle, { clientX: -900, clientY: 0 });
    expect(first.style.flexBasis).toBe("10.0000%");
  });

  it("D. 窄屏降级：disabled 不渲染手柄、不读写持久化", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-d"
        defaultRatio={0.4}
        disabled
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    expect(screen.queryByTestId("resize-handle-t-d")).toBeNull();
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-d")).toBeNull();
    // 堆叠单栏：disabled 组根类名含 disabled
    expect(
      document.querySelector('.bp-resize-group.disabled[data-storage-key="t-d"]'),
    ).not.toBeNull();
  });

  it("E. 重置布局：清空持久化 + 回默认比例", () => {
    function Inner() {
      const reset = useResizableLayoutReset();
      return (
        <>
          <SplitPane
            direction="horizontal"
            storageKey="t-e"
            defaultRatio={0.3}
            min={100}
            minSecond={100}
            first={<div>左</div>}
            second={<div>右</div>}
          />
          <button onClick={reset}>重置布局</button>
        </>
      );
    }
    function Harness() {
      return (
        <ResizableLayoutProvider>
          <Inner />
        </ResizableLayoutProvider>
      );
    }
    render(<Harness />);
    const group = document.querySelector('[data-storage-key="t-e"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-e");
    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 600, clientY: 0 }); // 60%
    fireEvent.pointerUp(handle, { clientX: 600, clientY: 0 });
    expect(first.style.flexBasis).toBe("60.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e")).toBe("0.6");

    fireEvent.click(screen.getByText("重置布局"));
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e")).toBeNull();
  });

  it("E2. 双击分隔条恢复默认（同时清掉本块持久化）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-e2"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-e2"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-e2");
    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 600, clientY: 0 }); // 60%
    fireEvent.pointerUp(handle, { clientX: 600, clientY: 0 });
    expect(first.style.flexBasis).toBe("60.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e2")).toBe("0.6");

    // ✅ R146：双击分隔条 → 回默认 30% + 持久化被清空（刷新后仍是默认）
    fireEvent.doubleClick(handle);
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e2")).toBeNull();
  });

  it("F. 嵌套防冒泡：内层手柄拖拽不改变外层比例", () => {
    render(
      <SplitPane
        direction="vertical"
        storageKey="t-f-outer"
        defaultRatio={0.4}
        min={100}
        minSecond={100}
        first={
          <SplitPane
            direction="horizontal"
            storageKey="t-f-inner"
            defaultRatio={0.5}
            min={100}
            minSecond={100}
            first={<div>内左</div>}
            second={<div>内右</div>}
          />
        }
        second={<div>下</div>}
      />,
    );
    const outer = document.querySelector('[data-storage-key="t-f-outer"]') as HTMLElement;
    const outerFirst = outer.firstElementChild as HTMLElement;
    mockRects(outer, outerFirst, 1000, 400);

    const innerHandle = screen.getByTestId("resize-handle-t-f-inner");
    fireEvent.pointerDown(innerHandle, { button: 0, clientX: 250, clientY: 0 });
    fireEvent.pointerMove(innerHandle, { clientX: 450, clientY: 0 });
    fireEvent.pointerUp(innerHandle, { clientX: 450, clientY: 0 });

    // 外层比例不受内层拖拽影响（仍为初始 40%）
    expect(outerFirst.style.flexBasis).toBe("40.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-f-outer")).toBeNull();
  });

  it("G. 手柄语义：role=separator + aria-orientation + 轴类光标", () => {
    render(
      <>
        <SplitPane
          direction="horizontal"
          storageKey="t-g-h"
          first={<div>左</div>}
          second={<div>右</div>}
        />
        <SplitPane
          direction="vertical"
          storageKey="t-g-v"
          first={<div>上</div>}
          second={<div>下</div>}
        />
      </>,
    );
    const h = screen.getByTestId("resize-handle-t-g-h");
    const v = screen.getByTestId("resize-handle-t-g-v");
    expect(h.getAttribute("role")).toBe("separator");
    expect(h.getAttribute("aria-orientation")).toBe("vertical"); // 横向切分的手柄是竖条
    expect(h.className).toContain("bp-resize-handle");
    expect(h.className).toContain("axis-x");
    expect(v.getAttribute("aria-orientation")).toBe("horizontal");
    expect(v.className).toContain("axis-y");
  });

  it("H. 键盘可达性：tabIndex=0 + 方向键调比例 + aria-valuenow 语义", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h-kb"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h-kb"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    // 默认 30% → 模拟容器 1000px、首面板 300px
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-h-kb");
    // 可达 + 语义：可聚焦、暴露当前值
    expect(handle.getAttribute("tabindex")).toBe("0");
    expect(handle.getAttribute("role")).toBe("separator");
    expect(handle.getAttribute("aria-valuenow")).toBe("30");
    // → 方向键调宽：每次 16px（300+16=316 → 31.6%）
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(first.style.flexBasis).toBe("31.6000%");
    // ← 方向键回调：316-16=300 → 30%
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(first.style.flexBasis).toBe("30.0000%");
  });

  it("H2. 键盘方向键方向：纵向切分（横条）用 ↑/↓", () => {
    render(
      <SplitPane
        direction="vertical"
        storageKey="t-h-kb-v"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>上</div>}
        second={<div>下</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h-kb-v"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-h-kb-v");
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    expect(first.style.flexBasis).toBe("31.6000%");
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(first.style.flexBasis).toBe("30.0000%");
  });
});

/**
 * ✅ R148b（2026-10-30）双击恢复默认 · 边界与嵌套
 *
 * E2 只覆盖「拖过之后双击」；这里补三类容易漏的分支：
 *  - 键盘改过的布局也能被双击撤回（键盘与拖拽共用同一重置出口）；
 *  - disabled 手柄的双击必须无副作用（窄屏降级时页面上的残留事件不得重置）；
 *  - 双击只作用于**本块**，嵌套/同页多块各自互不串扰；
 *  - 重置后再次调整能重新写入持久化（防「重置把键永久写废」）。
 */
describe("R148b：双击恢复默认（边界与嵌套）", () => {
  it("E3. 键盘调整后双击 → 回默认比例且持久化被清空", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-e3"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-e3"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-e3");

    // 键盘连按 5 档（每档 16px）→ 300 + 80 = 380 → 38%
    for (let i = 0; i < 5; i += 1) fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(first.style.flexBasis).toBe("38.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e3")).toBe("0.38");

    fireEvent.doubleClick(handle);
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e3")).toBeNull();
  });

  it("E4. disabled 手柄的双击不触发重置回调（窄屏降级无副作用）", () => {
    const resets: number[] = [];
    render(
      <ResizeHandle
        direction="horizontal"
        disabled
        onDelta={() => {}}
        onReset={() => resets.push(1)}
        testId="rh-dblclick-disabled"
      />,
    );
    const h = screen.getByTestId("rh-dblclick-disabled");
    fireEvent.doubleClick(h);
    expect(resets).toEqual([]);
  });

  it("E5. 嵌套分栏各自独立：双击内层只重置内层，外层比例与持久化不动", () => {
    window.localStorage.setItem(RESIZE_STORAGE_PREFIX + "t-e5-inner", "0.8");
    render(
      <SplitPane
        direction="vertical"
        storageKey="t-e5-outer"
        defaultRatio={0.4}
        min={100}
        minSecond={100}
        first={
          <SplitPane
            direction="horizontal"
            storageKey="t-e5-inner"
            defaultRatio={0.5}
            min={100}
            minSecond={100}
            first={<div>内左</div>}
            second={<div>内右</div>}
          />
        }
        second={<div>下</div>}
      />,
    );
    const outer = document.querySelector('[data-storage-key="t-e5-outer"]') as HTMLElement;
    const outerFirst = outer.firstElementChild as HTMLElement;
    mockRects(outer, outerFirst, 1000, 400);
    const inner = document.querySelector('[data-storage-key="t-e5-inner"]') as HTMLElement;
    const innerFirst = inner.firstElementChild as HTMLElement;
    // 内层按存储值还原为 80%
    expect(innerFirst.style.flexBasis).toBe("80.0000%");

    fireEvent.doubleClick(screen.getByTestId("resize-handle-t-e5-inner"));

    expect(innerFirst.style.flexBasis).toBe("50.0000%"); // 回内层默认
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e5-inner")).toBeNull();
    expect(outerFirst.style.flexBasis).toBe("40.0000%"); // 外层不受影响
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e5-outer")).toBeNull();
  });

  it("E6. 重置后再次拖拽仍能持久化（键没有被写废）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-e6"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-e6"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-e6");

    fireEvent.doubleClick(handle); // 未改已双击 → 仍是默认、无持久化
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e6")).toBeNull();

    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 350, clientY: 0 }); // 30% → 35%
    fireEvent.pointerUp(handle, { clientX: 350, clientY: 0 });
    expect(first.style.flexBasis).toBe("35.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-e6")).toBe("0.35");
  });
});

/**
 * ✅ R148b（2026-10-30）键盘可达性 · 范围语义 / 边界夹紧 / 持久化
 *
 * H/H2 只证明「方向键能走一档」；这里把无障碍路径最容易缺失的三件事钉住：
 *  - separator 暴露的 valuemin/valuemax 必须是**真实可调范围**（px 约束换算）；
 *  - 键盘到上下限要停住（不能把面板挤没，也不能让键盘比拖拽更宽松）；
 *  - 键盘调整必须同样写 LocalStorage（否则键盘用户刷新即丢布局）。
 */
describe("R148b：键盘可达性（范围语义 / 边界夹紧 / 持久化）", () => {
  it("H3. aria-valuemin/valuemax 由 px 约束换算（min=200→20%，1000-300→70%）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h3"
        defaultRatio={0.25}
        min={200}
        minSecond={300}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h3"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 250);
    const handle = screen.getByTestId("resize-handle-t-h3");

    // 首次渲染容器尺寸未知（jsdom clientWidth=0）→ 只有 valuenow；
    // 一次按键触发的重渲染读到的才是真实容器尺寸。
    expect(handle.getAttribute("aria-valuemin")).toBeNull();
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(first.style.flexBasis).toBe("26.6000%");
    expect(handle.getAttribute("aria-valuenow")).toBe("27");
    expect(handle.getAttribute("aria-valuemin")).toBe("20");
    expect(handle.getAttribute("aria-valuemax")).toBe("70");
  });

  it("H4. 方向键到上限停住（max=300px → 30%，不越界）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h4"
        defaultRatio={0.25}
        min={100}
        max={300}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h4"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 250);
    const handle = screen.getByTestId("resize-handle-t-h4");

    for (let i = 0; i < 4; i += 1) fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(first.style.flexBasis).toBe("30.0000%");
    fireEvent.keyDown(handle, { key: "ArrowRight" }); // 已到上限
    expect(first.style.flexBasis).toBe("30.0000%");
  });

  it("H5. 方向键到下限停住（min=200px → 20%，不坍塌）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h5"
        defaultRatio={0.25}
        min={200}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h5"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 250);
    const handle = screen.getByTestId("resize-handle-t-h5");

    for (let i = 0; i < 4; i += 1) fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(first.style.flexBasis).toBe("20.0000%");
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(first.style.flexBasis).toBe("20.0000%");
  });

  it("H6. 非方向键不改比例、不写持久化（Tab / 字母键交给浏览器默认行为）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h6"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h6"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-h6");

    fireEvent.keyDown(handle, { key: "Tab" });
    fireEvent.keyDown(handle, { key: "a" });
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-h6")).toBeNull();
  });

  it("H7. 键盘调整后写入 LocalStorage（键盘用户刷新同样保持布局）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h7"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h7"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-h7");

    fireEvent.keyDown(handle, { key: "ArrowRight" }); // 300 → 316
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-h7")).toBe("0.316");
  });

  it("H8. 轴向不匹配的方向键不响应（横向切分按 ↑/↓ 无效）", () => {
    render(
      <SplitPane
        direction="horizontal"
        storageKey="t-h8"
        defaultRatio={0.3}
        min={100}
        minSecond={100}
        first={<div>左</div>}
        second={<div>右</div>}
      />,
    );
    const group = document.querySelector('[data-storage-key="t-h8"]') as HTMLElement;
    const first = group.firstElementChild as HTMLElement;
    mockRects(group, first, 1000, 300);
    const handle = screen.getByTestId("resize-handle-t-h8");

    fireEvent.keyDown(handle, { key: "ArrowUp" });
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-h8")).toBeNull();
  });
});

describe("ResizeHandle 独立使用（region 1 侧边栏场景）", () => {
  it("onDelta 上报增量、onDragEnd 拖拽结束回调", () => {
    const deltas: number[] = [];
    let ended = 0;
    render(
      <ResizeHandle
        direction="horizontal"
        onDelta={(d) => deltas.push(d)}
        onDragEnd={() => (ended += 1)}
        testId="rh"
      />,
    );
    const h = screen.getByTestId("rh");
    fireEvent.pointerDown(h, { button: 0, clientX: 100, clientY: 0 });
    fireEvent.pointerMove(h, { clientX: 130, clientY: 0 });
    fireEvent.pointerMove(h, { clientX: 110, clientY: 0 });
    fireEvent.pointerUp(h, { clientX: 110, clientY: 0 });
    expect(deltas).toEqual([30, -20]);
    expect(ended).toBe(1);
  });

  it("disabled 时不响应拖拽", () => {
    const deltas: number[] = [];
    render(<ResizeHandle direction="horizontal" disabled onDelta={(d) => deltas.push(d)} testId="rh2" />);
    const h = screen.getByTestId("rh2");
    fireEvent.pointerDown(h, { button: 0, clientX: 100, clientY: 0 });
    fireEvent.pointerMove(h, { clientX: 150, clientY: 0 });
    fireEvent.pointerUp(h, { clientX: 150, clientY: 0 });
    expect(deltas).toEqual([]);
  });

  it("disabled 时不进 Tab 序且方向键不响应；aria-valuenow 透传", () => {
    const deltas: number[] = [];
    render(
      <ResizeHandle
        direction="horizontal"
        disabled
        ariaValueNow={260}
        ariaValueMin={200}
        ariaValueMax={400}
        onDelta={(d) => deltas.push(d)}
        testId="rh-kb-disabled"
      />,
    );
    const h = screen.getByTestId("rh-kb-disabled");
    expect(h.getAttribute("tabindex")).toBe("-1");
    expect(h.getAttribute("aria-valuenow")).toBe("260");
    expect(h.getAttribute("aria-valuemin")).toBe("200");
    expect(h.getAttribute("aria-valuemax")).toBe("400");
    fireEvent.keyDown(h, { key: "ArrowRight" });
    expect(deltas).toEqual([]); // disabled 不响应方向键
  });
});

describe("重置信号：非 0 才动作（初始挂载不误重置）", () => {
  it("挂载时 resetSignal=0 不清存储、不改比例", () => {
    window.localStorage.setItem(RESIZE_STORAGE_PREFIX + "t-h", "0.7");
    function Probe() {
      const sig = useResizableResetSignal();
      return <span data-testid="sig">{sig}</span>;
    }
    render(
      <ResizableLayoutProvider>
        <SplitPane
          direction="horizontal"
          storageKey="t-h"
          defaultRatio={0.3}
          first={<div>左</div>}
          second={<div>右</div>}
        />
        <Probe />
      </ResizableLayoutProvider>,
    );
    expect(screen.getByTestId("sig").textContent).toBe("0");
    const first = (
      document.querySelector('[data-storage-key="t-h"]') as HTMLElement
    ).firstElementChild as HTMLElement;
    expect(first.style.flexBasis).toBe("70.0000%"); // 存储值生效
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "t-h")).toBe("0.7");
  });
});

/**
 * ✅ R151（2026-10-30）目录树布局 · 双击重置与键盘可达性
 *
 * 背景：R151 删掉了 index.css 里「ant-tree 强制撑满父高」的全局覆写（它让 auto 高度
 * 父容器下的树 computed height 恒为 0）。修复后树的可见性由**分栏 pane 的拖拽/重置**
 * 与**树自身的键盘导航**共同决定，因此这里补两组真实 DOM 用例：
 *  - T1/T2：树放在分栏里时，分隔条的双击重置与方向键调整仍然生效（树不吞手柄事件）；
 *  - T3/T4：树本身可被 Tab 聚焦、方向键建立活动节点并提供读屏播报、←/→ 收起展开。
 *
 * 注：jsdom 未实现 `KeyboardEvent.which`，而 rc-tree 5.x 的键盘分派按 `which` 判断，
 * 故 T3/T4 用 `keyDownLegacy` 给原生事件补 which/keyCode 后再派发（等价真实浏览器）。
 */
const TREE_DATA: TreeDataNode[] = [
  {
    key: "c1",
    title: "第一章 工程概况",
    children: [
      { key: "c1-1", title: "1.1 编制依据" },
      { key: "c1-2", title: "1.2 施工部署" },
    ],
  },
  { key: "c2", title: "第二章 施工方案" },
];

/** rc-tree 5.x 依 `event.which` 分派方向键；jsdom 的 KeyboardEvent 没有该属性 */
function keyDownLegacy(el: Element, key: string, which: number) {
  const ev = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  Object.defineProperty(ev, "which", { value: which, configurable: true });
  Object.defineProperty(ev, "keyCode", { value: which, configurable: true });
  fireEvent(el, ev);
}

/** 渲染一棵与工作台目录树同 props 的树，返回树根（键盘交互都从隐藏焦点输入触发） */
function renderKeyboardTree(defaultExpandAll: boolean, onExpand: (...args: any[]) => void) {
  render(
    <Tree
      treeData={TREE_DATA}
      defaultExpandAll={defaultExpandAll}
      showLine={{ showLeafIcon: false }}
      blockNode
      onExpand={onExpand}
    />,
  );
  return document.querySelector(".ant-tree") as HTMLElement;
}

function renderTreeSplit(storageKey: string, defaultRatio: number) {
  render(
    <SplitPane
      direction="horizontal"
      storageKey={storageKey}
      defaultRatio={defaultRatio}
      min={240}
      minSecond={340}
      first={
        <div className="scroll-area bp-pane-scroll">
          <Tree treeData={TREE_DATA} defaultExpandAll showLine={{ showLeafIcon: false }} blockNode />
        </div>
      }
      second={<div>目录配置</div>}
    />,
  );
  const group = document.querySelector(`[data-storage-key="${storageKey}"]`) as HTMLElement;
  const first = group.firstElementChild as HTMLElement;
  mockRects(group, first, 1000, 300);
  return { group, first, handle: screen.getByTestId(`resize-handle-${storageKey}`) };
}

describe("R151：目录树所在分栏 —— 双击恢复默认", () => {
  it("T1. 拖动后双击分隔条：树容器回默认宽度、本块持久化清空、树内容不丢", () => {
    const { first, handle } = renderTreeSplit("wb-tree", 0.3);
    expect(first.style.flexBasis).toBe("30.0000%");

    fireEvent.pointerDown(handle, { button: 0, clientX: 300, clientY: 0 });
    fireEvent.pointerMove(handle, { clientX: 500, clientY: 0 }); // 300 → 500 = 50%
    fireEvent.pointerUp(handle, { clientX: 500, clientY: 0 });
    expect(first.style.flexBasis).toBe("50.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "wb-tree")).toBe("0.5");

    fireEvent.doubleClick(handle);
    expect(first.style.flexBasis).toBe("30.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "wb-tree")).toBeNull();
    // 重置只改布局，不重建树：默认展开的章节仍在
    expect(screen.getByText("1.1 编制依据")).toBeTruthy();
  });

  it("T2. 键盘改过的树分栏比例，同样能被双击撤回（键盘/拖拽共用同一重置出口）", () => {
    const { first, handle } = renderTreeSplit("wb-tab-outline", 0.44);
    expect(first.style.flexBasis).toBe("44.0000%");

    for (let i = 0; i < 5; i += 1) fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(first.style.flexBasis).toBe("52.0000%"); // 440 + 5×16 = 520
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "wb-tab-outline")).toBe("0.52");

    fireEvent.doubleClick(handle);
    expect(first.style.flexBasis).toBe("44.0000%");
    expect(window.localStorage.getItem(RESIZE_STORAGE_PREFIX + "wb-tab-outline")).toBeNull();
  });
});

describe("R151：目录树键盘可达性", () => {
  it("T3. 树可被 Tab 聚焦：隐藏焦点输入 tabIndex=0 + role=tree，聚焦后暴露 focused 态", () => {
    render(<Tree treeData={TREE_DATA} defaultExpandAll showLine={{ showLeafIcon: false }} blockNode />);

    const root = document.querySelector(".ant-tree") as HTMLElement;
    expect(root).toBeTruthy();
    // rc-tree 的键盘焦点挂在一个 1px 隐藏 input（真实浏览器里 Tab 落点就是它）
    const focusInput = root.querySelector(
      'input[aria-label="for screen reader"]',
    ) as HTMLInputElement;
    expect(focusInput).toBeTruthy();
    expect(focusInput.getAttribute("tabindex")).toBe("0");
    expect(focusInput.disabled).toBe(false);

    // 列表容器本身就是 role=tree（读屏软件据此播报树结构）
    const list = root.querySelector('[role="tree"]') as HTMLElement;
    expect(list).toBeTruthy();
    expect(list.className).toContain("ant-tree-list");

    expect(root.className).not.toContain("ant-tree-focused");
    fireEvent.focus(focusInput);
    expect(root.className).toContain("ant-tree-focused");
    // 结构语义：每个节点 role=treeitem
    expect(root.querySelectorAll('[role="treeitem"]').length).toBeGreaterThanOrEqual(2);
  });

  it("T4. ↓ 建立活动节点并读屏播报；← 收起已展开父节点（onExpand + aria-expanded 同步）", () => {
    const onExpand = vi.fn();
    const root = renderKeyboardTree(true, onExpand);
    const focusInput = root.querySelector(
      'input[aria-label="for screen reader"]',
    ) as HTMLInputElement;
    fireEvent.focus(focusInput);

    // ↓：activeKey 从 null → 第一个可见节点（第一章），根节点获得 active-focused 态
    keyDownLegacy(focusInput, "ArrowDown", 40);
    expect(root.className).toContain("ant-tree-active-focused");
    // 读屏播报的是**键路径**（rc-tree getAccessibilityPath 用 key 而非 title）
    const live = root.querySelector('[aria-live="assertive"]') as HTMLElement;
    expect(live).toBeTruthy();
    expect(live.textContent).toBe("c1");

    // 节点把展开态暴露在 role=treeitem 的 aria-expanded 上（读屏可感知）
    const firstNode = root.querySelector('[role="treeitem"]') as HTMLElement;
    expect(firstNode.getAttribute("aria-expanded")).toBe("true");

    // ←：在已展开的父节点上收起（onExpand 收到空展开集）
    keyDownLegacy(focusInput, "ArrowLeft", 37);
    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onExpand.mock.calls[0][0]).toEqual([]);
    expect(onExpand.mock.calls[0][1]).toMatchObject({ expanded: false });
    expect(firstNode.getAttribute("aria-expanded")).toBe("false");

    // 顶层/同级节点始终按内容自然渲染（R151 后非虚拟、不靠父容器高度）
    const titles = Array.from(root.querySelectorAll(".ant-tree-title")).map((n) => n.textContent);
    expect(titles).toContain("第一章 工程概况");
    expect(titles).toContain("第二章 施工方案");
  });

  it("T5. → 展开折叠节点（默认收起时按 ↓ 选中父节点再 → 展开）", () => {
    // ⚠️ rc-tree 在动画期间（listChanging）会忽略展开操作，故收起/展开分两次渲染各自验证
    const onExpand = vi.fn();
    const root = renderKeyboardTree(false, onExpand);
    const focusInput = root.querySelector(
      'input[aria-label="for screen reader"]',
    ) as HTMLInputElement;
    fireEvent.focus(focusInput);
    keyDownLegacy(focusInput, "ArrowDown", 40);
    const firstNode = root.querySelector('[role="treeitem"]') as HTMLElement;
    expect(firstNode.getAttribute("aria-expanded")).toBe("false");

    keyDownLegacy(focusInput, "ArrowRight", 39);
    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onExpand.mock.calls[0][0]).toEqual(["c1"]);
    expect(onExpand.mock.calls[0][1]).toMatchObject({ expanded: true });
    expect(screen.getByText("1.1 编制依据")).toBeTruthy();
  });
});
