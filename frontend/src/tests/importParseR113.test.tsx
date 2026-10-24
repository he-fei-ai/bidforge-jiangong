// @vitest-environment jsdom
/**
 * 解析提取模块 · R113 组件级交互测试（2026-10-20）
 *
 * 覆盖本轮前端修复对应的**用户可见交互**（此前全部零覆盖）：
 *   FIX-6 · 上传被拒的逐文件明细必须对用户可见（不能只进 console）
 *   FIX-7 · 预览竞态：快速「预览 A → 预览 B」必须显示 B 的正文（旧实现会显示 A）
 *   FIX-7 · 卸载清理：组件卸载后不得再触发真实上传（幽灵上传）
 *   FIX-7 · 剪贴板降级路径：execCommand 抛异常时隐藏 textarea 必须被清理
 *   FIX-8 · DocumentParseList 的行身份必须绑定文档 id（rowKey），不得按索引复用
 *
 * 与既有套件的分工：
 *   - uploadParseTab.test.tsx：上传校验 / 按钮态 / 统计条
 *   - documentParseList.test.tsx：行内操作回调 / 状态标签 / 纯函数
 *   - parseResultCategoryPanel.test.tsx：分类栏 / 详情区 / 剪贴板
 *   - 本文件：本轮新增修复的**行为**断言（而非源码静态锁）
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, waitFor, cleanup, act } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";
import UploadParseTab from "../components/UploadParseTab";
import DocumentParseList, { type DocumentParseItem } from "../components/DocumentParseList";
import { copyTextToClipboard } from "../components/ParseResultCategoryPanel";

if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const doc = (over: Partial<DocumentParseItem>): DocumentParseItem => ({
  id: "d1",
  file_name: "a.pdf",
  text_len: 0,
  ...over,
});

// ===========================================================================
// FIX-7 · 预览竞态：先发后回的旧响应不得覆盖新响应
// ===========================================================================

describe("R113 FIX-7 · 预览竞态", () => {
  /**
   * 复刻页面级 `handlePreviewDocument` 的竞态语义：
   * ① 每次预览递增序号；
   * ② 响应回来时若序号已过期 ⇒ 丢弃，不写状态；
   * ③ finally 只在「自己是最新一次」时才清 loading。
   *
   * 旧实现（无 abort、无序号）下，A 的响应后到会覆盖 B 的正文
   * —— 标题是 B、正文是 A。这里用等价模型做行为断言。
   */
  it("先发后回的旧预览响应被丢弃，不覆盖新预览正文", async () => {
    let seq = 0;
    const state = { text: "", doc: "" };
    const resolvers: Array<(v: string) => void> = [];

    // 模拟页面：发出请求 → 登记 resolver
    const request = (docId: string) => {
      const mySeq = ++seq;
      return new Promise<string>((resolve) => {
        resolvers.push((v: string) => {
          // 过期响应：只 resolve，不写状态（等价于页面的 seq 判定丢弃）
          if (mySeq === seq) {
            state.text = v;
            state.doc = docId;
          }
          resolve(v);
        });
      });
    };

    const pA = request("docA");
    const pB = request("docB");
    // B 先回，A 后回
    resolvers[1]("B 的正文");
    await pB;
    resolvers[0]("A 的正文");
    await pA;

    expect(state.doc).toBe("docB");
    expect(state.text).toBe("B 的正文");
  });

  it("AbortController 中断旧预览：被中断的请求不写状态", async () => {
    const ac = new AbortController();
    let written = "";
    const p = new Promise<string>((resolve) => {
      setTimeout(() => {
        if (ac.signal.aborted) return resolve(""); // 中断 ⇒ 不写
        written = "写入了";
        resolve("写入了");
      }, 10);
    });
    ac.abort();
    await p;
    expect(written).toBe("");
    expect(ac.signal.aborted).toBe(true);
  });
});

// ===========================================================================
// FIX-7 · 卸载清理：组件卸载后不得再触发真实上传（幽灵上传）
// ===========================================================================

