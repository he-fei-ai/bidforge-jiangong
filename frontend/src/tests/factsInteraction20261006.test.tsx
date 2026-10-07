// @vitest-environment jsdom
/**
 * 全局事实 · 组件级交互测试（2026-10-06 补齐）
 *
 * 此前 `factsTab.test.tsx` 的 53 例全部打在**模块级纯函数**与两个抽出的
 * 展示组件上；事实工作流的每个真实按钮 —— 提取 / 批量确认 / 解除过期 /
 * 分类筛选 / 冲突裁决 / 清空 / 手动新增 —— **零组件级覆盖**。
 * 直接后果是 `ClassificationPanel` 读 `ch.completeness`（后端下发
 * `field_coverage`）这类字段名漂移能长期存活：九大章节进度条恒显示 0%，
 * 没有任何测试会发现。
 *
 * 本文件补三组此前完全缺失的覆盖：
 *   A  ClassificationPanel —— 逐章字段覆盖率（读错字段名的回归防线）
 *   B  FactsGroupList  —— 枚举注入（章节/属性/来源标题）与来源类型展示
 *   C  批量确认 / 解除过期 的结果文案与安全闸门提示
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import {
  FactsGroupList, buildBatchResolveResultCopy, buildBatchResolveCopy,
  factChapterTitle, factAttrTitle, factSourceKindTitle,
  FACT_CHAPTER_TITLES, FALLBACK_FACT_ATTR_TITLES, FALLBACK_SOURCE_KIND_TITLES,
} from "../pages/SchemeWorkbenchPage";
import { ClassificationPanel, chapterFieldCoverage, chapterTitleOf }
  from "../components/BidAnalysisTab";

afterEach(cleanup);

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

const noop = () => {};

/** antd Collapse 默认折叠 —— 条目级内容不在 DOM，必须先点开分组头。
 *  ⚠️ 必须用 queryByText：getByText 找不到时**抛异常**而非返回 undefined，
 *     `getByText(a) || getByText(b)` 会在第一个标题不存在时直接炸掉。 */
function expandFirstPanel(utils: {
  queryByText: (t: string) => HTMLElement | null;
  getByText: (t: string) => HTMLElement;
}) {
  const header = utils.queryByText("工程概况")
    || utils.queryByText("技术参数")
    || utils.queryByText("基本信息");
  expect(header).toBeTruthy();
  fireEvent.click(header as HTMLElement);
}

// ===========================================================================
// A. ClassificationPanel —— 逐章字段覆盖率
// ===========================================================================

describe("chapterFieldCoverage · 后端字段名契约", () => {
  it("读 field_coverage（后端 validate_chapter_fields 实际下发的键）", () => {
    // ✅ 修复前读 ch.completeness → undefined → NaN → 进度条恒 0%
    expect(chapterFieldCoverage({ field_coverage: 0.75 })).toBeCloseTo(0.75);
    expect(chapterFieldCoverage({ field_coverage: 1 })).toBe(1);
    expect(chapterFieldCoverage({ field_coverage: 0 })).toBe(0);
  });

  it("反向用例：仅有 completeness（旧/未来契约）时仍可取值", () => {
    expect(chapterFieldCoverage({ completeness: 0.5 })).toBeCloseTo(0.5);
  });

  it("两键皆缺 / 非数值 → 回落 0（不得渲染 NaN）", () => {
    expect(chapterFieldCoverage({})).toBe(0);
    expect(chapterFieldCoverage(null)).toBe(0);
    expect(chapterFieldCoverage(undefined)).toBe(0);
    expect(chapterFieldCoverage({ field_coverage: "abc" })).toBe(0);
  });

  it("越界值被夹到 [0,1]（后端异常值不得让进度条越界）", () => {
    expect(chapterFieldCoverage({ field_coverage: 1.4 })).toBe(1);
    expect(chapterFieldCoverage({ field_coverage: -0.2 })).toBe(0);
  });

  it("field_coverage 优先于 completeness", () => {
    expect(chapterFieldCoverage({ field_coverage: 0.25, completeness: 0.9 }))
      .toBeCloseTo(0.25);
  });
});

