// @vitest-environment jsdom
/**
 * 「访问凭据」页面契约测试。
 *
 * 背景：前端 `getApiToken()` 认两个来源 —— 构建期 `VITE_API_TOKEN` 与
 * `localStorage.api_token`。但此前**没有任何 UI 会写 localStorage.api_token**，
 * 运维一旦在 backend/.env 启用 API_AUTH_TOKEN，前端不重新构建就整体 401、
 * 页面打不开，且用户看不到任何"该去哪填 token"的指引。
 *
 * 本文件锁定两件事：
 * 1. 页面保存的键必须与 api 层读取的键**逐字符一致**（键名漂移会导致
 *    "填了也没用"这种最难排查的静默失效）；
 * 2. 保存/清除操作确实落到 localStorage，且探测失败时给出可读提示。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, fireEvent } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";

// jsdom 没有 matchMedia，antd 的响应式组件会用到
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

const h = vi.hoisted(() => ({ activity: vi.fn() }));

// 页面只依赖 systemApi.activity 做「探测凭据是否可用」
vi.mock("../api", () => ({ systemApi: { activity: h.activity }, clearAuthShortCircuit: vi.fn() }));

import SecuritySettingsPage from "../pages/SecuritySettingsPage";

/** api 层 getApiToken() 读取的键；改动它必须同步改 src/api/index.ts */
const CONTRACT_KEY = "api_token";

function renderPage() {
  return render(
    <AntdApp>
      <SecuritySettingsPage />
    </AntdApp>
  );
}

function findButton(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) =>
    (b.textContent || "").includes(text),
  );
  if (!btn) throw new Error(`找不到按钮：${text}`);
  return btn as HTMLButtonElement;
}

describe("访问凭据页面", () => {
  beforeEach(() => {
    localStorage.clear();
    h.activity.mockReset();
    // 默认模拟"后端已启用鉴权且当前无凭据" → 401
    h.activity.mockRejectedValue({ response: { status: 401 }, message: "未授权" });
  });

  it("保存后写入 localStorage 的键与 api 层读取的键一致", async () => {
    const { container } = renderPage();

    const input = container.querySelector('input[type="password"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    fireEvent.change(input, { target: { value: "tok-from-test" } });
    fireEvent.click(findButton(container, "保存并生效"));

    await waitFor(() => {
      expect(localStorage.getItem(CONTRACT_KEY)).toBe("tok-from-test");
    });
  });

  it("清除按钮会移除本地凭据", async () => {
    localStorage.setItem(CONTRACT_KEY, "old-token");
    const { container } = renderPage();

    // 初始值应回填自 localStorage
    const input = container.querySelector('input[type="password"]') as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("old-token"));

    fireEvent.click(findButton(container, "清除本地凭据"));
    await waitFor(() => {
      expect(localStorage.getItem(CONTRACT_KEY)).toBeNull();
    });
  });

  it("保存空白值等价于清除（不会把空串写进 localStorage 造成假配置）", async () => {
    localStorage.setItem(CONTRACT_KEY, "old-token");
    const { container } = renderPage();

    const input = container.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(findButton(container, "保存并生效"));

    await waitFor(() => {
      expect(localStorage.getItem(CONTRACT_KEY)).toBeNull();
    });
  });

  it("401 时展示可读的未通过提示（而不是静默无反应）", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("凭据不可用");
    });
    expect(container.textContent || "").toContain("401");
  });

  it("凭据可用时展示通过状态", async () => {
    h.activity.mockResolvedValue({ data: { items: [] } });
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("凭据可用");
    });
  });
});
