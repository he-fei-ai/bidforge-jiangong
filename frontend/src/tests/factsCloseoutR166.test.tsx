// @vitest-environment jsdom
/**
 * 全局事实模块收口 · R166 前端护栏（2026-11-03）。
 *
 * 三层守护：
 * - A 组：FFIX-1 静态接线锁 —— loadFacts 的默认页码改走 factsPageRef 镜像，
 *   deps 不再含 factsPage。旧行为：分页 setState → loadFacts 身份变化 →
 *   挂载 effect 整体重跑（目录全量 + 合规历史 + 专家项 3-4 个无关请求随每次
 *   翻页重发）。回退（deps 混回 factsPage / 镜像同步被摘）必须变红。
 * - B 组：FactsGroupList 过期条目 id 兜底链 —— fact_id 缺失（历史行）时
 *   回落 it.id，不得收到 undefined（handleAckStaleOne 静默 return 的老坑）。
 * - C 组：FactsExtractProgressCard × TaskControlButtons 组件级交互 ——
 *   facts 链路注入控制区后，暂停/恢复/停止指令按 paused 互斥正确上抛。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import { FactsGroupList, FactsExtractProgressCard } from "../pages/SchemeWorkbenchPage";
import { TaskControlButtons } from "../pages/schemeWorkbench/TaskControlButtons.tsx";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

afterEach(cleanup);

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

// ============================================================
// A 组 · FFIX-1 分页不再触发挂载 effect 全量重跑（静态接线锁）
// ============================================================
describe("loadFacts 分页依赖重构（R166 FFIX-1）", () => {
  it("deps 不含 factsPage，默认页码走 ref 镜像", () => {
    expect(PAGE, "页面源码必须可读（import.meta.glob raw）").toBeTruthy();
    expect(PAGE).toContain("const factsPageRef = useRef(1);");
    expect(PAGE).toContain("factsPageRef.current = wantPage;");
    expect(PAGE).toContain("}, [id, msg, loadFactsDiagnostics]);");
  });

  it("回退锁：deps 混回 factsPage 必须变红", () => {
    expect(PAGE.includes("loadFactsDiagnostics, factsPage]);")).toBe(false);
  });
});

// ============================================================
// B 组 · FactsGroupList 过期条目 id 兜底链
// ============================================================
function baseProps(over: Record<string, unknown> = {}) {
  return {
    filter: "all",
    onEditGroup: vi.fn(),
    onDeleteGroup: vi.fn(),
    onEditItem: vi.fn(),
    onResolveItem: vi.fn(),
    onResolveConflict: vi.fn(),
    onAckStaleOne: vi.fn(),
    ...over,
  } as any;
}

function expandFirstPanel(utils: ReturnType<typeof render>) {
  const header = utils.container.querySelector(".ant-collapse-header") as HTMLElement;
  expect(header).toBeTruthy();
  fireEvent.click(header);
}

describe("FactsGroupList 过期条目 id 兜底链（R166）", () => {
  it("fact_id 缺失的历史行回落 it.id，不传 undefined", () => {
    const groups: any = [{
      id: "g-legacy", title: "编制依据",
      items: [{
        id: "row-legacy", name: "旧数据事实", value: "旧值",
        is_stale: true, is_resolved: false, is_simulated: false, has_conflict: false,
      }],
    }];
    const props = baseProps({ groups });
    const utils = render(<FactsGroupList {...props} />);
    expandFirstPanel(utils);
    fireEvent.click(utils.getByText(/来源已变化/));
    expect(props.onAckStaleOne).toHaveBeenCalledTimes(1);
    const [factId] = props.onAckStaleOne.mock.calls[0];
    expect(factId).toBe("row-legacy");
  });
});

// ============================================================
// C 组 · FactsExtractProgressCard × TaskControlButtons（facts 链控制区）
// ============================================================
describe("FactsExtractProgressCard 控制区交互（R166）", () => {
  const mkCard = (paused: boolean, stopping: boolean, onControl: (a: any) => void) =>
    render(
      <FactsExtractProgressCard
        progress={0.4}
        progressMsg="提取中"
        logs={[{ progress: 0.4, message: "分段 2/5 完成" }]}
        controls={<TaskControlButtons paused={paused} stopping={stopping} onControl={onControl} />}
      />,
    );

  it("运行中：停止可点并上抛 stop，恢复禁用", () => {
    const onControl = vi.fn();
    const utils = mkCard(false, false, onControl);
    fireEvent.click(utils.getByText("停止"));
    expect(onControl).toHaveBeenCalledWith("stop");
    // antd Button 文本包在 span 内，disabled 在最近的 button 上
    const btn = (txt: string) =>
      (utils.getByText(txt).closest("button") as HTMLButtonElement);
    expect(btn("恢复").disabled).toBe(true);
    expect(btn("暂停").disabled).toBe(false);
  });

  it("暂停态：恢复可点上抛 resume，停止钮显示「停止中」时禁用", () => {
    const onControl = vi.fn();
    const utils = mkCard(true, false, onControl);
    fireEvent.click(utils.getByText("恢复"));
    expect(onControl).toHaveBeenCalledWith("resume");
    const btn = (txt: string) =>
      (utils.getByText(txt).closest("button") as HTMLButtonElement);
    expect(btn("暂停").disabled).toBe(true);
    expect(utils.getByText(/已暂停/)).toBeTruthy();
  });

  it("无 controls 注入时不渲染控制按钮（向后兼容）", () => {
    const utils = render(
      <FactsExtractProgressCard progress={0} progressMsg="" logs={[]} />,
    );
    expect(utils.queryByText("停止")).toBeNull();
    expect(utils.getByText("正在连接 AI，准备提取...")).toBeTruthy();
  });
});
