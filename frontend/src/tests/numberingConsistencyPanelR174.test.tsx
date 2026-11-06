// @vitest-environment jsdom
/**
 * ✅ R174 F1（2026-11-03）：编号一致性整族界面接线回归。
 *
 * 旧缺陷（审计 R173 · D1/F1）：后端 4 个端点（校验/修复/版本/回滚）+
 * 前端 api 封装（sectionsApi.numberingConsistency 等）早已存在，api/index.ts
 * 注释却声称「✅ 已接线」，生产代码零调用 —— 编号漂移只能靠肉眼发现。
 *
 * 修复：工作台「审核与预检」Tab 新增 NumberingConsistencyPanel，消费全部
 * 4 个方法；订正失真注释。本文件锁定：
 *   ① 检查 → 渲染漂移汇总与明细；
 *   ② 修复需确认，成功后反馈「修复了多少处」；
 *   ③ 版本历史可回滚（带确认，回滚后自动重查）；
 *   ④ api/index.ts 注释不得再虚报接线（源码锚点）；
 *   ⑤ 工作台页面真实挂载该面板（源码锚点）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { NumberingConsistencyPanel } from "../pages/schemeWorkbench/NumberingConsistencyPanel";
import { sectionsApi } from "../api";

// jsdom 缺 matchMedia / ResizeObserver，antd 会用到
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

vi.mock("../api", () => ({
  sectionsApi: {
    numberingConsistency: vi.fn(),
    repairNumbering: vi.fn(),
    numberingVersions: vi.fn(),
    rollbackNumbering: vi.fn(),
  },
}));

const mockedCheck = vi.mocked(sectionsApi.numberingConsistency);
const mockedRepair = vi.mocked(sectionsApi.repairNumbering);
const mockedVersions = vi.mocked(sectionsApi.numberingVersions);
const mockedRollback = vi.mocked(sectionsApi.rollbackNumbering);

/** 校验报告夹具：3 章检查、1 章漂移（带逐行差异） */
const REPORT = {
  scheme_id: "s1",
  checked: 3,
  mismatched: 1,
  consistent: false,
  sections: [
    { section_id: "sec-a", consistent: true, skipped: false, changed: false, diffs: [] },
    {
      section_id: "sec-b",
      consistent: false,
      skipped: false,
      changed: true,
      diffs: [{ tag: "h3", old: "（一）旧编号", new: "2.1 新编号" }],
    },
    { section_id: "sec-c", consistent: true, skipped: true, changed: false, diffs: [] },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  mockedCheck.mockResolvedValue({ data: REPORT } as never);
  mockedRepair.mockResolvedValue({
    data: {
      fixed: 1,
      total: 3,
      // ✅ R181：修复后的报告不应再有 skipped 章节，否则三态口径下结论是
      //    「无法判定」而非「全部一致」（旧断言把跳过也当成通过，属语义过期）。
      report: {
        checked: 3,
        mismatched: 0,
        consistent: true,
        sections: [
          { section_id: "sec-a", consistent: true, skipped: false, changed: false, diffs: [] },
          { section_id: "sec-b", consistent: true, skipped: false, changed: true, diffs: [] },
          { section_id: "sec-c", consistent: true, skipped: false, changed: false, diffs: [] },
        ],
      },
      snapshot_id: "snap-123",
    },
  } as never);
  mockedVersions.mockResolvedValue({
    data: { scheme_id: "s1", versions: [{ snapshot_id: "snap-123" }, { snapshot_id: "snap-456" }] },
  } as never);
  mockedRollback.mockResolvedValue({
    data: { scheme_id: "s1", snapshot_type: "numbering_rollback" },
  } as never);
});

afterEach(() => cleanup());

function renderPanel() {
  return render(
    <AntdApp>
      <NumberingConsistencyPanel schemeId="s1" />
    </AntdApp>,
  );
}

