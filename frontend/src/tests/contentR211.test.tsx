// @vitest-environment jsdom
/**
 * R211（2026-11-10）正文生成模块 · 前端护栏
 *
 * 锁住 R211 落地的 3 项前端修复：
 *
 *  FIX-1 P1  「未闭合围栏吞正文」告警（后端 `issues[].code=fence_swallowed_prose`）
 *            在日志 Popover 里渲染为**空白标签** —— 前端 `issueLabel` 只读 `type`
 *            键，而后端这条告警用的是 `code` 键 ⇒ R179 FIX-3 / R181 两轮修复的
 *            用户可见效果被前端断链吃掉。
 *  FIX-2 P2  mermaid 安全闸的 `on\w+\s*=` 缺**左边界** ⇒ 合法工程图标签
 *            `Connection=` / `Condition=` 被判危险而拒渲染，且 AI 修复回填
 *            再过同一闸 ⇒ **合法标签变成死路**。
 *  FIX-3 P2  章节编辑「放弃」后 localStorage 草稿残留 ⇒ 下次进编辑仍弹
 *            「发现未保存的草稿」（用户已明确放弃却要反复处理同一份草稿）。
 *
 * 另含 1 条**组件级补面**（P2-3）：正文 Tab 的「重连中」态此前零 DOM 断言。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup, waitFor, act } from "@testing-library/react";
import React from "react";
import { App } from "antd";

import { issueLabel, STANDARD_ISSUE_LABELS } from "../utils/contentStandard";
import { assertMermaidCodeSafe } from "../components/mermaidRuntime";
import SectionContentCard from "../components/SectionContentCard";

afterEach(cleanup);

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

// ===========================================================================
// FIX-1 · 围栏吞正文告警不得渲染空白标签
// ===========================================================================
describe("issueLabel · 后端 code 键的围栏吞正文告警", () => {
  it("code 键 → 中文标签（不得为空串）", () => {
    const label = issueLabel({ code: "fence_swallowed_prose" });
    expect(label).toBeTruthy();
    expect(label).not.toBe("[object Object]");
    expect(label).toContain("围栏");
    expect(STANDARD_ISSUE_LABELS["fence_swallowed_prose"]).toBeTruthy();
  });

  it("既有 type 键口径逐字不变", () => {
    expect(issueLabel({ type: "fuzzy_expression" })).toBe("模糊表述");
    expect(issueLabel({ type: "value_mismatch" })).toBe("数值不一致");
    expect(issueLabel({ type: "model_conflict" })).toBe("型号与事实冲突");
  });

  it("未知码原样回退（不空白）", () => {
    expect(issueLabel({ type: "some_new_code" })).toBe("some_new_code");
    expect(issueLabel({ code: "some_new_code" })).toBe("some_new_code");
    expect(issueLabel(null)).toBe("");
  });

  it("code 优先于 type（后端两条路径同源）", () => {
    expect(issueLabel({ code: "fence_swallowed_prose", type: "fuzzy_expression" })).toContain("围栏");
  });
});

// ===========================================================================
// FIX-2 · mermaid 安全闸：合法标签放行 / 真事件仍拒
// ===========================================================================
describe("assertMermaidCodeSafe · 事件属性左边界", () => {
  it("含 Connection= / Condition= 的合法工程图标签必须放行", () => {
    expect(assertMermaidCodeSafe("graph TD\nA[Connection=OK] --> B").safe).toBe(true);
    expect(assertMermaidCodeSafe("graph TD\nA[Condition=True] --> B").safe).toBe(true);
    expect(assertMermaidCodeSafe("graph TD\nA[Monitoring=On] --> B").safe).toBe(true);
    expect(assertMermaidCodeSafe("graph TD\nA[Consumption=1.2] --> B").safe).toBe(true);
  });

  it("真正的内联事件属性仍必须拒绝", () => {
    expect(assertMermaidCodeSafe('graph TD\nA["<img src=x onerror=alert(1)>"]').safe).toBe(false);
    expect(assertMermaidCodeSafe('graph TD\nA["<b onclick=alert(1)>x</b>"]').safe).toBe(false);
    expect(assertMermaidCodeSafe("graph TD\nA[onload=alert(1)]").safe).toBe(false);
  });

  it("拒绝原因文案须点名命中的事件特征", () => {
    const g = assertMermaidCodeSafe('graph TD\nA["<b onclick=alert(1)>x</b>"]');
    expect(g.reason).toContain("onclick");
  });
});

// ===========================================================================
// FIX-3 · 放弃编辑必须清除草稿
// ===========================================================================
describe("SectionContentCard · 放弃编辑后草稿必须清除", () => {
  function setup(section: any, over: Record<string, any> = {}) {
    const handlers = {
      onEditStart: vi.fn(),
      onSave: vi.fn(),
      onCancel: vi.fn(),
      onDirtyChange: vi.fn(),
      onRegenerate: vi.fn(),
      onChartFixed: vi.fn(),
    };
    const utils = render(
      <App>
        <SectionContentCard
          section={section}
          treeEmpty={false}
          isEditing={false}
          saving={false}
          generating={false}
          draftKey="draft:s1"
          statusColor={{ generated: "green", empty: "default" }}
          statusText={{ generated: "已生成", empty: "未生成" }}
          {...handlers}
          {...over}
        />
      </App>,
    );
    return { ...utils, ...handlers };
  }

  function btnByText(container: HTMLElement, text: string): HTMLButtonElement | null {
    const norm = (s: string) => (s || "").replace(/\s+/g, "");
    return (
      (Array.from(container.querySelectorAll("button")).find((b) =>
        norm(b.textContent).includes(norm(text)),
      ) as HTMLButtonElement | undefined) ?? null
    );
  }

  /**
   * ⚠️ antd 会给**两个汉字**的按钮文案中间插空格（「取 消」「放 弃」），
   *    因此查找弹窗按钮必须先去掉所有空白，否则永远找不到。
   */
  function modalBtnByText(text: string): HTMLButtonElement | null {
    const norm = (s: string) => (s || "").replace(/\s+/g, "");
    return (
      (Array.from(document.body.querySelectorAll(".ant-btn")).find((b) =>
        norm(b.textContent).includes(norm(text)),
      ) as HTMLButtonElement | undefined) ?? null
    );
  }

  it("dirty 取消并确认放弃 → localStorage 草稿被清除", async () => {
    localStorage.clear();
    const { container } = setup(
      { key: "s1", title: "第一章", level: 1, status: "generated", word_count: 10, content: "正文" },
      { isEditing: true },
    );
    const ta = container.querySelector("textarea")!;
    fireEvent.change(ta, { target: { value: "改了点东西" } });
    // 草稿防抖写入（800ms）
    await waitFor(
      () => {
        expect(localStorage.getItem("draft:s1")).toBeTruthy();
      },
      { timeout: 2000 },
    );

    fireEvent.click(btnByText(container, "取消")!);
    const ok = modalBtnByText("放弃");
    expect(ok).toBeTruthy();
    await act(async () => {
      fireEvent.click(ok as HTMLButtonElement);
    });
    expect(localStorage.getItem("draft:s1")).toBeNull();
  });

  it("放弃后再次进入编辑：不得再弹「发现未保存的草稿」", async () => {
    localStorage.clear();
    const { container, unmount } = setup(
      { key: "s1", title: "第一章", level: 1, status: "generated", word_count: 10, content: "正文" },
      { isEditing: true },
    );
    const ta = container.querySelector("textarea")!;
    fireEvent.change(ta, { target: { value: "改了点东西" } });
    await waitFor(
      () => {
        expect(localStorage.getItem("draft:s1")).toBeTruthy();
      },
      { timeout: 2000 },
    );
    fireEvent.click(btnByText(container, "取消")!);
    const ok = modalBtnByText("放弃");
    expect(ok).toBeTruthy();
    await act(async () => {
      fireEvent.click(ok as HTMLButtonElement);
    });
    unmount();

    // 重新挂载（视图态）→ 点「手动编辑」不应再有草稿恢复弹窗
    const again = setup({
      key: "s1",
      title: "第一章",
      level: 1,
      status: "generated",
      word_count: 10,
      content: "正文",
    });
    fireEvent.click(btnByText(again.container, "手动编辑")!);
    await waitFor(() => {
      const prompts = Array.from(document.body.querySelectorAll(".ant-modal-confirm-title")).map(
        (n) => n.textContent || "",
      );
      expect(prompts.some((t) => t.includes("发现未保存的草稿"))).toBe(false);
    });
  });
});
