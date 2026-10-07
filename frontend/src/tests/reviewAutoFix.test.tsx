// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { App } from "antd";
import AutoFixModal from "../components/review/AutoFixModal";
import { reviewAutoFixApi } from "../api";
import type { PreflightFinding } from "../types/audit";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

// ✅ 2026-10-06：弹窗 msg 来自项目自封装 useAntdMessageHub。mock 它即可精确断言告文案。
//    ⚠️ 代理必须按 source 缓存后返回同一对象，否则组件的 useCallback/effect
//    以 msg 为依赖永变 → 无限重渲染（本轮实测卡死）。
const msgSpy = vi.hoisted(() => ({
  success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn(), loading: vi.fn(),
  hubCache: new Map<string, any>(),
}));
vi.mock("../utils/activityCenter", async (orig) => {
  const real = await orig<any>();
  return {
    ...real,
    useAntdMessageHub: (m: any, source: string) => {
      const hub = real.useAntdMessageHub(m, source);
      let w = msgSpy.hubCache.get(source);
      if (!w) {
        w = {
          success: (...a: any[]) => { msgSpy.success(...a); return hub.success(...a); },
          error: (...a: any[]) => { msgSpy.error(...a); return hub.error(...a); },
          warning: (...a: any[]) => { msgSpy.warning(...a); return hub.warning(...a); },
          info: (...a: any[]) => { msgSpy.info(...a); return hub.info(...a); },
          loading: (...a: any[]) => { msgSpy.loading(...a); return hub.loading(...a); },
        };
        msgSpy.hubCache.set(source, w);
      }
      return w;
    },
  };
});

vi.mock("../api", () => ({
  reviewAutoFixApi: {
    plan: vi.fn(), apply: vi.fn(), rollback: vi.fn(), capabilities: vi.fn(),
  },
}));

const PLAN = {
  ok: true, fixable: true, mode: "ai", reason: "", finding: {},
  targets: [{
    section_id: "sec-a", section_title: "工程概况", value: "120日历天",
    line: 12, matched: "120 日历天",
    context: "本工程总工期为 120 日历天，混凝土强度等级为 C30。",
    why: "该主题在此处的取值",
  }],
  max_sections: 10,
};

const APPLIED = {
  ok: true, status: "repaired", mode: "ai", rule_id: "CON-01", targets: [],
  snapshot_id: "ver-1", stats: { repaired: 1, failed: 0, skipped: 0 },
  items: [{
    section_id: "sec-a", section_title: "工程概况", status: "repaired",
    targets: [], before: "总工期 120 日历天", after: "总工期 90 日历天", problems: [],
  }],
};

function finding(autofix?: PreflightFinding["autofix"]): PreflightFinding {
  return {
    rule_id: "CON-01", dimension: "consistency", severity: "high",
    title: "工期口径不一致", detail: "「工期」出现 2 种取值",
    evidence: ["120日历天（工程概况）"], section_id: "", section_title: "",
    suggestion: "请统一工期口径", basis: "", mode: "program", autofix,
  } as PreflightFinding;
}

/** antd Modal 渲染在 portal 里（挂 document.body，不在 container 内）→ 全局查找 */
function btn(root: HTMLElement, text: string): HTMLButtonElement | undefined {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return Array.from(root.querySelectorAll("button")).find((b) => {
    const t = norm(b.textContent);
    if (!t) return false;   // ⚠️ 空文案按钮（纯图标）不得匹配任何查询词
    return t.includes(norm(text)) || norm(text).includes(t);
  }) as HTMLButtonElement | undefined;
}

/** 弹窗实际挂载在 body 上，断言一律针对 body */
const view = () => document.body as HTMLElement;

const ai = { mode: "ai", fixable: true, reason: "" } as const;

beforeEach(() => {
  (reviewAutoFixApi.plan as any).mockReset().mockResolvedValue({ data: PLAN });
  (reviewAutoFixApi.apply as any).mockReset().mockResolvedValue({ data: APPLIED });
  (reviewAutoFixApi.rollback as any).mockReset()
    .mockResolvedValue({ data: { status: "rolled_back" } });
});
afterEach(() => {
  cleanup();
  msgSpy.success.mockClear(); msgSpy.error.mockClear();
  msgSpy.warning.mockClear(); msgSpy.info.mockClear();
});

