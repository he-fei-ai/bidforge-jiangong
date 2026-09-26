// @vitest-environment jsdom
/**
 * factsApi 契约测试。
 *
 * 背景（QA 审计缺口）：前端此前完全没有 factsApi 的测试，全局事实的上传、
 * 解析、审核、删除等调用只靠人工点页面验证。一旦后端参数名/HTTP 方法变更
 * （例如 scheme_id 改成 query 还是 body、multipart 字段名、force 开关），
 * 失败方式是**界面静默无反应**或"操作成功但没生效"，很难定位。
 *
 * 本文件把每个方法锁定到「HTTP 方法 + 路径 + 参数位置 + 超时」四要素，
 * 并覆盖 X-API-Key 注入链路（后端 API_AUTH_TOKEN 启用后的关键依赖）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const calls: Array<{ method: string; url: string; data?: any; config?: any }> = [];
  // 直接持有注册进来的拦截器函数本体。不要读 `use.mock.calls`：
  // vitest 的 mock 调用记录可能被全局 mockReset / restoreAllMocks 清空，
  // 届时 `mock.calls[0]` 是 undefined（历史报错：Cannot read properties of
  // undefined (reading '0')），而普通数组不受影响。
  const requestHandlers: Array<(c: any) => any> = [];
  const responseHandlers: Array<{ ok: (r: any) => any; err: (e: any) => any }> = [];

  const make = (method: string) =>
    vi.fn((url: string, a?: any, b?: any) => {
      // 对齐 axios 真实签名：
      //   post/put/patch(url, data, config) → body 在第 2 参、config 在第 3 参
      //   get/delete(url, config)           → 没有 body，config 在第 2 参
      // 早期实现把 config 固定当第 3 参，导致所有 GET 请求的 config 记成
      // undefined，list / listDocuments 的 query 断言必然失败。
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
      request: {
        use: vi.fn((fn: any) => {
          requestHandlers.push(fn);
          return requestHandlers.length;
        }),
      },
      response: {
        use: vi.fn((ok: any, err: any) => {
          responseHandlers.push({ ok, err });
          return responseHandlers.length;
        }),
      },
    },
  };
  return { calls, instance, requestHandlers, responseHandlers };
});

vi.mock("axios", () => ({ default: { create: () => h.instance } }));

import { factsApi, docPipelineApi } from "../api/index";

const last = () => h.calls[h.calls.length - 1];

describe("factsApi 契约", () => {
  beforeEach(() => {
    h.calls.length = 0;
    localStorage.clear();
  });

  it("chapters / dangerCheck / categoryMap：作用域与 body/query 契约", async () => {
    await factsApi.categoryMap();
    expect(last()).toMatchObject({ method: "get", url: "/global-facts/category-map" });

    const ctrl = new AbortController();
    await factsApi.chapters("s1", { signal: ctrl.signal });
    expect(last()).toMatchObject({
      method: "get",
      url: "/global-facts/chapters",
      config: { params: { scheme_id: "s1" }, signal: ctrl.signal },
    });

    await factsApi.dangerCheck(
      { scheme_name: "深基坑支护专项施工方案" }, "s1", { signal: ctrl.signal });
    expect(last()).toMatchObject({
      method: "post",
      url: "/global-facts/danger-check",
      data: { scheme_name: "深基坑支护专项施工方案" },
    });
    expect(last().config.params).toEqual({ scheme_id: "s1" });
    expect(last().config.signal).toBe(ctrl.signal);
  });

  it("list / listDocuments：透传 AbortSignal，供方案切换取消旧响应", async () => {
    const ctrl = new AbortController();
    await factsApi.list("s1", { signal: ctrl.signal });
    expect(last().config.signal).toBe(ctrl.signal);
    await factsApi.listDocuments({ schemeId: "s1", signal: ctrl.signal });
    expect(last().config.signal).toBe(ctrl.signal);
  });

  it("list：GET /global-facts 且 scheme_id 走 query", async () => {
    await factsApi.list("s1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/global-facts",
      config: { params: { scheme_id: "s1" } },
    });
  });

  it("create：POST /global-facts，body 为数据、scheme_id 走 query", async () => {
    const payload = { title: "人员角色", content: "- **项目经理**: 张伟" };
    await factsApi.create(payload, "s1");
    expect(last().method).toBe("post");
    expect(last().url).toBe("/global-facts");
    expect(last().data).toEqual(payload);
    expect(last().config.params).toEqual({ scheme_id: "s1" });
  });

  it("update / resolve / resolveConflict / delete：写入时透传当前 scheme 作用域", async () => {
    await factsApi.update("f1", { title: "x" }, "s1");
    expect(last()).toMatchObject({
      method: "patch", url: "/global-facts/f1",
      config: { params: { scheme_id: "s1" } },
    });

    await factsApi.resolve("f1", "s1");
    expect(last()).toMatchObject({
      method: "patch", url: "/global-facts/f1/resolve",
      config: { params: { scheme_id: "s1" } },
    });

    await factsApi.resolveConflict("f1", "C35", "s1");
    expect(last()).toMatchObject({
      method: "patch", url: "/global-facts/f1/resolve-conflict",
      config: { params: { scheme_id: "s1" } },
    });

    await factsApi.delete("g1", "s1");
    expect(last()).toMatchObject({
      method: "delete", url: "/global-facts/g1",
      config: { params: { scheme_id: "s1" } },
    });
  });

  it("batchResolve：scheme_id 与 fact_ids 必须放在 body（后端读取 body）", async () => {
    await factsApi.batchResolve("s1", ["f1", "f2"]);
    expect(last().method).toBe("post");
    expect(last().url).toBe("/global-facts/batch-resolve");
    expect(last().data).toEqual({ scheme_id: "s1", fact_ids: ["f1", "f2"] });
  });

  it("uploadDocuments：multipart 字段名 files、scheme_id 走 query、放宽超时", async () => {
    const files = [new File(["a"], "a.txt"), new File(["b"], "b.csv")];
    await factsApi.uploadDocuments(files, "s1");

    const call = last();
    expect(call.method).toBe("post");
    expect(call.url).toBe("/global-facts/upload-documents");
    expect(call.data).toBeInstanceOf(FormData);
    const form = call.data as FormData;
    expect(form.getAll("files").length).toBe(2);
    expect(call.config.params).toEqual({ scheme_id: "s1" });
    // 后端按扩展名+文件头双重校验，这里锁定请求头与超时不被误改
    expect(call.config.headers["Content-Type"]).toBe("multipart/form-data");
    expect(call.config.timeout).toBe(120000);
  });

  it("uploadDocuments：透传 AbortSignal（用户取消上传）", async () => {
    const ctrl = new AbortController();
    await factsApi.uploadDocuments([new File(["a"], "a.txt")], "s1", {
      signal: ctrl.signal,
    });
    expect(last().config.signal).toBe(ctrl.signal);
  });

  it("parseDocument：force=false 时不带 force 参数，force=true 时带上", async () => {
    await factsApi.parseDocument("d1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/global-facts/documents/d1/parse",
    });
    expect(last().data).toBeNull();
    expect(last().config.params).toBeUndefined();
    expect(last().config.timeout).toBe(300000);

    await factsApi.parseDocument("d1", true);
    expect(last().config.params).toEqual({ force: true });
  });

  it("parseAllDocuments：force 仅在为 true 时出现在 query", async () => {
    await factsApi.parseAllDocuments("s1", "p1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/global-facts/documents/parse-all",
    });
    expect(last().config.params).toEqual({ scheme_id: "s1", project_id: "p1" });
    expect(last().config.timeout).toBe(600000);

    await factsApi.parseAllDocuments("s1", undefined, true);
    expect(last().config.params).toEqual({ scheme_id: "s1", project_id: undefined, force: true });
  });

  // list Documents 既有契约仍保持；新 signal 为可选，不改变旧调用方。
  it("listDocuments：以 scheme_id 为主键查询", async () => {
    await factsApi.listDocuments({ schemeId: "s1" });
    expect(last()).toMatchObject({
      method: "get",
      url: "/global-facts/documents",
      config: { params: { scheme_id: "s1", project_id: undefined } },
    });
  });

  it("deleteDocument：DELETE 按 doc_id", async () => {
    await factsApi.deleteDocument("d1");
    expect(last()).toMatchObject({
      method: "delete",
      url: "/global-facts/documents/d1",
    });
  });

  // ---- 2026-09-21 追加：此前五个方法零契约覆盖 ----

  it("clearAll：POST /global-facts/clear，scheme_id 在 body，放宽超时", async () => {
    await factsApi.clearAll("s1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/global-facts/clear",
      data: { scheme_id: "s1" },
    });
    expect(last().config.timeout).toBe(60000);
  });

  it("categories：GET /global-facts/categories（事实分类白名单）", async () => {
    await factsApi.categories();
    expect(last()).toMatchObject({ method: "get", url: "/global-facts/categories" });
  });

  it("previewDocument：GET /documents/{id}/preview，max_chars 走 query", async () => {
    await factsApi.previewDocument("d1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/global-facts/documents/d1/preview",
      config: { params: { max_chars: 5000 } },
    });
    await factsApi.previewDocument("d1", 800);
    expect(last().config.params).toEqual({ max_chars: 800 });
  });

  it("updateDocumentCategory：PATCH /documents/{id}/category，doc_category 在 body", async () => {
    await factsApi.updateDocumentCategory("d1", "contract");
    expect(last()).toMatchObject({
      method: "patch",
      url: "/global-facts/documents/d1/category",
      data: { doc_category: "contract" },
    });
  });

  it("categoryOptions：GET /documents/category-options（文档分类，与事实分类是两套）", async () => {
    await factsApi.categoryOptions();
    expect(last()).toMatchObject({
      method: "get",
      url: "/global-facts/documents/category-options",
    });
  });

  it("parseDocument / parseAllDocuments：透传 AbortSignal（卸载中断解析）", async () => {
    const ctrl = new AbortController();
    await factsApi.parseDocument("d1", false, { signal: ctrl.signal });
    expect(last().config.signal).toBe(ctrl.signal);
    const ctrl2 = new AbortController();
    await factsApi.parseAllDocuments("s1", "p1", false, { signal: ctrl2.signal });
    expect(last().config.signal).toBe(ctrl2.signal);
  });

  // ✅ 缺口修复（2026-09-24）：后端 POST /global-facts/adjust 早已实现，
  //    前端 api 层从未封装 → 「自然语言批量调整事实」能力对 UI 完全不可见。
  it("adjust：POST /global-facts/adjust，instruction 在 body，默认不落库", async () => {
    await factsApi.adjust({ instruction: "把年份统一改成 2026", scheme_id: "s1" });
    expect(last()).toMatchObject({
      method: "post",
      url: "/global-facts/adjust",
    });
    expect(last().data).toMatchObject({
      instruction: "把年份统一改成 2026",
      scheme_id: "s1",
    });
    // 零数据风险默认：不显式传 apply 时后端按 apply=false 处理（仅返回计划）
    expect(last().data.apply).toBeUndefined();
  });

  it("adjust：apply=true 时可携带已预览确认的 operations", async () => {
    const operations = [{ op: "update", fact_id: "f1", value: "5.8m" }];
    await factsApi.adjust({ instruction: "改值", scheme_id: "s1", apply: true, operations });
    expect(last().data).toMatchObject({ apply: true, scheme_id: "s1", operations });
  });

  it("adjust：apply=true 时才请求落库（后端默认 False 的显式反转）", async () => {
    await factsApi.adjust({ instruction: "删除空值事实", project_id: "p1", apply: true });
    expect(last().data).toMatchObject({ apply: true, project_id: "p1" });
  });
});

describe("docPipelineApi 契约（资料四层存储查询链路）", () => {
  // 背景：后端 routers/doc_pipeline.py 已实现 9 个端点，但前端 api/index.ts
  // 此前完全无封装 —— 「四层存储」能力对用户不可见，属模块间调用链断裂。
  // 现补封装，用本块测试钉住「路径 + HTTP 方法 + 参数名 + 超时」，
  // 避免与 global-facts 链路的 /documents/* 前缀混淆（两套路由并存）。
  beforeEach(() => {
    h.calls.length = 0;
    localStorage.clear();
  });

  it("status / freshness：GET /documents/{id}/status、/freshness", async () => {
    await docPipelineApi.status("d1");
    expect(last()).toMatchObject({ method: "get", url: "/documents/d1/status" });

    await docPipelineApi.freshness("d1");
    expect(last()).toMatchObject({ method: "get", url: "/documents/d1/freshness" });
  });

  it("extractions：后端参数名固定为 type（传错名会被 400 拒或静默忽略）", async () => {
    await docPipelineApi.extractions("d1", "project_info");
    expect(last()).toMatchObject({
      method: "get",
      url: "/documents/d1/extractions",
      config: { params: { type: "project_info" } },
    });

    // 未指定类别 → 不带 params（后端返回全部类别）
    await docPipelineApi.extractions("d1");
    expect(last().config.params).toEqual({});
  });

  it("chunks：按 chunk_type 过滤", async () => {
    await docPipelineApi.chunks("d1", "paragraph");
    expect(last()).toMatchObject({
      method: "get",
      url: "/documents/d1/chunks",
      config: { params: { chunk_type: "paragraph" } },
    });
  });

  it("completeness：默认读缓存，refresh=true 时下发 refresh 参数", async () => {
    await docPipelineApi.completeness("d1");
    expect(last().config.params).toEqual({});

    await docPipelineApi.completeness("d1", true);
    expect(last()).toMatchObject({
      method: "get",
      url: "/documents/d1/completeness",
      config: { params: { refresh: true } },
    });
  });

  it("reparse：POST 带 mode，OCR 场景需放宽到 300s 超时", async () => {
    await docPipelineApi.reparse("d1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/documents/d1/reparse",
      data: { mode: "incremental" },
      config: { timeout: 300000 },
    });

    await docPipelineApi.reparse("d1", "force");
    expect(last().data).toEqual({ mode: "force" });
  });

  it("syncExtractions / crossCheck：POST 无 body，超时按耗时设定", async () => {
    await docPipelineApi.syncExtractions("d1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/documents/d1/sync-extractions",
      data: null,
      config: { timeout: 60000 },
    });

    await docPipelineApi.crossCheck("d1");
    expect(last()).toMatchObject({
      method: "post",
      url: "/documents/d1/cross-check",
      config: { timeout: 120000 },
    });
  });

  it("projectIndex：GET /projects/{id}/documents/index（项目级索引，非单文档）", async () => {
    await docPipelineApi.projectIndex("p1");
    expect(last()).toMatchObject({
      method: "get",
      url: "/projects/p1/documents/index",
    });
  });
});

describe("API Token 注入链路", () => {
  beforeEach(() => {
    // 必须清空：否则上一个用例写入的 api_token 会串到"未配置 token"用例，
    // 让"本地默认不注入"的断言假失败。
    localStorage.clear();
  });

  it("请求拦截器已注册（未注册则后端启用 API_AUTH_TOKEN 后全部 401）", () => {
    expect(h.requestHandlers.length).toBeGreaterThan(0);
    expect(typeof h.requestHandlers[0]).toBe("function");
  });

  it("localStorage.api_token 存在时注入 X-API-Key", () => {
    const handler = h.requestHandlers[0];
    localStorage.setItem("api_token", "tok-123");
    const config: any = { headers: {} };
    handler(config);
    expect(config.headers["X-API-Key"]).toBe("tok-123");
  });

  it("未配置 token 时不注入任何鉴权头（本地默认行为不变）", () => {
    const handler = h.requestHandlers[0];
    const config: any = { headers: {} };
    handler(config);
    expect(config.headers["X-API-Key"]).toBeUndefined();
  });

  it("不覆盖调用方显式设置的同名头", () => {
    const handler = h.requestHandlers[0];
    localStorage.setItem("api_token", "tok-123");
    const config: any = { headers: { "X-API-Key": "explicit" } };
    handler(config);
    expect(config.headers["X-API-Key"]).toBe("explicit");
  });

  it("SSE（fetch 通道）同样携带 X-API-Key：sseFetch / sseGetStream 复用 authHeaders", async () => {
    localStorage.setItem("api_token", "tok-abc");
    const { sseFetch } = await import("../api/index");
    const seen: any[] = [];
    const origFetch = globalThis.fetch;
    (globalThis as any).fetch = vi.fn(async (_url: string, init: any) => {
      seen.push(init?.headers || {});
      return {
        ok: true,
        body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }), cancel: async () => {} }) },
      };
    });
    try {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      for await (const _ of sseFetch("/sse/test", { a: 1 })) {
        /* 无数据 */
      }
    } finally {
      globalThis.fetch = origFetch;
    }
    expect(seen[0]["X-API-Key"]).toBe("tok-abc");
  });
});
