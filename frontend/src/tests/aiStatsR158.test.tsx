// @vitest-environment jsdom
/**
 * R158（2026-11-01）遗留收口 · /ai/stats 新键前端消费护栏。
 *
 * 锁定「数据链接上」的最后一环（R155-O 教训③：消费面在饿着）：
 *  F1 by_provider[].p95_duration_ok 渲染为「/ P95 xs」（缺失/0 时不显示）
 *  F2 by_error_class 结构化失败分类块（失败/门禁跳过分列展示）
 *  F3 旧后端响应无 by_error_class 键 → 区块不渲染（向后兼容）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";
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
if (!(window as any).getComputedStyle) {
  (window as any).getComputedStyle = () => ({ getPropertyValue: () => "" });
}

const h = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getModels: vi.fn(),
  health: vi.fn(),
  stats: vi.fn(),
  auditLogs: vi.fn(),
  getSceneRoutes: vi.fn(),
  configAuditLogs: vi.fn(),
  getEnv: vi.fn(),
  getRuntime: vi.fn(),
}));

vi.mock("../api", () => ({
  aiApi: {
    getConfig: h.getConfig,
    getModels: h.getModels,
    health: h.health,
    stats: h.stats,
    auditLogs: h.auditLogs,
    getSceneRoutes: h.getSceneRoutes,
    configAuditLogs: h.configAuditLogs,
    getEnv: h.getEnv,
    getRuntime: h.getRuntime,
    saveConfig: vi.fn(),
    clearConfigKey: vi.fn(),
    deleteConfig: vi.fn(),
    updateSceneRoute: vi.fn(),
    setEnv: vi.fn(),
    setDisabledProviders: vi.fn(),
    testConfig: vi.fn(),
    toggleConfig: vi.fn(),
    updateFallbackChain: vi.fn(),
    precheck: vi.fn(),
    precheckAll: vi.fn(),
    exportConfig: vi.fn(),
    importConfig: vi.fn(),
    cleanupAuditLogs: vi.fn(),
    fetchCustomModels: vi.fn(),
    rollbackConfig: vi.fn(),
  },
}));

// /ai/stats 模块级 TTL 缓存换成永不命中（与 aiConfigGapfix 同口径，防跨用例泄漏）
vi.mock("../utils/ttlCache", () => ({
  TtlCache: class {
    get() { return { hit: false }; }
    set() {}
    clear() {}
    delete() {}
  },
}));

import AIConfigPage from "../pages/AIConfigPage";

const ROW_ACTIVE = {
  id: "c1", provider_name: "deepseek", plan: "pay_as_you_go",
  api_key: "****abcd", key_hint: "abcd", has_key: true, key_broken: false,
  base_url: "https://api.deepseek.com/v1", model: "deepseek-chat",
  max_tokens: 8192, temperature: 0.7, timeout: 900, concurrency: 4,
  request_mode: "normal", is_active: 1, priority: 0, remark: "", env: "",
};

function statsFixture(over: Record<string, any> = {}) {
  return {
    data: {
      by_provider: [{
        provider_name: "deepseek", model: "deepseek-chat", calls: 121,
        prompt_tokens: 1000, completion_tokens: 200, cached_tokens: 0,
        success_count: 90, fail_count: 31, avg_duration_ok: 24.8,
        success_rate: 74.4, cache_hit_rate: null, skipped_count: 0,
        p95_duration_ok: 59.3,
      }],
      by_action: [], by_error: [],
      by_error_class: [
        { error_class: "rate_limit", label: "限流(429)",
          failed_calls: 12, skipped_calls: 30, total: 42 },
        { error_class: "timeout", label: "超时",
          failed_calls: 0, skipped_calls: 0, total: 0 },
      ],
      by_scene: [],
      summary: {
        total: 121, total_tokens: 1200, cached_tokens: 0, prompt_tokens: 1000,
        success_count: 90, failed_count: 31, skipped_count: 0, avg_duration: 20,
        success_rate: 74.4, cache_hit_rate: 0, days: 7,
      },
      daily: [],
      ...over,
    },
  };
}

function renderPage() {
  return render(
    <AntdApp>
      <AIConfigPage />
    </AntdApp>,
  );
}

async function waitLoaded(container: HTMLElement) {
  await waitFor(() => {
    expect(container.textContent || "").toContain("deepseek-chat");
  });
}

beforeEach(() => {
  h.getConfig.mockResolvedValue({ data: [ROW_ACTIVE] });
  h.getModels.mockResolvedValue({ data: {} });
  h.health.mockResolvedValue({ data: { results: [] } });
  h.auditLogs.mockResolvedValue({ data: { items: [], total: 0, providers: [], actions: [], scenes: [] } });
  h.getSceneRoutes.mockResolvedValue({ data: { items: [], configured_count: 0 } });
  h.configAuditLogs.mockResolvedValue({ data: { items: [], total: 0 } });
  h.getEnv.mockResolvedValue({ data: { active_env: "" } });
  h.getRuntime.mockResolvedValue({ data: { disabled_providers: [] } });
  h.stats.mockResolvedValue(statsFixture());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("R158 /ai/stats 新键消费", () => {
  it("F1 by_provider Tag 渲染 P95 耗时", async () => {
    const { container } = renderPage();
    await waitLoaded(container);
    await waitFor(() => {
      expect(container.textContent || "").toContain("P95 59.3s");
    });
  });

  it("F1b 旧响应无 p95_duration_ok → 不渲染 P95 段", async () => {
    const fx: any = statsFixture();
    delete fx.data.by_provider[0].p95_duration_ok;
    h.stats.mockResolvedValue(fx);
    const { container } = renderPage();
    await waitLoaded(container);
    await waitFor(() => {
      expect(container.textContent || "").toContain("成功率74.4%");
    });
    expect(container.textContent || "").not.toContain("P95");
  });

  it("F2 by_error_class 失败分类块：失败与门禁跳过分列", async () => {
    const { container } = renderPage();
    await waitLoaded(container);
    await waitFor(() => {
      expect(container.textContent || "").toContain("失败分类统计");
    });
    expect(container.textContent || "").toContain("限流(429): 失败 12 次");
    expect(container.textContent || "").toContain("另熔断/冷却跳过 30 次未发出");
    // 零失败类也要可见（failed=0 不隐藏，仅着色区分）
    expect(container.textContent || "").toContain("超时: 失败 0 次");
  });

  it("F3 旧响应无 by_error_class 键 → 区块不渲染", async () => {
    const fx: any = statsFixture();
    delete fx.data.by_error_class;
    h.stats.mockResolvedValue(fx);
    const { container } = renderPage();
    await waitLoaded(container);
    await waitFor(() => {
      expect(container.textContent || "").toContain("各供应商调用分布");
    });
    expect(container.textContent || "").not.toContain("失败分类统计");
  });
});
