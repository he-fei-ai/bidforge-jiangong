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
afterEach(() => cleanup());

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
      batch_id: "batch-1", reject: ["DLV-05"],
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
      batch_id: "batch-2", accept: ["CON-01"],
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
      batch_id: "batch-3", accept: ["DLV-05"],
    }));
  });
});
