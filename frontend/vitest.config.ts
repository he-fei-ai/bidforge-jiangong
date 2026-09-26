import { defineConfig } from "vitest/config";

/**
 * ✅ 测试稳定性修复（2026-09-21）：全量套件此前会**间歇性失败**。
 *
 * 现象：`npm run test` 全量跑 26 个文件时，默认 26 个 isolate worker 并行
 * 启动（每个 ~4.35s 冷启动），CPU 过度订阅下 `OutlineLibraryPage.test.tsx`
 * 与 `ReviewWorkflowPanel.test.tsx` 各有一项在 **5000ms（默认 testTimeout）**
 * 处被判超时——但这两个文件单独跑只要 1.8s / 12s 就全绿。
 * 后果：CI 上出现与代码质量无关的红灯，掩盖真实回归。
 *
 * 修复口径：
 *   · testTimeout / hookTimeout 放宽到与真实耗时同量级（含 antd 渲染开销）；
 *   · maxWorkers 收敛，避免 worker 数 = 文件数导致启动风暴；
 *   · environment 仍为 node，需 jsdom 的用例在文件头显式声明
 *     `// @vitest-environment jsdom`（沿用既有约定）。
 */
export default defineConfig({
  test: {
    environment: "node",
    // ✅ 全局测试桩（2026-09-22）：补齐 jsdom/Node 缺失的 URL.createObjectURL，
    // 否则导出/下载类用例 vi.spyOn(URL,'createObjectURL') 直接抛错。
    setupFiles: ["./src/tests/setup.ts"],
    include: ["src/tests/**/*.test.ts", "src/tests/**/*.test.tsx"],
    testTimeout: 20000,
    hookTimeout: 10000,
    maxWorkers: 8,
  },
});
