// @vitest-environment jsdom
/** 审核与预检 HTTP 契约测试：防止组件 mock 通过、真实 axios 参数却漂移。 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const calls: Array<{ method: string; url: string; data?: any; config?: any }> = [];
  const make = (method: string) => vi.fn((url: string, a?: any, b?: any) => {
    const bodyMethod = method === "post" || method === "put" || method === "patch";
    calls.push({ method, url, data: bodyMethod ? a : undefined, config: bodyMethod ? b : a });
    return Promise.resolve({ data: { ok: true } });
  });
  const instance: any = {
    get: make("get"), post: make("post"), put: make("put"), patch: make("patch"), delete: make("delete"),
    interceptors: {
      request: { use: vi.fn(() => 1) },
      response: { use: vi.fn(() => 1) },
    },
  };
  return { calls, instance };
});

vi.mock("axios", () => ({ default: { create: () => h.instance } }));
import { complianceApi, reviewApi } from "../api";

const last = () => h.calls[h.calls.length - 1];

beforeEach(() => { h.calls.length = 0; localStorage.clear(); });

describe("审核与预检 API 契约", () => {
  it("总览 force=false 不发送空 query，force=true 才发送布尔参数", async () => {
    await complianceApi.overview("s1", false);
    expect(last()).toMatchObject({ method: "post", url: "/compliance/overview/s1", data: null });
    expect(last().config.params).toBeUndefined();
    await complianceApi.overview("s1", true);
    expect(last().config.params).toEqual({ force: true });
    expect(last().config.timeout).toBe(120000);
  });

  it("总检历史和整改报告路径/分页参数固定", async () => {
    await complianceApi.runs("s1", 10);
    expect(last()).toMatchObject({ method: "get", url: "/compliance/runs/s1", config: { params: { limit: 10 } } });
    await complianceApi.report("s1");
    expect(last()).toMatchObject({ method: "get", url: "/compliance/report/s1" });
  });

  it("方案提交把严格章节门禁放在 body，评审轨迹使用 query 分页", async () => {
    await reviewApi.submit("s1", { to_status: "approved", reviewer: "张三", require_all_sections_reviewed: true });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/submit",
      data: { to_status: "approved", reviewer: "张三", require_all_sections_reviewed: true },
    });
    await reviewApi.records("s1", "sec-1", 50, 100);
    expect(last()).toMatchObject({
      method: "get", url: "/schemes/s1/review/records",
      config: { params: { section_id: "sec-1", limit: 50, offset: 100 } },
    });
  });
});
