// @vitest-environment jsdom
/** 批量「一键修复全部阻断项」弹窗：collect → stage → confirm 链路契约测试 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { App } from "antd";
import BatchFixModal from "../components/review/BatchFixModal";
import { reviewAutoFixApi } from "../api";
import type {
  AutoFixCollectResult, AutoFixStageResult, AutoFixConfirmResult, PreflightFinding,
} from "../types/audit";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
}

const COLLECT: AutoFixCollectResult = {
  scheme_id: "s1", scope: "all_blocking", total: 1,
  items: [{
    rule_id: "DLV-05", dimension: "deliverability", severity: "block",
    title: "控制字符", detail: "含控制字符", evidence: [],
    section_id: "", section_title: "", suggestion: "", basis: "", mode: "program",
    autofix: { fixable: true, mode: "auto", reason: "" },
    targets: [{
      section_id: "sec-a", section_title: "工程概况", value: "x07", line: 3,
      sentence_idx: 1, sentence_total: 2, matched: "x07",
      context: "正常[\x07]内容", why: "命中",
    }],
  } as unknown as PreflightFinding],
};

const STAGE: AutoFixStageResult = {
  batch_id: "batch-1", status: "pending_confirm",
  items: [{
    rule_id: "DLV-05", section_id: "sec-a", section_title: "工程概况",
    mode: "auto", status: "repaired", reason: "",
    targets: [{
      section_id: "sec-a", section_title: "工程概况", value: "x07", line: 3,
      sentence_idx: 1, sentence_total: 2, matched: "x07", context: "x", why: "x",
    }],
    before: "正常[\x07]内容", after: "正常内容", problems: [], chain_index: 0,
    sentence_idx: 1, sentence_total: 2,
  }],
  stats: { repaired: 1, failed: 0, skipped: 0 },
};

/** 多项暂存：两条可修（不同章）+ 一条失败（带原因） —— 部分接受契约的用例数据 */
const STAGE_MULTI: AutoFixStageResult = {
  batch_id: "batch-2", status: "pending_confirm",
  items: [
    {
      rule_id: "DLV-05", section_id: "sec-a", section_title: "工程概况",
      mode: "auto", status: "repaired", reason: "", targets: [],
      before: "正常[\x07]内容", after: "正常内容", problems: [], chain_index: 0,
      sentence_idx: 0, sentence_total: 0,
    },
    {
      rule_id: "CON-01", section_id: "sec-b", section_title: "施工部署",
      mode: "ai", status: "repaired", reason: "", targets: [],
      before: "总工期 120 日历天", after: "总工期 90 日历天", problems: [], chain_index: 0,
      sentence_idx: 0, sentence_total: 0,
    },
    {
      rule_id: "DLV-07", section_id: "sec-c", section_title: "应急处置",
      mode: "auto", status: "failed", reason: "围栏闭合后校验未通过", targets: [],
      before: "```mermaid\ngraph TD", after: "```mermaid\ngraph TD", problems: [], chain_index: 0,
      sentence_idx: 0, sentence_total: 0,
    },
  ],
  stats: { repaired: 2, failed: 1, skipped: 0 },
};

const CONFIRM: AutoFixConfirmResult = {
  status: "confirmed", accepted: 1, repaired_sections: 1,
  snapshot_id: "ver-batch", batch_id: "batch-1",
};

/** 同章多项暂存：两条不同规则落在同一章节 —— 选择状态按 rule|section
 *  独立记录（若 key 退化成裸 section_id，两条会塞塌为一个勾选框/一条
 *  accept，后端前缀链式合并（_merged_after_if_prefix）拿不到完整接受集）。
 *  后端链语义已由 6 例锁定，此处是 UI 层双保险。 */
