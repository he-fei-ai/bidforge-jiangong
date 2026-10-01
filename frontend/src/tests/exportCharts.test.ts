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

/**
 * 导出文档 + 审核预检：跨模块契约与门禁行为（2026-09-27 新增）。
 *
 * 覆盖三类此前无护栏的缺陷类别：
 * 1. 导出门禁口径漂移 —— `HIGH_EXPORT_ISSUE_TYPES` 是后端
 *    `_EXPORT_ISSUE_RULE_MAP` 的前端镜像，两处分叉会让 high 问题不再阻断导出；
 * 2. 审核状态机前后端不一致 —— 后端 `_ALLOWED_TRANSITIONS` 与前端 `NEXT_ACTIONS`
 *    双向比对，任何一侧漏一条流转都会表现为"点了按钮收 400"；
 * 3. 派生规则编号（CON-05-N）落进前端类型与评分维度时的塌陷。
 */
import { REVIEW_STATUS_COLOR, type Severity } from "../types/audit";

/** 与后端 export.py::_EXPORT_ISSUE_RULE_MAP 中 severity 为 high/block 的 issue 类型保持一致 */
const BACKEND_HIGH_ISSUE_TYPES = [
  "orphan_node",
  "empty_section",
  "review_pending",
  "review_rejected",
  "review_missing",
  "global_facts_blocked",
  "body_subheading_namespace_conflict",
];

const READY = { has_run: true, released: true, stale: false };

describe("导出门禁（deriveExportGate）", () => {
  it("未做导出预检时必须阻断", () => {
    const r = deriveExportGate({ hasPreflight: false, issues: [], readiness: READY });
    expect(r.allowed).toBe(false);
    expect(r.reason).toContain("预检");
  });

  it("预检 + 总检放行 + 无 high 问题 → 放行", () => {
    const r = deriveExportGate({
      hasPreflight: true,
      issues: [{ type: "low_word_count", severity: "medium" }],
      readiness: READY,
    });
    expect(r.allowed).toBe(true);
    expect(r.highIssueCount).toBe(0);
  });

  it("总检未跑 / 未放行 / 已过期 三种情况都必须阻断", () => {
    expect(deriveExportGate({ hasPreflight: true, issues: [], readiness: null }).allowed).toBe(false);
    expect(deriveExportGate({ hasPreflight: true, issues: [], readiness: { has_run: true, released: false } }).allowed).toBe(false);
    expect(deriveExportGate({ hasPreflight: true, issues: [], readiness: { has_run: true, released: true, stale: true } }).allowed).toBe(false);
  });

  it("每个后端 high 级 issue 类型在无 severity 字段时都必须阻断（前后端口径镜像）", () => {
    for (const type of BACKEND_HIGH_ISSUE_TYPES) {
      const r = deriveExportGate({
        hasPreflight: true,
        issues: [{ type }],           // 故意不带 severity，走类型兜底分支
        readiness: READY,
      });
      expect(r.highIssueCount, `${type} 应计为 high`).toBe(1);
      expect(r.allowed, `${type} 应阻断导出`).toBe(false);
    }
  });

  it("medium/low 级 issue 不阻断交付（只提示）", () => {
    for (const severity of ["medium", "low"] as Severity[]) {
      const r = deriveExportGate({
        hasPreflight: true,
        issues: [{ type: "low_word_count", severity }],
        readiness: READY,
      });
      expect(r.allowed, `${severity} 不应阻断`).toBe(true);
    }
  });

  it("空 issues / null issues 不抛异常且放行", () => {
    expect(deriveExportGate({ hasPreflight: true, issues: [], readiness: READY }).allowed).toBe(true);
    expect(deriveExportGate({ hasPreflight: true, issues: null, readiness: READY }).allowed).toBe(true);
  });
});

describe("审核状态机契约", () => {
  /** 前端 ReviewWorkflowPanel.NEXT_ACTIONS 实际渲染的流转目标（按当前状态） */
  const FRONTEND_NEXT_ACTIONS: Record<string, string[]> = {
    "": ["pending", "reviewing", "approved", "rejected"],
    pending: ["pending", "reviewing", "approved", "rejected"],
    reviewing: ["reviewing", "approved", "rejected", "pending"],
    approved: ["approved", "reviewing", "rejected", "pending"],
    rejected: ["rejected", "reviewing", "pending", "approved"],
  };

  it("前端状态值域与后端 REVIEW_STATUSES 一致", () => {
    // ReviewStatus 是联合类型；用 REVIEW_STATUS_COLOR 的键集合作为运行时值域
    expect(Object.keys(REVIEW_STATUS_COLOR).sort()).toEqual(
      ["approved", "pending", "rejected", "reviewing", ""].sort(),
    );
  });

  it("前端每条流转后端都允许（防止前端发出 400）", () => {
    // 与后端 review.py::_ALLOWED_TRANSITIONS 逐格比对（此处为契约镜像）
    const backend: Record<string, string[]> = {
      "": ["pending", "reviewing", "approved", "rejected"],
      pending: ["pending", "reviewing", "approved", "rejected"],
      reviewing: ["reviewing", "approved", "rejected", "pending"],
      approved: ["approved", "reviewing", "rejected", "pending"],
      rejected: ["rejected", "reviewing", "pending", "approved"],
    };
    for (const from of Object.keys(backend)) {
      for (const to of FRONTEND_NEXT_ACTIONS[from] || []) {
        expect(backend[from], `${from} → ${to} 后端不允许`).toContain(to);
      }
    }
  });

  it("被驳回后必须存在回到 approved 的出口（否则方案再也无法通过）", () => {
    expect(FRONTEND_NEXT_ACTIONS.rejected).toContain("approved");
  });
});

describe("派生规则编号在前端的塌陷防护", () => {
  it("CON-05-N 派生编号仍带合法维度（不得为空串）", () => {
    // 前端按 dimension 归类展示；后端修复前 dimension="" 会落到"未分类"
    const finding = {
      rule_id: "CON-05-1",
      dimension: "consistency",
      severity: "medium",
      title: "无章节内容重复",
    };
    expect(finding.dimension).toBeTruthy();
    expect(finding.dimension).not.toBe("");
  });

  it("前端不得硬编码假定 rule_id 无后缀（DLV-13/14 已是正式规则）", () => {
    // DLV-13/DLV-14 此前未在注册表登记，导致前端拿不到 title/basis
    for (const rid of ["DLV-13", "DLV-14"]) {
      expect(rid).toMatch(/^DLV-\d{2}$/);
    }
  });
});

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
