// @vitest-environment jsdom
/**
 * ✅ R197（2026-11-09）· 审核与预检模块收口 · 前端护栏。
 *
 * 源码棘轮锁（?raw）—— 任何一项退回旧实现都会变红：
 * - F1  useReviewAudit 五个 AbortController 的切方案/卸载收口 + handler 落地守卫；
 * - F2  切方案复位本域瞬态（专家/合规/审计/CR）；
 * - F3  finally loading 复位收敛进 ref 门禁 + 合规检查重入锁；
 * - F4  confirm 在飞锁 + 后端 R181 加法键 rejected_not_restored 消费；
 * - F5  Drawer 帮助文案三态语义 + confirming 锁四个确认按钮；
 * - F6  ReviewTab score/EXPORT_ISSUE_LABEL 兜底 + 清除结果清 exportCheckIncomplete；
 * - F7  ReviewWorkflowPanel Progress success 仅在全部通过时点亮；
 * - F8  useOutlineGate 内层覆盖确认取消时给出路提示。
 */
import { describe, it, expect } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
);

function src(pathSuffix: string): string {
  const hit = Object.entries(RAW).find(([k]) => k.endsWith(pathSuffix));
  if (!hit) throw new Error(`源码文件未找到：${pathSuffix}`);
  return hit[1];
}

