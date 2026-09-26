// @vitest-environment jsdom
/**
 * 「正文生成」Tab · 组件级交互测试（2026-09-20 补齐 F5）。
 *
 * content Tab 主体此前内联在 8000 行工作台页面里、零组件级测试。
 * 现已抽为受控组件 ContentGenerationTab（只渲染 + 回调上抛），本文件锁定：
 *   1. 主操作按钮渲染与 disabled/loading 口径；
 *   2. 暂停 / 恢复按钮互斥 + 「已暂停」Tag（F3）；
 *   3. 「下一步：审核与预检」按钮上抛 onNextStep（F2）；
 *   4. 字数自定义态、并发/一致性/自动压缩开关回调；
 *   5. children 透传（SectionContentCard 由页面注入）。
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, fireEvent, cleanup, waitFor } from "@testing-library/react";
import React from "react";
import ContentGenerationTab, {
  type ContentGenerationTabProps,
} from "../components/ContentGenerationTab";

afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Select）会用到
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

function baseProps(over: Partial<ContentGenerationTabProps> = {}): ContentGenerationTabProps {
  return {
    generating: false,
    running: false,
    taskPaused: false,
    shrinking: false,
    hasTree: true,
    hasSelectedSection: true,
    canShrink: true,
    wordBudgetOption: "default",
    customWordBudget: 2000,
    concurrencyOption: "balanced",
    autoConsistencyRepair: true,
    consistencySeverity: "high",
    autoShrinkOver: false,
    crSummary: null,
    onGenerateAll: vi.fn(),
    onGenerateCurrent: vi.fn(),
    onGenerateMissing: vi.fn(),
    onContinueSection: vi.fn(),
    onShrinkSection: vi.fn(),
    onReset: vi.fn(),
    canReset: true,
    onControl: vi.fn(),
    onWordBudgetChange: vi.fn(),
    onCustomWordBudgetChange: vi.fn(),
    onConcurrencyChange: vi.fn(),
    onAutoConsistencyChange: vi.fn(),
    onSeverityChange: vi.fn(),
    onOpenConsistencyWorkbench: vi.fn(),
    onAutoShrinkChange: vi.fn(),
    onNextStep: vi.fn(),
    ...over,
  } as ContentGenerationTabProps;
}

/** 通过可见文本拿到对应 <button> */
function btnByText(utils: ReturnType<typeof render>, text: string): HTMLButtonElement {
  const el = utils.getByText(text);
  const b = el.closest("button") || (el as HTMLButtonElement);
  return b as HTMLButtonElement;
}

describe("ContentGenerationTab · 主操作按钮", () => {
  it("渲染 5 个主操作按钮 + 下一步按钮", () => {
    const utils = render(<ContentGenerationTab {...baseProps()} />);
    expect(btnByText(utils, "一键生成全文")).toBeTruthy();
    expect(btnByText(utils, "生成当前章节")).toBeTruthy();
    expect(btnByText(utils, "补全生成")).toBeTruthy();
    expect(btnByText(utils, "续写本章")).toBeTruthy();
    expect(btnByText(utils, "压缩本章")).toBeTruthy();
    expect(btnByText(utils, "下一步：审核与预检")).toBeTruthy();
  });

  it("generating=true 时主按钮禁用、下一步禁用", () => {
    const utils = render(<ContentGenerationTab {...baseProps({ generating: true, running: true })} />);
    expect(btnByText(utils, "一键生成全文").disabled).toBe(true);
    expect(btnByText(utils, "下一步：审核与预检").disabled).toBe(true);
  });

  it("点击一键生成全文上抛 onGenerateAll", () => {
    const p = baseProps();
    const utils = render(<ContentGenerationTab {...p} />);
    fireEvent.click(btnByText(utils, "一键生成全文"));
    expect(p.onGenerateAll).toHaveBeenCalledTimes(1);
  });

  it("未选章节时生成当前章节禁用；选中且可压缩时压缩本章可用并上抛", () => {
    const noSel = baseProps({ hasSelectedSection: false, canShrink: false });
    const u1 = render(<ContentGenerationTab {...noSel} />);
    expect(btnByText(u1, "生成当前章节").disabled).toBe(true);
    expect(btnByText(u1, "压缩本章").disabled).toBe(true);
    cleanup();

    const canDo = baseProps();
    const u2 = render(<ContentGenerationTab {...canDo} />);
    fireEvent.click(btnByText(u2, "压缩本章"));
    expect(canDo.onShrinkSection).toHaveBeenCalledTimes(1);
  });

  it("重置正文：无已生成正文或生成中禁用；可用时点击上抛 onReset", () => {
    const noGen = baseProps({ canReset: false });
    const u1 = render(<ContentGenerationTab {...noGen} />);
    expect(btnByText(u1, "重置正文").disabled).toBe(true);
    cleanup();

    const gen = baseProps({ canReset: true, generating: true });
    const u2 = render(<ContentGenerationTab {...gen} />);
    expect(btnByText(u2, "重置正文").disabled).toBe(true);
    cleanup();

    const canDo = baseProps({ canReset: true });
    const u3 = render(<ContentGenerationTab {...canDo} />);
    expect(btnByText(u3, "重置正文").disabled).toBe(false);
    fireEvent.click(btnByText(u3, "重置正文"));
    expect(canDo.onReset).toHaveBeenCalledTimes(1);
  });
});

