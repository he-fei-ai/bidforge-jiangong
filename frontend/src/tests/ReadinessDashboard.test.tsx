// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { App } from "antd";
import ReadinessDashboard from "../components/review/ReadinessDashboard";
import { complianceApi, reviewAutoFixApi } from "../api";

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

// ✅ 2026-10-06：组件的 msg 来自项目自封装 useAntdMessageHub（同时推 ActivityCenter）。
//    此前本仓无任何用例断言告警文案 —— stale 提醒 / cached 提示 / 维度漂移告警
//    三条「静默失效」链路因此完全无锁。mock 该 hook 即可精确断言，不必依赖
//    antd message 的 DOM portal（jsdom 下不稳定）。
const msgSpy = vi.hoisted(() => ({
  success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn(), loading: vi.fn(),
  // ⚠️ hub 代理缓存必须与 msgSpy 同处 vi.hoisted —— vi.mock 工厂被提升到文件顶部，
  //    引用外层 const 会在初始化时命中 TDZ。
  hubCache: new Map<string, any>(),
}));
vi.mock("../utils/activityCenter", async (orig) => {
  const real = await orig<any>();
  // 包裹真实 hub：既记录文案，又保留 pushActivity（ActivityCenter 仍能收到告警）
  return {
    ...real,
    // ⚠️ 必须返回**同一对象**：组件的 useCallback/effect 以 msg 为依赖，
    //    每次渲染都造新对象会导致依赖永变 → 无限重渲染（本轮实测卡死）。
    //    真实 hub 每次仍调用（保持 hook 顺序），但包装后的代理按 source 缓存。
    useAntdMessageHub: (m: any, source: string) => {
      const hub = real.useAntdMessageHub(m, source);
      let w = msgSpy.hubCache.get(source);
      if (!w) {
        w = {
          success: (...a: any[]) => { msgSpy.success(...a); return hub.success(...a); },
          error: (...a: any[]) => { msgSpy.error(...a); return hub.error(...a); },
          warning: (...a: any[]) => { msgSpy.warning(...a); return hub.warning(...a); },
          info: (...a: any[]) => { msgSpy.info(...a); return hub.info(...a); },
          loading: (...a: any[]) => { msgSpy.loading(...a); return hub.loading(...a); },
        };
        msgSpy.hubCache.set(source, w);
      }
      return w;
    },
  };
});