describe("chapterTitleOf · 章节标题来源优先级", () => {
  it("优先用响应自带 title（后端逐章下发）", () => {
    expect(chapterTitleOf({ title: "工程概况" }, "overview")).toBe("工程概况");
  });

  it("title 缺失时回落内置兜底表", () => {
    expect(chapterTitleOf({}, "basis")).toBe("编制依据");
  });

  it("title 与兜底表都没有时回落 key（不空白，便于排障）", () => {
    expect(chapterTitleOf({}, "brand_new_chapter")).toBe("brand_new_chapter");
  });
});

describe("ClassificationPanel · 逐章进度条渲染", () => {
  const mkPayload = () => ({
    classification: { category_names: ["基坑工程"], sub_names: [], is_hazardous: true, is_oversize: false, standards_keys: [] },
    chapter_completeness: {
      completeness: 0.6,
      chapters: {
        overview: { title: "工程概况", field_coverage: 1, missing_fields: [] },
        basis: { title: "编制依据", field_coverage: 0.5, missing_fields: ["适用法规清单"] },
        plan: { title: "施工计划", field_coverage: 0, missing_fields: ["材料需求清单"] },
      },
    },
  });

  it("逐章渲染后端 field_coverage 对应的百分比（修复前全为 0%）", () => {
    const { container } = render(<ClassificationPanel data={mkPayload()} />);
    const text = container.textContent || "";
    expect(text).toContain("100%");   // overview
    expect(text).toContain("50%");    // basis
    expect(text).toContain("0%");     // plan
  });

  it("总体百分比读顶层 completeness（与逐章字段名不同，勿混用）", () => {
    const { container } = render(<ClassificationPanel data={mkPayload()} />);
    expect(container.textContent || "").toContain("总体 60%");
  });

  it("缺失字段清单被渲染", () => {
    const { container } = render(<ClassificationPanel data={mkPayload()} />);
    const text = container.textContent || "";
    expect(text).toContain("适用法规清单");
    expect(text).toContain("材料需求清单");
  });

  it("章节标题用响应自带 title（后端增删章节后前端不失真）", () => {
    const payload = mkPayload();
    (payload.chapter_completeness.chapters as any).brand_new = {
      title: "后端新增章节", field_coverage: 0.3, missing_fields: [],
    };
    const { container } = render(<ClassificationPanel data={payload} />);
    expect(container.textContent || "").toContain("后端新增章节");
  });

  it("智能分类关闭时只渲染提示，不渲染进度条", () => {
    const { container } = render(
      <ClassificationPanel data={{ disabled: true, reason: "已关闭" }} />);
    expect(container.textContent || "").toContain("智能分类已关闭");
    expect(container.textContent || "").not.toContain("%");
  });

  it("分类失败时渲染错误提示", () => {
    const { container } = render(<ClassificationPanel data={{ error: "模型超时" }} />);
    expect(container.textContent || "").toContain("智能分类失败");
  });

  it("data 为空时渲染 null", () => {
    const { container } = render(<ClassificationPanel data={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("章节条目为 undefined 时不崩溃（回落空对象）", () => {
    const payload: any = mkPayload();
    payload.chapter_completeness.chapters = { overview: undefined };
    const { container } = render(<ClassificationPanel data={payload} />);
    expect(container.textContent || "").toContain("0%");
  });
});

// ===========================================================================
// B. 枚举注入：/category-map 覆盖兜底表
// ===========================================================================

describe("枚举标题解析 · category-map 注入", () => {
  it("缺省时用本地兜底表", () => {
    expect(factChapterTitle("safety")).toBe(FACT_CHAPTER_TITLES.safety);
    expect(factAttrTitle("norm")).toBe(FALLBACK_FACT_ATTR_TITLES.norm);
    expect(factSourceKindTitle("bid_doc")).toBe(FALLBACK_SOURCE_KIND_TITLES.bid_doc);
  });

  it("注入的 category-map 覆盖兜底表（后端增删枚举后界面跟随）", () => {
    const chapters = { safety: "后端改名·安全", brand_new: "后端新增章节" };
    expect(factChapterTitle("safety", chapters)).toBe("后端改名·安全");
    expect(factChapterTitle("brand_new", chapters)).toBe("后端新增章节");
    expect(factSourceKindTitle("survey", { survey: "后端改名·勘察" }))
      .toBe("后端改名·勘察");
  });

  it("空表 / 未注入时安全回落到兜底表（不得渲染空白）", () => {
    expect(factChapterTitle("safety", {})).toBe(FACT_CHAPTER_TITLES.safety);
    expect(factAttrTitle("norm", {})).toBe(FALLBACK_FACT_ATTR_TITLES.norm);
    expect(factSourceKindTitle("bid_doc", {})).toBe(FALLBACK_SOURCE_KIND_TITLES.bid_doc);
  });

  it("未知枚举键返回空串（不渲染标签，与 factChapterTitle 同口径）", () => {
    expect(factChapterTitle("nope")).toBe("");
    expect(factAttrTitle("nope")).toBe("");
    expect(factSourceKindTitle("nope")).toBe("");
  });

  it("空值返回空串（不渲染标签）", () => {
    expect(factChapterTitle(undefined)).toBe("");
    expect(factAttrTitle(null)).toBe("");
    expect(factSourceKindTitle("")).toBe("");
  });
});

// ===========================================================================
// C. FactsGroupList · 来源类型展示 + 枚举注入渲染
// ===========================================================================

describe("FactsGroupList · 来源类型与枚举注入渲染", () => {
  const mkGroups = (item: any) => ([{
    id: "g1", title: "工程概况", category: "basic", content: "md", items: [item],
  }]);

  const baseItem = {
    fact_id: "f1", name: "基坑深度", value: "12.5m",
    is_simulated: false, is_resolved: true, has_conflict: false, is_stale: false,
  };

  const renderList = (item: any, titles?: any) => {
    const utils = render(
      <FactsGroupList
        groups={mkGroups(item)}
        filter="all"
        onEditGroup={noop} onDeleteGroup={noop} onEditItem={noop}
        onResolveItem={noop} onResolveConflict={noop}
        chapterTitles={titles?.chapterTitles}
        factAttrTitles={titles?.factAttrTitles}
        sourceKindTitles={titles?.sourceKindTitles}
      />,
    );
    // ✅ antd Collapse 默认折叠：不展开则条目级标签根本不在 DOM 里
    expandFirstPanel(utils as any);
    return utils;
  };

  it("✅ 新增：渲染数据来源类型标签（此前后端下发但界面零展示）", () => {
    const { container } = renderList({ ...baseItem, source_kind: "drawing" });
    expect(container.textContent || "").toContain("施工图设计文件提取");
  });

  it("来源类型走注入的 category-map 标题", () => {
    const { container } = renderList(
      { ...baseItem, source_kind: "drawing" },
      { sourceKindTitles: { drawing: "图纸(注入)" } },
    );
    expect(container.textContent || "").toContain("图纸(注入)");
  });

  it("无 source_kind 时不渲染来源标签", () => {
    const { container } = renderList({ ...baseItem });
    expect(container.textContent || "").not.toContain("数据来源");
  });

  it("✅ 新增：事实属性用注入标题（旧实现是 4 个内联三元的缩写）", () => {
    const { container } = renderList(
      { ...baseItem, fact_attr: "quantitative" },
      { factAttrTitles: { quantitative: "定量事实(注入)" } },
    );
    expect(container.textContent || "").toContain("定量事实(注入)");
  });

  it("事实属性缺省时用完整名（不再是「定量」缩写）", () => {
    const { container } = renderList({ ...baseItem, fact_attr: "quantitative" });
    expect(container.textContent || "").toContain("定量事实");
  });

  it("章节徽标走注入标题", () => {
    const { container } = renderList(
      { ...baseItem, chapter: "safety" },
      { chapterTitles: { safety: "安全章(注入)" } },
    );
    expect(container.textContent || "").toContain("安全章(注入)");
  });

  it("未注入时全部回落兜底表（既有渲染点零行为变化）", () => {
    const { container } = renderList({
      ...baseItem, chapter: "safety", fact_attr: "norm", source_kind: "survey",
    });
    const text = container.textContent || "";
    expect(text).toContain("施工安全保证措施");
    expect(text).toContain("规范事实");
    expect(text).toContain("勘察报告提取");
  });

  it("未知枚举键一律不渲染标签（三套枚举同口径，不回落原键）", () => {
    const { container } = renderList({
      ...baseItem, chapter: "nope", fact_attr: "nope", source_kind: "nope",
    });
    const text = container.textContent || "";
    expect(text).not.toContain("📖");
    expect(text).not.toContain("🗂");
    expect(text).not.toContain("nope");
  });
});

// ===========================================================================
// D. 批量确认 / 解除过期的结果文案与安全闸门
// ===========================================================================

describe("批量确认结果文案", () => {
  const mkGroups = (items: any[]) => ([{
    id: "g1", title: "工程概况", category: "basic", content: "md", items,
  }]);

  it("被安全闸门拦下时按实际条数提示，不虚报成功", () => {
    // ✅ 返回形状是 { tone, text }（此前按字符串断言，属测试自身误用）
    const r = buildBatchResolveResultCopy({
      ok: true, changed: 3, skipped: 0,
      skipped_safety: [], skipped_safety_count: 2,
    } as any);
    expect(r.tone).toBe("warning");
    expect(r.text).toContain("3");
    expect(r.text).toContain("2");
  });

  it("全部被安全闸门拦下时文案非空且不谎报成功", () => {
    const r = buildBatchResolveResultCopy({
      ok: true, changed: 0, skipped: 0, skipped_safety_count: 5,
    } as any);
    expect(r.tone).toBe("warning");
    expect(r.text).toContain("0");
    expect(r.text).toContain("5");
  });

  it("无拦截时只报确认条数", () => {
    const r = buildBatchResolveResultCopy({
      ok: true, changed: 5, skipped: 0, skipped_safety_count: 0,
    } as any);
    expect(r.tone).toBe("success");
    expect(r.text).toContain("5");
    expect(r.text).not.toContain("未放行");
  });

  it("✅ 被安全闸门拦下时列出具体是哪几条（后端早已逐条回传 skipped_safety）", () => {
    const r = buildBatchResolveResultCopy({
      ok: true, changed: 3, skipped: 0, skipped_safety_count: 3,
      skipped_safety: [
        { name: "基坑深度" }, { name: "支护形式" }, { name: "混凝土强度等级" },
      ],
    } as any);
    expect(r.tone).toBe("warning");
    expect(r.blockedNames).toEqual(["基坑深度", "支护形式", "混凝土强度等级"]);
    expect(r.text).toContain("基坑深度");
    expect(r.text).toContain("混凝土强度等级");
  });

  it("被拦条数多时只列前 6 条并给出总数（不刷屏）", () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ name: `事实${i + 1}` }));
    const r = buildBatchResolveResultCopy({
      ok: true, changed: 0, skipped: 0, skipped_safety_count: 12,
      skipped_safety: many,
    } as any);
    expect(r.blockedNames.length).toBe(12);
    expect(r.text).toContain("事实1");
    expect(r.text).not.toContain("事实12");
    expect(r.text).toContain("12 条");
  });

  it("缺字段 / null 时安全回退（后端契约演进不致崩 UI）", () => {
    expect(() => buildBatchResolveResultCopy({} as any)).not.toThrow();
    expect(() => buildBatchResolveResultCopy(null as any)).not.toThrow();
    expect(buildBatchResolveResultCopy({} as any).text).toContain("0");
    expect(buildBatchResolveResultCopy({} as any).blockedNames).toEqual([]);
  });

  it("预检文案：含模拟值时升级为 warning 并给出核对提示", () => {
    // ✅ buildBatchResolveCopy 收 stats 入参、返回结构体（此前按 (n, n) + 字符串断言）
    const r = buildBatchResolveCopy({ unresolved: 5, simulated: 2, conflicts: 1 });
    expect(r.headline).toContain("5");
    expect(r.confirmType).toBe("warning");
    expect(r.simulatedNote).toContain("2");
    expect(r.conflictNote).toContain("1");
  });

  it("预检文案：无模拟值时用普通确认且不带模拟提示", () => {
    const r = buildBatchResolveCopy({ unresolved: 3, simulated: 0, conflicts: 0 });
    expect(r.confirmType).toBe("confirm");
    expect(r.simulatedNote).toBe("");
    expect(r.conflictNote).toBe("");
  });

  it("预检文案：stats 缺失时安全回退为 0", () => {
    expect(() => buildBatchResolveCopy(null)).not.toThrow();
    expect(buildBatchResolveCopy(null).headline).toContain("0");
  });
});

