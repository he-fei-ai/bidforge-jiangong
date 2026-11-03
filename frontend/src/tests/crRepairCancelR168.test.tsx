// @vitest-environment jsdom
/**
 * ✅ R168（2026-11-03）· 一致性修复工作台取消链路 + 竞态守卫 · 组件级交互测试。
 *
 * 背景（R106 FIX-5 的同域收尾缺口）：
 * CR 扫描/修复（POST /consistency/scan、/consistency/repair）是 10 分钟级
 * AI 长任务（timeout 600000），此前与同域三个预检端点不同——
 *   GAP-1  无 AbortController、无取消按钮：点错只能等 AI 跑完或杀后端；
 *   GAP-2  handleQualityCheck 无重入锁：checkingQuality 异步置位挡不住同帧双击；
 *   GAP-3  EXPORT_ISSUE_LABEL 残留后端零产出的死键 duplicate_section_title
 *          （旧 parity 只查「后端 ⊆ 前端」，多余死键不可见）。
 *
 * 本文件按 A（api 契约）/ B（hook 级取消交互）/ C（页面接线源码锁）/
 * D（死键棘轮）四组验证。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, waitFor, cleanup, act } from "@testing-library/react";
import { createRef } from "react";
import type { MutableRefObject } from "react";

afterEach(cleanup);

// ⚠️ vi.mock 提升到 import 之前；工厂内引用的变量必须 vi.hoisted 声明
const { scanMock, repairMock } = vi.hoisted(() => ({
  scanMock: vi.fn(),
  repairMock: vi.fn(),
}));
vi.mock("../api", () => ({
  complianceApi: {
    check: vi.fn(),
    expertReview: vi.fn(),
    consistencyAudit: vi.fn(),
  },
  consistencyRepairApi: {
    scan: (...args: unknown[]) => (scanMock as any)(...args),
    repair: (...args: unknown[]) => (repairMock as any)(...args),
    conflicts: vi.fn().mockResolvedValue({ data: { exists: false } }),
    repairs: vi.fn().mockResolvedValue({ data: { items: [] } }),
    confirm: vi.fn(),
    rollback: vi.fn(),
  },
}));

import { useReviewAudit } from "../pages/schemeWorkbench/useReviewAudit";
import type { ReviewAuditDeps } from "../pages/schemeWorkbench/useReviewAudit";

// ---------------------------------------------------------------------------
// 工具：构造 hook deps（全部 no-op / 空实现，只服务本域被测路径）
// ---------------------------------------------------------------------------
function makeDeps(over: Partial<ReviewAuditDeps> = {}): ReviewAuditDeps {
  const msg = {
    success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn(),
  };
  return {
    id: "scheme-1",
    msg,
    modal: { confirm: vi.fn() },
    expertAbortRef: createRef<AbortController | null>() as MutableRefObject<AbortController | null>,
    expertRunning: false,
    setExpertRunning: vi.fn(),
    setExpertResult: vi.fn(),
    checkAbortRef: createRef<AbortController | null>() as MutableRefObject<AbortController | null>,
    setCheckingCompliance: vi.fn(),
    checklistText: "",
    checklistDirty: false,
    aiRules: [],
    setComplianceResults: vi.fn(),
    loadComplianceHistory: vi.fn(),
    auditAbortRef: createRef<AbortController | null>() as MutableRefObject<AbortController | null>,
    consistencySeverity: "high",
    forceFullRepair: false,
    crScanning: false,
    setCrScanning: vi.fn(),
    crRepairing: false,
    setCrRepairing: vi.fn(),
    crScanId: "",
    setCrRepair: vi.fn(),
    crRepair: null,
    setCrConflicts: vi.fn(),
    setCrSummary: vi.fn(),
    setCrScanId: vi.fn(),
    setCrHistory: vi.fn(),
    setCrOpen: vi.fn(),
    setCrLoading: vi.fn(),
    selectedSectionRef: { current: null },
    setSelectedSection: vi.fn(),
    load: vi.fn(),
    setReviewTick: vi.fn(),
    ...over,
  } as ReviewAuditDeps;
}

/** abort 即以 AbortError 拒绝的挂起 promise（模拟在飞的 10 分钟级请求） */
function hangUntilAbort(signal?: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    if (!signal) return; // 永不 resolve/reject：无 signal 视为接线缺失，B 组会超时红
    signal.addEventListener("abort", () => {
      const e = new Error("canceled") as Error & { code?: string };
      e.name = "AbortError";
      e.code = "ERR_CANCELED";
      reject(e);
    });
  });
}

