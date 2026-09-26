/**
 * 审核与预检模块的类型契约回归锁（2026-09-23）。
 *
 * 背景：后端 review.py 的 summary 新增了 reviewed_sections / reviewed_progress /
 * approved_progress，/overview 的 stats 新增了 export_issue_count，但前端
 * types/audit.ts 长期未同步 —— 类型契约漂移只能在运行期"点了没反应"才暴露。
 * 本用例用 TS 编译期赋值 + 运行期断言双向锁住字段存在性。
 */
import { describe, it, expect } from "vitest";
import {
  REVIEW_STATUS_COLOR,
  SEVERITY_COLOR,
  SEVERITY_LABEL,
  type PreflightStats,
  type ReviewSummary,
  type Severity,
} from "../types/audit";

describe("审核与预检 · 类型契约（与后端字段同步）", () => {
  it("严重度标签 / 色板完整覆盖四档（含 block），杜绝未映射档位", () => {
    expect(Object.keys(SEVERITY_LABEL).sort()).toEqual(
      ["block", "high", "low", "medium"]);
    for (const k of ["block", "high", "medium", "low"] as Severity[]) {
      expect(SEVERITY_LABEL[k]).toBeTruthy();
      expect(SEVERITY_COLOR[k]).toBeTruthy();
    }
  });

  it("ReviewSummary 声明后端 summary 端点返回的 reviewed_* 字段", () => {
    const s: ReviewSummary = {
      scheme_status: "目录已确认",
      review_status: "pending",
      scheme_name: "示例方案",
      counts: {},
      labels: {},
      total_sections: 2,
      generated_sections: 1,
      approved_sections: 1,
      progress: 50,
      reviewed_sections: 2,
      reviewed_progress: 100,
      approved_progress: 50,
      reviewed_all: true,
      records: [],
    };
    expect(s.reviewed_sections).toBe(2);
    expect(s.reviewed_progress).toBe(100);
    expect(s.approved_progress).toBe(50);
  });

  it("PreflightStats 声明 /overview 返回的 export_issue_count（G1 数据链）", () => {
    const st: PreflightStats = {
      section_count: 2, leaf_count: 1, generated_count: 2,
      total_words: 100, word_budget: 0, empty_ratio: 0,
      chart_total: 0, chart_done: 0,
      export_issue_count: 3,
    };
    expect(st.export_issue_count).toBe(3);
  });

  it("审核状态色板覆盖状态机全部五个状态", () => {
    for (const k of ["", "pending", "reviewing", "approved", "rejected"] as const) {
      expect(k in REVIEW_STATUS_COLOR).toBe(true);
    }
  });
});
