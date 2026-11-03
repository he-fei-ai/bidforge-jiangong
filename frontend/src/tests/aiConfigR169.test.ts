/**
 * R169（2026-11-03 · 跨模块审计收口）前端源码锁 ×2：
 *
 * 1. 测试连接超时对齐后端最坏探测路径 —— 后端 /ai/config/test 双探测
 *    （流式 120s + 普通 120s + DNS/TCP 预检 ~5s ≈ 245s），旧前端 axios
 *    120000ms 先行中止，后端分类好的境外延迟建议永远到不了用户
 *    （只见泛化的 "timeout of 120000ms exceeded"）。
 * 2. duplicate_hint 前端消费 —— 后端 R111 落地的同三元组去重提示，
 *    旧前端类型缺字段 + handleSave 零消费，提示在 UI 永远不可达
 *    （「服务层就绪、消费端断线」同族缺口）。
 *
 * 判据沿用本仓既有范式（stopControlR106）：import.meta.glob 读源码文本，
 * 不引 node:fs。
 */
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const API = RAW["api/index.ts"];
const PAGE = RAW["pages/AIConfigPage.tsx"];
const TYPES = RAW["types/aiConfig.ts"];

describe("R169 · 文本模型配置模块前端接线锁", () => {
  it("测试连接超时 ≥ 后端最坏探测路径（/ai/config/test → 260000ms）", () => {
    expect(API, "api/index.ts 应在 glob 覆盖内").toBeTruthy();
    const pos = API!.indexOf('"/ai/config/test"');
    expect(pos, "找不到 /ai/config/test 调用点").toBeGreaterThan(-1);
    const window = API!.slice(pos, pos + 300);
    expect(window).toContain("timeout: 260000");
    expect(window).not.toContain("timeout: 120000");
  });

  it("handleSave 消费 duplicate_hint（去重提示 UI 可达）", () => {
    expect(PAGE, "AIConfigPage.tsx 应在 glob 覆盖内").toBeTruthy();
    const pos = PAGE!.indexOf("const handleSave = async");
    expect(pos, "找不到 handleSave").toBeGreaterThan(-1);
    const fn = PAGE!.slice(pos, pos + 1600);
    expect(fn).toContain("data?.duplicate_hint");
    expect(fn).toContain("msg.info(data.duplicate_hint)");
  });

  it("AIConfigSaveResponse 契约含 duplicate_hint 可缺省字段", () => {
    expect(TYPES, "types/aiConfig.ts 应在 glob 覆盖内").toBeTruthy();
    const pos = TYPES!.indexOf("interface AIConfigSaveResponse");
    expect(pos).toBeGreaterThan(-1);
    const iface = TYPES!.slice(pos, pos + 600);
    expect(iface).toContain("duplicate_hint?: string");
  });
});
