// @vitest-environment jsdom
/**
 * R106 · 停止按钮全链路收口 —— 组件级交互测试。
 *
 * 锁定本次修复的五条前端契约：
 *   1. TaskControlButtons（目录 / 事实两 Tab 新增的停止入口）：
 *      暂停/恢复互斥、停止上抛、stopping 时三点全禁用且文案「停止中」；
 *   2. FactsExtractProgressCard 的 controls 插槽：不传即不渲染（旧用例口径不变）、
 *      传入则渲染在进度条之后；
 *   3. ContentGenerationTab 的 taskStopping：停止按钮置灰显示「停止中」，
 *      暂停/恢复禁用、「已暂停」Tag 隐去（避免停止中仍显示暂停态）；
 *   4. BidAnalysisTab 的 stopping：同上，且点击不再触发 onStop（防连点重复发指令）。
 *   5. FIX-8（源码接线锁）：三条生成链发起时先清空陈旧 activeTaskIdRef，
 *      handleControl 在"刚发起、task_id 还没回"窗口内如实回执、绝不误发指令。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import {
  TaskControlButtons, FactsExtractProgressCard,
} from "../pages/SchemeWorkbenchPage";
import ContentGenerationTab, {
  type ContentGenerationTabProps,
} from "../components/ContentGenerationTab";
import BidAnalysisTab from "../components/BidAnalysisTab";
import type { BaItemDef } from "../utils/bidAnalysis";

afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Progress）会用到
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

/** 通过可见文本拿到对应 <button> */
function btnByText(utils: ReturnType<typeof render>, text: string): HTMLButtonElement {
  const el = utils.getByText(text);
  const b = el.closest("button") || (el as HTMLButtonElement);
  return b as HTMLButtonElement;
}

describe("TaskControlButtons · 目录/事实 Tab 停止入口（R106 FIX-2）", () => {
  it("渲染暂停/恢复/停止三键；未暂停时恢复禁用、无「已暂停」Tag", () => {
    const onControl = vi.fn();
    const utils = render(<TaskControlButtons paused={false} stopping={false} onControl={onControl} />);
    expect(btnByText(utils, "暂停").disabled).toBe(false);
    expect(btnByText(utils, "恢复").disabled).toBe(true);
    expect(btnByText(utils, "停止").disabled).toBe(false);
    expect(utils.queryByText(/已暂停/)).toBeNull();
  });

  it("已暂停：暂停禁用、恢复可点、显示「已暂停」Tag", () => {
    const utils = render(<TaskControlButtons paused stopping={false} onControl={vi.fn()} />);
    expect(btnByText(utils, "暂停").disabled).toBe(true);
    expect(btnByText(utils, "恢复").disabled).toBe(false);
    expect(utils.queryByText(/已暂停/)).toBeTruthy();
  });

  it("点击上抛对应 action", () => {
    const onControl = vi.fn();
    const utils = render(<TaskControlButtons paused={false} stopping={false} onControl={onControl} />);
    fireEvent.click(btnByText(utils, "暂停"));
    fireEvent.click(btnByText(utils, "停止"));
    expect(onControl).toHaveBeenNthCalledWith(1, "pause");
    expect(onControl).toHaveBeenNthCalledWith(2, "stop");
  });

  it("停止中：文案「停止中」+ 三点全禁用（防连点、防 stop/resume 竞态）", () => {
    const onControl = vi.fn();
    const utils = render(<TaskControlButtons paused stopping onControl={onControl} />);
    const stop = btnByText(utils, "停止中");
    expect(stop.disabled).toBe(true);
    expect(btnByText(utils, "暂停").disabled).toBe(true);
    expect(btnByText(utils, "恢复").disabled).toBe(true);
    // 停止中不再显示「已暂停」，避免与「正在停止」两套语义同时挂出
    expect(utils.queryByText(/已暂停/)).toBeNull();
    fireEvent.click(stop);
    expect(onControl).not.toHaveBeenCalled();
  });

  it("停止中（未暂停）：暂停键也必须禁用 —— 禁用语义来自 stopping 而非 paused", () => {
    // A/B F-M2 口径：把 `disabled={paused || stopping}` 摘成 `disabled={paused}`
    // 时，上一用例（paused=true）两键本来就禁用、抓不到；此处必须 paused=false。
    const utils = render(
      <TaskControlButtons paused={false} stopping onControl={vi.fn()} />,
    );
    expect(btnByText(utils, "暂停").disabled).toBe(true);
    expect(btnByText(utils, "恢复").disabled).toBe(true);
    expect(btnByText(utils, "停止中").disabled).toBe(true);
  });
});