// ---------------------------------------------------------------------------
// B 组 · hook 级取消交互（R106 FIX-5 同口径）
// ---------------------------------------------------------------------------
describe("CR 扫描/修复取消链路（hook 级）", () => {
  it("B1: handleCrScan 把 AbortSignal 传给 api，取消后置 info 提示并复位 crScanning", async () => {
    scanMock.mockImplementation(
      (_id: string, _data: unknown, signal?: AbortSignal) => hangUntilAbort(signal));
    const deps = makeDeps();
    const { result } = renderHook(() => useReviewAudit(deps));

    act(() => { void result.current.handleCrScan(); });
    await waitFor(() => expect(scanMock).toHaveBeenCalledTimes(1));
    // 第一参起依次是 (schemeId, payload, signal)
    const [, , signal] = scanMock.mock.calls[0] as [string, unknown, AbortSignal];
    expect(signal).toBeInstanceOf(AbortSignal);

    act(() => { result.current.handleCancelCrScan(); });
    expect(signal!.aborted).toBe(true);
    await waitFor(() =>
      expect(deps.msg.info).toHaveBeenCalledWith("已取消一致性扫描"));
    await waitFor(() => {
      // setCrScanning(true) → ... → setCrScanning(false)：最后一个调用是复位
      const calls = (deps.setCrScanning as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[calls.length - 1][0]).toBe(false);
    });
  });

  it("B2: handleCrRepair 同口径 —— signal 透传 + 取消提示 + setCrRepairing 复位", async () => {
    repairMock.mockImplementation(
      (_id: string, _data: unknown, signal?: AbortSignal) => hangUntilAbort(signal));
    const deps = makeDeps({ crScanId: "scan-9" });
    const { result } = renderHook(() => useReviewAudit(deps));

    act(() => { void result.current.handleCrRepair(); });
    await waitFor(() => expect(repairMock).toHaveBeenCalledTimes(1));
    const [, payload, signal] = repairMock.mock.calls[0] as [string, Record<string, unknown>, AbortSignal];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(payload.scan_id).toBe("scan-9");

    act(() => { result.current.handleCancelCrRepair(); });
    expect(signal!.aborted).toBe(true);
    await waitFor(() =>
      expect(deps.msg.info).toHaveBeenCalledWith("已取消一致性修复"));
    await waitFor(() => {
      const calls = (deps.setCrRepairing as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls[calls.length - 1][0]).toBe(false);
    });
  });

  it("B3: 正常完成路径不回归 —— scan 成功后写回 conflicts/summary/scanId", async () => {
    scanMock.mockResolvedValue({
      data: {
        scan_id: "scan-1",
        conflicts: [{ id: "c1" }],
        summary: { total: 1, high: 1, medium: 0, low: 0 },
      },
    });
    const deps = makeDeps();
    const { result } = renderHook(() => useReviewAudit(deps));
    await act(async () => { await result.current.handleCrScan(); });
    expect(deps.setCrConflicts).toHaveBeenCalledWith([{ id: "c1" }]);
    expect(deps.setCrScanId).toHaveBeenCalledWith("scan-1");
    expect(deps.msg.success).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// C 组 · 页面接线源码锁（stopControlR106 先例：源码级静态锁防「hook 有、按钮无」）
// ---------------------------------------------------------------------------
describe("页面接线源码锁", () => {
  // SchemeWorkbenchPage 体量 8000+ 行，页面级挂载测试成本过高；
  // 按本仓 stopControlR106 先例用 raw 源码锁锁「解构 + 按钮条件渲染」。
  const pageSources = import.meta.glob("../pages/SchemeWorkbenchPage.tsx", {
    query: "?raw", import: "default", eager: true,
  }) as Record<string, string>;
  const pageSrc = Object.values(pageSources)[0] || "";

  it("C1: 页面解构了 handleCancelCrScan / handleCancelCrRepair", () => {
    expect(pageSrc).toContain("handleCancelCrScan");
    expect(pageSrc).toContain("handleCancelCrRepair");
  });

  it("C2: 取消按钮条件渲染 —— crScanning/crRepairing 运行中才出现", () => {
    expect(pageSrc).toMatch(/\{crScanning && \(/);
    expect(pageSrc).toMatch(/\{crRepairing && \(/);
  });

  it("C3: handleQualityCheck 重入锁（同帧双击不再并发）", () => {
    expect(pageSrc).toMatch(/if \(!id \|\| checkingQuality\) return;/);
  });
});

// ---------------------------------------------------------------------------
// D 组 · 死键棘轮（与后端 TestExportIssueLabelParity::test_no_frontend_dead_labels 互补）
// ---------------------------------------------------------------------------
describe("EXPORT_ISSUE_LABEL 死键棘轮", () => {
  const sharedSources = import.meta.glob("../pages/schemeWorkbench/exportShared.ts", {
    query: "?raw", import: "default", eager: true,
  }) as Record<string, string>;
  const sharedSrc = Object.values(sharedSources)[0] || "";

  it("D1: duplicate_section_title 死键不得回流（后端 _EXPORT_ISSUE_RULE_MAP 零产出）", () => {
    expect(sharedSrc).not.toContain("duplicate_section_title");
    // 15 类在册键抽查（与后端 pytest 双向锁互补，防文件级误删）
    for (const key of ["empty_section", "orphan_node", "global_facts_blocked",
      "stale_cross_reference", "body_subheading_namespace_conflict"]) {
      expect(sharedSrc).toContain(`${key}:`);
    }
  });
});
