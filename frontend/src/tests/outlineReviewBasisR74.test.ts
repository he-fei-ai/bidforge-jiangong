/// <reference types="vite/client" />
/**
 * 目录审核依据呈现 · R74（G1）前端护栏（2026-10-12）。
 *
 * 背景：后端 `_review_and_fix_outline` 的每个程序化出口都下发
 * `review.review_mode` / `review.suggestions`（completed 事件与 checkpoint
 * 白名单均携带），但旧前端没有任何消费方 ⇒ 用户看不到「本次结论未经完整
 * AI 审核」与已执行的补救动作（R73b 遗留 #1）。属呈现增强、不改判定。
 *
 * 两层守护：
 * - A 组：`summarizeOutlineReviewBasis` 纯函数直测（值域映射 / 脏数据防御 /
 *   未知 token 透出 / 建议截断）；
 * - B 组：静态接线锁（沿用 importParseWarningWiringR72 的
 *   `import.meta.glob(..., {query:"?raw"})` 范式）——三处消费点
 *   （在线 completed / 刷新接管恢复 / 断线重挂）摘掉任何一处都必须变红，
 *   防「实现了但链路不生效」（AGENTS §5 同族）。
 */
import { describe, it, expect } from "vitest";
import { summarizeOutlineReviewBasis } from "../pages/SchemeWorkbenchPage";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

describe("R74·G1 A组 · summarizeOutlineReviewBasis 纯函数", () => {
  it("programmatic（覆盖全过）：报程序化预检且明示未经完整 AI 审核", () => {
    const r = summarizeOutlineReviewBasis({ review_mode: "programmatic" });
    expect(r.level).toBe("info");
    expect(r.logs[0]).toContain("程序化预检");
    expect(r.logs[0]).toContain("全部必备项已覆盖");
    expect(r.logs[0]).toContain("未经完整 AI 审核");
  });

  it("programmatic+titled / +surgical / +surgical+titled 三细分逐条映射", () => {
    const titled = summarizeOutlineReviewBasis({ review_mode: "programmatic+titled" });
    expect(titled.logs[0]).toContain("占位标题外科式重写");
    const surg = summarizeOutlineReviewBasis({ review_mode: "programmatic+surgical" });
    expect(surg.logs[0]).toContain("外科式补齐缺失章节");
    const both = summarizeOutlineReviewBasis({
      review_mode: "programmatic+surgical+titled",
    });
    expect(both.logs[0]).toContain("外科式补齐缺失章节");
    expect(both.logs[0]).toContain("占位标题外科式重写");
  });

  it("未登记的模式 token 照原文透出（后端新增细分不得被前端咽掉）", () => {
    const r = summarizeOutlineReviewBasis({ review_mode: "programmatic+future_x" });
    expect(r.logs[0]).toContain("future_x");
  });

  it("无 review_mode / 非 programmatic 开头 → 报 AI 审核链路", () => {
    expect(summarizeOutlineReviewBasis({}).logs[0]).toContain("AI 审核链路");
    expect(
      summarizeOutlineReviewBasis({ review_mode: "ai_full" }).logs[0],
    ).toContain("AI 审核链路");
  });

  it("suggestions 逐条列出，脏元素（null/非串/空白）逐个过滤不抛", () => {
    const r = summarizeOutlineReviewBasis({
      review_mode: "programmatic",
      suggestions: ["已补齐：基坑支护", null, 42, "   ", "已重写占位标题 2 处"],
    });
    expect(r.logs).toContain("　· 已补齐：基坑支护");
    expect(r.logs).toContain("　· 已重写占位标题 2 处");
    expect(r.logs.filter((l) => l.startsWith("　·")).length).toBe(2);
  });

  it("suggestions 超过 8 条截断并如实报总数", () => {
    const many = Array.from({ length: 11 }, (_, i) => `建议${i + 1}`);
    const r = summarizeOutlineReviewBasis({ suggestions: many });
    expect(r.logs.filter((l) => l.startsWith("　· 建议")).length).toBe(8);
    expect(r.logs.some((l) => l.includes("共 11 条"))).toBe(true);
  });

  it("review 为 null / 非对象 → level none、logs 空（渲染路径绝不抛）", () => {
    for (const bad of [null, undefined, "str", 7]) {
      const r = summarizeOutlineReviewBasis(bad);
      expect(r.level).toBe("none");
      expect(r.logs).toEqual([]);
    }
  });
});

describe("R74·G1 B组 · 三处消费点静态接线锁", () => {
  const CALL = "summarizeOutlineReviewBasis(";

  it("前提：能取到被测文件源码（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(PAGE).toContain("export function summarizeOutlineReviewBasis");
  });

  it("全页恰好 3 处调用（在线 completed / 刷新接管 / 断线重挂，一处都不能少）", () => {
    const n = PAGE.split(CALL).length - 1;
    // 减去定义处自身不带调用括号的干扰：export function 行也是 `summarizeOutlineReviewBasis(`
    expect(n).toBe(4); // 1 定义 + 3 消费点
  });

  it("completed 分支：消费点位于质量摘要之后、开闸确认之前（同口径顺序）", () => {
    const qi = PAGE.indexOf("summarizeOutlineQuality(evt.review)");
    const ri = PAGE.indexOf("summarizeOutlineReviewBasis(evt.review)");
    const gi = PAGE.indexOf("openOutlineGate(evt.outline)", ri);
    expect(qi).toBeGreaterThan(-1);
    expect(ri).toBeGreaterThan(qi);
    expect(gi).toBeGreaterThan(ri);
  });

  it("两条 checkpoint 恢复路径都必须消费 ckpt.review", () => {
    const n = PAGE.split("summarizeOutlineReviewBasis(ckpt.review)").length - 1;
    expect(n).toBe(2);
  });

  it("反向锚：摘除全部消费点（只留定义）后，接线判据必须归零（证明扫描在守）", () => {
    const stripped = PAGE
      .split("\n")
      .filter((line) => !line.trim().startsWith(CALL))
      .join("\n");
    expect(stripped).toContain("export function summarizeOutlineReviewBasis");
    expect(stripped.split(CALL).length - 1).toBe(1); // 只剩定义自身
  });
});
