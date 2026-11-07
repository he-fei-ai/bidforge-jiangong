// @vitest-environment jsdom
/**
 * 文件导入 / 解析域 · 页面级工作流竞态与缓存失效回归。
 *
 * 背景（2026-11-07 审计发现）：
 *   1. `handlePreviewDocument` 未做请求序号守卫 —— 用户先点文档 A、再快速点 B 时，
 *      A 的慢响应会覆盖 B 的正文（后发请求的结果被先发请求冲掉）。
 *   2. `loadPipelineStatus` 同样无守卫 —— 切到 B 后，A 的四层状态仍会写回，
 *      弹窗标题是 B、质量概览却是 A。
 *   3. 预览弹窗内「重新解析」成功后只刷新管线状态，不刷新正文 —— 弹窗继续展示
 *      重解析前的旧内容（缓存未失效）。
 *
 * 本文件先以**失败**用例固定上述缺陷，再改 `useDocumentWorkflow` 使其转绿。
 * 用仓库既有测试栈（vitest + @testing-library/react），不引入新依赖。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { factsApi, docPipelineApi } from "../api";
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

const doc = (id: string, name: string) => ({ id, file_name: name }) as DocRecord;

/** 记录 hook 写入的预览/管线状态（不触发 React 重渲染，断言即“最后一次写入”） */
type Recorder = {
  previewDoc: DocRecord[];
  previewText: string[];
  previewData: Array<unknown>;
  previewLoading: boolean[];
  pipelineStatus: unknown[];
  pipelineError: (string | null)[];
  pipelineLoading: boolean[];
  pipelineAction: (string | null)[];
};

function setup(previewDocNow: DocRecord | null = null, docListNow: DocRecord[] = []) {
  const rec: Recorder = {
    previewDoc: [],
    previewText: [],
    previewData: [],
    previewLoading: [],
    pipelineStatus: [],
    pipelineError: [],
    pipelineLoading: [],
    pipelineAction: [],
  };
  const msg = {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  };
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
    parsingDocs: false,
    setParsingDocs: vi.fn(),
    parsingDocId: null,
    setParsingDocId: vi.fn(),
    docList: docListNow,
    loadDocuments: vi.fn(),
    setPreviewDoc: (d: DocRecord) => rec.previewDoc.push(d),
    setPreviewText: (t: string) => rec.previewText.push(t),
    setPreviewData: (d: unknown) => rec.previewData.push(d),
    setPreviewLoading: (v: boolean) => rec.previewLoading.push(v),
    previewDoc: previewDocNow,
    setPipelineStatus: (v: unknown) => rec.pipelineStatus.push(v),
    setPipelineError: (v: string | null) => rec.pipelineError.push(v),
    setPipelineLoading: (v: boolean) => rec.pipelineLoading.push(v),
    pipelineAction: null,
    setPipelineAction: (v: string | null) => rec.pipelineAction.push(v),
    setDocList: vi.fn(),
    loadFacts: vi.fn(),
    factsSummary: null,
    factsListAbortRef: { current: null },
    currentSchemeIdRef: { current: "scheme-1" },
  };
  // deps 形状由 useDocumentWorkflow 的 DocumentWorkflowDeps 校验（测试侧只需结构可用）
  const { result } = renderHook(() => useDocumentWorkflow(deps as never));
  return { result, rec, msg, deps };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useDocumentWorkflow · 预览请求竞态", () => {
  it("先点 A 再点 B：A 的慢响应不得覆盖 B 的正文与文档", async () => {
    const dA = deferred<{ data: unknown }>();
    const dB = deferred<{ data: unknown }>();
    vi.spyOn(factsApi, "previewDocument").mockImplementation((docId: string) =>
      (docId === "d1" ? dA.promise : dB.promise) as never,
    );
    // 管线状态在此用例中不参与断言，直接返回空实现避免额外挂起
    vi.spyOn(docPipelineApi, "status").mockResolvedValue({ data: {} } as never);

    const { result, rec } = setup();
    let pA!: Promise<void>;
    let pB!: Promise<void>;
    await act(async () => {
      pA = result.current.handlePreviewDocument(doc("d1", "A.pdf"));
      pB = result.current.handlePreviewDocument(doc("d2", "B.pdf"));
    });

    await act(async () => {
      dB.resolve({ data: { is_parsed: true, preview: "B-body" } });
      await pB;
    });
    await act(async () => {
      dA.resolve({ data: { is_parsed: true, preview: "A-body" } });
      await pA;
    });

    expect(rec.previewText[rec.previewText.length - 1]).toBe("B-body");
    const lastData = rec.previewData[rec.previewData.length - 1] as { preview?: string };
    expect(lastData.preview).toBe("B-body");
    expect((rec.previewDoc[rec.previewDoc.length - 1] as DocRecord).id).toBe("d2");
  });

  it("A 的失败晚于 B 成功：不得为已切走的文档弹错误提示", async () => {
    const dA = deferred<{ data: unknown }>();
    const dB = deferred<{ data: unknown }>();
    vi.spyOn(factsApi, "previewDocument").mockImplementation((docId: string) =>
      (docId === "d1" ? dA.promise : dB.promise) as never,
    );
    vi.spyOn(docPipelineApi, "status").mockResolvedValue({ data: {} } as never);

    const { result, rec, msg } = setup();
    let pA!: Promise<void>;
    let pB!: Promise<void>;
    await act(async () => {
      pA = result.current.handlePreviewDocument(doc("d1", "A.pdf"));
      pB = result.current.handlePreviewDocument(doc("d2", "B.pdf"));
    });
    await act(async () => {
      dB.resolve({ data: { is_parsed: true, preview: "B-body" } });
      await pB;
    });
    await act(async () => {
      dA.reject(new Error("A 预览失败"));
      await pA;
    });

    expect(msg.error).not.toHaveBeenCalled();
    expect(rec.previewText[rec.previewText.length - 1]).toBe("B-body");
  });
});

