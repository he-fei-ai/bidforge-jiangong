// @vitest-environment jsdom
/**
 * 全局事实 · 九章事实分布面板与分页契约（R89 · 2026-10-17）
 *
 * 本文件存在的理由是一个**已实证的真实缺陷**，以及它暴露出的测试缺口：
 *
 * BUG-1【P0 · 前后端契约断裂】
 *   后端 `list_facts` 的 `stats.by_chapter` 是**对象**
 *   （`{chapters, totals, by_fact_attr, by_source_kind, shared_count}`），
 *   前端 `buildFactChapterStats` 却按 `Array.isArray` 取值 ⇒ 恒 `[]` ⇒
 *   `FactsChapterCoveragePanel` 自 2026-10-06 引入起**永久**显示「暂无章节统计」。
 *   ⚠️ 更致命的是 `types/facts.ts` 当时也把该字段声明成数组 —— 契约与实现
 *   **同错**，`tsc --noEmit` 在原理上不可能报错。
 *   ⚠️ 该缺陷能长期存活的第二个原因就是本文件要补的东西：
 *   `FactsChapterCoveragePanel` / `buildFactChapterStats` / `buildFactsPagination`
 *   **三者全部零用例** —— 一个「看起来正常的」空面板没人断言。
 *
 * BUG-2【P1 · 分页页码可产出小数 / NaN】
 *   `(offset)/limit + 1` 未取整；`Math.min/max` 对 NaN 恒返回 NaN。
 *
 * 判据纪律：面板类测试必须断言**真实渲染出的行**，而不是只断言纯函数返回值 ——
 * 「函数对了但没接线」是本仓反复记录的盲区。
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import {
  FactsChapterCoveragePanel,
  buildFactChapterStats,
  buildFactsPagination,
  FACTS_PAGE_SIZE,
  buildFactsSummary,
} from "../pages/SchemeWorkbenchPage";
import type { FactChapterStatBlock } from "../types/facts";

afterEach(cleanup);

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// ============================================================
// 真实后端形状（逐字段对照 backend  _chapter_stats_for_items）
// ============================================================
/**
 * ⚠️ 必须显式标注 `FactChapterStatBlock`（而不是 `as any`）：
 *   这是 `buildFactChapterStats` 真实类型生效的直接证据 —— 标注后
 *   `chapters[].key` 必须落在 `FactChapterKey` 闭集内，若契约与后端分叉
 *   （例如某章键被改）本文件在 **tsc 阶段**即失败，而不是运行时静默降级。
 *   首版未标注时 `key` 被推断为 `string`，tsc 报 TS2345 —— 这正是「类型
 *   真的接上了」的可观察反馈。
 */
const REAL_STATS: { by_chapter: FactChapterStatBlock } = {
  by_chapter: {
    chapters: [
      { order: 1, key: "overview", title: "工程概况", count: 12, coverage: 0.75,
        missing_fields: ["建设单位", "结构形式"], shared_count: 3 },
      { order: 2, key: "basis", title: "编制依据", count: 5, coverage: 0.5,
        missing_fields: ["执行标准"], shared_count: 1 },
      { order: 3, key: "plan", title: "施工计划", count: 0, coverage: 0,
        missing_fields: [], shared_count: 0 },
    ],
    totals: { facts: 17, uncategorized: 0, total_fields: 12, covered_fields: 9,
      coverage: 0.75, missing_chapters: 7 },
    by_fact_attr: { quantitative: 9, qualitative: 8 },
    by_source_kind: { bid_doc: 12, manual: 5 },
    shared_count: 4,
  },
};

