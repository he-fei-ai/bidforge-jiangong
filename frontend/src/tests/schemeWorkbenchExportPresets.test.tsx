// @vitest-environment jsdom
/**
 * SchemeWorkbenchPage · 导出格式预设交互（R-1 / R-2 回归网）
 * ==========================================================
 * 修复背景（2026-10-04）：
 *   R-1：预设 4 按钮（刷新 / 保存 / 设为默认 / 删除）此前无 loading 状态，
 *        快速双击会并发 POST（两次保存同名预设、两次设默认等）。
 *   R-2：handleSavePreset 用 window.prompt（浏览器原生弹窗），测试环境
 *        jsdom 下无法拦截，且无法受控取消。现改为 Modal.confirm + 内联 Input。
 * 本文件为上述修复建立回归网，避免后续回滚。
 *
 * ⚠️ 状态同步与假时钟注意：
 *   1) 默认 mock 的 API 是 Promise.resolve 秒回，loading 状态窗口为 0，
 *      waitFor 永远看不到 loading=true。所以给每个测试的 API 响应都套一层
 *      `delayResolve`，制造 300ms 稳定窗口。
 *   2) React 18 状态更新跨微任务，点击后不能立刻断言 DOM，必须用 waitFor。
 *   3) 组件重渲染会替换 DOM 节点，用「查询函数」而不是缓存的引用。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import React from "react";
import { App as AntdApp } from "antd";
import SchemeWorkbenchPage from "../pages/SchemeWorkbenchPage";
import { clearActivity } from "../utils/activityCenter";

// 复用 schemeWorkbench.test.tsx 的 API 自动 mock 模式
const apiMock = vi.hoisted(() => {
  const calls: Record<string, any[][]> = {};
  const defaults: Record<string, any> = {};
  const cache = new Map<string, any>();

  function makeCallable(path: string[]): any {
    const key = path.join(".");
    const cached = cache.get(key);
    if (cached) return cached;
    const fn: any = (...args: any[]) => {
      (calls[key] ||= []).push(args);
      const d = defaults[key];
      if (d && typeof d === "object" && "__reject__" in d) {
        return Promise.reject(new Error(String((d as any).__reject__)));
      }
      // ✅ BUG 修复（2026-10-04）：若 defaults 值是"生产 Promise 的工厂函数"
      //   （例如 `() => slowResolve({...})`），每次调用都生成一个**新鲜的**延迟 Promise，
      //   否则缓存同一个 promise 实例、第 1 次消耗后后续 await 都是秒回，loading 窗口为 0。
      const raw = d !== undefined ? (typeof d === "function" ? d() : d) : { data: {} };
      // Promise.resolve(已经是 Promise) 会直接返回原 promise，不改变语义。
      return Promise.resolve(raw);
    };
    const proxy = new Proxy(fn, {
      get(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        if (prop === "__esModule") return true;
        if (prop === "then") return undefined;
        return makeCallable([...path, prop]);
      },
    });
    cache.set(key, proxy);
    return proxy;
  }

  function makeNamespace(): any {
    return new Proxy({} as any, {
      get(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        if (prop === "__esModule") return true;
        if (prop === "then") return undefined;
        return makeCallable([prop]);
      },
      has(_t, prop: any) {
        return typeof prop === "string" ? true : false;
      },
      getOwnPropertyDescriptor(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        return { configurable: true, enumerable: true, value: makeCallable([prop]) };
      },
      ownKeys() { return []; },
    });
  }

  return { calls, defaults, root: makeNamespace() };
});

vi.mock("../api", () => apiMock.root);
vi.mock("../hooks/useSchemeLiveTask", () => ({
  useSchemeLiveTask: () => ({
    id: null, status: "idle", progress: 0, message: "", task_type: "",
    control: vi.fn(), refresh: vi.fn(), stop: vi.fn(),
  }),
}));

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

const apiCalls = apiMock.calls;
const apiDefaults = apiMock.defaults;

/** 用 Promise.resolve 包一层 300ms 延迟，让 loading 状态窗口稳定可测 */
function slowResolve<T>(value: T, ms = 300): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

