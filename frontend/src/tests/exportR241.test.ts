/**
 * R241 · 导出文档模块前端护栏（2026-11-13）。
 *
 * 定罪五项前端缺陷（主会话亲验定罪）：
 * - FE-1（P2）: R233 FE-4 修复不完整 —— 「渲染 X/Y」分母只剔了 skipped_json，
 *   同码去重条目（多章共用同图，exportResponse D1 明文合理场景）留在分母里，
 *   「渲染 1/10」被误读为 9 张失败。本轮 renderChartsForExport 补 dedup_skipped
 *   计数出口，useExportRun 分母剔除。
 * - FE-2（P2）: fetchExportPresets 在 save/delete/refresh 后无条件回填默认预设
 *   配置 + 下拉指向默认 —— 违反 workflowDerived 契约「默认预设仅作为首次进入
 *   导出页的初始配置」，用户未保存的表单编辑被静默抹掉。本轮仅初始化路径
 *   （applyDefault:true）回填。
 * - FE-3（P2）: 预设下拉切换 setFieldsValue 合并语义残留（旧预设 page_header /
 *   scheme_forms 等字段混入新预设），且 config 可空时无 `|| {}` 防护。
 *   本轮 resetFields + 替换语义。
 * - FE-4（P3）: 形态 A 正则行尾 `[ \t]*$` 不吃 `\r` —— 后端解析链先归一 \r
 *   再做形态归一，预览链无 \r 处理，CRLF 场景形态 A 解包失败（F1 反向分叉
 *   在 CRLF 复活）。本轮正则容 `\r`（防御对齐；生产库当前 0/262 含 CR）。
 * - FE-5（P3）: DossierExportCard 无同步重入锁（主链 R-3 口径不一致残余）。
 */
import { describe, it, expect, vi } from "vitest";
import rawPresetsSrc from "../pages/schemeWorkbench/useExportPresets.tsx?raw";
import rawTabSrc from "../pages/schemeWorkbench/tabs/ExportTab.tsx?raw";
import rawDossierSrc from "../components/review/DossierExportCard.tsx?raw";
import rawRunSrc from "../pages/schemeWorkbench/useExportRun.ts?raw";

const renderSpy = vi.fn(async () => ({ svg: "<svg></svg>" }));

vi.mock("../components/mermaidRuntime", () => ({
  ensureMermaid: vi.fn(async () => ({ render: renderSpy })),
  nextMermaidId: vi.fn(() => "test-id"),
}));

import { renderChartsForExport } from "../utils/exportCharts";
import { normalizeChartFenceShapes } from "../utils/fencePreprocess";

// ---------------------------------------------------------------------------
// FE-1 行为实证：同码去重条目计数出口
// ---------------------------------------------------------------------------

