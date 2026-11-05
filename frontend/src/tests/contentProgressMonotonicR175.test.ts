/**
 * R175 BUG-3 回归：进度单调护栏 `nextProgress`。
 *
 * 背景：正文链进度有多个来源（stats/ping 心跳、section_stage 单章阶段、
 * progress 事件、终态收尾）。心跳是 raw 快照，存在「running 移除与 done
 * 自增之间」的窗口，会让进度条回退。页面统一经 `nextProgress` 过护栏。
 *
 * 本文件只测纯函数（不挂载页面），锁定：
 *   · 心跳不得使进度回退；
 *   · 单章阶段事件能驱动进度前进；
 *   · 终态进度不回退；
 *   · 非法输入保留 prev（不清零）。
 */
import { describe, expect, it } from "vitest";
import { nextProgress } from "../utils/contentEvents";

describe("R175 · nextProgress 单调护栏", () => {
  it("心跳 raw 值低于已上报值时不得回退", () => {
    expect(nextProgress(0.6, 0.45)).toBe(0.6);
  });

  it("单章阶段事件（更大的进度）能推进进度", () => {
    expect(nextProgress(0.3, 0.32)).toBe(0.32);
  });

  it("终态 progress=1 不回退；已到 1 后再收到旧值也保持 1", () => {
    expect(nextProgress(0.99, 1)).toBe(1);
    expect(nextProgress(1, 0.8)).toBe(1);
  });

  it("非法输入（undefined / NaN / 字符串）保留 prev", () => {
    expect(nextProgress(0.5, undefined)).toBe(0.5);
    expect(nextProgress(0.5, null)).toBe(0.5);
    expect(nextProgress(0.5, Number.NaN)).toBe(0.5);
    expect(nextProgress(0.5, "0.9")).toBe(0.5);
  });

  it("入参越界时夹紧到 [0, 1]", () => {
    expect(nextProgress(0, 1.2)).toBe(1);
    expect(nextProgress(0, -0.1)).toBe(0);
    expect(nextProgress(0, 0.123)).toBe(0.123);
  });

  it("prev 非法时按 0 起步，不抛异常", () => {
    expect(nextProgress(Number.NaN, 0.2)).toBe(0.2);
    expect(nextProgress(-1, 0.2)).toBe(0.2);
  });
});
