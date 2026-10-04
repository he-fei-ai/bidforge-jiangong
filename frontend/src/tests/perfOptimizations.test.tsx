// @vitest-environment jsdom
/**
 * 前端性能优化 · 回归测试（2026-09-24）
 * =====================================
 * 为本次「首屏/交互/重渲染/SSE/内存」性能治理补充的可执行断言。每条用例都对应
 * 一个已定位的卡顿根因，锁定「优化生效且不退化」：
 *
 *  1. BidAnalysisTab 默认导出必须是 memo 组件（防止后续改动把 memo 摘掉）；
 *  2. 派生计算（分组/扁平行/分类栏）已 useMemo 化：无关 props 变化不重算；
 *     反例：defs/groups 变化必须重算并刷新列表（防「过度记忆化」导致数据陈旧）；
 *  3. Layout 侧栏单挂载：桌面/窄屏均只挂载一份 Sidebar（其内含 TaskStatusBar，
 *     承载常驻 SSE 活动流 + 10s 健康轮询 + 看门狗）。修复前窄屏会同时挂载
 *     「隐藏的固定侧栏」与「抽屉侧栏」两份，造成双倍常驻连接与后端压力。
 *
 * 说明：TaskStatusBar 被替换为计数桩，既隔离真实 SSE（jsdom 下不可用），
 * 又可以直接统计「Sidebar 实例数」——这正是单挂载修复的观测点。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import React from "react";
import BidAnalysisTab from "../components/BidAnalysisTab";
// ⚠️ Layout 是具名导出（App.tsx 亦为 `import { Layout }`），不能用默认导入
import { Layout } from "../components/Layout";
import type { BaItemDef } from "../utils/bidAnalysis";

// 计数器必须在 vi.mock 工厂（会被提升到文件顶部）之前初始化
const counters = vi.hoisted(() => ({ normalizeCalls: 0, sidebarMounted: 0 }));

// 用包装函数统计 normalizeBaGroups 调用次数：它是 BidAnalysisTab 派生计算的入口，
// 调用次数 == 派生数据重算次数（其余实现全部走真实代码，避免测试失真）。
vi.mock("../utils/bidAnalysis", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/bidAnalysis")>();
  return {
    ...actual,
    normalizeBaGroups: (...args: any[]) => {
      counters.normalizeCalls += 1;
      return (actual as any).normalizeBaGroups(...args);
    },
  };
});

// TaskStatusBar → 计数桩（真实组件会在 jsdom 下开 SSE，不可用）
vi.mock("../components/TaskStatusBar", async () => {
  const R = await import("react");
  const MockTaskStatusBar = () => {
    R.useEffect(() => {
      counters.sidebarMounted += 1;
      return () => {
        counters.sidebarMounted -= 1;
      };
    }, []);
    return R.createElement("div", { "data-testid": "task-status-bar" });
  };
  return {
    default: MockTaskStatusBar,
  };
});

// 侧栏会拉取项目/方案列表：隔离网络，其余 API 保持真实
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    projectsApi: { ...actual.projectsApi, list: vi.fn().mockResolvedValue({ data: [] }) },
    schemesApi: { ...actual.schemesApi, list: vi.fn().mockResolvedValue({ data: [] }) },
  };
});

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Progress / Drawer）会用到
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

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  counters.normalizeCalls = 0;
  counters.sidebarMounted = 0;
});

const DEFS: BaItemDef[] = [
  { item_id: "a", label: "A项", required: 1, output_type: "markdown", group: "g1" },
  { item_id: "b", label: "B项", required: 0, output_type: "markdown", group: "g1" },
];
const GROUPS = [{ group: "g1", label: "分组一", items: [DEFS[0]] }];

/** 稳定引用：模拟父组件已 memo 化回调的理想情况 */
const HANDLERS = {
  onStart: () => {},
  onStop: () => {},
  onOpenConfig: () => {},
  onCheckSections: () => {},
  onRefresh: () => {},
  onSelectItem: () => {},
  onRerunItem: () => {},
  onOpenFullView: () => {},
  onDismissSectionResult: () => {},
  onGoImport: () => {},
  onGoOutline: () => {},
  onGoFacts: () => {},
  onGoNext: () => {},
};

