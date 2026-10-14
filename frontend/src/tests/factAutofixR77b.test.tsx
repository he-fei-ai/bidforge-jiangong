/// <reference types="vite/client" />
/**
 * R77b · 2026-10-15 · 事实冲突定点修复终态提示（前端纯函数层）
 *
 * 锁死的内容：
 * - `normalizeFactAutofixSummary` 防御式归一（缺键/脏值/旧后端未下发 → 全 0，
 *   事件循环里绝不抛异常 —— 与 normalizeChartYield 同纪律）；
 * - `factAutofixHint` 提示语值域：无冲突空串（不制造噪音）/ 已修复 /
 *   部分定位失败 / 完全未修复（转人工信号不静默）；
 * - 静态接线锁：两个 SSE 消费点（hook + 页面）都必须调用
 *   `factAutofixHint(evt.fact_autofix_summary)` —— 只加 payload 不接线
 *   等于前端静默丢弃（R75 同族缺陷的守门）。
 *
 * 判据实现沿用本仓既有范式（contentTerminalParity20261011 /
 * importParseWarningWiringR72）：Vite `import.meta.glob(..., { query: "?raw" })`
 * 读源码文本，不引 node:fs（本项目没装 @types/node，tests 在 tsc 范围内）。
 */
import { describe, it, expect } from "vitest";
import {
  normalizeFactAutofixSummary,
  factAutofixHint,
} from "../utils/contentEvents";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const HOOK = RAW["hooks/useContentGeneration.ts"];
const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

describe("normalizeFactAutofixSummary（防御式归一）", () => {
  it("全 0 空汇总", () => {
    expect(normalizeFactAutofixSummary({
      sections: 0, fixed_sections: 0, conflicts: 0, applied: 0, rejected: 0,
    })).toEqual({
      sections: 0, fixed_sections: 0, conflicts: 0, applied: 0, rejected: 0,
    });
  });

  it("正常值透传（含取整）", () => {
    expect(normalizeFactAutofixSummary({
      sections: 3, fixed_sections: 2, conflicts: 5, applied: 4, rejected: 1.9,
    })).toEqual({
      sections: 3, fixed_sections: 2, conflicts: 5, applied: 4, rejected: 1,
    });
  });

  it("坏形状一律回退全 0（绝不抛异常）", () => {
    const zero = {
      sections: 0, fixed_sections: 0, conflicts: 0, applied: 0, rejected: 0,
    };
    expect(normalizeFactAutofixSummary(undefined)).toEqual(zero);
    expect(normalizeFactAutofixSummary(null)).toEqual(zero);
    expect(normalizeFactAutofixSummary("x")).toEqual(zero);
    expect(normalizeFactAutofixSummary(42)).toEqual(zero);
    expect(normalizeFactAutofixSummary({ sections: "abc", applied: null })).toEqual(zero);
    // 负值/NaN 不回退负值（展示层不接受负数计数）
    expect(normalizeFactAutofixSummary({ sections: -2 }).sections).toBe(0);
    expect(normalizeFactAutofixSummary({ applied: NaN }).applied).toBe(0);
  });
});

describe("factAutofixHint（完成消息提示语值域）", () => {
  it("无冲突/无键 → 空串（不制造噪音）", () => {
    expect(factAutofixHint(undefined)).toBe("");
    expect(factAutofixHint(null)).toBe("");
    expect(factAutofixHint({})).toBe("");
    expect(factAutofixHint({
      sections: 0, fixed_sections: 0, conflicts: 0, applied: 0, rejected: 0,
    })).toBe("");
    // 旧后端未下发该键时的完整终态载荷也不得炸出提示
    expect(factAutofixHint({ standard_summary: {}, word_audit: {} })).toBe("");
  });

  it("全部修复成功", () => {
    expect(factAutofixHint({
      sections: 2, fixed_sections: 2, conflicts: 3, applied: 3, rejected: 0,
    })).toBe("事实一致性：2 章检出 3 处与全局事实的冲突，已自动修复 3 处");
  });

  it("部分定位失败 → 明示留待人工核对", () => {
    expect(factAutofixHint({
      sections: 3, fixed_sections: 2, conflicts: 5, applied: 4, rejected: 1,
    })).toBe("事实一致性：3 章检出 5 处与全局事实的冲突，已自动修复 4 处，1 处定位失败留待人工核对");
  });

  it("attempted 但完全未修复 → 不静默", () => {
    expect(factAutofixHint({
      sections: 1, fixed_sections: 0, conflicts: 2, applied: 0, rejected: 2,
    })).toBe("事实一致性：1 章检出 2 处与全局事实的冲突，未能自动修复，留待人工核对");
    // AI 判定「并非真实矛盾」返回空 edits（applied=0 rejected=0）同样要说话
    expect(factAutofixHint({
      sections: 1, fixed_sections: 0, conflicts: 1, applied: 0, rejected: 0,
    })).toBe("事实一致性：1 章检出 1 处与全局事实的冲突，未能自动修复，留待人工核对");
  });

  it("脏值（字符串数字）不抛", () => {
    expect(factAutofixHint({
      sections: "2", conflicts: "3", applied: "3",
    })).toBe("事实一致性：2 章检出 3 处与全局事实的冲突，已自动修复 3 处");
  });
});

describe("R77b 静态接线锁（payload → 用户可见）", () => {
  it("前提：glob 能取到被测文件（防空跑成常绿）", () => {
    expect(HOOK).toBeTruthy();
    expect(PAGE).toBeTruthy();
  });

  it("两个 SSE 消费点都调用 factAutofixHint", () => {
    for (const [name, src] of [["useContentGeneration", HOOK],
                               ["SchemeWorkbenchPage", PAGE]] as const) {
      expect(src).toContain("factAutofixHint(evt.fact_autofix_summary)");
      expect(name.length).toBeGreaterThan(0);
    }
  });

  it("contentEvents.ts 导出三件套", () => {
    const src = RAW["utils/contentEvents.ts"];
    expect(src).toContain("export function normalizeFactAutofixSummary(");
    expect(src).toContain("export function factAutofixHint(");
    expect(src).toContain("export type FactAutofixSummary");
  });

  it("checkpointSchema.ts 已含 fact_autofix_summary（断线重挂同构）", () => {
    expect(RAW["types/checkpointSchema.ts"])
      .toContain("fact_autofix_summary");
  });
});