const STAGE_SAME_SEC: AutoFixStageResult = {
  batch_id: "batch-3", status: "pending_confirm",
  items: [
    {
      rule_id: "DLV-05", section_id: "sec-a", section_title: "工程概况",
      mode: "auto", status: "repaired", reason: "", targets: [],
      before: "正常[\x07]内容", after: "正常内容", problems: [], chain_index: 0,
      sentence_idx: 0, sentence_total: 0,
    },
    {
      rule_id: "CON-01", section_id: "sec-a", section_title: "工程概况",
      mode: "ai", status: "repaired", reason: "", targets: [],
      before: "总工期 120 日历天", after: "总工期 90 日历天", problems: [],
      chain_index: 1, sentence_idx: 0, sentence_total: 0,
    },
  ],
  stats: { repaired: 2, failed: 0, skipped: 0 },
};

// ✅ 2026-10-06：弹窗 msg 来自项目自封装 useAntdMessageHub；代理按 source 缓存（否则无限重渲染）。
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
    collect: vi.fn(), stage: vi.fn(), confirm: vi.fn(),
    plan: vi.fn(), apply: vi.fn(), rollback: vi.fn(), capabilities: vi.fn(),
  },
}));

const api = reviewAutoFixApi as any;

function txt(root: HTMLElement) { return root.textContent || ""; }
function btn(root: HTMLElement, label: string): HTMLButtonElement | undefined {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return Array.from(root.querySelectorAll("button")).find((b) => {
    const t = norm(b.textContent);
    return t && (norm(label).includes(t) || t.includes(norm(label)));
  }) as HTMLButtonElement | undefined;
}
const view = () => document.body as HTMLElement;

beforeEach(() => {
  api.collect.mockReset().mockResolvedValue({ data: COLLECT });
  api.stage.mockReset().mockResolvedValue({ data: STAGE });
  api.confirm.mockReset().mockResolvedValue({ data: CONFIRM });
});
afterEach(() => {
  cleanup();
  msgSpy.success.mockClear(); msgSpy.error.mockClear();
  msgSpy.warning.mockClear(); msgSpy.info.mockClear();
});

/** 断言某条告警文案确实推给用户 */
function expectMsg(fn: any, frag: string) {
  const hit = fn.mock.calls.some((c: any[]) => String(c[0] ?? "").includes(frag));
  expect(hit, `未捕获到含「${frag}」的告警；实际：${JSON.stringify(fn.mock.calls)}`).toBe(true);
}

