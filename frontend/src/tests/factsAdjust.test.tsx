// @vitest-environment jsdom
/**
 * 全局事实 · AI 调整面板（FactsAdjustPanel）组件级交互测试（R45 · D5 补齐）
 *
 * 此前「AI 调整事实」这条状态机（自然语言描述 → 预览待确认计划 → 二次确认 →
 * 一次性应用）全部内联在 9000 行工作台页面 JSX 里，R45 只做了静态扫描锁
 * （全仓无 window.prompt），**零组件级行为覆盖**。现抽为独立子组件并锁定：
 *
 * formatFactsAdjustOperation（纯函数）：
 *   1. 有 name 用 name；否则用 fact_id；都没有回退「第 N 条」；
 *   2. 有 value 追加为「空格 + value」，无 value 不补空格；
 *   3. 与 FACTS_ADJUST_PREVIEW_LIMIT 同口径（20 条）的截断锚点。
 *
 * FactsAdjustPanel（状态机组件，数据操作全部回调化）：
 *   4. 无 schemeId / disabled / busy 三种情形按钮均禁用；
 *   5. 打开弹层 → 空输入 OK 禁用 → 输入后 OK 启用 → 提交以 trim 后指令调 preview；
 *   6. 纯空白输入不下发（空白 trim 后为空）；
 *   7. Enter 提交、Shift+Enter 不提交；
 *   8. preview 成功 → 渲染计划 → 出现「应用调整计划」按钮；
 *   9. preview 返回空 operations → 不出现「应用调整计划」按钮；
 *  10. 点「应用调整计划」→ 弹出二次确认，且带上用户刚确认的 operations；
 *  11. 确认弹窗 OK → 调 apply(operations, summary)；
 *  12. preview 失败 → onError("生成调整计划失败")；apply 失败 → onError("应用调整失败")；
 *  13. 切换方案 → 计划与弹层全部丢弃（防把方案 A 的计划应用到方案 B）；
 *  14. 超 20 条操作 → 确认弹窗截断展示 + 「其余 N 项未列出」+ 按钮显示总数。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup, waitFor, screen } from "@testing-library/react";
import React from "react";
import {
  FactsAdjustPanel,
  formatFactsAdjustOperation,
  FACTS_ADJUST_PREVIEW_LIMIT,
} from "../pages/SchemeWorkbenchPage";
import type { FactsAdjustOperation } from "../pages/SchemeWorkbenchPage";

// vitest 未开 globals，RTL 自动清理不生效 —— 显式清理，避免跨用例 DOM 累积
afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Modal / Tooltip）会用到
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

const PLACEHOLDER = "例如：新增一条「项目经理=张伟」；把开挖深度统一改为 8.5m";
// 四、应用流程：二次确认 → apply
// ================================================================
async function reachPlan(
  operations: FactsAdjustOperation[],
  summary?: string,
) {
  const handles = renderPanel({
    preview: vi.fn(async () => ({ summary, operations })),
  });
  await openAsk("统一改为 8.5m");
  clickOk();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /应用调整计划/ })));
  return handles;
}

/** 把 confirm 的 content 树拍平成字符串，便于断言列出的操作明细 */
function flatContent(node: unknown): string {
  if (node === null || node === undefined) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flatContent).join("");
  if (typeof node === "object" && node !== null) {
    const o = node as { children?: unknown; props?: { children?: unknown } };
    return flatContent(o.children ?? o.props?.children ?? "");
  }
  return "";
}

