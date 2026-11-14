/**
 * 阶段 2 · omp 目录生成旁路「前端入口 + 生产前加固」源码级护栏（2026-11-12）。
 *
 * 锁定的真实缺口（不锁就会被后续顺手改回去）：
 *   1. 全局 axios 超时 60s，而 omp 后端单任务 300s + 重试 —— 前端若沿用全局值
 *      会先超时、后端仍整表写入 sections，用户重试即重复生成 / 覆盖；
 *   2. omp 旁路不注册到主链路任务表，content/outline 双 409 感知不到它 ——
 *      omp 生成期间目录 Tab 所有会写目录树的按钮必须一并禁用；
 *   3. 按钮必须受 omp_task_enabled 预显隐（feature-flags 端点）；
 *   4. 超时文案必须明确「后端可能仍在生成、勿重复点击」。
 */
import { describe, it, expect } from "vitest";
import apiSource from "../api/index.ts?raw";
import typesSource from "../types/api.ts?raw";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import outlineTabSource from "../pages/schemeWorkbench/tabs/OutlineTab.tsx?raw";

const apiSrc: string = apiSource;
const typesSrc: string = typesSource;
const pageSrc: string = pageSource;
const outlineSrc: string = outlineTabSource;

describe("omp 目录生成旁路 · 前端长超时", () => {
  it("OMP_OUTLINE_TIMEOUT_MS 显著大于全局 60s（默认 6 分钟）", () => {
    const global = apiSrc.match(/API_GLOBAL_TIMEOUT_MS\s*=\s*(\d+)/);
    const omp = apiSrc.match(/OMP_OUTLINE_TIMEOUT_MS\s*=\s*(\d+)/);
    expect(global, "缺少 API_GLOBAL_TIMEOUT_MS 常量").not.toBeNull();
    expect(omp, "缺少 OMP_OUTLINE_TIMEOUT_MS 常量").not.toBeNull();
    expect(Number(omp?.[1]), "omp 超时必须覆盖后端 300s + 重试").toBeGreaterThanOrEqual(300000);
    expect(Number(omp?.[1])).toBeGreaterThan(Number(global?.[1]));
  });

  it("generateOutlineOmp 显式传 timeout（不能落到全局 60s）", () => {
    expect(apiSrc).toContain("generate-outline-omp");
    expect(apiSrc).toContain("timeout: OMP_OUTLINE_TIMEOUT_MS");
  });

  it("契约类型齐备（请求体 / feature-flags 响应）", () => {
    expect(typesSrc).toContain("export interface GenerateOutlineOmpPayload");
    expect(typesSrc).toContain("export interface FeatureFlags");
    expect(typesSrc).toContain("omp_task_enabled: boolean");
  });
});

describe("omp 目录生成旁路 · 受控显隐与并发互斥", () => {
  it("按钮受 ompTaskEnabled 预显隐，页面从 feature-flags 下发开关", () => {
    expect(outlineSrc).toContain("{ompTaskEnabled && (");
    expect(outlineSrc).toContain("handleGenerateOutlineOmp");
    expect(apiSrc).toContain('getFeatureFlags: () => api.get<FeatureFlags>("/system/feature-flags")');
    expect(pageSrc).toContain(".getFeatureFlags()");
    expect(pageSrc).toContain("ompTaskEnabled={ompTaskEnabled}");
  });

  it("目录 Tab 定义 outlineBusy 且生成期间禁用全部目录写入口", () => {
    expect(outlineSrc).toContain("const outlineBusy = generating || !!generatingOmp");
    // 任何残留的 disabled={generating...} 都意味着某入口在 omp 生成期间仍可点击
    expect(outlineSrc, "目录写入口漏接 generatingOmp 互斥").not.toContain("disabled={generating");
    expect((outlineSrc.match(/disabled=\{outlineBusy/g) || []).length).toBeGreaterThanOrEqual(5);
    // SSE 专属的 loading / TaskControlButtons 判据不得被 outlineBusy 误改
    expect(outlineSrc).toContain("generating && genType === \"outline\"");
  });

  it("成功后刷新目录树，超时文案提示勿重复点击", () => {
    expect(pageSrc).toContain("await load()");
    expect(pageSrc).toContain("请勿重复点击");
    expect(pageSrc).toContain("OMP_OUTLINE_TIMEOUT_MS");
  });
});
