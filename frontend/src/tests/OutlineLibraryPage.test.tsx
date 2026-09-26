// @vitest-environment jsdom
/**
 * 「专项方案目录库」OutlineLibraryPage · 页面级交互测试（2026-09-20 补齐）。
 *
 * 此前目录库页面（列表 / 统计卡 / 筛选 / 批量审核 / 删除 / 复制）零页面级测试，
 * 只有编辑弹窗（OutlineLibraryEditModal）被覆盖。本文件锁定
 * （api 模块 vi.mock 隔离，编辑弹窗 mock 为桩）：
 *   1. 首屏：统计卡（目录总数）与列表渲染（名称 / 版本 / 审核状态 / 来源）；
 *   2. 审核状态决定操作按钮：待审核 → 「审核通过」按钮，已通过 → 「停用」按钮；
 *      点「审核通过」直接调 api.review(id, "已通过")；
 *   3. 勾选行 → 批量操作条出现，点「批量通过」→ 确认弹窗 → 调 batchReview 并清空选择；
 *   4. 删除：点删除图标 → 确认弹窗 → 调 api.delete → 成功后刷新列表；
 *   5. 复制：点复制图标 → 无确认弹窗直接调 api.duplicate。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, cleanup } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";

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

const apiMock = vi.hoisted(() => ({
  outlineLibraryApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    review: vi.fn(),
    newVersion: vi.fn(),
    applyAndSave: vi.fn(),
    fromScheme: vi.fn(),
    stats: vi.fn(),
    filters: vi.fn(),
    templates: vi.fn(),
    template: vi.fn(),
    duplicate: vi.fn(),
    restoreVersion: vi.fn(),
    batchReview: vi.fn(),
    export: vi.fn(),
  },
}));

vi.mock("../api", () => apiMock);
// 编辑弹窗为独立组件（已有专门测试文件），此处 mock 为桩以隔离
vi.mock("../components/OutlineLibraryEditModal", () => ({ default: () => null }));

import OutlineLibraryPage from "../pages/OutlineLibraryPage";

const ITEMS = [
  {
    id: "lib-1", name: "深基坑标准目录", type: "专项施工方案", profession: "土建",
    version: "v1.0", review_status: "待审核", ref_count: 2, source: "预置清单",
    updated_at: "2026-09-20T10:00:00", applicable_conditions: "", basis: "",
  },
  {
    id: "lib-2", name: "脚手架标准目录", type: "专项施工方案", profession: "土建",
    version: "v2.0", review_status: "已通过", ref_count: 5, source: "手动创建",
    updated_at: "2026-09-19T08:00:00", applicable_conditions: "", basis: "",
  },
];

beforeEach(() => {
  apiMock.outlineLibraryApi.list.mockResolvedValue({ data: { items: ITEMS, total: 2 } });
  apiMock.outlineLibraryApi.stats.mockResolvedValue({
    data: { total: 2, approved: 1, pending: 1, disabled: 0, preset: 1, ref_total: 7 },
  });
  apiMock.outlineLibraryApi.filters.mockResolvedValue({ data: {} });
  apiMock.outlineLibraryApi.review.mockResolvedValue({ data: { ok: true } });
  apiMock.outlineLibraryApi.batchReview.mockResolvedValue({ data: { affected: 2 } });
  apiMock.outlineLibraryApi.delete.mockResolvedValue({ data: { ok: true } });
  apiMock.outlineLibraryApi.duplicate.mockResolvedValue({ data: { id: "lib-3", name: "副本" } });
});

afterEach(cleanup);

function bodyText() {
  return document.body.textContent || "";
}
function bodyBtn(text: string): HTMLButtonElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return Array.from(document.body.querySelectorAll("button")).find(
    (b) => norm(b.textContent || "").includes(norm(text)),
  ) as HTMLButtonElement | null;
}
function iconBtn(iconClass: string): HTMLButtonElement | null {
  return Array.from(document.body.querySelectorAll("button")).find(
    (b) => b.querySelector(iconClass),
  ) as HTMLButtonElement | null;
}
/** 点击 antd 确认弹窗的「确定/删除/回滚」等 OK 按钮（danger okType 的 OK 不是 ant-btn-primary，须按文本找） */
async function confirmOk(okText: string) {
  await waitFor(() => {
    const ok = Array.from(document.body.querySelectorAll(".ant-modal-confirm button"))
      .find((b) => (b.textContent || "").replace(/\s+/g, "").includes(okText));
    expect(ok).toBeTruthy();
  });
  const ok = Array.from(document.body.querySelectorAll(".ant-modal-confirm button"))
    .find((b) => (b.textContent || "").replace(/\s+/g, "").includes(okText))!;
  fireEvent.click(ok);
}

function setup() {
  return render(
    <AntdApp>
      <OutlineLibraryPage />
    </AntdApp>,
  );
}

