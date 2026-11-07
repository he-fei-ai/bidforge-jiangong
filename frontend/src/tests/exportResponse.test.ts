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
  ganttTruncatedWarning,
  headerValue,
  globalFactsStatusWarning,
  noHttpFallbackWarning,
  noPilFallbackWarning,
  numberingCheckWarning,
  parseChartRenderStats,
  parseExportBlock,
  parseExportDegraded,
  parseExportOverride,
  parseFixStats,
  parseGlobalFactsStatus,
  parseNumberingCheck,
  resolveExportError,
  resolveExportFilename,
} from "../utils/exportResponse";
import type { ChartRenderStats } from "../utils/exportResponse";

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
    expect(resolveExportFilename({ "X-Export-Filename": raw }, "兜底.docx")).toBe(
      "基坑降水专项施工方案_20261003_第3轮.docx",
    );
  });

  it("头缺失沿用兜底名", () => {
    expect(resolveExportFilename({}, "方案.pdf")).toBe("方案.pdf");
  });

  it("解码异常沿用兜底名不抛错（原内联 try/catch 行为等价）", () => {
    // 孤立 % 序列不是合法百分号编码
    expect(resolveExportFilename({ "x-export-filename": "%E4%BD%%A zz%" }, "兜底.docx")).toBe(
      "兜底.docx",
    );
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
    expect(chartRenderStatsParts({ fe: 10, backend_ok: 0, failed: 2 })).toEqual([
      "前端图 10 张",
      "失败 2 张",
    ]);
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
    expect(fixStatsParts({ formulas: 2, control_chars: 1, latin1_mojibake: 4 })).toEqual([
      "公式 2 处",
      "控制字符 1 处",
      "编码乱码 4 处",
    ]);
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
    expect(() => fixStatsParts({})).not.toThrow();
    expect(() => fixStatsParts({ formulas: 3 })).not.toThrow();
  });

  it("parseFixStats 能解析含新键的载荷", () => {
    const h = {
      "X-Fix-Stats": JSON.stringify({
        formulas: 1,
        block_render_failed: 2,
        table_truncated: 3,
      }),
    };
    const fs = parseFixStats(h);
    expect(fs?.block_render_failed).toBe(2);
    expect(fs?.table_truncated).toBe(3);
    expect(fs?.formulas).toBe(1);
  });

  it("既有 6 个展示项不受影响（向后兼容）", () => {
    const parts = fixStatsParts({
      formulas: 1,
      replacement_chars: 2,
      control_chars: 3,
      gbk_mojibake: 4,
      latin1_mojibake: 5,
      cyrillic: 6,
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
        fe: 1,
        backend_ok: 2,
        failed: 0,
        fallback_borrowed: 2,
        fallback_borrowed_details: [{ section_id: "s1", chart_type: "labor", borrowed_from: "s9" }],
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
    expect(fallbackBorrowWarning({})).toBeNull();
    expect(fallbackBorrowWarning({ fallback_borrowed: -1 })).toBeNull();
  });

  it("非数字 / NaN 安全降级为 null（不抛错）", () => {
    expect(fallbackBorrowWarning({ fallback_borrowed: "abc" as unknown as number })).toBeNull();
    expect(fallbackBorrowWarning(null as unknown as ChartRenderStats)).toBeNull();
  });
});

/**
 * ✅ D-23（R176 · 2026-11-04）：甘特图 >30 任务被截断的后端计数
 * （`X-Chart-Render-Stats.gantt_truncated`）必须被前端消费为 warning，
 * 否则「成稿少了任务」仍不可见。
 */