vi.mock("../api", () => ({
  complianceApi: {
    runs: vi.fn(async () => ({ data: { items: mocks.RUNS_FRESH } })),
    overview: vi.fn(async () => ({ data: mocks.OVERVIEW })),
    rules: vi.fn(async () => ({ data: mocks.RULES })),
    report: vi.fn(async () => ({ data: { content: "# 整改清单", filename: "报告.md" } })),
  },
  // ✅ ReadinessDashboard 现挂载 AutoFixModal，后者会 import 本 API；
  //    mock 工厂缺该导出会在导入期直接抛错（vitest 对缺失导出即报错）。
  reviewAutoFixApi: {
    plan: vi.fn(async () => ({ data: { ok: true, fixable: true, mode: "ai", reason: "", finding: {}, targets: [] } })),
    apply: vi.fn(async () => ({ data: { ok: true, status: "repaired", mode: "ai", rule_id: "", targets: [], items: [], snapshot_id: "ver-1" } })),
    rollback: vi.fn(async () => ({ data: { status: "rolled_back" } })),
    capabilities: vi.fn(async () => ({ data: { items: [], total: 0 } })),
    // ✅ 2026-10-03 遗留收口：Dashboard 级入口测试需要批量链路三端点在位
    //（BatchFixModal 挂载即 collect；stage/confirm 仅防未调用期报错）
    collect: vi.fn(async () => ({ data: { scheme_id: "s1", scope: "all_blocking", total: 0, items: [] } })),
    stage: vi.fn(async () => ({ data: { batch_id: "", status: "empty", items: [],
      stats: { repaired: 0, failed: 0, skipped: 0 } } })),
    confirm: vi.fn(async () => ({ data: { status: "confirmed", accepted: 0,
      repaired_sections: 0, snapshot_id: "", batch_id: "" } })),
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
  // ✅ 2026-10-03：Dashboard 级入口用例逐项断言调用参数，每例重置防串扰
  (reviewAutoFixApi.plan as any).mockReset().mockResolvedValue({
    data: { ok: true, fixable: true, mode: "ai", reason: "", finding: {}, targets: [] } });
  (reviewAutoFixApi.collect as any).mockReset().mockResolvedValue({
    data: { scheme_id: "s1", scope: "all_blocking", total: 0, items: [] } });
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  msgSpy.success.mockClear(); msgSpy.error.mockClear();
  msgSpy.warning.mockClear(); msgSpy.info.mockClear();
});

/** 断言某条告警文案确实推给用户（任一级别） */
function expectMsg(fn: ReturnType<typeof vi.fn>, frag: string) {
  const hit = fn.mock.calls.some((c: any[]) => String(c[0] ?? "").includes(frag));
  expect(hit, `未捕获到含「${frag}」的告警；实际调用：${JSON.stringify(fn.mock.calls)}`).toBe(true);
}

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

// =========================================================================
// Dashboard 级「自动修复」入口交互（2026-10-03 遗留收口）：
//   此前组件契约由 AutoFixModal / BatchFixModal 各自的独立测试锁定，但
//   「问题行的按钮能否打开弹窗、弹窗参数是否取自该行 finding」这层
//   宿主接线无人锁 —— 接线断了（如 setFixTarget 拿错行、批量按钮门控
//   条件变宽）两个子组件测试全绿也不会红。
// =========================================================================
describe("ReadinessDashboard · 自动修复入口接线", () => {
  /** 带 autofix 能力标注的总检结果（能力字段由后端 capability_summary 补充） */
  const withAutofix = (first: any) => ({
    ...mocks.OVERVIEW,
    findings: mocks.OVERVIEW.findings.map((f: any, i: number) =>
      (i === 0 ? { ...f, autofix: first } : f)),
  });

  it("可修项行内「自动修复」→ 打开 AutoFixModal，定位按钮按该行 rule_id 调 plan", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: withAutofix({ fixable: true, mode: "auto", reason: "" }),
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    const fixBtn = btnByText(container, "自动修复");
    expect(fixBtn).toBeTruthy();
    fireEvent.click(fixBtn!);
    // Modal 挂 body：标题取自被点行的 finding.title，并展示「先定位再修复」入口
    const body = document.body as HTMLElement;
    await waitFor(() =>
      expect((body.textContent || "").includes("自动修复：引用废止标准")).toBe(true));
    const locateBtn = btnByText(body, "定位矛盾位置");
    expect(locateBtn).toBeTruthy();
    fireEvent.click(locateBtn!);
    await waitFor(() => expect(reviewAutoFixApi.plan).toHaveBeenCalledWith(
      "s1", { rule_id: "REF-001", section_id: undefined }));
  });

  it("不可修项显示「需人工」且无行内按钮；无 block+fixable 项时批量按钮不出现", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: withAutofix({ fixable: false, mode: "manual", reason: "需人工核对引用" }),
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    expect(container.textContent || "").toContain("需人工");
    expect(btnByText(container, "自动修复")).toBeNull();
    // 唯一 block 项不可修 → blockingFixable 为空 → 批量入口门控生效
    expect(btnByText(container, "一键修复全部阻断项")).toBeNull();
  });

  it("存在 block+fixable 项 → 批量按钮带计数，点击后 BatchFixModal 发起 collect", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: withAutofix({ fixable: true, mode: "auto", reason: "" }),
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    const batchBtn = btnByText(container, "一键修复全部阻断项（1）");
    expect(batchBtn).toBeTruthy();
    fireEvent.click(batchBtn!);
    await waitFor(() => expect(reviewAutoFixApi.collect).toHaveBeenCalledWith(
      "s1", { scope: "all_blocking" }));
  });

  // =========================================================================
  // 组件级交互补漏（2026-10-04 · 用户任务 步骤 4）：
  // 「批量修复成功 → 父组件 runOverview(true) 强制重算」这条接线此前无人锁。
  // 场景背景：服务端总检有内容指纹缓存，正文被 BatchFixModal 改写后不 force
  // 会命中旧缓存 → 用户看到的评分是"修复前"的旧分。此测试是这条契约的护栏。
  // =========================================================================
  it("BatchFixModal onFixed → 父组件强制 runOverview(true) 重算", async () => {
    // 让批量链路走通到底：collect 有 1 项、stage 有 1 项、confirm 返回 snapshot_id
    (reviewAutoFixApi.collect as any).mockResolvedValue({
      data: {
        scheme_id: "s1", scope: "all_blocking", total: 1,
        items: [{
          rule_id: "DLV-05", dimension: "deliverability", severity: "block",
          title: "控制字符", detail: "含控制字符", evidence: [],
          section_id: "", section_title: "", suggestion: "", basis: "", mode: "program",
          autofix: { fixable: true, mode: "auto", reason: "" },
          targets: [{ section_id: "sec-a", section_title: "工程概况", value: "x07",
                      line: 3, sentence_idx: 1, sentence_total: 2, matched: "x07",
                      context: "正常内容", why: "命中" }],
        }],
      },
    });
    (reviewAutoFixApi.stage as any).mockResolvedValue({
      data: {
        batch_id: "b-1", status: "pending_confirm",
        items: [{
          rule_id: "DLV-05", section_id: "sec-a", section_title: "工程概况",
          mode: "auto", status: "repaired", reason: "", targets: [],
          before: "正常x07内容", after: "正常内容", problems: [],
          chain_index: 0, sentence_idx: 0, sentence_total: 0,
        }],
        stats: { repaired: 1, failed: 0, skipped: 0 },
      },
    });
    (reviewAutoFixApi.confirm as any).mockResolvedValue({
      data: { status: "confirmed", accepted: 1, repaired_sections: 1,
              snapshot_id: "ver-b", batch_id: "b-1" },
    });
    const onContentFixed = vi.fn();
    (complianceApi.overview as any).mockResolvedValue({
      data: withAutofix({ fixable: true, mode: "auto", reason: "" }),
    });

    const { container } = render(
      <App><ReadinessDashboard schemeId="s1" onContentFixed={onContentFixed} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(container.textContent || "").toContain("82"));

    // 打开批量弹窗
    fireEvent.click(btnByText(container, "一键修复全部阻断项（1）")!);
    await waitFor(() => expect(reviewAutoFixApi.collect).toHaveBeenCalled());
    // 生成预览
    await waitFor(() =>
      expect(btnByText(document.body as HTMLElement, "生成修复预览")!.disabled).toBe(false));
    fireEvent.click(btnByText(document.body as HTMLElement, "生成修复预览")!);
    await waitFor(() =>
      expect(reviewAutoFixApi.stage).toHaveBeenCalledWith("s1", { scope: "all_blocking" }));
    // 确认修复 → BatchFixModal 触发 onFixed(snapshot_id)
    fireEvent.click(btnByText(document.body as HTMLElement, "确认修复")!);
    await waitFor(() =>
      expect(reviewAutoFixApi.confirm).toHaveBeenCalledWith("s1", {
        batch_id: "b-1", accept_all: true,
      }));

    // 核心契约：宿主页 onContentFixed 被通知，且 overview 以 force=true 重算
    await waitFor(() => expect(onContentFixed).toHaveBeenCalled());
    const overviewCalls = (complianceApi.overview as any).mock.calls as any[];
    expect(overviewCalls.some((c) => c[0] === "s1" && c[1] === true)).toBe(true),
      "BatchFixModal 修复完成后必须 force=true 重算，避免命中旧缓存（拿到修复前旧分）";
  });
});