// ============================================================
// A · buildFactChapterStats —— 形状契约（BUG-1 的行为基线）
// ============================================================
describe("buildFactChapterStats · by_chapter 形状契约（BUG-1）", () => {
  it("【核心回归】对象包裹形状必须被解析出九章行（旧实现恒返回 []）", () => {
    const rows = buildFactChapterStats(REAL_STATS);
    expect(rows).toHaveLength(3);
    expect(rows[0].key).toBe("overview");
    expect(rows[0].count).toBe(12);
  });

  it("按 order 升序（面板标题带序号，乱序会显示错位）", () => {
    const shuffled = {
      by_chapter: {
        ...REAL_STATS.by_chapter,
        chapters: [
          REAL_STATS.by_chapter.chapters[2],
          REAL_STATS.by_chapter.chapters[0],
          REAL_STATS.by_chapter.chapters[1],
        ],
      },
    };
    expect(buildFactChapterStats(shuffled).map((r) => r.order)).toEqual([1, 2, 3]);
  });

  it("逐章 shared_count 落到 shared 字段（旧的顶层合计语义错位 ⇒ 恒 0）", () => {
    const rows = buildFactChapterStats(REAL_STATS);
    expect(rows.map((r) => r.shared)).toEqual([3, 1, 0]);
  });

  it("missing_fields → missingCount / missingFields（逐项字符串化）", () => {
    const rows = buildFactChapterStats(REAL_STATS);
    expect(rows[0].missingCount).toBe(2);
    expect(rows[0].missingFields).toEqual(["建设单位", "结构形式"]);
    expect(rows[2].missingCount).toBe(0);
  });

  it("容错：裸数组（旧后端/未来扁平化）仍能解析，不炸", () => {
    const rows = buildFactChapterStats(
      { by_chapter: REAL_STATS.by_chapter.chapters as any });
    expect(rows).toHaveLength(3);
  });

  it("容错：缺 by_chapter / null / undefined 一律返回空数组（不抛）", () => {
    expect(buildFactChapterStats({})).toEqual([]);
    expect(buildFactChapterStats(null)).toEqual([]);
    expect(buildFactChapterStats(undefined)).toEqual([]);
    expect(buildFactChapterStats({ by_chapter: {} as any })).toEqual([]);
  });

  it("容错：脏字段（order/count 缺失或非数字）归一为 0，不产生 NaN", () => {
    const rows = buildFactChapterStats({
      by_chapter: {
        chapters: [
          { key: "overview", count: "7", order: undefined, shared_count: null },
        ],
      } as any,
    });
    expect(rows[0].order).toBe(0);
    expect(rows[0].count).toBe(7);   // Number("7") = 7
    expect(rows[0].shared).toBe(0);  // Number(null) = 0
  });

  it("title 缺失时用 titles 注入回填，再退到本地兜底表", () => {
    const rows = buildFactChapterStats(
      { by_chapter: { chapters: [{ order: 1, key: "overview", count: 1 }] } } as any,
      { overview: "注入的工程概况" });
    expect(rows[0].title).toBe("注入的工程概况");
    const rows2 = buildFactChapterStats(
      { by_chapter: { chapters: [{ order: 1, key: "basis", count: 1 }] } } as any);
    expect(rows2[0].title).toBe("编制依据");
  });
});

// ============================================================
// B · FactsChapterCoveragePanel —— 组件级（此前零覆盖）
// ============================================================
describe("FactsChapterCoveragePanel · 组件级渲染", () => {
  it("【核心回归】真实对象形状下必须渲染出九章行（修复前只有空态）", () => {
    const { container } = render(
      <FactsChapterCoveragePanel stats={REAL_STATS} />);
    const txt = container.textContent || "";
    expect(txt).not.toContain("暂无章节统计");
    expect(txt).toContain("九章事实分布（共 17 条）");
    expect(txt).toContain("工程概况");
    expect(txt).toContain("编制依据");
    expect(txt).toContain("施工计划");
  });

  it("每章渲染条数；count=0 显示「无事实」而非「0 条」", () => {
    const { container } = render(
      <FactsChapterCoveragePanel stats={REAL_STATS} />);
    const txt = container.textContent || "";
    expect(txt).toContain("12 条");
    expect(txt).toContain("无事实");
  });

  it("shared>0 时渲染「共性 N」（该列修复前恒不显示）", () => {
    const { container } = render(
      <FactsChapterCoveragePanel stats={REAL_STATS} />);
    expect(container.textContent || "").toContain("共性 3");
  });

  it("缺字段清单渲染，且超过 4 项截断加省略号", () => {
    const many = {
      by_chapter: {
        chapters: [{
          order: 1, key: "overview", title: "工程概况", count: 1,
          missing_fields: ["f1", "f2", "f3", "f4", "f5", "f6"],
        }],
      },
    } as any;
    const { container } = render(<FactsChapterCoveragePanel stats={many} />);
    const txt = container.textContent || "";
    expect(txt).toContain("缺字段（6）");
    expect(txt).toContain("f1、f2、f3、f4");
    expect(txt).not.toContain("f5");
    expect(txt).toContain("…");
  });

  it("空态：无章节统计时显示占位文案且不渲染任何行", () => {
    const { container } = render(<FactsChapterCoveragePanel stats={{}} />);
    expect(container.textContent || "").toContain("暂无章节统计");
  });

  it("titles 注入生效（/category-map 下发的中文名优先于本地兜底）", () => {
    const { container } = render(
      <FactsChapterCoveragePanel
        stats={{ by_chapter: { chapters: [{ order: 1, key: "calc_drawings", count: 2 }] } } as any}
        titles={{ calc_drawings: "计算书及图纸" }} />);
    expect(container.textContent || "").toContain("计算书及图纸");
  });

  it("全部章节 count=0 时进度条不得 NaN%（totalFacts 为 0 的除零防护）", () => {
    const zero = {
      by_chapter: {
        chapters: [
          { order: 1, key: "overview", count: 0 },
          { order: 2, key: "basis", count: 0 },
        ],
      },
    } as any;
    const { container } = render(<FactsChapterCoveragePanel stats={zero} />);
    expect(container.textContent || "").not.toContain("NaN");
  });
});

