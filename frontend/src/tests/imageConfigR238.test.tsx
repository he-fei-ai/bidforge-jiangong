// frontend/src/tests/imageConfigR238.test.tsx
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const API = RAW["api/index.ts"];

describe("R238 · 图像模型配置前端 API 封装", () => {
  it("imageApi 封装三个端点", () => {
    expect(API, "api/index.ts 应在 glob 覆盖内").toBeTruthy();
    expect(API!).toContain("imageApi");
    expect(API!).toContain('getConfig: () => api.get("/image-config")');
    expect(API!).toContain('saveConfig: (data) => api.put("/image-config", data)');
    expect(API!).toContain('testConnection: (data) => api.post("/image-config/test", data)');
  });
});

const PAGE = RAW["pages/AIImageConfigPage.tsx"];

describe("R238 · 图像模型配置页", () => {
  it("页面渲染三段式（配置/协同决策/模板）", () => {
    expect(PAGE, "AIImageConfigPage.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(PAGE!).toContain("图像模型配置");
    expect(PAGE!).toContain("AI 配图协同决策流");
    expect(PAGE!).toContain("文生图 Prompt 模板");
  });
  it("提供商下拉含 5 家 + DALL-E-3 回填 OpenAI 端点", () => {
    for (const v of ["DALL-E-3", "通义万相", "豆包 Seedream", "腾讯混元", "自定义厂商"]) {
      expect(PAGE!).toContain(v);
    }
    expect(PAGE!).toContain("https://api.openai.com/v1");
    expect(PAGE!).toContain("dall-e-3");
  });
  it("决策流三段 structured/illustration/none 与预算旋钮在场", () => {
    expect(PAGE!).toContain("structured");
    expect(PAGE!).toContain("illustration");
    expect(PAGE!).toContain("none");
    expect(PAGE!).toContain("max_ai_images");
  });
  it("load() 反推 display_provider 并回填隐藏 provider（R238 页面正确性修复）", () => {
    // 反推：以 DB 实际 provider 匹配 presets 取展示键；未命中 → "custom"
    expect(PAGE!).toMatch(/Object\.entries\(d\.presets\)\.find/);
    expect(PAGE!).toContain("provider: d.config.image_provider");
    // 硬编码 display_provider: "custom" 必须已移除
    expect(PAGE!).not.toContain('display_provider: "custom",');
  });
  it("load() 反推 custom/dalle3 共享 provider 消歧（P2 显示回归修复）", () => {
    // provider=custom 时不得仅按 provider 匹配（find 恒取首个 dalle3）→ 自定义厂商被误显示为 DALL-E-3。
    // 消歧：仅当 base_url+model 与 dalle3 预设完全一致才取 dalle3，否则取 custom。
    expect(PAGE!).toContain("d.config.image_provider === \"custom\"");
    expect(PAGE!).toContain('d.presets["dalle3"]');
    // 与 dalle3 预设 base_url/model 逐字段比较（完全匹配判据在场）
    expect(PAGE!).toMatch(/d\.config\.image_base_url === d3\.base_url/);
    expect(PAGE!).toMatch(/d\.config\.image_model === d3\.model/);
    expect(PAGE!).toContain('displayProvider = "dalle3"');
    expect(PAGE!).toContain('displayProvider = "custom"');
    // 非 custom provider 仍按 provider 匹配取键；未命中 → "custom"
    expect(PAGE!).toMatch(/Object\.entries\(d\.presets\)\.find\(\(\[, preset\]\) => preset\.provider === d\.config\.image_provider\)/);
  });
});

const APP = RAW["App.tsx"];
const LAYOUT = RAW["components/Layout.tsx"];

describe("R238 · 路由与菜单", () => {
  it("App.tsx 注册 /settings/ai-image 懒加载页", () => {
    expect(APP!, "App.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(APP!).toContain('path="/settings/ai-image"');
    expect(APP!).toContain("AIImageConfigPage");
  });
  it("Layout.tsx 侧边菜单含 图像模型配置 于 文本模型配置 之后", () => {
    expect(LAYOUT!, "components/Layout.tsx 应在 glob 覆盖内").toBeTruthy();
    const aiPos = LAYOUT!.indexOf("文本模型配置");
    const imgPos = LAYOUT!.indexOf("图像模型配置");
    expect(aiPos, "找不到文本模型配置菜单").toBeGreaterThan(-1);
    expect(imgPos, "找不到图像模型配置菜单").toBeGreaterThan(aiPos);
  });
});
