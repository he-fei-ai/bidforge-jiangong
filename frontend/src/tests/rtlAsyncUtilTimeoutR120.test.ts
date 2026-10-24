// R120 · L1 遗留加固护栏：锁定 RTL 全局 asyncUtilTimeout 已被抬高（防回流）。
//
// 背景（详见 frontend/src/tests/setup.ts 桩 3）：backend 全量并发跑
// TestFrontendRealExecution::test_vitest_review_closeout 时，前端 waitFor 因
// RTL 默认 asyncUtilTimeout=1000ms（独立于 vitest testTimeout=60000）在 CPU
// 争抢下偶发超时抖动。修法是在 setup.ts 里 configure({ asyncUtilTimeout: 5000 })。
//
// 本护栏是「修复仍在位」的行为级锚：若 configure 被摘掉 / 调回 1000，getConfig()
// 立刻回到默认 ⇒ 本断言变红，逼改动者显式面对该抖动权衡（node 环境即可，无需 jsdom）。
import { describe, it, expect } from "vitest";
import { getConfig } from "@testing-library/dom";

describe("R120 · RTL asyncUtilTimeout 全局抬高（L1 加固锚）", () => {
  it("由 setup.ts 统一抬高到 >= 5000ms（覆盖 RTL 默认 1000ms）", () => {
    const t = getConfig().asyncUtilTimeout;
    expect(t).toBeGreaterThanOrEqual(5000);
    // 上界锚：不得离谱到接近/超过 vitest testTimeout(60000)，否则真失败用例会拖满整个测试超时预算。
    expect(t).toBeLessThan(60000);
  });
});
