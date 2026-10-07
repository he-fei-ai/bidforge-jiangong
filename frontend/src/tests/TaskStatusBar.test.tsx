// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import { App } from "antd";
import TaskStatusBar, { activityFingerprint } from "../components/TaskStatusBar";
import { systemApi, tasksApi } from "../api";

// jsdom 缺 matchMedia / ResizeObserver，antd 会用到
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

const h = vi.hoisted(() => ({
  SNAPSHOT: {
    server: { version: "1.2.3", uptime: 3600 },
    tasks: {
      running: [
        {
          id: "t-1", task_type: "content_generation", status: "running", progress: 0.42,
          scheme_id: "s1", scheme_name: "基坑方案",
          stats: { done: 2, total: 5, elapsed_ms: 120000 },
        },
      ],
      recent: [
        { id: "t-0", task_type: "outline_generation", status: "completed", scheme_name: "旧方案", updated_at: "2026-09-20T10:00:00" },
      ],
    },
    ai: {
      in_flight: 2, total: 100, calls_today: 42, tokens_today: 120000, avg_duration: 1.5,
      failed: 1, success_rate: 97.6, last_provider: "deepseek", last_model: "deepseek-v3",
      last_ok: true, last_duration: 1.2, last_at: Date.now(),
    },
  },
}));

vi.mock("../api", () => ({
  systemApi: {
    // 轮询快照（SSE 断线退避重连期间 / 回退轮询时使用）
    activity: vi.fn(async () => ({ data: h.SNAPSHOT })),
    // ✅ 2026-09-25 修复：组件已改为「SSE 优先、断线指数退避(3s起)后才回退轮询」，
    // 旧夹具的空生成器会让快照永远到不了组件（轮询最早也要 3s 后才启动，超过
    // waitFor 默认 1s 超时）。现按组件契约 yield 一条 snapshot 消息，走主路径写入。
    activityStream: vi.fn(async function* () {
      yield { event: "snapshot", data: h.SNAPSHOT };
    }),
  },
  tasksApi: {
    control: vi.fn(async () => ({ data: { ok: true, message: "已暂停" } })),
  },
}));

beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); localStorage.clear(); });

function btnByText(container: HTMLElement, text: string): HTMLButtonElement | null {
  const norm = (s: string) => (s || "").replace(/\s+/g, "");
  return (Array.from(container.querySelectorAll("button")).find(
    (b) => norm(b.textContent).includes(norm(text)),
  ) as HTMLButtonElement | undefined) ?? null;
}

describe("TaskStatusBar · 运行态渲染与任务控制", () => {
  it("拉取快照后渲染运行态概览（状态条）", async () => {
    const { container } = render(
      <App><TaskStatusBar collapsed={false} /></App>,
    );
    await waitFor(() => {
      expect(container.textContent || "").toContain("正文生成");
    });
    // 状态条仅展示任务/AI 概览；方案名与服务态在展开后的详情面板中
    expect(container.textContent || "").toContain("42%");
    expect(container.textContent || "").toContain("AI 调用中 ×2");
  });

  it("展开详情面板并暂停任务 → tasksApi.control(taskId, 'pause')", async () => {
    const { container } = render(
      <App><TaskStatusBar collapsed={false} /></App>,
    );
    await waitFor(() => expect(container.textContent || "").toContain("正文生成"));
    fireEvent.click(container.querySelector(".bp-task-bar")!);
    await waitFor(() => expect(document.querySelector(".bp-task-panel")).toBeTruthy());
    // 详情面板含方案名与服务态
    expect(document.body.textContent || "").toContain("基坑方案");
    expect(document.body.textContent || "").toContain("服务 v1.2.3");

    const pauseIcon = document.querySelector('[aria-label="pause-circle"]');
    expect(pauseIcon).toBeTruthy();
    const pauseBtn = pauseIcon!.closest("button") as HTMLButtonElement;
    await act(async () => { fireEvent.click(pauseBtn); });

    expect(tasksApi.control).toHaveBeenCalledWith("t-1", "pause");
  });
});

// =========================================================================
// 文档解析统计段（2026-10-06 新增，对应后端 documents 加法字段）
// =========================================================================
function useSnapshot(patch: Record<string, any>) {
  const snap = { ...structuredClone(h.SNAPSHOT), ...patch };
  vi.mocked(systemApi.activityStream).mockImplementation(async function* () {
    yield { event: "snapshot", data: snap };
  });
  vi.mocked(systemApi.activity).mockImplementation(
    async () => ({ data: snap }) as any);
  return snap;
}

async function renderAndExpand() {
  const { container } = render(
    <App><TaskStatusBar collapsed={false} /></App>,
  );
  await waitFor(() => expect(container.textContent || "").toContain("正文生成"));
  fireEvent.click(container.querySelector(".bp-task-bar")!);
  await waitFor(() => expect(document.querySelector(".bp-task-panel")).toBeTruthy());
}

describe("TaskStatusBar · 文档解析统计", () => {
  it("有失败：展示计数、待解析与失败率", async () => {
    useSnapshot({
      documents: {
        total: 4, parsed: 2, failed: 1, pending: 1, failure_rate: 25.0,
      },
    });
    await renderAndExpand();
    const text = document.body.textContent || "";
    expect(text).toContain("文档解析");
    expect(text).toContain("共 4 份");
    expect(text).toContain("已解析 2");
    expect(text).toContain("待解析 1");
    expect(text).toContain("失败 1 份");
    expect(text).toContain("失败率 25%");
  });

  it("无失败：显绿色「无解析失败」，不出现失败率", async () => {
    useSnapshot({
      documents: { total: 3, parsed: 3, failed: 0, pending: 0, failure_rate: 0.0 },
    });
    await renderAndExpand();
    const text = document.body.textContent || "";
    expect(text).toContain("无解析失败");
    expect(text).not.toContain("失败率");
  });

  it("旧后端不带 documents / total=0：整段不渲染（向后兼容）", async () => {
    useSnapshot({ documents: undefined });
    await renderAndExpand();
    expect(document.body.textContent || "").not.toContain("文档解析");

    useSnapshot({ documents: { total: 0, failure_rate: null } });
    await cleanup();
    await renderAndExpand();
    const panels = document.querySelectorAll(".bp-task-panel");
    const last = panels[panels.length - 1];
    expect(last.textContent || "").not.toContain("文档解析");
  });

  it("文档计数参与指纹：计数变化产生不同指纹（否则新快照被去重丢弃）", () => {
    const snap: any = structuredClone(h.SNAPSHOT);
    snap.documents = { total: 4, parsed: 2, failed: 1, pending: 1 };
    const fp1 = activityFingerprint(snap);
    snap.documents = { total: 4, parsed: 3, failed: 0, pending: 1 };
    expect(activityFingerprint(snap)).not.toBe(fp1);
  });
});
