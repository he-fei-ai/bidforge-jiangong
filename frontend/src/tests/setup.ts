// R120（L1 遗留加固 · 2026-10-21）：抬高 @testing-library 的 asyncUtilTimeout。
// @testing-library/dom 的 configure 是纯配置写入，import 置于模块顶部（eslint import/first）。
import { configure as _rtlConfigure } from "@testing-library/dom";

// 测试全局桩（vitest setupFiles）：补齐 jsdom/Node 环境下缺失或残缺的浏览器 API。
//
// 桩 1（2026-09-22 前端测试基线修复）：jsdom 未实现 `URL.createObjectURL` /
// `URL.revokeObjectURL`，而导出/下载类用例（ReadinessDashboard「导出整改清单」）
// 会 `vi.spyOn(URL, 'createObjectURL')`，属性不存在时 `vi.spyOn` 直接抛
// `createObjectURL does not exist`，导致该用例必败。此处全局打桩为 no-op，
// 既满足 `in URL` 判定，又让 spy 可挂载、下载逻辑不真正触发副作用。
const g = globalThis as unknown as {
  URL?: { createObjectURL?: unknown; revokeObjectURL?: unknown };
};

if (g.URL && typeof g.URL.createObjectURL !== "function") {
  // 给 jsdom/Node 的 URL 补一个测试用桩（Node 类型下赋值不报错，无需 ts-expect-error）
  (g.URL as any).createObjectURL = () => "blob:mock";
}
if (g.URL && typeof g.URL.revokeObjectURL !== "function") {
  (g.URL as any).revokeObjectURL = () => {};
}

// 桩 2（2026-09-22）：Node ≥22 内置的 `localStorage`（webstorage）只实现了
// getItem/setItem/removeItem/key，**缺 clear()**；且因为 globalThis 上已存在
// localStorage，jsdom 环境不再覆盖它 —— 任何 `localStorage.clear()` 都会抛
// 「clear is not a function」，SectionContentCard / TaskStatusBar 等
// beforeEach(localStorage.clear) 的既有用例全部必败。
// 此处统一替换为完整的内存桩（getItem/setItem/removeItem/key/clear/length），
// node 与 jsdom 环境行为一致。
const _ls = (globalThis as unknown as { localStorage?: unknown }).localStorage;
if (!_ls || typeof (_ls as { clear?: unknown }).clear !== "function") {
  const store = new Map<string, string>();
  const shim = {
    getItem: (k: string) => (store.has(String(k)) ? store.get(String(k))! : null),
    setItem: (k: string, v: string) => {
      store.set(String(k), String(v));
    },
    removeItem: (k: string) => {
      store.delete(String(k));
    },
    clear: () => {
      store.clear();
    },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: shim,
    configurable: true,
    writable: true,
  });
}

// 桩 3（R120，L1 遗留加固）：抬高 RTL 的 asyncUtilTimeout（waitFor/find* 内部超时）。
// 现象：backend 全量 9400 项并发跑 TestFrontendRealExecution::test_vitest_review_closeout
// 时，`BatchFixModal.test.tsx` 里 `await waitFor(() => expect(api.stage)...)` 偶发红
// ——但单独 `vitest run BatchFixModal` 稳定 23/23 绿（该用例 398ms 通过）。
// 根因：vitest.config.ts 只放宽了 **vitest 侧** testTimeout=60000，而 RTL 的 waitFor 自带
// **独立的** asyncUtilTimeout，默认仅 1000ms；全量 CPU 争抢下 antd 渲染 + 子进程冷启动被
// 拖过 1s 即抖红。二者是两套互不相干的超时，放宽前者管不到后者。
// 抬到 5000ms（与本仓已有显式 `{ timeout: 5000 }` 用例同量级）：命中即早返回 ⇒ 通过用例零
// 成本；仅真失败才多等，且仍受 testTimeout(60000) 兜底。**显式传 per-call timeout 的用例
// 不受影响**（RTL 中显式优先于全局默认），故只兜住「吃默认 1000ms」的那批抖动。
_rtlConfigure({ asyncUtilTimeout: 5000 });