describe("ContentGenerationTab · 暂停/恢复互斥与状态可见（F3）", () => {
  it("非运行态不渲染暂停/恢复/停止按钮", () => {
    const utils = render(<ContentGenerationTab {...baseProps({ running: false })} />);
    expect(utils.queryByText("暂停")).toBeNull();
    expect(utils.queryByText("恢复")).toBeNull();
  });

  it("运行且未暂停：暂停可点、恢复禁用、无「已暂停」Tag", () => {
    const p = baseProps({ running: true });
    const utils = render(<ContentGenerationTab {...p} />);
    expect(btnByText(utils, "暂停").disabled).toBe(false);
    expect(btnByText(utils, "恢复").disabled).toBe(true);
    expect(utils.queryByText(/已暂停/)).toBeNull();
    fireEvent.click(btnByText(utils, "暂停"));
    expect(p.onControl).toHaveBeenCalledWith("pause");
  });

  it("运行且已暂停：暂停禁用、恢复可点、显示「已暂停」Tag", () => {
    const p = baseProps({ running: true, taskPaused: true });
    const utils = render(<ContentGenerationTab {...p} />);
    expect(btnByText(utils, "暂停").disabled).toBe(true);
    expect(btnByText(utils, "恢复").disabled).toBe(false);
    expect(utils.getByText(/已暂停/)).toBeTruthy();
    fireEvent.click(btnByText(utils, "恢复"));
    expect(p.onControl).toHaveBeenCalledWith("resume");
  });

  it("停止按钮上抛 onControl('stop')", () => {
    const p = baseProps({ running: true });
    const utils = render(<ContentGenerationTab {...p} />);
    fireEvent.click(btnByText(utils, "停止"));
    expect(p.onControl).toHaveBeenCalledWith("stop");
  });
});

