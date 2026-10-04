/**
 * T3（2026-10-03）· 导出响应头解析纯函数单测。
 *
 * 背景：X-Export-Filename / X-Chart-Render-Stats / X-Fix-Stats 的解析此前内联
 * 在 SchemeWorkbenchPage.handleExport（9800+ 行组件）里无法单测；提取为
 * utils/exportResponse.ts 纯函数后在此独立验证。核心契约：任何解析失败
 * （头缺失 / JSON 非法 / 百分号解码失败）都不抛错、不阻断下载。
 */
import { describe, it, expect } from "vitest";

import {
  chartRenderStatsParts,
  fixStatsParts,
  headerValue,
  parseChartRenderStats,
  parseFixStats,
  resolveExportFilename,
} from "../utils/exportResponse";

describe("headerValue", () => {
  it("大小写不敏感取头", () => {
    expect(headerValue({ "X-Export-Filename": "a.docx" }, "x-export-filename")).toBe("a.docx");
    expect(headerValue({ "x-export-filename": "a.docx" }, "X-Export-Filename")).toBe("a.docx");
  });

  it("缺失/非字符串/非法入参返回 undefined 不抛错", () => {
    expect(headerValue({}, "x-missing")).toBeUndefined();
    expect(headerValue({ "x-n": 123 }, "x-n")).toBeUndefined();
    expect(headerValue(null, "x-n")).toBeUndefined();
    expect(headerValue(undefined, "x-n")).toBeUndefined();
  });
});

describe("resolveExportFilename", () => {
  it("百分号编码文件名正常解码", () => {
    const raw = encodeURIComponent("基坑降水专项施工方案_20261003_第3轮.docx");
    expect(resolveExportFilename({ "X-Export-Filename": raw }, "兜底.docx"))
      .toBe("基坑降水专项施工方案_20261003_第3轮.docx");
  });

  it("头缺失沿用兜底名", () => {
    expect(resolveExportFilename({}, "方案.pdf")).toBe("方案.pdf");
  });

  it("解码异常沿用兜底名不抛错（原内联 try/catch 行为等价）", () => {
    // 孤立 % 序列不是合法百分号编码
    expect(resolveExportFilename({ "x-export-filename": "%E4%BD%%A zz%" }, "兜底.docx"))
      .toBe("兜底.docx");
  });
});

describe("parseChartRenderStats", () => {
  it("合法 JSON 解析出 fe/backend_ok/failed", () => {
    const h = { "X-Chart-Render-Stats": JSON.stringify({ fe: 10, backend_ok: 3, failed: 2 }) };
    expect(parseChartRenderStats(h)).toEqual({ fe: 10, backend_ok: 3, failed: 2 });
  });

  it("头缺失 / JSON 非法 / 非对象一律 null 不抛错", () => {
    expect(parseChartRenderStats({})).toBeNull();
    expect(parseChartRenderStats({ "x-chart-render-stats": "{oops" })).toBeNull();
    expect(parseChartRenderStats({ "x-chart-render-stats": '"str"' })).toBeNull();
    expect(parseChartRenderStats(null)).toBeNull();
  });

  it("文案拼装：0 值项不出现，failed>0 保留", () => {
    expect(chartRenderStatsParts({ fe: 10, backend_ok: 0, failed: 2 }))
      .toEqual(["前端图 10 张", "失败 2 张"]);
    expect(chartRenderStatsParts({})).toEqual([]);
  });
});

describe("parseFixStats", () => {
  it("合法 JSON 解析全部修复维度", () => {
    const h = { "X-Fix-Stats": JSON.stringify({ formulas: 3, gbk_mojibake: 7 }) };
    expect(parseFixStats(h)).toEqual({ formulas: 3, gbk_mojibake: 7 });
  });

  it("头缺失 / JSON 非法返回 null 不抛错", () => {
    expect(parseFixStats({})).toBeNull();
    expect(parseFixStats({ "x-fix-stats": "not-json" })).toBeNull();
  });

  it("文案拼装：0 值项不出现且顺序稳定", () => {
    expect(fixStatsParts({ formulas: 2, control_chars: 1, latin1_mojibake: 4 }))
      .toEqual(["公式 2 处", "控制字符 1 处", "编码乱码 4 处"]);
    expect(fixStatsParts({})).toEqual([]);
  });
});
