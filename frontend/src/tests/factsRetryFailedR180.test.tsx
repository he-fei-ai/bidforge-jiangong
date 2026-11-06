// @vitest-environment jsdom
/**
 * R180（2026-11-06）：T-05 失败段告警「立即重试」入口。
 *
 * 锁定：
 *   1. failed>0 时展示可执行按钮，点击只回调一次（不重复触发）；
 *   2. retrying=true 时按钮进入 loading 并禁用（防止并发重跑）；
 *   3. 仅模型声明空段（declared_failed）时**不**展示重试按钮，避免把有效空段
 *      误当成失败重跑（变异验证：去掉 `failed > 0` 条件后用例 3 变红）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import { FactsSegmentFailuresAlert } from "../pages/schemeWorkbench/FactsSegmentFailuresAlert";

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

describe("FactsSegmentFailuresAlert · 立即重试失败段（R180/T-05）", () => {
  afterEach(() => cleanup());
  it("failed>0 时展示按钮且点击只触发一次回调", () => {
    const onRetry = vi.fn();
    const { getByTestId, getByText } = render(
      <FactsSegmentFailuresAlert stats={{ ok: 10, failed: 2, total: 12 }} onRetry={onRetry} />,
    );
    const btn = getByTestId("facts-retry-failed-btn");
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(getByText("立即重试失败段")).toBeTruthy();
  });

  it("retrying=true 时按钮禁用，不触发回调", () => {
    const onRetry = vi.fn();
    const { getByTestId } = render(
      <FactsSegmentFailuresAlert stats={{ failed: 2, total: 12 }} onRetry={onRetry} retrying />,
    );
    const btn = getByTestId("facts-retry-failed-btn") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it("只有 declared_failed（模型声明空段）时不展示重试按钮", () => {
    const onRetry = vi.fn();
    const { queryByTestId } = render(
      <FactsSegmentFailuresAlert
        stats={{ ok: 10, failed: 0, total: 12, declared_failed: 2, declared_failed_details: [] }}
        onRetry={onRetry}
      />,
    );
    expect(queryByTestId("facts-retry-failed-btn")).toBeNull();
  });
});