describe("冲突裁决交互", () => {
  const conflictItem = {
    fact_id: "f1", name: "混凝土强度等级", value: "C30",
    is_simulated: false, is_resolved: false, has_conflict: true, is_stale: false,
    conflict_values: [
      { value: "C35", source: "设计说明", confidence: 0.9 },
      { value: "C25", source: "施工组织设计", confidence: 0.6 },
    ],
  };

  const renderConflict = (item: any, onResolveConflict = vi.fn()) => {
    const r = render(
      <FactsGroupList
        groups={[{ id: "g1", title: "技术参数", category: "tech_param", content: "md", items: [item] }]}
        filter="all"
        onEditGroup={noop} onDeleteGroup={noop} onEditItem={noop}
        onResolveItem={noop} onResolveConflict={onResolveConflict}
      />,
    );
    // ✅ antd Collapse 默认折叠：不展开则裁决按钮不在 DOM 里
    expandFirstPanel(r as any);
    return { ...r, onResolveConflict };
  };

  it("「保留当前值」上抛当前 item", () => {
    const { getByText, onResolveConflict } = renderConflict(conflictItem);
    fireEvent.click(getByText("保留当前值"));
    expect(onResolveConflict).toHaveBeenCalledWith(
      expect.objectContaining({ fact_id: "f1" }), "C30",
    );
  });

  it("「选此值」逐个上抛各自候选值（多候选 → 多个按钮）", () => {
    const { getAllByText, onResolveConflict } = renderConflict(conflictItem);
    // 候选值列表只含「其它取值」，每个候选一个「选此值」按钮
    const btns = getAllByText("选此值");
    expect(btns.length).toBe(conflictItem.conflict_values.length);
    btns.forEach((b: HTMLElement) => fireEvent.click(b));
    const calls = onResolveConflict.mock.calls;
    expect(calls.length).toBe(2);
    expect(calls[0][0].fact_id).toBe("f1");
    expect(calls.map((c: any[]) => c[1]).sort())
      .toEqual(["C25", "C35"]);
  });

  it("当前值始终提供「保留当前值」入口（即便某候选值与它相同）", () => {
    // 候选值列表按「其它取值」语义构造，正常不会与当前值重复；
    // 即便数据异常重复，也不应剥夺用户「保留当前值」的权利。
    const dup = { ...conflictItem, conflict_values: [{ value: "C30", source: "另一处" }] };
    const { getByText, onResolveConflict } = renderConflict(dup);
    fireEvent.click(getByText("保留当前值"));
    expect(onResolveConflict).toHaveBeenCalledWith(
      expect.objectContaining({ fact_id: "f1" }), "C30",
    );
  });

  it("无矛盾事实不渲染裁决区", () => {
    const { queryByText } = renderConflict({ ...conflictItem, has_conflict: false });
    expect(queryByText("保留当前值")).toBeNull();
    expect(queryByText("选此值")).toBeNull();
  });
});
