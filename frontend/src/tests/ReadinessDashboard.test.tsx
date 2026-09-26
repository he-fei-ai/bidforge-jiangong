// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { App } from "antd";
import ReadinessDashboard from "../components/review/ReadinessDashboard";
import { complianceApi } from "../api";

// ✅ 与全项目测试约定的内联 mock：避免 jsdom 缺 matchMedia / ResizeObserver 导致渲染报错
if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

// ✅ mock 数据放 vi.hoisted 内（顶层 const 在 vi.mock 工厂闭包外引用会 TDZ）
const mocks = vi.hoisted(() => {
  const OVERVIEW = {
    scheme_id: "s1", scheme_name: "示例方案",
    total: 82, grade: "B", verdict: "建议整改后放行", released: false, blocked: true,
    blockers: [
      { rule_id: "REF-001", title: "引用废止标准 GB 50007-2011", section_title: "第3章", detail: "应更新为现行版本", suggestion: "替换为 2011 版" },
    ],
    dimensions: [
      { key: "completeness", label: "内容完整性", weight: 20, score: 90, penalty: 10, issue_count: 1, block_count: 0 },
      { key: "compliance", label: "规范符合性", weight: 20, score: 70, penalty: 30, issue_count: 3, block_count: 1 },
      { key: "safety", label: "安全措施", weight: 15, score: 80, penalty: 20, issue_count: 1, block_count: 0 },
      { key: "consistency", label: "一致性", weight: 15, score: 85, penalty: 15, issue_count: 1, block_count: 0 },
      { key: "traceability", label: "可追溯性", weight: 15, score: 88, penalty: 12, issue_count: 0, block_count: 0 },
      { key: "deliverability", label: "可交付性", weight: 15, score: 79, penalty: 21, issue_count: 1, block_count: 0 },
    ],
    sources: ["compliance", "consistency"],
    rule_version: "2026.1",
    weakest: "compliance",
    stats: { section_count: 10, leaf_count: 8, generated_count: 8, total_words: 52000, word_budget: 40000, empty_ratio: 20, chart_total: 4, chart_done: 3, standard_db_version: "2026.1", standard_db_checked_at: "2026-09-20T00:00:00" },
    findings: [
      { rule_id: "REF-001", title: "引用废止标准", severity: "block", detail: "应更新为现行版本", mode: "ai", evidence: ["GB 50007-2011 已废止"], suggestion: "替换版本", basis: "建标" },
      { rule_id: "MED-1", title: "缺少安全措施描述", severity: "medium", detail: "第5章无安全措施", mode: "rule", evidence: [], suggestion: "补充", basis: "" },
      { rule_id: "LOW-1", title: "图表缺单位", severity: "low", detail: "部分图表未标单位", mode: "rule", evidence: [], suggestion: "补单位", basis: "" },
    ],
    counts: { block: 1, high: 0, medium: 1, low: 1, total: 3 },
    // overview.created_at：默认与 runs[0] 时间戳一致，用于验证「非 stale」
    created_at: "2026-09-20T10:00:00",
  };
  const RUNS_FRESH = [
    { id: "r2", total: 82, grade: "B", verdict: "ok", blocked: 1, counts: OVERVIEW.counts, created_at: "2026-09-20T10:00:01", rule_version: "2026.1" },
    { id: "r1", total: 70, grade: "B", verdict: "ok", blocked: 0, counts: OVERVIEW.counts, created_at: "2026-09-19T10:00:00", rule_version: "2026.1" },
  ];
  const RULES = {
    items: [
      { rule_id: "R1", title: "标准时效性", severity: "block", mode: "rule", dimension: "compliance", basis: "建标", detail: "标准须为现行", keywords: [], deprecated: false },
    ],
    dimensions: [{ key: "compliance", label: "规范符合性", weight: 20, desc: "核对现行标准", rules: ["R1"] }],
    rule_version: "2026.1",
  };
  return { OVERVIEW, RUNS_FRESH, RULES };
});

vi.mock("../api", () => ({
  complianceApi: {
    runs: vi.fn(async () => ({ data: { items: mocks.RUNS_FRESH } })),
    overview: vi.fn(async () => ({ data: mocks.OVERVIEW })),
    rules: vi.fn(async () => ({ data: mocks.RULES })),
    report: vi.fn(async () => ({ data: { content: "# 整改清单", filename: "报告.md" } })),
  },
}));

beforeEach(() => {
  localStorage.clear();
  (complianceApi.runs as any).mockReset().mockResolvedValue({ data: { items: mocks.RUNS_FRESH } });
  (complianceApi.overview as any).mockReset().mockResolvedValue({ data: mocks.OVERVIEW });
  (complianceApi.rules as any).mockReset().mockResolvedValue({ data: mocks.RULES });
  (complianceApi.report as any).mockReset().mockResolvedValue({
    data: { content: "# 整改清单", filename: "报告.md" },
  });
});
afterEach(() => { cleanup(); localStorage.clear(); });

