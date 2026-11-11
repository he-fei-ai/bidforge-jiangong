/**
 * R214（2026-11-10）· 目录生成模块遗留项收口 —— 前端加固护栏。
 *
 * 覆盖 FIX-5（并行负载下的环境性超时假红）与 FIX-3（目录模块行尾归一）的
 * 「不得回流」棘轮；源码读取一律走 vite `?raw`，不执行被读模块。
 */
import { describe, expect, it } from "vitest";
import pageTestSrc from "./schemeWorkbench.test.tsx?raw";
import outlineTreeSrc from "../pages/schemeWorkbench/outlineTreeModel.ts?raw";
import outlineTabSrc from "./outlineTab.test.tsx?raw";
import vitestCfgSrc from "../../vitest.config.ts?raw";

describe("R214 · 页面级超时收敛在单文件", () => {
  it("重测试文件显式放宽 testTimeout / hookTimeout 到 120s", () => {
    expect(pageTestSrc).toContain("vi.setConfig({ testTimeout: 120000, hookTimeout: 120000 })");
    expect(pageTestSrc).not.toContain("vi.setConfig({ testTimeout: 60000 })");
  });

  it("全局 config 保持 60s/30s，其余 92 个文件反馈速度不变", () => {
    expect(vitestCfgSrc).toContain("testTimeout: 60000");
    expect(vitestCfgSrc).toContain("hookTimeout: 30000");
    expect(vitestCfgSrc).toContain("maxWorkers: 8");
  });
});

describe("R214 · 目录模块行尾棘轮（.gitattributes eol=lf）", () => {
  it("归一后的目录模块源码不含 CR / BOM / U+FFFD", () => {
    for (const src of [outlineTreeSrc, outlineTabSrc, vitestCfgSrc]) {
      expect(src).not.toContain("\r");
      expect(src).not.toContain("\ufeff");
      expect(src).not.toContain("\ufffd");
    }
  });
});