describe("BatchFixModal · 一键修复全部阻断项", () => {
  it("打开即收集阻断项，展示定位与句子级信息", async () => {
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(api.collect).toHaveBeenCalledWith("s1", { scope: "all_blocking" }));
    await waitFor(() => expect(txt(view())).toContain("一键修复全部阻断项"));
    expect(txt(view())).toContain("DLV-05");
    // 句子级定位回显
    expect(txt(view())).toContain("第 1/2 句");
    // 收集阶段出现「生成修复预览」
    expect(btn(view(), "生成修复预览")).toBeTruthy();
  });

  it("生成预览 → 确认修复：stage 后 confirm(accept_all) 落库", async () => {
    const onFixed = vi.fn();
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} onFixed={onFixed} /></App>);
    // 收集结束前该按钮处于 disabled，必须等其可用再点（否则 click 是 no-op）
    const sb = await waitFor(() => {
      const b = btn(view(), "生成修复预览") as HTMLButtonElement | undefined;
      expect(b).toBeTruthy();
      expect(b!.disabled).toBe(false);
      return b!;
    });
    fireEvent.click(sb);
    await waitFor(() => expect(api.stage).toHaveBeenCalledWith("s1", { scope: "all_blocking" }));
    await waitFor(() => expect(txt(view())).toContain("修复后："));
    expect(txt(view())).toContain("正常内容");
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-1", accept_all: true,
    }));
    await waitFor(() => expect(onFixed).toHaveBeenCalledWith("ver-batch"));
  });

  it("拒绝全部：confirm(reject) 丢弃批次，正文不动", async () => {
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(btn(view(), "生成修复预览")?.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    await waitFor(() => expect(btn(view(), "拒绝全部")).toBeTruthy());
    await act(async () => { fireEvent.click(btn(view(), "拒绝全部")!); });
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-1", reject: ["DLV-05|sec-a"],
    }));
  });

  it("空暂存：后端返回 empty 不进入预览、不调用 confirm", async () => {
    api.stage.mockResolvedValue({ data: { batch_id: "", status: "empty", items: [],
      stats: { repaired: 0, failed: 0, skipped: 0 },
      reason: "当前范围内没有可自动修复的问题" } });
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(btn(view(), "生成修复预览")?.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    // 核心契约：stage 已调用，但因 empty 不进入预览、不触发 confirm，停留在收集态
    await waitFor(() => expect(api.stage).toHaveBeenCalledWith("s1", { scope: "all_blocking" }));
    expect(api.confirm).not.toHaveBeenCalled();
    expect(txt(view())).toContain("共 1 项阻断问题支持自动修复");
  });

  // =========================================================================
  // 部分接受交互（2026-10-03 补齐）：后端 _merged_after_if_prefix 前缀链式
  // 改写语义依赖 confirm 的 accept/reject 数组与全选的 accept_all 分流正确，
  // 此前只测了 accept_all 与拒绝全部两条极端路径。
  // =========================================================================

  const openReview = async (stageRes: AutoFixStageResult) => {
    api.stage.mockResolvedValue({ data: stageRes });
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(btn(view(), "生成修复预览")?.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    await waitFor(() => expect(txt(view())).toContain("确认修复"));
  };

  it("多项暂存：默认全勾选已修复项；失败项展示原因且无勾选框", async () => {
    await openReview(STAGE_MULTI);
    // 只有 repaired 项渲染 Checkbox（failed 项无 → 不可被误接受）
    expect(view().querySelectorAll("input[type=checkbox]").length).toBe(2);
    view().querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((c) =>
      expect(c.checked).toBe(true));
    expect(txt(view())).toContain("围栏闭合后校验未通过");
    expect(txt(view())).toContain("失败");
  });

  it("取消勾选一条 → confirm 走 accept 数组（非 accept_all，前缀链式语义）", async () => {
    await openReview(STAGE_MULTI);
    const boxes = Array.from(view().querySelectorAll("input[type=checkbox]"));
    fireEvent.click(boxes[0]);           // 取消 DLV-05，保留 CON-01
    await waitFor(() =>
      expect(txt(view())).toContain("确认修复（接受选中 1）"));
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-2", accept: ["CON-01|sec-b"],
    }));
  });

  it("全不选 → 确认按钮禁用（空接受集不得发 confirm）", async () => {
    await openReview(STAGE_MULTI);
    const boxes = Array.from(view().querySelectorAll("input[type=checkbox]"));
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    const cb = btn(view(), "确认修复") as HTMLButtonElement | undefined;
    expect(cb).toBeTruthy();
    expect(cb!.disabled).toBe(true);
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("「全不选/全选」切换钮：一键反选全部已修复项", async () => {
    await openReview(STAGE_MULTI);
    const toggle = btn(view(), "全不选")!;
    expect(toggle).toBeTruthy();
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(txt(view())).toContain("确认修复（接受选中 0）"));
    const back = btn(view(), "全选")!;
    expect(back).toBeTruthy();
    fireEvent.click(back);
    await waitFor(() =>
      expect(txt(view())).toContain("确认修复（接受选中 2）"));
  });

  it("收集失败（接口异常）：停留在收集态，预览按钮禁用且不可达 stage", async () => {
    api.collect.mockRejectedValueOnce(new Error("network down"));
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(api.collect).toHaveBeenCalled());
    // collectData 为 null → 可修项计数 0 → 预览按钮禁用；stage 从未被调用
    const sb = btn(view(), "生成修复预览") as HTMLButtonElement | undefined;
    expect(sb).toBeTruthy();
    expect(sb!.disabled).toBe(true);
    expect(api.stage).not.toHaveBeenCalled();
  });

  // =========================================================================
  // 同章多条（2026-10-03 遗留收口）：_merged_after_if_prefix 的链式前缀语义
  // 要求同章多项按顺序全部下发；UI 选择集按 rule|section 独立记录是链路正确
  // 性的前提 —— 此前 STAGE_MULTI 只覆盖了异章形态。
  // =========================================================================

  it("同章两条已修复项：各自独立勾选，全选时仍走 accept_all", async () => {
    await openReview(STAGE_SAME_SEC);
    // 关键区分度：同 section_id 的两条不得塞塌为一个勾选框
    expect(view().querySelectorAll("input[type=checkbox]").length).toBe(2);
    view().querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((c) =>
      expect(c.checked).toBe(true));
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-3", accept_all: true,
    }));
  });

  it("同章两条取消其一 → accept 数组只带另一条 rule_id（链式语义可拆）", async () => {
    await openReview(STAGE_SAME_SEC);
    const boxes = Array.from(view().querySelectorAll("input[type=checkbox]"));
    fireEvent.click(boxes[1]);           // 取消同章的 CON-01，保留 DLV-05
    await waitFor(() =>
      expect(txt(view())).toContain("确认修复（接受选中 1）"));
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-3", accept: ["DLV-05|sec-a"],
    }));
  });
});

