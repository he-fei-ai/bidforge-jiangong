// @vitest-environment jsdom
/**
 * 事实面板两个用户可踩缺陷 · R176（2026-11-04）前端护栏。
 *
 * 1. 刷新失败沿用旧结论：`mergeFactsDiagnosticsSettled` 在 danger-check 失败时
 *    不设 `danger` 键，旧实现 `if (merged.danger !== undefined)` 因此保留上一次
 *    报告 → 刷新失败后界面仍显示上一次「已识别 / 未识别」。本组锁定：
 *    「先成功载入 → 再失败刷新」的序列，最终必须清空并落到「无法判定」。
 *
 * 2. 切方案带旧页码：`/scheme/:id` 复用组件不卸载，`factsPageRef` 保留旧页，
 *    新方案首个请求带着旧 offset（后端 clamp 只是补救）。本组锁定：
 *    方案变化 ⇒ 第 1 页（offset=0）；同方案翻页零回归。
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import {
  FACTS_PAGE_SIZE,
  mergeFactsDiagnosticsSettled,
  resolveFactsChapterSettled,
  resolveFactsDangerSettled,
  resolveFactsLoadPage,
} from "../pages/SchemeWorkbenchPage";
import { FactsDiagnosticsPanel } from "../pages/schemeWorkbench/FactsDiagnosticsPanel";
import type {
  DangerReportLike,
  FactsChapterReport,
} from "../pages/schemeWorkbench/FactsDiagnosticsPanel";

afterEach(cleanup);

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

const ok = <T,>(data: T) => ({ status: "fulfilled" as const, value: { data } });
const fail = (message: string) => ({ status: "rejected" as const, reason: { message } });

/**
 * 夹具必须与 `FactsDiagnosticsPanel` 的入参声明严格对齐，避免
 * `mergeFactsDiagnosticsSettled` 的默认泛型（`unknown`）混入测试对象。
 */
const mergeDiag = (
  chaptersRes: PromiseSettledResult<{ data?: FactsChapterReport }>,
  dangerRes: PromiseSettledResult<{ data?: DangerReportLike }>,
) => mergeFactsDiagnosticsSettled<FactsChapterReport, DangerReportLike>(chaptersRes, dangerRes);

describe("刷新失败不得沿用旧结论（R176）", () => {
  it("序列：先成功载入（false 结论）→ 再失败刷新 ⇒ 旧结论必须被清空", () => {
    const first = mergeDiag(
      ok({ chapters: [{ key: "overview", title: "工程概况" }] }),
      ok({ classification: { is_hazardous: false, is_oversize: false } }),
    );
    const loaded = resolveFactsDangerSettled(first);
    expect(loaded).not.toBeNull();
    expect(loaded?.classification?.is_hazardous).toBe(false);

    // 第二次刷新：chapters 照常成功、danger-check 500
    const second = mergeDiag(ok({ chapters: [] }), fail("危大分类服务不可用"));
    const refreshed = resolveFactsDangerSettled(second);
    expect(refreshed).toBeNull(); // 旧结论不得残留
    expect(second.warning).toContain("危大分类服务不可用");
  });

  it("先成功（true 结论）→ 再失败刷新 ⇒ 也不得残留「危大工程」肯定结论", () => {
    const first = resolveFactsDangerSettled(
      mergeDiag(
        ok({ chapters: [] }),
        ok({ classification: { is_hazardous: true, is_oversize: true } }),
      ),
    );
    expect(first?.classification?.is_hazardous).toBe(true);
    expect(resolveFactsDangerSettled(mergeDiag(ok({ chapters: [] }), fail("timeout")))).toBeNull();
  });

  it("失败后渲染面板：只允许「无法判定」，不得出现任何旧结论文案", () => {
    const status = resolveFactsDangerSettled(
      mergeDiag(
        ok({ chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5 }] }),
        fail("HTTP 500"),
      ),
    );
    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={{
          chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5 }],
        }}
        dangerReport={status}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("无法判定（危大分类数据不可用）")).toBeTruthy();
    expect(getByText("无法判定（超规模）")).toBeTruthy();
    expect(queryByText("未识别为危大工程")).toBeNull();
    expect(queryByText("危大工程")).toBeNull();
  });

  it("成功刷新仍照常更新结论（零回归）", () => {
    const r = resolveFactsDangerSettled(
      mergeDiag(ok({ chapters: [] }), ok({ classification: { is_hazardous: true } })),
    );
    expect(r).not.toBeNull();
  });
});