describe("ganttTruncatedWarning · 甘特截断告警（D-23）", () => {
  it("告警文案含数量、上限与可执行动作", () => {
    const w = ganttTruncatedWarning({ gantt_truncated: 5 });
    expect(w).toBeTruthy();
    expect(w).toContain("5");
    expect(w).toContain("30");
    expect(w).toContain("精简");
  });

  it("截断量为 0 / 缺省时返回 null（不发无关提示）", () => {
    expect(ganttTruncatedWarning({ gantt_truncated: 0 })).toBeNull();
    expect(ganttTruncatedWarning({})).toBeNull();
    expect(ganttTruncatedWarning({ fe: 3 })).toBeNull();
  });

  it("非数字 / 负数 / null 安全降级为 null（不抛错）", () => {
    expect(ganttTruncatedWarning({ gantt_truncated: "abc" as unknown as number })).toBeNull();
    expect(ganttTruncatedWarning({ gantt_truncated: -1 })).toBeNull();
    expect(ganttTruncatedWarning(null as unknown as ChartRenderStats)).toBeNull();
  });

  it("gantt_truncated 可被 parseChartRenderStats 正常解析", () => {
    const stats = parseChartRenderStats({
      "X-Chart-Render-Stats": JSON.stringify({ backend_ok: 2, gantt_truncated: 4 }),
    });
    expect(stats?.gantt_truncated).toBe(4);
  });
});

/**
 * ✅ R179（G-06 · 2026-11-05）：HTTP-only 图表（mindmap/sequence/class 等）没有
 * PIL 本地兜底，渲染失败时不能被泛化成「开启允许 PIL 兜底」建议。
 */