describe("R241 FE-1: renderChartsForExport dedup_skipped 计数", () => {
  it("同一 (chart_type, code) 的重复条目计入 dedup_skipped，不再静默吞掉", async () => {
    const code = "flowchart TD\n  A[开工] --> B[验收]";
    const items = [
      { chart_type: "flowchart", mermaid_code: code },
      { chart_type: "flowchart", mermaid_code: code },
      { chart_type: "flowchart", mermaid_code: code },
    ];
    const stats = { failed: 0, skipped_json: 0, dedup_skipped: 0 };
    const out = await renderChartsForExport(items, undefined, undefined, stats);
    // 去重：产物唯一（svgToPng 在 node 环境抛错 → 第一条计入 failed，仍占 attempted）
    expect(out.length).toBeLessThanOrEqual(1);
    // ✅ R241 核心：两条重复条目被显式计数（旧实现静默 return，分母不剔除）
    expect(stats.dedup_skipped).toBe(2);
  });

  it("无重复时 dedup_skipped 恒 0（不污染既有口径）", async () => {
    const items = [
      { chart_type: "flowchart", mermaid_code: "flowchart TD\n  A --> B" },
      { chart_type: "gantt", mermaid_code: "gantt\n  task :t1, 1, 3d" },
    ];
    const stats = { failed: 0, skipped_json: 0, dedup_skipped: 0 };
    await renderChartsForExport(items, undefined, undefined, stats);
    expect(stats.dedup_skipped).toBe(0);
  });

  it("stats 可选出口缺省不传 = 旧行为（不抛错）", async () => {
    const out = await renderChartsForExport(
      [{ chart_type: "flowchart", mermaid_code: "flowchart TD\n  A --> B" }],
      undefined,
      undefined,
    );
    expect(Array.isArray(out)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// FE-4 行为实证：CRLF 场景形态 A 解包（防御对齐）
// ---------------------------------------------------------------------------

describe("R241 FE-4: normalizeChartFenceShapes CRLF 容错", () => {
  const LF_FORM = [
    "| 图 9-1 检测流程 |",
    "| :---: |",
    "| ```mermaid |",
    "flowchart TD",
    "  A[开工] --> B[验收]",
    "| ``` |",
  ].join("\n");

  it("LF 形态 A 正常解包（既有行为回归锚）", () => {
    const r = normalizeChartFenceShapes(LF_FORM);
    expect(r.unwrapped).toBe(1);
    expect(r.text).toContain("```mermaid\nflowchart TD");
  });

  it("CRLF 形态 A 同样解包（行尾 \\r 挂尾不再使形态判定失效）", () => {
    const crlfForm = LF_FORM.replace(/\n/g, "\r\n");
    const r = normalizeChartFenceShapes(crlfForm);
    expect(r.unwrapped).toBe(1);
    // 解包后 mermaid 围栏开行不带管道前缀（源码不进表格单元格）
    expect(r.text).toContain("```mermaid");
    expect(r.text).not.toContain("| ```mermaid");
  });
});

// ---------------------------------------------------------------------------
// FE-2/FE-3/FE-5 源码锁（vite ?raw 范式）
// ---------------------------------------------------------------------------

// ✅ R241：源码锚走 vite ?raw 范式（与 exportR230.test.ts 同款）——
//    vitest ESM 下无 __dirname，readFileSync+resolve 形态 tsc 直接报错。
const useExportPresetsSrc = rawPresetsSrc;
const exportTabSrc = rawTabSrc;
const dossierSrc = rawDossierSrc;
const useExportRunSrc = rawRunSrc;

describe("R241 源码锁", () => {
  it("FE-2: fetchExportPresets 带 applyDefault 可选形参，回填与选中都在其守卫内", () => {
    expect(useExportPresetsSrc).toContain("opts?: { applyDefault?: boolean }");
    expect(useExportPresetsSrc).toContain("if (opts?.applyDefault)");
    // effect 初始化路径显式传 true
    expect(useExportPresetsSrc).toContain("fetchExportPresets(id, { applyDefault: true })");
    // CRUD 路径不传（不回填）：save / setDefault / delete 三处调用均无第二参
    expect(useExportPresetsSrc).toContain("await fetchExportPresets(sid);");
    // 回填的默认预设形态不得回流（无条件 setFieldsValue(defaultPreset.config)）
    expect(useExportPresetsSrc).not.toContain("exportForm.setFieldsValue(defaultPreset.config || {});\n        } else {");
  });

  it("FE-2: 保存预设后下拉指向新建预设（create 响应 id 联动）", () => {
    expect(useExportPresetsSrc).toContain("const created = await exportApi.presets.create(sid, name, config);");
    expect(useExportPresetsSrc).toContain("createdId");
  });

  it("FE-3: 预设切换先 resetFields 再整体套用（替换语义 + 空值防护）", () => {
    expect(exportTabSrc).toContain("exportForm.resetFields();");
    expect(exportTabSrc).toContain("exportForm.setFieldsValue(p.config || {})");
    // 旧合并形态不得回流
    expect(exportTabSrc).not.toContain("if (p) exportForm.setFieldsValue(p.config);");
  });

  it("FE-5: DossierExportCard 同步重入锁（与主链 R-3 口径对齐）", () => {
    expect(dossierSrc).toContain("exportingRef.current");
    expect(dossierSrc).toContain("if (exportingRef.current) return;");
  });

  it("FE-1: useExportRun 分母剔除 skipped_json 与 dedup_skipped 两项", () => {
    expect(useExportRunSrc).toContain("dedup_skipped: 0");
    expect(useExportRunSrc).toContain(
      "items.length - chartRenderStats.skipped_json - chartRenderStats.dedup_skipped",
    );
  });
});
