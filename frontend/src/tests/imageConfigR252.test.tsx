// frontend/src/tests/imageConfigR252.test.tsx
// R252 · 图像模型配置增强：多模型 CRUD / 场景路由 / 参数模板 / 密钥脱敏 / 热更新。
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const API = RAW["api/index.ts"];
const PAGE = RAW["pages/AIImageConfigPage.tsx"];
const TYPES = RAW["types/imageConfig.ts"];

describe("R252 · 图像模型配置前端 API 封装", () => {
  it("imageApi 新增多模型 / 场景路由 / 审计端点", () => {
    expect(API!, "api/index.ts 应在 glob 覆盖内").toBeTruthy();
    expect(API!).toContain('listModels: () => api.get("/image-models")');
    expect(API!).toContain('createModel: (data) => api.post("/image-models", data)');
    expect(API!).toContain('updateModel: (id, data) => api.put(`/image-models/${id}`, data)');
    expect(API!).toContain('setDefaultModel: (id) => api.post(`/image-models/${id}/set-default`)');
    expect(API!).toContain('listSceneRoutes: () => api.get("/image-models/scene-routes")');
    expect(API!).toContain('saveSceneRoutes: (routes) => api.put("/image-models/scene-routes", { routes })');
    expect(API!).toContain('listAuditLogs: (limit = 50, offset = 0) =>');
  });
});

describe("R252 · 图像模型配置类型契约", () => {
  it("类型含多模型 / 场景路由 / 审计 / 参数模板字段", () => {
    expect(TYPES!, "types/imageConfig.ts 应在 glob 覆盖内").toBeTruthy();
    for (const marker of [
      "ImageModelListResponse", "ImageModelSavePayload", "ImageSceneRoutesResponse",
      "ImageAuditResponse", "key_hint", "key_broken", "default_params", "config_version",
    ]) {
      expect(TYPES!).toContain(marker);
    }
  });
});

describe("R252 · 图像模型配置页增强", () => {
  it("页面含多模型管理 / 场景路由 / 参数模板 / 测试连接", () => {
    expect(PAGE!, "AIImageConfigPage.tsx 应在 glob 覆盖内").toBeTruthy();
    for (const marker of [
      "多模型管理", "场景模型路由", "参数模板", "测试连接",
      "listModels", "saveSceneRoutes", "default_params", "setDefaultModel", "deleteModel",
    ]) {
      expect(PAGE!).toContain(marker);
    }
  });
  it("页面新建载荷封装 default_params（分辨率/采样/CFG/负面词/风格）", () => {
    expect(PAGE!).toMatch(/default_params:\s*\{/);
    expect(PAGE!).toContain("resolution: v.resolution");
    expect(PAGE!).toContain("steps: Number(v.steps");
    expect(PAGE!).toContain("negative_prompt: v.negative_prompt");
    expect(PAGE!).toContain("style: v.style");
  });
  it("页面密钥三态展示（key_hint / key_broken / 密钥失效）", () => {
    expect(PAGE!).toContain("key_broken");
    expect(PAGE!).toContain("key_hint");
    expect(PAGE!).toContain("密钥失效");
  });
});