describe("noPilFallbackWarning · 无本地兜底告警（G-06）", () => {
  it("文案含失败数量与类型明细，并明确 PIL 兜底无效", () => {
    const w = noPilFallbackWarning({
      failed: 2,
      no_pil_fallback: 1,
      no_pil_fallback_types: ["mindmap"],
    });
    expect(w).toBeTruthy();
    expect(w).toContain("1 张");
    expect(w).toContain("mindmap");
    expect(w).toContain("PIL 兜底");
    expect(w).toContain("无法补救");
  });

  it("0 / 缺省时返回 null（不发无关提示）", () => {
    expect(noPilFallbackWarning({})).toBeNull();
    expect(noPilFallbackWarning({ no_pil_fallback: 0 })).toBeNull();
  });

  it("R181：甘特 JSON 失败单独提示「无 HTTP 兜底」，与 no_pil_fallback 区分", () => {
    const w = noHttpFallbackWarning({
      failed: 1,
      no_http_fallback: 1,
      no_http_fallback_types: ["gantt"],
    });
    expect(w).toBeTruthy();
    expect(w).toContain("1 张");
    expect(w).toContain("gantt");
    expect(w).toContain("没有 HTTP 渲染兜底");
    // 不得误导为「开启 PIL 兜底可补救」
    expect(w).not.toContain("PIL 兜底");
  });

  it("R181：no_http_fallback 缺省/0 时返回 null", () => {
    expect(noHttpFallbackWarning({})).toBeNull();
    expect(noHttpFallbackWarning({ no_http_fallback: 0 })).toBeNull();
  });

  it("X-Chart-Render-Stats 可解析出新增字段", () => {
    const stats = parseChartRenderStats({
      "X-Chart-Render-Stats": JSON.stringify({
        failed: 2,
        no_pil_fallback: 1,
        no_pil_fallback_types: ["mindmap"],
      }),
    });
    expect(stats?.no_pil_fallback).toBe(1);
    expect(stats?.no_pil_fallback_types).toEqual(["mindmap"]);
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

/**
 * ✅ R175 BUG-7（2026-11-03）：导出失败错误体解析。
 *
 * 旧缺陷：导出走 responseType:"blob"，catch 只读 err.message → 用户只看到
 * 「Request failed with status code N」，后端 409/500 的真实 detail 被吞。
 * 现 resolveExportError 优先读响应体（Blob→文本→JSON），其次拦截器文案，
 * 最后泛化兜底；取消/网络错误/超时保持原文案，不新增误报。
 */
describe("resolveExportError · JSON 错误体解析（R175 BUG-7）", () => {
  it("对象错误体 {detail} → 展示真实文案", async () => {
    const r = await resolveExportError({
      message: "Request failed with status code 409",
      response: { status: 409, data: { detail: "正文编号与目录不一致，请先修复编号漂移" } },
    });
    expect(r.canceled).toBe(false);
    expect(r.generic).toBe(false);
    expect(r.message).toBe("正文编号与目录不一致，请先修复编号漂移");
  });

  it("对象错误体 {message}（无 detail）→ 展示真实文案", async () => {
    const r = await resolveExportError({
      response: { status: 500, data: { message: "图表渲染失败：第 3 章甘特图" } },
    });
    expect(r.message).toBe("图表渲染失败：第 3 章甘特图");
    expect(r.generic).toBe(false);
  });

  it("blob 错误体内容为 JSON → 先读文本再解析 detail", async () => {
    const blob = new Blob([JSON.stringify({ detail: "导出中断：章节正文为空（sec-7）" })], {
      type: "application/json",
    });
    const r = await resolveExportError({
      message: "Request failed with status code 422",
      response: { status: 422, data: blob },
    });
    expect(r.message).toBe("导出中断：章节正文为空（sec-7）");
    expect(r.generic).toBe(false);
  });

  it("blob 内 {message} 同样可解析", async () => {
    const blob = new Blob([JSON.stringify({ message: "PDF 转换失败：LibreOffice 超时" })], {
      type: "application/json",
    });
    const r = await resolveExportError({ response: { status: 500, data: blob } });
    expect(r.message).toBe("PDF 转换失败：LibreOffice 超时");
  });

  it("blob 非 JSON（二进制残片）→ 不把乱码抛给用户，回退泛化文案", async () => {
    const blob = new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04])], {
      type: "application/octet-stream",
    });
    const r = await resolveExportError({
      message: "Request failed with status code 500",
      response: { status: 500, data: blob },
    });
    expect(r.generic).toBe(true);
    expect(r.message).toContain("导出失败");
    expect(r.message).toContain("500");
    expect(r.message).not.toContain("PK");
  });

  it("无错误体（空响应）→ 回退泛化文案（不展示 axios 裸状态码）", async () => {
    const r = await resolveExportError({
      message: "Request failed with status code 502",
      response: { status: 502, data: undefined },
    });
    expect(r.generic).toBe(true);
    expect(r.message).toContain("导出失败");
    expect(r.message).not.toContain("Request failed");
  });

  it("拦截器已翻译的中文文案（响应体不可读）→ 保留该文案", async () => {
    const r = await resolveExportError({
      message: "方案尚未生成正文，无法导出",
      response: { status: 400, data: undefined },
    });
    expect(r.message).toBe("方案尚未生成正文，无法导出");
    expect(r.generic).toBe(false);
  });
});

describe("resolveExportError · 取消/网络/超时保持原文案（R175 BUG-7）", () => {
  it("AbortError → canceled=true 且保留原始文案（调用方不得弹错误）", async () => {
    const r = await resolveExportError({
      name: "AbortError",
      message: "The operation was aborted.",
    });
    expect(r.canceled).toBe(true);
    expect(r.message).toBe("The operation was aborted.");
  });

  it("axios ERR_CANCELED → canceled=true", async () => {
    const r = await resolveExportError({ code: "ERR_CANCELED", message: "canceled" });
    expect(r.canceled).toBe(true);
    expect(r.message).toBe("canceled");
  });

  it("超时（无 response）→ 保留 axios timeout 原文案，不改成泛化文案", async () => {
    const r = await resolveExportError({
      code: "ECONNABORTED",
      message: "timeout of 600000ms exceeded",
    });
    expect(r.canceled).toBe(false);
    expect(r.generic).toBe(false);
    expect(r.message).toBe("timeout of 600000ms exceeded");
  });

  it("网络错误（无 response）→ 保留 Network Error 原文案", async () => {
    const r = await resolveExportError({ message: "Network Error" });
    expect(r.message).toBe("Network Error");
    expect(r.generic).toBe(false);
  });

  it("完全无信息的异常 → 泛化「导出失败」（不抛错）", async () => {
    const r = await resolveExportError(null);
    expect(r.canceled).toBe(false);
    expect(r.message).toBe("导出失败");
    expect(r.generic).toBe(true);
  });
});