/** ✅ 中文按钮含空格/换行 → 归一化后匹配，避免 antd Button 渲染差异 */
function btnByText(container: HTMLElement, text: string): HTMLButtonElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return (Array.from(container.querySelectorAll("button")).find(
    (b) => norm(b.textContent).includes(norm(text)),
  ) as HTMLButtonElement | undefined) ?? null;
}

describe("ReadinessDashboard · 交付就绪度总检", () => {
  it("空态：runs 无历史 → 显示「尚未总检」且导出按钮禁用", async () => {
    (complianceApi.runs as any).mockResolvedValueOnce({ data: { items: [] } });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    expect(btnByText(container, "导出整改清单")!.disabled).toBe(true);
  });

  it("一键总检 → 渲染综合评分 / 等级 / 放行结论 / 来源标签", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => {
      const t = container.textContent || "";
      expect(t).toContain("82");
      expect(t).toContain("B 级");
      expect(t).toContain("暂不放行");
      expect(t).toContain("引用废止标准");
      expect(t).toContain("规范符合性（AI）");
      expect(t).toContain("一致性审计（AI）");
    });
    // ✅ G2 契约：「一键总检」以 force=false 调用（服务端可命中同指纹缓存）
    expect(complianceApi.overview).toHaveBeenCalledWith("s1", false);
  });

  it("一键总检后阻断项 Alert + 六维得分卡 + 客观统计齐全", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("阻断"));
    const text = container.textContent || "";
    expect(text).toContain("内容完整性");
    expect(text).toContain("规范符合性");
    expect(text).toContain("安全措施");
    expect(text).toContain("一致性");
    expect(text).toContain("可追溯性");
    expect(text).toContain("可交付性");
    expect(text).toContain("章节");
    expect(text).toContain("总字数");
    expect(text).toContain("空章节占比");
    expect(text).toContain("图表完成");
  });

  it("严重度筛选：默认显示「阻断」置顶（sorter 语义正确）；切「提示」/「阻断+严重」过滤", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("问题清单"));
    // ✅ 语义确认：sorter=`b - a` + defaultSortOrder:'ascend' → 权重大的排前 → 阻断项置顶
    const rows = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"));
    expect(rows.length).toBe(3);
    expect(rows[0].textContent || "").toContain("REF-001");
    // 切到「提示」 → 只剩 LOW-1
    const segItem = (kw: string): HTMLElement => {
      const n = Array.from(container.querySelectorAll("label, .ant-segmented-item"))
        .find((x) => (x.textContent || "").includes(kw));
      if (!n) throw new Error(`未找到分段按钮：${kw}`);
      return n as HTMLElement;
    };
    fireEvent.click(segItem("提示"));
    await waitFor(() => {
      const rows = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"));
      expect(rows.length).toBe(1);
      expect(rows[0].textContent || "").toContain("LOW-1");
    });
    // 切到「阻断+严重」 → 只剩 REF-001
    fireEvent.click(segItem("阻断+严重"));
    await waitFor(() => {
      const rows = Array.from(container.querySelectorAll(".ant-table-tbody .ant-table-row"));
      expect(rows.length).toBe(1);
      expect(rows[0].textContent || "").toContain("REF-001");
    });
  });

  it("问题行展开 → 显示证据 / 整改建议 / 行业依据", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("REF-001"));
    const expander = container.querySelector(".ant-table-tbody .ant-table-row-expand-icon") as HTMLElement;
    expect(expander).toBeTruthy();
    fireEvent.click(expander!);
    await waitFor(() => {
      const t = container.textContent || "";
      expect(t).toContain("证据：");
      expect(t).toContain("GB 50007-2011 已废止");
      expect(t).toContain("整改建议：");
      expect(t).toContain("替换版本");
      expect(t).toContain("行业依据：");
      expect(t).toContain("建标");
    });
  });

  it("规则说明 → 打开抽屉并展示维度 + 权重", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "规则说明")!);
    await waitFor(() => {
      expect(document.body.textContent || "").toContain("审核规则说明");
      expect(document.body.textContent || "").toContain("规范符合性");
      expect(document.body.textContent || "").toContain("权重 20");
    });
    expect(complianceApi.rules).toHaveBeenCalled();
  });

  it("导出整改清单 → 调用 report + 触发下载", async () => {
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const urlSpy = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    try {
      const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
      await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
      fireEvent.click(btnByText(container, "一键总检")!);
      await waitFor(() => expect(container.textContent || "").toContain("82"));
      fireEvent.click(btnByText(container, "导出整改清单")!);
      await waitFor(() => expect(complianceApi.report).toHaveBeenCalledWith("s1"));
      expect(clickSpy).toHaveBeenCalled();
      expect(urlSpy).toHaveBeenCalled();
    } finally {
      clickSpy.mockRestore();
      urlSpy.mockRestore();
    }
  });

  it("历史趋势：runs ≥ 2 → 显示「本次」与历史分数标签", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("历史总检"));
    expect(container.textContent || "").toContain("本次 82");
    expect(container.textContent || "").toContain("#1 70");
  });

  it("STALE 提示：runs 出现比 overview 更新的记录 → warning + 重新总检按钮", async () => {
    // ✅ BUG 修复回归：正文/目录变更后陈旧评分提示
    const FUTURE_RUNS = [
      { id: "r99", total: 55, grade: "C", verdict: "差", blocked: 1, counts: mocks.OVERVIEW.counts,
        created_at: "2026-09-21T20:00:00", rule_version: "2026.1" },
    ];
    (complianceApi.runs as any).mockResolvedValue({ data: { items: FUTURE_RUNS } });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    await waitFor(() => {
      expect(container.textContent || "").toContain("本次总检之后已有更新的运行记录");
    });
    expect(btnByText(container, "重新总检")).toBeTruthy();
  });

  it("总检接口失败 → 不进入结果态（catch 兜底，页面仍显示空态）", async () => {
    (complianceApi.overview as any).mockRejectedValueOnce(new Error("网络异常"));
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    // API 被调用（force=false 与组件契约一致）
    await waitFor(() => expect(complianceApi.overview).toHaveBeenCalledWith("s1", false));
    // 页面没有进入结果态（未渲染「建议整改后放行」等结论）
    await new Promise((r) => setTimeout(r, 50));
    expect(container.textContent || "").not.toContain("建议整改后放行");
    expect(container.textContent || "").not.toContain("B 级");
  });

  it("切换 schemeId → 重新拉取 runs（schemeId 依赖）", async () => {
    render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(complianceApi.runs).toHaveBeenCalledWith("s1", 10));
    const { unmount } = render(<App><ReadinessDashboard schemeId="s2" /></App>);
    await waitFor(() => expect(complianceApi.runs).toHaveBeenCalledWith("s2", 10));
    unmount();
  });

  it("刷新键递增 → 重新拉取 runs", async () => {
    const { container, rerender } = render(<App><ReadinessDashboard schemeId="s1" refreshKey={0} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    const initialCalls = (complianceApi.runs as any).mock.calls.length;
    rerender(<App><ReadinessDashboard schemeId="s1" refreshKey={1} /></App>);
    await waitFor(() => {
      expect((complianceApi.runs as any).mock.calls.length).toBeGreaterThan(initialCalls);
    });
  });

  it("后端明确标记最新运行 stale=true → 主结论立即进入过期态", async () => {
    const STALE_RUNS = [{ ...mocks.RUNS_FRESH[0], stale: true }];
    (complianceApi.runs as any).mockResolvedValue({ data: { items: STALE_RUNS } });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => {
      expect(container.textContent || "").toContain("本次总检对应的正文或图表已发生变化");
    });
    expect(btnByText(container, "重新总检")).toBeTruthy();
  });

  it("切换 schemeId：旧方案迟到的 runs 响应不得覆盖新方案", async () => {
    let resolveOld: ((value: any) => void) | undefined;
    const oldPromise = new Promise<any>((resolve) => { resolveOld = resolve; });
    const newRuns = [
      { ...mocks.RUNS_FRESH[0], id: "new", total: 66 },
      { ...mocks.RUNS_FRESH[1], id: "new-history", total: 50 },
    ];
    (complianceApi.runs as any).mockImplementation((sid: string) =>
      sid === "old" ? oldPromise : Promise.resolve({ data: { items: newRuns } }));
    const { container, rerender } = render(
      <App><ReadinessDashboard schemeId="old" /></App>);
    await waitFor(() => expect(complianceApi.runs).toHaveBeenCalledWith("old", 10));
    rerender(<App><ReadinessDashboard schemeId="new" /></App>);
    await waitFor(() => expect(complianceApi.runs).toHaveBeenCalledWith("new", 10));
    await act(async () => {
      resolveOld?.({ data: { items: [{ ...mocks.RUNS_FRESH[0], id: "old", total: 11 }] } });
      await Promise.resolve();
    });
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    await waitFor(() => expect(container.textContent || "").toContain("本次 66"));
    expect(container.textContent || "").not.toContain("本次 11");
  });

  it("runs 只有 1 条 → 不显示历史趋势区（边界）", async () => {
    (complianceApi.runs as any).mockResolvedValue({
      data: { items: [mocks.RUNS_FRESH[0]] },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    expect(container.textContent || "").not.toContain("历史总检（最近");
  });
});