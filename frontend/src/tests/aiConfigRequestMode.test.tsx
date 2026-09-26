// @vitest-environment jsdom
/**
 * 文本模型配置 · 「请求方式」（普通请求 / 流式请求）前端契约测试。
 *
 * 功能背景：每个厂商平台设置界面新增「请求方式」下拉。
 * 流式请求只影响**后端与厂商之间**的调用方式，应用侧仍等待完整结果后继续流程。
 *
 * 本文件锁定三件事（都是最容易在后续重构中被静默破坏的点）：
 * 1. 列表能展示每条配置的请求方式（否则用户改完看不出差别）；
 * 2. 编辑配置时表单**回显**已保存的请求方式（回显丢失 = 保存时被悄悄改回默认）；
 * 3. 「测试连接」把表单里的请求方式**真的传给了后端**
 *    （后端据此优先用该方式探测；不传则探测结论与实际链路不符）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, cleanup } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";

// jsdom 缺 matchMedia / ResizeObserver，antd 的 Table / Select 会用到
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

const h = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getModels: vi.fn(),
  health: vi.fn(),
  stats: vi.fn(),
  auditLogs: vi.fn(),
  testConfig: vi.fn(),
  saveConfig: vi.fn(),
  // ✅ 2026-09-23 新增端点（场景模型路由 / 配置变更审计）
  getSceneRoutes: vi.fn(),
  configAuditLogs: vi.fn(),
  getEnv: vi.fn(),
}));

vi.mock("../api", () => ({
  aiApi: {
    getConfig: h.getConfig,
    getModels: h.getModels,
    health: h.health,
    stats: h.stats,
    auditLogs: h.auditLogs,
    testConfig: h.testConfig,
    saveConfig: h.saveConfig,
    getSceneRoutes: h.getSceneRoutes,
    configAuditLogs: h.configAuditLogs,
    getEnv: h.getEnv,
    deleteConfig: vi.fn(),
    toggleConfig: vi.fn(),
    updateFallbackChain: vi.fn(),
    precheck: vi.fn(),
    precheckAll: vi.fn(),
    exportConfig: vi.fn(),
    importConfig: vi.fn(),
    cleanupAuditLogs: vi.fn(),
    fetchCustomModels: vi.fn(),
    clearConfigKey: vi.fn(),
    updateSceneRoute: vi.fn(),
    setEnv: vi.fn(),
    rollbackConfig: vi.fn(),
  },
}));

import AIConfigPage from "../pages/AIConfigPage";

const CONFIG_ROW = {
  id: "c1",
  provider_name: "deepseek",
  plan: "pay_as_you_go",
  api_key: "****abcd",
  key_hint: "abcd",
  has_key: true,
  key_broken: false,
  base_url: "https://api.deepseek.com/v1",
  model: "deepseek-chat",
  max_tokens: 8192,
  temperature: 0.7,
  timeout: 900,
  concurrency: 4,
  // 关键：这条配置用的是「流式请求」
  request_mode: "stream",
  is_active: 1,
  priority: 0,
  remark: "",
};

beforeEach(() => {
  vi.clearAllMocks();
  h.getConfig.mockResolvedValue({
    data: {
      items: [CONFIG_ROW],
      presets: {
        deepseek: {
          base_url: "https://api.deepseek.com/v1",
          model: "deepseek-chat",
          plans: {
            pay_as_you_go: { label: "按量计费", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
          },
        },
      },
      active_id: "c1",
      count: 1,
    },
  });
  h.getModels.mockResolvedValue({
    data: {
      providers: {
        deepseek: {
          label: "DeepSeek",
          models: [{ value: "deepseek-chat", label: "DeepSeek-V3", context: "64K", recommended: true }],
          description: "国产高性价比大模型",
          website: "https://platform.deepseek.com",
          pricing: "输入¥0.5/百万Token",
          default_model: "deepseek-chat",
          base_url: "https://api.deepseek.com/v1",
        },
      },
    },
  });
  h.health.mockResolvedValue({
    data: {
      status: "configured",
      base_url: "https://api.deepseek.com/v1",
      fallback_count: 0,
      concurrency: 4,
      live_concurrency: 4,
      degraded_providers: {},
      request_mode: "stream",
      request_mode_label: "流式请求",
    },
  });
  h.stats.mockResolvedValue({ data: null });
  h.auditLogs.mockResolvedValue({
    data: { items: [], total: 0, providers: [], actions: [] },
  });
  h.getSceneRoutes.mockResolvedValue({
    data: { items: [], known_scenes: [], configured_count: 0 },
  });
  h.configAuditLogs.mockResolvedValue({ data: { items: [], total: 0 } });
  h.getEnv.mockResolvedValue({ data: { active_env: "", envs: [], routed: false } });
  h.testConfig.mockResolvedValue({
    data: { ok: true, response: "ok", mode: "stream_probe" },
  });
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function renderPage() {
  return render(
    <AntdApp>
      <AIConfigPage />
    </AntdApp>,
  );
}

function btnByText(root: ParentNode, text: string): HTMLButtonElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return (
    (Array.from(root.querySelectorAll("button")).find((b) =>
      norm(b.textContent).includes(norm(text)),
    ) as HTMLButtonElement | undefined) ?? null
  );
}

describe("文本模型配置 · 请求方式", () => {
  it("列表展示每条配置的请求方式（流式配置显示「流式请求」）", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("deepseek-chat");
    });
    expect(container.textContent || "").toContain("流式请求");
    expect(container.textContent || "").toContain("请求方式");
  });

  it("运行时信息卡片回显当前使用配置的请求方式", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("api.deepseek.com");
    });
    expect(container.textContent || "").toContain("流式请求");
  });

  it("编辑配置时表单回显已保存的流式请求方式", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("deepseek-chat");
    });

    const editBtn = btnByText(container, "编辑");
    expect(editBtn).toBeTruthy();
    fireEvent.click(editBtn!);

    await waitFor(() => {
      expect(document.body.textContent || "").toContain("编辑供应商配置");
    });
    // 弹窗内的「请求方式」下拉必须选中流式选项 —— 回显丢失会让保存时静默改回默认
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("后端边收边拼");
    });
  });

  it("「测试连接」把表单里的请求方式传给后端（探测口径必须与真实链路一致）", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("deepseek-chat");
    });

    fireEvent.click(btnByText(container, "编辑")!);
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("编辑供应商配置");
    });

    fireEvent.click(btnByText(document.body, "测试连接")!);

    await waitFor(() => {
      expect(h.testConfig).toHaveBeenCalled();
    });
    const payload = h.testConfig.mock.calls[0][0];
    expect(payload.request_mode).toBe("stream");
    expect(payload.config_id).toBe("c1");
  });

  it("新增配置默认「普通请求」，并随表单一起提交（不被隐式切成流式）", async () => {
    const { container } = renderPage();
    await waitFor(() => {
      expect(container.textContent || "").toContain("deepseek-chat");
    });

    const addBtn = btnByText(container, "添加供应商");
    expect(addBtn).toBeTruthy();
    fireEvent.click(addBtn!);
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("添加供应商配置");
    });
    // 说明文案随选中项切换：新增时默认普通请求
    expect(document.body.textContent || "").toContain("普通请求：一次性发起请求");

    const pwd = document.querySelector('input[type="password"]') as HTMLInputElement;
    expect(pwd).toBeTruthy();
    fireEvent.change(pwd, { target: { value: "sk-test-key" } });

    h.saveConfig.mockResolvedValue({ data: { id: "new", ok: true, warning: "" } });
    fireEvent.click(btnByText(document.body, "保存配置")!);

    await waitFor(() => {
      expect(h.saveConfig).toHaveBeenCalled();
    });
    expect(h.saveConfig.mock.calls[0][0].request_mode).toBe("normal");
  });
});
