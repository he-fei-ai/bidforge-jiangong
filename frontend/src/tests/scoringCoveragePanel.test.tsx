// @vitest-environment jsdom
/**
 * ✅ R179 M1（2026-11-05）：评分项 → 目录覆盖校验面板 · 组件级测试（方案 §1.8）。
 *
 * 六条用例与方案一一对应：
 *   1. undetermined 渲染「无法判定」，不出现 0%；
 *   2. partial 渲染「部分来源未解析」提示；
 *   3. missing 行点击「新增建议章节」仅回调注入的创建函数（参数正确），
 *      不发起解析/正文生成类请求（v1 无补齐链路）；
 *   4. extra 行点击定位回调携带正确 section_id；
 *   5. 已覆盖默认折叠，展开后可见；
 *   6. 缺失行只展示信息，无「一键补齐」入口（v1 未提供，拍板结论 #2）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { ScoringCoveragePanel } from "../pages/schemeWorkbench/tabs/ScoringCoveragePanel";
import { scoringApi } from "../api";
import type { ScoringCoverageResponse } from "../types/scoring";

// vitest 未开 globals，RTL 自动清理不生效 —— 显式清理
afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd 会用到（用 unknown 收窄，避免 any）
const jsdomWindow = window as unknown as {
  matchMedia?: (query: string) => unknown;
};
if (!jsdomWindow.matchMedia) {
  jsdomWindow.matchMedia = (query: string) => ({
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
const jsdomGlobal = globalThis as unknown as {
  ResizeObserver?: new () => { observe(): void; unobserve(): void; disconnect(): void };
};
if (!jsdomGlobal.ResizeObserver) {
  jsdomGlobal.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

vi.mock("../api", () => ({
  scoringApi: {
    extract: vi.fn(),
    listItems: vi.fn(),
    coverage: vi.fn(),
  },
}));

const mockedCoverage = vi.mocked(scoringApi.coverage);
const mockedExtract = vi.mocked(scoringApi.extract);

const MAPPED_ITEM = {
  id: "item-1",
  item_no: "1.1",
  item_name: "施工组织设计总体安排",
  score: 20,
  score_raw: "20分",
  score_band: "high" as const,
  is_primary: true,
  page_ref: "doc-1#page:5",
  page_num: 5,
  source_doc_id: "doc-1",
  source_quote: "施工组织设计 20分",
  criteria: "",
  status: "mapped" as const,
  confidence: 0.95,
  method: "exact" as const,
  section_id: "sec-1",
  section_title: "施工组织设计总体安排",
  matched_sections: [],
  also_section_ids: [],
  needs_review: false,
  suggestion: null,
  reason: "",
};

const MISSING_ROW = {
  item_id: "item-2",
  item_no: "2.1",
  name: "安全保证措施",
  score: 10,
  score_raw: "10分",
  suggestion: {
    title: "安全保证措施",
    level: 3,
    parent_id: "sec-parent",
    parent_title: "安全管理",
  },
  page_num: 12,
  page_ref: "doc-1#page:12",
  reason: "低于关键词匹配阈值",
};

const COVERAGE_OK: ScoringCoverageResponse = {
  scheme_id: "s1",
  project_id: "p1",
  status: "ok",
  undetermined_reason: null,
  extraction: {
    run_id: "run-1",
    status: "success",
    docs_total: 2,
    docs_ok: 2,
    failed_sources: [],
    items_count: 2,
    error: "",
    updated_at: "2026-11-05 10:00:00",
  },
  summary: {
    total_items: 2,
    mapped: 1,
    missing: 1,
    extra: 1,
    unmatched: 0,
    coverage_rate: 0.5,
    undetermined_items: 0,
  },
  coverage_rate: 0.5,
  items: [MAPPED_ITEM],
  missing: [MISSING_ROW],
  extra: [
    {
      section_id: "sec-x",
      title: "附录杂项",
      level: 3,
      path: "第一章 > 工程概况 > 附录杂项",
      parent_id: "sec-p",
      status: "extra",
      reason: "叶子章节且无评分项支撑",
    },
  ],
  unmatched: [],
  overloaded_sections: [],
  diagnostics: { run_status: "success", thresholds: { exact: 1, contains: 0.6, keyword: 0.35 } },
};

const COVERAGE_UNDETERMINED: ScoringCoverageResponse = {
  ...COVERAGE_OK,
  status: "undetermined",
  undetermined_reason: "no_extraction",
  extraction: null,
  summary: {
    total_items: 0,
    mapped: 0,
    missing: 0,
    extra: 0,
    unmatched: 0,
    coverage_rate: null,
    undetermined_items: 0,
  },
  coverage_rate: null,
  items: [],
  missing: [],
  extra: [],
  unmatched: [],
};

const COVERAGE_PARTIAL: ScoringCoverageResponse = {
  ...COVERAGE_OK,
  status: "partial",
  unmatched: [{ doc_id: "doc-2", file_name: "商务标.pdf", reason: "解析超时" }],
};

function msgStub() {
  return { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() } as never;
}

function renderPanel(overrides: Partial<Record<string, unknown>> = {}) {
  const onLocate = vi.fn();
  const onCreateSuggested = vi.fn().mockResolvedValue(undefined);
  const { container } = render(
    <ScoringCoveragePanel
      schemeId="s1"
      projectId="p1"
      msg={msgStub()}
      onLocate={onLocate}
      onCreateSuggested={onCreateSuggested}
      {...overrides}
    />,
  );
  return { container, onLocate, onCreateSuggested };
}

describe("ScoringCoveragePanel · 评分项覆盖校验（R179 M1 §1.8）", () => {
  it("用例1：undetermined 渲染「无法判定」，不出现 0%，不显示覆盖率", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_UNDETERMINED } as never);
    const { container } = renderPanel();
    await waitFor(() => expect(mockedCoverage).toHaveBeenCalledWith("s1"));
    await waitFor(() => expect(screen.getByTestId("scoring-undetermined")).toBeTruthy());
    expect(container.textContent).toContain("无法判定（未解析到评分项）");
    // 不显示覆盖率百分比（拍板：undetermined 不显示 0%）
    expect(container.textContent).not.toContain("%");
    expect(screen.queryByTestId("scoring-coverage-rate")).toBeNull();
  });

  it("用例2：partial 渲染「部分来源未解析」状态与未解析来源明细", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_PARTIAL } as never);
    const { container } = renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("scoring-status-tag").textContent).toContain("部分来源未解析"),
    );
    expect(container.textContent).toContain("商务标.pdf");
    expect(container.textContent).toContain("解析超时");
  });

  it("用例3：missing 行「新增建议章节」仅回调创建函数且参数正确，不发起解析/补齐请求", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_OK } as never);
    const { onCreateSuggested } = renderPanel();
    await waitFor(() => expect(screen.getByTestId("scoring-add-suggested-item-2")).toBeTruthy());
    fireEvent.click(screen.getByTestId("scoring-add-suggested-item-2"));
    await waitFor(() =>
      expect(onCreateSuggested).toHaveBeenCalledWith("安全保证措施", "sec-parent", 3),
    );
    // v1 无补齐链路：不触发解析，也不存在任何一键补齐类请求
    expect(mockedExtract).not.toHaveBeenCalled();
  });

  it("用例4：extra 行点击定位，回调携带正确 section_id", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_OK } as never);
    const { onLocate } = renderPanel();
    await waitFor(() => expect(screen.getByTestId("scoring-locate-sec-x")).toBeTruthy());
    fireEvent.click(screen.getByTestId("scoring-locate-sec-x"));
    expect(onLocate).toHaveBeenCalledWith("sec-x");
  });

  it("用例5：已覆盖默认折叠，展开后明细可见", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_OK } as never);
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByTestId("scoring-mapped-collapse")).toBeTruthy());
    // 默认折叠：明细行不可见
    expect(container.textContent).not.toContain("施工组织设计总体安排");
    // 展开后可见
    fireEvent.click(
      container.querySelector(
        '[data-testid="scoring-mapped-collapse"] .ant-collapse-header',
      ) as Element,
    );
    await waitFor(() => expect(container.textContent).toContain("施工组织设计总体安排"));
    expect(container.textContent).toContain("→ 施工组织设计总体安排");
  });

  it("用例6：缺失行只展示信息，无「一键补齐」入口（拍板 #2：v1 不提供）", async () => {
    mockedCoverage.mockResolvedValue({ data: COVERAGE_OK } as never);
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByTestId("scoring-missing-list")).toBeTruthy());
    expect(container.textContent).toContain("安全保证措施");
    expect(container.textContent).not.toContain("一键补齐");
    expect(container.textContent).not.toContain("补齐");
  });
});