/** 断言某条告警文案确实推给用户（任一级别） */
function expectMsg(fn: ReturnType<typeof vi.fn>, frag: string) {
  const hit = fn.mock.calls.some((c: any[]) => String(c[0] ?? "").includes(frag));
  expect(hit, `未捕获到含「${frag}」的告警；实际调用：${JSON.stringify(fn.mock.calls)}`).toBe(true);
}

describe("AutoFixModal · 审核预检问题自动修复", () => {
  it("ai 模式：未定位前不允许直接修复（必须先定位确认）", async () => {
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open onClose={() => {}} /></App>);
    const applyBtn = btn(view(), "调用 AI 修复此处")!;
    expect(applyBtn).toBeTruthy();
    expect(applyBtn.disabled).toBe(true);
    expect(reviewAutoFixApi.plan).not.toHaveBeenCalled();
    expect(reviewAutoFixApi.apply).not.toHaveBeenCalled();
  });

  it("定位后回显矛盾位置（章节 + 行号 + 原文），才放开修复按钮", async () => {
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open onClose={() => {}} /></App>);
    fireEvent.click(btn(view(), "定位矛盾位置")!);
    await waitFor(() =>
      expect((view().textContent || "")).toContain("已定位到 1 处矛盾位置"));
    expect(reviewAutoFixApi.plan).toHaveBeenCalledWith("s1", { rule_id: "CON-01" });
    // 定位回显必须带行号与原文（用户据此确认位置）
    expect((view().textContent || "")).toContain("第 12 行");
    expect((view().textContent || "")).toContain("总工期为 120 日历天");
    expect(btn(view(), "调用 AI 修复此处")!.disabled).toBe(false);
  });

  it("定位失败：提示原因且修复按钮保持禁用（不盲改）", async () => {
    (reviewAutoFixApi.plan as any).mockResolvedValue({ data: {
      ...PLAN, ok: false, fixable: false, targets: [],
      reason: "未能在正文中定位到该问题的具体位置" } });
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open onClose={() => {}} /></App>);
    fireEvent.click(btn(view(), "定位矛盾位置")!);
    await waitFor(() =>
      expect((view().textContent || "")).toContain("未能定位到问题位置"));
    expect(btn(view(), "调用 AI 修复此处")!.disabled).toBe(true);
    expect(reviewAutoFixApi.apply).not.toHaveBeenCalled();
  });

  it("manual 模式：不提供定位/修复入口，并展示替代路径", async () => {
    render(
      <App><AutoFixModal schemeId="s1"
        finding={finding({ mode: "manual", fixable: false, reason: "请到「正文生成」页补全生成" })}
        open onClose={() => {}} /></App>);
    expect((view().textContent || "")).toContain("该问题不支持自动修复");
    expect((view().textContent || "")).toContain("请到「正文生成」页补全生成");
    expect(btn(view(), "定位矛盾位置")).toBeFalsy();
    expect(reviewAutoFixApi.plan).not.toHaveBeenCalled();
  });

  it("auto 模式：按钮文案为「执行修复」且明确不调用 AI", async () => {
    render(
      <App><AutoFixModal schemeId="s1"
        finding={finding({ mode: "auto", fixable: true, reason: "" })}
        open onClose={() => {}} /></App>);
    expect((view().textContent || "")).toContain("程序化修复（不调用 AI）");
    expect(btn(view(), "调用 AI 修复此处")).toBeFalsy();
    expect(btn(view(), "执行修复")).toBeTruthy();
  });

  it("修复成功：展示前后对比 + 审核失效提示 + 出现回滚按钮", async () => {
    const onFixed = vi.fn();
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open
        onClose={() => {}} onFixed={onFixed} /></App>);
    fireEvent.click(btn(view(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(view(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(view(), "调用 AI 修复此处")!);
    await waitFor(() => expect(reviewAutoFixApi.apply).toHaveBeenCalled());
    expect((view().textContent || "")).toContain("总工期 90 日历天");
    expect((view().textContent || "")).toContain("审核结论已自动退回");
    expect(btn(view(), "回滚本次修复")).toBeTruthy();
    // 修复后必须通知宿主页刷新（正文与总检结论均已变更）
    expect(onFixed).toHaveBeenCalledWith("ver-1");
  });

  it("回滚：调用 rollback 并清空结果", async () => {
    const onFixed = vi.fn();
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open
        onClose={() => {}} onFixed={onFixed} /></App>);
    fireEvent.click(btn(view(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(view(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(view(), "调用 AI 修复此处")!);
    await waitFor(() => expect(btn(view(), "回滚本次修复")).toBeTruthy());
    fireEvent.click(btn(view(), "回滚本次修复")!);
    await waitFor(() => expect(reviewAutoFixApi.rollback).toHaveBeenCalledWith(
      "s1", { snapshot_id: "ver-1" }));
    await waitFor(() => expect(btn(view(), "回滚本次修复")).toBeFalsy());
    expect(onFixed).toHaveBeenLastCalledWith("");
  });

  it("修复失败（校验未通过）：展示原因且不出现回滚入口", async () => {
    (reviewAutoFixApi.apply as any).mockResolvedValue({ data: {
      ok: false, status: "failed", mode: "ai", rule_id: "CON-01", targets: [],
      snapshot_id: "",
      items: [{
        section_id: "sec-a", section_title: "工程概况", status: "failed", targets: [],
        before: "原文", after: "原文", problems: ["修复后内容与原文一致，问题未消除"],
      }],
    } });
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(ai)} open onClose={() => {}} /></App>);
    fireEvent.click(btn(view(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(view(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(view(), "调用 AI 修复此处")!);
    await waitFor(() => expect((view().textContent || "")).toContain("未修复（保留原文）"));
    expect((view().textContent || "")).toContain("问题未消除");
    expect(btn(view(), "回滚本次修复")).toBeFalsy();
  });

  it("autofix 字段缺失（后端未标注）：不提供定位入口，提示重新总检", async () => {
    render(
      <App><AutoFixModal schemeId="s1" finding={finding(undefined)}
        open onClose={() => {}} /></App>);
    // 旧数据无 autofix 字段时退化为「不可修」：不给定位入口，修复按钮禁用
    expect(btn(view(), "定位矛盾位置")).toBeFalsy();
    expect(btn(view(), "执行修复")!.disabled).toBe(true);
    expect((view().textContent || "")).toContain("该问题未提供自动修复");
  });
});

// ===========================================================================
// ✅ 2026-10-06 缺口收口：AutoFixModal 失败 / 取消 / 状态分支补齐
//
// 旧覆盖只走了「定位成功 → 修复成功 → 回滚」一条主干路径，以下四类交互零覆盖：
//   ① 三个接口的失败分支（plan/apply/rollback）—— 特别是 rollback 失败，
//      用户正需要它恢复数据，却只看到一口错误提示。
//   ② apply 的 not_located / unsupported 状态分支—— 与「repaired/failed」部分很差异。
//   ③ 弹窗关闭 → reset()（之前完全未验证，重开时会残留上一次的计划与结果）。
//   ④ items 为空但有 reason → Empty 分支。
// ===========================================================================
describe("AutoFixModal · 失败与状态分支", () => {
  function mount(f: PreflightFinding | null, onFixed = vi.fn(), onClose = vi.fn()) {
    const r = render(<App><AutoFixModal schemeId="s1" finding={f} open
      onClose={onClose} onFixed={onFixed} /></App>);
    return { ...r, onFixed, onClose, body: () => document.body as HTMLElement };
  }

  it("plan 失败 → 报错且可重试（定位按钮仍可点）", async () => {
    (reviewAutoFixApi.plan as any).mockRejectedValue(new Error("定位服务不可用"));
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expectMsg(msgSpy.error, "定位服务不可用"));
    // 失败后不得调 apply（未定位就修改 = 盲改），且定位按钮仍可重试
    expect(reviewAutoFixApi.apply).not.toHaveBeenCalled();
    expect(btn(body(), "定位矛盾位置")).toBeTruthy();
  });

  it("apply 失败 → 报错且不出现回滚按钮", async () => {
    (reviewAutoFixApi.apply as any).mockRejectedValue(new Error("AI 模型超时"));
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expectMsg(msgSpy.error, "AI 模型超时"));
    // 无快照 ⇒ 不得提供「回滚本次修复」（无从回滚）
    expect(btn(body(), "回滚本次修复")).toBeFalsy();
  });

  it("rollback 失败 → 报错且结果不被清空", async () => {
    (reviewAutoFixApi.rollback as any).mockRejectedValue(new Error("回滚服务不可用"));
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expect(btn(body(), "回滚本次修复")).toBeTruthy());
    fireEvent.click(btn(body(), "回滚本次修复")!);
    await waitFor(() => expectMsg(msgSpy.error, "回滚服务不可用"));
    // 修复结果仍在 → 用户仍能再次尝试回滚
    expect(btn(body(), "回滚本次修复")).toBeTruthy();
  });

  it("apply 返回 not_located → 告知未定位，不得显示回滚按钮", async () => {
    (reviewAutoFixApi.apply as any).mockResolvedValue({ data: {
      ...APPLIED, ok: false, status: "not_located", reason: "未能在正文中定位到取值",
      items: [], snapshot_id: "",
    } });
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expectMsg(msgSpy.warning, "未能在正文中定位到取值"));
    expect(btn(body(), "回滚本次修复")).toBeFalsy();
  });

  it("apply 返回 unsupported → info 告知需人工处理", async () => {
    (reviewAutoFixApi.apply as any).mockResolvedValue({ data: {
      ...APPLIED, ok: false, status: "unsupported", reason: "该规则需人工判断",
      items: [], snapshot_id: "",
    } });
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expectMsg(msgSpy.info, "该规则需人工判断"));
  });

  it("stats.skipped > 0 → 成功提示说明跳过章数", async () => {
    (reviewAutoFixApi.apply as any).mockResolvedValue({ data: {
      ...APPLIED, stats: { repaired: 1, failed: 0, skipped: 3 },
    } });
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expectMsg(msgSpy.success, "3"));
  });

  it("关闭弹窗 → reset 生效（重开不残留上一次的计划与结果）", async () => {
    const onClose = vi.fn();
    const r = render(<App><AutoFixModal schemeId="s1" finding={finding(ai)} open
      onClose={onClose} /></App>);
    const body = () => document.body as HTMLElement;
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(body().textContent || "").toContain("已定位到"));
    // 点底部关闭
    fireEvent.click(btn(body(), "关闭")!);
    await waitFor(() => expect(onClose).toHaveBeenCalled());

    // 重开：必须回到未定位的首帯（不得残留旧计划 / 旧结果）
    r.rerender(<App><AutoFixModal schemeId="s1" finding={finding(ai)} open={false}
      onClose={onClose} /></App>);
    r.rerender(<App><AutoFixModal schemeId="s1" finding={finding(ai)} open
      onClose={onClose} /></App>);
    expect(body().textContent || "").not.toContain("已定位到");
    expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(true);
  });

  it("items 为空但带 reason → Empty 分支", async () => {
    (reviewAutoFixApi.apply as any).mockResolvedValue({ data: {
      ...APPLIED, ok: false, status: "failed", reason: "校验未通过，已保留原文",
      items: [], snapshot_id: "",
    } });
    const { body } = mount(finding(ai));
    fireEvent.click(btn(body(), "定位矛盾位置")!);
    await waitFor(() => expect(btn(body(), "调用 AI 修复此处")!.disabled).toBe(false));
    fireEvent.click(btn(body(), "调用 AI 修复此处")!);
    await waitFor(() => expectMsg(msgSpy.warning, "修复未通过校验"));
    expect(body().textContent || "").toContain("已保留原文");
  });

  it("finding 为 null → 不渲染任何内容", () => {
    mount(null);
    expect((document.body.textContent || "").includes("定位矛盾位置")).toBe(false);
  });
});