function baseProps(over: Record<string, any> = {}) {
  return {
    defs: DEFS,
    groups: GROUPS,
    items: [],
    summary: null,
    running: false,
    progress: 0,
    progressMsg: "",
    parsedDocCount: 1,
    selectedItem: null,
    sectionChecking: false,
    sectionCheckResult: null,
    ...HANDLERS,
    ...over,
  };
}

describe("BidAnalysisTab 渲染性能", () => {
  it("默认导出为 memo 组件（父组件无关重渲可整体跳过）", () => {
    expect((BidAnalysisTab as any).$$typeof).toBe(Symbol.for("react.memo"));
  });

  it("无关 props 变化不重算分组/扁平行（派生数据已 useMemo 化）", () => {
    const { rerender } = render(<BidAnalysisTab {...baseProps()} progressMsg="阶段一" />);
    // 首次渲染算一次
    expect(counters.normalizeCalls).toBe(1);
    // 仅 progressMsg（非派生依赖）变化：组件会重渲，但派生数据不应重算
    rerender(<BidAnalysisTab {...baseProps()} progressMsg="阶段二" />);
    expect(counters.normalizeCalls).toBe(1);
  });

  it("反例：defs 变化必须重算且列表随之更新（防过度记忆化导致陈旧）", () => {
    const { rerender, container } = render(<BidAnalysisTab {...baseProps()} />);
    expect(counters.normalizeCalls).toBe(1);
    // 基线：分组口径以 groups 为准，当前只有 1 行
    expect(container.querySelectorAll(".ba-item-row").length).toBe(1);

    const DEFS2: BaItemDef[] = [
      ...DEFS,
      { item_id: "c", label: "C项", required: 0, output_type: "markdown", group: "g1" },
    ];
    const GROUPS2 = [{ group: "g1", label: "分组一", items: DEFS2 }];
    rerender(<BidAnalysisTab {...baseProps({ defs: DEFS2, groups: GROUPS2 })} />);
    expect(counters.normalizeCalls).toBe(2);
    // 重算真的生效：任务列表行数随之增加（不是缓存了旧结果）
    expect(container.querySelectorAll(".ba-item-row").length).toBe(3);
    expect(screen.getByText("C项")).toBeTruthy();
  });

  it("反例：groups 变化必须重算（分组口径以后端 groups 为准）", () => {
    const { rerender } = render(<BidAnalysisTab {...baseProps()} />);
    expect(counters.normalizeCalls).toBe(1);
    rerender(
      <BidAnalysisTab
        {...baseProps({ groups: [{ group: "g1", label: "改名分组", items: [DEFS[0]] }] })}
      />
    );
    expect(counters.normalizeCalls).toBe(2);
  });
});

describe("Layout 侧栏单挂载（常驻 SSE / 轮询不翻倍）", () => {
  const setWidth = (w: number) => {
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: w });
  };

  it("桌面端：只挂载一份固定侧栏", () => {
    setWidth(1440);
    render(
      <MemoryRouter>
        <Layout>
          <div>内容区</div>
        </Layout>
      </MemoryRouter>
    );
    expect(counters.sidebarMounted).toBe(1);
  });

  it("窄屏（<768）：不出现「隐藏固定侧栏 + 抽屉侧栏」双挂载", () => {
    setWidth(480);
    render(
      <MemoryRouter>
        <Layout>
          <div>内容区</div>
        </Layout>
      </MemoryRouter>
    );
    // 修复前窄屏为 2（一份 display:none 的固定侧栏 + 一份抽屉侧栏，各开一条 SSE
    // 活动流 + 一套 10s 健康轮询 + 看门狗）；修复后最多 1 份。
    expect(counters.sidebarMounted).toBeLessThanOrEqual(1);
  });
});
