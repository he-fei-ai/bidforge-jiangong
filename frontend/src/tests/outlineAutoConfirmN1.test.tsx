// @vitest-environment jsdom
/**
 * ✅ R179 N1（2026-11-05）：目录生成自动确认 —— 纯判定 + 前端编排契约。
 *
 * 本文件锁定三件事（对应任务书 §N1）：
 *  1. 纯判定：开关关闭恒 manual；已有正文 / 未 ready / 空目录一律 manual。
 *  2. 等价性：开关关闭时 `handleOutlineReady` **逐字委托** `openOutlineGate`
 *     （同一次调用、同一个参数引用、不启动倒计时、不写快照）。
 *  3. 开启后：只在 ready 时 arm 倒计时；任何取消/交互立即回人工；
 *     倒计时到期才调用自动保存（snapshot_before=outline_auto_confirm）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { MessageInstance } from "antd/es/message/interface";
import {
  countdownSeconds,
  decideAutoConfirm,
  describeAutoConfirmBlockers,
  parseAutoConfirm,
} from "../pages/schemeWorkbench/outlineAutoConfirm";
import { useOutlineAutoConfirm } from "../pages/schemeWorkbench/useOutlineAutoConfirm.tsx";

const READY = { ready: true, blockers: [] };

function makeMsg() {
  return {
    info: vi.fn(),
    warning: vi.fn(),
  } as unknown as MessageInstance;
}

describe("outlineAutoConfirm 纯判定", () => {
  it("开关关闭：即使后端 ready 也恒为 manual（reason=switch_off）", () => {
    const sig = parseAutoConfirm(READY);
    const d = decideAutoConfirm(false, sig, { hasContent: false, outlineLength: 3 });
    expect(d.mode).toBe("manual");
    expect(d.reason).toBe("switch_off");
  });

  it("已存在正文：永不自动确认（reason=existing_content）", () => {
    const d = decideAutoConfirm(true, parseAutoConfirm(READY), {
      hasContent: true,
      outlineLength: 3,
    });
    expect(d.mode).toBe("manual");
    expect(d.reason).toBe("existing_content");
  });

  it("未 ready（含 blockers）：转人工（reason=not_ready）", () => {
    const sig = parseAutoConfirm({
      ready: false,
      blockers: [{ code: "plant", message: "占位标题", count: 2 }],
    });
    const d = decideAutoConfirm(true, sig, { hasContent: false, outlineLength: 3 });
    expect(d.mode).toBe("manual");
    expect(d.reason).toBe("not_ready");
  });

  it("后端带 blockers 时即使 ready=true 也不视为 ready（防御脏数据）", () => {
    const sig = parseAutoConfirm({ ready: true, blockers: [{ code: "empty_chapters" }] });
    expect(sig.ready).toBe(false);
  });

  it("旧后端/缺失键：parseAutoConfirm 一律未就绪", () => {
    expect(parseAutoConfirm(undefined).ready).toBe(false);
    expect(parseAutoConfirm(null).ready).toBe(false);
    expect(parseAutoConfirm("garbage").ready).toBe(false);
  });

  it("空目录不自动确认", () => {
    const d = decideAutoConfirm(true, parseAutoConfirm(READY), {
      hasContent: false,
      outlineLength: 0,
    });
    expect(d.mode).toBe("manual");
    expect(d.reason).toBe("empty_outline");
  });

  it("阻断原因摘要：code 映射中文 + 计数", () => {
    const sig = parseAutoConfirm({
      ready: false,
      blockers: [
        { code: "placeholder_titles", count: 2 },
        { code: "empty_chapters", count: 1 },
      ],
    });
    expect(describeAutoConfirmBlockers(sig)).toBe("占位/泛化标题×2、空章");
  });

  it("倒计时秒数只读换算（默认 8000ms → 8s）", () => {
    expect(countdownSeconds(8000)).toBe(8);
    expect(countdownSeconds(0)).toBe(8);
    expect(countdownSeconds(3000)).toBe(3);
  });
});

describe("useOutlineAutoConfirm 编排", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("开关关闭：逐字委托 openOutlineGate（同一引用、不 arm、不保存）", () => {
    const gate = vi.fn();
    const save = vi.fn(async () => true);
    const msg = makeMsg();
    const outline = [{ title: "工程概况" }];
    const { result } = renderHook(() =>
      useOutlineAutoConfirm({
        id: "s1",
        enabled: false,
        delayMs: 8000,
        hasExistingContent: () => false,
        openOutlineGate: gate,
        saveAuto: save,
        onSwitchToManual: vi.fn(),
        msg,
      }),
    );
    act(() => result.current.handleOutlineReady(outline, READY));
    expect(gate).toHaveBeenCalledTimes(1);
    expect(gate.mock.calls[0][0]).toBe(outline);
    expect(save).not.toHaveBeenCalled();
    expect(result.current.armed).toBe(false);
    // 关闭态不打印「自动确认已启动」
    expect(msg.info).not.toHaveBeenCalled();
  });

  it("开启 + ready：arm 倒计时，不立即打开人工闸门", () => {
    const gate = vi.fn();
    const outline = [{ title: "工程概况" }];
    const { result } = renderHook(() =>
      useOutlineAutoConfirm({
        id: "s1",
        enabled: true,
        delayMs: 8000,
        hasExistingContent: () => false,
        openOutlineGate: gate,
        saveAuto: vi.fn(async () => true),
        onSwitchToManual: vi.fn(),
        msg: makeMsg(),
      }),
    );
    act(() => result.current.handleOutlineReady(outline, READY));
    expect(gate).not.toHaveBeenCalled();
    expect(result.current.armed).toBe(true);
  });

  it("开启 + 已存在正文：不 arm，直接回人工（红线）", () => {
    const gate = vi.fn();
    const outline = [{ title: "工程概况" }];
    const { result } = renderHook(() =>
      useOutlineAutoConfirm({
        id: "s1",
        enabled: true,
        delayMs: 8000,
        hasExistingContent: () => true,
        openOutlineGate: gate,
        saveAuto: vi.fn(async () => true),
        onSwitchToManual: vi.fn(),
        msg: makeMsg(),
      }),
    );
    act(() => result.current.handleOutlineReady(outline, READY));
    expect(gate).toHaveBeenCalledTimes(1);
    expect(result.current.armed).toBe(false);
  });

  it("倒计时中被取消：立即清计时器并回人工", () => {
    const gate = vi.fn();
    const outline = [{ title: "工程概况" }];
    const { result } = renderHook(() =>
      useOutlineAutoConfirm({
        id: "s1",
        enabled: true,
        delayMs: 8000,
        hasExistingContent: () => false,
        openOutlineGate: gate,
        saveAuto: vi.fn(async () => true),
        onSwitchToManual: vi.fn(),
        msg: makeMsg(),
      }),
    );
    act(() => result.current.handleOutlineReady(outline, READY));
    act(() => result.current.cancelAuto("用户交互"));
    expect(result.current.armed).toBe(false);
    expect(gate).toHaveBeenCalledTimes(1);
    expect(gate.mock.calls[0][0]).toBe(outline);
  });

  it("倒计时到期：才调用自动保存（snapshot_before=outline_auto_confirm）", async () => {
    vi.useFakeTimers();
    const save = vi.fn<(o: unknown[], snapshotBefore: string) => Promise<boolean>>(
      async () => true,
    );
    const outline = [{ title: "工程概况" }];
    const { result } = renderHook(() =>
      useOutlineAutoConfirm({
        id: "s1",
        enabled: true,
        delayMs: 8000,
        hasExistingContent: () => false,
        openOutlineGate: vi.fn(),
        saveAuto: save,
        onSwitchToManual: vi.fn(),
        msg: makeMsg(),
      }),
    );
    act(() => result.current.handleOutlineReady(outline, READY));
    expect(save).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(8200);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0][0]).toBe(outline);
    expect(save.mock.calls[0][1]).toBe("outline_auto_confirm");
    expect(result.current.armed).toBe(false);
  });
});
