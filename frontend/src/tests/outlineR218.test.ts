/**
 * R218（2026-11-11）· 目录生成模块第三轮深度探索增强 —— 前端加固护栏。
 *
 * 覆盖：D-C（buildTree 映射 generation_standard / last_generation_standard /
 * word_status —— 刷新后章节级生成标准覆盖回显丢失）、D-D（/export-tree 与
 * /reorder 消费 unmounted_count 的 outlineUnmountedNotice 提示 —— 成环/悬挂
 * 章节静默缺章零提示）、D-E（快照回退消费 empty_top_chapters 空壳一级章提示）。
 *
 * 源码读取一律走 vite `?raw`，不执行被读模块。
 */
import { describe, expect, it } from "vitest";
import outlineTreeModelSrc from "../pages/schemeWorkbench/outlineTreeModel.ts?raw";
import workbenchTypesSrc from "../pages/schemeWorkbench/workbenchTypes.ts?raw";
import useOutlineActionsSrc from "../pages/schemeWorkbench/useOutlineActions.tsx?raw";
import useOutlineTreeEditSrc from "../pages/schemeWorkbench/useOutlineTreeEdit.tsx?raw";
import outlineVersionsPanelSrc from "../pages/schemeWorkbench/OutlineVersionsPanel.tsx?raw";

describe("R218 · buildTree 映射生成标准字段（D-C）", () => {
  it("映射 generation_standard（章节级覆盖回显，空串=沿用方案级）", () => {
    expect(outlineTreeModelSrc).toContain('generation_standard: n.generation_standard || ""');
  });
  it("映射 last_generation_standard", () => {
    expect(outlineTreeModelSrc).toContain("last_generation_standard: n.last_generation_standard");
  });
  it("映射 word_status", () => {
    expect(outlineTreeModelSrc).toContain("word_status: n.word_status");
  });
});

describe("R218 · OutlineNodeInput 声明三字段（D-C 契约）", () => {
  it("接口声明 generation_standard / last_generation_standard / word_status", () => {
    expect(workbenchTypesSrc).toContain("generation_standard?: string;");
    expect(workbenchTypesSrc).toContain("last_generation_standard?: string;");
    expect(workbenchTypesSrc).toContain("word_status?: string;");
  });
});

describe("R218 · unmounted_count 消费（D-D）", () => {
  it("/export-tree 导出目录提示不可达节点（不阻断导出）", () => {
    expect(useOutlineActionsSrc).toContain("outlineUnmountedNotice(data)");
  });
  it("/reorder 拖拽后提示不可达节点（不阻断重排）", () => {
    expect(useOutlineTreeEditSrc).toContain("outlineUnmountedNotice(_reorderData)");
  });
});

describe("R218 · empty_top_chapters 消费（D-E）", () => {
  it("快照回退检出空壳一级章并提示（不阻断回退）", () => {
    expect(outlineVersionsPanelSrc).toContain("empty_top_chapters");
    expect(outlineVersionsPanelSrc).toContain("_emptyTop > 0");
  });
});
