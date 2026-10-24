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
    expect(resolveExportFilename({}, "方案.docx")).toBe("方案.docx");
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

describe("EXPORT_STAGE_LABEL / ORDER · 导出阶段（D2 · R126 收敛为 DOCX 单链路）", () => {
  it("三个阶段均有文案（idle/charts/docx，PDF 已随 R126 下线）", () => {
    for (const k of ["idle", "charts", "docx"] as const) {
      expect(EXPORT_STAGE_LABEL[k]).toBeTruthy();
    }
  });

  it("非空闲阶段号从 1 连续到 2（阶段计数必须与 UI “2 阶”一致）", () => {
    expect(EXPORT_STAGE_ORDER.charts).toBe(1);
    expect(EXPORT_STAGE_ORDER.docx).toBe(2);
    expect(EXPORT_STAGE_ORDER.idle).toBeUndefined();
    expect(Math.max(...Object.values(EXPORT_STAGE_ORDER))).toBe(2);
  });

  // ✅ R126 反向锚（E 项）：PDF 导出已整体下线——阶段枚举里再现 pdf 即红，
  //    防「死灰复燃」式回归（重新加回 pdf 阶段会被这两条抓住）。
  it("pdf 阶段不得再现（PDF 下线反向锚）", () => {
    expect((EXPORT_STAGE_LABEL as Record<string, string>)["pdf"]).toBeUndefined();
    expect(EXPORT_STAGE_ORDER["pdf"]).toBeUndefined();
  });

  it("文案不得为空字符串（防死状态）", () => {
    for (const v of Object.values(EXPORT_STAGE_LABEL)) {
      expect(v.trim().length).toBeGreaterThan(0);
    }
  });
});


/**
 * 【R117 FIX-10 / FIX-11（2026-10-20）· 正文重复的前端消费面闭环。
 *
 * 病灶（R115 FIX-6 同一类）：后端把「已折叠的逐字重复」与「刻意没改的近似重复」
 * 都写进了 X-Fix-Stats，前端**从不读** ⇒ 用户看到的仍是一份"看起来一切正常"的
 * 成稿。此处锁两件事：① 两类计数都有文案；② 「没修」的那类绝不能混进
 * 「已自动修复」的成功语气里。
 */
import { fixStatsWarnings } from "../utils/exportResponse";

// 静态接线锁沿用本仓既有范式（importParseWarningWiringR72 / contentTerminalParity）：
// Vite `import.meta.glob(..., { query: "?raw" })` 读源码文本，不引 node:fs
// （本项目没装 @types/node，tests 也在 tsc 范围内）。
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

describe("fixStatsParts · 逐字重复已折叠（FIX-10，属成功修复）", () => {
  it("计数进成功文案并点明「仅保留首次」", () => {
    const parts = fixStatsParts({ repeated_blocks: 5 });
    expect(parts).toHaveLength(1);
    expect(parts[0]).toContain("5");
    expect(parts[0]).toContain("逐字重复");
    expect(parts[0]).toContain("仅保留首次");
  });

  it("计数为 0 时不展示", () => {
    expect(fixStatsParts({ repeated_blocks: 0 })).toEqual([]);
  });
});