describe("章节覆盖度刷新失败不得沿用旧数据（R177）", () => {
  it("序列：先成功载入覆盖率 → 再失败刷新 ⇒ 清空且渲染「无法判定」，旧覆盖率不得残留", () => {
    const first = mergeDiag(
      ok({ chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5 }] }),
      ok({ classification: { is_hazardous: false, is_oversize: false } }),
    );
    const loaded = resolveFactsChapterSettled(first);
    expect(loaded?.chapters?.[0]?.title).toBe("工程概况");
    expect(loaded?.chapters?.[0]?.coverage).toBe(0.5);

    // 第二次刷新：章节覆盖度 500、危大照常成功
    const second = mergeDiag(
      fail("章节覆盖度服务不可用"),
      ok({ classification: { is_hazardous: false, is_oversize: false } }),
    );
    const refreshed = resolveFactsChapterSettled(second);
    expect(refreshed).toBeNull(); // 旧覆盖率不得残留
    expect(second.warning).toContain("章节覆盖度服务不可用");

    const { getByText, queryByText } = render(
      <FactsDiagnosticsPanel
        loading={false}
        chapterReport={refreshed}
        dangerReport={{ classification: { is_hazardous: false, is_oversize: false } }}
        onRefresh={() => {}}
      />,
    );
    expect(getByText("无法判定（章节覆盖度数据不可用）")).toBeTruthy();
    expect(queryByText("工程概况")).toBeNull(); // 上一次的章名/覆盖率不得残留
    expect(queryByText("50%")).toBeNull();
  });

  it("成功但无数据（fulfilled 且 data 缺失）同样按「无法判定」，不沿用旧值", () => {
    const loaded = resolveFactsChapterSettled(
      mergeDiag(
        ok({ chapters: [{ order: 1, key: "overview", title: "工程概况", coverage: 0.5 }] }),
        ok({ classification: { is_hazardous: false } }),
      ),
    );
    expect(loaded).not.toBeNull();
    expect(
      resolveFactsChapterSettled(
        mergeDiag(ok(undefined), ok({ classification: { is_hazardous: false } })),
      ),
    ).toBeNull();
  });

  it("成功刷新仍照常更新覆盖率（零回归）", () => {
    const r = resolveFactsChapterSettled(
      mergeDiag(
        ok({ chapters: [{ order: 2, key: "basis", title: "编制依据", coverage: 1 }] }),
        ok({ classification: { is_hazardous: false } }),
      ),
    );
    expect(r?.chapters?.[0]?.title).toBe("编制依据");
  });
});

describe("切方案页码回第 1 页（R176 · BUG-06 前端半边）", () => {
  it("方案变化 ⇒ 第 1 页（首请求 offset = 0）", () => {
    expect(resolveFactsLoadPage({ schemeId: "B", lastSchemeId: "A", refPage: 3 })).toBe(1);
    expect((1 - 1) * FACTS_PAGE_SIZE).toBe(0);
    // 首次挂载（lastSchemeId 空串）同样从第 1 页开始
    expect(resolveFactsLoadPage({ schemeId: "A", lastSchemeId: "", refPage: 5 })).toBe(1);
  });

  it("同方案翻页：override 与 ref 原优先级不变（零回归）", () => {
    expect(resolveFactsLoadPage({ schemeId: "A", lastSchemeId: "A", refPage: 3 })).toBe(3);
    expect(
      resolveFactsLoadPage({ schemeId: "A", lastSchemeId: "A", pageOverride: 2, refPage: 3 }),
    ).toBe(2);
    expect(
      resolveFactsLoadPage({ schemeId: "A", lastSchemeId: "A", pageOverride: NaN, refPage: 3 }),
    ).toBe(3);
    expect(
      resolveFactsLoadPage({ schemeId: "A", lastSchemeId: "A", pageOverride: 0, refPage: 3 }),
    ).toBe(3);
  });

  it("切换后再翻页不被误重置（方案登记后同方案即正常翻页）", () => {
    const p1 = resolveFactsLoadPage({ schemeId: "B", lastSchemeId: "A", refPage: 3 });
    expect(p1).toBe(1);
    // 页面在首个请求后把 lastSchemeId 登记为 B —— 第 2 页须正常放行
    expect(
      resolveFactsLoadPage({ schemeId: "B", lastSchemeId: "B", pageOverride: 2, refPage: 1 }),
    ).toBe(2);
  });
});
