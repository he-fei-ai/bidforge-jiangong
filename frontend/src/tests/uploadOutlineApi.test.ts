// @vitest-environment jsdom
/**
 * uploadOutlineApi 契约测试（2026-10-03 解析提取模块链路修复配套）。
 *
 * 背景：uploaded_outlines 的 project_id 归属链（F1）此前前后端双双断链 ——
 * 后端 INSERT 不填、前端不传。本轮接线后，前端侧「project_id 是否真的进入
 * query、且做了 URL 编码、空值是否省略」必须钉死，否则任何人改动序列化
 * 顺序/条件，删项目级联清理（R32）会再次静默失效。
 *
 * 复用 bidAnalysisApi.test.ts 的 axios mock 模式（直接持有调用记录数组，
 * 避免 mock.calls 被全局 mockReset 清空）。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const calls: Array<{ method: string; url: string; data?: any; config?: any }> = [];
  const make = (method: string) =>
    vi.fn((url: string, a?: any, b?: any) => {
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
      request: { use: vi.fn(() => 0) },
      response: { use: vi.fn(() => 0) },
    },
  };
  return { calls, instance };
});

vi.mock("axios", () => ({ default: { create: () => h.instance } }));

import { uploadOutlineApi } from "../api/index";

const last = () => h.calls[h.calls.length - 1];

function makeFile(name = "大纲.txt"): File {
  return new File(["第一章 工程概况"], name, { type: "text/plain" });
}

describe("uploadOutlineApi.parse 契约", () => {
  beforeEach(() => {
    h.calls.length = 0;
  });

  it("基本形态：POST multipart，file 走 FormData，超时 120s", async () => {
    await uploadOutlineApi.parse(makeFile());
    const c = last();
    expect(c.method).toBe("post");
    expect(c.url).toBe("/upload-outline/parse");
    expect(c.data).toBeInstanceOf(FormData);
    expect(c.data.get("file")).toBeInstanceOf(File);
    expect(c.config.headers["Content-Type"]).toBe("multipart/form-data");
    expect(c.config.timeout).toBe(120000);
  });

  it("project_id 非空时进入 query 且 URL 编码（F1 归属链前端侧）", async () => {
    await uploadOutlineApi.parse(makeFile(), { project_id: "p 中文/1" });
    expect(last().url).toContain(`project_id=${encodeURIComponent("p 中文/1")}`);
  });

  it("project_id 未传或为空串时省略该参数（无项目上下文入口行为不变）", async () => {
    await uploadOutlineApi.parse(makeFile(), { scheme_name: "s" });
    expect(last().url).not.toContain("project_id");
    await uploadOutlineApi.parse(makeFile(), { project_id: "" });
    expect(last().url).not.toContain("project_id");
  });

  it("scheme_name 非空才发送；reorganize 按显式传入透传（true/false 都发）", async () => {
    await uploadOutlineApi.parse(makeFile(), { scheme_name: "基坑支护", reorganize: true });
    let url = last().url;
    expect(url).toContain(`scheme_name=${encodeURIComponent("基坑支护")}`);
    expect(url).toContain("reorganize=true");

    await uploadOutlineApi.parse(makeFile(), { reorganize: false });
    url = last().url;
    expect(url).toContain("reorganize=false");
    expect(url).not.toContain("scheme_name");

    await uploadOutlineApi.parse(makeFile());
    expect(last().url).toBe("/upload-outline/parse");
  });

  it("全参数组合：query 顺序稳定 scheme_name → reorganize → project_id", async () => {
    await uploadOutlineApi.parse(makeFile(), {
      scheme_name: "n", reorganize: true, project_id: "p1",
    });
    expect(last().url).toBe(
      "/upload-outline/parse?scheme_name=n&reorganize=true&project_id=p1");
  });
});

describe("uploadOutlineApi.saveAsLibrary 契约", () => {
  beforeEach(() => {
    h.calls.length = 0;
  });

  it("POST 路径带 upload id，body 原样下发", async () => {
    await uploadOutlineApi.saveAsLibrary("u1", { name: "库A", outline: [] });
    const c = last();
    expect(c.method).toBe("post");
    expect(c.url).toBe("/upload-outline/u1/save-as-library");
    expect(c.data).toEqual({ name: "库A", outline: [] });
  });
});
