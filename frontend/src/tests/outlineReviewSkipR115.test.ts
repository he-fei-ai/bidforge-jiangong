/**
 * R115 · 目录审核「跳过」可归因 · 前端消费护栏（2026-10-20）
 *
 * 用户症状（原话）：「目录（TOC）暴露出明显问题：三级标题大量是占位式泛化名称
 * （如『专项6』『总体实施策划（2）』『总体实施策划（3）』），且存在编号冲突后缀。」
 *
 * 生产日志实证（trace=acdef1e40a64，2026-10-20 15:06:26~15:11:06）：
 *   · 15:10:06 `外科式补齐超时（120s），回退完整 AI 审核`
 *   · 15:11:06 `目录审核超时（60s），跳过审核直接完成`
 *   · 请求以 280582ms 返回 200，返回体 `passed: true`
 * ⇒ 修复链路整体不可用是**常态**（R115 FIX-1 用零 AI 确定性兜底解决标题），
 *   但「审核没跑成」这件事此前前端读成「📘 审核依据：AI 审核链路」——
 *   用户以为审过了，所以也没人去核对标题。
 *
 * 本文件锁两件事：
 *   A. 纯函数值域：`review_skipped` / `review_skip_reason` / `fix_skipped`
 *      必须**置顶为唯一主结论**且 level=warn；脏值（"false" / 1 / "true"）
 *      不得把正常审核误报成跳过；无跳过键时逐字回到 R74 行为。
 *   B. 静态接线锁：主结论行不得再出现「无条件 report AI 审核链路」的写法
 *      （即 `logs.push("📘 ...")` 必须在 `else` 分支里，且其前有 skip 分支）。
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

describe("R115·A组 · 审核/修复跳过必须可归因", () => {
  it("审核超时 → 首行警示且明示未经审核，绝不出现「AI 审核链路」", () => {
    const r = summarizeOutlineReviewBasis({
      passed: true,
      suggestions: ["审核超时已跳过"],
      review_skipped: true,
      review_skip_reason: "timeout",
    });
    expect(r.level).toBe("warn");
    expect(r.logs[0]).toContain("审核超时");
    expect(r.logs[0]).toContain("未经审核");
    expect(r.logs.join("\n")).not.toContain("AI 审核链路");
  });

  it("审核调用失败 / 结构异常 → 各自映射", () => {
    const err = summarizeOutlineReviewBasis({
      review_skipped: true,
      review_skip_reason: "error",
    });
    expect(err.logs[0]).toContain("调用失败");
    const bad = summarizeOutlineReviewBasis({
      review_skipped: true,
      review_skip_reason: "invalid_shape",
    });
    expect(bad.logs[0]).toContain("结构异常");
  });

  it("未登记的 reason token 照原文透出（后端新增细分不得被咽掉）", () => {
    const r = summarizeOutlineReviewBasis({
      review_skipped: true,
      review_skip_reason: "future_reason",
    });
    expect(r.logs[0]).toContain("future_reason");
    expect(r.level).toBe("warn");
  });

  it("修复跳过（invalid/timeout/error）→ 追加警示且 level=warn", () => {
    for (const [code, word] of [
      ["invalid", "校验不通过"],
      ["timeout", "修复超时"],
      ["error", "修复调用失败"],
    ] as const) {
      const r = summarizeOutlineReviewBasis({ fix_skipped: code });
      expect(r.level).toBe("warn");
      expect(r.logs.some((l) => l.includes(word))).toBe(true);
    }
  });

  it("跳过优先于 programmatic（跳过时没有任何审核结论可依据）", () => {
    const r = summarizeOutlineReviewBasis({
      review_mode: "programmatic+surgical",
      review_skipped: true,
      review_skip_reason: "timeout",
    });
    expect(r.logs[0]).toContain("已跳过审核");
    expect(r.logs.join("\n")).not.toContain("程序化预检");
  });

  it("⚠ 脏值防御：非字面 true 不得把正常审核误报成跳过", () => {
    for (const bad of ["false", "true", 1, 0, null, undefined, {}]) {
      const r = summarizeOutlineReviewBasis({
        review_skipped: bad,
        review_skip_reason: "timeout",
      });
      expect(r.level).toBe("info");
      expect(r.logs[0]).toContain("AI 审核链路");
    }
  });

  it("无跳过键 → 逐字回到 R74 行为（零回归）", () => {
    const plain = summarizeOutlineReviewBasis({});
    expect(plain.level).toBe("info");
    expect(plain.logs[0]).toContain("AI 审核链路");
    const prog = summarizeOutlineReviewBasis({ review_mode: "programmatic+titled" });
    expect(prog.level).toBe("info");
    expect(prog.logs[0]).toContain("占位标题外科式重写");
  });

  it("review 为 null/非对象 → level none、logs 空（渲染路径绝不抛）", () => {
    for (const bad of [null, undefined, "str", 7]) {
      const r = summarizeOutlineReviewBasis(bad);
      expect(r.level).toBe("none");
      expect(r.logs).toEqual([]);
    }
  });
});

describe("R115·B组 · 静态接线锁", () => {
  it("前提：能取到被测文件源码（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(PAGE).toContain("export function summarizeOutlineReviewBasis");
  });

  it("三个归因键全部被消费（后端写了前端不读 = 修复不闭环）", () => {
    for (const k of ["review_skipped", "review_skip_reason", "fix_skipped"]) {
      expect(PAGE).toContain(k);
    }
  });

  it("主结论行必须有 skip 前置分支（不得无条件报 AI 审核链路）", () => {
    const fn = PAGE.slice(PAGE.indexOf("export function summarizeOutlineReviewBasis"));
    const head = fn.slice(0, fn.indexOf("// ⚠️ 脏数据防御"));
    // skip 分支必须先于 AI 审核链路行出现，且该行落在 else 分支里
    expect(head.indexOf("skipReason")).toBeGreaterThan(-1);
    expect(head.indexOf("skipReason")).toBeLessThan(head.indexOf("AI 审核链路"));
    expect(head).toContain("} else if (mode.startsWith(\"programmatic\"))");
  });
});
