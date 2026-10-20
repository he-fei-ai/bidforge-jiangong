// @vitest-environment jsdom
/**
 * ✅ R104（2026-10-18）一致性审计 422 错误文案可达性护栏
 *
 * 收口 R103 §6 遗留第三/第四项：`POST /compliance/consistency-audit/{id}`
 * 是 FastAPI `HTTPException(422, "中文消息")` 形态（后端 `compliance.py` 两处：
 * 「方案尚无正文内容，无法审计」/「尚未提取项目关键事实…」），响应体是
 * `{"detail": "字符串"}` —— **不是** 422 校验错误的数组形 `{"detail":[{loc,msg}]}`。
 *
 * 已有的 R55 护栏（test_review_autofix_closeout_r55_20261008::
 * test_api_detail_array_single_source）只锁了**数组形**的单一出口，
 * 于是这条**字符串形**链路全程无断言：拦截器把 detail 写进 error.message、
 * 页面 handler 消费 e.message 弹给用户 —— 两跳只要有一跳被"顺手简化"，
 * 用户就退回 axios 默认的 "Request failed with status code 422"，
 * 而前端契约测试（只校验 URL/timeout）与后端源码锁都不会红。
 *
 * 本文件用**真实拦截器回调**（捕获 interceptors.response.use 的 onRejected）
 * 做行为级断言，不再只比字符串：既锁"字符串形/数组形两态都能到达用户"，
 * 也锁静默分支（Abort、无 detail）不丢原始消息。
 * 静态跨语言 parity（后端 422 文案 ↔ 前端提示语）放 pytest 侧，见
 * backend/tests/test_consistency_audit_error_parity_r104_20261018.py
 * （前端测试无法读 .py，沿用 R71/R75b 的同源惯例）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// 捕获拦截器回调：与 reviewApiContract.test.ts 的「no-op 拦截器」不同，
// 这里必须真正保存 onFulfilled/onRejected，否则测不到错误链路。
const h = vi.hoisted(() => {
  const hooks: { ok: ((r: any) => any) | null; err: ((e: any) => Promise<any>) | null } = {
    ok: null,
    err: null,
  };
  const mk = (method: string) => vi.fn((..._args: any[]) => Promise.resolve({ data: { ok: true } }));
  const instance: any = {
    get: mk("get"),
    post: mk("post"),
    put: mk("put"),
    patch: mk("patch"),
    delete: mk("delete"),
    interceptors: {
      request: { use: vi.fn((_f: any) => 1) },
      response: { use: vi.fn((f: any, e: any) => { hooks.ok = f; hooks.err = e; return 1; }) },
    },
  };
  return { instance, hooks };
});

vi.mock("axios", () => ({ default: { create: () => h.instance } }));

import { authGuard, clearAuthShortCircuit } from "../api";

/** 把一次 axios 失败喂给真实响应拦截器，返回最终抛出的错误对象。 */
async function runInterceptor(status: number, data: any, extra: Record<string, any> = {}): Promise<any> {
  const err: any = Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data },
    ...extra,
  });
  const cb = h.hooks.err;
  expect(cb, "响应拦截器未被注册（onRejected 为空 = 错误链路整体失效）").toBeTruthy();
  if (typeof cb !== "function") {
    throw new Error("响应拦截器未被注册（类型守卫，运行时不可达）");
  }
  try {
    await cb(err);
    return null; // 理论上不会走到：拦截器恒 Promise.reject
  } catch (e: any) {
    return e;
  }
}

beforeEach(() => {
  localStorage.clear();
  clearAuthShortCircuit();
});

afterEach(() => {
  clearAuthShortCircuit();
});

describe("R104 · 一致性审计 422/409 错误文案可达性", () => {
  // 后端 compliance.py 两条真实 422 文案（consistency-audit 端点）
  const DETAIL_NO_CONTENT = "方案尚无正文内容，无法审计";
  const DETAIL_NO_FACTS = "尚未提取项目关键事实，请先执行「全局事实提取」再审计一致性";

  it("422 字符串形 detail 逐字透传到 error.message（consistency-audit 的真实形态）", async () => {
    for (const detail of [DETAIL_NO_CONTENT, DETAIL_NO_FACTS]) {
      const e = await runInterceptor(422, { detail });
      expect(e.message, `detail 未透传: ${detail}`).toBe(detail);
      // 状态码在 error.response 上（axios 契约）；拦截器不得丢响应对象
      expect(e.response.status).toBe(422);
    }
  });

  it("409 字符串形 detail 同样透传（审核自动修复并发冲突同源形态）", async () => {
    const detail = "该章节正在生成中，请等待生成完成后再操作";
    const e = await runInterceptor(409, { detail });
    expect(e.message).toBe(detail);
  });

  it("422 数组形 detail 走 detailArrayToText 翻译，且不落成裸状态码", async () => {
    const e = await runInterceptor(422, {
      detail: [{ loc: ["body", "scheme_id"], msg: "Field required", type: "missing" }],
    });
    expect(e.message).toBeTruthy();
    expect(e.message).toContain("scheme_id");
    expect(e.message).not.toBe("Request failed with status code 422");
  });

  it("401 走 authGuard 短路并附带后端 detail 作为原因", async () => {
    const e = await runInterceptor(401, { detail: "Token 已失效，请重新配置" });
    expect(e.isAuthBlocked).toBe(true);
    expect(authGuard.blocked).toBe(true);
    expect(authGuard.reason).toContain("Token 已失效");
    // cleanup 后恢复，避免污染同文件其它用例（用例间共享模块单例）
    clearAuthShortCircuit();
    expect(authGuard.blocked).toBe(false);
  });

  it("响应体无 detail 时保留 axios 原始消息（不得清空，也不得抛错）", async () => {
    const before = "Request failed with status code 500";
    const e = await runInterceptor(500, {});
    expect(e.message).toBe(before);
  });

  it("responseType:blob 的失败体被解包后仍能取出中文 detail", async () => {
    const blob = new Blob([JSON.stringify({ detail: "PDF 转换工具缺失" })], {
      type: "application/json",
    });
    const e = await runInterceptor(500, blob);
    expect(e.message).toBe("PDF 转换工具缺失");
  });

  it("AbortController 主动取消被标记为静默分支，且原消息不被改写", async () => {
    const e = await runInterceptor(0, undefined, { code: "ERR_CANCELED", message: "canceled" });
    expect(e.name).toBe("AbortError");
    expect(e.message).toBe("canceled");
    // 静默分支不得产生任何副作用：不置鉴权短路、不标记鉴权错误
    expect(e.isAuthBlocked).toBeUndefined();
    expect(authGuard.blocked).toBe(false);
  });
});