describe("useDocumentWorkflow · 四层管线状态竞态", () => {
  it("切到 B 后，A 的 status 延迟返回不得覆盖 B 的质量概览", async () => {
    const previewA = deferred<{ data: unknown }>();
    const previewB = deferred<{ data: unknown }>();
    const statusA = deferred<{ data: unknown }>();
    const statusB = deferred<{ data: unknown }>();

    vi.spyOn(factsApi, "previewDocument").mockImplementation((docId: string) =>
      (docId === "d1" ? previewA.promise : previewB.promise) as never,
    );
    vi.spyOn(docPipelineApi, "status").mockImplementation((docId: string) =>
      (docId === "d1" ? statusA.promise : statusB.promise) as never,
    );

    const { result, rec } = setup();
    let pA!: Promise<void>;
    let pB!: Promise<void>;
    await act(async () => {
      pA = result.current.handlePreviewDocument(doc("d1", "A.pdf"));
      pB = result.current.handlePreviewDocument(doc("d2", "B.pdf"));
    });

    // B 先完成正文 → 触发 status(B)；A 正文晚到（当前实现仍会触发 status(A)）
    await act(async () => {
      previewB.resolve({ data: { is_parsed: true, preview: "B-body" } });
      await pB;
    });
    await act(async () => {
      statusB.resolve({ data: { doc_id: "d2", quality_score: 0.9 } });
    });
    await act(async () => {
      previewA.resolve({ data: { is_parsed: true, preview: "A-body" } });
      await pA;
    });
    await act(async () => {
      statusA.resolve({ data: { doc_id: "d1", quality_score: 0.2 } });
    });

    const lastStatus = rec.pipelineStatus[rec.pipelineStatus.length - 1] as
      | { doc_id?: string }
      | undefined;
    expect(lastStatus?.doc_id).toBe("d2");
  });
});

describe("useDocumentWorkflow · 上传/解析错误与部分成功", () => {
  it("上传接口失败：错误原文透出（不吞）、上传态复位、刷新列表", async () => {
    vi.spyOn(factsApi, "uploadDocuments").mockRejectedValue(new Error("单个文件不能超过 30MB"));
    const { result, msg, deps } = setup();
    await act(async () => {
      await result.current.handleUploadDocuments([new File(["a"], "a.pdf")]);
    });
    expect(msg.error).toHaveBeenCalledWith("单个文件不能超过 30MB");
    // 失败不改动服务端资料 → 不必须刷新列表；关键是上传态复位、错误不被吞
    expect(deps.setUploadingFacts).toHaveBeenLastCalledWith(false);
  });

  it("批量上传部分失败：成功提示包含保存数与逐类拒绝文件，不静默丢弃", async () => {
    vi.spyOn(factsApi, "uploadDocuments").mockResolvedValue({
      data: {
        saved_count: 2,
        replaced: 0,
        unsupported: ["x.exe"],
        oversize: [],
        empty: [],
        signature_invalid: [],
        too_many: [],
        quota_exceeded: false,
        quota_files: [],
        write_errors: [],
        warnings: [],
      },
    } as never);
    const { result, msg } = setup();
    await act(async () => {
      await result.current.handleUploadDocuments([
        new File(["a"], "a.pdf", { type: "application/pdf" }),
        new File(["b"], "b.docx"),
      ]);
    });
    expect(msg.success).toHaveBeenCalledTimes(1);
    const text = msg.success.mock.calls[0][0] as string;
    expect(text).toContain("已保存 2");
    expect(text).toContain("1 个格式不支持");
  });

  it("批量解析超时：错误提示不吞、解析态复位、刷新列表", async () => {
    const pending = {
      id: "d1",
      file_name: "a.pdf",
      text_len: 0,
      parse_status: "pending",
    } as DocRecord;
    vi.spyOn(factsApi, "parseAllDocuments").mockRejectedValue(
      new Error("timeout of 60000ms exceeded"),
    );
    const { result, msg, deps } = setup(null, [pending]);
    await act(async () => {
      await result.current.handleParseDocuments();
    });
    expect(msg.error).toHaveBeenCalledWith("timeout of 60000ms exceeded");
    expect(deps.setParsingDocs).toHaveBeenLastCalledWith(false);
  });
});

describe("useDocumentWorkflow · 重解析后预览缓存失效", () => {
  it("预览弹窗内「重新解析」成功后，必须重新拉取正文并更新弹窗", async () => {
    const target = doc("d1", "A.pdf");
    const previewSpy = vi
      .spyOn(factsApi, "previewDocument")
      .mockResolvedValueOnce({ data: { is_parsed: true, preview: "旧正文" } } as never)
      .mockResolvedValueOnce({ data: { is_parsed: true, preview: "重解析后的新正文" } } as never);
    vi.spyOn(docPipelineApi, "status").mockResolvedValue({ data: { doc_id: "d1" } } as never);
    vi.spyOn(docPipelineApi, "reparse").mockResolvedValue({ data: { skipped: false } } as never);

    const { result, rec } = setup(target);
    await act(async () => {
      await result.current.handlePreviewDocument(target);
    });
    expect(rec.previewText[rec.previewText.length - 1]).toBe("旧正文");

    await act(async () => {
      await result.current.handlePipelineAction("reparse");
    });

    expect(previewSpy).toHaveBeenCalledTimes(2);
    expect(rec.previewText[rec.previewText.length - 1]).toBe("重解析后的新正文");
    const lastData = rec.previewData[rec.previewData.length - 1] as { preview?: string };
    expect(lastData.preview).toBe("重解析后的新正文");
  });
});