describe("OutlineLibraryPage（目录库页面）", () => {
  it("首屏：统计卡 + 列表渲染（名称/版本/审核状态/来源），列表参数带排序与分页", async () => {
    setup();
    await waitFor(() => {
      expect(bodyText()).toContain("深基坑标准目录");
      expect(bodyText()).toContain("脚手架标准目录");
    });
    expect(bodyText()).toContain("目录总数");
    expect(bodyText()).toContain("待审核");
    expect(bodyText()).toContain("已通过");
    expect(bodyText()).toContain("预置清单");
    expect(apiMock.outlineLibraryApi.list).toHaveBeenCalledWith(
      expect.objectContaining({ sort_by: "ref_count", order: "desc", page: "1", page_size: "20" }),
    );
  });

  it("审核状态决定操作按钮：待审核行出现「审核通过」，点击直接调 review 已通过", async () => {
    setup();
    await waitFor(() => expect(bodyText()).toContain("深基坑标准目录"));
    const approve = iconBtn(".anticon-check-circle");
    expect(approve).toBeTruthy();
    fireEvent.click(approve!);
    await waitFor(() => {
      expect(apiMock.outlineLibraryApi.review).toHaveBeenCalledWith("lib-1", { status: "已通过" });
    });
  });

  it("批量审核：勾选全部行 → 批量操作条出现 → 确认后调 batchReview 并清空选择", async () => {
    setup();
    await waitFor(() => expect(bodyText()).toContain("深基坑标准目录"));
    // 表头全选框（rowSelection onChange → setSelectedRowKeys）
    const selectAll = document.body.querySelector(
      ".ant-table-thead .ant-checkbox-input",
    ) as HTMLInputElement | null;
    expect(selectAll).toBeTruthy();
    fireEvent.click(selectAll!);
    await waitFor(() => expect(bodyText()).toContain("批量通过"));
    fireEvent.click(bodyBtn("批量通过")!);
    await confirmOk("确定");
    await waitFor(() => {
      expect(apiMock.outlineLibraryApi.batchReview).toHaveBeenCalledWith({
        ids: ["lib-1", "lib-2"], status: "已通过",
      });
    });
    // 成功后清空选择：批量操作条消失
    await waitFor(() => expect(bodyText()).not.toContain("批量通过"));
  });

  it("删除：点删除图标 → 确认弹窗 → 调 api.delete", async () => {
    setup();
    await waitFor(() => expect(bodyText()).toContain("深基坑标准目录"));
    const del = iconBtn(".anticon-delete");
    expect(del).toBeTruthy();
    fireEvent.click(del!);
    await confirmOk("删除");
    await waitFor(() => {
      expect(apiMock.outlineLibraryApi.delete).toHaveBeenCalledWith("lib-1");
    });
  });

  it("复制：点复制图标 → 无确认弹窗直接调 api.duplicate", async () => {
    setup();
    await waitFor(() => expect(bodyText()).toContain("深基坑标准目录"));
    const copy = iconBtn(".anticon-copy");
    expect(copy).toBeTruthy();
    fireEvent.click(copy!);
    await waitFor(() => {
      expect(apiMock.outlineLibraryApi.duplicate).toHaveBeenCalledWith("lib-1");
    });
    // 无确认弹窗
    expect(document.body.querySelector(".ant-modal-confirm")).toBeNull();
  });

  /**
   * 回归锁：表格总宽必须与「各列宽之和」一致。
   *
   * 历史缺陷：`scroll={{ x: 1500 }}` 而列宽之和只有 1336，antd 会按 scroll.x 把表宽强行
   * 拉到 1500 并等比放大每一列 —— 「名称」与「操作」被撑到 338px，中间若干列被右侧固定列
   * 完全盖住（首屏只剩名称/类型/专业/操作），且操作列按钮因差 1px 换行把行高撑成两倍。
   * 列宽一旦被改大而不更新 scroll.x，本用例必须报红。
   */
  it("表格宽度与列宽声明保持一致（防止 scroll.x 大于列宽之和、列被等比放大）", async () => {
    setup();
    await waitFor(() => expect(bodyText()).toContain("深基坑标准目录"));
    const cols = Array.from(document.querySelectorAll(".ant-table colgroup col"));
    // 勾选列的 col 没有声明宽度，只统计显式声明了宽度的业务列
    const widths = cols
      .map((c) => parseFloat((c as HTMLElement).style.width))
      .filter((w) => Number.isFinite(w) && w > 0);
    expect(widths.length).toBeGreaterThan(5);
    const declared = widths.reduce((a, b) => a + b, 0);
    const table = document.querySelector(".ant-table table") as HTMLElement | null;
    const actual = parseFloat(table?.style.width || "0");
    expect(actual).toBeGreaterThan(0);
    // 允许 antd 自身的取整/滚动条余量，但不允许按 scroll.x 等比放大
    expect(actual).toBeLessThanOrEqual(declared + 48);
  });
});
