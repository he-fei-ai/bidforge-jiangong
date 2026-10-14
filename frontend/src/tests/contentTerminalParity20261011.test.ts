/// <reference types="vite/client" />
// @vitest-environment jsdom
/**
 * 「终态集合必须单一事实源」· 源码级防漂移护栏（2026-10-11 · 遗留项 #1）。
 *
 * 背景：
 * 后端有一个加法式终态 `degraded`（2026-10-07 引入，一致性修复有失败项时替代
 * `completed`）。前端此前把「终态」**各自内联**成三态白名单，全仓共 5 处：
 *   1. SchemeWorkbenchPage  pollTaskUntilTerminalImpl
 *   2. SchemeWorkbenchPage  接管后台任务轮询
 *   3. SchemeWorkbenchPage  正文 SSE 终态分支
 *   4. useContentGeneration 正文 SSE 终态分支 + 断线重挂轮询
 *   5. useSchemeLiveTask    后台任务终态跳变检测（本轮补⑤）
 * 2026-10-10 修复了前 4 处，2026-10-11 补第 5 处。
 *
 * ⚠️ 为什么需要本文件：上面的修法是「各自改一遍」，**下一次再有人加第六处判定、
 * 或有人把 `completed` 单独拎出来重写判断**，不会有任何现有测试变红 —— 缺陷会
 * 静默复发（这正是它此前能存活到今天的原因）。本文件把「不得内联终态白名单」
 * 从**口头约定**升级为**可执行判据**：
 *
 *   A 组：扫描 src（排除测试），不得出现任何内联的终态白名单写法；
 *   B 组：把 `degraded` 当作**任务状态/事件**处理的文件，必须走共享出口；
 *   C 组：liveTaskFinishMessage 纯函数行为；
 *   D 组：反向锚 —— 证明上面的扫描真能抓住旧写法。
 *
 * 实现说明：用 Vite 的 `import.meta.glob(..., { query: "?raw" })` 读取源码文本，
 * 不用 node:fs / __dirname（本项目没装 @types/node，tests 也在 tsc 范围内）。
 */
import { describe, it, expect } from "vitest";
import { liveTaskFinishMessage } from "../hooks/useSchemeLiveTask";
import {
  isTerminalTaskStatus, isTerminalContentEvent, TERMINAL_TASK_STATUSES,
} from "../utils/contentEvents";
import { STATUS_META, statusMeta } from "../components/TaskStatusBar";

/**
 * 相对 src/ 的路径 → 源码全文。
 *
 * ⚠️ glob 的 key 是**相对本文件（src/tests/）**的：
 *   `../pages/X.tsx`（src 下的文件）  → `pages/X.tsx`
 *   `./Y.test.tsx`（tests 下的同级文件）→ `tests/Y.test.tsx`
 * 首版只处理了 `../`，导致 tests/ 下的用例文件被当成生产源码扫进来，
 * B 组把自己同目录下的兄弟测试判成了违规。
 */
function normalizeKey(key: string): string {
  if (key.startsWith("../")) return key.slice(3);
  if (key.startsWith("./")) return `tests/${key.slice(2)}`;
  return key;
}

const RAW_SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [normalizeKey(k), v]),
);

/**
 * ⚠️ 只收「非测试源码」：测试里为了做反向锚，必须能复刻旧写法
 * （把 A 组规则扫到测试文件上会让 D 组自相矛盾）。
 */
const SOURCES = Object.entries(RAW_SOURCES).filter(
  ([rel]) => !rel.startsWith("tests/"),
);

/**
 * 「内联终态白名单」的判据（不是单条正则，而是**复合判据**）：
 *   同一行里出现 ≥2 个终态字面量（completed/failed/stopped），并且做了一次
 *   集合判定（`.includes(`）或等值链（`||`）。
 *
 * ⚠️ 首版用的是单条 `/includes\(\s*(?:\w+\.)?status\s*\)/`，会把
 *   `["configured","no_key","key_invalid"].includes(health.status)`（AI 健康态）
 *   与 `["empty","failed","pending"].includes(l.status)`（叶子：只有 1 个终态词）
 *   一并误伤 —— **扫到了不是终态判别的 status 用法**。加「≥2 个终态字面量」这道
 *   闸门后才真正对准「在枚举任务终态」这一件事。
 */
