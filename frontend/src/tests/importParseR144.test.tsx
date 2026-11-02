// @vitest-environment jsdom
import { render, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App as AntdApp } from "antd";
import UploadParseTab from "../components/UploadParseTab";

afterEach(() => {
  cleanup();
});

// 静态接线锁沿用本仓既有范式（exportResponse / importParseWarningWiringR72）：
// Vite `import.meta.glob(..., { query: "?raw" })` 读源码文本，不引 node:fs
// （本项目没装 @types/node，tests 也在 tsc 范围内）。
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"] ?? "";
const TAB = RAW["components/UploadParseTab.tsx"] ?? "";
const HOOK = RAW["hooks/useDelayedCallback.ts"] ?? "";

function renderTab(onUploadFiles = vi.fn()) {
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
  return { input, onUploadFiles };
}

describe("R144 · F9 上传延后触发（行为级，hook 收敛后正常路径不变）", () => {
  it("单次选文件仍触发一次 onUploadFiles（收敛不得误伤正常路径）", async () => {
    const { input, onUploadFiles } = renderTab();
    const f1 = new File(["a"], "a.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [f1] } });
    await waitFor(() => expect(onUploadFiles).toHaveBeenCalledTimes(1));
    expect((onUploadFiles.mock.calls[0][0] as File[])[0].name).toBe("a.pdf");
  });
});

describe("R157 · L1 收口：双上传入口收敛到 useDelayedCallback 单一实现", () => {
  it("读取到三份源码（防空跑成常绿）", () => {
    expect(PAGE.length).toBeGreaterThan(10000);
    expect(TAB.length).toBeGreaterThan(1000);
    expect(HOOK.length).toBeGreaterThan(200);
  });

  it("hook 本体锁：赋值前清旧 timer + 卸载 effect 清理 + 回调置空", () => {
    const clearIdx = HOOK.indexOf("window.clearTimeout(timerRef.current);");
    const setIdx = HOOK.indexOf("timerRef.current = window.setTimeout(() => {");
    expect(clearIdx).toBeGreaterThanOrEqual(0);
    expect(clearIdx).toBeLessThan(setIdx);
    expect(HOOK).toContain("useEffect(() => clear, []);");
    expect(HOOK).toContain("timerRef.current = null;");
  });

  it("两条入口都必须消费 hook（页面入口显式 clear，Tab 依赖 hook 卸载自清理）", () => {
    for (const [name, src, fire] of [
      ["page", PAGE, "uploadTrigger.schedule(() => handleUploadDocuments(files));"],
      ["tab", TAB, "scheduleUpload(() => onUploadFiles(accepted));"],
    ] as const) {
      expect(src).toContain("useDelayedCallback()");
      expect(src).toContain(fire);
    }
    // 页面的卸载清单与各 AbortController 并列处显式 clear（幂等，双保险）
    expect(PAGE).toContain("uploadTrigger.clear();");
  });

  it("双份手工 uploadTimerRef 副本必须消失（收敛的反向锚）", () => {
    expect(PAGE).not.toContain("uploadTimerRef");
    expect(TAB).not.toContain("uploadTimerRef");
  });

  it("禁止回归裸 setTimeout 幽灵上传写法（R144 缺陷原始形态）", () => {
    expect(PAGE).not.toContain("setTimeout(() => handleUploadDocuments(files), 0);");
  });

  it("两条入口的禁用/防重入口径都必须含解析并发位（parsingDocId，R144 契约保持）", () => {
    expect(PAGE).toContain("|| !!parsingDocId");
    // ⚠️ 位点绑定锚（R144 教训②）：`if (parsingDocs || parsingDocId) return;`
    //    在页面的解析处理器里还有同形出现，只断字符串会在「摘掉上传入口守卫」
    //    的变异下被其他出现满足（A/B FM2 首轮 MISSED 即此因）。必须断
    //    「handleUploadDocuments 注释尾行 + 守卫行」的相邻序列。
    expect(PAGE).toContain(
      "的 `parsingDocs || parsingDocId` 防重入口径对齐。\n    if (parsingDocs || parsingDocId) return;"
    );
    expect(TAB).toContain("parsingDocs || !!parsingDocId");
  });
});
