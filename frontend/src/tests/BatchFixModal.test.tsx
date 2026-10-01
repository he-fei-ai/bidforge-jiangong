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

const CONFIRM: AutoFixConfirmResult = {
  status: "confirmed", accepted: 1, repaired_sections: 1,
  snapshot_id: "ver-batch", batch_id: "batch-1",
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
});
