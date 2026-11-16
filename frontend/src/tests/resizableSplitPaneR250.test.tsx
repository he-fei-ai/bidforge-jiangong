// @vitest-environment jsdom
/**
 * R250 回归测试：可拖拽分隔面板（SplitPane）的布局持久化与边界钳制，
 * 以及 layoutStore 纯函数与全量清理。
 *
 * SplitPane 内部基于 re-resizable，props 与手写版逐字一致：
 *   orientation / storageKey / defaultSize / minSize / maxSize / first / second / ariaLabel。
 * 初始化时读 localStorage（键 = storageKey）→ Number 解析 → 钳制到 [minSize, maxSize]，
 * 缺失或非法回落 defaultSize。首窗格的可拖拽那一维（horizontal=width）以内联样式暴露。
 */
import { describe, it, expect, beforeEach, afterEach, beforeAll } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { StrictMode } from "react";
import { SplitPane } from "../components/SplitPane";
import {
  readLayout,
  writeLayout,
  clearAllLayouts,
  emitLayoutReset,
  LAYOUT_RESET_EVENT,
  LAYOUT_STORAGE_PREFIX,
} from "../components/resizable/layoutStore";

// ✅ 与全项目测试约定一致：补齐 jsdom 缺失的 matchMedia / ResizeObserver。
//    re-resizable / antd 在 jsdom 下会触碰到这两个 API，缺了会在渲染期抛错。
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
  const g = globalThis as unknown as { ResizeObserver?: unknown };
  if (!g.ResizeObserver) {
    g.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

// 统一测试参数（Group A 共用）。
const STORAGE_KEY = "swb-split-test-width";
const DEFAULT_SIZE = 300;
const MIN_SIZE = 200;
const MAX_SIZE = 600;

/** 渲染一个标准水平分隔面板，左右栏文案固定，便于 screen.getByText 断言。 */
function renderPane() {
  return render(
    <SplitPane
      orientation="horizontal"
      storageKey={STORAGE_KEY}
      defaultSize={DEFAULT_SIZE}
      minSize={MIN_SIZE}
      maxSize={MAX_SIZE}
      first={<div>左栏</div>}
      second={<div>右栏</div>}
      ariaLabel="测试分隔"
    />,
  );
}

/** 读首窗格当前尺寸：取 `.swb-split-first` 的父元素（Resizable 根）内联 width 并 parseFloat。 */
function readFirstWidth(): number {
  const first = document.querySelector<HTMLElement>(".swb-split-first");
  expect(first, "应渲染出 .swb-split-first 首窗格").toBeTruthy();
  const resizableRoot = first!.parentElement;
  expect(resizableRoot, "首窗格应被 Resizable 包裹").toBeTruthy();
  return parseFloat(resizableRoot!.style.width);
}

beforeEach(() => {
  window.localStorage.clear();
  cleanup();
});

afterEach(() => {
  window.localStorage.clear();
  cleanup();
});

describe("可拖拽分隔面板 · 布局持久化与边界（R250 回归）", () => {
  it("1. 无存储记录时首窗格按 defaultSize=300 渲染", () => {
    renderPane();
    expect(readFirstWidth()).toBe(DEFAULT_SIZE);
  });

  it("2. 存储合法值 420 时恢复到 420（刷新/切页持久化）", () => {
    window.localStorage.setItem(STORAGE_KEY, "420");
    renderPane();
    expect(readFirstWidth()).toBe(420);
  });

  it("3. 存储值 40 低于 minSize=200 时钳制到 200", () => {
    window.localStorage.setItem(STORAGE_KEY, "40");
    renderPane();
    expect(readFirstWidth()).toBe(MIN_SIZE);
  });

  it("4. 存储值 9999 超过 maxSize=600 时钳制到 600", () => {
    window.localStorage.setItem(STORAGE_KEY, "9999");
    renderPane();
    expect(readFirstWidth()).toBe(MAX_SIZE);
  });

  it("5. 存储非数字 not-a-number 时回落到 defaultSize=300", () => {
    window.localStorage.setItem(STORAGE_KEY, "not-a-number");
    renderPane();
    expect(readFirstWidth()).toBe(DEFAULT_SIZE);
  });

  it("6. 收到 LAYOUT_RESET_EVENT 广播后复位到 300 并清掉 localStorage 键", () => {
    window.localStorage.setItem(STORAGE_KEY, "520");
    renderPane();
    expect(readFirstWidth()).toBe(520);

    // 模拟「重置布局」按钮广播复位事件。
    fireEvent(window, new CustomEvent(LAYOUT_RESET_EVENT));

    expect(readFirstWidth()).toBe(DEFAULT_SIZE);
    // 复位事件应移除本地记忆键（而非写入默认值）。
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("7. 双击拖拽手柄后复位到 300 并把 300 写回 localStorage", () => {
    window.localStorage.setItem(STORAGE_KEY, "540");
    renderPane();
    expect(readFirstWidth()).toBe(540);

    const handle = document.querySelector<HTMLElement>(".swb-split-handle");
    expect(handle, "应渲染出 .swb-split-handle 拖拽手柄").toBeTruthy();
    fireEvent.dblClick(handle!);

    expect(readFirstWidth()).toBe(DEFAULT_SIZE);
    // 双击复位会显式把默认值写回本地记忆。
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("300");
  });

  it("8. 手柄具备 .swb-split-handle 类，且左右两栏文案均渲染", () => {
    renderPane();
    const handle = document.querySelector<HTMLElement>(".swb-split-handle");
    expect(handle).toBeTruthy();
    expect(handle!.className).toContain("swb-split-handle");
    expect(screen.getByText("左栏")).toBeTruthy();
    expect(screen.getByText("右栏")).toBeTruthy();
  });

  it("9. StrictMode 下收到复位广播仍稳定复位到 300（无双监听抖动）", () => {
    window.localStorage.setItem(STORAGE_KEY, "450");
    render(
      <StrictMode>
        <SplitPane
          orientation="horizontal"
          storageKey={STORAGE_KEY}
          defaultSize={DEFAULT_SIZE}
          minSize={MIN_SIZE}
          maxSize={MAX_SIZE}
          first={<div>左栏</div>}
          second={<div>右栏</div>}
          ariaLabel="测试分隔"
        />
      </StrictMode>,
    );
    expect(readFirstWidth()).toBe(450);

    fireEvent(window, new CustomEvent(LAYOUT_RESET_EVENT));

    expect(readFirstWidth()).toBe(DEFAULT_SIZE);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("layoutStore · 纯函数与全量清理（R250 回归）", () => {
  it("1. writeLayout 后 readLayout 读回相同比例数组", () => {
    writeLayout("group-a", [40, 60]);
    expect(readLayout("group-a")).toEqual([40, 60]);
    // 确认写入带前缀键。
    expect(window.localStorage.getItem(LAYOUT_STORAGE_PREFIX + "group-a")).toBe("[40,60]");
  });

  it("2. 各类非法记录 readLayout 一律返回 null（走默认布局）", () => {
    // 非数组 JSON。
    window.localStorage.setItem(LAYOUT_STORAGE_PREFIX + "k1", '{"a":1}');
    expect(readLayout("k1")).toBeNull();
    // 比例之和为 0（面板全部塌陷）。
    window.localStorage.setItem(LAYOUT_STORAGE_PREFIX + "k2", "[0,0]");
    expect(readLayout("k2")).toBeNull();
    // 含负数。
    window.localStorage.setItem(LAYOUT_STORAGE_PREFIX + "k3", "[-1,60]");
    expect(readLayout("k3")).toBeNull();
    // 畸形 JSON。
    window.localStorage.setItem(LAYOUT_STORAGE_PREFIX + "k4", "{oops");
    expect(readLayout("k4")).toBeNull();
    // 空键。
    expect(readLayout("")).toBeNull();
    // 缺失键（从未写入）。
    expect(readLayout("never-written")).toBeNull();
  });

  it("3. clearAllLayouts 同时清掉带前缀键与旧版裸 swb-split-* 键，返回计数≥2", () => {
    writeLayout("g1", [50, 50]);
    window.localStorage.setItem("swb-split-legacy-width", "320");

    const removed = clearAllLayouts();

    expect(removed).toBeGreaterThanOrEqual(2);
    expect(window.localStorage.getItem(LAYOUT_STORAGE_PREFIX + "g1")).toBeNull();
    expect(window.localStorage.getItem("swb-split-legacy-width")).toBeNull();
  });

  it("4. emitLayoutReset 真正派发 window 上的 LAYOUT_RESET_EVENT", () => {
    let fired = 0;
    const onReset = () => {
      fired += 1;
    };
    window.addEventListener(LAYOUT_RESET_EVENT, onReset);
    try {
      emitLayoutReset();
      expect(fired).toBe(1);
    } finally {
      window.removeEventListener(LAYOUT_RESET_EVENT, onReset);
    }
  });
});
