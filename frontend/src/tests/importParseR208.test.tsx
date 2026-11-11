// @vitest-environment jsdom
/**
 * 解析提取模块 · R208 组件级 / Tab 级交互测试（2026-11-10）。
 *
 * 本轮（R208，第三轮同模块探索）修复的前端缺陷与对应护栏：
 *   A. FactsTab 行内文档列表（此前**零 DOM 覆盖** —— 行内按钮的门禁口径与
 *      force 透传从未被任何用例断言，两项缺陷因此长期存活）
 *      1. FE-1：行内「解析 / 重新解析」按钮门禁必须是**三维**
 *         （parsingDocs || !!parsingDocId || uploadingFacts），
 *         与 DocumentParseList.parseDisabled 逐字对齐；
 *      2. FE-2：failed 文档行内「重试解析」必须**恒透传 force=true**
 *         （否则后端「正文非空且非 force」短路 → 死路）；
 *      3. 删除按钮在 uploadingFacts 时禁用（R191 C-F3 在另一侧已落地）。
 *   B. DocumentParseList 门禁的**行为级**断言（R191 只留了源码锚）
 *      4. uploadingFacts=true 时解析/重解析/删除按钮均 disabled；
 *      5. parsingDocId 命中时该行按钮 disabled。
 *   C. UploadParseTab
 *      6. FE-3：单份解析（parsingDocId）进行中时也必须出现「停止解析」按钮；
 *      7. 批量解析进行中时「停止解析」按钮出现（既有行为护栏）。
 *   D. parseSourceNotice
 *      8. FE-5：预览响应 parse_status='failed' 必须产出 warning 级提示
 *         （R203 BE-12 后端已回传，前端此前零消费 → 警示链不可达）；
 *      9. parse_status='success' 不产出失败提示（零误报）。
 *
 * ⚠️ 纪律（承自 AGENTS.md）：静态源码锁**必须覆盖使用点**（M11 教训第 4 次实证
 * —— 只锁定义、摘除使用点仍静态通过）。本文件以**真实 DOM 交互**为主，
 * 源码锁仅作补充。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup, screen } from "@testing-library/react";
import React from "react";
import { FactsTab } from "../pages/schemeWorkbench/tabs/FactsTab";
import type { FactsTabProps } from "../pages/schemeWorkbench/tabs/FactsTab";
import DocumentParseList from "../components/DocumentParseList";
import type { DocumentParseItem } from "../components/DocumentParseList";
import UploadParseTab from "../components/UploadParseTab";
import { previewNotices } from "../utils/parseSourceNotice";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

if (!(window as unknown as { matchMedia?: unknown }).matchMedia) {
  (window as unknown as { matchMedia: unknown }).matchMedia = (query: string) => ({
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
if (!(globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ===========================================================================
// A. FactsTab · 行内文档列表（Tab 级 DOM）
// ===========================================================================

function makeFactsTabProps(over: Partial<FactsTabProps> = {}): FactsTabProps {
  const noop = () => undefined;
  return {
    id: "s1",
    facts: [],
    factsSummary: null,
    factsCrossConflicts: [],
    factsSegmentStats: null,
    factsFilter: "all",
    setFactsFilter: vi.fn(),
    factChapterTitles: {},
    factAttrTitles: {},
    factSourceKindTitles: {},
    factCategoryEntries: [],
    activeFactCategory: null,
    activeFactGroups: [],
    factsPagination: null,
    docList: [],
    docStats: {
      actionableCount: 0,
      pendingCount: 0,
      failedCount: 0,
      parsedCount: 0,
      truncatedCount: 0,
      allParsed: true,
    },
    uploadedFiles: [],
    factsDocsCollapsed: false,
    setFactsDocsCollapsed: vi.fn(),
    generating: false,
    genType: "",
    progress: 0,
    progressMsg: "",
    missingValueMode: "fabricate",
    setMissingValueMode: vi.fn(),
    factsLogs: [],
    uploadingFacts: false,
    parsingDocs: false,
    taskPaused: false,
    taskStopping: false,
    factsChapterReport: null,
    factsDangerReport: null,
    factsDiagnosticsLoading: false,
    setActiveTab: vi.fn(),
    // ✅ 与 FactsTab 的 msg prop 类型逐字对齐（只有 success/error）——
    //    早先多传 warning/info 触发 TS2353 ⇒ tsc 退出码非 0 ⇒
    //    连带仓库内「守 tsc 退出码」的护栏（test_review_autofix_closeout_r55）
    //    当场变红。⚠️ FE-8 生产代码用的 msg.info 属于 DocumentWorkflowDeps.msg
    //    （确有 success/error/warning/info），与本组件的 msg 是两处。
    msg: { success: vi.fn(), error: vi.fn() },
    modal: { confirm: vi.fn() },
    handleAckStaleAll: noop,
    handleBatchResolve: noop,
    handleUploadDocuments: noop,
    handleParseDocuments: noop,
    handleParseDocument: noop,
    handleDeleteDocument: noop,
    handleGenerateFacts: noop,
    handleRetryFailedSegments: noop,
    openFactCreate: noop,
    handleClearAllFacts: noop,
    openFactEdit: noop,
    handleDeleteFactGroup: noop,
    openFactItemEdit: noop,
    handleResolveFactItem: noop,
    handleResolveConflictItem: noop,
    handleAckStaleOne: noop,
    handleControl: noop,
    loadFactsDiagnostics: noop,
    loadFacts: noop,
    loadDocuments: noop,
    ...over,
  };
}

/**
 * 按**精确可见文本**查找按钮。
 *
 * ⚠️ 本轮教训：原先用 `includes` 匹配时，「解析」命中的是 Tab 顶部的
 * 「② 解析文档」按钮（其 textContent 含「解析」）而非行内按钮 —— Tab 级与
 * 行内同名按钮并存时，模糊匹配必错（当场抓红：点错按钮 + 断言错 disabled 态）。
 */