// ============================================================
// C · buildFactsPagination（BUG-2）
// ============================================================
describe("buildFactsPagination · 页码契约（BUG-2）", () => {
  it("正常整倍数 offset 还原为正确页码", () => {
    const r = buildFactsPagination({ total_groups: 100, offset: 40 }, 20);
    expect(r).toMatchObject({ page: 3, pageSize: 20, total: 100, totalPages: 5, show: true });
  });

  it("【核心回归】offset 非 limit 整倍数时页码必须是整数（修复前得 2.25）", () => {
    const r = buildFactsPagination({ total_groups: 100, offset: 25 }, 20);
    expect(Number.isInteger(r.page)).toBe(true);
    expect(r.page).toBe(2);
  });

  it("【核心回归】offset 缺失/非法时不得产出 NaN（Math.min/max 对 NaN 恒 NaN）", () => {
    for (const bad of [{}, { offset: undefined }, { offset: NaN },
      { offset: Infinity }, { offset: -5 }, { offset: null }]) {
      const r = buildFactsPagination({ total_groups: 60, ...(bad as any) }, 20);
      expect(Number.isFinite(r.page)).toBe(true);
      expect(r.page).toBe(1);
    }
  });

  it("offset 超界夹到 totalPages（末页之后不越界）", () => {
    const r = buildFactsPagination({ total_groups: 30, offset: 999 }, 20);
    expect(r.totalPages).toBe(2);
    expect(r.page).toBe(2);
  });

  it("total<=limit 时不渲染分页控件（show=false）", () => {
    expect(buildFactsPagination({ total_groups: 5, offset: 0 }, 20).show).toBe(false);
    expect(buildFactsPagination({ total_groups: 20, offset: 0 }, 20).show).toBe(false);
    expect(buildFactsPagination({ total_groups: 21, offset: 0 }, 20).show).toBe(true);
  });

  it("pageSize 恒 >= 1，页码恒落在 [1, totalPages]（对任意输入的不变式）", () => {
    // 0 / NaN / 缺失 → `|| 20` 回落 20；负数 → `Math.max(1, ·)` 夹到 1。
    // ⚠️ 负数夹到 1 是**既有行为**（pageSize 恒为编译期常量 FACTS_PAGE_SIZE，
    //    无真实调用方传负数），此处刻意只锁「>=1 且页码不越界」这一不变式，
    //    不把「负数该回落 20 还是夹到 1」固化成契约 —— 那会把一个无调用方的
    //    任意选择锁死，日后想改就得先改护栏。
    for (const ps of [20, 1, 0, -5, NaN, undefined as any, null as any]) {
      const r = buildFactsPagination({ total_groups: 40, offset: 20 }, ps);
      expect(r.pageSize).toBeGreaterThanOrEqual(1);
      expect(Number.isInteger(r.page)).toBe(true);
      expect(r.page).toBeGreaterThanOrEqual(1);
      expect(r.page).toBeLessThanOrEqual(r.totalPages);
    }
    // 0 / NaN 走 `|| 20` 显式回落（这两条是调用方真正可能传来的形态）
    expect(buildFactsPagination({ total_groups: 40, offset: 20 }, 0).pageSize).toBe(20);
    expect(buildFactsPagination({ total_groups: 40, offset: 20 }, NaN).pageSize).toBe(20);
  });

  it("total=0 时 totalPages 下限 1（不渲染分页但页码仍合法）", () => {
    const empty = buildFactsPagination({ total_groups: 0, offset: 0 }, 20);
    expect(empty).toMatchObject({ page: 1, totalPages: 1, show: false });
  });

  it("FACTS_PAGE_SIZE 与 loadFacts 的偏移计算口径一致（单一常量）", () => {
    expect(FACTS_PAGE_SIZE).toBe(20);
    // loadFacts 用 (page-1)*FACTS_PAGE_SIZE 作为 offset ⇒ 必须能被页码整除还原
    for (const page of [1, 2, 3, 7]) {
      const r = buildFactsPagination(
        { total_groups: 200, offset: (page - 1) * FACTS_PAGE_SIZE }, FACTS_PAGE_SIZE);
      expect(r.page).toBe(page);
    }
  });
});

// ============================================================
// D · 零回归：has_warnings 口径不被本轮改动波及
// ============================================================
describe("buildFactsSummary · 门控计数口径（零回归锁）", () => {
  it("stale 必须计入 has_warnings（漏算会误显示「全部就绪」并放行正文生成）", () => {
    const s = buildFactsSummary({ total: 5, simulated: 0, unresolved: 0, conflicts: 0, stale: 2 });
    expect(s!.has_warnings).toBe(true);
  });

  it("四类计数全 0 时 has_warnings=false（不制造噪音）", () => {
    const s = buildFactsSummary({ total: 5, simulated: 0, unresolved: 0, conflicts: 0, stale: 0 });
    expect(s!.has_warnings).toBe(false);
  });

  it("仍保留后端原样下发的统计维度（by_chapter 不得被归一过程丢弃）", () => {
    const s = buildFactsSummary({ total: 17, simulated: 0, unresolved: 0, conflicts: 0, stale: 0,
      by_chapter: REAL_STATS.by_chapter } as any);
    expect((s as any).by_chapter.chapters).toHaveLength(3);
  });
});
