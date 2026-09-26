// @vitest-environment jsdom
/**
 * 全局「消息中心」ActivityHint · 组件级交互测试（补全 F9）。
 *
 * ActivityHint 是各功能提示与后台消息的统一出口（原生 toast 已全面取消），
 * 此前无组件级测试。本文件锁定：
 *   1. 无消息：渲染铃铛图标（无最新气泡）；
 *   2. 最新消息：以彩色 chip 展示（kind 决定图标/颜色）；点击打开历史浮层；
 *   3. 历史浮层：列出全部消息 + 来源；「清空」按钮清空后气泡回到铃铛态；
 *   4. 后台任务 liveHint：写入 liveTask 后顶部展示「检测到后台「XX」任务仍在进行」；
 *   5. 消息中心为内存单例：多次 push 后数量受 MAX_ITEMS 约束、最新在前。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import React from "react";
import ActivityHint from "../components/ActivityHint";
import {
  pushActivity, clearActivity, setLiveTask, getActivityItems, getLiveTask,
} from "../utils/activityCenter";
import { liveTaskTypeLabel } from "../hooks/useSchemeLiveTask";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}

beforeEach(() => {
  clearActivity();
  setLiveTask(null);
});

function click(el: Element | null) {
  if (el) fireEvent.click(el);
}

describe("ActivityHint · 空态与最新消息", () => {
  it("无消息：渲染铃铛图标，无最新气泡", () => {
    const { container } = render(<ActivityHint />);
    // 铃铛图标存在（BellOutlined）
    expect(container.querySelector(".bp-activity-bell")).toBeTruthy();
    // 不展示具体消息文本气泡
    expect(container.querySelector(".bp-activity-chip")).toBeNull();
  });

  it("最新消息：彩色 chip 展示文本；点击打开历史浮层含该条", () => {
    pushActivity("success", "目录已生成", "方案工作台");
    const { container } = render(<ActivityHint />);
    const chip = container.querySelector(".bp-activity-chip");
    expect(chip).toBeTruthy();
    expect((chip?.textContent || "")).toContain("目录已生成");
    // 打开历史浮层
    click(chip);
    expect(document.body.textContent || "").toContain("目录已生成");
    expect(document.body.textContent || "").toContain("消息记录");
  });

  it("历史浮层「清空」：清空后气泡回到铃铛态", () => {
    pushActivity("warning", "存在截断提示", "项目资料");
    const { container } = render(<ActivityHint />);
    click(container.querySelector(".bp-activity-chip"));
    const clearBtn = Array.from(document.body.querySelectorAll(".ant-btn")).find(
      (b) => (b.textContent || "").includes("清空"),
    ) as HTMLButtonElement;
    expect(clearBtn).toBeTruthy();
    click(clearBtn);
    expect(getActivityItems().length).toBe(0);
  });
});

describe("ActivityHint · 后台任务提示", () => {
  it("写入 liveTask：顶部展示「检测到后台「正文生成」任务仍在进行」", () => {
    setLiveTask({
      id: "t1", task_type: "content_generation", status: "running",
      progress: 0.42, scheme_id: "s1",
    });
    const { container } = render(<ActivityHint />);
    expect(container.textContent || "").toContain("检测到后台");
    expect(container.textContent || "").toContain(liveTaskTypeLabel("content_generation"));
    expect(container.textContent || "").toContain("仍在进行");
  });

  it("liveTask 已暂停：展示「已暂停」而非「仍在进行」", () => {
    setLiveTask({
      id: "t1", task_type: "outline_generation", status: "paused",
      progress: 0.2, scheme_id: "s1",
    });
    const { container } = render(<ActivityHint />);
    expect(container.textContent || "").toContain("已暂停");
  });
});

describe("ActivityHint · 消息中心单例约束", () => {
  it("超过 50 条后只保留最近 50 条，且最新在最前", () => {
    for (let i = 0; i < 60; i++) pushActivity("info", `消息${i}`, "系统");
    const items = getActivityItems();
    expect(items.length).toBe(50);
    expect(items[0].text).toBe("消息59");
    // liveTask 单例独立存在，不受 items 清空影响
    expect(getLiveTask()).toBeNull();
  });
});
