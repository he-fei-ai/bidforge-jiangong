// @vitest-environment node
/**
 * R252（2026-11-16）：甘特图源码泄漏进导出 DOCX 根因修复 —— 前端护栏。
 *
 * 与后端 test_chart_fence_lang_alias_r252_20261116.py 同源：
 *   - canonicalChartFenceLang：图表类型名 → mermaid；真实代码围栏不误伤
 *   - normalizeChartFenceShapes：就地归一围栏标签（行数不变 + 幂等）
 *   - ensureGanttDeclaration：给缺首行 gantt 的载荷补声明（mermaid.js 要求）
 *   - 前后端 alias 集合 parity 源码锁
 */
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import {
  canonicalChartFenceLang,
  isChartFenceLang,
  chartFenceLang,
  ensureGanttDeclaration,
  normalizeChartFenceShapes,
  CHART_FENCE_LANG_ALIASES,
  scanFenceBlocks,
} from "../utils/fencePreprocess";

const GANTT_ALIAS = [
  "机电安装进度计划横道图如下：",
  "",
  "```gantt",
  "title 机电安装施工进度计划",
  "dateFormat YYYY-MM-DD",
  "axisFormat %m-%d",
  "section 施工准备",
  "现场勘测与技术交底 :2026-05-18, 5d",
  "section 系统调试",
  "竣工验收与移交 :2026-11-11, 8d",
  "```",
].join("\n");

describe("canonicalChartFenceLang (R252)", () => {
  it("normalizes chart type names to mermaid", () => {
    for (const a of ["gantt", "flowchart", "graph", "pie", "timeline", "architecture", "labor", "sequenceDiagram", "mindmap"]) {
      expect(canonicalChartFenceLang(a)).toBe("mermaid");
    }
  });

  it("keeps canonical and real code langs", () => {
    for (const l of ["mermaid", "chart-json", "ai_image"]) expect(canonicalChartFenceLang(l)).toBe(l);
    for (const l of ["python", "js", "markdown", "text", "json", "bash"]) expect(canonicalChartFenceLang(l)).toBe(l);
  });

  it("isChartFenceLang / chartFenceLang use canonical predicate", () => {
    expect(isChartFenceLang("gantt")).toBe(true);
    expect(isChartFenceLang("flowchart")).toBe(true);
    expect(isChartFenceLang("python")).toBe(false);
    expect(chartFenceLang("gantt")).toBe("mermaid");
  });
});

describe("normalizeChartFenceShapes (R252 alias)", () => {
  it("rewrites ```gantt to ```mermaid (line count invariant)", () => {
    const out = normalizeChartFenceShapes(GANTT_ALIAS);
    expect(out.text).not.toContain("```gantt");
    expect(out.text).toContain("```mermaid");
    expect(out.text.split("\n").length).toBe(GANTT_ALIAS.split("\n").length);
  });

  it("is idempotent", () => {
    const once = normalizeChartFenceShapes(GANTT_ALIAS).text;
    const twice = normalizeChartFenceShapes(once).text;
    expect(twice).toBe(once);
  });

  it("leaves real code fences untouched", () => {
    const code = "```python\nprint('hi')\n```\n```js\nlet x=1\n```";
    expect(normalizeChartFenceShapes(code).text).toBe(code);
  });

  it("scanFenceBlocks marks aliased fence as chart after normalize", () => {
    const norm = normalizeChartFenceShapes(GANTT_ALIAS).text;
    const blocks = scanFenceBlocks(norm);
    expect(blocks.length).toBe(1);
    expect(blocks[0].isChart).toBe(true);
    expect(blocks[0].lang).toBe("mermaid");
  });

  it("handles tilde + indented fences", () => {
    expect(normalizeChartFenceShapes("~~~gantt\ntitle x\n~~~\n").text).toContain("~~~mermaid");
    expect(normalizeChartFenceShapes("   ```gantt\ntitle x\n   ```\n").text).toContain("   ```mermaid");
  });
});

describe("ensureGanttDeclaration (R252)", () => {
  it("prepends gantt for title-first gantt body", () => {
    const code = "title 计划\ndateFormat X\nsection A\n任务:t1,0,5d";
    const out = ensureGanttDeclaration(code);
    expect(out.startsWith("gantt\n")).toBe(true);
    expect(out).toContain("title 计划");
  });

  it("leaves code with valid declaration unchanged", () => {
    const code = "gantt\ntitle 计划\nsection A\n任务:t1,0,5d";
    expect(ensureGanttDeclaration(code)).toBe(code);
    const flow = "flowchart TD\n A-->B";
    expect(ensureGanttDeclaration(flow)).toBe(flow);
  });

  it("does not touch non-gantt text", () => {
    expect(ensureGanttDeclaration("普通段落文字")).toBe("普通段落文字");
  });

  // R253：section + 2 任务（省略 dateFormat/axisFormat）的残缺变体也应补 gantt
  it("prepends gantt for section+2task variant (R253)", () => {
    const code = "title 计划\nsection 施工准备\n任务A :2026-05-18, 5d\n任务B :2026-05-20, 7d";
    expect(ensureGanttDeclaration(code).startsWith("gantt\n")).toBe(true);
  });

  it("single directive + single task is NOT treated as gantt (R253 parity)", () => {
    const code = "title 计划\nsection 施工准备\n任务A :2026-05-18, 5d";
    expect(ensureGanttDeclaration(code)).toBe(code);
  });
});

describe("front/back parity (R252 source lock)", () => {
  it("alias set contains all 7 chart types + common mermaid keywords", () => {
    for (const a of ["gantt", "flowchart", "timeline", "architecture", "labor", "comparison", "layout", "pie"]) {
      expect(CHART_FENCE_LANG_ALIASES).toContain(a);
    }
  });

  it("backend fence_common alias table matches frontend set", () => {
    const rel = "app/services/fence_common.py";
    const candidates = [
      path.resolve(process.cwd(), "backend", rel),
      path.resolve(process.cwd(), "..", "backend", rel),
      path.resolve(process.cwd(), "..", "..", "backend", rel),
    ];
    const p = candidates.find((c) => fs.existsSync(c));
    expect(p, `未找到后端文件：${rel}（已尝试 ${candidates.join(" / ")}）`).toBeTruthy();
    const be = fs.readFileSync(p!, "utf8");
    for (const a of CHART_FENCE_LANG_ALIASES) {
      expect(be, `backend missing alias: ${a}`).toContain(`"${a}"`);
    }
    expect(be).toContain("def canonical_chart_fence_lang");
  });
});
