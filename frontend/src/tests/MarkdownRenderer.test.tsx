// @vitest-environment jsdom
/**
 * MarkdownRenderer 组件级测试（P1-6 优化行为验证）：
 * 1. 无图表代码块时跳过 mermaid 阶段（不加载渲染引擎）
 * 2. mermaid 渲染 + SVG 按代码复用（同图不重复 render）
 * 3. chart-json 数据块走后端渲染引擎（chartsApi.render）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import MarkdownRenderer from "../components/MarkdownRenderer";

const { renderMock, ensureMermaidMock, nextMermaidIdMock, chartsRenderMock, chartsFixMock } =
  vi.hoisted(() => ({
    renderMock: vi.fn(async (_id: string, code: string) => ({
      svg: `<svg id="svg-${_id}"><text>${code.slice(0, 20)}</text></svg>`,
    })),
    ensureMermaidMock: vi.fn(async () => ({ render: renderMock })),
    nextMermaidIdMock: vi.fn(() => "mmd-test-id"),
    chartsRenderMock: vi.fn(async () => ({ data: new Uint8Array([137, 80, 78, 71]) })),
    // 返回结构与后端 fix-mermaid 响应对齐（code / written_back / content_updated / new_content）
    chartsFixMock: vi.fn(async (_p?: Record<string, unknown>) => ({
      data: { code: "", written_back: false, content_updated: false, new_content: "" },
    })),
  }));

vi.mock("../components/mermaidRuntime", () => ({
  ensureMermaid: ensureMermaidMock,
  nextMermaidId: nextMermaidIdMock,
}));

vi.mock("../api", () => ({
  chartsApi: {
    render: chartsRenderMock,
    fixMermaid: chartsFixMock,
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-png");
  }
});

// ✅ 修复（2026-09-22）：组件用 window.setTimeout 做 50ms 防抖，若实例未在
// 用例结束后卸载，定时器会在 jsdom 环境销毁后触发 setState → 抛
// `window is not defined`。显式卸载确保 clearTimeout 生效，消除残留定时器。
afterEach(() => {
  cleanup();
});

describe("MarkdownRenderer P1-6 优化行为", () => {
  it("列表样式显式保留项目符号，防止被全局 reset 拍平", async () => {
        const { container } = render(<MarkdownRenderer content={"- 第一项\n1. 第二项"} />);
    await waitFor(() => expect(container.querySelector("ul")).not.toBeNull());
    const style = Array.from(document.querySelectorAll("style"))
      .map((el) => el.textContent || "").join("\n");
    expect(style).toContain("list-style-type: disc");
    expect(style).toContain("list-style-type: decimal");
  });

  it("纯文本/无图表内容：跳过 mermaid 阶段，不加载渲染引擎", async () => {
    const { container } = render(<MarkdownRenderer content={"## 标题\n\n正文段落"} />);
    // 解析结果同步写入 DOM（useMemo → setHtml）
    await waitFor(() => {
      expect(container.querySelector(".markdown-body h2")).not.toBeNull();
    });
    expect(container.querySelector("h2")?.textContent).toBe("标题");
    expect(ensureMermaidMock).not.toHaveBeenCalled();
    expect(renderMock).not.toHaveBeenCalled();
  });

  it("mermaid 代码块：渲染 SVG 并替换占位 div", async () => {
    const code = "graph TD\n  A-->B";
    const { container } = render(
      <MarkdownRenderer content={`# 流程图\n\n\`\`\`mermaid\n${code}\n\`\`\``} />
    );
    await waitFor(() => {
      expect(container.querySelector(".mmd-rendered")).not.toBeNull();
    });
    expect(ensureMermaidMock).toHaveBeenCalledTimes(1);
    expect(renderMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".mmd-rendered")?.innerHTML).toContain("<svg");
    expect(container.querySelector(".mmd-ph")).toBeNull();
  });

  it("相同 mermaid 代码复用 SVG：第二次渲染命中缓存，不再调 mermaid.render", async () => {
    const code = "flowchart LR\n  X --> Y";
    const content = `\`\`\`mermaid\n${code}\n\`\`\``;
    const first = render(<MarkdownRenderer content={content} />);
    await waitFor(() => {
      expect(first.container.querySelector(".mmd-rendered")).not.toBeNull();
    });
    expect(renderMock).toHaveBeenCalledTimes(1);

    // 第二次渲染相同代码（新组件实例）：命中模块级 _svgCache，不再调 render
    const second = render(<MarkdownRenderer content={content} />);
    await waitFor(() => {
      expect(second.container.querySelector(".mmd-rendered")).not.toBeNull();
    });
    expect(renderMock).toHaveBeenCalledTimes(1);
    expect(ensureMermaidMock).toHaveBeenCalledTimes(1);
  });

  it("chart-json 数据块：走后端渲染引擎出图（不经过 mermaid）", async () => {
    const { container } = render(
      <MarkdownRenderer
        content={'```chart-json\n{"type":"labor","title":"劳动力结构"}\n```'}
      />
    );
    await waitFor(() => {
      expect(chartsRenderMock).toHaveBeenCalledTimes(1);
    });
    expect(chartsRenderMock).toHaveBeenCalledWith(
      expect.objectContaining({ chart_type: "labor", skip_http: true })
    );
    await waitFor(() => {
      expect(container.querySelector("img[src^='blob:']")).not.toBeNull();
    });
    expect(renderMock).not.toHaveBeenCalled();
  });

  it("渲染失败 → AI 修复：请求携带 section_id（不硬编码 chart_type），content_updated 时回调 onContentReplaced", async () => {
    const badCode = "flowchart TD\n  A --> ??"; // 独特代码避免命中 SVG 缓存
    const fixedCode = "flowchart TD\n  A --> B";
    // 第一次 mermaid.render（坏代码）失败，第二次（修复结果）走默认成功实现
    renderMock.mockRejectedValueOnce(new Error("Parse error on line 2"));
    chartsFixMock.mockResolvedValueOnce({
      data: { code: fixedCode, written_back: true, content_updated: true, new_content: "重写后的正文" },
    });
    const onContentReplaced = vi.fn();
    const { container } = render(
      <MarkdownRenderer
        content={`\`\`\`mermaid\n${badCode}\n\`\`\``}
        sectionId="sec-abc"
        onContentReplaced={onContentReplaced}
      />
    );
    // 渲染失败 → 出现错误块与修复按钮
    const fixBtn = await waitFor(() => {
      const el = container.querySelector<HTMLButtonElement>("button[data-fix-code]");
      expect(el).not.toBeNull();
      return el!;
    });
    fireEvent.click(fixBtn);
    // ✅ 请求带 section_id，且不再硬编码 chart_type（类型由后端按代码推断）
    await waitFor(() => {
      expect(chartsFixMock).toHaveBeenCalledWith({
        code: badCode,
        error: "Parse error on line 2",
        section_id: "sec-abc",
      });
    });
    // ✅ 后端已重写正文 → 通知父组件同步预览状态
    await waitFor(() => {
      expect(onContentReplaced).toHaveBeenCalledWith("重写后的正文");
    });
  });

  it("未传 sectionId 时修复请求不携带 section_id，也不触发 onContentReplaced", async () => {
    const badCode = "sequenceDiagram\n  A->>?B: hi";
    renderMock.mockRejectedValueOnce(new Error("Parse error"));
    chartsFixMock.mockResolvedValueOnce({
      data: { code: "sequenceDiagram\n  A->>B: hi", written_back: false, content_updated: false, new_content: "" },
    });
    const onContentReplaced = vi.fn();
    const { container } = render(
      <MarkdownRenderer content={`\`\`\`mermaid\n${badCode}\n\`\`\``} onContentReplaced={onContentReplaced} />
    );
    const fixBtn = await waitFor(() => {
      const el = container.querySelector<HTMLButtonElement>("button[data-fix-code]");
      expect(el).not.toBeNull();
      return el!;
    });
    fireEvent.click(fixBtn);
    await waitFor(() => {
      expect(chartsFixMock).toHaveBeenCalledWith({ code: badCode, error: "Parse error" });
    });
    expect(onContentReplaced).not.toHaveBeenCalled();
  });
});