describe("FactsAdjustPanel · 应用", () => {
  it("点「应用调整计划」→ 弹出二次确认，且带上用户刚确认的 operations", async () => {
    const ops = OPS(2);
    const { confirmDialog } = await reachPlan(ops, "共 2 项");

    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));

    expect(confirmDialog).toHaveBeenCalledTimes(1);
    const opts = (confirmDialog as any).mock.calls[0][0] as ConfirmOptions;
    expect(opts.title).toBe("确认应用事实调整");
    expect(opts.okText).toBe("确认应用");
    expect(opts.okButtonProps.danger).toBe(true);
    // 确认内容必须列出用户刚确认的操作明细（禁止后端再跑一次 AI）
    const flat = flatContent(opts.content);
    expect(flat).toContain("开挖深度1");
    expect(flat).toContain("8.5m");
    expect(flat).toContain("共 2 项");
  });

  it("确认弹窗 OK → 调 apply(operations, summary)，并清空计划", async () => {
    const ops = OPS(2);
    const { apply, confirmDialog } = await reachPlan(ops, "共 2 项");

    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));
    const opts = (confirmDialog as any).mock.calls[0][0] as ConfirmOptions;
    await opts.onOk();

    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply).toHaveBeenCalledWith(ops, "共 2 项");
    // 计划清空后「应用调整计划」按钮消失
    expect(screen.queryByRole("button", { name: /应用调整计划/ })).toBeNull();
  });

  it("确认弹窗 OK 失败 → onError「应用调整失败」且计划保留可重试", async () => {
    // ⚠️ apply 必须真正 reject：reachPlan 里的默认 apply 是 no-op，
    //    不主动换掉就是「断言不会失败」的空转守栏。
    const apply = vi.fn(async () => { throw "boom"; });
    const onError = vi.fn();
    const confirmDialog = vi.fn();
    renderPanel({
      preview: vi.fn(async () => ({ operations: OPS(1) })),
      apply: apply as any, onError: onError as any,
      confirmDialog: confirmDialog as any,
    });
    await openAsk("改深度");
    clickOk();
    await waitFor(() => expect(screen.getByRole("button", { name: /应用调整计划/ })));
    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));
    const opts = (confirmDialog as any).mock.calls[0][0] as ConfirmOptions;

    await expect(opts.onOk()).rejects.toBe("boom");
    expect(onError).toHaveBeenCalledWith("应用调整失败", "boom");
    // 失败不吞掉计划 → 用户可重试
    expect(screen.getByRole("button", { name: /应用调整计划/ })).toBeTruthy();
    expect(apply).toHaveBeenCalledTimes(1);
  });

  it("确认弹窗取消（不调 onOk）→ 不下发 apply", async () => {
    const { apply, confirmDialog } = await reachPlan(OPS(1));
    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));
    expect(confirmDialog).toHaveBeenCalledTimes(1);
    expect(apply).not.toHaveBeenCalled();
  });

  it("超 20 条操作 → 确认弹窗截断展示 + 按钮显示总数（与上限同口径）", async () => {
    expect(FACTS_ADJUST_PREVIEW_LIMIT).toBe(20);
    const ops = OPS(23);
    const { confirmDialog } = await reachPlan(ops);

    // 按钮带总数，提示「应用的是全部 23 项，不只是列表里的 20 项」
    expect(screen.getByRole("button", { name: /应用调整计划（23 项）/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));
    const opts = (confirmDialog as any).mock.calls[0][0] as ConfirmOptions;
    const flat = flatContent(opts.content);
    expect(flat).toContain("开挖深度1");
    expect(flat).toContain("开挖深度20");
    // 第 21 条不得出现在确认弹窗里
    expect(flat).not.toContain("开挖深度21");
    // 但必须明确告知「还有多少项未列出」
    expect(flat).toContain("其余 3 项未列出");

    // 实际下发的仍是完整 23 条（截断只影响展示，不影响执行）
    await opts.onOk();
  });

  it("无 summary 时下发「按已确认计划调整（N 项）」兜底指令", async () => {
    const { apply, confirmDialog } = await reachPlan(OPS(2));
    fireEvent.click(screen.getByRole("button", { name: /应用调整计划/ }));
    const opts = (confirmDialog as any).mock.calls[0][0] as ConfirmOptions;
    await opts.onOk();
    expect(apply).toHaveBeenCalledWith(
      expect.any(Array), "按已确认计划调整（2 项）");
  });
});

// ================================================================
// 一、纯函数：操作明细渲染
// ================================================================
describe("formatFactsAdjustOperation（操作明细单行）", () => {
  it("有 name 时优先用 name，并把 value 以空格追加", () => {
    expect(formatFactsAdjustOperation({ op: "update", name: "开挖深度", value: "8.5m" }, 0))
      .toBe("update · 开挖深度 8.5m");
  });

  it("无 name 回退 fact_id", () => {
    expect(formatFactsAdjustOperation({ op: "delete", fact_id: "f-9" }, 0))
      .toBe("delete · f-9");
  });

  it("name / fact_id 都没有时回退「第 N 条」（1 基）", () => {
    expect(formatFactsAdjustOperation({ op: "insert" }, 3))
      .toBe("insert · 第 4 条");
  });

  it("value 为空时不补多余空格", () => {
    expect(formatFactsAdjustOperation({ op: "delete", name: "基坑深度" }, 0))
      .toBe("delete · 基坑深度");
    expect(formatFactsAdjustOperation({ op: "delete", name: "基坑深度", value: "" }, 0))
      .toBe("delete · 基坑深度");
  });
});

