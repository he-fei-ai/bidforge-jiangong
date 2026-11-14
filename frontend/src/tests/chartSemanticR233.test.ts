/**
 * R233（2026-11-13 · 图表生成与渲染模块深度探索增强）前端护栏。
 *
 * 锁定本轮 4 项前端修复 + ENH-1（R228 遗留③收口）：
 * - FE-1：svgToPng viewBox 谓词只校验宽高分量（mermaid v11 的 minX/minY
 *   通常 ≤ 0，旧 `every(n => n > 0)` 恒假 → 前端 PNG 渲染轨几乎每张图恒走
 *   1200×800 letterbox 静默失真）。
 * - FE-2：chart-json title 进 alt **属性上下文**必须经 escapeHtmlAttr
 *   （DOMPurify 对纯文本不转义引号，`图1" onload="…` 可逃逸出事件属性）。
 * - FE-3：needEngine 排除 ai_image 占位 + SVG 缓存 miss 不再静默留白整个
 *   worker 循环。
 * - FE-4：导出提示口径拆分 skipped_json（去重+设计跳过不再被误读为失败）。
 * - ENH-1：charts_semantic_notes 归一消费（normalizeChartYield 防御式 +
 *   chartSemanticTexts 文案 + SectionLogList Tooltip 接线）。
 */
import { describe, expect, it } from "vitest";

// ✅ R213 先例：?raw 导入一律裸写，不加 suppress 指令（vite/client 已声明 *?raw）
import exportChartsSrc from "../utils/exportCharts.ts?raw";
import rendererSrc from "../components/MarkdownRenderer.tsx?raw";
import contentEventsSrc from "../utils/contentEvents.ts?raw";
import sectionLogListSrc from "../pages/schemeWorkbench/SectionLogList.tsx?raw";
import useExportRunSrc from "../pages/schemeWorkbench/useExportRun.ts?raw";

import { normalizeChartYield, chartSemanticTexts } from "../utils/contentEvents";

describe("R233 ENH-1 · charts_semantic_notes 归一消费", () => {
  it("合法观测数组透传，count 缺失回 notes.length", () => {
    const out = normalizeChartYield({
      chart_count: 2,
      charts_dropped_count: 1,
      charts_dropped: [{ type: "gantt", reason: "scheme_type_limit" }],
      charts_semantic_notes: [{ type: "labor", detected: "gantt", score: 2, context: "进度安排如下：" }],
      charts_semantic_notes_count: 1,
    });
    expect(out.chart_count).toBe(2);
    expect(out.charts_dropped_count).toBe(1);
    expect(out.semantic_notes).toHaveLength(1);
    expect(out.semantic_notes![0].detected).toBe("gantt");
    expect(out.semantic_notes_count).toBe(1);
  });

  it("旧版后端缺键 / 非法形态 → 空数组，绝不抛异常", () => {
    const a = normalizeChartYield({ chart_count: 1 });
    expect(a.semantic_notes).toEqual([]);
    expect(a.semantic_notes_count).toBe(0);
    const b = normalizeChartYield({ chart_count: 1, charts_semantic_notes: "bad", charts_semantic_notes_count: -1 });
    expect(b.semantic_notes).toEqual([]);
    expect(b.semantic_notes_count).toBe(0);
    const c = normalizeChartYield(null);
    expect(c.semantic_notes).toEqual([]);
  });

  it("chartSemanticTexts：AI 选型 vs 程序检测的复核提示文案", () => {
    const lines = chartSemanticTexts([{ type: "labor", detected: "gantt", score: 3, context: "进度计划与工期安排" }]);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("劳动力配置图");
    expect(lines[0]).toContain("甘特图");
    expect(lines[0]).toContain("复核");
  });

  it("chartSemanticTexts：空清单 / undefined → 空数组（不渲染提示）", () => {
    expect(chartSemanticTexts(undefined)).toEqual([]);
    expect(chartSemanticTexts([])).toEqual([]);
  });
});

