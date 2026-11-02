// R120（L1 遗留加固 · 2026-10-21）：抬高 @testing-library 的 asyncUtilTimeout。
// @testing-library/dom 的 configure 是纯配置写入，import 置于模块顶部（eslint import/first）。
import { configure as _rtlConfigure } from "@testing-library/dom";
// R136（2026-10-27）：afterEach 钩子（见文末桩 4）。
import { afterEach as _afterEach } from "vitest";

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

// 桩 4（R136，2026-10-27 · 测试卫生）：每例结束后销毁 antd 消息容器。
// 现象：`vitest run` 跑 import/parse 簇时 154/154 全绿，但进程仍报
//   `Errors 1 error` → 退出码 1（CI 红）：
//   `ReferenceError: window is not defined`
//   at getCurrentEventPriority (react-dom) ← rc-notification Notice 的自动关闭 setTimeout。
// 根因：antd `message` 的提示默认 4.5s 自动关闭，timer 挂在 rc-notification 的
//   Notice 组件上（见 Notice.js 的 useEffect），而 antd 的 holder 挂在 document.body、
//   **不在** RTL cleanup 的作用域内 —— 测试文件跑完后环境被拆掉，timer 才到点，
//   回调读 window 直接抛。该用例本身已绿，但 vitest 把“拆场后才抛的未处理异常”
//   计为错误并置非零退出码。
// 修法：在每例结束时（jsdom 仍存活）主动 destroy 两个 holder → 卸载 Notice →
//   其 effect 清理函数 clearTimeout，timer 不再活过环境。
// 注意：① 先探「DOM 里是否真存在 holder」再动手 —— antd 的 `message.destroy()`
//   内部会 `getInstance()` 惰性创建 holder（ReactDOM 挂到 body），对**从未弹过提示**
//   的文件调一下反而"凭空造一个再卸掉"，卸场后 react-dom 并发调度继续跑就变成新的
//   未处理异常（实测：factsApi.test.ts / contentTerminalParity20261011.test.ts 两个
//   零 antd 引用的文件反而因此报 `window is not defined` / `Should not already be working`）；
//   ② 动态 import 并先判 `typeof window` —— 仓内存在 `@vitest-environment node` 的用例，
//   静态 import antd 会给那些文件也加载 DOM 层依赖；③ 整块 try/catch wrap，antd
//   不可用时不阻断测试结论（保持"测试失败只因业务断言"口径）。
_afterEach(async () => {
  if (typeof window === "undefined") return;
  if (!document.querySelector(".ant-message, .ant-notification")) return;
  try {
    const { message, notification } = await import("antd");
    message.destroy();
    notification.destroy();
  } catch {
    /* 忽略：销毁是卫生措施，不应影响测试结论 */
  }
});