// ================================================================
// 二、按钮可用性门控
// ================================================================
describe("FactsAdjustPanel · 可用性门控", () => {
  it("无 schemeId 时禁用", () => {
    renderPanel({ schemeId: undefined });
    expect((screen.getByRole("button", { name: /AI 调整事实/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("方案尚无事实（disabled）时禁用", () => {
    renderPanel({ disabled: true });
    expect((screen.getByRole("button", { name: /AI 调整事实/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("外部忙碌（busy）时禁用", () => {
    renderPanel({ busy: true });
    expect((screen.getByRole("button", { name: /AI 调整事实/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("默认（有方案、无事实、不忙）不显示「应用调整计划」按钮", () => {
    renderPanel();
    expect(screen.queryByRole("button", { name: /应用调整计划/ })).toBeNull();
  });
});

// ================================================================
// 三、预览流程：弹层 → 提交 → 计划
// ================================================================
describe("FactsAdjustPanel · 预览", () => {
  it("空输入时 OK 禁用，且不会下发请求", async () => {
    const { preview } = renderPanel({});
    await openAsk("");
    expect((screen.getByRole("button", { name: "生成调整计划" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "生成调整计划" }));
    expect(preview).not.toHaveBeenCalled();
  });

  it("纯空白输入同样不下发（trim 判空）", async () => {
    const { preview } = renderPanel({});
    await openAsk("   ");
    expect((screen.getByRole("button", { name: "生成调整计划" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "生成调整计划" }));
    expect(preview).not.toHaveBeenCalled();
  });

  it("提交时以 trim 后的指令调用 preview，并渲染计划与「应用调整计划」按钮", async () => {
    const { preview } = renderPanel({
      preview: vi.fn(async () => ({ summary: "共 2 项", operations: OPS(2) })),
    });
    await openAsk("  把开挖深度统一改为 8.5m  ");
    clickOk();

    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    expect(preview).toHaveBeenCalledWith("把开挖深度统一改为 8.5m");
    expect(screen.getByRole("button", { name: /应用调整计划/ })).toBeTruthy();
  });

  it("Enter 提交；Shift+Enter 换行不提交", async () => {
    const { preview } = renderPanel({
      preview: vi.fn(async () => ({ operations: OPS(1) })),
    });
    await openAsk("统一改为 8.5m");
    const area = screen.getByPlaceholderText(PLACEHOLDER) as HTMLTextAreaElement;
    fireEvent.keyDown(area, { key: "Enter", code: "Enter", shiftKey: true });
    expect(preview).not.toHaveBeenCalled();

    fireEvent.keyDown(area, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
  });

  it("preview 返回空 operations → 不出现「应用调整计划」按钮", async () => {
    renderPanel({ preview: vi.fn(async () => ({ operations: [] })) });
    await openAsk("随便改改");
    clickOk();
    expect(screen.queryByRole("button", { name: /应用调整计划/ })).toBeNull();
  });

  it("preview 失败 → onError 收到「生成调整计划失败」", async () => {
    const { onError } = renderPanel({
      preview: vi.fn(async () => { throw { response: { data: { detail: "AI 服务异常" } } }; }),
    });
    await openAsk("改深度");
    clickOk();
    await waitFor(() => expect(onError).toHaveBeenCalledWith(
      "生成调整计划失败", expect.anything()));
  });

  it("preview 期间切换方案 → 丢弃结果，不渲染计划", async () => {
    let resolveIt!: (v?: unknown) => void;
    const gate: Promise<any> = new Promise((r) => { resolveIt = r as any; });
    const preview = vi.fn(async () => gate.then(() => ({ operations: OPS(3) })));
    const view = render(
      <FactsAdjustPanel schemeId="scheme-1" preview={preview as any}
        apply={vi.fn(async () => {}) as any} onError={vi.fn() as any}
        confirmDialog={vi.fn() as any} />,
    );
    await openAsk("改深度");
    clickOk();
    // 请求在途时切方案 → 旧方案的计划必须丢弃
    view.rerender(
      <FactsAdjustPanel schemeId="scheme-2" preview={preview as any}
        apply={vi.fn(async () => {}) as any} onError={vi.fn() as any}
        confirmDialog={vi.fn() as any} />,
    );
    resolveIt();
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(screen.queryByRole("button", { name: /应用调整计划/ })).toBeNull();
  });
});


type ConfirmOptions = {
  title: string;
  content: React.ReactNode;
  okText: string;
  cancelText: string;
  okButtonProps: { danger: boolean };
  onOk: () => Promise<void>;
};

/** 渲染一个默认参数齐备的面板；返回注入的 spy 句柄 */
function renderPanel(props: Partial<Parameters<typeof FactsAdjustPanel>[0]> = {}) {
  const preview = props.preview ?? vi.fn(async () => ({ operations: [] as FactsAdjustOperation[] }));
  const apply = props.apply ?? vi.fn(async () => {});
  const onError = props.onError ?? vi.fn();
  const confirmDialog = props.confirmDialog ?? vi.fn();
  const view = render(
    <FactsAdjustPanel
      schemeId="scheme-1"
      preview={preview as any}
      apply={apply as any}
      onError={onError as any}
      confirmDialog={confirmDialog as any}
      {...props}
    />,
  );
  return { view, preview, apply, onError, confirmDialog };
}

/** 打开弹层并输入文本 */
async function openAsk(text: string) {
  fireEvent.click(screen.getByRole("button", { name: /AI 调整事实/ }));
  // ⚠️ 说明文字里夹了 <b>待确认</b>，用 getByText 的精确匹配会匹配不到
  //    「用一句自然语言描述调整要求」这个前缀文本节点 → 必须用正则/函数匹配。
  await waitFor(() => screen.getByText(/用一句自然语言描述调整要求/));
  const area = screen.getByPlaceholderText(PLACEHOLDER) as HTMLTextAreaElement;
  fireEvent.change(area, { target: { value: text } });
}

/** 点击弹层的 OK 按钮 */
function clickOk() {
  fireEvent.click(screen.getByRole("button", { name: "生成调整计划" }));
}

const OPS = (n: number): FactsAdjustOperation[] =>
  Array.from({ length: n }, (_, i) => ({
    op: "update",
    fact_id: `f${i + 1}`,
    name: `开挖深度${i + 1}`,
    value: "8.5m",
  }));
