// @vitest-environment jsdom
/**
 * 文本模型配置 · 缺口修复回归（2026-09-25）。
 *
 * 锁定「数据链接上 + 静默失效可见」两件事：
 *  F1 用量日志可按业务场景下钻（场景筛选 + 场景列中文化 + 聚合标签点击跳转）
 *  F2 跨环境的场景模型路由在界面上显式标注「跨环境·暂不生效」
 *  F3 保存场景路由时后端返回的 warning 必须呈现（而不是照旧报「成功」）
 *  F4 删除配置时提示被连带解除的场景路由条数
 *  F5 「当前生效环境」值损坏时给出可读原因 + 一键恢复为通用环境
 *     （后端此时不再 500，而是回传 env_error / health.status=env_corrupt）
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
if (!(window as any).getComputedStyle) {
  (window as any).getComputedStyle = () => ({ getPropertyValue: () => "" });
}

const h = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getModels: vi.fn(),
  health: vi.fn(),
  stats: vi.fn(),
  auditLogs: vi.fn(),
  deleteConfig: vi.fn(),
  getSceneRoutes: vi.fn(),
  updateSceneRoute: vi.fn(),
  configAuditLogs: vi.fn(),
  getEnv: vi.fn(),
  setEnv: vi.fn(),
  getRuntime: vi.fn(),
}));

vi.mock("../api", () => ({
  aiApi: {
    getConfig: h.getConfig,
    getModels: h.getModels,
    health: h.health,
    stats: h.stats,
    auditLogs: h.auditLogs,
    deleteConfig: h.deleteConfig,
    getSceneRoutes: h.getSceneRoutes,
    updateSceneRoute: h.updateSceneRoute,
    configAuditLogs: h.configAuditLogs,
    getEnv: h.getEnv,
    setEnv: h.setEnv,
    getRuntime: h.getRuntime,
    saveConfig: vi.fn(),
    clearConfigKey: vi.fn(),
    rollbackConfig: vi.fn(),
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
  },
}));

// /ai/stats 走模块级 30s TTL 缓存（性能优化项）。它是**模块级单例**，会把前一个
// 用例缓存的 stats=null 泄漏给后面的用例（本文件的 by_scene 用例因此永远拿不到数据）。
// 缓存本身由 perfOptimizations 用例专门覆盖，这里换成「永不命中」的空实现，
// 保证每个用例渲染的都是自己 mock 的数据。
vi.mock("../utils/ttlCache", () => ({
  TtlCache: class {
    get() { return { hit: false }; }
    set() {}
    clear() {}
    delete() {}
  },
}));

import AIConfigPage from "../pages/AIConfigPage";
import { getActivityItems, clearActivity } from "../utils/activityCenter";

const ROW_ACTIVE = {
  id: "c1", provider_name: "deepseek", plan: "pay_as_you_go",
  api_key: "****abcd", key_hint: "abcd", has_key: true, key_broken: false,
  base_url: "https://api.deepseek.com/v1", model: "deepseek-chat",
  max_tokens: 8192, temperature: 0.7, timeout: 900, concurrency: 4,
  request_mode: "normal", is_active: 1, priority: 0, remark: "", env: "",
};
const ROW_SPARE = {
  ...ROW_ACTIVE,
  id: "c2", provider_name: "zhipu", model: "glm-4-flash",
  is_active: 0, priority: 1, has_key: true, env: "dev",
};

const PRESETS = {
  deepseek: {
    base_url: "https://api.deepseek.com/v1", model: "deepseek-chat",
    plans: { pay_as_you_go: { label: "按量计费", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" } },
  },
  zhipu: {
    base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash",
    plans: { pay_as_you_go: { label: "按量计费", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" } },
  },
};

const SCENE_ITEMS = (over: Record<string, any> = {}) => ({
  items: [
    {
      scene: "content_draft", label: "正文生成", config_id: "c2",
      provider_name: "zhipu", model: "glm-4-flash", missing: false,
      config_env: "dev", env_mismatch: false, ...over,
    },
    {
      scene: "facts_extract", label: "全局事实提取", config_id: "",
      provider_name: "", model: "", missing: false,
      config_env: "", env_mismatch: false,
    },
  ],
  known_scenes: [
    { value: "content_draft", label: "正文生成" },
    { value: "facts_extract", label: "全局事实提取" },
  ],
  configured_count: 1,
  active_env: "",
});

const AUDIT_LOGS = {
  items: [
    {
      id: "l1", provider_name: "deepseek", model: "deepseek-chat", action: "chat",
      scene: "content_draft", prompt_tokens: 100, completion_tokens: 50,
      cached_tokens: 0, duration: 1.2, success: 1, error: "",
      created_at: "2026-09-25T10:00:00",
    },
    {
      id: "l2", provider_name: "zhipu", model: "glm-4-flash", action: "chat",
      scene: "", prompt_tokens: 10, completion_tokens: 5,
      cached_tokens: 0, duration: 0.5, success: 0, error: "upstream 500",
      created_at: "2026-09-25T10:01:00",
    },
  ],
  total: 2, limit: 20, offset: 0,
  providers: ["deepseek", "zhipu"], actions: ["chat"],
  scenes: ["content_draft"],
};

beforeEach(() => {
  vi.clearAllMocks();
  clearActivity();
  h.getConfig.mockResolvedValue({
    data: {
      items: [ROW_ACTIVE, ROW_SPARE], presets: PRESETS, active_id: "c1",
      count: 2, active_env: "", envs: [], env_error: "",
    },
  });
  h.getModels.mockResolvedValue({
    data: {
      providers: {
        deepseek: { label: "DeepSeek", models: [], default_model: "deepseek-chat", base_url: "https://api.deepseek.com/v1" },
        zhipu: { label: "智谱 AI", models: [], default_model: "glm-4-flash", base_url: "https://open.bigmodel.cn/api/paas/v4" },
      },
    },
  });
  h.health.mockResolvedValue({
    data: {
      status: "configured", base_url: "https://api.deepseek.com/v1",
      fallback_count: 1, concurrency: 4, live_concurrency: 4,
      degraded_providers: {}, request_mode: "normal", request_mode_label: "普通请求",
      env_error: "",
    },
  });
  h.stats.mockResolvedValue({ data: null });
  h.auditLogs.mockResolvedValue({ data: AUDIT_LOGS });
  h.deleteConfig.mockResolvedValue({ data: { ok: true, warning: "", cleared_scene_routes: 0 } });
  h.getSceneRoutes.mockResolvedValue({ data: SCENE_ITEMS() });
  h.updateSceneRoute.mockResolvedValue({
    data: { ok: true, scene: "content_draft", config_id: "c2", warning: "" },
  });
  h.configAuditLogs.mockResolvedValue({ data: { items: [], total: 0, actions: [] } });
  h.getEnv.mockResolvedValue({
    data: { active_env: "", envs: ["dev"], routed: false, env_error: "" },
  });
  h.setEnv.mockResolvedValue({
    data: { ok: true, active_env: "", hint: "已恢复为通用环境：不做任何环境过滤" },
  });
  h.getRuntime.mockResolvedValue({
    data: {
      active_env: "", env_error: "", disabled_providers: [],
      providers: ["deepseek", "zhipu"], configured_providers: ["deepseek", "zhipu"],
      effective_provider_count: 2, all_configured_disabled: false,
    },
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

function btnByText(root: ParentNode, text: string): HTMLElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return (
    (Array.from(root.querySelectorAll("button")).find((b) =>
      norm(b.textContent).includes(norm(text)),
    ) as HTMLButtonElement | undefined) ?? null
  );
}

async function waitLoaded(container: HTMLElement) {
  await waitFor(() => {
    expect(container.textContent || "").toContain("deepseek-chat");
  });
}

/** 主 Tab 受控，未激活的 Tab 内容不在 DOM 里，需先点开 */
async function openTab(container: HTMLElement, label: string) {
  const tab = Array.from(container.querySelectorAll(".ant-tabs-tab")).find(
    (t) => (t.textContent || "").includes(label),
  ) as HTMLElement | undefined;
  expect(tab).toBeTruthy();
  fireEvent.click(tab!.querySelector(".ant-tabs-tab-btn") || tab!);
  await waitFor(() => {
    expect(container.textContent || "").toContain(label);
  });
}

