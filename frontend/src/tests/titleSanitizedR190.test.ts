/**
 * R190（2026-11-09）目录生成模块收口 —— 前端护栏
 *  A titleSanitizedNotice 纯函数（FIX-17：清洗不再静默生效）
 *  B context_degraded 消费（FIX-4：D-39 降级可见性）—— 源码锁
 *
 * 判据沿用本仓既有范式（aiConfigR169 / stopControlR106）：
 * import.meta.glob 读源码文本，不引 node:fs。
 */
import { describe, expect, it } from "vitest";
import { titleSanitizedNotice } from "../utils/genericResidual";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const pageSrc = RAW["pages/SchemeWorkbenchPage.tsx"] ?? "";
const typesSrc = RAW["pages/schemeWorkbench/workbenchTypes.ts"] ?? "";

describe("titleSanitizedNotice（R190 FIX-17）", () => {
  it("正整数 → 提示含条数与核对指引", () => {
    const text = titleSanitizedNotice({ title_sanitized: 3 });
    expect(text).toContain("3 个标题");
    expect(text).toContain("不可见/控制字符");
    expect(text).toContain("核对");
  });

  it("字符串数字 → 取整后提示", () => {
    expect(titleSanitizedNotice({ title_sanitized: "2" })).toContain("2 个标题");
  });

  it.each([
    ["无键", {}],
    ["0（未发生清洗）", { title_sanitized: 0 }],
    ["负数", { title_sanitized: -1 }],
    ["NaN", { title_sanitized: Number.NaN }],
    ["布尔 true（脏数据）", { title_sanitized: true }],
    ["非数字字符串", { title_sanitized: "abc" }],
    ["null", null],
    ["undefined", undefined],
  ])("%s → 空串（绝不误报）", (_name, data) => {
    expect(titleSanitizedNotice(data)).toBe("");
  });

  it("消费端已接线（useOutlineGate 同一出口）", () => {
    const gate = RAW["pages/schemeWorkbench/useOutlineGate.tsx"] ?? "";
    expect(gate).toContain("titleSanitizedNotice(data)");
  });
});

describe("context_degraded 前端消费（R190 FIX-4 源码锁）", () => {
  it("onSseEvent 存在 context_degraded 分支且拼接可读提示", () => {
    expect(pageSrc).toContain('evt.event === "context_degraded"');
    expect(pageSrc).toContain("ContextDegradedEvent");
    expect(pageSrc).toContain("上下文降级");
  });

  it("分段失败与截断两种 reason 均有文案", () => {
    expect(pageSrc).toContain("segment_failed");
    expect(pageSrc).toContain("被截断");
  });

  it("类型契约已在 workbenchTypes 登记（后端产出键 parity）", () => {
    expect(typesSrc).toContain("ContextDegradedEvent");
    expect(typesSrc).toContain('event: "context_degraded"');
    expect(typesSrc).toContain("original_chars");
    expect(typesSrc).toContain("kept_chars");
  });
});
