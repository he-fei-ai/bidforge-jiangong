/**
 * R116 · 目录审核「上游已实证不可用 ⇒ 跳过」· 前端消费护栏（2026-10-20）
 *
 * 承接 R115 §九 L3：审核超时的**最优解不是调大默认值**（AI 全挂时调大只会
 * 让用户等更久才拿到同样的「跳过审核」结果），而是「补齐刚失败 ⇒ 同一链路
 * 已实证不可用 ⇒ 直接跳过审核」。
 *
 * 后端（`sse_handlers._review_and_fix_outline`）新增归因值
 * `review_skip_reason="upstream_dead"`，受
 * `settings.outline_skip_review_when_upstream_dead`（默认 True）控制。
 *
 * 本文件锁两件事：
 *   A. 值域闭环：`upstream_dead` 必须映射为**用户能理解的原因**，且仍满足
 *      R115 契约（level=warn / 首行 / 明示未经审核 / 不出现「AI 审核链路」）。
 *   B. 静态接线锁：映射项必须真的写在 reasonMap 里（后端加值、前端不读 =
 *      用户看到原始英文 token，等于没做归因）。
 */
import { describe, it, expect } from "vitest";
import { summarizeOutlineReviewBasis } from "../pages/SchemeWorkbenchPage";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }),
  ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v as string]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

describe("R116·A组 · upstream_dead 必须可理解且不谎报已审核", () => {
  it("首行为警示、点明补齐已失败、明示未经审核", () => {
    const r = summarizeOutlineReviewBasis({
      passed: true,
      review_skipped: true,
      review_skip_reason: "upstream_dead",
    });
    expect(r.level).toBe("warn");
    expect(r.logs[0]).toContain("补齐已失败");
    expect(r.logs[0]).toContain("未经审核");
    expect(r.logs.join("\n")).not.toContain("AI 审核链路");
  });

  it("不得把原始英文 token 直接甩给用户", () => {
    const r = summarizeOutlineReviewBasis({
      review_skipped: true,
      review_skip_reason: "upstream_dead",
    });
    expect(r.logs[0]).not.toContain("upstream_dead");
  });

  it("与其它归因值互不串味（timeout 仍映射为审核超时）", () => {
    const t = summarizeOutlineReviewBasis({
      review_skipped: true,
      review_skip_reason: "timeout",
    });
    expect(t.logs[0]).toContain("审核超时");
    expect(t.logs[0]).not.toContain("补齐已失败");
  });
});

describe("R116·B组 · 静态接线锁", () => {
  it("前提：能取到被测文件源码（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(PAGE).toContain("export function summarizeOutlineReviewBasis");
  });

  it("reasonMap 必须含 upstream_dead 映射项（否则归因不闭环）", () => {
    const fn = PAGE.slice(PAGE.indexOf("export function summarizeOutlineReviewBasis"));
    const map = fn.slice(fn.indexOf("const reasonMap"), fn.indexOf("const why"));
    expect(map).toContain("upstream_dead:");
  });
});
