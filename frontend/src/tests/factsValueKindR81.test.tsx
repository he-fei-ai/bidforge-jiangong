/// <reference types="vite/client" />
/**
 * R81 · 2026-10-15 · 全局事实「要求 vs 事实口径」前端编辑链路护栏
 *
 * 锁死的内容：
 * - `factValueKindLabel` 徽标文案值域（requirement→要求口径 / fact→本方案口径 /
 *   空值与未区分→空串，与徽标「空值零渲染」纪律一致）；
 * - `FACT_VALUE_KIND_OPTIONS` 选项值与后端 `VALUE_KIND_ALLOWED`（fact/requirement）
 *   逐项一致（跨语言 parity，后端新增值域而前端漏改即红）；
 * - 静态接线锁（沿用 R77b 的 import.meta.glob(?raw) 范式，不引 node:fs）：
 *   打开编辑回填 value_kind、提交 item_updates 透传 value_kind、弹窗存在
 *   name="value_kind" 表单项、徽标走单一导出函数。
 */
import { describe, it, expect } from "vitest";
import {
  FACT_VALUE_KIND_OPTIONS,
  factValueKindLabel,
} from "../pages/SchemeWorkbenchPage";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

describe("factValueKindLabel（徽标文案值域）", () => {
  it("requirement → 要求口径", () => {
    expect(factValueKindLabel("requirement")).toBe("要求口径");
  });
  it("fact → 本方案口径", () => {
    expect(factValueKindLabel("fact")).toBe("本方案口径");
  });
  it("空串/undefined/null → 空串（不渲染徽标）", () => {
    expect(factValueKindLabel("")).toBe("");
    expect(factValueKindLabel(undefined)).toBe("");
    expect(factValueKindLabel(null)).toBe("");
  });
});

describe("FACT_VALUE_KIND_OPTIONS（与后端白名单 parity）", () => {
  it("值域恰为 fact/requirement", () => {
    expect(FACT_VALUE_KIND_OPTIONS.map((o) => o.value).sort())
      .toEqual(["fact", "requirement"]);
  });
  it("每项 label 非空（选择器不能出现空白选项）", () => {
    for (const o of FACT_VALUE_KIND_OPTIONS) {
      expect(typeof o.label).toBe("string");
      expect(o.label.length).toBeGreaterThan(0);
    }
  });
});

describe("单条事实编辑弹窗接线（静态锁）", () => {
  it("打开编辑时回填 value_kind", () => {
    expect(PAGE).toContain('value_kind: item.value_kind || ""');
  });
  it("提交 item_updates 时透传 value_kind", () => {
    expect(PAGE).toContain('value_kind: values.value_kind ?? ""');
  });
  it("弹窗存在 value_kind 表单项", () => {
    expect(PAGE).toContain('name="value_kind"');
  });
  it("徽标文案走单一导出函数", () => {
    expect(PAGE).toContain("factValueKindLabel(it.value_kind)");
  });
});