// ===========================================================================
// ✅ 2026-10-06 缺口收口：审核与预检前端组件级交互补齐
//
// 本组用例锁的是三条「静默失效」链路 —— 每条都在旧实现下**只丢一条断言**
// 就能让用户按过期结论继续整改：
//   ① 过期恢复：stale → 「重新总检」必须 force=true；单条修复 onFixed 也必须
//      force=true（此前只锁了批量那半条，单条那半条无人看守）；
//   ② 报告时效：report.stale=true 时必须额外告警（报告会被抄进评审意见）；
//   ③ 数据质量信号：cached / unknown_dimension_count / 零发现三条分支此前
//      完全无断言 —— 规则库与后端 DIMENSIONS 漂移会静默发生。
// ===========================================================================
describe("ReadinessDashboard · 过期恢复与告警链路", () => {
  /** 带 autofix 能力的一条 finding（供单条修复入口用）。 */
  function withAutofix(cap: Record<string, unknown> | null) {
    return {
      ...mocks.OVERVIEW,
      findings: mocks.OVERVIEW.findings.map((f: any, i: number) =>
        i === 0 ? { ...f, autofix: cap } : f),
    };
  }

  async function renderAndRunOverview(container: HTMLElement) {
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(complianceApi.overview).toHaveBeenCalled());
  }

  it("过期告警里的「重新总检」以 force=true 重算（不得复用旧缓存）", async () => {
    // runs[0].stale=true → 组件渲染 stale Alert
    (complianceApi.runs as any).mockResolvedValue({
      data: { items: [{ ...mocks.RUNS_FRESH[0], stale: true }] },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() =>
      expect(container.textContent || "").toContain("重新总检"));

    const btn = btnByText(container, "重新总检");
    expect(btn).toBeTruthy();
    fireEvent.click(btn!);

    await waitFor(() => {
      const calls = (complianceApi.overview as any).mock.calls as any[];
      expect(calls.some((c) => c[0] === "s1" && c[1] === true)).toBe(true);
    });
  });

  it("单条自动修复 onFixed → 同样 force=true 重算（与批量同口径）", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: withAutofix({ fixable: true, mode: "ai", reason: "" }),
    });
    (reviewAutoFixApi.plan as any).mockResolvedValue({
      data: {
        ok: true, fixable: true, mode: "ai", reason: "",
        finding: mocks.OVERVIEW.findings[0],
        targets: [{ section_id: "sec-a", section_title: "第1章", value: "x07",
                    line: 3, sentence_idx: 1, sentence_total: 2, matched: "x07",
                    context: "上下文", why: "命中控制字符" }],
        max_sections: 10,
      },
    });
    (reviewAutoFixApi.apply as any).mockResolvedValue({
      data: { ok: true, status: "repaired", mode: "ai", rule_id: "REF-001",
              targets: [], snapshot_id: "ver-single",
              items: [{ section_id: "sec-a", section_title: "第1章", status: "repaired",
                        before: "含\x07内容", after: "含内容", problems: [] }] },
    });
    const onContentFixed = vi.fn();
    const body = document.body as HTMLElement;
    const { container } = render(
      <App><ReadinessDashboard schemeId="s1" onContentFixed={onContentFixed} /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() => expect(container.textContent || "").toContain("82"));

    // 打开单条修复弹窗 → 定位 → 执行修复
    const fixBtn = (Array.from(container.querySelectorAll("button")).find(
      (b) => (b.textContent || "").includes("自动修复")) as HTMLButtonElement | undefined);
    expect(fixBtn).toBeTruthy();
    fireEvent.click(fixBtn!);
    await waitFor(() =>
      expect(btnByText(body, "定位矛盾位置")).toBeTruthy());
    fireEvent.click(btnByText(body, "定位矛盾位置")!);
    await waitFor(() =>
      expect(reviewAutoFixApi.plan).toHaveBeenCalledWith("s1", {
        rule_id: "REF-001", section_id: undefined,
      }));
    await waitFor(() => expect(btnByText(body, "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btnByText(body, "调用 AI 修复此处")!);
    await waitFor(() => expect(reviewAutoFixApi.apply).toHaveBeenCalled());

    await waitFor(() => expect(onContentFixed).toHaveBeenCalled());
    await waitFor(() => {
      const calls = (complianceApi.overview as any).mock.calls as any[];
      expect(calls.some((c) => c[0] === "s1" && c[1] === true)).toBe(true);
    });
  });

  it("导出报告 stale=true 时额外告警（报告会被抄进评审意见）", async () => {
    (complianceApi.runs as any).mockResolvedValue({ data: { items: mocks.RUNS_FRESH } });
    (complianceApi.report as any).mockResolvedValue({
      data: { content: "# 整改清单", filename: "报告.md", stale: true },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));

    const btn = btnByText(container, "导出整改清单")!;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    await waitFor(() => expect(complianceApi.report).toHaveBeenCalledWith("s1"));
    await waitFor(() => expectMsg(msgSpy.warning, "结论可能已过期"));
  });

  it("报告导出失败 → 报错且按钮恢复可点（不得卡在 loading）", async () => {
    (complianceApi.runs as any).mockResolvedValue({ data: { items: mocks.RUNS_FRESH } });
    (complianceApi.report as any).mockRejectedValue(new Error("报告服务不可用"));
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));

    const exportBtn = btnByText(container, "导出整改清单")!;
    expect(exportBtn.disabled).toBe(false);
    fireEvent.click(exportBtn);
    await waitFor(() => expect(complianceApi.report).toHaveBeenCalled());
    await waitFor(() => expectMsg(msgSpy.error, "报告服务不可用"));
    await waitFor(() => expect(exportBtn.disabled).toBe(false));
  });

  it("零发现 → 绿色「未检出任何问题」成功态", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: {
        ...mocks.OVERVIEW,
        findings: [], counts: { block: 0, high: 0, medium: 0, low: 0, total: 0 },
        blocked: false, released: true, blockers: [],
      },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() =>
      expect(container.textContent || "").toContain("未检出任何问题"));
  });

  it("规则目录加载失败 → 报错（不得静默给一个空抽屉）", async () => {
    (complianceApi.rules as any).mockRejectedValue(new Error("规则目录接口异常"));
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "规则说明")!);
    await waitFor(() => expect(complianceApi.rules).toHaveBeenCalled());
    await waitFor(() => expectMsg(msgSpy.error, "规则目录接口异常"));
  });

  it("规则目录缓存命中：二次打开不再重复请求", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "规则说明")!);
    await waitFor(() => expect(complianceApi.rules).toHaveBeenCalledTimes(1));
    const closeBtn = Array.from((document.body as HTMLElement).querySelectorAll(
      ".ant-drawer-close")).pop() as HTMLButtonElement | undefined;
    if (closeBtn) fireEvent.click(closeBtn);
    fireEvent.click(btnByText(container, "规则说明")!);
    await new Promise((r) => setTimeout(r, 30));
    expect((complianceApi.rules as any).mock.calls.length).toBe(1);
  });

  it("严重度筛选：全部 / 一般 两档均可用（此前只锁了提示与阻断+严重）", async () => {
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() => expect(container.textContent || "").toContain("82"));
    expect(container.textContent || "").toContain("缺少安全措施描述");
    expect(container.textContent || "").toContain("图表缺单位");

    const seg = (label: string) => (Array.from(container.querySelectorAll(
      ".ant-segmented-item-label")).find(
      (n) => (n.textContent || "").trim() === label) as HTMLElement | undefined);
    expect(seg("一般")).toBeTruthy();
    fireEvent.click(seg("一般")!);
    // 「一般」= medium：保留 MED-1、过滤 LOW-1
    await waitFor(() =>
      expect(container.textContent || "").not.toContain("图表缺单位"));
    expect(container.textContent || "").toContain("缺少安全措施描述");
    fireEvent.click(seg("全部")!);
    await waitFor(() => expect(container.textContent || "").toContain("图表缺单位"));
  });

  it("切换方案后旧方案的迟到总检响应不得覆盖新方案结论", async () => {
    let resolveOld: any;
    (complianceApi.overview as any)
      .mockImplementationOnce(() => new Promise((r) => { resolveOld = r; }))
      .mockResolvedValueOnce({ data: { ...mocks.OVERVIEW, total: 66 } });
    (complianceApi.runs as any).mockResolvedValue({ data: { items: mocks.RUNS_FRESH } });

    const { container, rerender } = render(
      <App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expect(complianceApi.overview).toHaveBeenCalledTimes(1));

    // 切到方案 s2 并完成一次总检（66 分）
    rerender(<App><ReadinessDashboard schemeId="s2" /></App>);
    await waitFor(() =>
      expect((complianceApi.runs as any).mock.calls.some((c: any[]) => c[0] === "s2")).toBe(true));

    // 旧方案 s1 的迟到响应此刻才回来 —— 必须被丢弃，不得渲染 82 分
    resolveOld?.({ data: { ...mocks.OVERVIEW, total: 82 } });
    await new Promise((r) => setTimeout(r, 60));
    expect(container.textContent || "").not.toContain("82 分");
  });

  it("cached=true 且非 force → 告知用户「未重复计入历史趋势」", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: { ...mocks.OVERVIEW, cached: true },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() => expectMsg(msgSpy.info, "未重复计入历史趋势"));
  });

  it("unknown_dimension_count > 0 → 提示核对规则库与后端维度口径", async () => {
    (complianceApi.overview as any).mockResolvedValue({
      data: { ...mocks.OVERVIEW, unknown_dimension_count: 3 },
    });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() => expectMsg(msgSpy.warning, "未登记的评分维度"));
  });

  it("总检失败 → 报错，且保留上一轮结论（不得清空面板）", async () => {
    (complianceApi.overview as any).mockResolvedValueOnce({ data: mocks.OVERVIEW });
    const { container } = render(<App><ReadinessDashboard schemeId="s1" /></App>);
    await waitFor(() => expect(container.textContent || "").toContain("尚未总检"));
    await renderAndRunOverview(container);
    await waitFor(() => expect(container.textContent || "").toContain("82 分"));

    (complianceApi.overview as any).mockRejectedValueOnce(new Error("预检引擎异常"));
    fireEvent.click(btnByText(container, "一键总检")!);
    await waitFor(() => expectMsg(msgSpy.error, "预检引擎异常"));
    expect(container.textContent || "").toContain("82 分");
  });
});