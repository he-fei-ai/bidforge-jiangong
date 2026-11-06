// @vitest-environment jsdom
/**
 * R181（2026-11-06）前端遗留项回归。
 *
 * 锁定三件「后端已回传、前端此前未消费」的可见信号：
 *  * 导出配置时 `scene_routes_ok=false` 必须给出告警；
 *  * 导入预演必须提示「其中 N 条导入后仍为停用」；
 *  * 正文保存必须提示代码围栏自动修复（含疑似吞行）。
 *
 * 变异判别：把对应文案函数改回返回空串 / 旧的 `str()` 口径，用例立即变红。
 */
import { describe, expect, it } from "vitest";
import {
  exportSceneRoutesNotice,
  importDryRunDisabledNotice,
  sectionSaveFenceNotice,
} from "../utils/diagnosticsText";

describe("R181 前端诊断字段接线", () => {
  it("scene_routes_ok=false 时给出可见告警", () => {
    expect(exportSceneRoutesNotice(false)).toContain("场景模型路由读取失败");
    expect(exportSceneRoutesNotice(false)).toContain("请稍后重新导出");
  });

  it("scene_routes_ok=true / undefined 时不打扰用户", () => {
    expect(exportSceneRoutesNotice(true)).toBe("");
    expect(exportSceneRoutesNotice(undefined)).toBe("");
  });

  it("导入预演提示停用条数（skip 不计入）", () => {
    const planned = [
      { action: "create", enabled: 0 },
      { action: "update", enabled: 0 },
      { action: "skip", enabled: 0 },
      { action: "create", enabled: 1 },
    ];
    expect(importDryRunDisabledNotice(planned)).toContain("其中 2 条将保持「停用」状态");
    expect(importDryRunDisabledNotice([{ action: "create", enabled: 1 }])).toBe("");
    expect(importDryRunDisabledNotice([])).toBe("");
  });

  it("围栏修复计数转成可见提示（含吞行风险）", () => {
    expect(sectionSaveFenceNotice({ fence_fixed: 2, fence_swallowed_lines: 5 })).toContain(
      "已自动补齐未闭合代码围栏 2 处",
    );
    expect(sectionSaveFenceNotice({ fence_fixed: 2, fence_swallowed_lines: 5 })).toContain(
      "约 5 行疑似被误困在围栏内",
    );
    expect(sectionSaveFenceNotice({ fence_fixed: 1, fence_swallowed_lines: 0 })).toContain(
      "仅闭合标记",
    );
  });

  it("没有围栏修复时不产生噪声提示", () => {
    expect(sectionSaveFenceNotice({ fence_fixed: 0, fence_swallowed_lines: 9 })).toBe("");
    expect(sectionSaveFenceNotice({})).toBe("");
    expect(sectionSaveFenceNotice({ fence_fixed: "x" })).toBe("");
  });
});