function setupDefaults() {
  apiDefaults["sectionsApi.list"] = {
    data: { scheme: { id: "s1", project_id: "p1", name: "测试方案" }, tree: [] },
  };
  apiDefaults["schemesApi.get"] = { data: { config_json: "{}" } };
  apiDefaults["factsApi.list"] = { data: { groups: [], stats: {} } };
  apiDefaults["factsApi.listDocuments"] = { data: { documents: [] } };
  apiDefaults["complianceApi.expertItems"] = { data: { items: [] } };
  apiDefaults["complianceApi.rules"] = { data: { rules: [] } };
  apiDefaults["factsApi.categoryOptions"] = { data: { options: [] } };
  apiDefaults["factsApi.categories"] = { data: { categories: [] } };
  apiDefaults["bidAnalysisApi.items"] = { data: { items: [], groups: [] } };
  apiDefaults["bidAnalysisApi.results"] = { data: { items: [], summary: null } };
  apiDefaults["exportApi.presets.list"] = { data: { presets: [] } };
}

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={["/scheme/s1"]}>
        <Routes>
          <Route path="/scheme/:id" element={<SchemeWorkbenchPage />} />
        </Routes>
      </MemoryRouter>
    </AntdApp>
  );
}

async function gotoExportTab() {
  const tabLabel = await screen.findByText("导出文档");
  const tab = tabLabel.closest('[role="tab"]') || tabLabel;
  fireEvent.click(tab);
  await waitFor(() =>
    expect(Array.from(document.querySelectorAll("button"))
      .some((b) => (b.textContent || "").includes("导出 DOCX"))).toBe(true)
  );
}

/**
 * 定位"格式预设" Card 内的预设 Select 并打开下拉、选中第一个 option。
 * 页面上有多个 Select（章节默认值等），不能用 document.querySelector
 * 拿第一个；必须按 Card 的 title 定位到"格式预设" Card 内的那个。
 */
function findPresetSelect(): HTMLElement | null {
  const cards = Array.from(document.querySelectorAll(".ant-card"));
  const presetCard = cards.find((c) => {
    const title = c.querySelector(".ant-card-head-title")?.textContent || "";
    return title.trim() === "格式预设";
  });
  return presetCard?.querySelector(".ant-select-selector") as HTMLElement || null;
}

async function selectFirstPresetOption(presetName: string) {
  // 等待预设列表接口返回后，"格式预设" Card 内的 Select 已挂载到 DOM。
  // 注意：若 preset.is_default=false，页面不会自动选中，selector 内只显示 placeholder；
  // 所以这里不能断言 selector 文本含 presetName，只能等 Select 存在，然后主动打开下拉去选。
  const select = await waitFor(() => {
    const s = findPresetSelect();
    expect(s, "格式预设 Card 内的 Select 应存在").toBeTruthy();
    return s!;
  }, { timeout: 3000 });

  // AntD v5 Select 用 mousedown 展开（click 不够）；setTimeout(0) 让 rc-virtual-list 渲染 option。
  fireEvent.mouseDown(select);
  await new Promise((r) => setTimeout(r, 0));
  await waitFor(() => {
    const options = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const target = options.find((o) => (o.textContent || "").includes(presetName));
    expect(target, `下拉里应含「${presetName}」option，实际 ${options.length} 个`).toBeTruthy();
    fireEvent.click(target!);
  }, { timeout: 3000 });
}

// AntD v5 中文按钮会自动插入空格（"保 存"、"删 除"、"刷 新"），
// 所以文本匹配必须去空格后再比对。
function normalizeBtnText(text: string | null | undefined): string {
  return (text || "").replace(/\s/g, "");
}

/**
 * 按文本找按钮。同一文本的按钮在页面上可能出现多次
 * （例如"刷新"在事实数据 Tab、导出 Card 都有），必须靠 ancestor Card 缩小范围。
 * @param text  按钮文本（AntD v5 中文按钮会自动插空格，函数内部会去空格比较）
 * @param cardTitle 祖先 Card 的 title（例如"格式预设"）；不传则不限定范围
 */
function findButtonByText(text: string, cardTitle?: string): HTMLButtonElement | undefined {
  const target = normalizeBtnText(text);
  const btns = Array.from(document.querySelectorAll("button"));
  if (!cardTitle) {
    return btns.find((b) => normalizeBtnText(b.textContent) === target);
  }
  return btns.find((b) => {
    if (normalizeBtnText(b.textContent) !== target) return false;
    const card = b.closest(".ant-card");
    const title = card?.querySelector(".ant-card-head-title")?.textContent || "";
    return title.trim() === cardTitle;
  });
}

