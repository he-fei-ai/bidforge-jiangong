/**
 * R222（2026-11-12）· 日志驱动：导出渲染降级可见性前端护栏。
 *
 * 后台日志实证存在「成稿缺图、failed=0、X-Cache-Status=miss、产物写缓存」
 * 的导出，前端此前完全没有「成稿实际落图数」这一维度。本轮补：
 * ① 交付侧落图统计展示与告警；② 缓存命中回带历史缺图提示；
 * ③ X-Export-Degraded: charts-dropped 的中文文案。
 *
 * 纯函数测试，不挂载 DOM / antd。
 */
import { describe, expect, it } from "vitest";
import {
  cachedChartDegradedWarning,
  chartDeliveryWarning,
  chartRenderStatsParts,
  parseChartRenderStats,
  parseExportDegraded,
} from "../utils/exportResponse";

describe("R222 · 成稿落图统计展示", () => {
  it("旧统计形状逐字不变（加法式字段不得破坏既有精确断言）", () => {
    expect(chartRenderStatsParts({ fe: 3, backend_ok: 2, failed: 1 })).toEqual([
      "前端图 3 张",
      "后端原生 2 张",
      "失败 1 张",
    ]);
    expect(chartRenderStatsParts({})).toEqual([]);
  });

  it("成稿缺图时追加「落图 M/N 张（缺 K 张）」", () => {
    expect(
      chartRenderStatsParts({
        backend_ok: 1,
        chart_expected: 4,
        chart_inserted: 2,
        chart_dropped: 1,
        chart_duplicate_skipped: 1,
      }),
    ).toEqual(["后端原生 1 张", "成稿落图 2/4 张（缺 1 张）"]);
  });

  it("正常出图（dropped=0）不追加噪声片段", () => {
    expect(chartRenderStatsParts({ backend_ok: 1, chart_expected: 1, chart_inserted: 1 })).toEqual([
      "后端原生 1 张",
    ]);
  });
});

describe("R222 · 成稿缺图告警", () => {
  it("点名数量、预期/实际落图、降级与不写缓存，并给出可行动建议", () => {
    const w = chartDeliveryWarning({
      failed: 0,
      chart_expected: 4,
      chart_inserted: 2,
      chart_dropped: 2,
      chart_dropped_details: [
        { section_id: "s1", chart_type: "gantt", reason: "no_code" },
        { section_id: "s2", chart_type: "flowchart", reason: "insert_failed" },
      ],
    });
    expect(w).toContain("2 张图表未插入");
    expect(w).toContain("无登记图表代码");
    expect(w).toContain("图片插入失败");
    expect(w).toContain("正文共 4 处");
    expect(w).toContain("实际落图 2 张");
    expect(w).toContain("降级");
    expect(w).toContain("不写入缓存");
    expect(w).toContain("重新导出");
  });

  it("未知 reason 不产生字面 undefined / 空括号", () => {
    const w = chartDeliveryWarning({
      chart_expected: 2,
      chart_inserted: 1,
      chart_dropped: 1,
      chart_dropped_details: [{ section_id: "s", chart_type: "x", reason: "future_reason" }],
    });
    expect(w).not.toContain("undefined");
    expect(w).not.toContain("（）");
  });

  it("0 / 缺省 / 脏数据返回 null", () => {
    expect(chartDeliveryWarning({})).toBeNull();
    expect(chartDeliveryWarning({ chart_dropped: 0 })).toBeNull();
    expect(chartDeliveryWarning({ chart_dropped: Number.NaN })).toBeNull();
    expect(chartDeliveryWarning({ chart_dropped: -1 })).toBeNull();
  });
});

describe("R222 · 缓存复用历史缺图提示", () => {
  it("命中且上次成稿缺图 → 提示重新导出", () => {
    const w = cachedChartDegradedWarning({
      cached: true,
      cached_degraded: true,
      cached_stats: { chart_expected: 5, chart_inserted: 4, chart_dropped: 1 },
    });
    expect(w).toContain("复用了上次导出的成果");
    expect(w).toContain("1 张图表未插入");
    expect(w).toContain("正文 5 处");
    expect(w).toContain("重新导出");
  });

  it("旧旁车（只有 cached:true）→ null，响应与文案零变化", () => {
    expect(cachedChartDegradedWarning({ cached: true })).toBeNull();
  });

  it("命中但上次成稿完整 → 不误报", () => {
    expect(
      cachedChartDegradedWarning({
        cached: true,
        cached_stats: { chart_expected: 3, chart_inserted: 3, chart_dropped: 0, failed: 0 },
      }),
    ).toBeNull();
  });

  it("非命中路径（本次全量渲染）→ 不误报", () => {
    expect(cachedChartDegradedWarning({ chart_dropped: 2 })).toBeNull();
  });
});

describe("R222 · X-Export-Degraded: charts-dropped", () => {
  it("给出精确中文文案，不落入未知信号通用文案", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "charts-dropped" });
    expect(d).not.toBeNull();
    expect(d!.signals).toEqual(["charts-dropped"]);
    expect(d!.message).toContain("成稿有图表未插入");
    expect(d!.message).toContain("不写入缓存");
    expect(d!.message).not.toContain("导出已降级");
  });

  it("与其它降级信号合并时逐项翻译", () => {
    const d = parseExportDegraded({ "X-Export-Degraded": "content-residue,charts-dropped" });
    expect(d!.message).toContain("格式残留");
    expect(d!.message).toContain("成稿有图表未插入");
  });
});

describe("R222 · 响应头解析新字段", () => {
  it("解析 chart_* 落图统计与明细", () => {
    const stats = parseChartRenderStats({
      "X-Chart-Render-Stats": JSON.stringify({
        failed: 0,
        backend_ok: 1,
        chart_expected: 2,
        chart_inserted: 1,
        chart_dropped: 1,
        chart_duplicate_skipped: 0,
        chart_dropped_details: [{ section_id: "s", chart_type: "gantt", reason: "no_code" }],
      }),
    });
    expect(stats?.chart_expected).toBe(2);
    expect(stats?.chart_dropped).toBe(1);
    expect(stats?.chart_dropped_details?.[0].reason).toBe("no_code");
  });

  it("解析缓存命中回带的 cached_stats / cached_degraded", () => {
    const stats = parseChartRenderStats({
      "x-chart-render-stats": JSON.stringify({
        cached: true,
        cached_degraded: true,
        cached_stats: { chart_expected: 5, chart_inserted: 4, chart_dropped: 1 },
      }),
    });
    expect(stats?.cached).toBe(true);
    expect(stats?.cached_degraded).toBe(true);
    expect(stats?.cached_stats?.chart_dropped).toBe(1);
    expect(cachedChartDegradedWarning(stats!)).not.toBeNull();
  });

  it("旧后端命中响应 {\"cached\":true} 仍可解析且不产生提示", () => {
    const stats = parseChartRenderStats({ "X-Chart-Render-Stats": '{"cached":true}' });
    expect(stats?.cached).toBe(true);
    expect(cachedChartDegradedWarning(stats!)).toBeNull();
  });
});