describe("R233 FE-1 · viewBox 谓词只校验宽高分量", () => {
  it("svgToPng 谓词已从 four-component every 收窄为宽高分量校验", () => {
    expect(exportChartsSrc).toContain("Number.isFinite(vb[2]) && vb[2] > 0");
    expect(exportChartsSrc).toContain("Number.isFinite(vb[3]) && vb[3] > 0");
    // 旧谓词不得回流（四个分量全部 >0 会把 mermaid 标准 viewBox 误判非法）
    expect(exportChartsSrc).not.toMatch(/vb\.every\(\(n\) => !Number\.isNaN\(n\) && n > 0\)/);
  });
  it("1200×800 兜底分支保留（真正畸形 viewBox 仍有兜底）", () => {
    expect(exportChartsSrc).toContain("w = 1200");
    expect(exportChartsSrc).toContain("h = 800");
  });
});

describe("R233 FE-2 · alt 属性上下文转义", () => {
  it("escapeHtmlAttr 已定义且用于 img alt 拼接", () => {
    expect(rendererSrc).toContain("function escapeHtmlAttr");
    expect(rendererSrc).toContain("alt=\"${escapeHtmlAttr(ph.json!.title)}\"");
  });
  it("旧写法（DOMPurify 直拼属性上下文）不得回流", () => {
    expect(rendererSrc).not.toContain('alt="${DOMPurify.sanitize(ph.json!.title)}"');
  });
  it("转义函数覆盖引号与尖括号", () => {
    // 源码锚：替换顺序覆盖 & → " → < → >（& 最先，防二次转义）
    const fn = rendererSrc.match(/function escapeHtmlAttr[\s\S]*?\n}/)?.[0] || "";
    expect(fn).toContain('.replace(/&/g, "&amp;")');
    expect(fn).toContain('.replace(/"/g, "&quot;")');
    expect(fn).toContain('.replace(/</g, "&lt;")');
    expect(fn).toContain('.replace(/>/g, "&gt;")');
  });
});

describe("R233 FE-3 · needEngine 排除 ai_image + 缓存 miss 兜底", () => {
  it("chartPhs 过滤排除 aiImage（只含 ai_image 块不再加载 mermaid 库）", () => {
    expect(rendererSrc).toContain("!p.json && !p.math && !p.aiImage");
  });
  it("SVG 缓存 miss 且引擎未就绪 → 抛错走错误占位通道（不再 return 整个 worker）", () => {
    expect(rendererSrc).toContain("图表渲染引擎未就绪（SVG 缓存已被淘汰）");
    // 旧写法不得回流：miss 分支静默 return 会让该块与后续块全部留白
    expect(rendererSrc).not.toMatch(/if \(!mermaid\) return;\s*\n\s*const \{ svg: _svg \}/);
  });
});

describe("R233 FE-4 · 导出提示口径拆分 skipped_json", () => {
  it("renderChartsForExport stats 支持 skipped_json（设计跳过计数）", () => {
    expect(exportChartsSrc).toContain("stats.skipped_json = (stats.skipped_json || 0) + 1");
    expect(exportChartsSrc).toContain("skipped_json?: number");
  });
  it("useExportRun 分母剔除 JSON 载荷条目（去重+跳过不再误读为失败）", () => {
    expect(useExportRunSrc).toContain("skipped_json: 0");
    expect(useExportRunSrc).toContain("items.length - chartRenderStats.skipped_json");
  });
});

describe("R233 ENH-1 · SectionLogList Tooltip 接线", () => {
  it("语义观测行并入配图 Tag 的 Tooltip（与删图理由同通道）", () => {
    expect(sectionLogListSrc).toContain("chartSemanticTexts");
    expect(sectionLogListSrc).toContain("const tipLines = [...texts, ...semTexts]");
  });
  it("Tag 颜色语义不变：有删图 warning、无删图 blue", () => {
    expect(sectionLogListSrc).toContain('texts.length > 0 ? "warning" : "blue"');
  });
  it("contentEvents 归一层含 CHART_SEMANTIC_TYPE_LABELS（7 类与后端同值）", () => {
    for (const t of ["flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"]) {
      expect(contentEventsSrc).toContain(`${t}:`);
    }
  });
});
