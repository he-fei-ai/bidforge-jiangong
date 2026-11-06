// @vitest-environment jsdom
/**
 * ✅ R181 N1（2026-11-06）：目录版本面板回归。
 *
 * 后端 outline-versions 列表 / 回退在 R179 已就绪，但前端没有入口：
 * 用户看不到「自动确认前的 5 条快照」，也无法回退。本文件锁定：
 *   ① 挂载即拉取最近 5 条，展示时间 / 来源 / 章节数；
 *   ② 回退必须二次确认，确认后携带 snapshot_id；
 *   ③ 回退成功后刷新列表并通知页面重新加载目录状态；
 *   ④ 回退缓存失效由后端强制完成，前端不再重复造逻辑。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { App as AntdApp } from "antd";
import { OutlineVersionsPanel } from "../pages/schemeWorkbench/OutlineVersionsPanel";
import { sectionsApi } from "../api";

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
    outlineVersions: vi.fn(),
    restoreOutlineVersion: vi.fn(),
  },
}));

const mockedVersions = vi.mocked(sectionsApi.outlineVersions);
const mockedRestore = vi.mocked(sectionsApi.restoreOutlineVersion);

const SNAPSHOTS = [
  {
    snapshot_id: "snap-1",
    type: "outline_auto_confirm",
    created_at: "2026-11-06T10:00:00",
    node_count: 12,
    has_content: true,
  },
  {
    snapshot_id: "snap-2",
    type: "outline_rollback_undo",
    created_at: "2026-11-05T09:00:00",
    node_count: 10,
    has_content: false,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedVersions.mockResolvedValue({ data: { ok: true, snapshots: SNAPSHOTS } } as never);
  mockedRestore.mockResolvedValue({ data: { ok: true } } as never);
});

afterEach(() => cleanup());

function renderPanel(onRestored = vi.fn()) {
  const msg = { success: vi.fn(), error: vi.fn() };
  render(
    <AntdApp>
      <OutlineVersionsPanel
        schemeId="s1"
        msg={msg as unknown as import("antd/es/message/interface").MessageInstance}
        onRestored={onRestored}
      />
    </AntdApp>,
  );
  return { onRestored, msg };
}

describe("OutlineVersionsPanel · 目录版本（R181 N1）", () => {
  it("挂载即拉取 5 条快照，展示时间、来源与章节数", async () => {
    renderPanel();
    await waitFor(() => expect(mockedVersions).toHaveBeenCalledWith("s1", 5));
    await waitFor(() => expect(screen.getAllByTestId("outline-version-item")).toHaveLength(2));
    const panel = screen.getByTestId("outline-versions-panel");
    expect(panel.textContent).toContain("自动确认前");
    expect(panel.textContent).toContain("回退前（可撤销）");
    expect(panel.textContent).toContain("12 个章节");
    expect(panel.textContent).toContain("含正文");
  });

  it("回退需二次确认，确认后携带 snapshot_id 并刷新列表与页面状态", async () => {
    const { onRestored } = renderPanel();
    await waitFor(() => expect(screen.getAllByTestId("outline-version-restore")).toHaveLength(2));
    fireEvent.click(screen.getAllByTestId("outline-version-restore")[0]);
    await waitFor(() => expect(document.querySelector(".ant-popover")).toBeTruthy());
    const okBtn = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".ant-popover button"),
    ).find((b) => b.textContent!.includes("确认回退"));
    expect(okBtn).toBeTruthy();
    fireEvent.click(okBtn!);
    await waitFor(() => expect(mockedRestore).toHaveBeenCalledWith("s1", "snap-1"));
    // 回退后重新拉取列表（快照会新增「回退前」一条）
    await waitFor(() => expect(mockedVersions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onRestored).toHaveBeenCalledTimes(1));
  });

  it("加载失败 → 错误提示且不回退", async () => {
    mockedVersions.mockRejectedValue(new Error("网络中断") as never);
    const { msg } = renderPanel();
    await waitFor(() => expect(msg.error).toHaveBeenCalled());
    expect(screen.queryByTestId("outline-version-item")).toBeNull();
  });
});
