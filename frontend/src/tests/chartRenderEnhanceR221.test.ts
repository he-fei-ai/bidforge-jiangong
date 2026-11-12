/**
 * R221（2026-11-11）· 图表导出渲染增强前端护栏。
 *
 * 承接 `chart-render-fix-report/chart-render-fix-report.md` 的两个短期建议：
 * ① 导出预检增加图表渲染后端可用性提示；② render_stats 增加 PIL 兜底计数。
 * 另锁定「开关被关闭」与「无本地兜底」两类失败必须给不同建议 —— 前者
 * 开启开关即可修复，后者开启也无效，混为一谈会直接误导用户。
 *
 * 纯函数测试，不挂载 DOM、不依赖 antd。
 */
import { describe, expect, it } from "vitest";
import {
  chartRenderReadinessHint,
  chartRenderStatsParts,
  noHttpFallbackWarning,
  noPilFallbackWarning,
  parseChartRenderStats,
  pilFallbackDisabledWarning,
} from "../utils/exportResponse";

describe("R221 · PIL 兜底计数（报告 §6.1 ②）", () => {
  it("旧统计形状逐字不变（加法式字段不得破坏既有精确断言）", () => {
    expect(chartRenderStatsParts({ fe: 10, backend_ok: 0, failed: 2 })).toEqual([
      "前端图 10 张",
      "失败 2 张",
    ]);
    expect(chartRenderStatsParts({})).toEqual([]);
    expect(chartRenderStatsParts({ fe: 2, fallback_borrowed: 0 })).toEqual(["前端图 2 张"]);
  });

  it("出现 pil_fallback 时追加「PIL 兜底 N 张」且不改写总数文案", () => {
    expect(
      chartRenderStatsParts({ fe: 1, backend_ok: 4, pil_fallback: 3, http_ok: 1 }),
    ).toEqual(["前端图 1 张", "后端原生 4 张", "PIL 兜底 3 张"]);
  });

  it("0 值不展示（不发噪声提示）", () => {
    expect(chartRenderStatsParts({ backend_ok: 2, pil_fallback: 0 })).toEqual(["后端原生 2 张"]);
  });
});

describe("R221 · 开关关闭告警（与「无本地兜底」严格区分）", () => {
  it("给出可行动建议并带上类型明细", () => {
    const w = pilFallbackDisabledWarning({
      failed: 2,
      pil_fallback_disabled: 2,
      pil_fallback_disabled_types: ["flowchart", "gantt"],
    });
    expect(w).toContain("2 张");
    expect(w).toContain("flowchart");
    expect(w).toContain("gantt");
    expect(w).toContain("开启");
    expect(w).toContain("PIL兜底渲染");
  });

  it("不得宣称「无法补救」—— 那是对无本地兜底类型的专属措辞", () => {
    const w = pilFallbackDisabledWarning({ pil_fallback_disabled: 1 });
    expect(w).not.toContain("无法补救");
  });

  it("0 / 缺省 / 非有限数时返回 null", () => {
    expect(pilFallbackDisabledWarning({})).toBeNull();
    expect(pilFallbackDisabledWarning({ pil_fallback_disabled: 0 })).toBeNull();
    expect(pilFallbackDisabledWarning({ pil_fallback_disabled: Number.NaN })).toBeNull();
  });
});

describe("R221 · 归因文案覆盖其它图表类型", () => {
  it("no_http_fallback 不再硬编码「甘特图」，且不出现无效建议", () => {
    const w = noHttpFallbackWarning({
      no_http_fallback: 1,
      no_http_fallback_types: ["layout", "labor"],
    });
    expect(w).toContain("1 张");
    expect(w).toContain("layout");
    expect(w).toContain("没有 HTTP 渲染兜底");
    expect(w).not.toContain("甘特图");
    expect(w).not.toContain("PIL 兜底");
  });

  it("no_pil_fallback 保留既有关键词（R179 护栏不回归）", () => {
    const w = noPilFallbackWarning({
      no_pil_fallback: 1,
      no_pil_fallback_types: ["architecture"],
    });
    expect(w).toContain("1 张");
    expect(w).toContain("architecture");
    expect(w).toContain("PIL 兜底");
    expect(w).toContain("无法补救");
  });
});

describe("R221 · 预检渲染就绪度（报告 §6.1 ①）", () => {
  it("HTTP 不可用 + 语法载荷 → 提示兜底路径已开启", () => {
    const hint = chartRenderReadinessHint({
      http_service_available: false,
      syntax_payload_count: 3,
      unrenderable_syntax_count: 0,
      pil_fallback_default: true,
    });
    expect(hint).toContain("3 张");
    expect(hint).toContain("HTTP 渲染服务不可用");
    expect(hint).toContain("默认已开启");
  });

  it("存在本地画不了的语法载荷 → 点名数量与类型并说明后果", () => {
    const hint = chartRenderReadinessHint({
      all_backends_unavailable: true,
      syntax_payload_count: 5,
      unrenderable_syntax_count: 2,
      unrenderable_syntax_types: ["architecture", "mindmap"],
      pil_fallback_default: true,
    });
    expect(hint).toContain("其中 2 张");
    expect(hint).toContain("architecture");
    expect(hint).toContain("mindmap");
    expect(hint).toContain("成稿中缺失");
  });

  it("默认值关闭时给出「建议开启」而非「默认已开启」", () => {
    const hint = chartRenderReadinessHint({
      http_service_available: false,
      syntax_payload_count: 1,
      pil_fallback_default: false,
    });
    expect(hint).toContain("建议开启");
    expect(hint).not.toContain("默认已开启");
  });

  it("服务可用 / 尚未探测 / 无语法图 / 空对象一律不提示（三态不得当布尔用）", () => {
    expect(chartRenderReadinessHint({ http_service_available: true, syntax_payload_count: 2 })).toBeNull();
    expect(chartRenderReadinessHint({ http_service_available: null, syntax_payload_count: 2 })).toBeNull();
    expect(chartRenderReadinessHint({ http_service_available: false, syntax_payload_count: 0 })).toBeNull();
    expect(chartRenderReadinessHint({ http_service_available: false })).toBeNull();
    expect(chartRenderReadinessHint(null)).toBeNull();
    expect(chartRenderReadinessHint(undefined)).toBeNull();
  });
});

describe("R221 · 响应头解析新增字段", () => {
  it("X-Chart-Render-Stats 可解析 pil_fallback / http_ok / pil_fallback_disabled", () => {
    const stats = parseChartRenderStats({
      "X-Chart-Render-Stats": JSON.stringify({
        failed: 1,
        backend_ok: 2,
        pil_fallback: 2,
        http_ok: 0,
        pil_fallback_disabled: 1,
        pil_fallback_disabled_types: ["flowchart"],
      }),
    });
    expect(stats?.pil_fallback).toBe(2);
    expect(stats?.http_ok).toBe(0);
    expect(stats?.pil_fallback_disabled).toBe(1);
    expect(stats?.pil_fallback_disabled_types).toEqual(["flowchart"]);
  });
});
