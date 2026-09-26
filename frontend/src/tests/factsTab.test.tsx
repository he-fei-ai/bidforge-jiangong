// @vitest-environment jsdom
/**
 * 「全局事实」Tab · 组件级交互测试（2026-09-20 补齐）。
 *
 * 此前 facts Tab 的「筛选 + 分组 Collapse + 条目卡片 + 矛盾裁决」全部内联在
 * 8000 行工作台页面 JSX 里，零组件级测试覆盖，且分组级「编辑 / 删除」全断链。
 * 现拆出两个可测试单元并锁定其行为：
 *
 * applyFactsFilter（纯筛选逻辑）：
 *   1. all：全部保留、空组也保留，仅回填 filteredItems；
 *   2. simulated / conflict / unresolved：按标记过滤，无命中的组整组剔除；
 *   3. groups 为空 / null 的安全回退。
 *
 * FactsGroupList（分组列表组件，数据操作全部回调化）：
 *   4. 头部徽标计数（总项 / 模拟 / 矛盾 / 待审）取自全量 items 而非过滤后；
 *   5. 分组级「编辑分组 / 删除」上抛正确的 group；
 *   6. 条目级「编辑 / 确认」上抛正确的 (group, item) / (item)；
 *   7. 矛盾「保留当前值 / 选此值」上抛 (item, 对应 value)；
 *   8. filter 生效后不在筛选结果内的条目不渲染；
 *   9. 无结构化条目时回退整段 Markdown。
 *
 * 2026-09-21 追加（全局事实专项）：
 *   10. hasFactsFilterMatch：筛选悬空回退 effect 的唯一判定源；
 *   11. buildBatchResolveCopy：批量确认文案（修复交集重复计数）；
 *   12. FactsExtractProgressCard：提取进度卡（进度/日志最新在上/空态）；
 *   13. FactsSegmentFailuresAlert：分段失败告警（部分失败/全部失败/明细截断）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import {
  applyFactsFilter, FactsGroupList, hasFactsFilterMatch,
  buildBatchResolveCopy, buildBatchResolveResultCopy, isCurrentSchemeRequest,
  FactsExtractProgressCard, FactsSegmentFailuresAlert, FactsDiagnosticsPanel,
  buildFactsCategoryEntries, resolveSelectedFactCategory,
  filterFactsGroupsByCategory, FactsCategoryPanel,
  FACT_CHAPTER_TITLES, factChapterTitle,
} from "../pages/SchemeWorkbenchPage";

// vitest 未开 globals，RTL 自动清理不生效 —— 显式清理，避免跨用例 DOM 累积
afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Collapse）会用到
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

// ============================================================
// applyFactsFilter · 纯筛选逻辑
// ============================================================
describe("applyFactsFilter（事实筛选纯逻辑）", () => {
  const groups = [
    {
      id: "g1",
      title: "基本信息",
      items: [
        { name: "A", value: "1", is_simulated: true, is_resolved: false, has_conflict: false },
        { name: "B", value: "2", is_simulated: false, is_resolved: true, has_conflict: true },
      ],
    },
    {
      id: "g2",
      title: "工期安排",
      items: [
        { name: "C", value: "3", is_simulated: false, is_resolved: false, has_conflict: false },
      ],
    },
    { id: "g3", title: "空组", items: [] },
  ];

  it("all：保留全部分组（含空组），filteredItems 回填为原始条目", () => {
    const out = applyFactsFilter(groups, "all");
    expect(out.map((g) => g.id)).toEqual(["g1", "g2", "g3"]);
    expect(out[0].filteredItems).toHaveLength(2);
    expect(out[2].filteredItems).toEqual([]);
  });

  it("simulated：只留模拟值，无命中的分组整体剔除", () => {
    const out = applyFactsFilter(groups, "simulated");
    expect(out.map((g) => g.id)).toEqual(["g1"]);
    expect(out[0].filteredItems.map((it: any) => it.name)).toEqual(["A"]);
  });

  it("conflict：只留矛盾条目", () => {
    const out = applyFactsFilter(groups, "conflict");
    expect(out.map((g) => g.id)).toEqual(["g1"]);
    expect(out[0].filteredItems.map((it: any) => it.name)).toEqual(["B"]);
  });

  it("unresolved：只留未确认条目", () => {
    const out = applyFactsFilter(groups, "unresolved");
    expect(out.find((g) => g.id === "g1")!.filteredItems.map((it: any) => it.name)).toEqual(["A"]);
    expect(out.find((g) => g.id === "g2")!.filteredItems.map((it: any) => it.name)).toEqual(["C"]);
  });

  it("groups 为 null / 空数组时安全返回空", () => {
    expect(applyFactsFilter(null as any, "all")).toEqual([]);
    expect(applyFactsFilter([], "conflict")).toEqual([]);
  });
});

// ============================================================
// FactsGroupList · 组件级交互
// ============================================================
function baseProps(over: Partial<React.ComponentProps<typeof FactsGroupList>> = {}) {
  return {
    filter: "all",
    onEditGroup: vi.fn(),
    onDeleteGroup: vi.fn(),
    onEditItem: vi.fn(),
    onResolveItem: vi.fn(),
    onResolveConflict: vi.fn(),
    ...over,
  } as any;
}

function makeGroup() {
  return [
    {
      id: "g1",
      title: "基本信息",
      items: [
        { fact_id: "f1", name: "工程名称", value: "示范工程", is_simulated: true, is_resolved: false, has_conflict: false, confidence: 0.5 },
        { fact_id: "f2", name: "合同工期", value: "365", is_simulated: false, is_resolved: true, has_conflict: true, confidence: 0.8,
          conflict_values: [{ value: "400", source: "招标文件", confidence: 0.7 }] },
      ],
    },
  ];
}

// antd Collapse 默认只渲染展开面板内容；测试条目级按钮前先展开第一个面板
function expandFirstPanel(utils: ReturnType<typeof render>) {
  const header = utils.container.querySelector(".ant-collapse-header") as HTMLElement;
  expect(header).toBeTruthy();
  fireEvent.click(header);
}

describe("FactsGroupList（全局事实分组列表）", () => {
  it("渲染分组标题与头部徽标计数（总/模拟/矛盾/待审）", () => {
    const props = baseProps({ groups: makeGroup() });
    const { getByText } = render(<FactsGroupList {...props} />);
    expect(getByText("基本信息")).toBeTruthy();
    expect(getByText("2 项")).toBeTruthy();
    // g1：模拟 1、矛盾 1、待审 1
    expect(getByText("⚠️1")).toBeTruthy();
    expect(getByText("🔸1")).toBeTruthy();
    expect(getByText("📝1")).toBeTruthy();
  });

  it("分组级「编辑分组 / 删除」上抛正确的 group", () => {
    const groups = makeGroup();
    const props = baseProps({ groups });
    const { getByText } = render(<FactsGroupList {...props} />);
    fireEvent.click(getByText("编辑分组"));
    expect(props.onEditGroup).toHaveBeenCalledTimes(1);
    expect(props.onEditGroup.mock.calls[0][0].id).toBe("g1");
    fireEvent.click(getByText("删除"));
    expect(props.onDeleteGroup).toHaveBeenCalledTimes(1);
    expect(props.onDeleteGroup.mock.calls[0][0].id).toBe("g1");
  });

  it("展开面板后：条目级「编辑 / 确认」上抛正确的 (group,item)/(item)", () => {
    const props = baseProps({ groups: makeGroup() });
    const utils = render(<FactsGroupList {...props} />);
    expandFirstPanel(utils);
    // f1 未确认 → 有「确认」；f1/f2 有 fact_id → 各有「编辑」
    const editBtns = utils.getAllByText("编辑");
    fireEvent.click(editBtns[0]);
    expect(props.onEditItem).toHaveBeenCalledTimes(1);
    expect(props.onEditItem.mock.calls[0][0].id).toBe("g1");
    expect(props.onEditItem.mock.calls[0][1].fact_id).toBe("f1");
    fireEvent.click(utils.getByText("确认"));
    expect(props.onResolveItem).toHaveBeenCalledTimes(1);
    expect(props.onResolveItem.mock.calls[0][0].fact_id).toBe("f1");
  });

  it("矛盾条目：「保留当前值 / 选此值」上抛 (item, 对应 value)", () => {
    const props = baseProps({ groups: makeGroup() });
    const utils = render(<FactsGroupList {...props} />);
    expandFirstPanel(utils);
    fireEvent.click(utils.getByText("保留当前值"));
    expect(props.onResolveConflict).toHaveBeenCalledTimes(1);
    expect(props.onResolveConflict.mock.calls[0][0].fact_id).toBe("f2");
    expect(props.onResolveConflict.mock.calls[0][1]).toBe("365"); // 当前值
    fireEvent.click(utils.getByText("选此值"));
    expect(props.onResolveConflict).toHaveBeenCalledTimes(2);
    expect(props.onResolveConflict.mock.calls[1][1]).toBe("400"); // 候选值
  });

  it("filter=simulated：非模拟条目不进入渲染", () => {
    const props = baseProps({ groups: makeGroup(), filter: "simulated" });
    const utils = render(<FactsGroupList {...props} />);
    expandFirstPanel(utils);
    expect(utils.getByText("工程名称")).toBeTruthy(); // f1 模拟，保留
    expect(utils.queryByText("合同工期")).toBeNull();   // f2 非模拟，被过滤
  });

  it("无结构化条目时回退整段 Markdown", () => {
    const groups = [{ id: "gm", title: "编制依据", items: [], content: "参照 GB50300 执行" }];
    const props = baseProps({ groups });
    const utils = render(<FactsGroupList {...props} />);
    expandFirstPanel(utils);
    expect(utils.getByText(/GB50300/)).toBeTruthy();
  });
});

// ============================================================
// hasFactsFilterMatch · 筛选悬空回退的唯一判定源
// ============================================================
describe("hasFactsFilterMatch（筛选命中判定）", () => {
  const groups = [
    { id: "g1", items: [
      { name: "A", is_simulated: true, is_resolved: false, has_conflict: false },
    ] },
  ];

  it("all 恒真（不参与悬空判定）", () => {
    expect(hasFactsFilterMatch([], "all")).toBe(true);
  });

  it("按标记判定命中", () => {
    expect(hasFactsFilterMatch(groups, "simulated")).toBe(true);
    expect(hasFactsFilterMatch(groups, "unresolved")).toBe(true);
    expect(hasFactsFilterMatch(groups, "conflict")).toBe(false);
  });

  it("空列表 / null 时除 all 外均视为无命中", () => {
    expect(hasFactsFilterMatch([], "simulated")).toBe(false);
    expect(hasFactsFilterMatch(null as any, "unresolved")).toBe(false);
  });
});

// ============================================================
// buildBatchResolveCopy · 批量确认文案（口径回归锁）
// ============================================================
describe("buildBatchResolveCopy（批量确认文案）", () => {
  it("未确认数不得加模拟值（旧实现 unresolved+simulated 重复计数交集）", () => {
    const c = buildBatchResolveCopy({ unresolved: 5, simulated: 3, conflicts: 2 });
    expect(c.headline).toContain("共有 5 项事实未确认");
    expect(c.headline).not.toContain("8");
  });

  it("有模拟值时追加风险提示并升级为 warning", () => {
    const c = buildBatchResolveCopy({ unresolved: 2, simulated: 2, conflicts: 0 });
    expect(c.confirmType).toBe("warning");
    expect(c.simulatedNote).toContain("模拟值 2 项");
    expect(c.conflictNote).toBe("");
  });

  it("有矛盾时追加逐条裁决提示", () => {
    const c = buildBatchResolveCopy({ unresolved: 1, simulated: 0, conflicts: 4 });
    expect(c.confirmType).toBe("confirm");
    expect(c.conflictNote).toContain("4 项存在矛盾");
    expect(c.simulatedNote).toBe("");
  });

  it("脏数据（null/undefined）按 0 处理不抛异常", () => {
    const c = buildBatchResolveCopy(null as any);
    expect(c.headline).toContain("共有 0 项");
    expect(c.confirmType).toBe("confirm");
  });
});

// ============================================================
// FactsExtractProgressCard · 提取进度卡
// ============================================================
describe("FactsExtractProgressCard（提取进度卡）", () => {
  it("渲染进度百分比、阶段文案与日志计数", () => {
    const { getByText, container } = render(
      <FactsExtractProgressCard
        progress={0.42}
        progressMsg="正在提取第 2/5 段"
        logs={[{ progress: 0.2, message: "分段完成" }, { progress: 0.4, message: "提取中" }]}
      />,
    );
    expect(getByText("正在提取第 2/5 段")).toBeTruthy();
    expect(getByText("42%")).toBeTruthy();
    expect(getByText(/共 2 条进度记录/)).toBeTruthy();
    // 进度条 percent 同步
    expect(container.querySelector(".ant-progress-text")?.textContent).toContain("42");
  });

  it("日志最新在上（列表倒序渲染）", () => {
    const utils = render(
      <FactsExtractProgressCard
        progress={0.9}
        progressMsg="x"
        logs={[
          { progress: 0.1, message: "第一条" },
          { progress: 0.5, message: "中间条" },
          { progress: 0.9, message: "最新条" },
        ]}
      />,
    );
    const html = utils.container.textContent || "";
    expect(html).toContain("最新条");
    const idx = (s: string) => html.indexOf(s);
    expect(idx("最新条")).toBeLessThan(idx("中间条"));
    expect(idx("中间条")).toBeLessThan(idx("第一条"));
  });

  it("无日志时显示连接占位文案", () => {
    const { getByText } = render(
      <FactsExtractProgressCard progress={0} progressMsg="正在提取..." logs={[]} />,
    );
    expect(getByText("正在连接 AI，准备提取...")).toBeTruthy();
  });
});

// ============================================================
// FactsSegmentFailuresAlert · 分段失败告警
// ============================================================
describe("FactsSegmentFailuresAlert（分段失败告警）", () => {
  it("部分失败：warning 文案 + 失败明细（最多 6 条）+ 常见原因", () => {
    const details = Array.from({ length: 8 }, (_, i) => ({
      index: i + 1, heading: `段${i + 1}`, reason: "模型超时",
    }));
    const { getByText, queryByText, container } = render(
      <FactsSegmentFailuresAlert stats={{ ok: 4, failed: 8, total: 12, failed_details: details }} />,
    );
    expect(getByText("本次提取有 8/12 段失败，结果可能不完整")).toBeTruthy();
    expect(getByText(/第 1 段（段1）：模型超时/)).toBeTruthy();
    expect(queryByText(/第 7 段/)).toBeNull(); // 明细最多展示 6 条
    expect(container.querySelector(".ant-alert-warning")).toBeTruthy();
    expect(getByText(/常见原因：模型超时\/限流/)).toBeTruthy();
  });

  it("全部失败：error 文案 + 无明细时的重试引导", () => {
    const { getByText, container } = render(
      <FactsSegmentFailuresAlert stats={{ ok: 0, failed: 5, total: 5, failed_details: [] }} />,
    );
    expect(getByText("本次提取全部 5 段失败，未提取到事实")).toBeTruthy();
    expect(getByText(/建议稍后点击「③ AI 提取事实」重新提取/)).toBeTruthy();
    expect(container.querySelector(".ant-alert-error")).toBeTruthy();
  });
});

// ============================================================
// buildFactsSummary · stats → summary 收口（2026-09-21 新增）
// ============================================================
import { buildFactsSummary, applyFactsCompletedEvent } from "../pages/SchemeWorkbenchPage";

describe("buildFactsSummary（stats → summary）", () => {
  it("has_warnings = 模拟值 / 未确认 / 矛盾 任一 > 0", () => {
    expect(buildFactsSummary({ total: 5, simulated: 1, unresolved: 0, conflicts: 0 }).has_warnings).toBe(true);
    expect(buildFactsSummary({ total: 5, simulated: 0, unresolved: 2, conflicts: 0 }).has_warnings).toBe(true);
    expect(buildFactsSummary({ total: 5, simulated: 0, unresolved: 0, conflicts: 3 }).has_warnings).toBe(true);
    expect(buildFactsSummary({ total: 5, simulated: 0, unresolved: 0, conflicts: 0 }).has_warnings).toBe(false);
  });

  it("脏值（undefined / 字符串）按 0 处理，不抛异常", () => {
    const s = buildFactsSummary({ total: "3", simulated: undefined, unresolved: "x" });
    expect(s.total).toBe(3);
    expect(s.simulated).toBe(0);
    expect(s.unresolved).toBe(0);
    expect(s.has_warnings).toBe(false);
  });

  it("null / 非对象直接返回 null（切方案重置口径）", () => {
    expect(buildFactsSummary(null)).toBeNull();
    expect(buildFactsSummary(undefined)).toBeNull();
    expect(buildFactsSummary("x")).toBeNull();
  });

  it("透传 by_category 等扩展字段", () => {
    const s = buildFactsSummary({ total: 1, by_category: [{ category: "schedule" }] });
    expect(s.by_category).toEqual([{ category: "schedule" }]);
  });
});

// ============================================================
// applyFactsCompletedEvent · SSE completed 缺字段即清空（2026-09-21 新增）
//   锁住修复：增量「全部跳过」的 completed 不带 segment_stats，
//   旧实现 `if (evt.segment_stats)` 只写不清 → 上一次提取的失败告警残留。
// ============================================================
describe("applyFactsCompletedEvent（completed 事件收口）", () => {
  it("有 segment_stats / cross_conflicts 时原样回传", () => {
    const out = applyFactsCompletedEvent({
      segment_stats: { failed: 2, total: 5 },
      cross_conflicts: [{ a: 1 }],
    });
    expect(out.segmentStats).toEqual({ failed: 2, total: 5 });
    expect(out.crossConflicts).toEqual([{ a: 1 }]);
  });

  it("缺 segment_stats（全部跳过）→ null（清空上一次的失败告警）", () => {
    const out = applyFactsCompletedEvent({ message: "全部 3 段均已提取过", skipped: 3 });
    expect(out.segmentStats).toBeNull();
    expect(out.crossConflicts).toEqual([]);
  });

  it("cross_conflicts 非数组 → 清空为 []（防脏值）", () => {
    expect(applyFactsCompletedEvent({ cross_conflicts: "x" }).crossConflicts).toEqual([]);
    expect(applyFactsCompletedEvent({ cross_conflicts: null }).crossConflicts).toEqual([]);
  });

  it("segment_stats 非对象 → null", () => {
    expect(applyFactsCompletedEvent({ segment_stats: "x" }).segmentStats).toBeNull();
  });

  it("null / 空事件安全回退", () => {
    expect(applyFactsCompletedEvent(null).segmentStats).toBeNull();
    expect(applyFactsCompletedEvent({}).crossConflicts).toEqual([]);
  });
});

// ============================================================
// 左侧分类面板（2026-09-23 改版）：目录树不再在事实页显示，左栏改为
// 「提取结果分类」，点击分类 → 右侧项目资料下方展示该分类详情。
//   buildFactsCategoryEntries：分组按 category 聚合（组数/条数/标记计数）
//   resolveSelectedFactCategory：选中悬空回退（分组删除/清空后不白屏）
//   filterFactsGroupsByCategory：右侧详情只取选中分类的分组
//   FactsCategoryPanel：渲染 + 点击上抛
// ============================================================
describe("buildFactsCategoryEntries（分类聚合）", () => {
  const groups = [
    {
      id: "g1", title: "工程概况", category: "project_info",
      items: [
        { name: "A", value: "1", is_simulated: true, is_resolved: false, has_conflict: false },
        { name: "B", value: "2", is_simulated: false, is_resolved: true, has_conflict: true },
      ],
    },
    {
      id: "g2", title: "项目名称", category: "project_info",
      items: [{ name: "C", value: "3", is_simulated: false, is_resolved: false, has_conflict: false }],
    },
    {
      id: "g3", title: "工期要求", category: "schedule",
      items: [{ name: "D", value: "4", is_simulated: false, is_resolved: true, has_conflict: false }],
    },
    { id: "g4", title: "未归类", items: [] }, // 无 category → 兜底 other
  ];

  it("同分类多组合并，计数正确", () => {
    const entries = buildFactsCategoryEntries(groups, (c) => ({ project_info: "项目信息", schedule: "工期安排" }[c] || ""));
    expect(entries.map((e) => e.category)).toEqual(["project_info", "schedule", "other"]);
    const pi = entries[0];
    expect(pi.title).toBe("项目信息");
    expect(pi.groupCount).toBe(2);
    expect(pi.itemCount).toBe(3);
    expect(pi.simulated).toBe(1);
    expect(pi.conflicts).toBe(1);
    expect(pi.unresolved).toBe(2);
  });

  it("labelOf 取不到中文名时回退 category 原文", () => {
    const entries = buildFactsCategoryEntries([groups[3]], () => "");
    expect(entries[0].category).toBe("other");
    expect(entries[0].title).toBe("other");
  });

  it("groups 空 / null 安全返回 []", () => {
    expect(buildFactsCategoryEntries([], (c) => c)).toEqual([]);
    expect(buildFactsCategoryEntries(null as any, (c) => c)).toEqual([]);
  });
});

describe("resolveSelectedFactCategory（选中悬空回退）", () => {
  const entries = [
    { category: "a", title: "A", groupCount: 1, itemCount: 1, simulated: 0, conflicts: 0, unresolved: 0 },
    { category: "b", title: "B", groupCount: 1, itemCount: 1, simulated: 0, conflicts: 0, unresolved: 0 },
  ];

  it("选中仍存在 → 原样保留", () => {
    expect(resolveSelectedFactCategory(entries, "b")).toBe("b");
  });

  it("选中悬空（分类被删）→ 回退第一个分类", () => {
    expect(resolveSelectedFactCategory(entries, "gone")).toBe("a");
    expect(resolveSelectedFactCategory(entries, "")).toBe("a");
  });

  it("无任何分类（尚未提取）→ null", () => {
    expect(resolveSelectedFactCategory([], "a")).toBeNull();
    expect(resolveSelectedFactCategory(null as any, "a")).toBeNull();
  });
});

describe("filterFactsGroupsByCategory（按分类取分组）", () => {
  const groups = [
    { id: "g1", category: "a", items: [{}] },
    { id: "g2", category: "b", items: [] },
    { id: "g3", items: [{}] }, // 无 category → other
  ];

  it("只返回选中分类的分组，保持原顺序", () => {
    expect(filterFactsGroupsByCategory(groups, "a").map((g) => g.id)).toEqual(["g1"]);
    expect(filterFactsGroupsByCategory(groups, "b").map((g) => g.id)).toEqual(["g2"]);
    expect(filterFactsGroupsByCategory(groups, "other").map((g) => g.id)).toEqual(["g3"]);
  });

  it("空分类串 → 返回全量（安全兜底）", () => {
    expect(filterFactsGroupsByCategory(groups, "")).toHaveLength(3);
    expect(filterFactsGroupsByCategory(null as any, "a")).toEqual([]);
  });
});

describe("FactsCategoryPanel（左侧分类面板组件）", () => {
  const entries = [
    { category: "a", title: "项目信息", groupCount: 2, itemCount: 3, simulated: 1, conflicts: 0, unresolved: 2 },
    { category: "b", title: "工期安排", groupCount: 1, itemCount: 4, simulated: 0, conflicts: 0, unresolved: 0 },
  ];

  it("渲染分类标题、条数徽标与模拟/待审标记", () => {
    const { getByText } = render(
      <FactsCategoryPanel entries={entries} totalItems={7} selected="a" onSelect={() => {}} />,
    );
    expect(getByText("事实分类")).toBeTruthy();
    expect(getByText("项目信息")).toBeTruthy();
    expect(getByText("工期安排")).toBeTruthy();
    expect(getByText("7 项")).toBeTruthy();
    expect(getByText("⚠️1")).toBeTruthy();
    expect(getByText("📝2")).toBeTruthy();
  });

  it("点击分类上抛对应 category；空态显示提示", () => {
    const onSelect = vi.fn();
    const { getByText } = render(
      <FactsCategoryPanel entries={entries} totalItems={7} selected="a" onSelect={onSelect} />,
    );
    fireEvent.click(getByText("工期安排"));
    expect(onSelect).toHaveBeenCalledWith("b");

    const { getByText: getByText2 } = render(
      <FactsCategoryPanel entries={[]} totalItems={0} selected={null} onSelect={onSelect} />,
    );
    expect(getByText2("暂无提取结果")).toBeTruthy();
  });
});


describe("事实安全与诊断交互", () => {
  it("批量确认安全跳过时返回告警，不误报注入就绪", () => {
    expect(buildBatchResolveResultCopy({ changed: 2, skipped: 1, skipped_safety_count: 3 })).toEqual({
      tone: "warning",
      text: "已确认 2 项；3 项模拟值或安全关键事实未放行，请逐条核对裁决",
    });
    expect(buildBatchResolveResultCopy({ changed: 2, skipped: 1, skipped_safety_count: 0 }).tone)
      .toBe("success");
  });

  it("方案请求守卫同时校验路由 ID 与 AbortSignal", () => {
    const ac = new AbortController();
    expect(isCurrentSchemeRequest("s1", "s1", ac.signal)).toBe(true);
    expect(isCurrentSchemeRequest("s1", "s2", ac.signal)).toBe(false);
    ac.abort();
    expect(isCurrentSchemeRequest("s1", "s1", ac.signal)).toBe(false);
  });

  it("诊断面板展示九章覆盖率、危大结论与缺失参数", () => {
    const onRefresh = vi.fn();
    const { getByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={{ chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5, missing_fields: ["工程规模"] }] }}
        dangerReport={{ classification: { is_hazardous: true, is_oversize: false }, missing_params: ["depth"] }}
        onRefresh={onRefresh}
      />,
    );
    expect(getByText("九大章节完整性 · 危大诊断")).toBeTruthy();
    expect(getByText("危大工程")).toBeTruthy();
    expect(getByText("缺参：depth")).toBeTruthy();
    expect(getByText("1. 工程概况")).toBeTruthy();
    fireEvent.click(getByText("刷新诊断"));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("诊断面板无数据时显示空态", () => {
    const { getByText } = render(
      <FactsDiagnosticsPanel loading={false} chapterReport={null} dangerReport={null} onRefresh={() => {}} />,
    );
    expect(getByText("暂无诊断数据")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// ✅ 2026-09-24：九大章节分类（建办质〔2018〕31号）—— 徽标取中文、未识别回退空
// ---------------------------------------------------------------------------
describe("factChapterTitle · 九大章节中文标题", () => {
  it("九个章节键全部有中文名", () => {
    expect(FACT_CHAPTER_TITLES).toEqual({
      overview: "工程概况",
      basis: "编制依据",
      plan: "施工计划",
      technique: "施工工艺技术",
      safety: "施工安全保证措施",
      personnel: "施工管理及作业人员配备和分工",
      acceptance: "验收要求",
      emergency: "应急处置措施",
      calc_drawings: "计算书及相关施工图纸",
    });
  });

  it("已知章节键返回中文标题", () => {
    expect(factChapterTitle("overview")).toBe("工程概况");
    expect(factChapterTitle("calc_drawings")).toBe("计算书及相关施工图纸");
  });

  it("空值 / 未识别的键返回空串（不渲染徽标）", () => {
    expect(factChapterTitle(undefined)).toBe("");
    expect(factChapterTitle(null)).toBe("");
    expect(factChapterTitle("")).toBe("");
    expect(factChapterTitle("nope")).toBe("");
  });
});

