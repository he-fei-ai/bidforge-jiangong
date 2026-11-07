import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

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

// 桩 3（2026-11-07）：全局自动卸载。
//
// 背景：`vitest.config.ts` 未开启 `globals`，而 RTL 只在检测到全局 `afterEach` 时
// 才注册自动 cleanup（见 @testing-library/react/dist/index.js 的启注册分支）。
// 本仓库因此**没有任何自动卸载**：任何忘记手写 `cleanup()` 的测试文件都会把已
// 挂载组件留到文件结束，jsdom 拆除后再触发定时器就会变成
// `ReferenceError: window is not defined` 这类与代码质量无关的偶发红。
//
// 处置：在 setupFiles 里显式注册一次（`globals: false` 下的标准写法，不改
// vitest 配置、不引入全局变量）。实测全量 93 文件 / 1570 用例全绿，未暴露
// 任何跨用例挂载依赖；各文件里已有的单独 `cleanup` 重复执行无副作用。
afterEach(() => {
  cleanup();
});
