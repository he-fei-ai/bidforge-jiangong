// @vitest-environment jsdom
/**
 * R225（2026-11-12）全局事实模块深度探索增强 · 前端护栏
 *
 * A 组 源码棘轮（?raw）：D2 文档写路径后事实回读（upload/parse-all×2/
 *      单份 parse/delete 五处收尾 loadFacts）；D6 ackStaleOne 缺 id 反馈
 *      （R210 假动作家族对齐）。
 * B 组 组件级 DOM：D4 FactsAdjustPanel busy 门禁补 !!parsingDocId ——
 *      单份解析在飞时「AI 调整事实」必须禁用（与 DocumentParseList 权威
 *      口径成对，副本门禁分裂家族第 4 处漏改）。
 * C 组 自证：锚点真存在（防 vacuous pass）。
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";

import { FactsAdjustPanel } from "../pages/schemeWorkbench/FactsAdjustPanel";

import docFlowSrc from "../pages/schemeWorkbench/useDocumentWorkflow.tsx?raw";
import factsTabSrc from "../pages/schemeWorkbench/tabs/FactsTab.tsx?raw";
import docListSrc from "../components/DocumentParseList.tsx?raw";

afterEach(cleanup);

// ===========================================================================
// A 组 · 源码棘轮（?raw）
// ===========================================================================
describe("D2 文档写路径后事实回读（R225）", () => {
  it("upload 收尾在 loadDocuments() 之后回读 loadFacts()", () => {
    const i = docFlowSrc.indexOf("msg.success(notice.text + uploadNote)");
    expect(i).toBeGreaterThan(0);
    const tail = docFlowSrc.slice(i, i + 400);
    expect(tail).toContain("loadDocuments();");
    expect(tail).toContain("void loadFacts();");
    // 回读必须在 loadDocuments 之后（顺序契约）
    expect(tail.indexOf("void loadFacts();")).toBeGreaterThan(tail.indexOf("loadDocuments();"));
  });

  it("parse-all 正常与失败补偿两处收尾都回读 loadFacts()", () => {
    const ok = docFlowSrc.indexOf("if (isWarning) msg.warning(detail);");
    expect(ok).toBeGreaterThan(0);
    expect(docFlowSrc.slice(ok, ok + 300)).toContain("void loadFacts();");

    const comp = docFlowSrc.indexOf('requestErrorText(e, "批量解析", 600, "解析失败")');
    expect(comp).toBeGreaterThan(0);
    expect(docFlowSrc.slice(comp, comp + 400)).toContain("void loadFacts();");
  });

  it("单份 parse 收尾回读 loadFacts()", () => {
    const i = docFlowSrc.indexOf('「${fileName}」${force ? "重新" : ""}解析完成');
    expect(i).toBeGreaterThan(0);
    expect(docFlowSrc.slice(i, i + 300)).toContain("void loadFacts();");
  });

  it("delete 文档收尾回读 loadFacts()", () => {
    const i = docFlowSrc.indexOf('msg.success("文件已删除")');
    expect(i).toBeGreaterThan(0);
    expect(docFlowSrc.slice(i, i + 200)).toContain("void loadFacts();");
  });

  it("回读总计数：新增 5 处 void loadFacts() + 既有 3 处 await loadFacts()", () => {
    // 既有三处（批量确认/批量解除/单条解除）是 await 形态；本轮新增五处收尾
    // 用 void 形态（fire-and-forget 回读，不阻塞 toast 流程）。
    expect((docFlowSrc.match(/void loadFacts\(\);/g) || []).length).toBe(5);
    expect((docFlowSrc.match(/await loadFacts\(\);/g) || []).length).toBe(3);
  });
});

describe("D6 ackStaleOne 缺 id 用户反馈（R225）", () => {
  it("缺 fact_id 不再静默 return（R210 假动作家族对齐）", () => {
    const i = docFlowSrc.indexOf("const handleAckStaleOne");
    expect(i).toBeGreaterThan(0);
    const head = docFlowSrc.slice(i, i + 400);
    expect(head).toContain('msg.info("该条目缺少事实标识');
    // 静默形态不得回流：if (!id || !factId) return; 裸返回
    expect(head).not.toMatch(/if \(!id \|\| !factId\) return;/);
  });
});

// ===========================================================================
// B 组 · 组件级 DOM（D4）
// ===========================================================================
describe("D4 FactsAdjustPanel busy 门禁（R225）", () => {
  function renderPanel(props: { busy?: boolean; disabled?: boolean }) {
    return render(
      <FactsAdjustPanel
        schemeId="scheme-1"
        disabled={props.disabled}
        busy={props.busy}
        preview={vi.fn().mockResolvedValue({})}
        apply={vi.fn().mockResolvedValue(undefined)}
        confirmDialog={(opts) => {
          opts.onOk?.();
          return { update: vi.fn(), destroy: vi.fn() } as never;
        }}
      />,
    );
  }

  it("busy=true（如单份解析在飞）时「AI 调整事实」禁用", () => {
    const { container } = renderPanel({ busy: true });
    const btn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("AI 调整事实"),
    );
    expect(btn).toBeTruthy();
    expect(btn!.disabled).toBe(true);
  });

  it("busy=false 时按钮保持可用（不误伤既有可用态）", () => {
    const { container } = renderPanel({ busy: false, disabled: false });
    const btn = Array.from(container.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("AI 调整事实"),
    );
    expect(btn).toBeTruthy();
    expect(btn!.disabled).toBe(false);
  });

  it("源码锁：busy 表达式含全部四个维度（与 DocumentParseList 权威口径成对）", () => {
    expect(factsTabSrc).toContain("busy={generating || uploadingFacts || parsingDocs || !!parsingDocId}");
    // 权威口径三维度在场（parseDisabled）
    expect(docListSrc).toContain("parsingDocs || !!parsingDocId || uploadingFacts");
  });
});

// ===========================================================================
// C 组 · 自证
// ===========================================================================
describe("自证（防 vacuous pass）", () => {
  it("锚点真实存在", () => {
    expect(docFlowSrc).toContain("handleAckStaleOne");
    expect(docFlowSrc).toContain("void loadFacts();");
    expect(factsTabSrc).toContain("FactsAdjustPanel");
    expect(docFlowSrc.length).toBeGreaterThan(10000);
  });
});
