// @vitest-environment jsdom
/**
 * R238 解析提取模块：ImportTab（Tab 级）组件交互测试。
 *
 * 补齐此前缺失的 Tab 级装配测试：ImportTab 此前只被 source 级护栏
 * （workflowTabsGuard / importParseR203 用 ?raw 检查文本）覆盖，无任何
 * render 级交互验证。本测试 mock 叶子组件（UploadParseTab / BidAnalysisTab），
 * 用真实 ImportSubTabBar 验证：
 *   - docs/extract 子 Tab 内容切换（setImportSubTab 驱动）；
 *   - ImportSubTabBar onChange 回调接线；
 *   - UploadParseTab 的 onSwitchToExtract → 切回 docs；
 *   - BidAnalysisTab 的 onGoImport → 切回 docs（baGoImport）；
 *   - 上传/解析/分类/预览等 handler 透传到 UploadParseTab。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import React from "react";

type MockProps = Record<string, unknown>;
const uploadParseProps: MockProps = {};
const bidAnalysisProps: MockProps = {};
let uploadParseRenderCount = 0;
let bidAnalysisRenderCount = 0;

vi.mock("../components/UploadParseTab", () => ({
  default: (props: Record<string, unknown>) => {
    Object.assign(uploadParseProps, props);
    uploadParseRenderCount += 1;
    return (
      <div data-testid="mock-upload-parse-tab">
        <button onClick={() => (props.onSwitchToExtract as () => void)()}>
          switch-to-extract
        </button>
      </div>
    );
  },
}));

vi.mock("../components/BidAnalysisTab", () => ({
  default: (props: Record<string, unknown>) => {
    Object.assign(bidAnalysisProps, props);
    bidAnalysisRenderCount += 1;
    return (
      <div data-testid="mock-bid-analysis-tab">
        <button onClick={() => (props.onGoImport as () => void)()}>go-import</button>
      </div>
    );
  },
}));

import { ImportTab, type ImportTabProps } from "../pages/schemeWorkbench/tabs/ImportTab";

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

function baseProps(over: Partial<ImportTabProps> = {}): ImportTabProps {
  return {
    importSubTab: "docs",
    setImportSubTab: vi.fn(),
    docList: [],
    docStats: {
      parsedCount: 0,
      pendingCount: 0,
      truncatedCount: 0,
      failedCount: 0,
      actionableCount: 0,
      allParsed: false,
    } as ImportTabProps["docStats"],
    maxUploadBytes: 30 * 1024 * 1024,
    categoryOptions: ["招标文件", "合同文件"],
    generating: false,
    uploadingFacts: false,
    uploadedFiles: [],
    parsingDocs: false,
    parsingDocId: null,
    handleUploadDocuments: vi.fn(),
    handleParseDocuments: vi.fn(),
    loadDocuments: vi.fn(),
    handleParseDocument: vi.fn(),
    handlePreviewDocument: vi.fn(),
    handleDeleteDocument: vi.fn(),
    handleCategoryChange: vi.fn(),
    setActiveTab: vi.fn(),
    baSummary: null,
    baSummaryDerived: null,
    baDefs: [],
    baGroups: [],
    baItems: [],
    selectedBaItem: null,
    setSelectedBaItem: vi.fn(),
    setBaFullItem: vi.fn(),
    openBaEdit: vi.fn(),
    baLoading: false,
    baError: null,
    baDefsError: null,
    sharedParseState: { expanded: {}, activeKey: null } as ImportTabProps["sharedParseState"],
    baRefreshResults: vi.fn(),
    baActive: false,
    id: "s1",
    scheme: null,
    baShownProgress: 0,
    baShownMsg: "",
    sectionChecking: false,
    sectionCheckResult: null,
    baTextStats: null,
    loadBaMeta: vi.fn(),
    baOpenConfig: vi.fn(),
    handleStopBa: vi.fn(),
    baStopping: false,
    handleCheckSections: vi.fn(),
    baRefreshAll: vi.fn(),
    handleRerunItem: vi.fn(),
    baDismissSectionCheck: vi.fn(),
    baGoImport: vi.fn(),
    baGoOutline: vi.fn(),
    baGoFacts: vi.fn(),
    ...over,
  };
}

beforeEach(() => {
  Object.keys(uploadParseProps).forEach((k) => delete uploadParseProps[k]);
  Object.keys(bidAnalysisProps).forEach((k) => delete bidAnalysisProps[k]);
  uploadParseRenderCount = 0;
  bidAnalysisRenderCount = 0;
});

describe("ImportTab · Tab 级装配", () => {
  it("docs 子 Tab 渲染 UploadParseTab；extract 子 Tab 渲染 BidAnalysisTab", () => {
    const { container, rerender } = render(<ImportTab {...baseProps()} />);
    expect(screen.getByTestId("mock-upload-parse-tab")).toBeTruthy();
    expect(container.querySelector(".ant-tabs-tab")).toBeTruthy();

    rerender(<ImportTab {...baseProps({ importSubTab: "extract" })} />);
    expect(screen.getByTestId("mock-bid-analysis-tab")).toBeTruthy();
    expect(screen.queryByTestId("mock-upload-parse-tab")).toBeNull();
  });

  it("点击子 Tab「项目提取」触发 setImportSubTab('extract')", () => {
    const setImportSubTab = vi.fn();
    const { container } = render(<ImportTab {...baseProps({ setImportSubTab })} />);
    const tabs = Array.from(container.querySelectorAll(".ant-tabs-tab"));
    fireEvent.click(tabs[1]);
    expect(setImportSubTab).toHaveBeenCalledWith("extract");
  });

  it("UploadParseTab 的 onSwitchToExtract 切到 extract 子 Tab", () => {
    const setImportSubTab = vi.fn();
    render(<ImportTab {...baseProps({ setImportSubTab })} />);
    fireEvent.click(screen.getByText("switch-to-extract"));
    expect(setImportSubTab).toHaveBeenCalledWith("extract");
  });

  it("BidAnalysisTab 的 onGoImport 回调 baGoImport", () => {
    const baGoImport = vi.fn();
    render(<ImportTab {...baseProps({ importSubTab: "extract", baGoImport })} />);
    fireEvent.click(screen.getByText("go-import"));
    expect(baGoImport).toHaveBeenCalledTimes(1);
  });

  it("上传/解析/分类/预览/删除 handler 透传到 UploadParseTab", () => {
    render(<ImportTab {...baseProps()} />);
    expect(uploadParseProps.onUploadFiles).toBeDefined();
    expect(uploadParseProps.onParse).toBeDefined();
    expect(uploadParseProps.onPreview).toBeDefined();
    expect(uploadParseProps.onDelete).toBeDefined();
    expect(uploadParseProps.onCategoryChange).toBeDefined();
    expect(uploadParseProps.onParseAll).toBeDefined();
    expect(uploadParseProps.onReparseAll).toBeDefined();
    expect(uploadParseProps.onRefresh).toBeDefined();
  });

  it("onParseAll 接线：包装 handleParseDocuments 无参调用", () => {
    const handleParseDocuments = vi.fn();
    render(<ImportTab {...baseProps({ handleParseDocuments })} />);
    const onParseAll = uploadParseProps.onParseAll as () => void;
    onParseAll();
    expect(handleParseDocuments).toHaveBeenCalledTimes(1);
    expect(handleParseDocuments).toHaveBeenCalledWith();
  });
});