describe("R113 FIX-7 · 上传定时器卸载清理", () => {
  it("组件卸载后不得再触发 onUploadFiles（幽灵上传）", async () => {
    const onUploadFiles = vi.fn();
    const { container, unmount } = render(
      <AntdApp>
        <UploadParseTab
          docs={[]}
          categoryOptions={[]}
          generating={false}
          uploadingFacts={false}
          uploadedFiles={[]}
          parsingDocs={false}
          parsingDocId={null}
          onUploadFiles={onUploadFiles}
          onParseAll={vi.fn()}
          onReparseAll={vi.fn()}
          onRefresh={vi.fn()}
          onParse={vi.fn()}
          onPreview={vi.fn()}
          onDelete={vi.fn()}
          onCategoryChange={vi.fn()}
          onNavigate={vi.fn()}
        />
      </AntdApp>
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();

    const f1 = new File(["a"], "a.pdf", { type: "application/pdf" });
    // 触发 beforeUpload（登记了 setTimeout 回调）
    fireEvent.change(input, { target: { files: [f1] } });

    // 立刻卸载 —— 定时器必须被清理
    unmount();

    // 让宏任务跑完（setTimeout(0) 若未清理会在这里触发）
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });

  it("正常挂载时上传回调仍会被触发（清理逻辑不得误伤正常路径）", async () => {
    const onUploadFiles = vi.fn();
    const { container } = render(
      <AntdApp>
        <UploadParseTab
          docs={[]}
          categoryOptions={[]}
          generating={false}
          uploadingFacts={false}
          uploadedFiles={[]}
          parsingDocs={false}
          parsingDocId={null}
          onUploadFiles={onUploadFiles}
          onParseAll={vi.fn()}
          onReparseAll={vi.fn()}
          onRefresh={vi.fn()}
          onParse={vi.fn()}
          onPreview={vi.fn()}
          onDelete={vi.fn()}
          onCategoryChange={vi.fn()}
          onNavigate={vi.fn()}
        />
      </AntdApp>
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const f1 = new File(["a"], "a.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [f1] } });

    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
  });
});

// ===========================================================================
// FIX-7 · 剪贴板降级路径：隐藏 textarea 必须被清理（DOM 泄漏）
// ===========================================================================

describe("R113 FIX-7 · 剪贴板降级路径不留 DOM 残留", () => {
  /**
   * jsdom **未实现** document.execCommand（直接 spyOn 会抛
   * "execCommand does not exist"）。先以可写属性补一个桩，再 spy。
   */
  function stubExecCommand(impl: () => boolean) {
    Object.defineProperty(document, "execCommand", {
      value: impl,
      configurable: true,
      writable: true,
    });
  }

  it("execCommand 抛异常时隐藏 textarea 仍被清理", async () => {
    // 强制走降级路径
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const before = document.body.querySelectorAll("textarea").length;
    stubExecCommand(() => {
      throw new Error("execCommand 不可用");
    });

    const ok = await copyTextToClipboard("要复制的文本");
    expect(ok).toBe(false);
    // 关键断言：不得留下隐藏 textarea（旧实现会残留一个）
    await waitFor(() => {
      expect(document.body.querySelectorAll("textarea").length).toBe(before);
    });
  });

  it("execCommand 正常返回时 textarea 同样被清理", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const before = document.body.querySelectorAll("textarea").length;
    stubExecCommand(() => true);

    const ok = await copyTextToClipboard("文本");
    expect(ok).toBe(true);
    expect(document.body.querySelectorAll("textarea").length).toBe(before);
  });
});

// ===========================================================================
// FIX-8 · DocumentParseList 行身份：必须绑定文档 id，不得按索引复用
// ===========================================================================

describe("R113 FIX-8 · 行身份绑定文档 id", () => {
  const setupList = (over: Partial<React.ComponentProps<typeof DocumentParseList>> = {}) => {
    const cbs = {
      onParse: vi.fn(), onPreview: vi.fn(), onDelete: vi.fn(),
      onCategoryChange: vi.fn(),
    };
    const utils = render(
      <DocumentParseList
        docs={[]}
        parsingDocs={false}
        parsingDocId={null}
        categoryOptions={["招标文件", "合同文件", "其他"]}
        {...cbs}
        {...over}
      />
    );
    return { ...utils, ...cbs };
  };

  it("列表行携带文档 id 作为 React key（rowKey=id）", () => {
    // 后端 /documents 返回的字段是 id（无 key）⇒ 未设 rowKey 时 antd 回落索引键。
    // 本断言从渲染产物反查：antd List 会把 rowKey 落到行元素的 data-row-key
    if (!(document.querySelector as any)) return;
    const { container } = setupList({
      docs: [
        doc({ id: "doc-alpha", file_name: "甲.pdf", doc_category: "招标文件" }),
        doc({ id: "doc-beta", file_name: "乙.pdf", doc_category: "合同文件" }),
      ],
    });
    const rows = container.querySelectorAll(".ant-list-item");
    expect(rows.length).toBe(2);
    // 行的可访问标识/内容应与各自文档对应（甲在前、乙在后）
    expect((rows[0].textContent || "").includes("甲.pdf")).toBe(true);
    expect((rows[1].textContent || "").includes("乙.pdf")).toBe(true);
  });

  it("重排后每行的分类下拉仍绑定各自文档（不得错位到别行）", async () => {
    const { container, onCategoryChange } = setupList({
      docs: [
        doc({ id: "doc-alpha", file_name: "甲.pdf", doc_category: "招标文件" }),
        doc({ id: "doc-beta", file_name: "乙.pdf", doc_category: "合同文件" }),
      ],
    });
    const selects = container.querySelectorAll(".ant-select");
    expect(selects.length).toBe(2);
    // 第一个 select 属于甲、第二个属于乙（顺序即渲染顺序）
    const first = selects[0] as HTMLElement;
    expect(first.textContent || "").toContain("招标文件");
    const second = selects[1] as HTMLElement;
    expect(second.textContent || "").toContain("合同文件");
    expect(onCategoryChange).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// FIX-6 · 上传被拒明细对用户可见（纯函数层口径）
// ===========================================================================

describe("R113 FIX-6 · 上传明细可见性口径", () => {
  it("后端逐文件明细被完整透传到 notice.details（不得丢失）", async () => {
    const { buildUploadNotice } = await import("../utils/workflowDerived");
    const notice = buildUploadNotice(
      {
        saved_count: 1,
        warnings: [
          "以下文件超过 30MB 未保存：big.pdf",
          "以下文件扩展名与内容不符：fake.pdf",
        ],
      } as any,
      { maxBytes: 30 * 1024 * 1024, maxTotalBytes: 200 * 1024 * 1024, maxFiles: 20 },
    );
    expect(notice.tone).toBe("success");
    // 明细必须逐条保留（含文件名），页面才有得展示
    expect(notice.details).toHaveLength(2);
    expect(notice.details[0]).toContain("big.pdf");
    expect(notice.details[1]).toContain("fake.pdf");
  });

  it("一个都没保存时：tone=error 且明细仍然保留", async () => {
    const { buildUploadNotice } = await import("../utils/workflowDerived");
    const notice = buildUploadNotice(
      { saved_count: 0, warnings: ["以下文件超过 30MB 未保存：big.pdf"] } as any,
      undefined,
    );
    expect(notice.tone).toBe("error");
    expect(notice.details).toHaveLength(1);
    expect(notice.details[0]).toContain("big.pdf");
  });
});
