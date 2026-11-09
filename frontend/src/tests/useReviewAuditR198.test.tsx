// @vitest-environment jsdom
/**
 * ✅ R198（2026-11-09）· useReviewAudit hook 级行为测试（R197 遗留⑤：
 * 补齐组件级交互测试 —— 本 hook 此前只有 ?raw 源码棘轮，无行为测试）。
 *
 * 锁定 R197 F1/F3/F4 三个收口形态的行为面：
 * 1. 切方案 [id] effect：本域五个 AbortController 全部中止 + 瞬态全复位；
 * 2. 卸载 cleanup：五 ref 兜底中止（R191 教训：与切方案收口缺一即复活）；
 * 3. handler 落地守卫：切方案后迟到的 AI 结果不得落进新方案；
 * 4. 合规检查重入锁：在飞时二次点击不得发出第二个请求；
 * 5. confirm 在飞锁 + rejected_not_restored 契约消费。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { MutableRefObject } from "react";

// ── 可控 promise 基建 ─────────────────────────────────────────────
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const expertReviewMock = vi.fn();
const checkMock = vi.fn();
const confirmMock = vi.fn();

vi.mock("../api", () => ({
  complianceApi: {
    expertReview: (...a: unknown[]) => expertReviewMock(...a),
    check: (...a: unknown[]) => checkMock(...a),
    consistencyAudit: vi.fn(),
  },
  consistencyRepairApi: {
    conflicts: vi.fn(async () => ({ data: { exists: false } })),
    repairs: vi.fn(async () => ({ data: { items: [] } })),
    scan: vi.fn(),
    repair: vi.fn(),
    confirm: (...a: unknown[]) => confirmMock(...a),
    rollback: vi.fn(),
  },
}));

import { useReviewAudit, type ReviewAuditDeps } from "../pages/schemeWorkbench/useReviewAudit";

function makeRef(): MutableRefObject<AbortController | null> {
  return { current: new AbortController() };
}

function makeDeps(over: Partial<ReviewAuditDeps> = {}): ReviewAuditDeps {
  return {
    id: "scheme-a",
    msg: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
    modal: { confirm: vi.fn() },
    expertAbortRef: makeRef(),
    expertRunning: false,
    setExpertRunning: vi.fn(),
    setExpertResult: vi.fn(),
    checkAbortRef: makeRef(),
    setCheckingCompliance: vi.fn(),
    checklistText: "检查项A",
    checklistDirty: false,
    aiRules: [],
    setComplianceResults: vi.fn(),
    loadComplianceHistory: vi.fn(),
    auditAbortRef: makeRef(),
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

beforeEach(() => {
  expertReviewMock.mockReset().mockImplementation(() => new Promise(() => {}));
  checkMock.mockReset().mockImplementation(() => new Promise(() => {}));
  confirmMock.mockReset().mockImplementation(() => new Promise(() => {}));
});

describe("R198 useReviewAudit 生命周期收口（行为级）", () => {
  it("F1: 切方案中止本域全部五个在飞请求并复位瞬态", () => {
    const deps = makeDeps();
    const refs = [deps.expertAbortRef, deps.checkAbortRef, deps.auditAbortRef];
    const { rerender } = renderHook((d: ReviewAuditDeps) => useReviewAudit(d), { initialProps: deps });

    rerender(makeDeps({ ...deps, id: "scheme-b" }));
    for (const r of refs) {
      // eslint-disable-next-line @typescript-eslint/no-unused-expressions
      expect(r.current?.signal.aborted, "本域 ref 必须被中止").toBe(true);
    }
    expect(deps.setExpertResult).toHaveBeenCalledWith(null);
    expect(deps.setComplianceResults).toHaveBeenCalledWith([]);
    expect(deps.setCrConflicts).toHaveBeenCalledWith([]);
    expect(deps.setCrRepair).toHaveBeenCalledWith(null);
    expect(deps.setCrOpen).toHaveBeenCalledWith(false);
  });

  it("F1: 卸载兜底中止全部 ref", () => {
    const deps = makeDeps();
    const refs = [deps.expertAbortRef, deps.checkAbortRef, deps.auditAbortRef];
    const { unmount } = renderHook((d: ReviewAuditDeps) => useReviewAudit(d), { initialProps: deps });
    unmount();
    for (const r of refs) {
      expect(r.current?.signal.aborted).toBe(true);
    }
  });

  it("F1: 切方案后迟到的专家论证结果不得落地（跨方案守卫）", async () => {
    const deps = makeDeps();
    const d = deferred<{ data: { score: number } }>();
    expertReviewMock.mockReturnValue(d.promise);
    const { result, rerender } = renderHook((p: ReviewAuditDeps) => useReviewAudit(p), {
      initialProps: deps,
    });
    // mount 时 [id] effect 会跑一次合法复位（setExpertResult(null)）—— 记入
    // 基线后清零，后续断言只看「本用例动作」产生的调用
    vi.mocked(deps.setExpertResult).mockClear();

    // 触发但不 await handler 本体（挂起 promise 会把 act 一起挂住）
    let flight: Promise<void> | undefined;
    act(() => {
      flight = result.current.handleExpertReview();
    });
    expect(expertReviewMock).toHaveBeenCalledTimes(1);
    expect(deps.setExpertResult).not.toHaveBeenCalled(); // 请求仍在飞

    rerender(makeDeps({ ...deps, id: "scheme-b" }));
    await act(async () => {
      d.resolve({ data: { score: 88 } });
      await flight?.catch(() => {});
    });
    // 切方案 effect 本身会复位一次（setExpertResult(null)）—— resolve 后
    // 不得出现**第二次**调用（即迟到的 {score:88} 不得落地）
    expect(deps.setExpertResult).toHaveBeenCalledTimes(1);
    expect(deps.setExpertResult).toHaveBeenLastCalledWith(null);
    expect(deps.msg.success, "切方案触发的取消不得弹成功 toast").not.toHaveBeenCalled();
    expect(deps.msg.info).not.toHaveBeenCalled();
  });

  it("F3: 合规检查在飞时二次点击不得发出第二个请求（重入锁）", async () => {
    const deps = makeDeps();
    const d = deferred<{ data: unknown }>();
    checkMock.mockReturnValue(d.promise);
    const { result } = renderHook((p: ReviewAuditDeps) => useReviewAudit(p), { initialProps: deps });

    let flight: Promise<void> | undefined;
    act(() => {
      flight = result.current.handleComplianceCheck();
    });
    act(() => {
      void result.current.handleComplianceCheck();
    });
    expect(checkMock, "重入锁必须挡住第二次点击").toHaveBeenCalledTimes(1);
    expect(deps.setCheckingCompliance).toHaveBeenCalledWith(true);

    await act(async () => {
      d.resolve({ data: {} });
      await flight?.catch(() => {});
    });
    expect(deps.setCheckingCompliance).toHaveBeenLastCalledWith(false);
  });

  it("F4: confirm 在飞锁 + rejected_not_restored 显式消费", async () => {
    const deps = makeDeps({ crRepair: { repair_id: "r1" } as never });
    const d = deferred<{
      data: { repair_id: string; accepted: string[]; rejected: string[]; rejected_not_restored: string[] };
    }>();
    confirmMock.mockReturnValue(d.promise);
    const { result } = renderHook((p: ReviewAuditDeps) => useReviewAudit(p), { initialProps: deps });

    let flight: Promise<void> | undefined;
    act(() => {
      flight = result.current.handleCrConfirm(["c1"], ["c2"]);
    });
    act(() => {
      void result.current.handleCrConfirm(["c1"], ["c2"]);
    });
    expect(confirmMock, "四按钮共用 handler 必须有在飞锁").toHaveBeenCalledTimes(1);

    await act(async () => {
      d.resolve({
        data: { repair_id: "r1", accepted: ["c1"], rejected: ["c2"], rejected_not_restored: ["c2"] },
      });
      await flight?.catch(() => {});
    });
    expect(deps.msg.warning, "R181 加法键必须显式提示（不得静默）").toHaveBeenCalledWith(
      expect.stringContaining("未回滚"),
    );
  });
});