const TERMINAL_LITERAL_RE = /["'](completed|failed|stopped)["']/g;

function isInlineTerminalWhitelist(line: string): boolean {
  const names = new Set(
    Array.from(line.matchAll(TERMINAL_LITERAL_RE), (m) => m[1]),
  );
  if (names.size < 2) return false;
  return /\.includes\(|\|\|/.test(line);
}

const CONTENT_EVENTS = "utils/contentEvents.ts";

function findLines(pred: (rel: string, line: string) => boolean): string[] {
  const hits: string[] = [];
  for (const [rel, src] of SOURCES) {
    src.split(/\r?\n/).forEach((line: string, i: number) => {
      if (pred(rel, line)) hits.push(`${rel}:${i + 1}: ${line.trim()}`);
    });
  }
  return hits;
}

describe("A 组 · 源码扫描：不得内联终态白名单", () => {
  it("A1: 源码树非空且能取到被测文件（前提校验，防 glob 空跑成常绿）", () => {
    expect(SOURCES.length).toBeGreaterThan(50);
    const rels = SOURCES.map(([r]) => r);
    for (const must of [
      "pages/SchemeWorkbenchPage.tsx",
      "hooks/useContentGeneration.ts",
      "hooks/useSchemeLiveTask.ts",
      CONTENT_EVENTS,
    ]) {
      expect(rels).toContain(must);
    }
  });

  it("A2: 生产源码中不存在内联的终态白名单写法", () => {
    const hits = findLines(
      (rel, line) => rel !== CONTENT_EVENTS && isInlineTerminalWhitelist(line),
    );
    expect(hits).toEqual([]);
  });

  it("A2b: 判据不受非终态 status 用法干扰（前提校验）", () => {
    // 同为 `.includes(x.status)`，但枚举的不是任务终态 ⇒ 不许被判为违规
    expect(isInlineTerminalWhitelist(
      `{!!health && ["configured", "no_key", "key_invalid"].includes(health.status) && (`,
    )).toBe(false);
    expect(isInlineTerminalWhitelist(
      `filtered = leaves.filter((l) => ["empty", "failed", "pending"].includes(l.status));`,
    )).toBe(false);
  });

  it("A3: 共享判定函数确实被各调用点引用（防止只声明不用）", () => {
    const usingSymbol = (symbol: string) =>
      SOURCES.filter(([, s]) => s.includes(symbol)).map(([r]) => r);
    const usingStatus = usingSymbol("isTerminalTaskStatus");
    for (const must of [
      "pages/SchemeWorkbenchPage.tsx",
      "hooks/useContentGeneration.ts",
      "hooks/useSchemeLiveTask.ts",
    ]) {
      expect(usingStatus).toContain(must);
    }
    expect(usingSymbol("isTerminalContentEvent")).toContain("hooks/useContentGeneration.ts");
  });
});

describe("B 组 · degraded 处理点必须可见可控", () => {
  /**
   * ⚠️ 判据要窄两层，否则会误伤：
   *   ① `degraded` 在 AI 配置域另有用例（``degraded_providers`` 熔断状态），
   *      所以**不能**只匹配字面量 —— 必须限定是「把 degraded 当任务状态/事件」；
   *   ② TaskStatusBar 里的 `degraded` 是**纯展示**（状态色/图标），不是终态逻辑，
   *      强行要求它引用 `isTerminalTaskStatus` 是错的治疗方法。
   *
   * 因此本组改成**棘轮式可见性**：凡是新增一处「把 degraded 当状态处理」的文件，
   * 要么走共享出口，要么必须在本白名单里带理由显式登记 —— 静默新增会让本条变红。
   */
  const KNOWN_DEGRADED_HANDLERS: Record<string, string> = {
    "pages/SchemeWorkbenchPage.tsx": "正文 SSE 终态 + 三处轮询",
    "hooks/useContentGeneration.ts": "正文 SSE 终态 + 断线重挂轮询",
    "hooks/useSchemeLiveTask.ts": "后台任务终态跳变检测（触发刷新）",
    "components/TaskStatusBar.tsx": "纯展示：状态色与图标（不走终态逻辑）",
  };

  it("B1: 出现「degraded 当状态处理」的文件，必须走共享出口或已登记", () => {
    const offenders: string[] = [];
    for (const [rel, src] of SOURCES) {
      if (rel === CONTENT_EVENTS) continue;
      const isStatusCompare = /(status|event)\s*[!=]==\s*["']degraded["']/.test(src);
      if (!isStatusCompare) continue;
      const shared = src.includes("isTerminalTaskStatus")
        || src.includes("isTerminalContentEvent")
        || src.includes("TERMINAL_TASK_STATUSES")
        || src.includes("CONTENT_TERMINAL_EVENTS");
      if (shared || KNOWN_DEGRADED_HANDLERS[rel]) continue;
      offenders.push(rel);
    }
    expect(offenders).toEqual([]);
  });

  it("B2: 反向锚 —— 共享出口确实覆盖了各终态判定点", () => {
    const degradedFiles = SOURCES
      .filter(([, src]) => /(status|event)\s*[!=]==\s*["']degraded["']/.test(src))
      .map(([rel]) => rel);
    for (const must of [
      "hooks/useSchemeLiveTask.ts",
      "pages/SchemeWorkbenchPage.tsx",
      "hooks/useContentGeneration.ts",
    ]) {
      expect(degradedFiles).toContain(must);
    }
  });

  it("B3: 白名单不得过期（登记项必须真的还在处理 degraded）", () => {
    for (const rel of Object.keys(KNOWN_DEGRADED_HANDLERS)) {
      const hit = SOURCES.find(([r]) => r === rel);
      expect(hit, `白名单登记了 ${rel}，但源码树里找不到该文件`).toBeTruthy();
      expect(/(status|event)\s*[!=]==\s*["']degraded["']/.test(hit![1]),
        `白名单登记了 ${rel}，但该文件已不再处理 degraded，请移除登记项`).toBe(true);
    }
  });
});

describe("C 组 · liveTaskFinishMessage 行为", () => {
  it("C1: completed / degraded / failed / stopped 四态均有文案", () => {
    expect(liveTaskFinishMessage("completed", "正文生成")).toEqual({
      level: "success", text: "后台「正文生成」任务已完成，数据已刷新",
    });
    expect(liveTaskFinishMessage("degraded", "正文生成", "3 处失败")?.level).toBe("warning");
    expect(liveTaskFinishMessage("degraded", "正文生成", "3 处失败")?.text)
      .toContain("一致性修复有失败项");
    expect(liveTaskFinishMessage("failed", "目录生成", "配额不足")?.text)
      .toContain("配额不足");
    expect(liveTaskFinishMessage("stopped", "事实提取")).toEqual({
      level: "info", text: "后台「事实提取」任务已停止",
    });
  });

  it("C2: 非终态必须返回 null（调用方据此决定是否触发 onFinished）", () => {
    for (const s of ["running", "paused", "pending", ""]) {
      expect(liveTaskFinishMessage(s, "正文生成")).toBeNull();
    }
  });

  it("C3: completed 不附加 message（旧口径：成功时不拼病因）", () => {
    const got = liveTaskFinishMessage("completed", "正文生成", "已完成");
    expect(got?.text).toBe("后台「正文生成」任务已完成，数据已刷新");
  });

  it("C4: 共享判定与正文 SSE 口径同源", () => {
    expect(isTerminalTaskStatus("degraded")).toBe(isTerminalTaskStatus("completed"));
    expect(isTerminalContentEvent({ event: "degraded" })).toBe(true);
  });
});

// ===========================================================================
// F 组 · 两套 SSE 消费实现的事件覆盖必须一致（遗留项 #1 的可执行判据）
// ===========================================================================

const CONTENT_ENDPOINT = "/sse/generate-content/";
const EVENT_RE = /evt\.event === "([a-z_]+)"/g;

/**
 * 截取「正文生成 SSE 事件循环」的源码片段。
 *
 * 两个消费者形态不同：`useContentGeneration.ts` 整个文件只服务正文这一条链路，
 * 而 `SchemeWorkbenchPage.tsx` 里同一套 `for await (const evt of sseFetch(...))`
 * 写法重复了三次（目录 / 正文 / 事实），必须**只取正文那一段** —— 直接全文匹配
 * 会把目录的 `outline` 事件混进来，两边根本不可比。
 * 切法：从 `/sse/generate-content/` 起，到下一个 `sseFetch(` 止。
 */
function contentLoopSlice(src: string): string {
  const start = src.indexOf(CONTENT_ENDPOINT);
  if (start < 0) return "";
  const next = src.indexOf("sseFetch(", start + CONTENT_ENDPOINT.length);
  return src.slice(start, next < 0 ? src.length : next);
}

const handledEvents = (rel: string) => {
  const entry = RAW_SOURCES[rel];
  return new Set(Array.from(contentLoopSlice(entry || "").matchAll(EVENT_RE), (m) => m[1]));
};

const HOOK_EVENTS = handledEvents("hooks/useContentGeneration.ts");
const PAGE_EVENTS = handledEvents("pages/SchemeWorkbenchPage.tsx");

describe("F 组 · 两套正文 SSE 消费实现的事件覆盖一致", () => {
  it("F1: 前提校验 —— 两边都确实取到了事件（为空会让 F2 变成常绿）", () => {
    expect(HOOK_EVENTS.size).toBeGreaterThan(10);
    expect(PAGE_EVENTS.size).toBeGreaterThan(10);
  });

  it("F2: 两边处理的事件集合必须完全相同（差集双向为空）", () => {
    const onlyHook = [...HOOK_EVENTS].filter((e) => !PAGE_EVENTS.has(e));
    const onlyPage = [...PAGE_EVENTS].filter((e) => !HOOK_EVENTS.has(e));
    expect({ onlyHook, onlyPage }).toEqual({ onlyHook: [], onlyPage: [] });
  });

  it("F3: 核心事件两边都必须处理", () => {
    const core = [
      "section_start", "section_done", "section_error", "section_stage",
      "stats", "ping", "stopped",
      "consistency_scan_start", "consistency_scan_progress", "consistency_scan_done",
      "consistency_repair_progress", "consistency_repair_done",
      "consistency_skipped", "consistency_failed",
    ];
    for (const e of core) {
      expect([...HOOK_EVENTS]).toContain(e);
      expect([...PAGE_EVENTS]).toContain(e);
    }
  });

  it("F4: 终态事件两边都不用字面量判断（走共享出口，见 A3）", () => {
    for (const events of [HOOK_EVENTS, PAGE_EVENTS]) {
      expect(events.has("completed")).toBe(false);
    }
    expect(isTerminalContentEvent({ event: "completed" })).toBe(true);
    expect(isTerminalContentEvent({ event: "degraded" })).toBe(true);
  });

  it("F5: 反向锚 —— 判据能识别出「某一边漏了某个事件」", () => {
    const missing = [...HOOK_EVENTS].filter((e) => !PAGE_EVENTS.has(e));
    expect(missing).toEqual([]);
    // 人为删掉一个事件后必须能抓到
    const shrunk = new Set([...PAGE_EVENTS]);
    shrunk.delete("consistency_failed");
    expect([...HOOK_EVENTS].filter((e) => !shrunk.has(e))).toEqual(["consistency_failed"]);
  });
});

describe("E 组 · 状态栏不得再把终态显示成英文原文", () => {
  it("E1: 每个任务终态 + running/paused 都有中文标签（不落到兜底英文）", () => {
    for (const s of [...TERMINAL_TASK_STATUSES, "running", "paused"]) {
      expect(STATUS_META[s], `缺少状态 ${s} 的展示元数据`).toBeTruthy();
      expect(statusMeta(s).label).not.toBe(s); // 落到兜底就会原样吐英文
    }
  });

  it("E2: degraded 的展示色既区别于成功绿、也区别于失败红", () => {
    expect(STATUS_META.degraded.label).toBe("降级完成");
    expect(STATUS_META.degraded.color).not.toBe(STATUS_META.completed.color);
    expect(STATUS_META.degraded.color).not.toBe(STATUS_META.failed.color);
  });

  it("E3: 未知状态仍安全回退（不得因新增补数据显示崩溃）", () => {
    expect(statusMeta("some_new_status").label).toBe("some_new_status");
    expect(statusMeta("").label).toBe("未知");
  });
});

describe("D 组 · 反向锚：证明扫描能抓住旧写法", () => {
  it("D1: 旧三态数组写法会被 A2 抓住", () => {
    expect(isInlineTerminalWhitelist(
      `if (["completed", "failed", "stopped"].includes(st.status)) {`)).toBe(true);
  });

  it("D2: 旧 status 等值链写法会被 A2 抓住", () => {
    expect(isInlineTerminalWhitelist(
      `if (t.status === "completed" || t.status === "failed") {`)).toBe(true);
  });

  it("D3: 仅含单个终态词的 status 用法不算违规（判据不能过宽）", () => {
    expect(isInlineTerminalWhitelist(`if (st.status === "completed") {`)).toBe(false);
  });

  it("D4: 换用共享判定后不再被判为内联白名单", () => {
    expect(isInlineTerminalWhitelist(
      `if (isTerminalTaskStatus(t.status)) { optsRef.current?.onFinished?.(); }`,
    )).toBe(false);
  });
});
