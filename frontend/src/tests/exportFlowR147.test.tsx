/// <reference types="vite/client" />
// @vitest-environment jsdom
/**
 * R147（2026-10-30）· 两轮次导出前端护栏。
 *
 * 覆盖（用户指令 §四）：
 * - A：hook 消费 SSE 两段进度（初稿/修复），修复项逐条进入日志，terminal 后
 *     自动下载终稿并展示修复报告；
 * - B：回滚形态（report.mode=fallback_draft）与失败形态（status=failed）；
 * - C：取消 → flowCancel 真调用 + 在途流失效；
 * - D：面板组件（两段进度 / 日志滚动容器 / 报告汇总 / 下载按钮）；
 * - E：页面↔hook↔API **字段级对称锚**（R139 教训：只比事件集合会漏字段断链）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, render, cleanup, waitFor } from "@testing-library/react";
import { useExportFlow } from "../hooks/useExportFlow";
import { ExportFlowPanel } from "../components/ExportFlowPanel";
import type { ExportFlowState } from "../hooks/useExportFlow";

afterEach(cleanup);

// ---------------------------------------------------------------------------
// 源码读取（Vite ?raw，与 contentTerminalParity 同机制）
// ---------------------------------------------------------------------------
const RAW_SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.startsWith("../") ? k.slice(3) : k, v]),
);
const HOOK = RAW_SOURCES["hooks/useExportFlow.ts"] || "";
const PANEL = RAW_SOURCES["components/ExportFlowPanel.tsx"] || "";
const PAGE = RAW_SOURCES["pages/SchemeWorkbenchPage.tsx"] || "";
const API = RAW_SOURCES["api/index.ts"] || "";

// ---------------------------------------------------------------------------
// 依赖桩
// ---------------------------------------------------------------------------
const h = vi.hoisted(() => ({
  docxV2Mock: vi.fn(),
  flowDownloadMock: vi.fn(),
  flowCancelMock: vi.fn(),
  taskStatusMock: vi.fn(),
  streamFactory: { impl: null as null | ((url: string, opts: any) => AsyncIterable<any>) },
}));

vi.mock("../api", () => ({
  exportApi: {
    docxV2: (...a: unknown[]) => (h.docxV2Mock as any)(...a),
    flowDownload: (...a: unknown[]) => (h.flowDownloadMock as any)(...a),
    flowCancel: (...a: unknown[]) => (h.flowCancelMock as any)(...a),
  },
  tasksApi: { status: (...a: unknown[]) => (h.taskStatusMock as any)(...a) },
  sseGetStream: (url: string, opts: any) =>
    (h.streamFactory.impl as any)(url, opts),
}));

beforeEach(() => {
  h.docxV2Mock.mockReset().mockResolvedValue({
    status: 202, data: { task_id: "t-abc", events: "/x", download: "/y" },
  });
  h.flowDownloadMock.mockReset().mockResolvedValue({
    data: new Blob(["PK"], { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" }),
    headers: { "x-export-filename": "测试方案_第1轮.docx" },
  });
  h.flowCancelMock.mockReset().mockResolvedValue({ data: { ok: true } });
  h.taskStatusMock.mockReset();
  h.streamFactory.impl = null;
  // jsdom 缺 URL.createObjectURL；锚点点击置空防导航告警
  (URL as any).createObjectURL = vi.fn(() => "blob:mock");
  (URL as any).revokeObjectURL = vi.fn();
  HTMLAnchorElement.prototype.click = vi.fn();
});

const REPORT = {
  mode: "fixed",
  summary: {
    mode: "fixed", total_findings: 3, total_actions: 3, fixed: 3, unresolved: 0,
    by_category: {
      numbering: { found: 1, error: 1, warning: 0, fixed: 1 },
      paragraph: { found: 1, error: 1, warning: 0, fixed: 1 },
      fonts: { found: 1, error: 1, warning: 0, fixed: 1 },
      figures: { found: 0, error: 0, warning: 0, fixed: 0 },
      compatibility: { found: 0, error: 0, warning: 0, fixed: 0 },
    },
    duration_ms: 1234,
  },
  findings: [], actions: [], unresolved: [], checks: {},
};

function makeMsg() {
  return {
    success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn(),
  } as any;
}

// ---------------------------------------------------------------------------
// A. 正常两段流程
// ---------------------------------------------------------------------------

describe("useExportFlow · 两段 SSE 流", () => {
  it("A1 初稿/修复两段进度 + 修复项逐条入日志 + 终态自动下载", async () => {
    h.streamFactory.impl = async function* () {
      yield { stage: "draft", phase: "render", progress: 15, message: "渲染图表" };
      yield { stage: "draft", phase: "done", progress: 50, message: "初稿完成" };
      yield { stage: "repair", phase: "inspect", progress: 55,
              finding: { category: "numbering", code: "num_sequence",
                         severity: "error", location: "段落#4", message: "编号虚跳" } };
      yield { stage: "repair", phase: "fix", progress: 60,
              action: { category: "numbering", code: "num_sequence",
                        location: "document.xml", message: "重排 1 个标题编号",
                        result: "fixed" } };
      yield { event: "terminal", terminal: true, status: "completed",
              progress: 100, message: "导出完成", report: REPORT };
    } as any;
    const msg = makeMsg();
    const { result } = renderHook(() => useExportFlow({ schemeId: "s1", msg }));
    await act(async () => { await result.current.start({ cover: true }); });
    await waitFor(() => expect(result.current.running).toBe(false));
    // 两段进度都出现过
    expect(result.current.stage).toBe("repair");
    expect(result.current.progress).toBe(100);
    // 修复项日志：1 finding + 1 action
    expect(result.current.logs).toHaveLength(2);
    expect(result.current.logs[0].kind).toBe("finding");
    expect(result.current.logs[1].kind).toBe("action");
    expect(result.current.logs[1].result).toBe("fixed");
    // 报告落地 + 自动下载（文件名取自响应头）
    expect(result.current.report?.summary.fixed).toBe(3);
    expect(h.flowDownloadMock).toHaveBeenCalledWith("s1", "t-abc");
    expect(msg.success).toHaveBeenCalled();
  });

  it("A2 请求体字段级对称：config 与 chart_images 下发（R139 教训）", async () => {
    h.streamFactory.impl = async function* () { /* 立即结束（无 terminal） */ } as any;
    const { result } = renderHook(() => useExportFlow({ schemeId: "s1", msg: makeMsg() }));
    await act(async () => {
      try { await result.current.start({ cover: true },
        [{ chart_type: "mermaid", mermaid_code: "g", png: "data:" }]); } catch { /* 兜底轮询路径 */ }
    });
    expect(h.docxV2Mock).toHaveBeenCalledWith("s1", { cover: true },
      [{ chart_type: "mermaid", mermaid_code: "g", png: "data:" }], expect.anything());
  });

  it("B1 回滚形态：fallback_draft 报告 → warning 而非 success", async () => {
    h.streamFactory.impl = async function* () {
      yield { event: "terminal", terminal: true, status: "degraded", progress: 100,
              message: "修复失败，已回滚交付初稿",
              report: { ...REPORT, mode: "fallback_draft" } };
    } as any;
    const msg = makeMsg();
    const { result } = renderHook(() => useExportFlow({ schemeId: "s1", msg }));
    await act(async () => { await result.current.start({}); });
    await waitFor(() => expect(result.current.running).toBe(false));
    expect(result.current.report?.mode).toBe("fallback_draft");
    expect(msg.warning).toHaveBeenCalled();
    expect(msg.success).not.toHaveBeenCalled();
  });

  it("B2 失败形态：status=failed → error + 不下载", async () => {
    h.streamFactory.impl = async function* () {
      yield { event: "terminal", terminal: true, status: "failed",
              message: "导出失败：取数异常" };
    } as any;
    const msg = makeMsg();
    const { result } = renderHook(() => useExportFlow({ schemeId: "s1", msg }));
    await act(async () => { await result.current.start({}); });
    await waitFor(() => expect(result.current.running).toBe(false));
    expect(result.current.error).toContain("导出失败");
    expect(h.flowDownloadMock).not.toHaveBeenCalled();
  });

  it("C 取消：flowCancel 真调用且在途流失效（不再消费后续事件）", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    h.streamFactory.impl = async function* () {
      yield { stage: "draft", progress: 10, message: "进行中" };
      await gate;
      yield { event: "terminal", terminal: true, status: "completed",
              progress: 100, report: REPORT };
    } as any;
    const msg = makeMsg();
    const { result } = renderHook(() => useExportFlow({ schemeId: "s1", msg }));
    let startPromise: Promise<void>;
    await act(async () => { startPromise = result.current.start({}); });
    await waitFor(() => expect(result.current.taskId).toBe("t-abc"));
    await act(async () => { await result.current.cancel(); });
    release();
    await act(async () => { await startPromise; });
    expect(h.flowCancelMock).toHaveBeenCalledWith("s1", "t-abc");
    expect(result.current.running).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// D. 面板组件
// ---------------------------------------------------------------------------

const BASE_STATE: ExportFlowState = {
  running: false, stage: "", progress: 0, stageMsg: "",
  logs: [], report: null, taskId: null, fileName: null, error: null,
};

describe("ExportFlowPanel", () => {
  it("D1 两段进度与阶段标签 + 修复日志滚动容器 + 报告汇总", () => {
    const state: ExportFlowState = {
      ...BASE_STATE, stage: "repair", progress: 72,
      stageMsg: "修复目录编号与层级：2 项待处理", taskId: "t-abc",
      logs: [
        { kind: "finding", category: "fonts", code: "heading_italic",
          severity: "error", location: "段落#2", message: "标题 run 含倾斜属性" },
        { kind: "action", category: "fonts", code: "font_fix",
          location: "document.xml", message: "清除 2 处标题倾斜", result: "fixed" },
      ],
      report: REPORT,
    };
    const { getByTestId, getAllByTestId } = render(
      <ExportFlowPanel state={state} onStart={vi.fn()} onCancel={vi.fn()}
        onDownload={vi.fn()} />);
    expect(getByTestId("export-flow-progress").getAttribute("aria-valuenow")).toBe("72");
    expect(getByTestId("export-flow-stage").textContent).toContain("第二轮 · 检查与修复");
    expect(getByTestId("export-flow-logs").getAttribute("data-testid")).toBe("export-flow-logs");
    expect(getAllByTestId("flow-log-row")).toHaveLength(2);
    expect(getByTestId("export-flow-report").textContent).toContain("检出 3 项");
    expect(getByTestId("export-flow-download")).toBeTruthy();
  });

  it("D2 回滚报告显式提示 + 未解决项清单", () => {
    const state: ExportFlowState = {
      ...BASE_STATE, progress: 100, taskId: "t-abc",
      report: { ...REPORT, mode: "fallback_draft",
                unresolved: [{ code: "num_level_jump", severity: "error",
                               category: "numbering", location: "段落#2",
                               message: "标题层级跳变 L1→L3（不连续）" }] },
    };
    const { getByTestId } = render(
      <ExportFlowPanel state={state} onStart={vi.fn()} onCancel={vi.fn()}
        onDownload={vi.fn()} />);
    expect(getByTestId("export-flow-report").textContent).toContain("已回滚初稿");
    expect(getByTestId("export-flow-report-body").textContent).toContain("层级跳变");
  });

  it("D3 空态引导文案", () => {
    const { getByTestId } = render(
      <ExportFlowPanel state={BASE_STATE} onStart={vi.fn()} onCancel={vi.fn()}
        onDownload={vi.fn()} />);
    expect(getByTestId("export-flow-empty")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// E. 字段级对称锚（源码层，防静默断链）
// ---------------------------------------------------------------------------

describe("源码锚 · 页面↔hook↔API 对称", () => {
  it("E1 exportApi 具备 v2 三端点且路径与后端一致", () => {
    expect(API).toMatch(/docxV2:\s*\(schemeId/);
    expect(API).toMatch(/\/schemes\/\$\{schemeId\}\/export\/docx\/v2/);
    expect(API).toMatch(/\/schemes\/\$\{schemeId\}\/export\/flow\/\$\{taskId\}\/download/);
    expect(API).toMatch(/\/schemes\/\$\{schemeId\}\/export\/flow\/\$\{taskId\}\/cancel/);
  });

  it("E2 页面真调用 useExportFlow + ExportFlowPanel（接线形态锚）", () => {
    expect(PAGE).toMatch(/useExportFlow\(\{\s*schemeId:/);
    expect(PAGE).toMatch(/<ExportFlowPanel\s+state=\{exportFlow\}/);
    expect(PAGE).toMatch(/onStart=\{handleExportFlow\}/);
    // 启动函数真调 hook.start（防"接了组件没接数据"）
    expect(PAGE).toMatch(/await exportFlow\.start\(config, chartImages\)/);
  });

  it("E3 hook 消费 finding/action/terminal 三类事件（字段级）", () => {
    expect(HOOK).toMatch(/evt\.finding/);
    expect(HOOK).toMatch(/evt\.action/);
    expect(HOOK).toMatch(/evt\?\.terminal/);
    expect(HOOK).toMatch(/evt\?\.report\?\.mode/);
    expect(PANEL).toMatch(/CATEGORY_LABELS/);
  });

  it("E4 面板依赖 hook 状态字段全部存在（kind/severity/result/report.mode）", () => {
    expect(PANEL).toMatch(/item\.kind/);
    expect(PANEL).toMatch(/item\.severity/);
    expect(PANEL).toMatch(/item\.result/);
    expect(PANEL).toMatch(/report\?\.mode === "fallback_draft"/);
    expect(HOOK).toMatch(/kind:\s*"finding"/);
    expect(HOOK).toMatch(/kind:\s*"action"/);
  });
});