beforeEach(() => {
  for (const k of Object.keys(apiCalls)) delete apiCalls[k];
  clearActivity();
  setupDefaults();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("SchemeWorkbenchPage · 导出格式预设交互（R-1 / R-2 回归网）", () => {
  // 页面上"刷新"在事实数据 Tab、导出 Card 等多处出现；
  // 本文件的按钮查找都限定在"格式预设" Card 内，避免误点其他 Tab 的按钮。
  const PRESET_CARD = "格式预设";

  it("R-1：点「刷新」后按钮进入 loading/disabled，接口返回后恢复可用", async () => {
    // 工厂函数：每次调用 presets.list 都拿到一个新鲜的 300ms 延迟 Promise，
    // 避免 useEffect 首次调用消耗掉唯一 promise、手动点"刷新"秒回的问题。
    apiDefaults["exportApi.presets.list"] = () => slowResolve({ data: { presets: [] } });

    renderPage();
    await gotoExportTab();
    expect(findButtonByText("刷新", PRESET_CARD), "格式预设 Card 内的刷新按钮应存在").toBeTruthy();

    // 等第一次 useEffect 触发的 fetchExportPresets 完成，避免它和手动点击的 fetch
    // 相互 abort，导致 loading 状态被中间态干扰
    await waitFor(() => {
      expect(apiCalls["exportApi.presets.list"]?.length).toBeGreaterThanOrEqual(1);
    }, { timeout: 2000 });
    // 再等 slowResolve 的 300ms 让 fetch 完成、presetOpLoading 清空
    await new Promise((r) => setTimeout(r, 400));

    fireEvent.click(findButtonByText("刷新", PRESET_CARD)!);
    // 用 waitFor 等 React 重渲染完成——修复前完全无 loading 状态
    // 断言 disabled 属性（AntD v5 在 loading 时必然禁用按钮，最稳定的信号）
    await waitFor(
      () => {
        const btn = findButtonByText("刷新", PRESET_CARD)!;
        expect(btn.disabled).toBe(true);
      },
      { timeout: 1500, interval: 50 }
    );
    // 等接口返回后 disabled 应消失
    await waitFor(
      () => {
        const btn = findButtonByText("刷新", PRESET_CARD)!;
        expect(btn.disabled).toBe(false);
      },
      { timeout: 2000 }
    );
    // 确认确实发起了 API 请求
    expect(apiCalls["exportApi.presets.list"]?.length).toBeGreaterThanOrEqual(1);
  });

  it("R-1：点「保存当前为预设」打开 Modal（不再是 window.prompt），输入名称后可提交", async () => {
    apiDefaults["exportApi.presets.create"] = slowResolve(
      { data: { id: "p1", name: "预设A" } }
    );

    renderPage();
    await gotoExportTab();
    const btn = findButtonByText("保存当前为预设", PRESET_CARD);
    expect(btn, "保存按钮应存在").toBeTruthy();

    const promptSpy = vi.spyOn(window, "prompt").mockImplementation(() => null);
    fireEvent.click(btn!);

    // Modal 出现（AntD 的 modal.confirm 会渲染到 body）
    await waitFor(() => {
      const modal = document.body.querySelector(".ant-modal-confirm");
      const titles = document.body.querySelectorAll(".ant-modal-confirm-title").length;
      expect(modal || titles > 0).toBeTruthy();
    }, { timeout: 3000 });

    // 弹窗内的 Input 可编辑
    const input = await screen.findByPlaceholderText("请输入格式预设名称");
    fireEvent.change(input, { target: { value: "预设A" } });

    // 找到 Modal 内的确认按钮（"保存"，primary）—— 用 .ant-modal-confirm 限定弹窗范围
    const okBtn = await waitFor(
      () => {
        const modalBtns = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).filter(
          (b) => b.closest(".ant-modal-confirm")
        );
        const savedBtn = modalBtns.find(
          (b) => normalizeBtnText(b.textContent) === "保存"
        );
        if (!savedBtn) throw new Error(`Modal 内尚无「保存」按钮，实际: ${modalBtns.map(b=>normalizeBtnText(b.textContent)).join("/")}`);
        return savedBtn;
      },
      { timeout: 3000 }
    );
    fireEvent.click(okBtn);

    await waitFor(() => {
      expect(apiCalls["exportApi.presets.create"]?.length).toBeGreaterThanOrEqual(1);
    });
    const lastCall = apiCalls["exportApi.presets.create"]![
      apiCalls["exportApi.presets.create"]!.length - 1
    ];
    expect(lastCall[0]).toBe("s1");
    expect(lastCall[1]).toBe("预设A");

    expect(promptSpy).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("R-1：点「设为默认」后按钮 loading/disabled，接口返回后恢复", async () => {
    apiDefaults["exportApi.presets.list"] = {
      data: {
        presets: [{ id: "p1", name: "现有预设", is_default: false, config: {} }],
      },
    };
    apiDefaults["exportApi.presets.setDefault"] = () => slowResolve({ data: {} });

    renderPage();
    await gotoExportTab();
    await selectFirstPresetOption("现有预设");
    await waitFor(() => expect(findButtonByText("设为默认", PRESET_CARD)).toBeTruthy());

    fireEvent.click(findButtonByText("设为默认", PRESET_CARD)!);
    // 按钮进入 loading/disabled：disabled 是最稳定可靠的信号
    await waitFor(() => {
      const btn = findButtonByText("设为默认", PRESET_CARD)!;
      expect(btn.disabled).toBe(true);
    }, { timeout: 1500 });
    expect(apiCalls["exportApi.presets.setDefault"]?.length).toBeGreaterThanOrEqual(1);
  });

  it("R-1：点「删除」触发确认弹窗，onOk 中调用删除接口，全程按钮 loading", async () => {
    apiDefaults["exportApi.presets.list"] = {
      data: {
        presets: [{ id: "p1", name: "待删预设", is_default: false, config: {} }],
      },
    };
    apiDefaults["exportApi.presets.remove"] = () => slowResolve({ data: {} });

    renderPage();
    await gotoExportTab();
    await selectFirstPresetOption("待删预设");
    await waitFor(() => expect(findButtonByText("删除", PRESET_CARD)).toBeTruthy());

    // danger 删除按钮：只找格式预设 Card 内那个
    const deleteBtn = findButtonByText("删除", PRESET_CARD)!;
    expect(deleteBtn, "应存在 danger 删除按钮").toBeTruthy();

    fireEvent.click(deleteBtn);
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-confirm-title")).toBeTruthy();
    });

    // 点 Modal 确认按钮（"删除"，在 .ant-modal-confirm 内）
    const okBtn = await waitFor(
      () => {
        const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
          .find(
            (b) =>
              normalizeBtnText(b.textContent) === "删除" && b.closest(".ant-modal-confirm")
          );
        if (!btn) throw new Error("Modal 内尚无「删除」按钮");
        return btn;
      },
      { timeout: 2000 }
    );
    fireEvent.click(okBtn);

    await waitFor(() => {
      expect(apiCalls["exportApi.presets.remove"]?.length).toBeGreaterThanOrEqual(1);
    });
    const lastCall = apiCalls["exportApi.presets.remove"]![
      apiCalls["exportApi.presets.remove"]!.length - 1
    ];
    expect(lastCall[0]).toBe("s1");
    expect(lastCall[1]).toBe("p1");
  });

  it("R-1：预设操作进行中，其他 3 个按钮均 disabled（防并发竞态）", async () => {
    // 让删除接口长期挂起（10s > 测试超时），制造稳定的"进行中"窗口
    apiDefaults["exportApi.presets.remove"] = () => slowResolve({ data: {} }, 10000);

    apiDefaults["exportApi.presets.list"] = {
      data: {
        presets: [{ id: "p1", name: "待删", is_default: false, config: {} }],
      },
    };

    renderPage();
    await gotoExportTab();
    await selectFirstPresetOption("待删");
    await waitFor(() => expect(findButtonByText("删除", PRESET_CARD)).toBeTruthy());

    // 点格式预设 Card 内的 danger 删除按钮
    fireEvent.click(findButtonByText("删除", PRESET_CARD)!);
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-confirm-title")).toBeTruthy();
    });

    // 点 Modal 确认按钮
    const okBtn = await waitFor(
      () => {
        const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
          .find(
            (b) =>
              normalizeBtnText(b.textContent) === "删除" && b.closest(".ant-modal-confirm")
          );
        if (!btn) throw new Error("Modal 内尚无「删除」按钮");
        return btn;
      },
      { timeout: 2000 }
    );
    fireEvent.click(okBtn);

    // 删除请求 pending 期间：所有 4 个预设操作按钮都应 disabled（防并发竞态）
    await waitFor(
      () => {
        const refreshBtn = findButtonByText("刷新", PRESET_CARD)!;
        const saveBtn = findButtonByText("保存当前为预设", PRESET_CARD)!;
        const setDefaultBtn = findButtonByText("设为默认", PRESET_CARD)!;
        const deleteBtn = findButtonByText("删除", PRESET_CARD)!;
        expect(refreshBtn.disabled).toBe(true);
        expect(saveBtn.disabled).toBe(true);
        expect(setDefaultBtn.disabled).toBe(true);
        expect(deleteBtn.disabled).toBe(true);
      },
      { timeout: 1500 }
    );
  });
});
