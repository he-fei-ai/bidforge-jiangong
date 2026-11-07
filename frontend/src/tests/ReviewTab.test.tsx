// @vitest-environment jsdom
/**
 * R183（2026-11-07）· 审核与预检 Tab（ReviewTab）组件级交互测试。
 *
 * 此前 ReviewTab 没有任何直接测试，两个「没检查 → 显示没问题」的可见出口
 * 完全无护栏：
 *   1. 质量自检 checked=0（无正文可检）→ 旧 UI 绿色「正文可直接交付」；
 *   2. 导出预检已发现问题、但章节/图表齐全 → 旧 UI 绿色「可前往导出页生成文档」。
 * 本文件锁定修复后的三态口径：无法判定必须显式展示，不得折叠成通过。
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render } from "@testing-library/react";
import { App } from "antd";

vi.mock("../components/review/ReadinessDashboard", () => ({
  default: () => <div data-testid="readiness-stub" />,
}));
vi.mock("../components/review/ReviewWorkflowPanel", () => ({
  default: () => <div data-testid="workflow-stub" />,
}));
vi.mock("../components/PlaceholderReportPanel", () => ({
  default: () => <div data-testid="placeholder-stub" />,
}));
vi.mock("../pages/schemeWorkbench/NumberingConsistencyPanel", () => ({
  NumberingConsistencyPanel: () => <div data-testid="numbering-stub" />,
}));

import { ReviewTab, type ReviewTabProps } from "../pages/schemeWorkbench/tabs/ReviewTab";

beforeAll(() => {
  if (!(window as any).matchMedia) {
    (window as any).matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
  if (!(globalThis as any).ResizeObserver) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

function baseProps(over: Partial<ReviewTabProps> = {}): ReviewTabProps {
  const noop = vi.fn();
  const asyncNoop = vi.fn(async () => {});
  return {
    id: "scheme-r183",
    load: asyncNoop,
    reviewTick: 0,
    setReviewTick: noop,
    checklistText: "",
    setChecklistText: noop,
    setChecklistDirty: noop,
    aiRules: [],
    checkingCompliance: false,
    complianceResults: [],
    setComplianceResults: noop,
    complianceHistory: [],
    historyLoading: false,
    loadComplianceHistory: asyncNoop,
    handleComplianceCheck: asyncNoop,
    handleCancelComplianceCheck: noop,
    consistencyResult: null,
    auditRunning: false,
    loadConsistencyLatest: asyncNoop,
    handleConsistencyAudit: asyncNoop,
    handleCancelConsistencyAudit: noop,
    expertResult: null,
    expertItems: [],
    expertRunning: false,
    handleExpertReview: asyncNoop,
    handleCancelExpertReview: noop,
    exportIssues: [],
    setExportIssues: noop,
    exportStats: null,
    setExportStats: noop,
    exportPreflight: null,
    setExportPreflight: noop,
    placeholderReport: null,
    setPlaceholderReport: noop,
    rerunPlan: null,
    setRerunPlan: noop,
    checkingExport: false,
    exportCheckIncomplete: false,
    handleExportCheck: asyncNoop,
    handlePlaceholderJump: noop,
    rerunning: false,
    handleRerunAffected: asyncNoop,
    qualityResult: null,
    setQualityResult: noop,
    checkingQuality: false,
    handleQualityCheck: asyncNoop,
    ...over,
  };
}

function renderTab(over: Partial<ReviewTabProps> = {}) {
  return render(
    <App>
      <ReviewTab {...baseProps(over)} />
    </App>,
  );
}

describe("ReviewTab · 正文质量自检三态", () => {
  it("checked=0 且零问题 → 显示「无法判定」，不得声称正文可直接交付", () => {
    const { container } = renderTab({
      qualityResult: {
        summary: { checked: 0, problem_sections: 0, colloquial: 0, deprecated: 0 },
        items: [],
      } as any,
    });
    const text = container.textContent || "";
    expect(text).toContain("没有可检查的正文");
    expect(text).toContain("无法判定");
    expect(text).not.toContain("正文可直接交付");
  });

  it("checked>0 且 unchecked=0 → 才允许绿色「可直接交付」，并显示 X / 共 Y", () => {
    const { container } = renderTab({
      qualityResult: {
        summary: {
          checked: 12,
          section_total: 12,
          unchecked: 0,
          problem_sections: 0,
          colloquial: 0,
          deprecated: 0,
        },
        items: [],
      } as any,
    });
    const text = container.textContent || "";
    expect(text).toContain("正文可直接交付");
    // ✅ R184：分母取后端 section_total，不在前端另算
    expect(text).toContain("12 / 12");
  });

  it("仍有 unchecked 章节 → 不得绿色通过，必须报「未纳入检查」", () => {
    const { container } = renderTab({
      qualityResult: {
        summary: {
          checked: 12,
          section_total: 20,
          unchecked: 8,
          problem_sections: 0,
          colloquial: 0,
          deprecated: 0,
        },
        items: [],
      } as any,
    });
    const text = container.textContent || "";
    expect(text).not.toContain("正文可直接交付");
    expect(text).toContain("仍有 8 章没有正文");
    expect(text).toContain("无法判定");
  });
});

describe("ReviewTab · 导出预检结论三态", () => {
  const STATS = {
    total_words: 10000,
    section_count: 8,
    generated_count: 8,
    empty_ratio: 0,
    chart_total: 3,
    chart_done: 3,
    crossref_check: { undetermined: false, reason: "", fig_count: 3, tbl_count: 2 },
  } as any;

  it("章节/图表齐全但仍有 2 个预检问题 → 不得显示「可前往导出页」绿态", () => {
    const { container } = renderTab({
      exportStats: STATS,
      exportIssues: [
        { type: "low_word_count", title: "字数不足" },
        { type: "stale_cross_reference", title: "失效引用" },
      ] as any,
    });
    const text = container.textContent || "";
    expect(text).toContain("本次预检仍发现 2 个问题");
    expect(text).not.toContain("未检出其他问题，可前往");
  });

  it("零问题且已就绪 → 显示可前往导出页", () => {
    const { container } = renderTab({ exportStats: STATS, exportIssues: [] });
    expect(container.textContent || "").toContain("未检出其他问题，可前往");
  });

  it("只有表却无图 → crossref 无法判定且原因文案准确", () => {
    const { container } = renderTab({
      exportStats: {
        ...STATS,
        chart_total: 0,
        chart_done: 0,
        crossref_check: { undetermined: true, reason: "no_fig_set", fig_count: 0, tbl_count: 2 },
      } as any,
    });
    const node = container.querySelector('[data-testid="export-crossref-undetermined"]');
    expect(node).toBeTruthy();
    expect(node!.textContent || "").toContain("方案无图（仅有表）");
    expect(node!.textContent || "").toContain("无法判定");
  });
});
