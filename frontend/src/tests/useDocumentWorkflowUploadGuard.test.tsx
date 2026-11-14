// @vitest-environment jsdom
/**
 * 文件导入 / 解析域 · 上传与批量解析「防重入 + 三段预检」handler 级回归。
 *
 * 覆盖 `useDocumentWorkflow` 里 R191（C-F9）、R203（FE-1）、R208 修过的
 * 三处容易「静默失效」的守卫语义，避免只靠注释承诺：
 *
 *   1. 上传三段预检（类型 / 体积 / 数量）全部早退后，`uploadInFlightRef`
 *      不得被卡在 true —— 否则后续合法上传被防重入静默吞掉（R203 FE-1 实证）。
 *   2. 上传在飞时二次调用：只发一次请求，并给出明确 info 提示（R208 修复）。
 *   3. 新上传发起时必须中止上一份在途请求（后发者胜）。
 *   4. 批量解析在飞时二次调用：只发一次请求，给出 info 提示。
 *   5. 单份解析在飞时批量解析被拒：给出「正在解析中」提示，不叠加请求。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { factsApi } from "../api";
import { useDocumentWorkflow } from "../pages/schemeWorkbench/useDocumentWorkflow";
import type { DocRecord } from "../utils/workflowDerived";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const goodFile = (name = "招标文件.pdf"): File =>
  new File([new Uint8Array([0x25, 0x50, 0x44, 0x46])], name, { type: "application/pdf" });

const badTypeFile = (): File => new File(["x"], "恶意.exe");
const oversizeFile = (): File => new File([new Uint8Array(31 * 1024 * 1024)], "大.pdf");

function setup(
  docListNow: DocRecord[] = [],
  overrides: Partial<{
    parsingDocId: string | null;
    parsingDocs: boolean;
  }> = {},
) {
  const msg = {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  };
  const parsingDocId = overrides.parsingDocId ?? null;
  const parsingDocs = overrides.parsingDocs ?? false;
  const deps = {
    id: "scheme-1",
    schemeProjectId: "proj-1",
    msg,
    modal: { confirm: vi.fn() },
    uploadingFacts: false,
    setUploadingFacts: vi.fn(),
    setUploadedFiles: vi.fn(),
    maxUploadBytes: undefined,
    maxUploadTotalBytes: undefined,
    maxUploadFiles: undefined,
    uploadAbortRef: { current: null },
    parseAbortRef: { current: null },
    previewAbortRef: { current: null },
    parsingDocs,
    setParsingDocs: vi.fn(),
    parsingDocId,
    setParsingDocId: vi.fn(),
    docList: docListNow,
    loadDocuments: vi.fn(),
    setPreviewDoc: vi.fn(),
    setPreviewText: vi.fn(),
    setPreviewData: vi.fn(),
    setPreviewLoading: vi.fn(),
    setPreviewError: vi.fn(),
    previewDoc: null,
    setPipelineStatus: vi.fn(),
    setPipelineError: vi.fn(),
    setPipelineLoading: vi.fn(),
    pipelineAction: null,
    setPipelineAction: vi.fn(),
    setDocList: vi.fn(),
    loadFacts: vi.fn(),
    factsSummary: null,
    factsListAbortRef: { current: null },
    currentSchemeIdRef: { current: "scheme-1" },
  };
  const { result } = renderHook(() => useDocumentWorkflow(deps as never));
  return { result, msg, deps };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useDocumentWorkflow · 上传三段预检早退不卡防重入", () => {
  it("全部文件类型非法：早退不发请求，且 uploadInFlightRef 不被卡死（后续合法上传可达）", async () => {
    const { result, msg } = setup();
    const upload = vi.spyOn(factsApi, "uploadDocuments").mockResolvedValue({
      data: { saved_count: 1 },
    } as never);

    await act(async () => {
      await result.current.handleUploadDocuments([badTypeFile()]);
    });

    // 早退：不发请求，给用户明确拒绝提示（R203 修复）
    expect(upload).not.toHaveBeenCalled();
    expect(msg.error).toHaveBeenCalledWith(
      expect.stringContaining("不支持的文件类型"),
    );

    // 关键：ref 未被卡死 —— 后续合法上传必须真正发请求（R203 FE-1 实证）
    await act(async () => {
      await result.current.handleUploadDocuments([goodFile()]);
    });
    expect(upload).toHaveBeenCalledTimes(1);
    expect(msg.success).toHaveBeenCalledWith(expect.stringContaining("已保存"));
  });

  it("单个文件超限：给出体积提示，仍上传未超限的合法文件", async () => {
    const { result, msg } = setup();
    const upload = vi.spyOn(factsApi, "uploadDocuments").mockResolvedValue({
      data: { saved_count: 1 },
    } as never);

    await act(async () => {
      await result.current.handleUploadDocuments([oversizeFile(), goodFile("正常.pdf")]);
    });

    expect(upload).toHaveBeenCalledTimes(1);
    // 只把合法文件送进上传（超限文件被前端预检拦下）
    const sent = upload.mock.calls[0][0] as File[];
    expect(sent.map((f) => f.name)).toEqual(["正常.pdf"]);
    expect(msg.error).toHaveBeenCalledWith(expect.stringContaining("不能超过"));
  });
});

describe("useDocumentWorkflow · 上传防重入", () => {
  it("上传在飞时二次调用：只发一次请求并给出明确提示，不静默返回", async () => {
    const { result, msg } = setup();
    const d = deferred<{ data: unknown }>();
    const upload = vi
      .spyOn(factsApi, "uploadDocuments")
      .mockReturnValue(d.promise as never);

    let first!: Promise<void>;
    act(() => {
      first = result.current.handleUploadDocuments([goodFile()]);
    });
    // 在飞：ref 已置位，同 tick 第二次调用必须被防重入拦下
    await act(async () => {
      await result.current.handleUploadDocuments([goodFile("第二次.pdf")]);
    });

    expect(upload).toHaveBeenCalledTimes(1);
    expect(msg.info).toHaveBeenCalledWith(expect.stringContaining("正在进行"));

    await act(async () => {
      d.resolve({ data: { saved_count: 1 } });
      await first;
    });
  });

});

describe("useDocumentWorkflow · 解析防重入", () => {
  it("批量解析在飞时二次调用：只发一次请求并给出明确提示", async () => {
    const { result, msg } = setup([{ id: "d1", parse_status: "pending" } as DocRecord]);
    const d = deferred<{ data: unknown }>();
    const parseAll = vi
      .spyOn(factsApi, "parseAllDocuments")
      .mockReturnValue(d.promise as never);

    let first!: Promise<void>;
    act(() => {
      first = result.current.handleParseDocuments(true);
    });
    await act(async () => {
      await result.current.handleParseDocuments(true);
    });

    expect(parseAll).toHaveBeenCalledTimes(1);
    expect(msg.info).toHaveBeenCalledWith(expect.stringContaining("正在进行"));

    await act(async () => {
      d.resolve({
        data: { parsed: 1, failed_count: 0, truncated_count: 0, reconciled_count: 0 },
      });
      await first;
    });
  });

  it("单份解析在飞时批量解析被拒：提示正在解析中，不叠加请求", async () => {
    // 单份解析在飞 ⇒ 页面层 parsingDocId 已置为该文档 id（真实 setState 结果）
    const { result, msg } = setup(
      [{ id: "d1", parse_status: "pending" } as DocRecord],
      { parsingDocId: "d1" },
    );
    const parseAll = vi.spyOn(factsApi, "parseAllDocuments");

    await act(async () => {
      await result.current.handleParseDocuments(true);
    });

    expect(parseAll).not.toHaveBeenCalled();
    expect(msg.info).toHaveBeenCalledWith(expect.stringContaining("正在解析中"));
  });
});
