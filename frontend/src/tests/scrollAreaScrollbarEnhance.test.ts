/**
 * 滚动条增强护栏（2026-10-30）：6 步工作流标签页（解析提取/目录生成/全局事实/
 * 正文生成/审核与预检/导出文档）的内容区均复用 `.scroll-area` 滚动容器，
 * 其滚动条样式统一收敛在 index.css。
 *
 * 背景：Chrome/Edge 121+ 与 Firefox 中，标准属性 scrollbar-width/scrollbar-color
 * 优先于 ::-webkit-scrollbar 系列生效。旧配置 `thin + 透明轨道` 导致滚动条又细
 * 又难辨认（透明轨道上只剩 ~6px 浅灰滑块），用户难以看到、更难以抓取拖动。
 *
 * 本护栏锁定增强后的口径：
 *   1. `.scroll-area` 标准属性 = auto 宽度 + #bfbfbf 滑块 / #f5f5f5 可见轨道；
 *   2. ::-webkit-scrollbar 兜底（旧浏览器）= 12px 宽 + 可见轨道 + 滑块 min-height；
 *   3. 接线锚：6 个 Tab 的内容根（页面内联 5 处 + docs 子 Tab 包裹 1 处）与
 *      UploadParseTab / BidAnalysisTab / ContentGenerationTab 组件根均挂 scroll-area。
 *
 * 源码读取沿用本仓既有范式：Vite `import.meta.glob(..., { query: "?raw" })`，
 * 不引 node:fs（本项目没装 @types/node，tests 在 tsc 范围内）。
 */
import { describe, it, expect } from "vitest";
import css from "../index.css?raw";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import uploadParseSource from "../components/UploadParseTab.tsx?raw";
import bidAnalysisSource from "../components/BidAnalysisTab.tsx?raw";
import contentGenSource from "../components/ContentGenerationTab.tsx?raw";

/** 取 selector 首个匹配块的花括号内文本 */
function blockOf(source: string, selector: string): string {
  const idx = source.indexOf(selector);
  expect(idx, `找不到选择器：${selector}`).toBeGreaterThanOrEqual(0);
  const open = source.indexOf("{", idx);
  const close = source.indexOf("}", open);
  return source.slice(open + 1, close);
}

describe("index.css .scroll-area 滚动条增强（2026-10-30）", () => {
  it("标准属性：auto 宽度 + 滑块/轨道双色可见（不再是 thin + 透明轨道）", () => {
    const block = blockOf(css, ".scroll-area {");
    expect(block).toContain("scrollbar-width: auto");
    expect(block).toContain("scrollbar-color: #bfbfbf #f5f5f5");
    expect(block, "不允许回退到 thin（细滚动条难以抓取拖动）").not.toContain("scrollbar-width: thin");
    expect(block, "轨道色不允许回退为透明").not.toContain("#bfbfbf transparent");
  });

  it("webkit 兜底：12px 宽 + 可见轨道 + 滑块最小可抓取高度", () => {
    expect(blockOf(css, ".scroll-area::-webkit-scrollbar {")).toContain("width: 12px");
    expect(blockOf(css, ".scroll-area::-webkit-scrollbar-track {")).toContain("#f5f5f5");
    const thumb = blockOf(css, ".scroll-area::-webkit-scrollbar-thumb {");
    expect(thumb).toContain("min-height: 32px");
    expect(thumb).toContain("#bfbfbf");
  });

  it("接线锚：6 个 Tab 内容根均挂 scroll-area（页面 ≥5 处 + 3 个内容组件根各 1 处）", () => {
    // ✅ R146（2026-10-30）：分栏 pane 改用 `scroll-area bp-pane-scroll`（6px 细条），
    //    判据放宽为「className 以 scroll-area 开头」，仍锁定「每个 Tab 根都有滚动容器」。
    expect(
      (pageSource.match(/className="scroll-area[^"]*"/g) ?? []).length,
      "页面内联滚动容器（import docs 包裹 / outline / facts / review / export Form）应 ≥5 处",
    ).toBeGreaterThanOrEqual(5);
    for (const [name, src] of [
      ["UploadParseTab", uploadParseSource],
      ["BidAnalysisTab", bidAnalysisSource],
      ["ContentGenerationTab", contentGenSource],
    ] as const) {
      expect(src.length, `应能读到 ${name}.tsx 源码`).toBeGreaterThan(0);
      expect(src, `${name} 根容器应挂 scroll-area`).toMatch(/className="scroll-area[^"]*"/);
    }
  });
});
