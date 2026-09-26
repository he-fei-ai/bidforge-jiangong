// @vitest-environment jsdom
/**
 * R14 回归测试：全局 401 短路保护。
 *
 * 背景：后端启用 API_AUTH_TOKEN 后，前端若未及时更新凭据，会持续收到 401。
 * 各业务组件（Layout 的 projectsApi.list、useSchemeLiveTask 的 3s 轮询、
 * TaskStatusBar 的多轮询）仍会继续发请求，导致日志噪音（120 次连续同一签名）。
 *
 * 测试锁定：
 * 1. authGuard 默认未阻塞；
 * 2. 收到 401 后置 blocked=true，reject 的 error 带 isAuthBlocked 与 status 标记；
 * 3. clearAuthShortCircuit() 重置标志。
 *
 * 通过 mock axios.create 让导出的 api 实例使用假 adapter，完全隔离网络。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// 直接 mock axios 的默认导出：create() 返回带假 adapter 的实例
vi.mock("axios", async () => {
  const actual = (await vi.importActual<typeof import("axios")>("axios")).default;
  const instance = actual.create({ baseURL: "/api/v1" });
  // 覆盖 adapter：所有请求都返回 401
  (instance.defaults as any).adapter = async () => {
    const err: any = new Error("unauthorized");
    err.response = { status: 401, data: { detail: "未授权访问" } };
    throw err;
  };
  return { ...actual, default: { ...actual, create: () => instance, isAxiosError: actual.isAxiosError } };
});

import { authGuard, clearAuthShortCircuit, projectsApi } from "../api";

describe("R14 全局 401 短路保护", () => {
  beforeEach(() => {
    clearAuthShortCircuit();
  });

  it("初始状态 authGuard.blocked=false", () => {
    expect(authGuard.blocked).toBe(false);
    expect(authGuard.reason).toBe("");
  });

  it("收到 401 后置 authGuard.blocked=true 且 reason 可读", async () => {
    try {
      await projectsApi.list();
    } catch {
      // expected
    }
    expect(authGuard.blocked).toBe(true);
    expect(typeof authGuard.reason).toBe("string");
    expect(authGuard.reason.length).toBeGreaterThan(0);
  });

  it("blocked=true 后再次请求不发网络，直接 reject 并带 isAuthBlocked 标记", async () => {
    authGuard.blocked = true;
    authGuard.reason = "未授权访问";
    let caught: any = null;
    try {
      await projectsApi.list();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeTruthy();
    expect(caught.isAuthBlocked).toBe(true);
    expect(caught.status).toBe(401);
    expect(caught.message).toContain("未授权");
  });

  it("clearAuthShortCircuit 重置后请求能重新发到 adapter", async () => {
    authGuard.blocked = true;
    authGuard.reason = "旧凭据";
    clearAuthShortCircuit();
    expect(authGuard.blocked).toBe(false);
    // 现在再次请求应该走 adapter（会被 401 拦截器再次置 blocked）
    try {
      await projectsApi.list();
    } catch {
      // 401 again
    }
    expect(authGuard.blocked).toBe(true);
  });
});

