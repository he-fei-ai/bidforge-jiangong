// @vitest-environment jsdom
/**
 * R210（2026-11-10）全局事实模块 · 前端护栏
 *
 * 锁住 R210 落地的 3 项前端修复 + 1 项跨端契约：
 *
 *  FIX-A P1/P2  写保护 409 的 `detail` 是**对象** `{code, message}`；此前各处一律
 *               按 string 消费 ⇒ `msg.error({...})` 在 React 18 下抛
 *               "Objects are not valid as a React child" ⇒ **整条提示不出现**
 *               （用户体感「点了没反应」）。现统一走 `factErrorText` 单一出口。
 *  FIX-B P2     手工新增被后端「同名同值幂等去重」时**一条都没写**，前端此前
 *               无条件提示「事实条目已新增」⇒ 假成功（列表无变化却报成功）。
 *  FIX-C P2     「待确认清单」须含**来源已变化（过期）**事实：过期事实被注入门控
 *               （is_stale=0）挡在正文/导出之外，若不进清单 ⇒ 审核面板显示
 *               「0 项待确认」而正文实际少了一条事实。
 *  FIX-D P3     `factsApi.create/delete` 补返回泛型（契约不再靠"响应整体丢弃"）。
 *
 * 护栏风格：行为实证优先（渲染 / 调用断言），源码棘轮仅用于「接线必须在位」这类
 * 无法用纯函数表达的点。
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, renderHook, cleanup, screen, waitFor } from "@testing-library/react";
import React from "react";

import { factErrorText } from "../pages/schemeWorkbench/factsShared";
import { useFactsCrud } from "../pages/schemeWorkbench/useFactsCrud";
import { PendingConfirmPanel } from "../components/review/PendingConfirmPanel";

import { factsApi, defectsApi } from "../api";

afterEach(cleanup);
vi.restoreAllMocks();

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ===========================================================================
// FIX-A · 统一错误文案出口（对象 detail）
// ===========================================================================
describe("factErrorText · 写保护 409 的对象 detail", () => {
  it("对象 detail 取 message（409 facts_write_conflict 的真实形状）", () => {
    const err = {
      response: {
        data: {
          detail: { code: "facts_write_conflict", message: "项目事实正在被「ai_extraction」写入，请稍后重试" },
        },
      },
    };
    expect(factErrorText(err, "兜底")).toBe("项目事实正在被「ai_extraction」写入，请稍后重试");
  });

  it("对象 detail 无 message 时退回 code", () => {
    const err = { response: { data: { detail: { code: "facts_write_conflict" } } } };
    expect(factErrorText(err, "兜底")).toBe("facts_write_conflict");
  });

  it("字符串 detail 原样取（既有 400/404 契约不变）", () => {
    const err = { response: { data: { detail: "同名事实已存在" } } };
    expect(factErrorText(err, "兜底")).toBe("同名事实已存在");
  });

  it("无 detail 时退回顶层 message / 兜底文案", () => {
    expect(factErrorText({ message: "Network Error" }, "兜底")).toBe("Network Error");
    expect(factErrorText({}, "兜底")).toBe("兜底");
    expect(factErrorText(null, "兜底")).toBe("兜底");
  });

  it("对象 detail 不得渲染成 [object Object]", () => {
    const err = { response: { data: { detail: { code: "c", message: "冲突" } } } };
    expect(factErrorText(err, "兜底")).not.toBe("[object Object]");
  });
});

// ===========================================================================
// FIX-B · 手工新增去重不得谎报「已新增」
// ===========================================================================
describe("useFactsCrud · 手工新增的同名同值去重（假成功防线）", () => {
  function setup(createImpl: () => Promise<unknown>) {
    const msg = {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
    };
    vi.spyOn(factsApi, "create").mockImplementation(createImpl as never);
    const loadFacts = vi.fn().mockResolvedValue(undefined);
    const form = {
      validateFields: vi.fn().mockResolvedValue({ title: "合同工期", content: "- **合同工期**: 30天" }),
      resetFields: vi.fn(),
      setFieldsValue: vi.fn(),
    };
    const { result } = renderHook(() =>
      useFactsCrud({
        id: "s1",
        msg: msg as never,
        modal: { confirm: vi.fn() } as never,
        loadFacts: loadFacts as never,
        factEditingId: null,
        setFactEditingId: vi.fn(),
        factEditingCategory: "",
        setFactEditingCategory: vi.fn(),
        factForm: form as never,
        setFactModalOpen: vi.fn(),
        setFactSubmitting: vi.fn(),
        factItemEditing: null,
        setFactItemEditing: vi.fn(),
        factItemForm: form as never,
        setFactItemModalOpen: vi.fn(),
        setFactItemSubmitting: vi.fn(),
        setFactsFilter: vi.fn(),
        setFactsSegmentStats: vi.fn(),
        setFactsCrossConflicts: vi.fn(),
      }),
    );
    return { msg, result };
  }

  it("deduplicated>0 → 提示「未重复新增」，不得提示「已新增」", async () => {
    const { msg, result } = setup(async () => ({
      data: { id: "g1", ok: true, deduplicated: 1 },
    }));
    await result.current.submitFact();
    await waitFor(() => {
      expect(msg.info).toHaveBeenCalled();
    });
    expect(msg.success).not.toHaveBeenCalledWith("事实条目已新增");
  });

  it("deduplicated=0 → 仍提示「事实条目已新增」（零行为变更）", async () => {
    const { msg, result } = setup(async () => ({
      data: { id: "g1", ok: true, deduplicated: 0 },
    }));
    await result.current.submitFact();
    await waitFor(() => {
      expect(msg.success).toHaveBeenCalledWith("事实条目已新增");
    });
  });

  it("响应缺 deduplicated 字段（后端旧版）→ 仍按「已新增」处理", async () => {
    const { msg, result } = setup(async () => ({ data: { id: "g1", ok: true } }));
    await result.current.submitFact();
    await waitFor(() => {
      expect(msg.success).toHaveBeenCalledWith("事实条目已新增");
    });
  });
});

// ===========================================================================
// FIX-C · 待确认清单须含「来源已变化」
// ===========================================================================
describe("PendingConfirmPanel · 过期事实进入待确认清单", () => {
  const staleFact = {
    fact_id: "f-stale",
    name: "合同工期",
    value: "30天",
    category: "schedule",
    confidence: 0.95,
    is_resolved: true,
    is_simulated: false,
    has_conflict: false,
    is_stale: true,
  };

  beforeEach(() => {
    vi.spyOn(factsApi, "list").mockResolvedValue({
      data: { groups: [{ id: "g1", category: "schedule", title: "工期", items: [staleFact] }] },
    } as never);
    vi.spyOn(defectsApi, "list").mockResolvedValue({ data: { items: [] } } as never);
  });

  it("已确认但来源已过期的事实出现在清单并标注原因", async () => {
    render(<PendingConfirmPanel schemeId="s1" />);
    await waitFor(() => {
      expect(screen.getByText(/合同工期/)).toBeTruthy();
    });
    expect(screen.getByText(/来源已变化/)).toBeTruthy();
  });
});

// ===========================================================================
// FIX-D · api 返回泛型与接线（契约/接线锁）
// ===========================================================================
describe("factsApi 契约接线", () => {
  it("create/delete 有返回泛型（类型层契约）", async () => {
    const apiSrc = (await import("../api/index.ts?raw")).default as unknown as string;
    expect(apiSrc).toContain("api.post<FactCreateResponse>(\"/global-facts\"");
    expect(apiSrc).toContain("api.delete<FactDeleteResponse>(`/global-facts/${id}`");
  });

  it("事实写路径的错误文案统一走 factErrorText（不再内联取 detail）", async () => {
    const crudSrc = (await import("../pages/schemeWorkbench/useFactsCrud.ts?raw")).default as unknown as string;
    const tabSrc = (await import("../pages/schemeWorkbench/tabs/FactsTab.tsx?raw")).default as unknown as string;
    expect(crudSrc).toContain("factErrorText");
    expect(tabSrc).toContain("factErrorText");
    expect(crudSrc).not.toContain("e?.response?.data?.detail ||");
  });
});

// ===========================================================================
// FIX-E · CRUD 守卫不得静默 return（假动作防线）
// ===========================================================================
describe("useFactsCrud · 守卫必须有可见反馈", () => {
  function setupWith(item: Record<string, unknown>) {
    const msg = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
    vi.spyOn(factsApi, "resolve").mockResolvedValue({ data: { ok: true } } as never);
    const form = {
      validateFields: vi.fn().mockResolvedValue({ name: "n", value: "v" }),
      resetFields: vi.fn(),
      setFieldsValue: vi.fn(),
    };
    const { result } = renderHook(() =>
      useFactsCrud({
        id: "s1",
        msg: msg as never,
        modal: { confirm: vi.fn() } as never,
        loadFacts: vi.fn().mockResolvedValue(undefined) as never,
        factEditingId: null,
        setFactEditingId: vi.fn(),
        factEditingCategory: "",
        setFactEditingCategory: vi.fn(),
        factForm: form as never,
        setFactModalOpen: vi.fn(),
        setFactSubmitting: vi.fn(),
        factItemEditing: null,
        setFactItemEditing: vi.fn(),
        factItemForm: form as never,
        setFactItemModalOpen: vi.fn(),
        setFactItemSubmitting: vi.fn(),
        setFactsFilter: vi.fn(),
        setFactsSegmentStats: vi.fn(),
        setFactsCrossConflicts: vi.fn(),
      }),
    );
    return { msg, result, item };
  }

  it("fact_id 缺失时点「确认」必须给 msg.info 反馈（不得静默）", async () => {
    const { msg, result, item } = setupWith({});
    await result.current.handleResolveFactItem(item as never);
    await waitFor(() => {
      expect(msg.info).toHaveBeenCalled();
    });
    expect(factsApi.resolve).not.toHaveBeenCalled();
  });

  it("fact_id 缺失时点「裁决」必须给 msg.info 反馈（不得静默）", async () => {
    const { msg, result, item } = setupWith({});
    await result.current.handleResolveConflictItem(item as never, "v");
    await waitFor(() => {
      expect(msg.info).toHaveBeenCalled();
    });
  });
});

// ===========================================================================
// FIX-F · 组件副本门禁成对锁（FactsTab ↔ UploadParseTab / DocumentParseList）
// ===========================================================================
describe("FactsTab 门禁维度与副本一致", () => {
  it("提取/新增/清空/重解析/删除 均含 parsingDocId 维度", async () => {
    const tabSrc = (await import("../pages/schemeWorkbench/tabs/FactsTab.tsx?raw")).default as unknown as string;
    // 三维门禁（缺 parsingDocId）不得再出现
    expect(tabSrc).not.toContain("disabled={generating || uploadingFacts || parsingDocs}");
    // 行内删除必须与 DocumentParseList 副本同口径
    expect(tabSrc).toContain("disabled={parsingDocs || uploadingFacts || parsingDocId === doc.id}");
    // 全部重解析必须走四维 docParseDisabled
    expect(tabSrc).not.toContain("disabled={generating || parsingDocs}");
  });

  it("轮询入口必须传 skipAbortPrev=true（不得 abort 用户刷新）", async () => {
    const wfSrc = (await import("../pages/schemeWorkbench/useDocumentWorkflow.tsx?raw")).default as unknown as string;
    const loaderSrc = (await import("../pages/schemeWorkbench/useWorkbenchDataLoaders.ts?raw")).default as unknown as string;
    expect(wfSrc).toContain("loadDocumentsPollRef.current(true)");
    expect(loaderSrc).toContain("skipAbortPrev");
  });
});
