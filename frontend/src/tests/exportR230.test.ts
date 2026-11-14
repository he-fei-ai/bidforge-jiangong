/**
 * R230 导出文档模块深度审计增强 —— 前端护栏（2026-11-12）。
 *
 * 行为组：F1 畸形围栏归一（形态 A/B/幂等/行数不变/纯代码样例零触碰）、
 * F2/F3 头解析（X-Content-Audit / X-Export-Gate）。
 * 源码锚组（vite ?raw 范式，与既有 exportCharts.test / exportGateParity 同款）：
 * F8 PNG 失败计数、F10 blob 延迟回收、F11 预设加载失败提示、
 * F12 失败导出刷新缓存卡、F9 覆写确认期间取消必须可见、
 * 前后端头 parity（后端在发、前端必须解析）。
 */
import { describe, expect, it } from "vitest";
import {
  normalizeChartFenceShapes,
} from "../utils/fencePreprocess";
import {
  parseContentAudit as parseContentAuditFromResponse,
  parseExportGate as parseExportGateFromResponse,
  exportGateWarning,
} from "../utils/exportResponse";

// ✅ 源码锚走 vite ?raw 导入（R210 教训⑤：前端无 @types/node，node:fs 会让 tsc 红；
//    ?raw 不执行被读模块、类型由 vite/client 提供、零新增依赖）。
import runSrc from "../pages/schemeWorkbench/useExportRun.ts?raw";
import chartsSrc from "../utils/exportCharts.ts?raw";
import dossierSrc from "../components/review/DossierExportCard.tsx?raw";
import presetsSrc from "../pages/schemeWorkbench/useExportPresets.tsx?raw";
import responseSrc from "../utils/exportResponse.ts?raw";
import rendererSrc from "../components/MarkdownRenderer.tsx?raw";
// ⚠️ 后端源码（backend/）在 vite root 之外，?raw 会撞 fs.allow（Denied ID 实测）；
//    「后端在发」一侧由后端护栏锁定（test_export_closeout_r99 锁 X-Content-Audit
//    发送 / test_export_r195 锁 X-Export-Gate 发送），此处锁前端解析面 + 头名镜像。

// ---------------------------------------------------------------------------
// F1：畸形图表围栏形态归一（与后端 normalize_chart_fence_shapes 同语义）
// ---------------------------------------------------------------------------

describe("R230-F1 normalizeChartFenceShapes", () => {
  const FIG_A = [
    "| 图 9-1 井道垂直度检测示意图 |",
    "| :---: |",
    "| ```mermaid",
    "flowchart TD",
    " A[电梯] --> B[导轨]",
    "``` |",
    "",
    "后续正文",
  ].join("\n");

  it("形态 A：图题假表格包裹 → 解包为正常图表围栏（行数不变）", () => {
    const { text, unwrapped } = normalizeChartFenceShapes(FIG_A);
    expect(unwrapped).toBeGreaterThan(0);
    const lines = text.split("\n");
    expect(lines).toHaveLength(FIG_A.split("\n").length);
    expect(lines[0]).toBe("图 9-1 井道垂直度检测示意图"); // 图题还原段落
    expect(lines[1]).toBe(""); // 分隔行置空
    expect(lines[2]).toBe("```mermaid"); // 开启行剥管道
    expect(lines[5]).toBe("```"); // 闭合行归一
    expect(text).toContain("flowchart TD");
    expect(text).toContain("后续正文");
  });

  it("形态 A 幂等：二次归一零改动", () => {
    const once = normalizeChartFenceShapes(FIG_A).text;
    const twice = normalizeChartFenceShapes(once);
    expect(twice.text).toBe(once);
    expect(twice.unwrapped).toBe(0);
  });

  it("形态 B：文档围栏包裹正文+内嵌图表 → 解包（只置空外层开/闭行）", () => {
    const src = [
      "```markdown",
      "### 施工工艺",
      "安装前需复核导轨基准。",
      "```chart-json",
      '{"type":"gantt"}',
      "```",
      "收尾说明。",
      "```",
    ].join("\n");
    const { text, unwrapped } = normalizeChartFenceShapes(src);
    expect(unwrapped).toBeGreaterThan(0);
    const lines = text.split("\n");
    expect(lines).toHaveLength(8);
    expect(lines[0]).toBe(""); // 外层开启行置空
    expect(lines[7]).toBe(""); // 外层闭合行置空（嵌套穿透定位，不是内嵌图表的闭合）
    expect(lines[3]).toBe("```chart-json"); // 内嵌图表围栏原样保留
    expect(lines[5]).toBe("```"); // 内嵌图表自己的闭合行不被误抹
    expect(text).toContain("### 施工工艺"); // 正文结构恢复
  });

  it("形态 B 收窄：纯 markdown 代码样例（无图表）零触碰", () => {
    const src = ["```markdown", "# 示例", "普通代码样例", "```"].join("\n");
    const { text, unwrapped } = normalizeChartFenceShapes(src);
    expect(unwrapped).toBe(0);
    expect(text).toBe(src);
  });

  it("无围栏字符零开销直返", () => {
    const src = "普通正文，没有围栏。";
    expect(normalizeChartFenceShapes(src)).toEqual({ text: src, unwrapped: 0 });
  });
});

