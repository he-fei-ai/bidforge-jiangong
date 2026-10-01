import { describe, it, expect } from "vitest";
import { deriveExportGate } from "../utils/exportCharts";

/**
 * 导出门禁 · 前后端 high 类型表 parity（2026-09-27）
 *
 * 后端 `_EXPORT_ISSUE_RULE_MAP`（backend/app/routers/export.py）决定 severity；
 * 但 `GET /export/check` 返回的 `issues` **不带 severity 字段**，前端因此
 * 回退到本地 `HIGH_EXPORT_ISSUE_TYPES` 兜底判定。两份字面量必须逐项一致，
 * 否则两端对「哪些问题算高风险」的口径会分叉。
 *
 * ⚠️ 2026-10-01 变更：导出门禁由「阻断」改为「只提示不阻断」，
 * 即 `allowed` 恒为 true、预检与就绪度状态不再影响导出。
 * 本表因此**不再决定能否导出**，但仍决定导出页提示里的高风险问题计数，
 * parity 断言继续保留（口径分叉会让提示数量失真）。
 *
 * 真正的逐项一致性断言放在 pytest 侧
 * （tests/test_seven_module_fixes_20260927.py::TestExportGateHighSeverityParity，
 *  它能直接读后端常量并与本文件比对）。此处锁定前端行为：
 * 缺 severity 时按本地表判定，且表内容不漂移。
 */
describe("exportCharts · deriveExportGate", () => {
  const noSeverity = (type: string) => ({ type } as any);

  it("未做预检 → 仍放行（仅提示）", () => {
    const r = deriveExportGate({ issues: [], hasPreflight: false, readiness: null } as any);
    expect(r.allowed).toBe(true);
    expect(r.reason).toContain("不受");
  });

  it("后端显式 severity=high → 计数但不阻断（不依赖本地表）", () => {
    const r = deriveExportGate({
      issues: [{ type: "some_new_issue", severity: "high" }],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.allowed).toBe(true);
    expect(r.highIssueCount).toBe(1);
  });

  it("后端显式 severity=medium → 不计入 high（本地表不得越权）", () => {
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
    expect(r.allowed).toBe(true);
  });

  it("缺 severity 的 medium 类问题不计入 high", () => {
    const r = deriveExportGate({
      issues: [noSeverity("low_word_count")],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.highIssueCount).toBe(0);
    expect(r.allowed).toBe(true);
  });

  it("就绪度未跑 / 过期 / 未放行 均不再阻断导出", () => {
    expect(deriveExportGate({ issues: [], hasPreflight: true, readiness: null } as any).allowed).toBe(true);
    expect(deriveExportGate({
      issues: [], hasPreflight: true,
      readiness: { has_run: true, released: true, stale: true },
    } as any).allowed).toBe(true);
    expect(deriveExportGate({
      issues: [], hasPreflight: true,
      readiness: { has_run: true, released: false, stale: false },
    } as any).allowed).toBe(true);
  });

  it("全部通过 → 放行且无 high 计数", () => {
    const r = deriveExportGate({
      issues: [noSeverity("chart_ungenerated")],
      hasPreflight: true,
      readiness: { has_run: true, released: true, stale: false },
    } as any);
    expect(r.allowed).toBe(true);
    expect(r.highIssueCount).toBe(0);
  });
});