function findButtonByText(text: string): HTMLElement | null {
  const nodes = Array.from(document.querySelectorAll("button"));
  return nodes.find((b) => (b.textContent || "").trim() === text) ?? null;
}

describe("FactsTab · 行内文档列表（R208 补齐零 DOM 覆盖）", () => {
  it("A1. 未解析文档渲染行内「解析」按钮，点击回调 handleParseDocument(docId, fileName, false)", () => {
    const handleParseDocument = vi.fn();
    render(
      <FactsTab
        {...makeFactsTabProps({
          handleParseDocument,
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("解析");
    expect(btn, "行内必须渲染「解析」按钮").toBeTruthy();
    fireEvent.click(btn as Element);
    expect(handleParseDocument).toHaveBeenCalledWith("d1", "招标公告.pdf", false);
  });

  it("A2. 【FE-2】failed 文档行内「重试解析」必须透传 force=true（否则后端短路 → 死路）", () => {
    const handleParseDocument = vi.fn();
    render(
      <FactsTab
        {...makeFactsTabProps({
          handleParseDocument,
          docList: [
            {
              id: "d2",
              file_name: "扫描件.pdf",
              text_len: 1200,
              parse_status: "failed",
            } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("重试解析");
    expect(btn, "failed 文档行内必须渲染「重试解析」按钮").toBeTruthy();
    fireEvent.click(btn as Element);
    // 第三参必须是 true：后端「正文非空且非 force」会短路返回 already_parsed
    expect(handleParseDocument).toHaveBeenCalledWith("d2", "扫描件.pdf", true);
  });

  it("A3. 【FE-1】uploadingFacts=true 时行内「解析」按钮禁用（防同名 upsert 换主键致结果丢失）", () => {
    render(
      <FactsTab
        {...makeFactsTabProps({
          uploadingFacts: true,
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("解析");
    expect(btn).toBeTruthy();
    expect((btn as HTMLButtonElement).disabled, "上传中行内解析按钮必须禁用").toBe(true);
  });

  it("A4. 【FE-1】parsingDocId 非空时行内「解析」按钮禁用（防重入静默吞请求 → 假动作）", () => {
    render(
      <FactsTab
        {...makeFactsTabProps({
          parsingDocId: "d9",
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("解析");
    expect(btn).toBeTruthy();
    expect((btn as HTMLButtonElement).disabled, "单份解析中行内解析按钮必须禁用").toBe(true);
  });

  it("A5. parsingDocs=true 时行内「解析」按钮禁用（既有行为护栏）", () => {
    render(
      <FactsTab
        {...makeFactsTabProps({
          parsingDocs: true,
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("解析");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it("A6. uploadingFacts=true 时行内「删除」按钮禁用（R191 C-F3 在本副本的同步）", () => {
    render(
      <FactsTab
        {...makeFactsTabProps({
          uploadingFacts: true,
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("删除");
    expect(btn, "行内必须渲染「删除」按钮").toBeTruthy();
    expect((btn as HTMLButtonElement).disabled, "上传中行内删除按钮必须禁用").toBe(true);
  });

  it("A7. 已解析且截断的文档渲染「重新解析」按钮，点击恒透传 force=true", () => {
    const handleParseDocument = vi.fn();
    render(
      <FactsTab
        {...makeFactsTabProps({
          handleParseDocument,
          docList: [
            {
              id: "d3",
              file_name: "大表.xlsx",
              text_len: 900,
              parse_status: "success",
              truncated: true,
            } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("重新解析");
    expect(btn, "已解析且截断必须渲染「重新解析」按钮").toBeTruthy();
    fireEvent.click(btn as Element);
    expect(handleParseDocument).toHaveBeenCalledWith("d3", "大表.xlsx", true);
  });

  it("A8. 正常态下行内按钮均可用（零过度禁用护栏）", () => {
    render(
      <FactsTab
        {...makeFactsTabProps({
          docList: [
            { id: "d1", file_name: "招标公告.pdf", text_len: 0, parse_status: "pending" } as never,
          ],
        })}
      />,
    );
    const btn = findButtonByText("解析");
    expect((btn as HTMLButtonElement).disabled).toBe(false);
  });
});

// ===========================================================================
// B. DocumentParseList · 门禁行为级断言
// ===========================================================================

function makeDoc(over: Partial<DocumentParseItem>): DocumentParseItem {
  return { id: "d1", file_name: "招标公告.pdf", ...over } as DocumentParseItem;
}

function setupList(over: Partial<React.ComponentProps<typeof DocumentParseList>> = {}) {
  const cbs = {
    onParse: vi.fn(),
    onPreview: vi.fn(),
    onDelete: vi.fn(),
    onCategoryChange: vi.fn(),
  };
  const utils = render(
    <DocumentParseList
      docs={[makeDoc({})]}
      parsingDocs={false}
      parsingDocId={null}
      categoryOptions={["招标文件", "其他"]}
      {...cbs}
      {...over}
    />,
  );
  return { ...utils, ...cbs };
}

describe("DocumentParseList · 门禁行为级断言（R208 补齐源码锚之外的 DOM 覆盖）", () => {
  it("B1. uploadingFacts=true 时「解析」与「删除」按钮均禁用", () => {
    setupList({ uploadingFacts: true });
    const parseBtn = findButtonByText("解析");
    const delBtn = findButtonByText("删除");
    expect(parseBtn).toBeTruthy();
    expect(delBtn).toBeTruthy();
    expect((parseBtn as HTMLButtonElement).disabled).toBe(true);
    expect((delBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("B2. parsingDocId 命中时该行「解析」按钮禁用", () => {
    setupList({ parsingDocId: "d1" });
    const parseBtn = findButtonByText("解析");
    expect((parseBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("B3. parsingDocs=true 时「解析」按钮禁用（既有行为护栏）", () => {
    setupList({ parsingDocs: true });
    const parseBtn = findButtonByText("解析");
    expect((parseBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("B4. 正常态下「解析」按钮可用且点击回调带 force=false", () => {
    const { onParse } = setupList({});
    const parseBtn = findButtonByText("解析");
    expect((parseBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(parseBtn as Element);
    expect(onParse).toHaveBeenCalledWith("d1", "招标公告.pdf", false);
  });
});

// ===========================================================================
// C. UploadParseTab · 停止解析按钮
// ===========================================================================

function makeUploadProps(over: Partial<React.ComponentProps<typeof UploadParseTab>> = {}) {
  const noop = () => undefined;
  return {
    docs: [],
    categoryOptions: ["招标文件", "其他"],
    generating: false,
    uploadingFacts: false,
    uploadedFiles: [],
    parsingDocs: false,
    parsingDocId: null,
    onUploadFiles: noop,
    onParseAll: noop,
    onReparseAll: noop,
    onRefresh: noop,
    onParse: noop,
    onPreview: noop,
    onDelete: noop,
    onCategoryChange: noop,
    onNavigate: noop,
    ...over,
  } as React.ComponentProps<typeof UploadParseTab>;
}

describe("UploadParseTab · 停止解析入口（R208）", () => {
  it("C1. 【FE-3】单份解析（parsingDocId）进行中也必须渲染「停止解析」按钮", () => {
    const onStopParse = vi.fn();
    render(<UploadParseTab {...makeUploadProps({ parsingDocId: "d1", onStopParse })} />);
    const btn = findButtonByText("停止解析");
    expect(
      btn,
      "单份解析（OCR 分钟级）同样需要取消入口 —— 此前按钮只在 parsingDocs 时渲染",
    ).toBeTruthy();
    fireEvent.click(btn as Element);
    expect(onStopParse).toHaveBeenCalled();
  });

  it("C2. 批量解析进行中渲染「停止解析」按钮（既有行为护栏）", () => {
    const onStopParse = vi.fn();
    render(<UploadParseTab {...makeUploadProps({ parsingDocs: true, onStopParse })} />);
    const btn = findButtonByText("停止解析");
    expect(btn).toBeTruthy();
  });

  it("C3. 未传 onStopParse 时不渲染该按钮（向后兼容护栏）", () => {
    render(<UploadParseTab {...makeUploadProps({ parsingDocs: true })} />);
    expect(findButtonByText("停止解析")).toBeNull();
  });

  it("C4. 无解析进行时不渲染停止按钮", () => {
    const onStopParse = vi.fn();
    render(<UploadParseTab {...makeUploadProps({ onStopParse })} />);
    expect(findButtonByText("停止解析")).toBeNull();
  });
});

// ===========================================================================
// D. parseSourceNotice · 预览失败警示
// ===========================================================================

describe("parseSourceNotice · 预览失败警示（R208）", () => {
  it("D1. 【FE-5】parse_status='failed' 必须产出 warning 级提示", () => {
    const notices = previewNotices({
      is_parsed: true,
      text_len: 1000,
      parse_status: "failed",
    });
    const failed = notices.find((n) => n.kind === "warning" && n.title.includes("上次解析失败"));
    expect(failed, "失败文档的预览必须给出 warning 级警示").toBeTruthy();
    expect(failed?.detail).toContain("强制重新解析");
  });

  it("D2. parse_status='success' 不产出失败提示（零误报护栏）", () => {
    const notices = previewNotices({
      is_parsed: true,
      text_len: 1000,
      parse_status: "success",
    });
    expect(notices.some((n) => n.title.includes("上次解析失败"))).toBe(false);
  });

  it("D3. 未解析文档（is_parsed=false）不产出失败提示", () => {
    const notices = previewNotices({ is_parsed: false, parse_status: "pending" });
    expect(notices.some((n) => n.title.includes("上次解析失败"))).toBe(false);
  });

  it("D4. 解析级截断提示仍然生效（既有行为护栏，零回退）", () => {
    const notices = previewNotices({ is_parsed: true, text_len: 100, truncated: true });
    expect(notices.some((n) => n.title.includes("内容不完整"))).toBe(true);
  });
});