describe("ContentGenerationTab · 导航与设置", () => {
  it("点击下一步上抛 onNextStep（接线 NEXT_TAB.content）", () => {
    const p = baseProps();
    const utils = render(<ContentGenerationTab {...p} />);
    fireEvent.click(btnByText(utils, "下一步：审核与预检"));
    expect(p.onNextStep).toHaveBeenCalledTimes(1);
  });

  it("字数自定义态才显示「字 / 章节」输入单位", () => {
    const u1 = render(<ContentGenerationTab {...baseProps({ wordBudgetOption: "default" })} />);
    expect(u1.queryByText("字 / 章节")).toBeNull();
    cleanup();
    const u2 = render(<ContentGenerationTab {...baseProps({ wordBudgetOption: "custom" })} />);
    expect(u2.getByText("字 / 章节")).toBeTruthy();
  });

  it("两个开关分别上抛一致性 / 自动压缩变更", () => {
    const p = baseProps();
    const utils = render(<ContentGenerationTab {...p} />);
    const switches = Array.from(utils.container.querySelectorAll(".ant-switch"));
    expect(switches.length).toBe(2); // 一致性修复 + 超字数自动压缩
    fireEvent.click(switches[1]); // 自动压缩开关
    expect(p.onAutoShrinkChange).toHaveBeenCalledTimes(1);
  });

  it("一致性修复关闭时修复等级选择器禁用", () => {
    const utils = render(
      <ContentGenerationTab {...baseProps({ autoConsistencyRepair: false })} />,
    );
    const disabledSelect = utils.container.querySelector(
      ".ant-select-disabled",
    );
    expect(disabledSelect).toBeTruthy();
  });

  it("最近一致性冲突摘要（total>0）以 Tag 展示", () => {
    const utils = render(
      <ContentGenerationTab {...baseProps({ crSummary: { total: 4, repaired: 2 } })} />,
    );
    expect(utils.getByText(/最近发现 4 处冲突/)).toBeTruthy();
    expect(utils.getByText(/已修复 2/)).toBeTruthy();
  });

  it("children 透传渲染（页面注入 SectionContentCard）", () => {
    const utils = render(
      <ContentGenerationTab {...baseProps()}>
        <div data-testid="section-card">章节内容卡</div>
      </ContentGenerationTab>,
    );
    expect(utils.getByTestId("section-card")).toBeTruthy();
  });
});
  // ------------------------------------------------------------------
  // 2026-09-20 补齐：正文生成 Tab 组件级交互测试补全
  //   此前 5 个主按钮只测了 2 个（一键生成全文 / 生成当前章节），
  //   字数/并发/自定义字数/修复工作台/摘要缺省分支均无覆盖。
  // ------------------------------------------------------------------
  describe("ContentGenerationTab · 主操作按钮（补全）", () => {
    it("点击补全生成上抛 onGenerateMissing", () => {
      const p = baseProps();
      const utils = render(<ContentGenerationTab {...p} />);
      fireEvent.click(btnByText(utils, "补全生成"));
      expect(p.onGenerateMissing).toHaveBeenCalledTimes(1);
    });

    it("点击续写本章上抛 onContinueSection", () => {
      const p = baseProps();
      const utils = render(<ContentGenerationTab {...p} />);
      fireEvent.click(btnByText(utils, "续写本章"));
      expect(p.onContinueSection).toHaveBeenCalledTimes(1);
    });

    it("未选中章节时「生成当前章节 / 续写本章」禁用，「补全生成」不受选中态约束", () => {
      const utils = render(
        <ContentGenerationTab {...baseProps({ hasSelectedSection: false })} />,
      );
      expect(btnByText(utils, "生成当前章节").disabled).toBe(true);
      expect(btnByText(utils, "续写本章").disabled).toBe(true);
      // 「补全生成」生成的是全部空章节，不依赖当前选中项（组件口径：
      // hasSelectedSection 只作用于「生成当前章节 / 续写本章」两个按钮）
      expect(btnByText(utils, "补全生成").disabled).toBe(false);
      // 「一键生成全文」同样不依赖章节选中，仍然可点
      expect(btnByText(utils, "一键生成全文").disabled).toBe(false);
    });

    it("hasTree 控制右下提示文案：有目录树显示引导，无目录树不渲染", () => {
      // 组件口径：提示是普通 Text（非 Tooltip），随 hasTree 条件渲染
      const u1 = render(<ContentGenerationTab {...baseProps({ hasTree: true })} />);
      expect(u1.getByText(/左侧点击任意章节可查看\/编辑已生成内容/)).toBeTruthy();
      cleanup();
      const u2 = render(<ContentGenerationTab {...baseProps({ hasTree: false })} />);
      expect(u2.queryByText(/左侧点击任意章节可查看\/编辑已生成内容/)).toBeNull();
    });

    it("字数非默认档时显示「按二级章节总字数控制」说明 Tag", () => {
      const u1 = render(
        <ContentGenerationTab {...baseProps({ wordBudgetOption: "default" })} />,
      );
      expect(u1.queryByText(/按二级章节总字数控制/)).toBeNull();
      cleanup();
      const u2 = render(
        <ContentGenerationTab {...baseProps({ wordBudgetOption: "2000" })} />,
      );
      expect(u2.getByText(/按二级章节总字数控制/)).toBeTruthy();
    });
  });

  describe("ContentGenerationTab · 压缩本章的三重闸门", () => {
    it("shrinking=true 时「压缩本章」禁用并 loading", () => {
      const p = baseProps({ shrinking: true });
      const utils = render(<ContentGenerationTab {...p} />);
      const b = btnByText(utils, "压缩本章");
      expect(b.disabled).toBe(true);
      expect(b.className).toContain("ant-btn-loading");
    });

    it("有任意生成任务在跑时「压缩本章」禁用（防止与生成并发写同一章节）", () => {
      const utils = render(<ContentGenerationTab {...baseProps({ generating: true })} />);
      expect(btnByText(utils, "压缩本章").disabled).toBe(true);
    });

    it("canShrink=false 时「压缩本章」禁用且不响应点击", () => {
      const p = baseProps({ canShrink: false });
      const utils = render(<ContentGenerationTab {...p} />);
      const b = btnByText(utils, "压缩本章");
      expect(b.disabled).toBe(true);
      fireEvent.click(b);
      expect(p.onShrinkSection).not.toHaveBeenCalled();
    });
  });

  describe("ContentGenerationTab · 生成参数设置回调", () => {
    it("切换并发档位上抛 onConcurrencyChange", () => {
      const p = baseProps({ concurrencyOption: "balanced" });
      const utils = render(<ContentGenerationTab {...p} />);
      fireEvent.click(utils.getByText("🚀 快速（并发 5）"));
      expect(p.onConcurrencyChange).toHaveBeenCalledWith("fast");
    });

    it("自定义字数输入变更上抛 onCustomWordBudgetChange", () => {
      const p = baseProps({ wordBudgetOption: "custom", customWordBudget: 2000 });
      const utils = render(<ContentGenerationTab {...p} />);
      const input = utils.container.querySelector(
        ".ant-input-number-input",
      ) as HTMLInputElement;
      expect(input).toBeTruthy();
      fireEvent.change(input, { target: { value: "3000" } });
      expect(p.onCustomWordBudgetChange).toHaveBeenCalledWith(3000);
    });

    it("字数档位选择器渲染 6 个预设项 + 自定义项", async () => {
      const utils = render(<ContentGenerationTab {...baseProps()} />);
      const select = utils.container.querySelector(".ant-select") as HTMLElement;
      expect(select).toBeTruthy();
      // 打开下拉（rc-select 的选项渲染在 document.body 的 portal 里，
      // 不在 utils.container 内，故必须查 document）
      fireEvent.mouseDown(select.querySelector(".ant-select-selector")!);
      await waitFor(() => {
        expect(
          Array.from(document.querySelectorAll(".ant-select-item-option")).length,
        ).toBe(6);
      });
      const labels = Array.from(
        document.querySelectorAll(".ant-select-item-option-content"),
      ).map((i) => (i.textContent || "").trim());
      expect(labels).toEqual([
        "使用目录默认值",
        "1000 字",
        "1500 字",
        "2000 字",
        "3000 字",
        "自定义",
      ]);
    });
  });

  describe("ContentGenerationTab · 全文一致性 Agent 修复（补全）", () => {
    it("点击「打开修复工作台」上抛 onOpenConsistencyWorkbench", () => {
      const p = baseProps();
      const utils = render(<ContentGenerationTab {...p} />);
      fireEvent.click(btnByText(utils, "打开修复工作台"));
      expect(p.onOpenConsistencyWorkbench).toHaveBeenCalledTimes(1);
    });

    it("一致性修复开关（第一个）上抛 onAutoConsistencyChange", () => {
      const p = baseProps({ autoConsistencyRepair: false });
      const utils = render(<ContentGenerationTab {...p} />);
      const switches = Array.from(utils.container.querySelectorAll(".ant-switch"));
      expect(switches.length).toBe(2);
      fireEvent.click(switches[0]);
      expect(p.onAutoConsistencyChange).toHaveBeenCalledTimes(1);
      // 自动压缩开关不受影响
      expect(p.onAutoShrinkChange).not.toHaveBeenCalled();
    });

    it("修复等级选择器渲染 3 个档位且当前选中值可见", async () => {
      const utils = render(
        <ContentGenerationTab {...baseProps({ consistencySeverity: "medium" })} />,
      );
      // 当前值直接渲染在触发器里（Select 收起态显示选中项文本）
      expect(utils.getByText("中及以上")).toBeTruthy();
      const severitySelect = Array.from(
        utils.container.querySelectorAll(".ant-select"),
      ).find((s) => (s.textContent || "").includes("中及以上")) as HTMLElement;
      expect(severitySelect).toBeTruthy();
      fireEvent.mouseDown(severitySelect.querySelector(".ant-select-selector")!);
      await waitFor(() => {
        expect(
          Array.from(document.querySelectorAll(".ant-select-item-option")).length,
        ).toBe(3);
      });
      const labels = Array.from(
        document.querySelectorAll(".ant-select-item-option-content"),
      ).map((i) => (i.textContent || "").trim());
      expect(labels).toContain("仅高危（推荐）");
      expect(labels).toContain("中及以上");
      expect(labels).toContain("全部（含低危）");
    });

    it("crSummary 存在但 total=0 时不显示冲突 Tag（0 处不算发现）", () => {
      const utils = render(
        <ContentGenerationTab {...baseProps({ crSummary: { total: 0, repaired: 0 } })} />,
      );
      expect(utils.queryByText(/最近发现/)).toBeNull();
    });

    it("crSummary 缺 repaired 字段时只显示发现数，不显示「已修复」", () => {
      const utils = render(
        <ContentGenerationTab {...baseProps({ crSummary: { total: 3 } })} />,
      );
      expect(utils.getByText(/最近发现 3 处冲突/)).toBeTruthy();
      expect(utils.queryByText(/已修复/)).toBeNull();
    });
  });

