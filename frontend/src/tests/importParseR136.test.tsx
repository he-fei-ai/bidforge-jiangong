// @vitest-environment jsdom
/**
 * 解析提取模块 · R136 组件级交互护栏（2026-10-27）。
 *
 * 背景：R136 补齐了 `.tif` 格式缺口（TIFF 的两个官方扩展名 .tif/.tiff 此前只放行
 * 后者，而多数扫描仪/传真件默认输出 .tif）。该缺口在四个层面同时存在：
 *   ① 后端 `file_parser.SUPPORTED_EXTENSIONS` / `_BINARY_SIGNATURES` / 分派分支
 *   ② 后端目录识别 `upload_outline.is_image`（OCR 惩罚名单）
 *   ③ 前端白名单 `utils/uploadAccept.ts`
 *   ④ 前端资料列表的扩展名图标映射
 *
 * 层 ①③ 已有护栏（`test_upload_format_contract.py` 四层一致性 + `uploadAccept.test.ts`
 * 集合相等）。本文件补的是**层 ③ 在组件链路上的落地**与**层 ④**：
 *   A 组 —— `Upload` 组件的 accept 属性（组件级，不只是 util 常量）真的含 .tif，
 *            且必须逐字等于 `UPLOAD_FILE_ACCEPT`（防组件内二次硬编码再度漂移）；
 *   B 组 —— `.tif` / 大写 `.TIF` 能真正穿过 Tab 的硬校验进入 onUploadFiles，
 *            与非法类型混批时不误伤（类型拒绝提示不得点名 .tif）；
 *   C 组 —— 资料列表扩展名图标映射含 tif（摘除即回落到 📎 通用图标）。
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, waitFor, act } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";
import UploadParseTab from "../components/UploadParseTab";
import { UPLOAD_FILE_ACCEPT } from "../utils/uploadAccept";

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

type Overrides = Partial<React.ComponentProps<typeof UploadParseTab>>;

function setup(over: Overrides = {}) {
  const cbs = {
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
  const utils = render(
    <AntdApp>
      <UploadParseTab
        docs={[]}
        categoryOptions={[]}
        generating={false}
        uploadingFacts={false}
        uploadedFiles={[]}
        parsingDocs={false}
        parsingDocId={null}
        {...cbs}
        {...over}
      />
    </AntdApp>
  );
  const input = utils.container.querySelector('input[type="file"]') as HTMLInputElement | null;
  return { ...utils, ...cbs, input };
}

/** 等 antd message 提示（App 上下文 holder 渲染在 document.body） */
async function expectToast(text: string): Promise<void> {
  await waitFor(() => expect(document.body.textContent || "").toContain(text));
}

async function flushTimer(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 10));
  });
}

describe("R136-A · 组件级 accept 落地", () => {
  it("前提：Upload 渲染出 file input（防空跑成常绿）", () => {
    const { input } = setup();
    expect(input).not.toBeNull();
  });

  it("file input 的 accept 属性含 .tif 与 .tiff（两个官方扩展名都要在）", () => {
    const { input } = setup();
    const accept = input!.getAttribute("accept") || "";
    const parts = accept.split(",").map((s) => s.trim().replace(/^\./, ""));
    expect(parts).toContain("tif");
    expect(parts).toContain("tiff");
  });

  it("accept 属性逐字等于 UPLOAD_FILE_ACCEPT（组件内不得二次硬编码）", () => {
    const { input } = setup();
    expect(input!.getAttribute("accept")).toBe(UPLOAD_FILE_ACCEPT);
  });
});

describe("R136-B · .tif 穿过组件硬校验", () => {
  it(".tif 扫描件被接受并交给 onUploadFiles（不再被类型校验剔除）", async () => {
    const { input, onUploadFiles } = setup();
    const tif = new File(["II*\u0000"], "scan.tif", { type: "image/tiff" });
    fireEvent.change(input!, { target: { files: [tif] } });

    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    const files = onUploadFiles.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(["scan.tif"]);
  });

  it("大写 .TIF 同样被接受（扩展名大小写不敏感）", async () => {
    const { input, onUploadFiles } = setup();
    const upper = new File(["II*\u0000"], "SCAN.TIF", { type: "image/tiff" });
    fireEvent.change(input!, { target: { files: [upper] } });

    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    const files = onUploadFiles.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(["SCAN.TIF"]);
  });

  it("与非法类型混批：只上传 .tif，类型拒绝提示不得点名 .tif", async () => {
    const { input, onUploadFiles } = setup();
    const tif = new File(["II*\u0000"], "scan.tif", { type: "image/tiff" });
    const evil = new File(["x"], "evil.exe", { type: "application/octet-stream" });
    fireEvent.change(input!, { target: { files: [tif, evil] } });

    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    const files = onUploadFiles.mock.calls[0][0] as File[];
    expect(files.map((f) => f.name)).toEqual(["scan.tif"]);

    await expectToast("不支持的文件类型，已忽略：evil.exe");
    // 反向锁：合法文件不得出现在「已忽略」清单里
    expect(document.body.textContent || "").not.toContain("已忽略：scan.tif");
  });

  it("空列表 + 只有合法 .tif 时不得把该文件列入「已忽略」清单", async () => {
    const { input, onUploadFiles } = setup();
    fireEvent.change(input!, {
      target: { files: [new File(["II*\u0000"], "only.tif", { type: "image/tiff" })] },
    });
    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    await flushTimer();
    // 断言语义收敛到「本文件不得被点名」：antd message holder 挂在 document.body
    // 且 RTL cleanup 不会清理它，上一个用例的 toast 仍在 ⇒ 不能用全局「已忽略」。
    expect(document.body.textContent || "").not.toContain("已忽略：only.tif");
  });
});

// ---------------------------------------------------------------------------
// C 组：资料列表扩展名图标映射（源码锚）
// 判据实现沿用本仓既有范式（importParseWarningWiringR72.test.ts）：用 Vite
// `import.meta.glob(..., { query: "?raw" })` 读源码文本，不引 node:fs。
// ---------------------------------------------------------------------------
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

/** 截取 `const ftypeIcon: Record<string, string> = { ... };` 的字面量块 */
function ftypeIconBody(src: string): string {
  const start = src.indexOf("const ftypeIcon: Record<string, string> = {");
  if (start < 0) return "";
  const end = src.indexOf("};", start);
  return src.slice(start, end < 0 ? src.length : end);
}

describe("R136-C · 资料列表图标映射含 tif", () => {
  it("前提：能取到被测文件与 ftypeIcon 字面量（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(ftypeIconBody(PAGE).length).toBeGreaterThan(50);
  });

  it("映射表含 tif 键（否则 .tif 资料在列表里回落 📎 通用图标）", () => {
    const body = ftypeIconBody(PAGE);
    // 注意：`tiff:` 不含子串 `tif:`，故该断言只认 tif 自己的键
    expect(body).toContain("tif:");
  });

  it("映射表同时保留 tiff（同族扩展名不得顾此失彼）", () => {
    expect(ftypeIconBody(PAGE)).toContain("tiff:");
  });

  it("反向锚：摘除 tif 键后判据必须变红（证明扫描真的在守）", () => {
    const stripped = PAGE.replace(/tif:\s*"🖼️",\s*/, "");
    expect(ftypeIconBody(stripped)).not.toContain("tif:");
  });
});
