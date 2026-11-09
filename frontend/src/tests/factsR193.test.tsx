// @vitest-environment jsdom
/**
 * R193（2026-11-09）全局事实模块收口 · 前端护栏
 *
 * A 组 纯函数：buildFactsCategoryEntriesFromStats（L-07 全量数据源）
 * B 组 组件级：FactsSegmentFailuresAlert skipped 口径；FactsAdjustPanel
 *      Enter 在飞门禁（D-04 行为实证）
 * C 组 源码棘轮（?raw）：D-01 abort 迁 cleanup / D-02 切换复位运行态 /
 *      D-08 跨方案守卫 / D-03 apply 在飞锁 / D-06 safeFactText 主值位 /
 *      L-04 泛型与 signal 通道 / F4 skipped 契约
 * D 组 自证：锚点真存在（防 vacuous pass）
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup, waitFor, screen } from "@testing-library/react";
import React from "react";

import {
  buildFactsCategoryEntriesFromStats,
  resolveSelectedFactCategory,
  FACT_CATEGORY_OPTIONS,
} from "../pages/schemeWorkbench/factsShared";
import { FactsSegmentFailuresAlert } from "../pages/schemeWorkbench/FactsSegmentFailuresAlert";
import { FactsAdjustPanel } from "../pages/schemeWorkbench/FactsAdjustPanel";

import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import hookSrc from "../pages/schemeWorkbench/useFactsGeneration.ts?raw";
import panelSrc from "../pages/schemeWorkbench/FactsAdjustPanel.tsx?raw";
import groupListSrc from "../pages/schemeWorkbench/FactsGroupList.tsx?raw";
import apiSrc from "../api/index.ts?raw";
import typesSrc from "../types/facts.ts?raw";
import workbenchTypesSrc from "../pages/schemeWorkbench/workbenchTypes.ts?raw";
import crudSrc from "../pages/schemeWorkbench/useFactsCrud.ts?raw";

afterEach(cleanup);

// ===========================================================================
// A 组 · buildFactsCategoryEntriesFromStats（L-07）
// ===========================================================================
describe("buildFactsCategoryEntriesFromStats（L-07 全量数据源）", () => {
  const stats = {
    total: 5,
    by_category: [
      {
        category: "project_info",
        title: "基本信息",
        groups: 25,
        items: 60,
        simulated: 2,
        conflicts: 1,
        unresolved: 3,
        safety_critical: 0,
      },
      { category: "schedule", title: "工期安排", groups: 4, items: 9, simulated: 0, conflicts: 0, unresolved: 1 },
    ],
  };

  it("全量统计直映 entries（>20 组方案不再缺行）", () => {
    const entries = buildFactsCategoryEntriesFromStats(stats);
    expect(entries.map((e) => e.category)).toEqual(["project_info", "schedule"]);
    expect(entries[0].groupCount).toBe(25);
    expect(entries[0].itemCount).toBe(60);
    expect(entries[0].conflicts).toBe(1);
  });

  it("labelOf 覆盖后端 title（前端白名单优先）", () => {
    const entries = buildFactsCategoryEntriesFromStats(stats, (cat) =>
      FACT_CATEGORY_OPTIONS.find((o) => o.value === cat)?.label || "",
    );
    expect(entries[0].title).toBe("基本信息");
  });

  it("脏数据 / 缺 by_category 安全返回 []", () => {
    expect(buildFactsCategoryEntriesFromStats(null)).toEqual([]);
    expect(buildFactsCategoryEntriesFromStats({})).toEqual([]);
    expect(buildFactsCategoryEntriesFromStats({ by_category: [null, 42, { category: "x" }] }).length).toBe(3);
  });

  it("与分组排序同源：面板首项必为分组列表头部分类（默认选中不悬空）", () => {
    const entries = buildFactsCategoryEntriesFromStats(stats);
    expect(resolveSelectedFactCategory(entries, "")).toBe("project_info");
  });
});

// ===========================================================================
// B 组 · 组件级
// ===========================================================================
describe("FactsSegmentFailuresAlert · skipped 口径（F4）", () => {
  it("failed>0 且 skipped>0 时文案附增量跳过说明", () => {
    const { container } = render(
      <FactsSegmentFailuresAlert
        stats={{ ok: 6, failed: 2, total: 10, skipped: 2, failed_details: [{ index: 3, heading: "三", reason: "超时" }] }}
      />,
    );
    expect(container.textContent).toContain("2/10 段失败");
    expect(container.textContent).toContain("另有 2 段增量跳过");
  });

  it("skipped=0 时文案不含跳过说明（零行为变更）", () => {
    const { container } = render(<FactsSegmentFailuresAlert stats={{ ok: 8, failed: 2, total: 10 }} />);
    expect(container.textContent).not.toContain("增量跳过");
  });
});

describe("FactsAdjustPanel · Enter 在飞门禁（D-04 行为实证）", () => {
  function setup(previewImpl: (instruction: string, signal?: AbortSignal) => Promise<unknown>) {
    const preview = vi.fn(previewImpl);
    render(
      <FactsAdjustPanel
        schemeId="s1"
        preview={preview as never}
        apply={vi.fn() as never}
        onError={vi.fn()}
        confirmDialog={vi.fn() as never}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /AI 调整事实/ }));
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    return { preview, textarea };
  }

  it("预览在飞时再次按 Enter 不重复下发（旧行为：双份 AI 计费）", async () => {
    let release!: (v: unknown) => void;
    const gate = new Promise((r) => (release = r));
    const { preview, textarea } = setup(() => gate);
    fireEvent.change(textarea, { target: { value: "把开挖深度统一改为 8.5m" } });
    fireEvent.keyDown(textarea, { key: "Enter", keyCode: 13, shiftKey: false });
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    // 在飞期间再按 Enter：busyNow 门禁必须挡住第二次下发
    fireEvent.keyDown(textarea, { key: "Enter", keyCode: 13, shiftKey: false });
    expect(preview).toHaveBeenCalledTimes(1);
    release({ summary: "ok", operations: [] });
  });
});

// ===========================================================================
// C 组 · 源码棘轮（?raw，锁调用点而非只锁定义）
// ===========================================================================
describe("R193 源码棘轮", () => {
  it("D-01：factsListAbortRef 的 abort 已迁入列表 effect cleanup，切换 effect 不再持有", () => {
    const switchIdx = pageSrc.indexOf("const previousSchemeId = mountedSchemeIdRef.current;");
    const switchEnd = pageSrc.indexOf("initialTabDecidedRef.current = false;", switchIdx);
    const switchSeg = pageSrc.slice(switchIdx, switchEnd);
    expect(switchSeg).not.toContain("factsListAbortRef.current?.abort()");
    // cleanup 必须存在于 loadFacts effect（与 loadDocuments effect 的 docs 清理同构）
    expect(pageSrc).toContain("return () => {\n      factsListAbortRef.current?.abort();\n    };");
  });

  it("D-02：切换 effect 显式复位生成运行态（世代校验跳过复位后的唯一权威时机）", () => {
    const switchIdx = pageSrc.indexOf("const previousSchemeId = mountedSchemeIdRef.current;");
    const switchEnd = pageSrc.indexOf("initialTabDecidedRef.current = false;", switchIdx);
    const seg = pageSrc.slice(switchIdx, switchEnd);
    expect(seg).toContain("setGenerating(false)");
    expect(seg).toContain('setGenType("")');
    expect(seg).toContain("setTaskPaused(false)");
    expect(seg).toContain("setTaskStopping(false)");
    expect(seg).toContain('activeTaskIdRef.current = ""');
  });

  it("D-08：事实生成/重试链以 currentSchemeIdRef 守卫跨方案污染（两处）", () => {
    expect(hookSrc).toContain("currentSchemeIdRef?: MutableRefObject<string>");
    expect(hookSrc.match(/方案已切换/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("D-03/D-04：apply 在飞锁 + Enter 门禁对齐 busyNow", () => {
    expect(panelSrc).toContain("if (applyingRef.current) return;");
    expect(panelSrc).toContain("const busyNow = Boolean(busy) || adjusting || applying;");
    expect(panelSrc).toContain("text.trim() && !busyNow");
  });

  it("D-06：FactsGroupList 主值位走 safeFactText", () => {
    expect(groupListSrc).toContain("safeFactText(it.name)");
    expect(groupListSrc).toContain("safeFactText(it.value)");
  });

  it("L-04：adjust 带 signal 通道 + 9 个响应泛型在场", () => {
    expect(apiSrc).toContain("options?: { signal?: AbortSignal },\n  ) =>\n    api.post<FactAdjustResponse>");
    for (const t of [
      "FactUploadDocsResponse",
      "FactParseDocResponse",
      "FactParseAllResponse",
      "FactDocsListResponse",
      "FactDocPreviewResponse",
      "FactCategoriesResponse",
      "FactDocCategoryOptionsResponse",
      "FactDocDeleteResponse",
      "FactDocCategoryUpdateResponse",
    ]) {
      expect(apiSrc).toContain(`<${t}>`);
    }
    expect(typesSrc).toContain("stale_facts?: number;");
    expect(typesSrc).toContain("options: string[];");
  });

  it("D-11：翻页不重拉诊断（offset===0 才拉取）+ 刷新诊断不透传 MouseEvent", () => {
    expect(pageSrc).toContain("if (offset === 0) void loadFactsDiagnostics({ signal: ac.signal });");
  });

  it("D-09：useFactsCrud 六处错误文案走 detail 优先", () => {
    expect(crudSrc.match(/factsRequestErrorText\(e, /g)?.length).toBe(6);
    expect(crudSrc).not.toMatch(/msg\.error\(err\??\.message \|\| "/);
  });

  it("L-07：Page 分类面板数据源已切 FromStats（旧页内聚合不再作为面板数据源）", () => {
    expect(pageSrc).toContain("buildFactsCategoryEntriesFromStats(");
    expect(pageSrc).not.toContain("buildFactsCategoryEntries(\n        facts,");
  });
});

// ===========================================================================
// D 组 · 自证（锚点真存在，防 vacuous pass）
// ===========================================================================
describe("自证", () => {
  it("后端 segment_stats 生产方确实携带 skipped 键（F4 锚点有效性）", () => {
    expect(workbenchTypesSrc).toContain("skipped?: number;");
  });

  it("前端面板测试替身契约：preview 第 2 参为 signal（D-03 契约演进自证）", () => {
    expect(panelSrc).toContain("signal?: AbortSignal");
    expect(panelSrc).toContain("preview(instruction, ac.signal)");
  });
});