describe("R197 useReviewAudit 生命周期收口", () => {
  const hookSrc = src("pages/schemeWorkbench/useReviewAudit.tsx");

  it("F1: [id] effect 中止本域全部五个 AbortController", () => {
    // 切方案 effect 内：共享三 ref（页面持有）+ CR 两 ref（hook 私有）
    const effectBody = hookSrc.slice(
      hookSrc.indexOf("schemeIdRef.current = id ||"),
      hookSrc.indexOf("// 卸载兜底"),
    );
    expect(effectBody).toContain("expertAbortRef.current?.abort()");
    expect(effectBody).toContain("checkAbortRef.current?.abort()");
    expect(effectBody).toContain("auditAbortRef.current?.abort()");
    expect(effectBody).toContain("crScanAbortRef.current?.abort()");
    expect(effectBody).toContain("crRepairAbortRef.current?.abort()");
  });

  it("F1: 卸载 cleanup 中止全部五个 ref（R191 教训：缺一即复活）", () => {
    const cleanup = hookSrc.slice(hookSrc.indexOf("// 卸载兜底"), hookSrc.indexOf("const handleExpertReview"));
    expect(cleanup).toContain("expertAbortRef.current?.abort()");
    expect(cleanup).toContain("crScanAbortRef.current?.abort()");
    expect(cleanup).toContain("crRepairAbortRef.current?.abort()");
  });

  it("F1: handler 落地守卫覆盖 abort 与跨方案双通道", () => {
    // 五个 AI handler 的落地判据统一为 aborted || schemeIdRef.current !== sid
    const guards = hookSrc.match(/controller\.signal\.aborted \|\| schemeIdRef\.current !== sid/g) || [];
    expect(guards.length).toBeGreaterThanOrEqual(8); // try + catch × 5 个 handler
    // CR 清单/历史加载同样带守卫
    expect(hookSrc).toContain("if (schemeIdRef.current !== sid) return;");
  });

  it("F2: 切方案复位本域全部瞬态", () => {
    const effectBody = hookSrc.slice(
      hookSrc.indexOf("schemeIdRef.current = id ||"),
      hookSrc.indexOf("// 卸载兜底"),
    );
    expect(effectBody).toContain("setExpertResult(null)");
    expect(effectBody).toContain("setComplianceResults([])");
    expect(effectBody).toContain("setConsistencyResult(null)");
    expect(effectBody).toContain("setCrConflicts([])");
    expect(effectBody).toContain("setCrSummary(null)");
    expect(effectBody).toContain("setCrRepair(null)");
    expect(effectBody).toContain("setCrScanId(\"\")");
    expect(effectBody).toContain("setCrHistory([])");
  });

  it("F3: finally loading 复位收敛进 ref 门禁（同域并发不得互关 spinner）", () => {
    // 五个 finally 块都应先判 ref 再复位 state
    const gated = hookSrc.match(/if \(\w+AbortRef\.current === controller\) \{\s*\n\s*\w+AbortRef\.current = null;\s*\n\s*set\w+\(false\);/g) || [];
    expect(gated.length).toBeGreaterThanOrEqual(5);
  });

  it("F3: 合规检查重入锁在飞 ref", () => {
    expect(hookSrc).toContain("complianceInFlightRef");
    expect(hookSrc).toContain("if (complianceInFlightRef.current) return;");
  });

  it("F4: confirm 在飞锁 + rejected_not_restored 契约消费", () => {
    expect(hookSrc).toContain("crConfirming");
    expect(hookSrc).toContain("rejected_not_restored");
    expect(hookSrc).toContain("conflict_restore_results");
    expect(hookSrc).toContain("请人工核对后再导出");
  });

  it("F4: hook 返回 crConfirming 供 Drawer 锁按钮", () => {
    expect(hookSrc).toMatch(/return \{[\s\S]*crConfirming[\s\S]*\};/);
  });

  it("F4: 页面把 confirming 传入 ComplianceRepairDrawer", () => {
    const pageSrc = src("pages/SchemeWorkbenchPage.tsx");
    expect(pageSrc).toContain("confirming={crConfirming}");
    // 切方案 + 卸载清单补审核域三 ref
    expect(pageSrc.match(/expertAbortRef\.current\?\.abort\(\)/g)!.length).toBeGreaterThanOrEqual(2);
    expect(pageSrc.match(/checkAbortRef\.current\?\.abort\(\)/g)!.length).toBeGreaterThanOrEqual(2);
    expect(pageSrc.match(/auditAbortRef\.current\?\.abort\(\)/g)!.length).toBeGreaterThanOrEqual(2);
  });
});

describe("R197 ComplianceRepairDrawer", () => {
  const drawerSrc = src("pages/schemeWorkbench/ComplianceRepairDrawer.tsx");

  it("F5: 帮助文案对齐冲突级片段回滚三态语义（不再宣称整体恢复）", () => {
    // ⚠️ 断言特征用旧 UI 文案专属短语（注释里提及「整体恢复」不得误伤）
    expect(drawerSrc).not.toContain("会将该章节");
    expect(drawerSrc).toContain("回滚该");
    expect(drawerSrc).toContain("正文不会回滚");
  });

  it("F4: 四个确认按钮全部锁在飞态", () => {
    // 批量两枚 loading={confirming}，逐条两枚 disabled={confirming}
    expect(drawerSrc.match(/loading=\{confirming\}/g)!.length).toBe(2);
    expect(drawerSrc.match(/disabled=\{confirming\}/g)!.length).toBe(2);
    expect(drawerSrc).not.toContain("拒绝（恢复原文）");
  });
});

describe("R197 ReviewTab / ReviewWorkflowPanel / useOutlineGate", () => {
  const tabSrc = src("pages/schemeWorkbench/tabs/ReviewTab.tsx");

  it("F6: expertResult.score 非空断言兜底", () => {
    expect(tabSrc).toContain("expertResult.score ?? 0");
    expect(tabSrc).not.toMatch(/expertResult\.score! >=/);
  });

  it("F6: 清除结果一并清 exportCheckIncomplete", () => {
    expect(tabSrc).toContain("setExportCheckIncomplete(false)");
  });

  it("F6: EXPORT_ISSUE_LABEL 缺 type 兜底（不再渲染 undefined：）", () => {
    expect(tabSrc).toContain('iss.type || "未知问题"');
  });

  it("F7: Progress success 仅在全部通过时点亮", () => {
    const panelSrc = src("components/review/ReviewWorkflowPanel.tsx");
    expect(panelSrc).toContain("summary.approved_sections === summary.total_sections");
  });

  it("F8: 内层覆盖确认取消时给出路提示", () => {
    const gateSrc = src("pages/schemeWorkbench/useOutlineGate.tsx");
    expect(gateSrc).toContain("已取消覆盖：目录树预览仍在");
  });
});