const activityText = () => getActivityItems().map((i) => i.text).join(" | ");

describe("文本模型配置 · 缺口修复（2026-09-25）", () => {
  // ===== F5：环境值损坏必须可见且可恢复（此前是「加载失败」+ 无从下手） =====
  it("环境值损坏：给出可读原因与「恢复为通用环境」入口，点击后写回空环境", async () => {
    h.health.mockResolvedValue({
      data: {
        status: "env_corrupt", fallback_count: 0, degraded_providers: {},
        env_error: "运行时环境配置损坏，请重新选择环境",
        hint: "运行时环境配置损坏，请重新选择环境；请在「运行时设置」把当前生效环境清空（恢复通用环境）后重试",
      },
    });
    const { container } = renderPage();
    await waitLoaded(container);

    expect(container.textContent || "").toContain("当前生效环境」的值非法");
    // 绝不能被误报成「尚未启用任何文本模型配置」
    expect(container.textContent || "").not.toContain("尚未启用任何文本模型配置");

    const btn = btnByText(container, "恢复为通用环境");
    expect(btn).toBeTruthy();
    fireEvent.click(btn!);
    await waitFor(() => expect(h.setEnv).toHaveBeenCalledWith(""));
  });

  it("健康状态标签把 env_corrupt 显示为「环境值异常」而不是「未配置」", async () => {
    h.health.mockResolvedValue({
      data: {
        status: "env_corrupt", fallback_count: 0, degraded_providers: {},
        env_error: "运行时环境配置损坏", hint: "请清空环境",
      },
    });
    const { container } = renderPage();
    await waitLoaded(container);
    expect(container.textContent || "").toContain("环境值异常");
    expect(container.textContent || "").not.toContain("连接状态未配置");
  });

  // ===== F2：跨环境路由必须在界面上看得见（此前只在后端日志里 WARNING） =====
  it("跨环境的场景路由标注「跨环境·暂不生效」", async () => {
    h.getSceneRoutes.mockResolvedValue({
      data: { ...SCENE_ITEMS({ env_mismatch: true }), active_env: "prod" },
    });
    const { container } = renderPage();
    await waitLoaded(container);

    await waitFor(() => {
      expect(container.textContent || "").toContain("跨环境·暂不生效");
    });
    // 说明文案要点明以哪个环境为判定基准
    expect(container.textContent || "").toContain("当前生效环境为");
  });

  it("同环境的路由不标红（避免误报）", async () => {
    const { container } = renderPage();
    await waitLoaded(container);
    await waitFor(() => {
      expect(container.textContent || "").toContain("场景模型路由");
    });
    expect(container.textContent || "").not.toContain("跨环境·暂不生效");
  });

  // ===== F3：保存成功但不生效时必须说清楚（不再照旧弹「成功」） =====
  it("保存场景路由后返回 warning 时，提示的是 warning 而不是「已指定」", async () => {
    h.getSceneRoutes.mockResolvedValue({
      data: { ...SCENE_ITEMS({ env_mismatch: true }), active_env: "prod" },
    });
    h.updateSceneRoute.mockResolvedValue({
      data: {
        ok: true, scene: "content_draft", config_id: "c2",
        warning: "该配置属于环境「dev」，与当前生效环境「prod」不同，暂不生效",
      },
    });
    const { container } = renderPage();
    await waitLoaded(container);

    const card = Array.from(container.querySelectorAll(".ant-card")).find(
      (c) => (c.textContent || "").includes("场景模型路由"),
    ) as HTMLElement;
    expect(card).toBeTruthy();
    const selector = card.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    await waitFor(() => {
      expect(document.querySelectorAll(".ant-select-item-option").length)
        .toBeGreaterThan(0);
    });
    const follow = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((o) => (o.textContent || "").includes("跟随")) as HTMLElement;
    expect(follow).toBeTruthy();
    fireEvent.click(follow);

    await waitFor(() => expect(h.updateSceneRoute).toHaveBeenCalled());
    await waitFor(() => {
      expect(activityText()).toContain("暂不生效");
    });
    expect(activityText()).not.toContain("已为该场景指定模型");
  });

  // ===== F4：删除配置要交代被连带解除的场景路由 =====
  it("删除配置后提示连带解除的场景路由条数", async () => {
    h.deleteConfig.mockResolvedValue({
      data: { ok: true, warning: "", cleared_scene_routes: 2 },
    });
    const { container } = renderPage();
    await waitLoaded(container);

    const row = Array.from(container.querySelectorAll(".ant-table-row")).find(
      (r) => (r.textContent || "").includes("glm-4-flash"),
    ) as HTMLElement;
    expect(row).toBeTruthy();
    // 删除按钮是纯图标按钮（无文字），只能按图标定位
    const del = (row.querySelector(".anticon-delete")?.closest("button")
      ?? null) as HTMLElement | null;
    expect(del).toBeTruthy();
    fireEvent.click(del!);

    await waitFor(() => {
      expect(document.body.textContent || "").toContain("确认删除");
    });
    const confirmBox = document.querySelector(".ant-modal-confirm") as HTMLElement;
    expect(confirmBox).toBeTruthy();
    fireEvent.click(btnByText(confirmBox, "删除")!);

    await waitFor(() => expect(h.deleteConfig).toHaveBeenCalledWith("c2"));
    await waitFor(() => {
      expect(activityText()).toContain("2 条场景模型路由");
    });
  });

  // ===== F1：用量日志按场景下钻（此前 stats 有 by_scene 却只能按供应商筛） =====
  it("用量日志显示场景列（中文名，历史无埋点显示「未标记」）", async () => {
    const { container } = renderPage();
    await waitLoaded(container);
    await openTab(container, "用量日志");

    await waitFor(() => {
      expect(container.textContent || "").toContain("正文生成");
    });
    expect(container.textContent || "").toContain("未标记");
  });

  it("选择「场景」筛选后按 scene 重新请求审计明细", async () => {
    const { container } = renderPage();
    await waitLoaded(container);
    await openTab(container, "用量日志");

    const sceneSelect = Array.from(
      container.querySelectorAll(".ant-select-filter-item, .ant-select"),
    ).find((el) => (el.textContent || "").includes("全部场景")
      || (el.querySelector(".ant-select-selection-placeholder")?.textContent || "")
        .includes("全部场景")) as HTMLElement;
    expect(sceneSelect).toBeTruthy();
    fireEvent.mouseDown(sceneSelect.querySelector(".ant-select-selector") as HTMLElement);

    await waitFor(() => {
      const opts = Array.from(document.querySelectorAll(".ant-select-item-option"))
        .map((o) => o.textContent || "");
      expect(opts.some((t) => t.includes("正文生成"))).toBe(true);
    });
    const opt = Array.from(document.querySelectorAll(".ant-select-item-option"))
      .find((o) => (o.textContent || "").includes("正文生成")) as HTMLElement;
    fireEvent.click(opt);

    await waitFor(() => {
      expect(h.auditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ scene: "content_draft" }),
      );
    });
  });

  it("「按业务场景」聚合标签可点击下钻到该场景明细", async () => {
    h.stats.mockResolvedValue({
      data: {
        by_provider: [], by_action: [], by_error: [],
        by_scene: [
          { scene: "content_draft", calls: 10, success_count: 4, tokens: 900 },
          { scene: "facts_extract", calls: 3, success_count: 3, tokens: 120 },
        ],
        summary: {
          total: 13, total_tokens: 1020, cached_tokens: 0, prompt_tokens: 600,
          success_count: 7, failed_count: 6, skipped_count: 0, avg_duration: 1.5,
          success_rate: 53.8, cache_hit_rate: 0, days: 30,
        },
        daily: [],
      },
    });
    const { container } = renderPage();
    await waitLoaded(container);

    // 场景码必须显示为中文名，成功率着色便于定位坏场景
    await waitFor(() => {
      expect(container.textContent || "").toContain("按业务场景");
    });
    expect(container.textContent || "").toContain("正文生成: 10次");
    expect(container.textContent || "").toContain("全局事实提取: 3次");

    const tag = Array.from(container.querySelectorAll(".ant-tag")).find(
      (t) => (t.textContent || "").includes("正文生成: 10次"),
    ) as HTMLElement;
    expect(tag).toBeTruthy();
    fireEvent.click(tag);

    await waitFor(() => {
      expect(h.auditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ scene: "content_draft" }),
      );
    });
    // 跳转后应落在「用量日志」Tab，并回填了场景筛选控件
    await waitFor(() => {
      expect(container.textContent || "").toContain("未标记");
    });
  });
});