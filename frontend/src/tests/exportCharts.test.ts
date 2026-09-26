/**
 * exportCharts P1-5 单测：相同 code 去重 + 并发渲染（上限 4）+ JSON/空载荷跳过。
 *
 * 说明：svgToPng 依赖浏览器 DOM（DOMParser/Image/canvas），vitest 为 node 环境，
 * 渲染到 svgToPng 会抛错并被单图 catch 吞掉——因此本组测试以
 * ensureMermaid().render / nextMermaidId 的调用次数验证去重与重试逻辑，
 * 不验证 PNG 产物内容。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const renderSpy = vi.fn(async () => ({ svg: "<svg></svg>" }));

vi.mock("../components/mermaidRuntime", () => ({
  ensureMermaid: vi.fn(async () => ({ render: renderSpy })),
  nextMermaidId: vi.fn(() => "test-id"),
}));

import { deriveExportGate, renderChartsForExport } from "../utils/exportCharts";
import { ensureMermaid, nextMermaidId } from "../components/mermaidRuntime";

describe("renderChartsForExport (P1-5)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    renderSpy.mockClear();
  });

  it("相同 code 去重：只渲染一次，进度口径保持为已处理条目数", async () => {
    const items = [
      { chart_type: "flowchart", mermaid_code: "graph TD\n  A-->B" },
      { chart_type: "flowchart", mermaid_code: "graph TD\n  A-->B" }, // 与上重复
      { chart_type: "flowchart", mermaid_code: "graph TD\n  C-->D" },
    ];
    const progress: number[] = [];
    const out = await renderChartsForExport(items, (d, total) => progress.push(d));

    // 去重后唯一 code = 2 个；每个失败重试 1 次 → render 调用 4 次
    expect(renderSpy).toHaveBeenCalledTimes(4);
    expect(vi.mocked(nextMermaidId)).toHaveBeenCalledTimes(4);
    // 若不去重，3 个条目 × 2 次重试 = 6 次
    expect(vi.mocked(ensureMermaid)).toHaveBeenCalledTimes(1);
    // 进度：3 个条目各回调一次，末值为总数
    expect(progress).toEqual([1, 2, 3]);
    // node 无 DOM，svgToPng 失败被吞 → 空数组但不抛错
    expect(Array.isArray(out)).toBe(true);
  });

  it("JSON 数据载荷跳过渲染（后端 PIL 兜底）", async () => {
    const items = [{ chart_type: "layout", mermaid_code: '{"type":"layout"}' }];
    const out = await renderChartsForExport(items);
    expect(renderSpy).not.toHaveBeenCalled();
    expect(out).toEqual([]);
  });

  it("空 code 跳过渲染", async () => {
    const items = [{ chart_type: "flowchart", mermaid_code: "  " }];
    const out = await renderChartsForExport(items);
    expect(renderSpy).not.toHaveBeenCalled();
    expect(out).toEqual([]);
  });

  it("并发渲染不互相干扰：10 个唯一 code 全部处理完且无异常", async () => {
    const items = Array.from({ length: 10 }, (_, i) => ({
      chart_type: "flowchart",
      mermaid_code: `graph TD\n  N${i}-->M${i}`,
    }));
    const out = await renderChartsForExport(items);
    // 10 个唯一 code × 2 次重试（svgToPng 失败）= 20 次
    expect(renderSpy).toHaveBeenCalledTimes(20);
    expect(Array.isArray(out)).toBe(true);
  });


  it("取消信号会中断尚未完成的 Mermaid 渲染", async () => {
    let resolveRender: ((value: { svg: string }) => void) | undefined;
    renderSpy.mockImplementationOnce(() => new Promise((resolve) => { resolveRender = resolve; }));
    const controller = new AbortController();
    const pending = renderChartsForExport(
      [{ chart_type: "flowchart", mermaid_code: "graph TD\nA-->B" }],
      undefined,
      controller.signal,
    );
    await vi.waitFor(() => expect(renderSpy).toHaveBeenCalledTimes(1));
    controller.abort();
    resolveRender?.({ svg: "<svg></svg>" });
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("导出门禁：未预检时阻止", () => {
    expect(deriveExportGate({
      hasPreflight: false,
      readiness: { has_run: true, released: true },
    })).toMatchObject({ allowed: false, highIssueCount: 0 });
  });

  it("导出门禁：high 问题按后端类型映射并阻止", () => {
    const result = deriveExportGate({
      hasPreflight: true,
      issues: [{ type: "empty_section" }, { type: "low_word_count" }],
      readiness: { has_run: true, released: true },
    });
    expect(result).toMatchObject({ allowed: false, highIssueCount: 1 });
  });

  it("导出门禁：就绪度未执行、过期或未放行均阻止", () => {
    const base = { hasPreflight: true, issues: [] };
    expect(deriveExportGate({ ...base, readiness: { has_run: false } }).allowed).toBe(false);
    expect(deriveExportGate({ ...base, readiness: { has_run: true, released: true, stale: true } }).allowed).toBe(false);
    expect(deriveExportGate({ ...base, readiness: { has_run: true, released: false } }).allowed).toBe(false);
  });

  it("导出门禁：预检完成、仅 medium 问题且就绪度放行时允许", () => {
    expect(deriveExportGate({
      hasPreflight: true,
      issues: [{ type: "low_word_count" }, { type: "chart_failed" }],
      readiness: { has_run: true, released: true, stale: false },
    })).toMatchObject({ allowed: true, highIssueCount: 0 });
  });
});