describe("FactsExtractProgressCard · controls 插槽（R106 FIX-2）", () => {
  const logs = [{ progress: 0.4, message: "正在提取第 2 段" }];

  it("不传 controls 时不渲染控制区（旧用例口径不变）", () => {
    const utils = render(
      <FactsExtractProgressCard progress={0.4} progressMsg="提取中" logs={logs} />,
    );
    expect(utils.queryByText("停止")).toBeNull();
    expect(utils.queryByText("暂停")).toBeNull();
  });

  it("传入 controls 时渲染在卡片内", () => {
    const onControl = vi.fn();
    const utils = render(
      <FactsExtractProgressCard
        progress={0.4}
        progressMsg="提取中"
        logs={logs}
        controls={<TaskControlButtons paused={false} stopping={false} onControl={onControl} />}
      />,
    );
    fireEvent.click(btnByText(utils, "停止"));
    expect(onControl).toHaveBeenCalledWith("stop");
  });
});

function baseContentProps(over: Partial<ContentGenerationTabProps> = {}): ContentGenerationTabProps {
  return {
    generating: true,
    running: true,
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

describe("ContentGenerationTab · 停止中态（R106 FIX-4）", () => {
  it("taskStopping=true：停止按钮显示「停止中」且禁用，暂停/恢复禁用", () => {
    const p = baseContentProps({ taskStopping: true });
    const utils = render(<ContentGenerationTab {...p} />);
    const stop = btnByText(utils, "停止中");
    expect(stop.disabled).toBe(true);
    expect(btnByText(utils, "暂停").disabled).toBe(true);
    expect(btnByText(utils, "恢复").disabled).toBe(true);
    fireEvent.click(stop);
    expect(p.onControl).not.toHaveBeenCalled();
  });

  it("taskStopping 未传（旧调用方）时行为不变：停止可点并上抛 stop", () => {
    const p = baseContentProps();
    const utils = render(<ContentGenerationTab {...p} />);
    const stop = btnByText(utils, "停止");
    expect(stop.disabled).toBe(false);
    fireEvent.click(stop);
    expect(p.onControl).toHaveBeenCalledWith("stop");
  });

  it("已暂停且同时停止中：不再显示「已暂停」Tag", () => {
    const utils = render(<ContentGenerationTab {...baseContentProps({ taskPaused: true, taskStopping: true })} />);
    expect(utils.queryByText(/已暂停/)).toBeNull();
  });
});

const BA_DEFS: BaItemDef[] = [
  { item_id: "projectBasicInfo", label: "项目级基本信息", required: 1, output_type: "json", group: "project_info" },
];
const BA_GROUPS = [{ group: "project_info", label: "项目级基本信息", items: [BA_DEFS[0]] }];

describe("BidAnalysisTab · 停止中态（R106 FIX-4）", () => {
  function renderBa(stopping?: boolean, onStop = vi.fn()) {
    const utils = render(
      <BidAnalysisTab
        defs={BA_DEFS}
        groups={BA_GROUPS}
        items={[]}
        summary={null}
        running
        progress={0.5}
        progressMsg="解析中"
        parsedDocCount={1}
        selectedItem={null}
        sectionChecking={false}
        sectionCheckResult={null}
        stopping={stopping}
        onStart={vi.fn()}
        onStop={onStop}
        onOpenConfig={vi.fn()}
        onCheckSections={vi.fn()}
        onRefresh={vi.fn()}
        onSelectItem={vi.fn()}
        onRerunItem={vi.fn()}
        onOpenFullView={vi.fn()}
        onEditItem={vi.fn()}
        onDismissSectionResult={vi.fn()}
        onGoImport={vi.fn()}
        onGoOutline={vi.fn()}
        onGoFacts={vi.fn()}
      />,
    );
    return { utils, onStop };
  }

  it("stopping=true：停止按钮置灰显示「停止中」，点击不再上抛", () => {
    const { utils, onStop } = renderBa(true);
    const stop = btnByText(utils, "停止中");
    expect(stop.disabled).toBe(true);
    fireEvent.click(stop);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("未停止时文案仍为「停止」且可点（旧口径）", () => {
    const { utils, onStop } = renderBa(undefined);
    fireEvent.click(btnByText(utils, "停止"));
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// R106 FIX-8 · 停止指令的 task_id 新鲜度（源码接线锁）
//
// RC-1 机理：三条生成链发起时都不清 `activeTaskIdRef`，而本代任务的 task_id 要等
// SSE 首个 connecting 事件才写进 ref。后端旧实现把 connecting 放在
// `_assemble_context()` 之后（实测 30s），期间 ref 仍是**上一代已终结**的 id ——
// 用户点停止打到死 id，后端回 `{ok:false,message:"任务不存在或已结束"}`，新任务
// 从未收到指令，表现为"停止按钮完全无效"（活体浏览器 E2E 复现）。
// 后端 FIX-7 把窗口压到 ~1s；前端 FIX-8 保证哪怕窗口只剩 1s 也只会得到诚实回执。
// 判据沿用本仓既有范式：`import.meta.glob(..., { query: "?raw" })` 读源码文本，
// 不引 node:fs（本项目没装 @types/node，tests 在 tsc 范围内）。
// ---------------------------------------------------------------------------
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);
const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

/** 从函数声明处向后取一段源码（下一个顶层 `const ` 声明之前，封顶 maxLen） */
function sliceFn(decl: string, maxLen = 4000): string {
  const i = PAGE.indexOf(decl);
  expect(i, `源码里找不到 ${decl}`).toBeGreaterThan(-1);
  return PAGE.slice(i, i + maxLen);
}

describe("SchemeWorkbenchPage · 发起生成即清陈旧 task_id（R106 FIX-8）", () => {
  const starts: Array<[string, string]> = [
    ["目录链", "const doGenerateOutline = async () => {"],
    ["正文链", "const handleGenerateContent = async ("],
    ["事实链", "const runGenerateFactsSse = async () => {"],
  ];

  it("三条链在 `++genSeqRef.current` 之后立刻清空 activeTaskIdRef", () => {
    // 非空转自证：判据本身要能命中（三链都确有世代自增语句）
    let probed = 0;
    for (const [name, decl] of starts) {
      const body = sliceFn(decl);
      const seq = body.indexOf("++genSeqRef.current");
      expect(seq, `${name} 未找到世代自增`).toBeGreaterThan(-1);
      probed += 1;
      const clear = body.indexOf('activeTaskIdRef.current = "";', seq);
      expect(clear, `${name} 发起生成后未清空上一代 task_id（停止会打到死 id）`).toBeGreaterThan(-1);
      // 清空必须发生在函数开头的"发请求前"区段（正文链体量大，用相对距离而非
      // 是否命中 sseFetch 来判——命中与否取决于函数体长度，会退化成空判据）
      expect(clear - seq, `${name} 清空位置离世代自增太远，可能在请求之后`).toBeLessThan(800);
    }
    expect(probed).toBe(starts.length);
  });

  it("地板：全页清空 activeTaskIdRef 的语句至少 3 处（防整体摘除）", () => {
    const n = PAGE.split('activeTaskIdRef.current = "";').length - 1;
    expect(n).toBeGreaterThanOrEqual(3);
  });

  it("handleControl：无 task_id 但仍在生成中时如实提示，绝不发指令", () => {
    const body = sliceFn("const handleControl = async (", 2000);
    const lockAt = body.indexOf("controllingRef.current = true;");
    expect(lockAt, "未找到 FIX-4 的防连点同步锁（判据静默失灵）").toBeGreaterThan(-1);
    const before = body.slice(0, lockAt);
    // 非空转自证：整页确有控制指令调用
    expect(PAGE.split("tasksApi.control").length - 1).toBeGreaterThanOrEqual(1);
    expect(before).toContain("if (!tid)");
    expect(before).toContain("generating");
    expect(before.indexOf("tasksApi.control"), "空 id 分支里不得真的发出控制指令").toBe(-1);
    expect(body.indexOf("tasksApi.control")).toBeGreaterThan(lockAt);
  });
});

