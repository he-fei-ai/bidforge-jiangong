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
import { complianceApi, reviewApi, reviewAutoFixApi } from "../api";

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

  // =========================================================================
  // ✅ 2026-10-06 缺口收口：HTTP 契约从 3 个方法补齐到全量
  //
  // 为什么重要：所有组件用例都整体 mock 了 api 模块 → 只能证明
  // 「组件如何调用」，无法发现「axios 参数放错位（body → query）或
  // URL / timeout 漂移」。这类漂移在生产上表现为「接口 422 但前端无感」。
  // 本组用例锁定全部 20 个方法的契约。
  // =========================================================================
  it("complianceApi 其余方法：URL / 参数位置 / timeout", async () => {
    // 规范符合性检查（AI，120s）
    await complianceApi.check({ scheme_id: "s1", rule_ids: ["CMP-01"] });
    expect(last()).toMatchObject({
      method: "post", url: "/compliance/check",
      data: { scheme_id: "s1", rule_ids: ["CMP-01"] },
    });
    expect(last().config.timeout).toBe(120000);

    // 专家论证预检（AI，120s）
    await complianceApi.expertReview({ scheme_id: "s1" });
    expect(last()).toMatchObject({
      method: "post", url: "/compliance/expert-review", data: { scheme_id: "s1" },
    });
    expect(last().config.timeout).toBe(120000);

    // 一致性审计（AI，300s）
    await complianceApi.consistencyAudit("s1");
    expect(last()).toMatchObject({ method: "post", url: "/compliance/consistency-audit/s1" });
    expect(last().config.timeout).toBe(300000);

    // 一致性审计最近一次（GET）
    await complianceApi.consistencyLatest("s1");
    expect(last()).toMatchObject({
      method: "get", url: "/compliance/consistency-audit/s1/latest",
    });

    // 历史结果分页
    await complianceApi.results("s1", "compliance", 30);
    expect(last()).toMatchObject({
      method: "get", url: "/compliance/results/s1",
      config: { params: { check_type: "compliance", limit: 30 } },
    });

    // 规则目录 / 论证必要项
    await complianceApi.rules();
    expect(last()).toMatchObject({ method: "get", url: "/compliance/rules" });
    await complianceApi.expertItems();
    expect(last()).toMatchObject({
      method: "get", url: "/compliance/expert-review/items",
    });
  });

  it("reviewApi 其余方法：URL / 参数位置", async () => {
    await reviewApi.summary("s1");
    expect(last()).toMatchObject({ method: "get", url: "/schemes/s1/review/summary" });

    await reviewApi.checklist("s1");
    expect(last()).toMatchObject({ method: "get", url: "/schemes/s1/review/checklist" });

    await reviewApi.reviewSection("s1", "sec-1", { to_status: "approved", reviewer: "张三" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/sections/sec-1",
      data: { to_status: "approved", reviewer: "张三" },
    });

    await reviewApi.batch("s1", { section_ids: ["sec-1"], to_status: "approved" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/sections/batch",
      data: { section_ids: ["sec-1"], to_status: "approved" },
    });
    // 批量端点必须在 /sections/batch 而非 /sections/{id}
    expect(last().url).not.toContain("{");
  });

  it("reviewAutoFixApi 全部方法：URL / body / timeout", async () => {
    await reviewAutoFixApi.capabilities("s1");
    expect(last()).toMatchObject({
      method: "get", url: "/schemes/s1/review/autofix/capabilities",
    });

    await reviewAutoFixApi.plan("s1", { rule_id: "CON-01" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/plan",
      data: { rule_id: "CON-01" },
    });
    expect(last().config.timeout).toBe(120000);

    await reviewAutoFixApi.apply("s1", { rule_id: "CON-01", section_id: "sec-1" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/apply",
      data: { rule_id: "CON-01", section_id: "sec-1" },
    });
    expect(last().config.timeout).toBe(180000);

    await reviewAutoFixApi.rollback("s1", { snapshot_id: "ver-1" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/rollback",
      data: { snapshot_id: "ver-1" },
    });

    await reviewAutoFixApi.repairs("s1", 5);
    expect(last()).toMatchObject({
      method: "get", url: "/schemes/s1/review/autofix/repairs",
      config: { params: { limit: 5 } },
    });

    await reviewAutoFixApi.collect("s1", { scope: "all_blocking" });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/collect",
      data: { scope: "all_blocking" },
    });
    expect(last().config.timeout).toBe(120000);

    await reviewAutoFixApi.stage("s1", { rule_ids: ["DLV-05"] });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/stage",
      data: { rule_ids: ["DLV-05"] },
    });
    expect(last().config.timeout).toBe(180000);

    await reviewAutoFixApi.confirm("s1", { batch_id: "b1", accept_all: true });
    expect(last()).toMatchObject({
      method: "post", url: "/schemes/s1/review/autofix/confirm",
      data: { batch_id: "b1", accept_all: true },
    });
    expect(last().config.timeout).toBe(180000);
  });

  it("自动修复链路的三个写端点不得被改成 GET（写路径漂移 = 静默失效）", async () => {
    for (const fn of [reviewAutoFixApi.apply, reviewAutoFixApi.rollback,
                      reviewAutoFixApi.confirm, reviewAutoFixApi.stage]) {
      h.calls.length = 0;
      await (fn as any)("s1", { rule_id: "x", snapshot_id: "y", batch_id: "z" });
      expect(last().method, `${fn.name} 必须是 POST`).toBe("post");
    }
  });
});