// ===========================================================================
// ✅ 2026-10-06 缺口收口：BatchFixModal 确认阶段与失败反馈补齐
//
// 三步链路「收集 → 暂存 → 确认」旧覆盖只走了主干成功路径与「拒绝全部」，
// 以下均零覆盖：① stage/confirm 失败反馈；② confirm 返回 rejected 自动关闭；
// ③ done 阶段的结果渲染与底部「关闭」按钮；④ 采集为空的空态；
// ⑤ 重开弹窗的 reset；⑥ unsupported / not_located 项的标签与原因。
// 注：confirm 是**落库**链路，失败无法自动恢复——必须可见。
// ===========================================================================
describe("BatchFixModal · 确认阶段与失败反馈", () => {
  const mount = (onClose = vi.fn()) => {
    const r = render(<App><BatchFixModal schemeId="s1" open onClose={onClose} /></App>);
    return { ...r, onClose };
  };
  const stage2 = async () => {
    await waitFor(() => expect(api.collect).toHaveBeenCalled());
    await waitFor(() => expect(btn(view(), "生成修复预览")!.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    await waitFor(() => expect(api.stage).toHaveBeenCalled());
  };

  it("stage 失败 → 报错且停在收集阶段（不自动确认）", async () => {
    api.stage.mockRejectedValue(new Error("暂存服务不可用"));
    mount();
    await waitFor(() => expect(api.collect).toHaveBeenCalled());
    await waitFor(() => expect(btn(view(), "生成修复预览")!.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    await waitFor(() => expectMsg(msgSpy.error, "暂存服务不可用"));
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("confirm 失败 → 报错且不进入 done（正文未落库，用户必须知道）", async () => {
    api.confirm.mockRejectedValue(new Error("落库失败：数据库不可用"));
    mount();
    await stage2();
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalled());
    await waitFor(() => expectMsg(msgSpy.error, "数据库不可用"));
    expect(txt(view())).not.toContain("已接受");
  });

  it("confirm 返回 rejected → 提示已拒绝并自动关闭", async () => {
    api.confirm.mockResolvedValue({ data: {
      status: "rejected", accepted: 0, repaired_sections: 0,
      snapshot_id: "", batch_id: "batch-1",
    } });
    const { onClose } = mount();
    await stage2();
    fireEvent.click(btn(view(), "拒绝全部")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-1", reject: ["DLV-05|sec-a"],
    }));
    await waitFor(() => expectMsg(msgSpy.info, "已拒绝"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("done 阶段：正文已落库的结果与回滚提示均可见，底部按钮切为「关闭」", async () => {
    const { onClose } = mount();
    await stage2();
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-1", accept_all: true,
    }));
    await waitFor(() => expect(txt(view())).toContain("已修复 1 项问题"));
    // done 阶段必须告知正文已变更（导出缓存与审核结论失效）
    expect(txt(view())).toContain("正文已变更");
    const close = btn(view(), "关闭")!;
    expect(close).toBeTruthy();
    expect(btn(view(), "取消")).toBeFalsy();
    fireEvent.click(close);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("采集为空 → 空态提示且「生成修复预览」禁用", async () => {
    api.collect.mockResolvedValue({ data: { ...COLLECT, total: 0, items: [] } });
    mount();
    await waitFor(() => expect(api.collect).toHaveBeenCalled());
    await waitFor(() => expect(txt(view())).toContain("没有可自动修复"));
    expect(btn(view(), "生成修复预览")!.disabled).toBe(true);
    expect(api.stage).not.toHaveBeenCalled();
  });

  it("重开弹窗 → reset 并重新采集（不残留上一次的暂存结果）", async () => {
    const { rerender, onClose } = mount();
    await stage2();
    await waitFor(() => expect(api.confirm).not.toHaveBeenCalled());
    // 关闭后重开
    rerender(<App><BatchFixModal schemeId="s1" open={false} onClose={onClose} /></App>);
    rerender(<App><BatchFixModal schemeId="s1" open onClose={onClose} /></App>);
    // 重开后回到「收集」阶段：应重新获取 collect 并不再残留暂存项
    await waitFor(() =>
      expect((reviewAutoFixApi.collect as any).mock.calls.length).toBeGreaterThan(1));
    expect(btn(view(), "生成修复预览")!.disabled).toBe(false);
    const confirmBtn = btn(view(), "确认修复（接受选中");
    if (confirmBtn) expect(confirmBtn.disabled).toBe(true);
  });

  it("stage 返回 unsupported / not_located 项 → 标签与原因可见且不可勾选", async () => {
    api.stage.mockResolvedValue({ data: {
      batch_id: "batch-x", status: "pending_confirm",
      items: [
        { rule_id: "DLV-05", section_id: "sec-a", section_title: "工程概况",
          mode: "auto", status: "repaired", reason: "", targets: [],
          before: "a", after: "b", problems: [], chain_index: 0,
          sentence_idx: 0, sentence_total: 0 },
        { rule_id: "CON-02", section_id: "sec-b", section_title: "施工组织",
          mode: "manual", status: "unsupported", reason: "该规则需人工判断",
          targets: [], before: "c", after: "c", problems: [], chain_index: 0,
          sentence_idx: 0, sentence_total: 0 },
        { rule_id: "CON-03", section_id: "sec-c", section_title: "应急预案",
          mode: "ai", status: "not_located", reason: "未定位到冲突位置",
          targets: [], before: "d", after: "d", problems: [], chain_index: 0,
          sentence_idx: 0, sentence_total: 0 },
      ],
      stats: { repaired: 1, failed: 0, skipped: 2 },
    } });
    mount();
    await stage2();
    await waitFor(() => expect(txt(view())).toContain("需人工"));
    expect(txt(view())).toContain("未定位到冲突位置");
    // 只有 repaired 项可勾选（默认勾中 1 项）
    expect(btn(view(), "确认修复（接受选中 1）")).toBeTruthy();
  });

  it("采集回调失败 → 报错且不进入 stage", async () => {
    api.collect.mockRejectedValue(new Error("收集服务不可用"));
    mount();
    await waitFor(() => expectMsg(msgSpy.error, "收集服务不可用"));
    expect(api.stage).not.toHaveBeenCalled();
  });

  it("取消未选项时确认按钮禁用（不发空批次序）", async () => {
    api.stage.mockResolvedValue({ data: STAGE_MULTI });
    mount();
    await stage2();
    // 全不选
    await waitFor(() => expect(btn(view(), "全不选")).toBeTruthy());
    fireEvent.click(btn(view(), "全不选")!);
    await waitFor(() =>
      expect(btn(view(), "确认修复（接受选中 0）")!.disabled).toBe(true));
    expect(api.confirm).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// ✅ R55 F3（2026-10-08）：批量契约中「前端不可见」的三条链路补齐
//  ① /stage 命中章节上限截断 → skipped_sections + max_sections 必须可见
//    （toast + 清单上方 Alert）—— 上限是护栏，不可见才是缺陷；
//  ② /confirm 回传 skipped（链式重算时 finding 已失效被丢弃）→ 此前整段丢弃，
//    「以为修了、实际没修」在 UI 层复发；现 toast + done 阶段逐项明细；
//  ③ accept/reject 按 rule_id|section_id 复合键下发 —— 同规则跨章时勾选一项
//    ≠ 接受全部（旧实现按裸 rule_id，UI 勾选粒度与协议粒度分叉）。
// ===========================================================================
describe("BatchFixModal · R55 上限截断与失效跳过可见性", () => {
  const openStage = async (stageRes: AutoFixStageResult) => {
    api.stage.mockResolvedValue({ data: stageRes });
    render(<App><BatchFixModal schemeId="s1" open onClose={() => {}} /></App>);
    await waitFor(() => expect(btn(view(), "生成修复预览")?.disabled).toBe(false));
    fireEvent.click(btn(view(), "生成修复预览")!);
    await waitFor(() => expect(txt(view())).toContain("确认修复"));
  };

  it("stage 命中章节上限 → 告警 toast + 未处理章节清单 Alert（含上限值）", async () => {
    await openStage({
      ...STAGE,
      max_sections: 1,
      skipped_sections: [{
        section_id: "sec-z", section_title: "冬雨期施工", finding_count: 2,
        rule_ids: ["DLV-05", "CON-01"],
      }],
    } as AutoFixStageResult);
    expectMsg(msgSpy.warning, "因单次修复上限本轮未处理");
    const t = txt(view());
    expect(t).toContain("个章节本轮未处理");
    expect(t).toContain("（1 章）");
    expect(t).toContain("冬雨期施工");
    expect(t).toContain("2 项");
  });

  it("confirm 返回 skipped → toast 提醒 + done 阶段逐项明细可见（rule/章节/理由）", async () => {
    api.confirm.mockResolvedValue({ data: {
      status: "confirmed", accepted: 1, repaired_sections: 1,
      snapshot_id: "ver-batch", batch_id: "batch-1",
      skipped: [{
        rule_id: "CON-01", section_id: "sec-b", status: 404,
        detail: "该问题已不在当前总检结果中",
      }],
    } });
    await openStage(STAGE);
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expectMsg(msgSpy.warning, "已失效被跳过"));
    await waitFor(() =>
      expect(txt(view())).toContain("1 项问题在确认时已失效，本轮未修复"));
    const t = txt(view());
    expect(t).toContain("CON-01");
    expect(t).toContain("该问题已不在当前总检结果中");
  });

  it("同规则跨两章：只勾一章 → accept 为该章复合键（旧裸 rule_id 会两章全接受）", async () => {
    await openStage({
      ...STAGE,
      items: [
        { ...STAGE.items[0], section_id: "sec-a", chain_index: 0 },
        { ...STAGE.items[0], section_id: "sec-b", section_title: "施工部署",
          chain_index: 0 },
      ],
      stats: { repaired: 2, failed: 0, skipped: 0 },
    } as AutoFixStageResult);
    const boxes = Array.from(view().querySelectorAll("input[type=checkbox]"));
    expect(boxes.length).toBe(2);
    fireEvent.click(boxes[1]); // 取消 sec-b，仅保留 sec-a
    await waitFor(() => expect(txt(view())).toContain("确认修复（接受选中 1）"));
    fireEvent.click(btn(view(), "确认修复")!);
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("s1", {
      batch_id: "batch-1", accept: ["DLV-05|sec-a"],
    }));
  });
});
