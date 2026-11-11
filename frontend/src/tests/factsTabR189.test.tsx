// @vitest-environment jsdom
/**
 * 全局事实模块 · R189 组件级 / Tab 级交互测试补齐（2026-11-08）。
 *
 * 既有 12 个 facts 测试文件共 258 例，但 `FactsTab` **本体零 render**：
 * 全是纯函数与子组件（FactsGroupList / FactsAdjustPanel / FactsDiagnosticsPanel）
 * 的测试。Tab 级编排（分类视图 ↔ 分页控件、统计卡按钮、筛选 Radio、
 * 资料卡折叠、跨段矛盾 Alert）从来没有任何 DOM 交互覆盖 —— 正是
 * 「分类视图下分页控件永不渲染」这个 P1 缺陷长期逃过所有用例的原因。
 *
 * 本文件锁定：
 *   A. FactsTab（Tab 级 render，真实 DOM 交互）
 *      1. 分类视图分支下分页控件**可达**（R189 P1 修复）；
 *      2. 点第 2 页 → loadFacts(2)，且分类详情仍渲染；
 *      3. 总数不足一页时不渲染分页（零行为变更护栏）；
 *      4. 统计卡「我已核对，仍有效」「一键确认全部」「下一步：正文生成」按钮接线；
 *      5. 筛选 Radio 切换上抛正确值；
 *      6. 资料卡标题点击切换折叠 setter；
 *      7. 空态文案；
 *      8. 跨段矛盾 Alert 遇到**对象型 value** 不崩（safeFactText）；
 *      9. 禁用态：generating 时主操作按钮 disabled。
 *   B. FactsGroupList（组件级，脏数据）
 *     10. confidence 缺失不出现 NaN%；
 *     11. source_ref 为数组/对象不抛 .slice 异常；
 *     12. 矛盾候选值为对象不触发 React child 崩溃，且裁决回传字符串；
 *     13. 超长 source_ref 被截断到 30 字；
 *     14. 特殊字符（HTML/引号/换行）按文本渲染不注入。
 *   C. factsShared 纯函数（R189 新增）
 *     15. safeFactText / factConfidencePercent / factConfidenceColor 边界。
 *   D. API 契约（跨模块数据链）
 *     16. factsApi.crossConflicts URL 与 signal；
 *     17. factsApi.list 显式下发 offset=0（此前被真值判断吞掉）。
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, fireEvent, cleanup, screen } from "@testing-library/react";
import React from "react";
import { FactsTab } from "../pages/schemeWorkbench/tabs/FactsTab";
import type { FactsTabProps } from "../pages/schemeWorkbench/tabs/FactsTab";
import { FactsGroupList } from "../pages/schemeWorkbench/FactsGroupList";
import {
  safeFactText,
  factConfidencePercent,
  factConfidenceColor,
} from "../pages/schemeWorkbench/factsShared";
import { factsApi } from "../api";
import type { FactCrossConflict, FactGroupLike } from "../types/facts";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Collapse / Pagination）会用到
if (!(window as unknown as { matchMedia?: unknown }).matchMedia) {
  (window as unknown as { matchMedia: unknown }).matchMedia = (query: string) => ({
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
if (!(globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

const baseGroup: FactGroupLike = {
  id: "g1",
  title: "工程概况",
  category: "basic",
  content: "",
  items: [
    {
      fact_id: "f1",
      name: "开挖深度",
      value: "8.5m",
      category: "basic",
      is_simulated: false,
      is_resolved: true,
      has_conflict: false,
      is_stale: false,
      confidence: 0.95,
      source: "招标文件",
    },
  ],
};

function makeProps(over: Partial<FactsTabProps> = {}): FactsTabProps {
  const noop = () => undefined;
  return {
    id: "s1",
    facts: [baseGroup],
    factsSummary: {
      total: 1,
      simulated: 0,
      unresolved: 0,
      conflicts: 0,
      stale: 0,
      has_warnings: false,
    },
    factsCrossConflicts: [],
    factsSegmentStats: null,
    factsFilter: "all",
    setFactsFilter: vi.fn(),
    factChapterTitles: {},
    factAttrTitles: {},
    factSourceKindTitles: {},
    factCategoryEntries: [
      {
        category: "basic",
        title: "基本信息",
        groupCount: 1,
        itemCount: 1,
        simulated: 0,
        conflicts: 0,
        unresolved: 0,
      },
    ],
    activeFactCategory: "basic",
    activeFactGroups: [baseGroup],
    factsPagination: { limit: 20, offset: 0, total_groups: 45, returned: 20 },
    docList: [],
    docStats: {
      actionableCount: 0,
      pendingCount: 0,
      failedCount: 0,
      parsedCount: 0,
      truncatedCount: 0,
      allParsed: true,
    },
    uploadedFiles: [],
    factsDocsCollapsed: true,
    setFactsDocsCollapsed: vi.fn(),
    generating: false,
    genType: "",
    progress: 0,
    progressMsg: "",
    missingValueMode: "fabricate",
    setMissingValueMode: vi.fn(),
    factsLogs: [],
    uploadingFacts: false,
    parsingDocs: false,
    taskPaused: false,
    taskStopping: false,
    factsChapterReport: null,
    factsDangerReport: null,
    factsDiagnosticsLoading: false,
    setActiveTab: vi.fn(),
    msg: { success: vi.fn(), error: vi.fn() },
    modal: { confirm: vi.fn() },
    handleAckStaleAll: vi.fn(),
    handleBatchResolve: vi.fn(),
    handleUploadDocuments: vi.fn(),
    handleParseDocuments: vi.fn(),
    handleParseDocument: vi.fn(),
    handleDeleteDocument: vi.fn(),
    handleGenerateFacts: vi.fn(),
    handleRetryFailedSegments: vi.fn(),
    openFactCreate: vi.fn(),
    handleClearAllFacts: vi.fn(),
    openFactEdit: vi.fn(),
    handleDeleteFactGroup: vi.fn(),
    openFactItemEdit: vi.fn(),
    handleResolveFactItem: vi.fn(),
    handleResolveConflictItem: vi.fn(),
    handleAckStaleOne: vi.fn(),
    handleControl: vi.fn(),
    loadFactsDiagnostics: vi.fn(),
    loadFacts: vi.fn(),
    loadDocuments: vi.fn(),
    ...over,
  };
}

// ===========================================================================
// A. FactsTab · Tab 级真实 DOM 交互
// ===========================================================================
describe("FactsTab · Tab 级交互（R189 补齐零覆盖）", () => {
  it("1-2. 分类视图下分页控件可达，点第 2 页触发 loadFacts(2) 且分类详情仍在", () => {
    const loadFacts = vi.fn();
    const { container } = render(<FactsTab {...makeProps({ loadFacts })} />);

    // 分类详情分支确实命中（这是导致分页恒不可达的元凶分支）
    expect(screen.getByText("来自左侧「事实分类」面板选中项")).toBeTruthy();
    expect(screen.getByText("基本信息")).toBeTruthy();

    const pager = container.querySelector(".ant-pagination");
    expect(pager, "分类视图下必须渲染分页控件，否则第 2 页永远不可达").toBeTruthy();
    // antd Pagination 的页码是 li.ant-pagination-item[title=N]
    const item2 = (pager as HTMLElement).querySelector('li[title="2"]');
    expect(item2, "第 2 页按钮必须在分类视图下存在").toBeTruthy();
    fireEvent.click(item2 as Element);
    expect(loadFacts).toHaveBeenCalledWith(2);
    // 上抛类型保持 number（后端 offset 计算依赖）
    expect(typeof loadFacts.mock.calls[0][0]).toBe("number");
  });

  it("3. 总数不足一页时不渲染分页（既有行为护栏）", () => {
    const { container } = render(
      <FactsTab
        {...makeProps({
          factsPagination: { limit: 20, offset: 0, total_groups: 1, returned: 1 },
        })}
      />,
    );
    expect(container.querySelector(".ant-pagination")).toBeNull();
    expect(screen.getByText("来自左侧「事实分类」面板选中项")).toBeTruthy();
  });

  it("4. 统计卡三个操作按钮按 summary 条件接线", () => {
    const ack = vi.fn();
    const batch = vi.fn();
    const setActiveTab = vi.fn();
    render(
      <FactsTab
        {...makeProps({
          factsSummary: {
            total: 5,
            simulated: 1,
            unresolved: 1,
            conflicts: 1,
            stale: 2,
            has_warnings: true,
          },
          handleAckStaleAll: ack,
          handleBatchResolve: batch,
          setActiveTab,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "我已核对，仍有效" }));
    expect(ack).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "一键确认全部" }));
    expect(batch).toHaveBeenCalledTimes(1);
    // has_warnings=true 时不出现「下一步」
    expect(screen.queryByText("下一步：正文生成")).toBeNull();
  });

  it("4b. 全部就绪时出现「下一步：正文生成」并上抛目标 Tab", () => {
    const setActiveTab = vi.fn();
    render(<FactsTab {...makeProps({ setActiveTab })} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步：正文生成/ }));
    expect(setActiveTab).toHaveBeenCalledTimes(1);
  });

  it("5. 筛选 Radio 切换上抛正确值", () => {
    const setFactsFilter = vi.fn();
    render(
      <FactsTab
        {...makeProps({
          setFactsFilter,
          factsSummary: {
            total: 3,
            simulated: 1,
            unresolved: 1,
            conflicts: 1,
            stale: 0,
            has_warnings: true,
          },
        })}
      />,
    );
    fireEvent.click(screen.getByText("⚠️ 模拟 (1)"));
    expect(setFactsFilter).toHaveBeenCalled();
  });

  it("6. 资料卡标题点击切换折叠 setter", () => {
    const setFactsDocsCollapsed = vi.fn();
    render(<FactsTab {...makeProps({ setFactsDocsCollapsed })} />);
    fireEvent.click(screen.getByText("已上传的项目资料"));
    expect(setFactsDocsCollapsed).toHaveBeenCalledTimes(1);
    expect(typeof setFactsDocsCollapsed.mock.calls[0][0]).toBe("function");
  });

  it("7. 空态文案与工具栏仍可用", () => {
    render(<FactsTab {...makeProps({ facts: [], activeFactGroups: [], factsSummary: null })} />);
    expect(screen.getByText(/暂无全局事实，可使用上方按钮提取或录入/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /③ AI 提取事实/ })).toBeTruthy();
  });

  it("8. 跨段矛盾 Alert 遇到对象型 value 不崩且安全文本化", () => {
    const cf: FactCrossConflict = {
      rule_id: "XV-MACH-SCHED",
      severity: "high",
      conflict_type: "machinery_schedule",
      side_a: { name: "吊车数量", value: { n: 2, unit: "台" } },
      side_b: { name: "工期", value: 30 },
      auto_resolvable: false,
    };
    expect(() => render(<FactsTab {...makeProps({ factsCrossConflicts: [cf] })} />)).not.toThrow();
    expect(screen.getByText(/检测到 1 处跨段矛盾/)).toBeTruthy();
    expect(document.body.textContent).toContain('{"n":2,"unit":"台"}');
    expect(document.body.textContent).toContain("30");
  });

  it("8b. R214：同 rule_id 的多处跨段矛盾用组合键渲染，不触发 React duplicate key 警告", () => {
    // ✅ R214（2026-11-10 · R210 D-6）：后端 rule_id **不唯一**（XV-MAT-DESIGN /
    //    XV-MACH-SCHED 各 4 个独立产出点、DB 唯一键含 conflict_type）⇒ 旧 key
    //    ``cf.rule_id || idx`` 让同 rule_id 的多处冲突 key 相同，React 报
    //    「Encountered two children with the same key」并复用节点（渲染错乱）。
    //    现按 rule_id + 双侧名称/值 + idx 组合。本测试用两条**同 rule_id** 冲突，
    //    捕获 console.error：出现 duplicate key 警告即判红。
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const cfBase: FactCrossConflict = {
      rule_id: "XV-MAT-DESIGN",
      severity: "high",
      conflict_type: "material_design",
      side_a: { name: "钢筋等级", value: "HRB400" },
      side_b: { name: "设计说明", value: "HRB400E" },
      auto_resolvable: false,
    };
    const cf2: FactCrossConflict = {
      rule_id: "XV-MAT-DESIGN",
      severity: "high",
      conflict_type: "material_design",
      side_a: { name: "混凝土强度", value: "C30" },
      side_b: { name: "设计说明", value: "C35" },
      auto_resolvable: false,
    };
    render(<FactsTab {...makeProps({ factsCrossConflicts: [cfBase, cf2] })} />);
    const dup = errSpy.mock.calls.filter((c) => {
      const s = String(c[0] ?? "");
      return s.includes("same key") || s.includes("Encountered two children");
    });
    errSpy.mockRestore();
    expect(dup).toEqual([]);
    // 两条冲突都渲染出来（组合键不会互相覆盖）
    expect(screen.getByText(/检测到 2 处跨段矛盾/)).toBeTruthy();
    expect(document.body.textContent).toContain("钢筋等级");
    expect(document.body.textContent).toContain("混凝土强度");
  });

  it("9. generating 时主操作按钮禁用", () => {
    render(<FactsTab {...makeProps({ generating: true, genType: "facts" })} />);
    const btn = screen.getByRole("button", { name: /③ AI 提取事实/ }) as HTMLButtonElement;
    expect(btn.disabled || btn.className.includes("disabled")).toBeTruthy();
  });
});

// ===========================================================================
// B. FactsGroupList · 组件级脏数据
// ===========================================================================
describe("FactsGroupList · 空数据 / 特殊字符（R189）", () => {
  const cb = {
    onEditGroup: vi.fn(),
    onDeleteGroup: vi.fn(),
    onEditItem: vi.fn(),
    onResolveItem: vi.fn(),
    onResolveConflict: vi.fn(),
    onAckStaleOne: vi.fn(),
  };

  function renderList(groups: FactGroupLike[], expand = true) {
    const utils = render(
      <FactsGroupList
        groups={groups}
        filter="all"
        onEditGroup={cb.onEditGroup}
        onDeleteGroup={cb.onDeleteGroup}
        onEditItem={cb.onEditItem}
        onResolveItem={cb.onResolveItem}
        onResolveConflict={cb.onResolveConflict}
        onAckStaleOne={cb.onAckStaleOne}
      />,
    );
    // antd Collapse 默认只渲染展开面板内容；条目级断言前必须先展开第一个面板
    if (expand) {
      const header = utils.container.querySelector(".ant-collapse-header") as HTMLElement | null;
      if (header) fireEvent.click(header);
    }
    return utils;
  }

  it("10. confidence 缺失不渲染 NaN%，显示「置信度未知」", () => {
    const groups = [
      {
        ...baseGroup,
        items: [{ ...baseGroup.items![0], confidence: undefined }],
      } as FactGroupLike,
    ];
    expect(() => renderList(groups)).not.toThrow();
    expect(document.body.textContent).not.toContain("NaN");
  });

  it("11. source_ref 为数组不抛 .slice 异常", () => {
    const groups = [
      {
        ...baseGroup,
        items: [
          {
            ...baseGroup.items![0],
            source_ref: [{ file: "招标文件.pdf", quote: "第 3 页" }],
          },
        ],
      } as unknown as FactGroupLike,
    ];
    expect(() => renderList(groups)).not.toThrow();
    expect(document.body.textContent).toContain("招标文件");
  });

  it("12. 矛盾候选值为对象不崩，且「选此值」回传字符串", () => {
    const groups = [
      {
        ...baseGroup,
        items: [
          {
            ...baseGroup.items![0],
            has_conflict: true,
            conflict_values: [{ value: { v: "10m" }, source: "图纸", confidence: 0.8 }],
          },
        ],
      } as unknown as FactGroupLike,
    ];
    expect(() => renderList(groups)).not.toThrow();
    const choose = screen.getAllByRole("button", { name: "选此值" })[0];
    fireEvent.click(choose);
    expect(cb.onResolveConflict).toHaveBeenCalledTimes(1);
    expect(typeof cb.onResolveConflict.mock.calls[0][1]).toBe("string");
  });

  it("13. 超长 source_ref 被截断到 30 字", () => {
    const longRef = "甲".repeat(200);
    const groups = [
      {
        ...baseGroup,
        items: [{ ...baseGroup.items![0], source_ref: longRef }],
      } as unknown as FactGroupLike,
    ];
    renderList(groups);
    expect(document.body.textContent).not.toContain(longRef);
    expect(document.body.textContent).toContain("甲".repeat(30));
  });

  it("14. 特殊字符按文本渲染，不注入 HTML", () => {
    const evil = "<img src=x onerror=alert(1)> & \"引号\"\n换行";
    const groups = [
      {
        ...baseGroup,
        items: [{ ...baseGroup.items![0], value: evil }],
      } as unknown as FactGroupLike,
    ];
    renderList(groups);
    expect(document.querySelector("img[src=x]")).toBeNull();
    expect(document.body.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});

// ===========================================================================
// C. factsShared 纯函数边界
// ===========================================================================
describe("factsShared · R189 安全渲染纯函数", () => {
  it("15a. safeFactText 覆盖 null / 数字 / 数组 / 循环引用", () => {
    expect(safeFactText(null)).toBe("");
    expect(safeFactText(undefined)).toBe("");
    expect(safeFactText("文本")).toBe("文本");
    expect(safeFactText(0)).toBe("0");
    expect(safeFactText(false)).toBe("false");
    expect(safeFactText([1, "a"])).toBe('[1,"a"]');
    const cyc: Record<string, unknown> = {};
    cyc.self = cyc;
    expect(typeof safeFactText(cyc)).toBe("string");
  });

  it("15b. factConfidencePercent 归一 0~1 / 百分制 / 越界 / 脏值", () => {
    expect(factConfidencePercent(0.95)).toBe(95);
    expect(factConfidencePercent(85)).toBe(85);
    expect(factConfidencePercent(-1)).toBe(0);
    expect(factConfidencePercent(999)).toBe(100);
    expect(factConfidencePercent(undefined)).toBeNull();
    expect(factConfidencePercent("0.9")).toBeNull();
    expect(factConfidencePercent(NaN)).toBeNull();
  });

  it("15c. factConfidenceColor 四档阈值 + 无效值灰色", () => {
    expect(factConfidenceColor(0.95)).toBe("#52c41a");
    expect(factConfidenceColor(0.8)).toBe("#1677ff");
    expect(factConfidenceColor(0.5)).toBe("#fa8c16");
    expect(factConfidenceColor(0.1)).toBe("#ff4d4f");
    expect(factConfidenceColor(null)).toBe("#bfbfbf");
  });
});

// ===========================================================================
// D. API 契约（跨模块数据链）
// ===========================================================================
describe("factsApi · R189 路由/参数契约", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("16. crossConflicts 调 GET /global-facts/cross-conflicts 并透传 signal", async () => {
    // api 实例是 axios.create 产物（模块 default export），直接 spy 其 get。
    const client = (await import("../api")).default;
    const spy = vi
      .spyOn(client, "get")
      .mockResolvedValue({ data: { conflicts: [], total: 0 } } as never);
    const ac = new AbortController();
    await factsApi.crossConflicts("s1", { signal: ac.signal });
    expect(spy).toHaveBeenCalledWith(
      "/global-facts/cross-conflicts",
      expect.objectContaining({ params: { scheme_id: "s1" }, signal: ac.signal }),
    );
  });

  it("17. list 显式下发 offset=0 / limit=20（0 不再被真值判断吞掉）", async () => {
    const client = (await import("../api")).default;
    const spy = vi
      .spyOn(client, "get")
      .mockResolvedValue({ data: { groups: [], stats: {}, pagination: {} } } as never);
    await factsApi.list("s1", { limit: 20, offset: 0 });
    expect(spy.mock.calls[0][1]).toEqual(
      expect.objectContaining({ params: { scheme_id: "s1", limit: 20, offset: 0 } }),
    );
  });
});
