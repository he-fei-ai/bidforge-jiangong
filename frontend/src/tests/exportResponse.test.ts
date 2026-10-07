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


/**
 * 【2026-10-06 · 后端加法式扩展的响应头契约。
 *
 * 背景：后端导出层在逐块 fail-soft + 表格尺寸上限两项修复中，
 * 引入了两个新的降级计数（block_render_failed / table_truncated）。
 * 若前端不能展示它们，就等于“成稿里少了一块版式”又回到
 * 不可见状态——这正是本轮要消除的那类隐性缺陷。
 */
describe("fixStatsParts · 降级计数（2026-10-06 加法式扩展）", () => {
  it("内容块降级与表格截断均可见", () => {
    const parts = fixStatsParts({ block_render_failed: 2, table_truncated: 1 });
    expect(parts.some((p) => p.includes("内容块降级") && p.includes("2"))).toBe(true);
    expect(parts.some((p) => p.includes("表格截断") && p.includes("1"))).toBe(true);
  });

  it("计数为 0 时不展示（零值不噪干）", () => {
    expect(fixStatsParts({ block_render_failed: 0, table_truncated: 0 })).toEqual([]);
  });

  it("旧后端不会装未知键时不抛错", () => {
    expect(() => fixStatsParts({} as any)).not.toThrow();
    expect(() => fixStatsParts({ formulas: 3 } as any)).not.toThrow();
  });

  it("parseFixStats 能解析含新键的载荷", () => {
    const h = {
      "X-Fix-Stats": JSON.stringify({
        formulas: 1, block_render_failed: 2, table_truncated: 3,
      }),
    };
    const fs = parseFixStats(h);
    expect(fs?.block_render_failed).toBe(2);
    expect(fs?.table_truncated).toBe(3);
    expect(fs?.formulas).toBe(1);
  });

  it("既有 6 个展示项不受影响（向后兼容）", () => {
    const parts = fixStatsParts({
      formulas: 1, replacement_chars: 2, control_chars: 3,
      gbk_mojibake: 4, latin1_mojibake: 5, cyrillic: 6,
    });
    expect(parts).toHaveLength(6);
  });
});


/**
 * 【2026-10-06 · D1/D2 加固的前端契约补充。
 *
 * D1：跨章借图（该章节没有自己的图表登记，导出借用了别章的图）
 *     此前零信号 —— 成稿里 B 章可能出现 A 章的图，用户毫无知情。
 *     后端现已计入 X-Chart-Render-Stats 的 fallback_borrowed，此处锁定前端确实展示。
 * D2：阶段标签映射不得漂移（阶序号 + 文案）。
 */
import { EXPORT_STAGE_LABEL, EXPORT_STAGE_ORDER } from "../pages/SchemeWorkbenchPage";
import { fallbackBorrowWarning } from "../utils/exportResponse";

describe("chartRenderStatsParts · 跨章借图（D1）", () => {
  it("借图数量进入渲染统计文案", () => {
    const parts = chartRenderStatsParts({ fe: 3, backend_ok: 1, fallback_borrowed: 2 });
    expect(parts.some((p) => p.includes("跨章借图") && p.includes("2"))).toBe(true);
  });

  it("借图为 0 时不展示（零值不噪干）", () => {
    expect(chartRenderStatsParts({ fe: 2, fallback_borrowed: 0 })).toEqual(["前端图 2 张"]);
  });

  it("借图数量可被 parseChartRenderStats 正常解析", () => {
    const fs = parseChartRenderStats({
      "X-Chart-Render-Stats": JSON.stringify({
        fe: 1, backend_ok: 2, failed: 0, fallback_borrowed: 2,
        fallback_borrowed_details: [
          { section_id: "s1", chart_type: "labor", borrowed_from: "s9" },
        ],
      }),
    });
    expect(fs?.fallback_borrowed).toBe(2);
    expect(fs?.fallback_borrowed_details?.[0]?.borrowed_from).toBe("s9");
  });
});

describe("fallbackBorrowWarning · 跨章借图告警（D1）", () => {
  it("告警文案含数量与可执行动作", () => {
    const w = fallbackBorrowWarning({ fallback_borrowed: 2 });
    expect(w).toBeTruthy();
    expect(w).toContain("2");
    expect(w).toContain("回查");
  });

  it("没借图时返回 null（不发无关提示）", () => {
    expect(fallbackBorrowWarning({ fe: 3 })).toBeNull();
    expect(fallbackBorrowWarning({ fallback_borrowed: 0 })).toBeNull();
    expect(fallbackBorrowWarning({} as any)).toBeNull();
    expect(fallbackBorrowWarning({ fallback_borrowed: -1 } as any)).toBeNull();
  });

  it("非数字 / NaN 安全降级为 null（不抛错）", () => {
    expect(fallbackBorrowWarning({ fallback_borrowed: "abc" } as any)).toBeNull();
    expect(fallbackBorrowWarning(null as any)).toBeNull();
  });
});

describe("EXPORT_STAGE_LABEL / ORDER · 导出阶段（D2）", () => {
  it("四个阶段均有文案", () => {
    for (const k of ["idle", "charts", "docx", "pdf"] as const) {
      expect(EXPORT_STAGE_LABEL[k]).toBeTruthy();
    }
  });

  it("非空闲阶段号从 1 连续到 3（阶段计数必须与 UI “3 阶”一致）", () => {
    expect(EXPORT_STAGE_ORDER.charts).toBe(1);
    expect(EXPORT_STAGE_ORDER.docx).toBe(2);
    expect(EXPORT_STAGE_ORDER.pdf).toBe(3);
    expect(EXPORT_STAGE_ORDER.idle).toBeUndefined();
    expect(Math.max(...Object.values(EXPORT_STAGE_ORDER))).toBe(3);
  });

  it("文案不得为空字符串（防死状态）", () => {
    for (const v of Object.values(EXPORT_STAGE_LABEL)) {
      expect(v.trim().length).toBeGreaterThan(0);
    }
  });
});
