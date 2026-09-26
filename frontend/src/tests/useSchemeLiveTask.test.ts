/**
 * useSchemeLiveTask 自适应轮询逻辑单测（性能优化遗留项 #1）。
 *
 * 背景：后端 /sse/tasks 原本被 useSchemeLiveTask 固定 3s 轮询，工作台
 * 空闲时仍持续打后端连接池。自适应策略：有运行/暂停任务时快频（3s，
 * 保证任务完成及时触发 onFinished），空闲时慢频（15s，轮询量 ↓80%）。
 *
 * 锁定纯函数 liveTaskPollIntervalMs 的映射关系（hook 内部定时器重排
 * 通过代码审查保证，无需 jsdom 环境）。
 */
import { describe, it, expect } from "vitest";
import {
  ACTIVE_POLL_MS,
  IDLE_POLL_MS,
  liveTaskPollIntervalMs,
  liveTaskTypeLabel,
} from "../hooks/useSchemeLiveTask";

describe("useSchemeLiveTask · 自适应轮询间隔", () => {
  it("有运行/暂停任务时用快频（3s）", () => {
    expect(liveTaskPollIntervalMs(true)).toBe(ACTIVE_POLL_MS);
    expect(ACTIVE_POLL_MS).toBe(3000);
  });

  it("空闲（无运行任务）时用慢频（15s），轮询量 ↓80%", () => {
    expect(liveTaskPollIntervalMs(false)).toBe(IDLE_POLL_MS);
    expect(IDLE_POLL_MS).toBe(15000);
    // 空闲轮询量 = 3/15 = 1/5，即下降 80%
    expect(IDLE_POLL_MS / ACTIVE_POLL_MS).toBe(5);
  });

  it("任务类型标签映射保持不变（回归）：bid_analysis 显示中文", () => {
    expect(liveTaskTypeLabel("content_generation")).toBe("正文生成");
    expect(liveTaskTypeLabel("bid_analysis")).toBe("结构化提取");
    expect(liveTaskTypeLabel("unknown_type")).toBe("unknown_type");
    expect(liveTaskTypeLabel("")).toBe("后台任务");
  });
});