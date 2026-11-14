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
