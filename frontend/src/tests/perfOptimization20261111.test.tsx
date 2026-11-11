// @vitest-environment jsdom
/**
 * 前端性能治理护栏（2026-11-11）
 * ==============================
 * 目标不是"测 FPS"（jsdom 无合成器），而是把性能修复锁成**可观测、可裁决**
 * 的行为契约：防抖窗口、rAF 合帧次数、日志上限、草稿订阅隔离、树数据引用稳定、
 * 单一 resize 监听、打包反向依赖斩断。任何一项被改回旧实现都必须当场变红。
 *
 * 注：ui.tsx / useOutlineEditTreeRender.tsx 在 jsdom 的模块加载阶段会拖住
 * vitest（既有环境限制），故这两处改用 vite `?raw` 源码级棘轮断言 ——
 * 不执行被读模块、零新增依赖，与 R212 的跨端护栏同款手段。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { useDebouncedValue, DEFAULT_INPUT_DEBOUNCE_MS } from "../utils/perf/debounce";
import { appendBoundedLog, LIVE_LOG_MAX } from "../utils/perf/logBuffer";
import { createSseBatcher } from "../utils/sseBatcher";
import {
  getRenameDraft,
  setRenameDraft,
  subscribeRenameDraft,
  useRenameDraft,
} from "../utils/perf/renameDraftStore";
import { computePromptVariablesFallback } from "../utils/perf/computeWorker";
import { diffPromptVariables, extractPromptVariables } from "../utils/promptVariables";
import { subscribeResizePhase } from "../utils/perf/resizeCoordinator";
import modalSource from "../components/OutlineLibraryEditModal.tsx?raw";
import uiSource from "../utils/ui.tsx?raw";
import editTreeSource from "../pages/schemeWorkbench/useOutlineEditTreeRender.tsx?raw";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  act(() => setRenameDraft(""));
});

describe("参数输入联动：300ms 防抖 + 结果区隔离", () => {
  it("连续输入只在静默 300ms 后更新一次结果区，卸载后无残留 timer", () => {
    vi.useFakeTimers();
    const { result, rerender, unmount } = renderHook(({ v }) => useDebouncedValue(v), {
      initialProps: { v: "a" },
    });
    expect(DEFAULT_INPUT_DEBOUNCE_MS).toBe(300);
    rerender({ v: "ab" });
    rerender({ v: "abc" });
    act(() => vi.advanceTimersByTime(299));
    expect(result.current).toBe("a");
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe("abc");

    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("重命名草稿更新只通知订阅者，不重渲父组件", () => {
    let parentRenders = 0;
    function RenameInputView() {
      const draft = useRenameDraft();
      return <span data-testid="draft">{draft}</span>;
    }
    function Parent() {
      parentRenders += 1;
      return (
        <div>
          <RenameInputView />
          <span>static</span>
        </div>
      );
    }
    render(<Parent />);
    const before = parentRenders;
    act(() => setRenameDraft("第一章（改）"));
    expect(screen.getByTestId("draft").textContent).toBe("第一章（改）");
    expect(parentRenders).toBe(before);
    expect(getRenameDraft()).toBe("第一章（改）");

    const cb = vi.fn();
    const off = subscribeRenameDraft(cb);
    act(() => setRenameDraft("第二章"));
    expect(cb).toHaveBeenCalledTimes(1);
    off();
    act(() => setRenameDraft("第三章"));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("编辑树构造 deps 不得重新引入逐字符 renamingValue（源码棘轮）", () => {
    expect(editTreeSource).toContain("OutlineRenameInput");
    const deps = editTreeSource.match(
      /\[moveFlags, moveNode, renameNode, failedOutlineChapters, renamingKey[^\]]*\]/,
    );
    expect(deps).not.toBeNull();
    expect(deps![0]).not.toContain("renamingValue");
    // 切换重命名节点仍必须触发重建，防止被优化成"永不更新"
    expect(deps![0]).toContain("renamingKey");
  });

  it("Worker 与主线程 fallback 共用同一份变量扫描实现", () => {
    const req = { current: "标题 {scheme_name} __DEPTH__ {SHARED_X}", baseline: "{scheme_name}" };
    const got = computePromptVariablesFallback(req);
    expect(got.variables).toEqual(extractPromptVariables(req.current));
    expect(got.diff).toEqual(diffPromptVariables(req.current, req.baseline));
    expect(got.variables.some((v) => v.name === "SHARED_X")).toBe(false);
  });
});

describe("SSE 流式更新：合帧 + 有界缓冲", () => {
  it("同帧多次 schedule 只 flush 一次，且按 key 只保留最新值", () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.push(cb);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const batcher = createSseBatcher();
    let value = "init";
    batcher.schedule("progress", () => {
      value = "p1";
    });
    batcher.schedule("progress", () => {
      value = "p2";
    });
    batcher.schedule("logs", () => {
      value += "|logs";
    });
    expect(frames.length).toBe(1);
    act(() => frames.shift()!(0));
    expect(value).toBe("p2|logs");

    // stop 后挂起任务必须丢弃（卸载后不 setState）
    batcher.schedule("progress", () => {
      value = "after-stop";
    });
    batcher.stop();
    act(() => frames.shift()!(0));
    expect(value).toBe("p2|logs");
  });

  it("日志写入端有上限、保留最近 N 条、不修改原数组", () => {
    const list = [0, 1, 2, 3, 4];
    expect(appendBoundedLog(list, 99, 3)).toEqual([3, 4, 99]);
    expect(list).toEqual([0, 1, 2, 3, 4]);
    const long = Array.from({ length: LIVE_LOG_MAX + 50 }, (_, i) => i);
    const next = appendBoundedLog<number | string>(long, "tail");
    expect(next.length).toBe(LIVE_LOG_MAX);
    expect(next[next.length - 1]).toBe("tail");
    expect(next[0]).toBe(51);
  });
});

describe("尺寸变化 / 打包", () => {
  it("全局只有一个 resize 监听，最后一个订阅者离开会释放", () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const off1 = subscribeResizePhase(vi.fn());
    const off2 = subscribeResizePhase(vi.fn());
    expect(addSpy.mock.calls.filter((c) => c[0] === "resize")).toHaveLength(1);
    off1();
    expect(removeSpy.mock.calls.filter((c) => c[0] === "resize")).toHaveLength(0);
    off2();
    expect(removeSpy.mock.calls.filter((c) => c[0] === "resize")).toHaveLength(1);
  });

  it("断点宽度改为单一外部 store（源码棘轮：只挂一个监听）", () => {
    expect(uiSource).toContain("useSyncExternalStore(bpSubscribe, bpGetWidth");
    expect(uiSource.match(/addEventListener\("resize"/g)).toHaveLength(1);
    expect(uiSource).toContain('removeEventListener("resize", bpOnResize)');
  });

  it("目录库弹窗不再反向静态依赖工作台巨型模块", () => {
    expect(modalSource).toContain('from "../pages/schemeWorkbench/outlineTreeModel"');
    expect(modalSource).not.toContain('from "../pages/SchemeWorkbenchPage"');
  });
});