describe("fixStatsWarnings · 近似重复未改动（FIX-11，属遗留缺陷）", () => {
  it("告警含数量、「成稿未改」与可执行动作", () => {
    const ws = fixStatsWarnings({ near_duplicate_blocks: 3 });
    expect(ws).toHaveLength(1);
    expect(ws[0]).toContain("3");
    expect(ws[0]).toContain("未作改动");
    expect(ws[0]).toContain("重新生成");
  });

  it("不得混进成功文案（否则把没修说成已修）", () => {
    expect(fixStatsParts({ near_duplicate_blocks: 3 })).toEqual([]);
  });

  it("0 / 缺失 / 脏值安全：不产告警、不抛错", () => {
    expect(fixStatsWarnings({ near_duplicate_blocks: 0 })).toEqual([]);
    expect(fixStatsWarnings({})).toEqual([]);
    expect(fixStatsWarnings({} as any)).toEqual([]);
  });

  it("parseFixStats 能解析含两类新键的载荷", () => {
    const fs = parseFixStats({
      "X-Fix-Stats": JSON.stringify({
        repeated_blocks: 5, near_duplicate_blocks: 3, formulas: 2,
      }),
    });
    expect(fs?.repeated_blocks).toBe(5);
    expect(fs?.near_duplicate_blocks).toBe(3);
  });

  it("R128：cross_ref_fields 进成功文案并点明「可点击 / 更新域刷新」", () => {
    const parts = fixStatsParts({ cross_ref_fields: 7 });
    expect(parts).toHaveLength(1);
    expect(parts[0]).toContain("7");
    expect(parts[0]).toContain("REF 域");
    expect(parts[0]).toContain("更新域");
  });

  it("R128：0 / 缺失 / 脏值安全", () => {
    expect(fixStatsParts({ cross_ref_fields: 0 })).toEqual([]);
    expect(fixStatsParts({})).toEqual([]);
    expect(() => fixStatsParts({ cross_ref_fields: "x" } as any)).not.toThrow();
  });

  it("R128：parseFixStats 能解析含 cross_ref_fields 的载荷", () => {
    const fs = parseFixStats({
      "X-Fix-Stats": JSON.stringify({ cross_ref_fields: 4, formulas: 1 }),
    });
    expect(fs?.cross_ref_fields).toBe(4);
    expect(fs?.formulas).toBe(1);
  });

  // ------------------------------------------------------------------
  // ✅ R129（2026-10-22）：导出后校验 + Word 2010+ 命名空间清除 + 公式降级
  // ------------------------------------------------------------------
  it("R129：ns_parts_sanitized / formula_image_fallback 进成功文案", () => {
    const parts = fixStatsParts({ ns_parts_sanitized: 5, formula_image_fallback: 2 });
    expect(parts.some((p) => p.includes("Word 2010+") && p.includes("5"))).toBe(true);
    expect(parts.some((p) => p.includes("公式降级为图片") && p.includes("2"))).toBe(true);
  });

  it("R129：导出后校验残留聚合进告警，且绝不混进成功文案", () => {
    const warns = fixStatsWarnings({
      postcheck_markdown_residue: 2,
      postcheck_html_residue: 1,
      postcheck_placeholder_residue: 1,
    });
    expect(warns).toHaveLength(1);
    expect(warns[0]).toContain("Markdown 残留 2 处");
    expect(warns[0]).toContain("占位符残留 1 处");
    expect(warns[0]).toContain("重新导出");
    expect(fixStatsParts({ postcheck_markdown_residue: 2 })).toEqual([]);
  });

  it("R129：0 / 缺失 / 脏值安全", () => {
    expect(fixStatsParts({ ns_parts_sanitized: 0, formula_image_fallback: 0 })).toEqual([]);
    expect(fixStatsWarnings({ postcheck_entity_residue: 0 })).toEqual([]);
    expect(fixStatsWarnings({})).toEqual([]);
  });

  it("R129：parseFixStats 能解析含 R129 新键的载荷", () => {
    const fs = parseFixStats({
      "X-Fix-Stats": JSON.stringify({
        ns_parts_sanitized: 3, ns_clean: true, outline_entries: 12,
        postcheck_markdown_residue: 1, formula_image_fallback: 2,
      }),
    });
    expect(fs?.ns_parts_sanitized).toBe(3);
    expect(fs?.postcheck_markdown_residue).toBe(1);
    expect(fs?.formula_image_fallback).toBe(2);
  });

  it("页面确实消费了告警（antd message 在 jsdom 不可断言 ⇒ 锁接线）", () => {
    // 本仓既有说明：antd v5 message 在 jsdom 下不渲染到可查询树，故以源码接线为判据
    // —— 只要有一天这行接线被删，本用例立即变红。
    const page = RAW["pages/SchemeWorkbenchPage.tsx"];
    expect(page).toBeTruthy();
    const seg = page.slice(page.indexOf("const fs = parseFixStats(resp.headers);"));
    expect(seg.length).toBeGreaterThan(50);
    expect(seg).toContain("fixStatsWarnings(fs)");
    expect(seg).toContain("msg.warning");
    // 接线必须仍在成功文案之后（顺序错乱 ⇒ 用户先看到告警再看到"已修复"）
    expect(seg.indexOf("fixStatsWarnings")).toBeGreaterThan(seg.indexOf("msg.success"));
  });
});
