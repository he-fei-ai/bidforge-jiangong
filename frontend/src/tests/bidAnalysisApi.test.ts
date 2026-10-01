// @vitest-environment jsdom
/**
 * bidAnalysisApi 契约测试。
 *
 * 背景（QA 审计缺口，2026-09-26）：前端此前只有 `factsApi` 与 `docPipelineApi`
 * 的 axios 层契约测试（见 factsApi.test.ts），`bidAnalysisApi`（18 项结构化提取
 * 的全部网络调用）**完全没有网络层契约** —— 只有组件测试 mock 了返回数据，
 * 但"方法 / 路径 / 参数位置 / 超时 / startSse 的 selected_item_ids 序列化"
 * 这些极易被悄悄改坏、且失败表现为"界面静默无反应"的环节无人看护。
 *
 * 本文件把每个方法锁定到「HTTP 方法 + 路径 + 参数位置 + 超时 + startSse 序列化」
 * 四/五要素，复用 factsApi.test.ts 的 axios mock 模式（直接持有调用记录数组，
 * 避免 mock.calls 被全局 mockReset 清空的坑）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const calls: Array<{ method: string; url: string; data?: any; config?: any }> = [];
  const requestHandlers: Array<(c: any) => any> = [];

  const make = (method: string) =>
    vi.fn((url: string, a?: any, b?: any) => {
      // 对齐 axios 真实签名：
      //   post/put/patch(url, data, config) → body 在第 2 参、config 在第 3 参
      //   get/delete(url, config)           → 没有 body，config 在第 2 参
      const bodyMethod = method === "post" || method === "put" || method === "patch";
      calls.push({
        method,
        url,
        data: bodyMethod ? a : undefined,
        config: bodyMethod ? b : a,
      });
      return Promise.resolve({ data: { ok: true } });
    });

  const instance: any = {
    get: make("get"),
    post: make("post"),
    put: make("put"),
    patch: make("patch"),
    delete: make("delete"),
    interceptors: {
      request: { use: vi.fn((fn: any) => { requestHandlers.push(fn); return requestHandlers.length; }) },
      response: { use: vi.fn(() => 0) },
    },
  };
  return { calls, instance, requestHandlers };
});

vi.mock("axios", () => ({ default: { create: () => h.instance } }));

import { bidAnalysisApi } from "../api/index";

const last = () => h.calls[h.calls.length - 1];

describe("bidAnalysisApi 契约", () => {
  beforeEach(() => {
    h.calls.length = 0;
    localStorage.clear();
  });

  it("请求拦截器已注册（后端启用 API_AUTH_TOKEN 后鉴权链路依赖它）", () => {
    expect(h.requestHandlers.length).toBeGreaterThan(0);
    expect(typeof h.requestHandlers[0]).toBe("function");
  });

  it("items / item：只读 GET，item 带路径参数", async () => {
    await bidAnalysisApi.items();
    expect(last()).toMatchObject({ method: "get", url: "/bid-analysis/items" });

    await bidAnalysisApi.item("projectBasicInfo");
    expect(last()).toMatchObject({
      method: "get",
      url: "/bid-analysis/items/projectBasicInfo",
    });
  });

  it("checkSections：POST 无 body，scheme_id / project_id 走 query", async () => {
    await bidAnalysisApi.checkSections("s1", "p1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/bid-analysis/check-sections",
      data: null,
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("bidSections：GET，scheme_id / project_id 走 query", async () => {
    await bidAnalysisApi.bidSections("s1", "p1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/bid-analysis/bid-sections",
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("selectSection：POST，section 在 body，作用域走 query", async () => {
    const section = {
      section_id: "sec-1",
      section_title: "标段一",
      head_line: "第 1 行",
      description: "范围说明",
      evidence: ["p.3"],
    };
    await bidAnalysisApi.selectSection("s1", section, "p1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/bid-analysis/select-section",
      data: section,
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("results / singleResult：GET，scheme_id / project_id 走 query", async () => {
    await bidAnalysisApi.results("s1", "p1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/bid-analysis/results",
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });

    await bidAnalysisApi.singleResult("safetyMeasures", "s1", "p1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/bid-analysis/results/safetyMeasures",
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("updateResult：PUT，content 在 body、作用域走 query（人工校正 source='manual'）", async () => {
    await bidAnalysisApi.updateResult("safetyMeasures", "s1", "基坑深度 12.5m", "p1");
    expect(last()).toMatchObject({
      method: "put",
      url: "/bid-analysis/results/safetyMeasures",
      data: { content: "基坑深度 12.5m" },
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("clearResult：DELETE 带作用域 query", async () => {
    await bidAnalysisApi.clearResult("safetyMeasures", "s1", "p1");
    expect(last()).toMatchObject({
      method: "delete",
      url: "/bid-analysis/results/safetyMeasures",
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });
  });

  it("start：POST，整包 data 在 body、放宽超时到 600s", async () => {
    const data = {
      scheme_id: "s1",
      project_id: "p1",
      // ✅ as const：对象字面量里的 "full" 会被推断成 string，无法赋给
      //    mode?: "key" | "full" | "custom" | "item" 联合类型（TS2345）。
      mode: "full" as const,
      selected_item_ids: ["projectBasicInfo"],
      force_rerun: true,
    };
    await bidAnalysisApi.start(data);
    expect(last()).toMatchObject({
      method: "post",
      url: "/bid-analysis/start",
      data,
      config: { timeout: 600000 },
    });
    // 默认不带 params（作用域已在 body.scheme_id 内）
    expect(last().config.params).toBeUndefined();
  });

  it("startSse：返回 {path, params}，selected_item_ids 必须 JSON.stringify、force_rerun 为字符串", async () => {
    const r = bidAnalysisApi.startSse("s1", "p1", {
      mode: "custom",
      selectedItemIds: ["projectBasicInfo", "safetyMeasures"],
      forceRerun: true,
    });
    expect(r.path).toBe("/bid-analysis/start-sse");
    expect(r.params).toEqual({
      scheme_id: "s1",
      project_id: "p1",
      mode: "custom",
      selected_item_ids: JSON.stringify(["projectBasicInfo", "safetyMeasures"]),
      force_rerun: "true",
    });

    // 不传可选项 → 仅必填 scheme_id，且不带 selected_item_ids / force_rerun
    const r2 = bidAnalysisApi.startSse("s1");
    expect(r2.params).toEqual({ scheme_id: "s1" });
    expect(r2.params.selected_item_ids).toBeUndefined();
    expect(r2.params.force_rerun).toBeUndefined();
  });

  it("classify：POST，body 可空对象、作用域走 query", async () => {
    await bidAnalysisApi.classify("s1", "p1", {
      scheme_name: "深基坑支护专项施工方案",
      params: { depth: 12.5 },
      extra_text: "周边有地铁",
    });
    expect(last()).toMatchObject({
      method: "post",
      url: "/bid-analysis/classify",
      data: {
        scheme_name: "深基坑支护专项施工方案",
        params: { depth: 12.5 },
        extra_text: "周边有地铁",
      },
      config: { params: { scheme_id: "s1", project_id: "p1" } },
    });

    // 不传 body → 默认空对象（后端 classify 接受空 body 返回 disabled）
    await bidAnalysisApi.classify("s1", "p1");
    expect(last().data).toEqual({});
    expect(last().config.params).toEqual({ scheme_id: "s1", project_id: "p1" });
  });
});