describe("NumberingConsistencyPanel · 编号一致性接线（R174 F1）", () => {
  it("检查：调 GET numbering-consistency，渲染漂移汇总与差异明细", async () => {
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => expect(mockedCheck).toHaveBeenCalledWith("s1"));
    // 汇总 Tag：1/3 章漂移
    await waitFor(() => {
      expect(screen.getByTestId("numbering-summary-tag").textContent).toContain("1/3 章漂移");
    });
    // 漂移明细表只列不一致章节（sec-b），且展示旧 → 新
    const table = screen.getByTestId("numbering-drift-table");
    expect(table.textContent).toContain("旧编号");
    expect(table.textContent).toContain("新编号");
    expect(table.textContent).not.toContain("sec-a");
  });

  it("检查全部一致 → 绿色汇总，修复按钮禁用", async () => {
    mockedCheck.mockResolvedValue({
      data: { checked: 5, mismatched: 0, consistent: true, sections: [] },
    } as never);
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("numbering-summary-tag").textContent).toContain("全部一致");
    });
    expect((screen.getByTestId("numbering-repair-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("修复：需二次确认，确认后 POST repair 并反馈修复数量与快照", async () => {
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => screen.getByTestId("numbering-drift-table"));
    fireEvent.click(screen.getByTestId("numbering-repair-btn"));
    // 确认弹窗出现（含快照可回滚说明）
    await waitFor(() => expect(document.querySelector(".ant-modal-confirm")).toBeTruthy());
    expect(document.body.textContent).toContain("确认修复正文编号漂移");
    const okBtn = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".ant-modal-confirm button"),
    ).find((b) => b.textContent!.includes("开始修复"))!;
    expect(okBtn).toBeTruthy();
    fireEvent.click(okBtn);
    await waitFor(() => expect(mockedRepair).toHaveBeenCalledWith("s1"));
    // 修复结果反馈：修复了多少处 + 快照 id
    await waitFor(() => {
      expect(document.body.textContent).toContain("已修复 1 处（共检查 3 章）");
    });
    expect(document.body.textContent).toContain("snap-123".slice(0, 8));
    // 修复响应带回新报告 → 汇总刷新为无漂移
    await waitFor(() => {
      expect(screen.getByTestId("numbering-summary-tag").textContent).toContain("全部一致");
    });
  });

  it("版本历史：列出快照，回滚需确认并携带 snapshot_id，回滚后自动重查", async () => {
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-versions-btn"));
    await waitFor(() => expect(mockedVersions).toHaveBeenCalledWith("s1"));
    await waitFor(() => expect(screen.getByTestId("numbering-versions-table")).toBeTruthy());
    expect(screen.getByTestId("numbering-versions-table").textContent).toContain(
      "snap-456".slice(0, 18),
    );
    fireEvent.click(screen.getByTestId("numbering-rollback-snap-123"));
    await waitFor(() => expect(document.querySelector(".ant-modal-confirm")).toBeTruthy());
    const okBtn = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".ant-modal-confirm button"),
    ).find((b) => b.textContent!.includes("确认回滚"))!;
    fireEvent.click(okBtn);
    await waitFor(() => expect(mockedRollback).toHaveBeenCalledWith("s1", "snap-123"));
    // 回滚后自动重查（校验结果过期）
    await waitFor(() => expect(mockedCheck).toHaveBeenCalledTimes(1));
  });

  it("检查接口失败 → 错误提示（toast 展示异常信息）", async () => {
    mockedCheck.mockRejectedValue(new Error("网络中断") as never);
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => {
      expect(document.body.textContent).toContain("网络中断");
    });
    // 面板仍处于未检查空态（失败不产生假报告）
    expect(screen.getByTestId("numbering-empty")).toBeTruthy();
  });

  it("初始空态：未检查时展示引导文案", () => {
    renderPanel();
    expect(screen.getByTestId("numbering-empty").textContent).toContain("尚未检查");
  });

  /**
   * ✅ R181（2026-11-06）：校验三态 —— 通过 / 不通过 / 无法判定。
   * 旧实现只看 mismatched：skipped 章节被归入「全部一致 / 校验通过」，
   * 用户会把「无法判定」当成「通过」直接导出。
   */
  it("R181：有跳过章节且无漂移 → 无法判定（金色汇总 + 明确提示，不得显示通过）", async () => {
    mockedCheck.mockResolvedValue({
      data: {
        checked: 2,
        mismatched: 0,
        consistent: true,
        sections: [
          { section_id: "sec-a", consistent: true, skipped: false, changed: false, diffs: [] },
          { section_id: "sec-b", consistent: true, skipped: false, changed: false, diffs: [] },
          { section_id: "sec-c", consistent: true, skipped: true, changed: false, diffs: [] },
        ],
      },
    } as never);
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => {
      const tag = screen.getByTestId("numbering-summary-tag").textContent || "";
      expect(tag).toContain("1 章无法判定");
      expect(tag).not.toContain("全部一致");
    });
    expect(screen.getByTestId("numbering-undetermined-hint").textContent).toContain("无法判定");
    await waitFor(() => {
      expect(document.body.textContent).toContain("部分章节无法判定");
    });
  });

  it("R181：未检查到任何章节 → 无法判定，而不是通过", async () => {
    mockedCheck.mockResolvedValue({
      data: { checked: 0, mismatched: 0, consistent: true, sections: [] },
    } as never);
    renderPanel();
    fireEvent.click(screen.getByTestId("numbering-check-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("numbering-summary-tag").textContent).toContain("无法判定");
    });
    expect(screen.getByTestId("numbering-undetermined-hint")).toBeTruthy();
  });
});

// ---------- 源码锚点：注释不虚报 + 页面真实挂载 ----------
// 沿用仓内范式（import.meta.glob 读源码文本，不引 node:fs）
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
);
const apiSource = RAW["api/index.ts"];
const pageSource = RAW["pages/SchemeWorkbenchPage.tsx"];

describe("编号一致性 · 接线源码锚点（R174 F1）", () => {
  it("api/index.ts 注释不再虚报「已接线」，指向真实消费组件", () => {
    // 失真旧注释已订正
    expect(apiSource).not.toContain("2026-10-04 接线）：后端 4 个端点早已实现");
    expect(apiSource).toContain("NumberingConsistencyPanel");
  });

  it("工作台「审核与预检」Tab 挂载 NumberingConsistencyPanel", () => {
    // ✅ 2026-11-04 拆分（G5）：审核 Tab 的 children 迁到
    //    pages/schemeWorkbench/tabs/ReviewTab.tsx。判据语义不变
    //    （「工作台审核 Tab 挂载该面板」），故指向新位置。
    const reviewTab = RAW["pages/schemeWorkbench/tabs/ReviewTab.tsx"];
    expect(reviewTab, "tabs/ReviewTab.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(reviewTab).toContain(`import { NumberingConsistencyPanel }`);
    expect(reviewTab).toContain(`<NumberingConsistencyPanel schemeId={id || ""} />`);
  });
});
