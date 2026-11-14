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
});
