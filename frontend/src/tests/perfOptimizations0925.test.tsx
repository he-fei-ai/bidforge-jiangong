// @vitest-environment jsdom
/**
 * 前端性能优化 · 回归测试（2026-09-25 第二轮）
 * =============================================
 * 每条用例都对应一个已定位的卡顿/泄漏根因，锁定「优化生效且不退化」：
 *
 *  1. SseBatcher：同 key 只保留最新值（帧内合并）、flushNow 立即刷、
 *     stop 丢弃未刷任务且之后不再接受调度；
 *  2. activityFingerprint：内容未变（即使引用不同）指纹相同 → 跳过重渲；
 *     进度按整数百分比、耗时按整秒取粒度；AI 计数/版本变化必须改变指纹；
 *  3. treeFingerprint：结构/状态/字数变化改变指纹，content 大字段不参与；
 *  4. FactsGroupList / BaItemFlatList：必须是 memo 组件（防后续把 memo 摘掉），
 *     且统计口径与去重查表结果与旧实现一致（防「优化时改错语义」）；
 *  5. useAiConfigGovernance.setRefresh 跨重渲保持（回归：普通对象实现会让
 *     刷新回调静默失效，保存后列表不刷新）；
 *  6. Markdown 样式只注入一份（回归：旧实现每个实例各注入一份 <style>）。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { createSseBatcher } from "../utils/sseBatcher";
import { activityFingerprint } from "../components/TaskStatusBar";
import { treeFingerprint, FactsGroupList, type TreeNode } from "../pages/SchemeWorkbenchPage";
import BaItemFlatList from "../components/BaItemFlatList";
import { useAiConfigGovernance } from "../hooks/useAiConfigGovernance";
import MarkdownRenderer from "../components/MarkdownRenderer";

// 隔离网络：hooks 会调用 aiApi，测试只关心 setRefresh 的引用稳定性
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    aiApi: {
      ...actual.aiApi,
      setEnv: vi.fn().mockResolvedValue({ data: { active_env: "prod", hint: "" } }),
      getEnv: vi.fn().mockResolvedValue({ data: { active_env: "prod", envs: [] } }),
      getSceneRoutes: vi.fn().mockResolvedValue({ data: { items: [] } }),
      configAuditLogs: vi.fn().mockResolvedValue({ data: { items: [] } }),
      getRuntime: vi.fn().mockResolvedValue({ data: { disabled_providers: [] } }),
      setDisabledProviders: vi.fn().mockResolvedValue({ data: { count: 0 } }),
      updateSceneRoute: vi.fn().mockResolvedValue({ data: {} }),
    },
  };
});

// jsdom 缺 matchMedia / ResizeObserver，antd 组件（Tooltip / Progress）会用到
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

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

// ============================================================
describe("SseBatcher（高频 setState 的 rAF 合帧）", () => {
  it("同 key 调度多次只保留最新值，flush 后按序执行一次", () => {
    const b = createSseBatcher();
    const calls: string[] = [];
    b.schedule("k", () => calls.push("a"));
    b.schedule("k", () => calls.push("b"));
    b.schedule("k", () => calls.push("c"));
    expect(calls).toEqual([]); // 尚未到下一帧
    b.flushNow();
    expect(calls).toEqual(["c"]);
    b.stop();
  });

  it("不同 key 互不覆盖，flush 后全部执行", () => {
    const b = createSseBatcher();
    const calls: string[] = [];
    b.schedule("k1", () => calls.push("1"));
    b.schedule("k2", () => calls.push("2"));
    b.schedule("k1", () => calls.push("1-last"));
    b.flushNow();
    expect(calls.sort()).toEqual(["1-last", "2"]);
    b.stop();
  });

  it("stop 后丢弃未刷任务，且不再接受新调度", () => {
    const b = createSseBatcher();
    let n = 0;
    b.schedule("k", () => { n += 1; });
    b.stop();
    b.schedule("k", () => { n += 1; }); // stop 后应被忽略
    expect(n).toBe(0);
    b.flushNow();
    expect(n).toBe(0);
  });
});

// ============================================================
describe("activityFingerprint（活动快照去重）", () => {
  const snap = (over: Record<string, any> = {}) => ({
    server: { version: "1.0.0", uptime: 100 },
    tasks: {
      running: [{ id: "t1", task_type: "content_generation", status: "running", progress: 0.37, elapsed: 41.6 }],
      recent: [],
    },
    ai: { in_flight: 2, calls_today: 10, tokens_today: 5000, success_rate: 0.9, last_at: 1, last_ok: true },
    ...over,
  });

  it("空值返回空串", () => {
    expect(activityFingerprint(null)).toBe("");
    expect(activityFingerprint(undefined)).toBe("");
  });

  it("内容未变（即使整体引用不同）指纹相同 → 可安全跳过重渲", () => {
    expect(activityFingerprint(snap())).toBe(activityFingerprint(snap()));
    expect(activityFingerprint(snap())).toBe(activityFingerprint(JSON.parse(JSON.stringify(snap()))));
  });

  it("进度按整数百分比取粒度：同一百分比区间内的浮点抖动视为无变化", () => {
    const mk = (p: number) =>
      snap({ tasks: { running: [{ id: "t1", status: "running", progress: p }], recent: [] } });
    // 0.371 / 0.374 都落在 37%（Math.round(37.1)=Math.round(37.4)=37）
    expect(activityFingerprint(mk(0.371))).toBe(activityFingerprint(mk(0.374)));
    expect(activityFingerprint(mk(0.371))).not.toBe(activityFingerprint(mk(0.38)));
  });

  it("耗时按整秒取粒度：同秒内的浮点抖动视为无变化", () => {
    const a = snap({ tasks: { running: [{ id: "t1", status: "running", progress: 0.4, elapsed: 41.1 }], recent: [] } });
    const b = snap({ tasks: { running: [{ id: "t1", status: "running", progress: 0.4, elapsed: 41.9 }], recent: [] } });
    const c = snap({ tasks: { running: [{ id: "t1", status: "running", progress: 0.4, elapsed: 42.1 }], recent: [] } });
    expect(activityFingerprint(a)).toBe(activityFingerprint(b));
    expect(activityFingerprint(a)).not.toBe(activityFingerprint(c));
  });

  it("反例：状态 / 任务集合 / AI 计数 / 版本变化必须改变指纹（防过度去重导致数据陈旧）", () => {
    const base = activityFingerprint(snap());
    expect(activityFingerprint(snap({ tasks: { running: [{ id: "t1", status: "completed", progress: 1 }], recent: [] } }))).not.toBe(base);
    expect(activityFingerprint(snap({ tasks: { running: [], recent: [{ id: "t1", status: "completed" }] } }))).not.toBe(base);
    expect(activityFingerprint(snap({ ai: { in_flight: 3 } }))).not.toBe(base);
    expect(activityFingerprint(snap({ server: { version: "1.0.1" } }))).not.toBe(base);
  });
});

// ============================================================
describe("treeFingerprint（目录树去重，避免 3s 轮询空转重渲）", () => {
  const tree = (over: Record<string, any> = {}): TreeNode[] => [
    {
      key: "k1", title: "第一章", level: 1, status: "generated",
      word_count: 100, word_budget: 1500, content: "很长的正文内容",
      children: [{ key: "k1-1", title: "第一节", level: 2, status: "running", word_count: 0, word_budget: 800 }],
      ...over,
    },
  ];

  it("结构不变时指纹稳定（内容大字段不参与，轮询无变化即可跳过）", () => {
    expect(treeFingerprint(tree())).toBe(treeFingerprint(tree()));
    // content 变化不影响指纹（轻量轮询本就不返回正文）
    expect(treeFingerprint(tree({ content: "完全不同的另一份正文" }))).toBe(treeFingerprint(tree()));
  });

  it("反例：字数 / 状态 / 顺序变化必须改变指纹", () => {
    const base = treeFingerprint(tree());
    expect(treeFingerprint(tree({ word_count: 101 }))).not.toBe(base);
    expect(treeFingerprint(tree({ status: "failed" }))).not.toBe(base);
    expect(treeFingerprint([...tree(), tree()[0]] as TreeNode[])).not.toBe(base);
  });

  // ✅ 2026-09-29：facts_stale 是用户可感知的状态（目录树上的「事实已变更」徽标）。
  // 若指纹不含它，3s 轻量轮询会因指纹相同而保留旧树 —— 事实刚变更后
  // 徽标要等下一次完整 load() 才出现，表现为「后端已标、界面没显示」。
  it("反例：事实变更标记变化必须改变指纹（避免轮询吞掉刚出现的徽标）", () => {
    const base = treeFingerprint(tree());
    expect(treeFingerprint(tree({ facts_stale: 1 }))).not.toBe(base);
    expect(treeFingerprint(tree({ facts_stale: true }))).not.toBe(base);
    expect(treeFingerprint(tree({ facts_stale: false }))).toBe(base);
  });

  it("空树安全", () => {
    expect(treeFingerprint([])).toBe("");
    expect(treeFingerprint([])).toBe(treeFingerprint([]));
  });
});

// ============================================================
describe("FactsGroupList / BaItemFlatList memo 化", () => {
  const noop = () => {};

  it("FactsGroupList 是 memo 组件，且单次遍历统计口径正确（防优化时改错语义）", () => {
    expect((FactsGroupList as any).$$typeof).toBe(Symbol.for("react.memo"));

    const groups = [
      {
        id: "g1",
        title: "工程量",
        items: [
          { id: 1, name: "土方量", value: "100m³", is_simulated: true, has_conflict: false, is_resolved: true },
          { id: 2, name: "混凝土", value: "50m³", is_simulated: true, has_conflict: true, is_resolved: false },
          { id: 3, name: "钢筋", value: "20t", is_simulated: false, has_conflict: false, is_resolved: false },
        ],
      },
    ];
    render(
      <FactsGroupList
        groups={groups}
        filter="all"
        onEditGroup={noop}
        onDeleteGroup={noop}
        onEditItem={noop}
        onResolveItem={noop}
        onResolveConflict={noop}
      />
    );
    // 3 条条目；模拟 2 条；矛盾 1 条；未处理 2 条（id 2、3）
    expect(document.body.textContent).toContain("3 项");
    expect(document.body.textContent).toContain("2");
    expect(document.body.textContent).toContain("🔸1");
  });

  it("BaItemFlatList 是 memo 组件（父页无关重渲可整体跳过）", () => {
    expect((BaItemFlatList as any).$$typeof).toBe(Symbol.for("react.memo"));
  });
});

// ============================================================
describe("useAiConfigGovernance.setRefresh 跨重渲保持", () => {
  it("多次重渲后「切换环境」仍能触发刷新回调（回归：普通对象实现会让刷新静默失效）", async () => {
    const refreshFn = vi.fn().mockResolvedValue(undefined);
    const noopMsg = { success: () => {}, warning: () => {}, info: () => {}, error: () => {} };

    function Probe({ n: nProp }: { n: number }) {
      const { setRefresh, handleEnvChange } = useAiConfigGovernance(noopMsg);
      React.useEffect(() => {
        // AIConfigPage 的真实用法：只在挂载时注入一次刷新回调
        setRefresh(refreshFn);
      }, [setRefresh]);
      return (
        <div>
          <button data-testid="env" onClick={() => void handleEnvChange("prod")}>
            切换环境
          </button>
          <span data-testid="n">{nProp}</span>
        </div>
      );
    }

    function Harness() {
      const [n, setN] = React.useState(0);
      return (
        <div>
          <Probe n={n} />
          <button data-testid="bump" onClick={() => setN((v) => v + 1)}>
            rerender
          </button>
        </div>
      );
    }

    render(<Harness />);
    await screen.findByTestId("env");

    // 模拟真实页面：挂载后经历多轮无关重渲（其它 state 变化）
    fireEvent.click(screen.getByTestId("bump"));
    fireEvent.click(screen.getByTestId("bump"));
    fireEvent.click(screen.getByTestId("bump"));
    await waitFor(() => expect(screen.getByTestId("n").textContent).toBe("3"));

    expect(refreshFn).not.toHaveBeenCalled();
    // 关键断言：点击「切换环境」后必须真正刷新（旧「普通对象」实现下这里恒为 0 次
    // —— 用户看到「已保存」但列表不变，只能手动 F5）
    fireEvent.click(screen.getByTestId("env"));
    await waitFor(() => expect(refreshFn).toHaveBeenCalledTimes(1));
    expect(refreshFn).toHaveBeenCalledTimes(1);
  });
});

// ============================================================
describe("Markdown 样式只注入一份", () => {
  it("多个 MarkdownRenderer 实例不重复注入 <style>（旧实现每实例一份，CSSOM 线性膨胀）", () => {
    const before = document.querySelectorAll("style[data-markdown-body-style]").length;
    const content = "# 标题\n\n一段**正文**。\n\n- a\n- b";
    render(
      <div>
        <MarkdownRenderer content={content} />
        <MarkdownRenderer content={content} />
        <MarkdownRenderer content={content} />
      </div>
    );
    const after = document.querySelectorAll("style[data-markdown-body-style]").length;
    // 3 个实例渲染不应新增任何 <style>
    expect(after - before).toBe(0);
    // 但必须确实有一份真实样式（防「只建节点不写内容」或「根本没注入」）
    expect(after).toBeGreaterThanOrEqual(1);
    const css = document.querySelector("style[data-markdown-body-style]")!.textContent || "";
    expect(css).toContain(".markdown-body");
  });
});
