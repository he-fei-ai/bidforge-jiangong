/**
 * R205 · 六模块跨模块数据链收口（前端护栏，2026-11-09）。
 *
 * 行为级：parseCacheStatus（导出缓存降级信号）；
 * 源码棘轮：目录 degraded 终态 / 事实链终态刷新 / warning 与 error 载荷 /
 *           word_adjust 阶段 / 导出缓存状态 / 治理端点路径。
 */
import { describe, it, expect } from "vitest";
import { parseCacheStatus } from "../utils/exportResponse";
import outlineSrc from "../pages/schemeWorkbench/useOutlineGeneration.tsx?raw";
import factsSrc from "../pages/schemeWorkbench/useFactsGeneration.ts?raw";
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import exportRunSrc from "../pages/schemeWorkbench/useExportRun.ts?raw";
import responseSrc from "../utils/exportResponse.ts?raw";
import apiSrc from "../api/index.ts?raw";
import checkpointSrc from "../types/checkpointSchema.ts?raw";

describe("R205 · X-Cache-Status 缓存降级", () => {
  it("degraded 产出中文提示", () => {
    const info = parseCacheStatus({ "x-cache-status": "degraded" });
    expect(info?.degraded, "degraded 必须被识别").toBe(true);
    expect(info?.message).toContain("缓存降级");
  });

  it("大小写不敏感且 hit/miss 不误报", () => {
    expect(parseCacheStatus({ "X-Cache-Status": "DEGRADED" })?.degraded).toBe(true);
    expect(parseCacheStatus({ "X-Cache-Status": "HIT" })?.degraded).toBe(false);
    expect(parseCacheStatus({ "x-cache-status": "miss" })?.degraded).toBe(false);
  });

  it("头缺失 / 空值 / 非字符串返回 null", () => {
    expect(parseCacheStatus({})).toBeNull();
    expect(parseCacheStatus(null)).toBeNull();
    expect(parseCacheStatus({ "x-cache-status": "  " })).toBeNull();
    expect(parseCacheStatus({ "x-cache-status": 3 })).toBeNull();
  });
});

describe("R205 · 目录 SSE degraded 终态", () => {
  it("在线循环必须把 degraded 与 completed 一起收口", () => {
    expect(outlineSrc).toContain('evt.event === "degraded"');
    expect(outlineSrc).toContain('evt.event === "completed" || _isDegradedOutline');
  });

  it("降级完成仍要 flush / 落目录树 / 弹一级闸门 / break", () => {
    const from = outlineSrc.indexOf('evt.event === "completed" || _isDegradedOutline');
    const to = outlineSrc.indexOf('evt.event === "stopped"', from);
    const branch = outlineSrc.slice(from, to);
    expect(branch, "终态分支必须存在").not.toBe("");
    expect(branch).toContain("batcher.flushNow()");
    expect(branch).toContain("setTree(buildTree(evt.outline))");
    expect(branch).toContain("openOutlineGate(evt.outline)");
    expect(branch).toContain("break;");
  });

  it("stopped 分支消费后端 message（检查点失败原因）", () => {
    expect(outlineSrc).toContain('setProgressMsg(evt.message || "已停止")');
  });
});

describe("R205 · 事实链终态数据同步", () => {
  it("stopped 与 error 都刷新事实列表", () => {
    const errFrom = factsSrc.indexOf('evt.event === "error"');
    const warnFrom = factsSrc.indexOf('evt.event === "warning"', errFrom);
    expect(factsSrc.slice(errFrom, warnFrom), "error 分支须刷新已落库增量").toContain("loadFacts();");

    const stoppedFrom = factsSrc.indexOf('evt.event === "stopped"');
    const progressFrom = factsSrc.indexOf('evt.event === "progress"', stoppedFrom);
    const stoppedBranch = factsSrc.slice(stoppedFrom, progressFrom);
    expect(stoppedBranch, "stopped 分支须刷新已落库增量").toContain("loadFacts();");
    expect(stoppedBranch).toContain("evt.message ||");
  });
});

describe("R205 · 正文 SSE 载荷消费", () => {
  it("warning 兼容只带 warnings 数组的事实链载荷", () => {
    expect(pageSrc).toContain("_warnParts");
    expect(pageSrc).toContain("Array.isArray(evt.warnings)");
  });

  it("error 终态消费标准校验与事实修复汇总", () => {
    expect(pageSrc).toContain("_errStdHint");
    expect(pageSrc).toContain("_errFaHint");
  });

  it("word_adjust 四个阶段事件都有消费分支", () => {
    for (const key of [
      "word_adjust_start",
      "word_adjust_batch_start",
      "word_adjust_batch_done",
      "word_adjust_done",
    ]) {
      expect(pageSrc, `缺少 ${key} 分支`).toContain(`evt.event === "${key}"`);
    }
  });
});

describe("R205 · 导出与契约同步", () => {
  it("导出成功分支消费缓存降级状态", () => {
    expect(exportRunSrc).toContain("parseCacheStatus(resp.headers)");
    expect(responseSrc).toContain("export function parseCacheStatus");
  });

  it("治理读取端点前端调用路径未漂移", () => {
    expect(apiSrc).toContain('api.get<GovernanceSettings>("/system/governance")');
  });

  it("生成的 checkpoint 契约含三个新增键", () => {
    expect(checkpointSrc).toContain("processed?: number;");
    expect(checkpointSrc).toContain("charts?: number;");
    expect(checkpointSrc).toContain("fact_mapping?: unknown[];");
  });
});
