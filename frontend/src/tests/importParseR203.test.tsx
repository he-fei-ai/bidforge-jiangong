// @vitest-environment jsdom
/**
 * R203 · 上传解析/要点提取模块前端收口护栏（2026-11-09）。
 *
 * 覆盖面（对应后端轮次 docs/解析提取模块遗留收口_R203_20261109.md）：
 * - FE-1【P1】uploadInFlightRef 三段预检早退永久卡死（置位迁到全部早退之后）
 * - FE-2 failed 文档重试链（already_parsed 分支 note/parse_status 消费 + 行内 force）
 * - FE-3 上传响应 note/stale_facts 消费（R191 F1 后半程）
 * - FE-4 批量解析 catch 补偿刷新（与单份口径对齐）
 * - FE-5 FactsTab disabled 补 parsingDocId 维度（与 UploadParseTab busy 口径对齐）
 * - FE-6 分类 Select 上传中禁用
 * - FE-8【遗留承接】解析取消入口 UI 接线（停止解析按钮 + 透传链）
 * - FE-9【遗留承接】解析中轻量轮询列表（G6-SSE 最小承接）
 * - 类型契约补键 / 幽灵键清理
 *
 * 形态：组件级交互（UploadParseTab 停止按钮行为实证）+ ?raw 源码锚（R191 先例）。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import UploadParseTab from "../components/UploadParseTab";
import useDocumentWorkflowSrc from "../pages/schemeWorkbench/useDocumentWorkflow.tsx?raw";
import factsTabSrc from "../pages/schemeWorkbench/tabs/FactsTab.tsx?raw";
import importTabSrc from "../pages/schemeWorkbench/tabs/ImportTab.tsx?raw";
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import docParseListSrc from "../components/DocumentParseList.tsx?raw";
import factsTypesSrc from "../types/facts.ts?raw";
import workflowDerivedSrc from "../utils/workflowDerived.ts?raw";
import uploadParseTabSrc from "../components/UploadParseTab.tsx?raw";
import type { DocumentParseItem } from "../components/DocumentParseList";

// antd 组件级 jsdom 桩（R201 教训：栅格 responsiveObserver 在挂载期直接
// 抛 TypeError，与 uploadParseTab.test.tsx 同款）。
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

// =========================================================================
// FE-1【P1】uploadInFlightRef 置位顺序（源码锚）
// =========================================================================

describe("FE-1 uploadInFlightRef 置位顺序", () => {
  it("置位在三段预检全部早退之后、abort/try 之前", () => {
    const src = useDocumentWorkflowSrc;
    // 三段预检的早退 return 必须出现在置位行之前
    const posAccepted = src.indexOf("if (partition.accepted.length === 0) return;");
    const posQuota = src.indexOf("if (quota.accepted.length === 0) return;");
    const posCount = src.indexOf("if (byCount.accepted.length === 0) return;");
    const posSet = src.indexOf("uploadInFlightRef.current = true;");
    expect(posAccepted).toBeGreaterThan(-1);
    expect(posQuota).toBeGreaterThan(-1);
    expect(posCount).toBeGreaterThan(-1);
    expect(posSet).toBeGreaterThan(-1);
    expect(posSet).toBeGreaterThan(posAccepted);
    expect(posSet).toBeGreaterThan(posQuota);
    expect(posSet).toBeGreaterThan(posCount);
    // 置位之后紧接取消通道与 try 块（预检期间不得持锁）
    const posAbort = src.indexOf("uploadAbortRef.current?.abort();");
    expect(posAbort).toBeGreaterThan(posSet);
  });

  it("R191 parse 侧既有置位顺序锁保持（对照锚）", () => {
    const src = useDocumentWorkflowSrc;
    const posEarlyReturn = src.indexOf('msg.info("所有文档均已解析");');
    const posSet = src.indexOf("parseInFlightRef.current = true;");
    expect(posEarlyReturn).toBeGreaterThan(-1);
    expect(posSet).toBeGreaterThan(posEarlyReturn);
  });
});

// =========================================================================
// FE-2 / FE-3 / FE-4 / FE-9 处理器行为（源码锚）
// =========================================================================

describe("FE-2/3/4/9 处理器行为源码锚", () => {
  it("FE-2：already_parsed 短路分支消费 parse_status 与 note（failed → warning）", () => {
    const src = useDocumentWorkflowSrc;
    expect(src).toContain('data.parse_status === "failed"');
    expect(src).toContain("上次解析失败");
  });

  it("FE-3：上传成功文案拼接 note（同名替换重提指引不再丢失）", () => {
    const src = useDocumentWorkflowSrc;
    expect(src).toContain("uploadNote");
    expect(src).toContain('typeof data.note === "string" && data.note');
    // ✅ 使用点锁（M11 教训：只锁定义时摘除使用点仍静态通过）——
    //    error/success 两个 toast 出口都必须消费 uploadNote。
    expect(src).toContain("msg.error(notice.text + uploadNote)");
    expect(src).toContain("msg.success(notice.text + uploadNote)");
  });

  it("FE-4：批量解析 catch 补偿刷新 loadDocuments（与单份口径对齐）", () => {
    const src = useDocumentWorkflowSrc;
    // 批量 catch 块：requestErrorText(e, "批量解析"…) 之后应紧跟 loadDocuments
    const posCatch = src.indexOf('requestErrorText(e, "批量解析", 600, "解析失败")');
    expect(posCatch).toBeGreaterThan(-1);
    const after = src.slice(posCatch, posCatch + 400);
    expect(after).toContain("loadDocuments()");
  });

  it("FE-9：解析中轻量轮询（3s 间隔 + parsingDocs 依赖 + cleanup）", () => {
    const src = useDocumentWorkflowSrc;
    expect(src).toContain("loadDocumentsRef");
    expect(src).toContain("setInterval");
    expect(src).toContain("3000");
    expect(src).toContain("clearInterval(t)");
  });

  it("FE-2：DocumentParseList 行内「解析」透传 isFailed 为 force", () => {
    expect(docParseListSrc).toContain("onParse(doc.id, doc.file_name, isFailed)");
  });
});

// =========================================================================
// FE-5 / FE-6 门禁口径（源码锚）
// =========================================================================

describe("FE-5/6 门禁口径对齐", () => {
  it("FactsTab 上传按钮 / Upload 组件 / 批量解析按钮均补 !!parsingDocId", () => {
    const hits = factsTabSrc.match(/!!parsingDocId/g);
    expect(hits && hits.length).toBeGreaterThanOrEqual(3);
  });

  it("FactsTab props 声明 parsingDocId 可选键", () => {
    expect(factsTabSrc).toContain("parsingDocId?: string | null");
  });

  it("页面向 FactsTab 传 parsingDocId", () => {
    expect(pageSrc).toContain("parsingDocId={parsingDocId}");
  });

  it("DocumentParseList 分类 Select 上传中禁用", () => {
    expect(docParseListSrc).toContain("disabled={uploadingFacts}");
  });
});

// =========================================================================
// FE-8 解析取消入口（组件级行为实证 + 透传链源码锚）
// =========================================================================

function mkDoc(over: Partial<DocumentParseItem>): DocumentParseItem {
  return {
    id: "d1",
    file_name: "方案.pdf",
    file_type: "pdf",
    file_size: 1024,
    text_len: 0,
    parse_status: "pending",
    doc_category: "其他",
    ...over,
  } as DocumentParseItem;
}

describe("FE-8 解析取消入口（组件级）", () => {
  const baseProps = {
    docs: [mkDoc({})],
    categoryOptions: ["其他"],
    generating: false,
    uploadingFacts: false,
    uploadedFiles: [],
    parsingDocId: null,
    onUploadFiles: vi.fn(),
    onParseAll: vi.fn(),
    onReparseAll: vi.fn(),
    onRefresh: vi.fn(),
    onParse: vi.fn(),
    onPreview: vi.fn(),
    onDelete: vi.fn(),
    onCategoryChange: vi.fn(),
    onNavigate: vi.fn(),
  };

  it("解析中且传入 onStopParse → 渲染「停止解析」按钮，点击触发回调", () => {
    const onStopParse = vi.fn();
    render(<UploadParseTab {...baseProps} parsingDocs onStopParse={onStopParse} />);
    const btn = screen.getByRole("button", { name: "停止解析" });
    fireEvent.click(btn);
    expect(onStopParse).toHaveBeenCalledTimes(1);
  });

  it("未传 onStopParse → 不渲染按钮（向后兼容，既有调用方/测试零影响）", () => {
    render(<UploadParseTab {...baseProps} parsingDocs />);
    expect(screen.queryByRole("button", { name: "停止解析" })).toBeNull();
  });

  it("透传链：页面接线 parseAbortRef.abort → ImportTab 透传 → UploadParseTab 消费", () => {
    expect(pageSrc).toContain("onStopParse={() => {");
    expect(pageSrc).toContain("parseAbortRef.current?.abort();");
    expect(importTabSrc).toContain("onStopParse={onStopParse}");
    expect(uploadParseTabSrc).toContain("onStopParse");
  });
});

// =========================================================================
// 类型契约（FE-7）
// =========================================================================

describe("FE-7 类型契约", () => {
  it("FactParseDocResponse 补 stale_facts 键", () => {
    expect(factsTypesSrc).toContain("stale_facts?: number");
  });

  it("FactDocsListResponse 幽灵键已清理（后端从不回传 truncated_count/all_parsed）", () => {
    // 接口体内不含两幽灵键的**类型声明**（注释提及不算）
    expect(factsTypesSrc).toContain("幽灵键");
    const m = factsTypesSrc.match(/export interface FactDocsListResponse \{[\s\S]*?\n\}/);
    expect(m).toBeTruthy();
    const body = m![0];
    expect(body).not.toContain("truncated_count?:");
    expect(body).not.toContain("all_parsed?:");
  });
});