// @vitest-environment jsdom
/**
 * R212（2026-11-10）审核与预检模块 · 深度审计增强护栏（前端）
 *
 * 用户指令：「聚焦【审核与预检模块】，深入分析现有功能架构与实现现状，识别功能缺口和
 * 技术债务，增强模块功能完整性，优化工作流程，补齐组件级交互测试，完善与其他
 * 模块的数据传递、路由配置和调用链路，排查缺陷并修复所有 BUG」。
 *
 * 本文件锁住 R212 前端落地的 6 项修复（护栏风格：行为实证优先，源码棘轮仅用于
 * 「接线必须在位」这类无法用纯函数表达的点）：
 *
 *  FE-1 P1  聚合 finding 的「章节」列**不得**误显「（章节已删除）」
 *          （旧实现把「没有标题」与「章节被删除」用同一个 truthy 回退表达 ⇒
 *            20 章待审核显示成「章节已删除」，点击却跳到真实存在的章节）。
 *  FE-2 P1  一致性审计**覆盖度必须可见**（后端已回传、hook 已存，UI 零渲染）。
 *  FE-3 P2  专家论证 / 一致性审计必须有**同步**在飞锁（同帧双击不得发两次请求）。
 *  FE-4 P2  待确认清单「刷新」提示**三态**（数据源失败时不得弹绿色成功）。
 *  FE-5 P3  severity 色板/文案**单一事实源**（含 block ⇒ volcano + 「阻断」）。
 *  FE-6 P3  导出阻断明细须保留 `display_title`；问题数按涉及处数汇总。
 *
 * 禁改原则：这些断言锁的是**意图**（不谎报删除 / 覆盖度可见 / 不重复发请求 /
 * 提示与事实一致 / 色板同源 / 契约不丢字段），不是实现细节。
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, cleanup, screen, waitFor } from "@testing-library/react";
import React from "react";

import { SEVERITY_COLOR, SEVERITY_LABEL } from "../types/audit";
import { pendingRefreshTone } from "../components/review/PendingConfirmPanel";
import { parseExportBlock } from "../utils/exportResponse";

// 源码棘轮读取走 vite 的 ``?raw`` 导入（**不执行**被读模块 ⇒ 无副作用），
// 不用 ``node:fs`` —— 前端 tsconfig 未安装 @types/node，用 node 内置模块会
// 让 ``tsc --noEmit`` 直接红（TS2307），违反「类型检查必须 EXIT 0」的硬约束。
import readinessDashboardSrc from "../components/review/ReadinessDashboard.tsx?raw";
import reviewTabSrc from "../pages/schemeWorkbench/tabs/ReviewTab.tsx?raw";
import complianceRepairDrawerSrc from "../pages/schemeWorkbench/ComplianceRepairDrawer.tsx?raw";
import factsTabSrc from "../pages/schemeWorkbench/tabs/FactsTab.tsx?raw";
import useReviewAuditSrc from "../pages/schemeWorkbench/useReviewAudit.tsx?raw";
import schemeWorkbenchPageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

afterEach(cleanup);
vi.restoreAllMocks();

// ===========================================================================
// FE-1 · 聚合 finding 章节列不得谎报「章节已删除」
//    （DOM 行为用例见 ReadinessDashboard.test.tsx 的 R212 用例 —— 该文件已具备
//      总检加载 / 表格渲染的完整夹具；此处只锁「不得回流旧二态回退」的接线）
// ===========================================================================
describe("ReadinessDashboard · 聚合 finding 的章节列标签", () => {
  it("聚合 finding 的章节列标签必须含「处章节」分支（接线锁）", () => {
    // ⚠️ 只断言**正向**接线：旧文案「（章节已删除）」仍出现在本文件的**注释**里
    //    （说明旧实现形态），按「静态源码锁不得被注释里的字面量污染」的教训
    //    （R208），此处不做负向字符串断言；负向行为由 ReadinessDashboard.test.tsx
    //    的 R212 DOM 用例锁（真渲染 ⇒ 注释不参与）。
    const src = readinessDashboardSrc;
    expect(src).toContain("处章节");
    expect(src).toContain("章节（标题未知）");
  });
});

// ===========================================================================
// FE-4 · 待确认清单刷新提示三态（纯函数判定 + DOM 错误态）
// ===========================================================================
describe("PendingConfirmPanel · 刷新提示三态", () => {
  it("全部成功 → success 文案", () => {
    const tone = pendingRefreshTone({ facts: "ok", defects: "ok" });
    expect(tone.kind).toBe("success");
    expect(tone.text).toContain("已刷新");
  });

  it("事实源失败 → warning 且指明失败源", () => {
    const tone = pendingRefreshTone({ facts: "error", defects: "ok" });
    expect(tone.kind).toBe("warning");
    expect(tone.text).toContain("事实数据");
    expect(tone.text).not.toContain("缺陷数据");
  });

  it("两个源都失败 → warning 且同时列出", () => {
    const tone = pendingRefreshTone({ facts: "error", defects: "error" });
    expect(tone.kind).toBe("warning");
    expect(tone.text).toContain("事实数据");
    expect(tone.text).toContain("缺陷数据");
  });

  it("缺陷端 404（功能关闭）不算失败源", () => {
    const tone = pendingRefreshTone({ facts: "ok", defects: "off" });
    expect(tone.kind).toBe("success");
  });
});

// ===========================================================================
// FE-5 · severity 单一事实源（含 block）
// ===========================================================================
describe("severity 色板单一事实源", () => {
  it("block 必须是 volcano + 「阻断」（旧内联三元不含 block ⇒ 落 blue）", () => {
    expect(SEVERITY_COLOR.block).toBe("volcano");
    expect(SEVERITY_LABEL.block).toBe("阻断");
  });

  it("审核域三处不得再内联 severity 三元", () => {
    const files: Array<[string, string]> = [
      ["ReviewTab.tsx", reviewTabSrc],
      ["ComplianceRepairDrawer.tsx", complianceRepairDrawerSrc],
      ["FactsTab.tsx", factsTabSrc],
    ];
    for (const [name, src] of files) {
      expect(src, name).not.toContain('=== "high" ? "red"');
    }
  });
});

// ===========================================================================
// FE-6 · 导出阻断明细保留 display_title
// ===========================================================================
describe("parseExportBlock · 保留 display_title", () => {
  it("后端产出的可区分标题不得在前端解析层被丢弃", async () => {
    const body = {
      error: "export_blocked_high_issues",
      high_issue_count: 1,
      high_issue_total: 20,
      high_issues: [
        {
          type: "review_pending",
          rule_id: "DLV-09",
          severity: "high",
          count: 20,
          title: "正文均已通过审核",
          display_title: "正文均已通过审核·待审核",
        },
      ],
    };
    const info = await parseExportBlock({ response: { data: body } });
    expect(info).not.toBeNull();
    expect(info!.issues[0].display_title).toBe("正文均已通过审核·待审核");
    expect(info!.issueTotal).toBe(20);
  });
});

// ===========================================================================
// FE-2 / FE-3 · 接线必须在位（源码棘轮，意图锁定）
// ===========================================================================
describe("审核域接线锁", () => {
  it("一致性覆盖度必须在卡片上渲染（data-testid 接线）", () => {
    expect(reviewTabSrc).toContain('data-testid="consistency-coverage"');
    expect(reviewTabSrc).toContain('data-testid="consistency-partial-tag"');
  });

  it("专家论证 / 一致性审计必须有同步在飞锁（ref 判据）", () => {
    expect(useReviewAuditSrc).toContain("expertInFlightRef");
    expect(useReviewAuditSrc).toContain("auditInFlightRef");
    expect(useReviewAuditSrc).toContain("if (expertInFlightRef.current) return;");
    expect(useReviewAuditSrc).toContain("if (auditInFlightRef.current) return;");
  });

  it("载入上次一致性结果必须搬覆盖度字段", () => {
    expect(schemeWorkbenchPageSrc).toContain(
      "checked_chars: data.checked_chars ?? data.counts?.checked_chars",
    );
    expect(schemeWorkbenchPageSrc).toContain(
      "total_chars: data.total_chars ?? data.counts?.total_chars",
    );
  });
});