/**
 * ✅ R175 BUG-7：后端降级信号 X-Export-Degraded 识别（前端此前完全未读取，
 * 成稿缺章却零提示）。
 */
describe("parseExportDegraded · 降级信号（R175 BUG-7）", () => {
  it("empty-sections → 用户可见「部分章节为空，导出内容可能不完整」", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "empty-sections" });
    expect(d).not.toBeNull();
    expect(d!.signals).toEqual(["empty-sections"]);
    expect(d!.message).toContain("部分章节为空");
    expect(d!.message).toContain("可能不完整");
  });

  it("头缺失 / 空值 → null（旧后端行为零变化，不新增提示）", () => {
    expect(parseExportDegraded({})).toBeNull();
    expect(parseExportDegraded({ "X-Export-Degraded": "   " })).toBeNull();
    expect(parseExportDegraded(null)).toBeNull();
    expect(parseExportDegraded(undefined)).toBeNull();
  });

  it("未知信号 → 通用降级文案（不静默吞掉）", () => {
    const d = parseExportDegraded({ "x-export-degraded": "unknown-signal" });
    expect(d).not.toBeNull();
    expect(d!.signals).toEqual(["unknown-signal"]);
    expect(d!.message).toContain("导出已降级");
    expect(d!.message).toContain("unknown-signal");
  });

  it("多信号（逗号分隔，大小写不敏感）合并提示", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "empty-sections, FOO" });
    expect(d!.signals).toEqual(["empty-sections", "foo"]);
    expect(d!.message).toContain("部分章节为空");
    expect(d!.message).toContain("导出已降级");
  });

  it("global-facts-query-failed → 事实缺失提示（B1-2）", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "global-facts-query-failed" });
    expect(d!.message).toContain("全局事实读取失败");
    expect(d!.message).toContain("项目关键事实");
  });

  it("content-residue → 格式残留提示（R179 导出出口闸门）", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "content-residue" });
    expect(d).not.toBeNull();
    expect(d!.message).toContain("格式残留");
    expect(d!.message).toContain("不写入缓存");
  });
});

/** ✅ B1-2（R176 · 2026-11-04）：全局事实状态头消费（此前全仓零消费）。 */
describe("parseGlobalFactsStatus · 全局事实状态（B1-2）", () => {
  const enc = (obj: unknown) => encodeURIComponent(JSON.stringify(obj));

  it("query_failed → ok=false 且给出用户可读告警", () => {
    const st = parseGlobalFactsStatus({
      "X-Global-Facts-Status": enc({ ok: false, code: "query_failed", count: 0 }),
    });
    expect(st).toEqual({ ok: false, code: "query_failed", count: 0 });
    expect(globalFactsStatusWarning(st)).toContain("全局事实读取失败");
  });

  it("ok → 无告警（正常导出行为零变化）", () => {
    const st = parseGlobalFactsStatus({
      "X-Global-Facts-Status": enc({ ok: true, code: "empty", count: 0 }),
    });
    expect(st!.ok).toBe(true);
    expect(globalFactsStatusWarning(st)).toBeNull();
  });

  it("头缺失 / 解码失败 / JSON 非法 → null（不阻断下载）", () => {
    expect(parseGlobalFactsStatus({})).toBeNull();
    expect(parseGlobalFactsStatus({ "X-Global-Facts-Status": "%%%" })).toBeNull();
    expect(parseGlobalFactsStatus({ "X-Global-Facts-Status": "%7B" })).toBeNull();
    expect(parseGlobalFactsStatus(null)).toBeNull();
  });
});

