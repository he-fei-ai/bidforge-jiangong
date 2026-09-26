// @vitest-environment node
/**
 * 《待补充清单》前端派生纯函数回归测试（2026-09-24，治 F 层：人工补录兜底）
 *
 * 覆盖：严重度判定（无占位 / 仅规范占位 / 含不规范写法）、Alert 映射、
 * 摘要文案、聚合条目展示截断、非法输入不抛异常。
 */
import { describe, expect, it } from "vitest";
import {
  DISPLAY_ENTRY_LIMIT,
  PlaceholderReport,
  derivePlaceholderSeverity,
  deriveTrendDelta,
  limitEntries,
  severityToAlertProps,
  summarizePlaceholderReport,
} from "../utils/placeholderReport";

function makeReport(over: Partial<PlaceholderReport> = {}): PlaceholderReport {
  return {
    total: 0, formatted_total: 0, bare_total: 0, fuzzy_total: 0,
    field_count: 0, section_count: 0,
    by_field: [], by_section: [], occurrences: [], truncated: false,
    ...over,
  };
}

describe("derivePlaceholderSeverity", () => {
  it("无占位 / 空报告 → none", () => {
    expect(derivePlaceholderSeverity(null)).toBe("none");
    expect(derivePlaceholderSeverity(undefined)).toBe("none");
    expect(derivePlaceholderSeverity(makeReport())).toBe("none");
    expect(derivePlaceholderSeverity(makeReport({ total: -1 }))).toBe("none");
  });

  it("仅规范占位（AI 按红线留的可检索标记）→ warn", () => {
    expect(derivePlaceholderSeverity(makeReport({
      total: 3, formatted_total: 3, field_count: 2, section_count: 1,
      by_field: [{ field: "基坑深度", count: 3, section_ids: ["a"], section_titles: ["概况"] }],
      by_section: [{ section_id: "a", title: "概况", count: 3, fields: ["基坑深度"] }],
    }))).toBe("warn");
  });

  it("含裸标记 / 模糊占位（违反提示词规范）→ error", () => {
    expect(derivePlaceholderSeverity(makeReport({ total: 2, formatted_total: 1, bare_total: 1 }))).toBe("error");
    expect(derivePlaceholderSeverity(makeReport({ total: 1, fuzzy_total: 1 }))).toBe("error");
  });
});

describe("severityToAlertProps", () => {
  it("三档严重度分别映射 success / warning / error 与文案", () => {
    expect(severityToAlertProps("none").type).toBe("success");
    expect(severityToAlertProps("warn").type).toBe("warning");
    expect(severityToAlertProps("error").type).toBe("error");
    expect(severityToAlertProps("error").message).toContain("回改");
  });
});

describe("summarizePlaceholderReport", () => {
  it("空报告返回空串；有数据返回计数摘要", () => {
    expect(summarizePlaceholderReport(null)).toBe("");
    const s = summarizePlaceholderReport(makeReport({
      total: 12, field_count: 5, section_count: 4,
    }));
    expect(s).toContain("12 处");
    expect(s).toContain("5 个字段");
    expect(s).toContain("4 个章节");
    expect(s).not.toContain("截断");
  });

  it("truncated 时文案如实提示（不静默）", () => {
    expect(summarizePlaceholderReport(makeReport({ total: 1, field_count: 1, section_count: 1, truncated: true })))
      .toContain("截断");
  });
});

describe("limitEntries", () => {
  it("超出展示上限时截断并返回隐藏数（反例：空/undefined 不抛）", () => {
    const [vis, hidden] = limitEntries([1, 2, 3], 2);
    expect(vis).toEqual([1, 2]);
    expect(hidden).toBe(1);
    expect(limitEntries(undefined)[0]).toEqual([]);
    expect(limitEntries([])[1]).toBe(0);
  });

  it("默认上限为 DISPLAY_ENTRY_LIMIT", () => {
    const many = Array.from({ length: DISPLAY_ENTRY_LIMIT + 5 }, (_, i) => i);
    const [, hidden] = limitEntries(many);
    expect(hidden).toBe(5);
  });
});

describe("deriveTrendDelta", () => {
  it("空历史 / 非法输入 → 全 null（不抛异常）", () => {
    expect(deriveTrendDelta(null)).toEqual({ delta: null, latest: null, previous: null });
    expect(deriveTrendDelta(undefined)).toEqual({ delta: null, latest: null, previous: null });
    expect(deriveTrendDelta([])).toEqual({ delta: null, latest: null, previous: null });
    expect(deriveTrendDelta([{ total: NaN } as any])).toEqual({ delta: null, latest: null, previous: null });
  });

  it("仅一条基线：latest 有值但无 delta（首次建立基线）", () => {
    const t = deriveTrendDelta([{ total: 12 } as any]);
    expect(t.latest).toBe(12);
    expect(t.delta).toBeNull();
  });

  it("两条基线（倒序：最新在前）→ delta = 最新 - 上次（负数=改善）", () => {
    const t = deriveTrendDelta([{ total: 3 } as any, { total: 7 } as any]);
    expect(t.latest).toBe(3);
    expect(t.previous).toBe(7);
    expect(t.delta).toBe(-4);
  });

  it("上升给出正 delta（提示核查是否引入新占位）", () => {
    expect(deriveTrendDelta([{ total: 9 } as any, { total: 4 } as any]).delta).toBe(5);
  });
});