// ---------------------------------------------------------------------------
// F2/F3：响应头解析（载荷由后端 R230 护栏锁发送侧）
// ---------------------------------------------------------------------------

describe("R230-F2/F3 export response headers", () => {
  it("parseContentAudit：百分号编码 JSON → 结构化提示", () => {
    const payload = {
      total: 2,
      items: [
        { key: "placeholder", label: "数据占位符", level: "warn", count: 3, sections: ["概况"] },
        { key: "html_tag", label: "HTML 标签残留", level: "error", count: 1, sections: [] },
        { key: "bidding_terms", label: "投标场景用语", level: "warn", count: 2, sections: [] },
      ],
    };
    const headers = { "X-Content-Audit": encodeURIComponent(JSON.stringify(payload)) };
    const info = parseContentAuditFromResponse(headers);
    expect(info).not.toBeNull();
    expect(info!.total).toBe(2);
    expect(info!.message).toContain("占位符");
    expect(info!.items).toHaveLength(3);
  });

  it("parseContentAudit：缺失/坏载荷 → null（容错不阻断）", () => {
    expect(parseContentAuditFromResponse({})).toBeNull();
    expect(parseContentAuditFromResponse({ "X-Content-Audit": "%E4%BE%8B" })).toBeNull(); // 非 JSON
  });

  it("parseExportGate + exportGateWarning：unchecked 必须可见", () => {
    expect(parseExportGateFromResponse({ "X-Export-Gate": "unchecked" })).toBe("unchecked");
    expect(exportGateWarning("unchecked")).toContain("预检未能执行");
    expect(exportGateWarning(parseExportGateFromResponse({}))).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 源码锚（vite ?raw 范式）：交互面修复不得回退
// ---------------------------------------------------------------------------

describe("R230 source anchors", () => {
  it("F8：PNG 采集失败必须计数并可见（不再静默 catch）", () => {
    // ✅ R241 重锚（意图保留、强度不减）：stats 可选出口自 R233（+skipped_json）、
    //    R241（+dedup_skipped）两次加法式演进——锚从「锁完整类型字面量」改为
    //    「锁 failed 必选形态 + failed 计数行为」，对后续加法式演进仍保持判别力。
    expect(chartsSrc).toContain("stats?: { failed: number;");
    expect(chartsSrc).toContain("stats.failed += 1");
    expect(runSrc).toContain("渲染失败已交后端兜底");
  });

  it("F10：底稿导出 blob URL 延迟回收（对齐主导出链 10s）", () => {
    expect(dossierSrc).toContain("setTimeout(() => URL.revokeObjectURL(url), 10_000)");
  });

  it("F11：预设加载失败不得全静默（取消仍静默）", () => {
    expect(presetsSrc).toContain("controller.signal.aborted) return");
    expect(presetsSrc).toContain("导出预设加载失败");
  });

  it("F12：失败导出也刷新缓存状态卡", () => {
    const failBlock = runSrc.slice(runSrc.indexOf("const resolved = await resolveExportError"));
    expect(failBlock).toContain("loadCacheStatus");
  });

  it("F9：覆写确认期间取消必须可见（retryOverride 不静默丢弃）", () => {
    expect(runSrc).toContain("导出已被取消，覆写重试未执行");
  });

  it("F2/F3 头 parity：前端解析面与头名镜像成对（后端发送侧由后端护栏锁定）", () => {
    // 解析函数内的头名即后端 X-Content-Audit / X-Export-Gate 的镜像（小写比较）
    expect(responseSrc).toContain('"x-content-audit"');
    expect(responseSrc).toContain('"x-export-gate"');
    // 消费面必须接线（不是只定义不消费）
    expect(runSrc).toContain("parseContentAudit(");
    expect(runSrc).toContain("parseExportGate(");
    expect(runSrc).toContain("exportGateWarning(");
  });

  it("F13 证伪锚：X-Export-Round 为信息性头，前端刻意不消费（防后续轮误当缺陷重复上报）", () => {
    // 后端发送（export.py _export_name_headers）：轮次号已并入 X-Export-Filename
    // 的文件名语义，前端无需再消费 —— 该头属「后端在发、前端刻意不解析」的
    // 信息性信号，不是 parity 缺口（R230 §九 F13 裁决，R231 复核维持）。
    expect(responseSrc).not.toContain("x-export-round");
  });

  it("F1 接线：MarkdownRenderer 必须先做形态归一再补齐闭合", () => {
    const shapeIdx = rendererSrc.indexOf("normalizeChartFenceShapes(");
    const closeIdx = rendererSrc.indexOf("normalizeFenceCloses(");
    expect(shapeIdx).toBeGreaterThan(-1);
    expect(closeIdx).toBeGreaterThan(shapeIdx);
  });
});
