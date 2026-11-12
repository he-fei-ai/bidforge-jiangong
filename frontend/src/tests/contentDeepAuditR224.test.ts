/// <reference types="vite/client" />
/**
 * 正文生成模块深度审计增强 · R224 前端护栏（2026-11-12）。
 *
 * 收口面（配套后端护栏 tests/test_content_deep_audit_r224_20261112.py）：
 *   F3  刷新接管僵尸判定：阈值 0.95→0.99 且移到 active 选取之后（0.95 会把
 *       合法的一致性/字数阶段 0.95~0.98 误判成「已完成」且不接管，并挡住
 *       同方案真正在跑任务的接管选取）。
 *   F17 刷新接管 stopped 分支补 load()（outline 例外）+ facts 终态消费接线
 *       （factsTerminalFnRef / onFactsResult）——三路恢复矩阵补齐。
 *   F18 空 taskId 未知态守卫：先按类型检索最近任务再查询（旧实现空 id 直接
 *       GET /sse/task/"" → 404 → fail-open 放行重新生成）。
 *   F23 在线 completed 消费 run_words / over_count（后端 R-1 口径键此前
 *       只有类型声明零读取点）。
 *
 * 判据实现沿用本仓既有范式（contentCloseoutR194.test.ts）：
 * Vite import.meta.glob(..., { query: "?raw" }) 读源码文本，不引 node:fs。
 */
import { describe, it, expect } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"] || "";
const RECOVERY = RAW["pages/schemeWorkbench/useUnknownTaskRecovery.tsx"] || "";
const FACTS = RAW["pages/schemeWorkbench/useFactsGeneration.ts"] || "";
const TYPES = RAW["pages/schemeWorkbench/workbenchTypes.ts"] || "";

describe("R224 前端护栏 · 刷新接管僵尸判定（F3）", () => {
  it("僵尸阈值 0.99 且只对将要接管的 active 判定（先选 active 后判僵尸）", () => {
    const i = PAGE.indexOf("R224（F3）：僵尸判定移到 active 选取");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 900);
    expect(seg).toContain("const active = allRunning.find(Boolean);");
    const activePos = seg.indexOf("const active = allRunning.find(Boolean);");
    const zombiePos = seg.indexOf("(active.progress || 0) >= 0.99");
    expect(zombiePos).toBeGreaterThan(activePos);
    expect(seg).not.toContain(">= 0.95");
    // 旧实现的全量 find 僵尸先行 return 必须消失
    expect(PAGE).not.toContain('allRunning.find((t: TaskListItem) => (t.progress || 0) >= 0.95)');
  });
});

describe("R224 前端护栏 · stopped 分支 load 与 facts 终态接线（F17）", () => {
  it("刷新接管 stopped 补 load() 且 outline 例外", () => {
    const i = PAGE.indexOf("R224（F17）：stopped 前已逐章落库的正文不 load() 永不显示");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 400);
    expect(seg).toContain('active.task_type !== "outline_generation"');
    expect(seg).toContain("load();");
  });

  it("刷新接管 completed/degraded 对 facts 不再走通用分支（防双提示）", () => {
    expect(PAGE).toContain('st.status === "completed" && active.task_type !== "facts_generation"');
    expect(PAGE).toContain('st.status === "degraded" && active.task_type !== "facts_generation"');
  });

  it("factsTerminalFnRef 注入与 useFactsGeneration 回调消费成对存在", () => {
    expect(PAGE).toContain("factsTerminalFnRef.current?.(st)");
    expect(PAGE).toContain("onFactsResult: (fin) => factsTerminalFnRef.current?.(fin)");
    expect(PAGE).toContain("factsTerminalFnRef.current = consumeFactsTerminal");
    expect(FACTS).toContain("consumeFactsTerminal");
    expect(FACTS).toContain("const _fin = fin?.facts_result;");
    expect(FACTS).toContain("applyFactsCompletedEvent(_fin)");
  });

  it("手动接管 facts completed/degraded 走 onFactsResult 专用消费", () => {
    const i = RECOVERY.indexOf("R224（F17）：facts 手动接管 completed/degraded 走专用消费");
    expect(i).toBeGreaterThan(0);
    const seg = RECOVERY.slice(i, i + 500);
    expect(seg).toContain('ut.genType === "facts" && (fin.status === "completed" || fin.status === "degraded")');
    expect(seg).toContain("onFactsResult?.(fin)");
  });

  it("hook 依赖声明 onFactsResult（三路恢复矩阵的第三条入参）", () => {
    expect(RECOVERY).toContain("onFactsResult?: (fin: TaskTerminalInfo) => void;");
    expect(RECOVERY).toContain("    onFactsResult,\n");
  });
});

describe("R224 前端护栏 · 空 taskId 守卫 fail-open 修复（F18）", () => {
  it("守卫先按类型检索最近任务，检索不到才放行", () => {
    const i = RECOVERY.indexOf("R224（F18）：空 taskId");
    expect(i).toBeGreaterThan(0);
    const seg = RECOVERY.slice(i, i + 1200);
    expect(seg).toContain("let checkTaskId = ut.taskId;");
    expect(seg).toContain("findRecentOutlineTaskImpl");
    expect(seg).toContain("tasksApi.status(checkTaskId)");
    expect(seg).toContain("未发现仍在运行的后台任务，可安全重新生成");
  });
});

describe("R224 前端护栏 · 在线 completed 消费 run_words/over_count（F23）", () => {
  it("completed 分支读取 run_words 并透出落库字数/超字章数", () => {
    const i = PAGE.indexOf("R224（F23）：在线 completed 消费 run_words / over_count");
    expect(i).toBeGreaterThan(0);
    const seg = PAGE.slice(i, i + 500);
    expect(seg).toContain("evt.run_words");
    expect(seg).toContain("evt.over_count");
    expect(seg).toContain("本次落库");
  });

  it("ContentGenEvent 类型补 run_words/over_count 可选字段", () => {
    expect(TYPES).toContain("run_words?: number;");
    expect(TYPES).toContain("over_count?: number;");
  });
});
