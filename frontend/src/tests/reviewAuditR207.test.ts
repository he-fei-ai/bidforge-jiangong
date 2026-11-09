// @vitest-environment jsdom
/**
 * ✅ R207（2026-11-09）· 审核与预检模块深度加固 · 前端源码棘轮。
 *
 * 日志驱动：后台 21:15 导出 409 明细出现同族重复标题、21:23 合规检查分 4 批送审；
 * 本轮收口「长请求跨方案落地 / 取消零反馈 / 来源标签缺项 / 加法键零消费」四类。
 * 任一项退回旧实现即变红（与后端 test_review_audit_r207_20261109.py 双向互补）。
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

describe("R207 跨方案与取消入口", () => {
  const hookSrc = src("pages/schemeWorkbench/useReviewAudit.tsx");
  const pageSrc = src("pages/SchemeWorkbenchPage.tsx");
  const checkSrc = src("pages/schemeWorkbench/useExportCheck.ts");

  it("C1: 三个预检取消按钮均有即时反馈（R205 只修了 CR 两个）", () => {
    expect(hookSrc.split('msg.info("已取消当前检查")').length - 1).toBe(3);
    expect(hookSrc).toContain('msg.info("已取消一致性扫描")');
    expect(hookSrc).toContain('msg.info("已取消一致性修复")');
  });

  it("C2: 页面自持的一致性审计历史 GET 已纳入 abort 清单 + 落地守卫", () => {
    expect(pageSrc).toContain("const consistencyLatestAbortRef = useRef<AbortController | null>(null)");
    expect(pageSrc).toContain("currentSchemeIdRef.current !== sid) return;");
    expect(pageSrc.split("consistencyLatestAbortRef").length - 1).toBeGreaterThanOrEqual(4);
    expect(src("api/index.ts")).toContain(
      "consistencyLatest: (schemeId: string, signal?: AbortSignal)",
    );
  });

  it("C3: qualityAbortRef 具备 [id] effect / 同步重入锁 / finally 门禁", () => {
    expect(checkSrc).toContain("const qualityInFlightRef = useRef(false)");
    expect(checkSrc).toContain("}, [id]);");
    expect(checkSrc).toContain("qualityAbortRef.current?.abort()");
    const finallyBody = checkSrc.slice(checkSrc.lastIndexOf("} finally {"));
    expect(finallyBody).toContain("qualityAbortRef.current === controller");
    expect(finallyBody).toContain("currentSchemeIdRef.current === sid");
    // 旧实现：finally 无条件 setCheckingQuality(false)
    expect(finallyBody.trim().startsWith("} finally {\n      setCheckingQuality(false);")).toBe(false);
  });

  it("C4: CR 回滚弹窗有跨方案守卫 + 在飞锁（命令式 onOk 不经过 effect）", () => {
    expect(hookSrc).toContain("const crRollbackInFlightRef = useRef(false)");
    expect(hookSrc).toContain("if (schemeIdRef.current !== sid || crRollbackInFlightRef.current) return;");
    expect(hookSrc).toContain("if (schemeIdRef.current !== sid) return;");
    expect(hookSrc).toContain("crRollbackInFlightRef.current = false;");
  });
});

describe("R207 后端加法键消费与来源标签", () => {
  const hookSrc = src("pages/schemeWorkbench/useReviewAudit.tsx");

  it("C5: conflict_restore_results 从「类型声明」升级为真实消费", () => {
    expect(hookSrc).toContain("const restoreDetails = (result.conflict_restore_results ?? [])");
    expect(hookSrc).toContain("const detailNotRestored = restoreDetails.filter((d) => d.restored === false)");
    expect(hookSrc).toContain("请人工核对后再导出");
    // 不能只靠聚合数量（R197 的 rejected_not_restored 消费保持不动）
    expect(hookSrc).toContain("const rejectedNotRestored = result.rejected_not_restored ?? []");
  });

  it("C6: 一致性审计 partial 时不再弹绿色「完成」", () => {
    expect(hookSrc).toContain("if (data.partial === true) {");
    expect(hookSrc).toContain("一致性审计覆盖不完整");
    expect(hookSrc).toContain("partial: data.partial === true");
    expect(hookSrc).toContain("checked_chars: data.checked_chars");
  });

  it("C7: SOURCE_LABEL 补齐后端 numbering 来源", () => {
    expect(src("components/review/ReadinessDashboard.tsx")).toContain('numbering: "编号一致性"');
  });

  it("C8: 后端子类标签与前端 EXPORT_ISSUE_LABEL 同键同义", () => {
    const labelBlock = src("pages/schemeWorkbench/exportShared.ts")
      .split("export const EXPORT_ISSUE_LABEL")[1]
      .split("};")[0];
    expect(labelBlock).toContain('review_pending: "章节待审核"');
    expect(labelBlock).toContain('review_rejected: "章节审核未通过"');
    expect(labelBlock).toContain('review_missing: "缺少章节审核记录"');
  });
});