/** 页面接线锚点：确保导出链真实消费解析层（防回退到旧 catch）。 */
describe("SchemeWorkbenchPage · 导出错误体接线锚点（R175 BUG-7）", () => {
  const RAW: Record<string, string> = Object.fromEntries(
    Object.entries(
      import.meta.glob("../pages/**/*.{ts,tsx}", {
        query: "?raw",
        import: "default",
        eager: true,
      }) as Record<string, string>,
    ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
  );
  // ✅ G9（2026-11-04）：导出主链自页面迁至 useExportRun.ts（函数体逐字搬运），
  //    锚点同步扩到「页面 + 导出 hook」两个文件 —— 三条断言仍要求导出链路里
  //    真实出现同一段调用；若有人从 hook 删掉 res.parse/兜底，断言同样会红。
  const src = [
    RAW["pages/SchemeWorkbenchPage.tsx"],
    RAW["pages/schemeWorkbench/useExportRun.ts"],
  ].join("\n");

  it("导出 catch 走 resolveExportError（不再只看 err.message）", () => {
    expect(src, "SchemeWorkbenchPage.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(src).toContain("await resolveExportError(e)");
    expect(src).not.toContain('msg.error(err.message || "导出失败")');
  });

  it("导出成功路径读取 X-Export-Degraded 并给出 warning", () => {
    expect(src).toContain("parseExportDegraded(resp.headers)");
    expect(src).toContain("msg.warning(degraded.message");
  });

  it("成功/失败路径均消费 X-Global-Facts-Status（B1-2）", () => {
    expect(src).toContain("parseGlobalFactsStatus(resp.headers)");
    expect(src).toContain("parseGlobalFactsStatus(errHeaders)");
    expect(src).toContain("globalFactsStatusWarning");
  });
});

/**
 * ✅ B-15（R182 · 2026-11-06）：high 级问题阻断错误体的运行时解析。
 * 后端 409 体经 axios 可能是对象，也可能在 blob 响应类型下是 Blob —— 两种形态
 * 都必须可读；非阻断错误必须返回 null，交给 resolveExportError 的既有文案路径。
 */
describe("parseExportBlock · 阻断错误体（B-15）", () => {
  const payload = {
    error: "export_blocked_high_issues",
    detail: "导出已被阻断：存在 1 类高风险问题（共 2 处）",
    blocked: true,
    high_issue_count: 1,
    high_issue_total: 2,
    high_issues: [
      { type: "empty_section", rule_id: "DLV-01", severity: "high", count: 2, title: "空章节" },
    ],
    override_hint: "如确认已知悉，请在请求体传入 override_high_issues=true（会写入导出留痕）",
  };

  it("对象形态：解析出阻断项与可执行提示", async () => {
    const info = await parseExportBlock({ response: { data: payload } });
    expect(info?.code).toBe("export_blocked_high_issues");
    expect(info?.issueCount).toBe(1);
    expect(info?.issueTotal).toBe(2);
    expect(info?.issues[0].rule_id).toBe("DLV-01");
    expect(info?.overrideHint).toContain("override_high_issues");
  });

  it("Blob 形态（responseType=blob）同样可读，不得因 Blob 退化成泛化文案", async () => {
    const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
    const info = await parseExportBlock({ response: { data: blob } });
    expect(info?.detail).toContain("导出已被阻断");
  });

  it("非阻断错误 / 非 JSON / 无响应 → null（走既有错误文案路径）", async () => {
    expect(await parseExportBlock({ response: { data: { detail: "参数错误" } } })).toBeNull();
    expect(await parseExportBlock({ response: { data: new Blob(["not json"]) } })).toBeNull();
    expect(await parseExportBlock(new Error("network"))).toBeNull();
  });
});

/**
 * ✅ B-15：覆写信号头解析 —— 未覆写时必须为 null（零提示不变），
 * 覆写时返回稳定枚举，前端据此提示「已按已知悉并继续执行」。
 */
describe("parseExportOverride · 覆写信号（B-15）", () => {
  it("头存在（大小写不敏感）→ 返回 high-issues", () => {
    expect(parseExportOverride({ "X-Export-Override": "high-issues" })).toBe("high-issues");
    expect(parseExportOverride({ "x-export-override": "HIGH-ISSUES" })).toBe("high-issues");
  });

  it("头缺失 / 空值 → null（不得伪造覆写提示）", () => {
    expect(parseExportOverride({})).toBeNull();
    expect(parseExportOverride({ "X-Export-Override": "" })).toBeNull();
    expect(parseExportOverride(null)).toBeNull();
  });
});

/**
 * ✅ R183-补（2026-11-07）：严格模式「编号结构自检未执行」的头消费。
 *
 * 后端在自检崩溃/符号不可用时仍放行导出，只在 `X-Numbering-Check` 里回传
 * `checked=false` + 原因。前端若不消费，界面就只有「成功/失败」二态 ——
 * 「没检查」与「检查通过」不可区分。此处锁三件事：
 *   ① 头可解析；② checked=false 必产警示；③ checked=true / 头缺失 零提示。
 */
describe("parseNumberingCheck · 编号结构自检结论（R183-补）", () => {
  const enc = (obj: unknown) => encodeURIComponent(JSON.stringify(obj));

  it("checked=false：解析出原因，且必产「自检未执行」警示", () => {
    const info = parseNumberingCheck({
      "X-Numbering-Check": enc({
        checked: false,
        consistent: null,
        mismatched: null,
        reason: "结构自检执行异常：模拟崩溃",
      }),
    });
    expect(info).toEqual({
      checked: false,
      consistent: null,
      mismatched: null,
      reason: "结构自检执行异常：模拟崩溃",
    });
    const w = numberingCheckWarning(info);
    expect(w).toContain("编号结构自检未执行");
    expect(w).toContain("模拟崩溃");
    expect(w).toContain("不代表");
  });

  it("checked=true：解析结论但零提示（检查跑过就不是异常）", () => {
    const info = parseNumberingCheck({
      "X-Numbering-Check": enc({ checked: true, consistent: true, mismatched: 0, chapters: 12 }),
    });
    expect(info?.checked).toBe(true);
    expect(info?.consistent).toBe(true);
    expect(numberingCheckWarning(info)).toBeNull();
  });

  it("头缺失（非严格模式）→ null 且零提示（不把「没承诺检查」误报成异常）", () => {
    expect(parseNumberingCheck({})).toBeNull();
    expect(parseNumberingCheck(null)).toBeNull();
    expect(numberingCheckWarning(null)).toBeNull();
  });

  it("解码/JSON 非法、缺 checked 布尔字段 → null，不抛错", () => {
    expect(parseNumberingCheck({ "X-Numbering-Check": "%%%" })).toBeNull();
    expect(parseNumberingCheck({ "X-Numbering-Check": "%7B" })).toBeNull();
    expect(parseNumberingCheck({ "X-Numbering-Check": enc({ consistent: true }) })).toBeNull();
    expect(parseNumberingCheck({ "X-Numbering-Check": enc([1, 2]) })).toBeNull();
  });
});

/** 页面接线锚点：`checked=false` 必须真的进入导出页的常驻告警。 */
describe("导出页接线 · 编号结构自检未执行（R183-补）", () => {
  const RAW: Record<string, string> = Object.fromEntries(
    Object.entries(
      import.meta.glob("../pages/**/*.{ts,tsx}", {
        query: "?raw",
        import: "default",
        eager: true,
      }) as Record<string, string>,
    ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
  );

  it("导出 hook 解析响应头，导出 Tab 据 checked=false 渲染告警", () => {
    expect(RAW["pages/schemeWorkbench/useExportRun.ts"]).toContain(
      "parseNumberingCheck(resp.headers)",
    );
    const tab = RAW["pages/schemeWorkbench/tabs/ExportTab.tsx"] || "";
    expect(tab).toContain("numberingCheck && !numberingCheck.checked");
    expect(tab).toContain("numberingCheckWarning");
  });
});
