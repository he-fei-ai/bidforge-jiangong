// @vitest-environment jsdom
/**
 * chart-json 预览的 chart_type 契约（2026-09-27）。
 *
 * 回归缺陷：`MarkdownRenderer` 对 chart-json 块取 `obj.type || "labor"`，
 * 载荷**省略 type** 时会凭空捏造一个 labor 类型发给后端，把甘特图/架构图
 * 载荷送进 labor 渲染器必然失败 → 预览显示"渲染引擎暂不可用"，
 * 而**导出 DOCX 却正常**（导出侧按载荷结构推断）——预览/导出错配。
 *
 * 正确契约：只对**显式声明且合法**的类型取值；其余传空串，
 * 交由后端 `infer_chart_type_from_payload` 按结构推断（单一事实来源）。
 *
 * 注：与后端 `PIL_RENDERABLE_CHART_TYPES` 的**逐项一致性**由后端用例
 * `test_chart_json_registration_parity_20260927.py` 读取本文件源码锁定
 * （前端 vitest 无 node 类型声明，不能用 fs 读盘，见该文件说明）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";
import React from "react";
import MarkdownRenderer from "../components/MarkdownRenderer";
import { RENDERABLE_CHART_TYPES } from "../utils/chartTypes";

const { renderMock, ensureMermaidMock, nextMermaidIdMock, chartsRenderMock, chartsFixMock } =
  vi.hoisted(() => ({
    renderMock: vi.fn(async (_id: string) => ({ svg: `<svg id="svg-${_id}"></svg>` })),
    ensureMermaidMock: vi.fn(async () => ({ render: renderMock })),
    nextMermaidIdMock: vi.fn(() => "mmd-test-id"),
    // 显式声明入参形状：否则 tsc 无法推断 mock.calls[0][0] 的类型
    chartsRenderMock: vi.fn(async (_payload: { chart_type: string; code: string }) => ({
      data: new Uint8Array([137, 80, 78, 71]),
    })),
    chartsFixMock: vi.fn(async () => ({
      data: { code: "", written_back: false, content_updated: false, new_content: "" },
    })),
  }));

vi.mock("../components/mermaidRuntime", () => ({
  ensureMermaid: ensureMermaidMock,
  nextMermaidId: nextMermaidIdMock,
}));

vi.mock("../api", () => ({
  chartsApi: { render: chartsRenderMock, fixMermaid: chartsFixMock },
}));

beforeEach(() => {
  vi.clearAllMocks();
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-png");
  }
});

afterEach(() => cleanup());

/** 渲染含单个 chart-json 块的内容，返回后端实际收到的 chart_type。 */
async function sentChartType(payload: string): Promise<string> {
  const content = "前言段落。\n\n```chart-json\n" + payload + "\n```\n\n后文。";
  render(<MarkdownRenderer content={content} />);
  await waitFor(() => expect(chartsRenderMock).toHaveBeenCalled(), { timeout: 3000 });
  return String(chartsRenderMock.mock.calls[0][0].chart_type);
}

describe("chart-json 预览的 chart_type 契约", () => {
  it("显式合法 type 原样透传", async () => {
    expect(await sentChartType('{"type": "gantt", "tasks": [{"id": "a"}]}')).toBe("gantt");
  });

  it("省略 type 时传空串，不再凭空捏造 labor（回归核心）", async () => {
    const t = await sentChartType('{"tasks": [{"id": "a", "name": "施工"}]}');
    expect(t).toBe("");
    expect(t).not.toBe("labor");
  });

  it("架构图载荷省略 type 时同样传空串", async () => {
    const t = await sentChartType('{"root": {"name": "项目部", "children": []}}');
    expect(t).toBe("");
    expect(t).not.toBe("labor");
  });

  it("非法 type 视为未声明（传空串交后端推断）", async () => {
    expect(await sentChartType('{"type": "不是合法类型", "tasks": [{"id": "a"}]}')).toBe("");
  });

  it("type 大小写/空白容错后仍能识别", async () => {
    expect(await sentChartType('{"type": " GANTT ", "tasks": [{"id": "a"}]}')).toBe("gantt");
  });

  it("数组载荷（非法结构）不报错，传空串", async () => {
    expect(await sentChartType("[1, 2, 3]")).toBe("");
  });
});

describe("RENDERABLE_CHART_TYPES 白名单", () => {
  it("恰好覆盖 7 类结构化可渲染图表", () => {
    expect([...RENDERABLE_CHART_TYPES].sort()).toEqual(
      ["architecture", "comparison", "flowchart", "gantt", "labor", "layout", "timeline"],
    );
  });

  it("不含 Mermaid 语法专属类型（不走 PIL 轨）", () => {
    expect(RENDERABLE_CHART_TYPES.has("xychart")).toBe(false);
    expect(RENDERABLE_CHART_TYPES.has("pie")).toBe(false);
  });
});
