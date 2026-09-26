// @vitest-environment jsdom
/**
 * 「目录库编辑」OutlineLibraryEditModal · 组件级交互测试（补全 F10）。
 *
 * 目录库（12 分类 / 141 标准模板）的编辑入口，承载「新增/拖拽/上移下移/改名/
 * 三级深度拦截/套用模板/导入识别/保存落库」等交互，此前无组件级测试。
 * 本文件锁定（api 模块以 vi.mock 隔离，避免真实 HTTP）：
 *   1. open=true 渲染标题「新增目录库」+ 基本信息/目录章节 两个 Tab；
 *   2. 取消按钮上抛 onClose；
 *   3. 空树保存：拦截（不调用 api.create / onSaved），并提示「目录章节为空」；
 *   4. 新增一级章节：目录树出现「新章节」，章节计数 +1；
 *   5. 填写名称 + 有章节后保存：调用 api.create 并上抛 onSaved（保存链路打通）；
 *   6. 三级深度拦截：在三级节点下继续加子章节被拦截（告警，不新增第四级）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import React from "react";
import { App } from "antd";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

// vi.mock 工厂引用的对象必须在 hoist 阶段可用（vi.hoisted）
const apiMock = vi.hoisted(() => ({
  outlineLibraryApi: {
    templates: vi.fn(() => Promise.resolve({ data: { items: [] } })),
    filters: vi.fn(() => Promise.resolve({ data: { type: [] } })),
    get: vi.fn(() => Promise.resolve({ data: {} })),
    create: vi.fn(() => Promise.resolve({ data: { id: "lib1" } })),
    update: vi.fn(() => Promise.resolve({ data: { id: "lib1" } })),
    template: vi.fn(() => Promise.resolve({ data: { outline: [] } })),
  },
  uploadOutlineApi: {
    parse: vi.fn(() => Promise.resolve({ data: { outline: [], file_name: "x.pdf" } })),
  },
}));

vi.mock("../api", () => apiMock);

import OutlineLibraryEditModal from "../components/OutlineLibraryEditModal";
import { clearActivity, getActivityItems } from "../utils/activityCenter";

const bodyText = () => document.body.textContent || "";
function bodyBtn(text: string): HTMLButtonElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return Array.from(document.body.querySelectorAll("button")).find(
    (b) => norm(b.textContent).includes(norm(text)),
  ) as HTMLButtonElement | null;
}
function clickTab(label: string) {
  const tab = Array.from(document.body.querySelectorAll(".ant-tabs-tab")).find(
    (t) => (t.textContent || "").includes(label),
  );
  if (tab) fireEvent.click(tab);
}

beforeEach(() => {
  clearActivity();
  apiMock.outlineLibraryApi.create.mockClear();
  apiMock.outlineLibraryApi.update.mockClear();
});

// 隔离：每个用例结束后卸载，避免 antd Modal 默认 destroyOnClose=false 导致多份弹窗
// 在 document.body 中累积，干扰 bodyBtn / 树节点查询。
let mounted: ReturnType<typeof render> | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function setup(open: boolean, libraryId?: string | null) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const utils = render(
    <App>
      <OutlineLibraryEditModal open={open} libraryId={libraryId} onClose={onClose} onSaved={onSaved} />
    </App>,
  );
  mounted = utils;
  return { ...utils, onClose, onSaved };
}

describe("OutlineLibraryEditModal · 打开与关闭", () => {
  it("open=true：渲染标题与两个 Tab", () => {
    setup(true);
    expect(bodyText()).toContain("新增目录库");
    expect(bodyText()).toContain("基本信息");
    expect(bodyText()).toContain("目录章节");
  });

  it("open=false：内容仍在（forceRender 模式，避免 useForm 未连接告警）", () => {
    // forceRender 让 Modal 在关闭时仍挂载子树 —— form 实例始终有 Form DOM，
    // 同时 setFieldsValue 可在 Modal 打开前就回填。
    setup(false);
    expect(bodyText()).toContain("新增目录库");
    expect(bodyText()).toContain("基本信息");
  });

  it("取消按钮：上抛 onClose", () => {
    const { onClose } = setup(true);
    fireEvent.click(bodyBtn("取消")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("OutlineLibraryEditModal · 保存校验与链路", () => {
  it("空树保存：拦截，不调用 api.create / onSaved，并提示章节为空", async () => {
    const { onSaved } = setup(true);
    // 表单校验（名称必填）先于空树校验，先填名称再点保存
    const nameInput = document.body.querySelector('input[placeholder*="深基坑"]') as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "测试标准目录" } });
    await act(async () => { fireEvent.click(bodyBtn("保存")!); });
    expect(apiMock.outlineLibraryApi.create).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(getActivityItems().some((i) => i.text.includes("目录章节为空"))).toBe(true);
  });

  it("新增一级章节：树出现「新章节」且计数 +1", () => {
    setup(true);
    clickTab("目录章节");
    expect(bodyText()).toContain("暂无目录");
    fireEvent.click(bodyBtn("新增一级章节")!);
    expect(bodyText()).toContain("新章节");
    expect(bodyText()).toContain("共 1 个章节");
  });

  it("填写名称 + 有章节后保存：调用 api.create 并上抛 onSaved", async () => {
    const { onSaved } = setup(true);
    const nameInput = document.body.querySelector('input[placeholder*="深基坑"]') as HTMLInputElement;
    expect(nameInput).toBeTruthy();
    fireEvent.change(nameInput, { target: { value: "测试标准目录" } });
    clickTab("目录章节");
    fireEvent.click(bodyBtn("新增一级章节")!);
    expect(bodyText()).toContain("新章节");
    await act(async () => { fireEvent.click(bodyBtn("保存")!); });
    expect(apiMock.outlineLibraryApi.create).toHaveBeenCalledTimes(1);
    expect(onSaved).toHaveBeenCalledTimes(1);
  });
});

describe("OutlineLibraryEditModal · 三级深度拦截", () => {
  it("逐级加子章节直至三级；继续加第四级被拦截（告警，计数不增长）", () => {
    setup(true);
    clickTab("目录章节");
    // 新增一级章节（L1，自动选中）→ 连续「新增子章节」利用自动选中逐级下钻
    fireEvent.click(bodyBtn("新增一级章节")!);
    expect(bodyText()).toContain("共 1 个章节");
    fireEvent.click(bodyBtn("新增子章节")!); // L2
    expect(bodyText()).toContain("共 2 个章节");
    fireEvent.click(bodyBtn("新增子章节")!); // L3
    expect(bodyText()).toContain("共 3 个章节");
    // 在三级节点下继续加 → 拦截（newLevel=4 > MAX_OUTLINE_DEPTH=3）
    fireEvent.click(bodyBtn("新增子章节")!);
    expect(bodyText()).toContain("共 3 个章节");
    expect(getActivityItems().some((i) => i.text.includes("目录最多支持 3 级"))).toBe(true);
  });
});
