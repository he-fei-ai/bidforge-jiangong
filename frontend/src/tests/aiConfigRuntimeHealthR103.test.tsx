// @vitest-environment jsdom
/**
 * 文本模型配置 · 「运行时健康」前端契约测试（R103 · 2026-10-18）。
 *
 * 背景（R102 日志基线）：provider_factory 的熔断/配额冷却/实时成功率剔除都是
 * 进程内存态自愈机制——「已剔除实时成功率过低的候选」在生产日志刷了 209 条
 * WARNING，但「哪条配置被判死」在「文本模型配置」页完全看不见 ⇒ 用户只见
 * 「降级链 9→3」，只能靠翻日志。R103 把 /ai/configs/health 的
 * `summary.runtime_dead` 与 `items[].runtime_health` 接到 UI。
 *
 * 本文件锁定三件事：
 * 1. 体检汇总消息在 runtime_dead>0 时如实提示「已自动剔除，建议清理或禁用」；
 * 2. runtime_dead 为 0 / 字段缺失（旧后端）时不出现该提示（零误报、向后兼容）；
 * 3. 体检表格「运行时健康」列按状态分流展示：死配置标「成功率过低」、
 *    样本不足给「暂无样本」而不是假绿。
 *
 * 注意：页面用 useAntdMessageHub 包装 message——提示不弹原生 toast，
 * 而是写入全局消息中心，故断言读 getActivityItems() 而非 DOM 文本。
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
  setDisabledProviders: vi.fn(),
  configsHealth: vi.fn(),
  testConfig: vi.fn(),
  saveConfig: vi.fn(),
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
    setDisabledProviders: h.setDisabledProviders,
    configsHealth: h.configsHealth,
    testConfig: h.testConfig,
    saveConfig: h.saveConfig,
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
import { getActivityItems, clearActivity } from "../utils/activityCenter";

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
  request_mode: "normal",
  is_active: 1,
  priority: 0,
  remark: "",
};

const HEALTH_ITEM = (over: Record<string, unknown> = {}) => ({
  id: "c1",
  provider_name: "deepseek",
  model: "deepseek-chat",
  base_url: "https://api.deepseek.com/v1",
  plan: "pay_as_you_go",
  is_active: true,
  priority: 0,
  env: "",
  in_current_env: true,
  request_mode: "normal",
  request_mode_label: "普通请求",
  has_key: true,
  key_broken: false,
  key_hint: "abcd",
  no_url: false,
  disabled: false,
  network_ok: true,
  network_step: "ok",
  network_ip: "1.2.3.4",
  network_message: "",
  usable: true,
  ...over,
});

function healthResponse(items: unknown[], runtimeDead: number) {
  return {
    data: {
      items,
      summary: {
        total: items.length,
        active: 1,
        usable: items.length,
        no_key: 0,
        key_broken: 0,
        no_url: 0,
        out_of_env: 0,
        disabled: 0,
        network_ok: items.length,
        network_unreachable: 0,
        runtime_dead: runtimeDead,
      },
      active_env: "",
      disabled_providers: [],
      checked_at: "2026-10-18T00:00:00",
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  clearActivity();
  h.getConfig.mockResolvedValue({
    data: { items: [CONFIG_ROW], presets: {}, active_id: "c1", count: 1 },
  });
  h.getModels.mockResolvedValue({
    data: {
      providers: {
        deepseek: { label: "DeepSeek", models: [], default_model: "deepseek-chat", base_url: "https://api.deepseek.com/v1" },
      },
    },
  });
  h.health.mockResolvedValue({
    data: {
      status: "configured", base_url: "https://api.deepseek.com/v1",
      fallback_count: 1, concurrency: 4, live_concurrency: 4,
      degraded_providers: {}, request_mode: "normal", request_mode_label: "普通请求",
    },
  });
  h.stats.mockResolvedValue({ data: null });
  h.auditLogs.mockResolvedValue({ data: { items: [], total: 0, providers: [], actions: [] } });
  h.getSceneRoutes.mockResolvedValue({ data: { items: [], known_scenes: [], configured_count: 0 } });
  h.configAuditLogs.mockResolvedValue({ data: { items: [], total: 0 } });
  h.getEnv.mockResolvedValue({ data: { active_env: "", envs: [], routed: false } });
  h.getRuntime.mockResolvedValue({
    data: {
      active_env: "", disabled_providers: [], providers: ["deepseek"],
      configured_providers: ["deepseek"], effective_provider_count: 1,
      all_configured_disabled: false,
    },
  });
  h.setDisabledProviders.mockResolvedValue({
    data: { ok: true, disabled_providers: [], count: 0, warning: "" },
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

async function openHealthModal() {
  const { container } = renderPage();
  const btn = await waitFor(() => {
    const el = btnByText(container, "全部配置体检");
    if (!el) throw new Error("体检按钮未渲染");
    return el;
  });
  fireEvent.click(btn);
  // 等弹窗真正挂出（title 文本出现）再返回，后续断言才有对象
  await waitFor(() => {
    expect(document.body.textContent || "").toContain("全部配置体检");
  });
  return container;
}

describe("R103 · 运行时健康（配置体检）", () => {
  it("runtime_dead>0 时汇总消息如实提示「已自动剔除，建议清理或禁用」", async () => {
    h.configsHealth.mockResolvedValue(
      healthResponse(
        [HEALTH_ITEM({
          runtime_health: {
            samples: 12, success_rate: 0, dead: true,
            circuit_open: false, cooldown_remaining_s: 0, quota_cooldown_s: 0,
          },
        })],
        1,
      ),
    );
    await openHealthModal();
    await waitFor(() => {
      const texts = getActivityItems().map((i) => i.text).join(" | ");
      expect(texts).toContain("实时成功率过低");
      expect(texts).toContain("已自动剔除");
    });
    // 体检弹窗汇总标签也展示该计数（可一眼看出该清理几条）。
    // ⚠️ antd Modal 是 portal（挂在 document.body 下），必须查 body 而非 container。
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("成功率过低 1");
    });
  });

  it("runtime_dead=0 / 字段缺失（旧后端）时不出现剔除提示（零误报）", async () => {
    h.configsHealth.mockResolvedValue(healthResponse([HEALTH_ITEM()], 0));
    await openHealthModal();
    await waitFor(() => {
      const texts = getActivityItems().map((i) => i.text).join(" | ");
      expect(texts).toContain("均可用");
    });
    const texts = getActivityItems().map((i) => i.text).join(" | ");
    expect(texts).not.toContain("已自动剔除");
  });

  it("死配置行标「成功率过低」，样本不足行显示「暂无样本」不误标", async () => {
    h.configsHealth.mockResolvedValue(
      healthResponse(
        [
          HEALTH_ITEM({
            id: "dead",
            runtime_health: {
              samples: 12, success_rate: 0, dead: true,
              circuit_open: false, cooldown_remaining_s: 0, quota_cooldown_s: 0,
            },
          }),
          HEALTH_ITEM({
            id: "fresh",
            provider_name: "zhipu",
            runtime_health: {
              samples: 0, success_rate: null, dead: false,
              circuit_open: false, cooldown_remaining_s: 0, quota_cooldown_s: 0,
            },
          }),
        ],
        1,
      ),
    );
    await openHealthModal();
    // antd Modal 为 portal：表格单元格在 document.body 下
    await waitFor(() => {
      const cells = Array.from(document.body.querySelectorAll("td"));
      const texts = cells.map((c) => c.textContent || "");
      expect(texts.some((t) => t.includes("成功率过低"))).toBe(true);
      expect(texts.some((t) => t.includes("暂无样本"))).toBe(true);
    });
  });
});
