/// <reference types="vite/client" />
/**
 * 事实诊断单侧失败不丢成果 · R75（G-D）前端护栏（2026-10-13）。
 *
 * 背景：loadFactsDiagnostics 旧实现用 Promise.all 并行拉「章节覆盖度
 * （factsApi.chapters）」与「危大阈值（factsApi.dangerCheck）」两个
 * **互相独立**的诊断 —— 任一请求失败即整体 reject，另一侧已到达的成果
 * 连带丢弃，两个面板一起空白只剩一条「加载事实诊断失败」。生产实证形态：
 * danger-check 对无方案名/无补充文本的调用会 400（后端 global_facts.py
 * P0 修复注释记录过该形态），此时 chapters 明明 200 返回，界面却零展示。
 * 现改 Promise.allSettled 独立落定，合并判定抽成纯函数。
 *
 * 两层守护（沿用 outlineReviewBasisR74 范式）：
 * - A 组：`mergeFactsDiagnosticsSettled` 纯函数直测（值域 / Abort 语义 /
 *   axios detail 优先 / 单侧失败另一侧照常落板）；
 * - B 组：静态接线锁 —— 接线若从 allSettled 回退到 Promise.all、或成功侧
 *   落板语句被摘掉，必须变红（防「实现了但链路不生效」，AGENTS §5 同族）。
 */
import { describe, it, expect } from "vitest";
import { mergeFactsDiagnosticsSettled } from "../pages/SchemeWorkbenchPage";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

const abortErr = () => {
  const e = new Error("signal aborted");
  e.name = "AbortError";
  return e;
};
/** axios 风格错误（有 response.data.detail） */
const httpErr = (detail: string) =>
  Object.assign(new Error("Request failed with status code 400"), {
    response: { data: { detail } },
  });

describe("R75·G-D A组 · mergeFactsDiagnosticsSettled 纯函数", () => {
  it("双侧成功：两份成果各自返回，无告警不作废", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "fulfilled", value: { data: { chapters: 1 } } },
      { status: "fulfilled", value: { data: { danger: 2 } } },
    );
    expect(r.chapters).toEqual({ chapters: 1 });
    expect(r.danger).toEqual({ danger: 2 });
    expect(r.warning).toBeNull();
    expect(r.aborted).toBe(false);
  });

  it("核心场景：dangerCheck 失败、chapters 成功 → chapters 成果必须保留 + 单侧告警", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "fulfilled", value: { data: { stats: { total: 9 } } } },
      { status: "rejected", reason: httpErr("需要 scheme_name 或 extra_text") },
    );
    expect(r.chapters).toEqual({ stats: { total: 9 } });
    expect(r.danger).toBeUndefined();
    expect(r.warning).toBe("需要 scheme_name 或 extra_text");
    expect(r.aborted).toBe(false);
  });

  it("对称场景：chapters 失败、dangerCheck 成功 → danger 成果必须保留", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "rejected", reason: new Error("Network Error") },
      { status: "fulfilled", value: { data: { classification: {} } } },
    );
    expect(r.chapters).toBeUndefined();
    expect(r.danger).toEqual({ classification: {} });
    expect(r.warning).toBe("Network Error");
  });

  it("双侧皆 AbortError：整个请求作废（aborted=true），静默零告警", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "rejected", reason: abortErr() },
      { status: "rejected", reason: abortErr() },
    );
    expect(r.aborted).toBe(true);
    expect(r.warning).toBeNull();
    expect(r.chapters).toBeUndefined();
    expect(r.danger).toBeUndefined();
  });

  it("单侧 Abort、另一侧真实失败：真实失败不被静默（warning 取真实错误）", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "rejected", reason: abortErr() },
      { status: "rejected", reason: httpErr("boom") },
    );
    expect(r.aborted).toBe(false);
    expect(r.warning).toBe("boom");
  });

  it("单侧成功、另一侧 Abort：成功侧照常返回且无告警（取消不惩罚已完成方）", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "fulfilled", value: { data: { ok: 1 } } },
      { status: "rejected", reason: abortErr() },
    );
    expect(r.chapters).toEqual({ ok: 1 });
    expect(r.warning).toBeNull();
    expect(r.aborted).toBe(false);
  });

  it("detail 优先于 message；两者皆缺失回落固定文案", () => {
    const withDetail = mergeFactsDiagnosticsSettled(
      { status: "rejected", reason: httpErr("明细文案") },
      { status: "fulfilled", value: { data: {} } },
    );
    expect(withDetail.warning).toBe("明细文案");
    const bare = mergeFactsDiagnosticsSettled(
      { status: "rejected", reason: {} as any },
      { status: "fulfilled", value: { data: {} } },
    );
    expect(bare.warning).toBe("加载事实诊断失败");
  });

  it("fulfilled 但 data 为 null：透传 null（与 undefined=缺席 严格区分）", () => {
    const r = mergeFactsDiagnosticsSettled(
      { status: "fulfilled", value: { data: null } },
      { status: "fulfilled", value: { data: undefined } },
    );
    expect(r.chapters).toBeNull();
    expect(r.danger).toBeUndefined();
    expect("chapters" in r).toBe(true);
  });
});

describe("R75·G-D B组 · loadFactsDiagnostics 静态接线锁", () => {
  const bodyOf = (src: string): string => {
    const start = src.indexOf("const loadFactsDiagnostics = useCallback");
    if (start < 0) return "";
    const end = src.indexOf("}, [id, msg]);", start);
    return src.slice(start, end < 0 ? src.length : end);
  };
  const body = PAGE ? bodyOf(PAGE) : "";

  it("前提：能取到被测文件与 loadFactsDiagnostics 函数体（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(body.length).toBeGreaterThan(50);
  });

  it("必须用 Promise.allSettled 独立落定，禁止回退 Promise.all", () => {
    expect(body).toContain("Promise.allSettled(");
    expect(body).not.toContain("await Promise.all([");
  });

  it("必须经 mergeFactsDiagnosticsSettled 合并，且成功侧各自落板", () => {
    expect(body).toContain("mergeFactsDiagnosticsSettled(chaptersRes, dangerRes)");
    expect(body).toContain("if (merged.chapters !== undefined) setFactsChapterReport(merged.chapters);");
    expect(body).toContain("if (merged.danger !== undefined) setFactsDangerReport(merged.danger);");
  });

  it("aborted 必须短路（双侧取消静默返回），旧的整体赋值形态不得回流", () => {
    expect(body).toContain("if (merged.aborted) return;");
    expect(body).not.toContain("setFactsChapterReport(chapters.data)");
    expect(body).not.toContain("setFactsDangerReport(danger.data)");
  });

  it("竞态守卫保留：schemeId 变更后不得落板/告警", () => {
    expect(body).toContain("if (requestedSchemeId !== currentSchemeIdRef.current) return;");
    expect(body).toContain("merged.warning && requestedSchemeId === currentSchemeIdRef.current");
  });
});
