import { describe, it, expect } from "vitest";
import { deriveExportGate } from "../utils/exportCharts";

/**
 * 导出门禁 · 前后端 high 类型表 parity（2026-09-27）
 *
 * 后端 `_EXPORT_ISSUE_RULE_MAP`（backend/app/routers/export.py）决定 severity；
 * 但 `GET /export/check` 返回的 `issues` **不带 severity 字段**，前端因此
 * 回退到本地 `HIGH_EXPORT_ISSUE_TYPES` 兜底判定。两份字面量必须逐项一致，
 * 否则「后端判 high、前端不认」→ 门禁被静默绕过。
 *
 * 真正的逐项一致性断言放在 pytest 侧
 * （tests/test_seven_module_fixes_20260927.py::TestExportGateHighSeverityParity，
 *  它能直接读后端常量并与本文件比对）。此处锁定前端行为：
 * 缺 severity 时按本地表判定，且表内容不漂移。
 */
describe("exportCharts · deriveExportGate", () => {
  const noSeverity = (type: string) => ({ type } as any);

  it("未做预检 → 阻断", () => {
    const r = deriveExportGate({ issues: [], hasPreflight: false, readiness: null } as any);
    expect(r.allowed).toBe(false);
    expect(r.reason).toContain("预检");
  });

  it("后端显式 severity=high → 阻断（不依赖本地表）", () => {
    const r = deriveExportGate({
      issues: [{ type: "some_new_issue", severity: "high" }],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.allowed).toBe(false);
    expect(r.highIssueCount).toBe(1);
  });

  it("后端显式 severity=medium → 不阻断（本地表不得越权）", () => {
    const r = deriveExportGate({
      issues: [{ type: "orphan_node", severity: "medium" }],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.highIssueCount).toBe(0);
    expect(r.allowed).toBe(true);
  });

  it("缺 severity 时按本地 high 表判定（/export/check 实际形态）", () => {
    const r = deriveExportGate({
      issues: [noSeverity("global_facts_blocked")],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.highIssueCount).toBe(1);
    expect(r.allowed).toBe(false);
  });

  it("缺 severity 的 medium 类问题不阻断", () => {
    const r = deriveExportGate({
      issues: [noSeverity("low_word_count")],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.allowed).toBe(true);
  });

  it("就绪度未跑 / 过期 / 未放行 均阻断", () => {
    expect(deriveExportGate({ issues: [], hasPreflight: true, readiness: null } as any).allowed).toBe(false);
    expect(deriveExportGate({
      issues: [], hasPreflight: true,
      readiness: { has_run: true, released: true, stale: true },
    } as any).allowed).toBe(false);
    expect(deriveExportGate({
      issues: [], hasPreflight: true,
      readiness: { has_run: true, released: false, stale: false },
    } as any).allowed).toBe(false);
  });

  it("全部通过 → 放行", () => {
    const r = deriveExportGate({
      issues: [noSeverity("chart_ungenerated")],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.allowed).toBe(true);
  });
});
