/**
 * 6 步工作流导航护栏（源码级断言，参照后端 test_outline_stepwise_concurrency 的做法）。
 *
 * 历史背景：
 *   - 2026-09-20 事故：SchemeWorkbenchPage.tsx 重建时丢失了「上传解析」(import)
 *     与「提取项目」(bidAnalysis) 两个 Tab，且步骤号错位、下一步链硬编码绕步。
 *   - 2026-09-23 合并：把「上传解析」+「提取项目」合并为一个顶层 Tab（import），
 *     bidAnalysis 收进 import 内部的子 Tab（docs/extract）。顶层工作流降为 6 步：
 *     import → outline → facts → content → review → export。
 *
 * 本护栏锁定：
 *   1. tabItems 顶层 6 个 key 齐备且顺序正确（bidAnalysis 不再作为顶层 key）；
 *   2. 步骤号 1~6 与 6 步链一一对应；
 *   3. 「下一步」按钮走 NEXT_TAB 常量，不再硬编码跳步；
 *   4. 合并后两个组件（UploadParseTab + BidAnalysisTab）都还接入在同一顶层 Tab 中；
 *   5. WorkflowTabLabel 保留 swb-tab-title/swb-tab-hint 类名（窄屏隐藏标题自适应依赖）；
 *   6. 内嵌子 Tab 结构存在（importSubTab state + docs/extract 两个子 Tab）。
 */
import { describe, it, expect } from "vitest";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import subTabBarSource from "../components/ImportSubTabBar.tsx?raw";

const src: string = pageSource;
const subSrc: string = subTabBarSource;

describe("SchemeWorkbenchPage 6 步工作流导航护栏（2026-09-23 合并后）", () => {
  it("tabItems 顶层按链路顺序包含全部 6 个 key（bidAnalysis 已收进 import 子 Tab）", () => {
    const keys = ["import", "outline", "facts", "content", "review", "export"].map(
      (k) => `key: "${k}"`
    );
    let pos = -1;
    for (const marker of keys) {
      const idx = src.indexOf(marker, pos + 1);
      expect(idx, `缺少顶层 Tab 或顺序错乱：${marker}`).toBeGreaterThan(pos);
      pos = idx;
    }
  });

  it("顶层 tabItems 不包含 key: \"bidAnalysis\"（合并后已内嵌）", () => {
    // 允许在注释/文档中出现字符串 bidAnalysis，但不能再作为 tab key 出现
    expect(src).not.toContain('key: "bidAnalysis"');
  });

  it("步骤号 1~6 与 6 步链一一对应（无错位）", () => {
    const pairs: Array<[number, string]> = [
      [1, "解析提取"],
      [2, "目录生成"],
      [3, "全局事实"],
      [4, "正文生成"],
      [5, "审核与预检"],
      [6, "导出文档"],
    ];
    for (const [step, title] of pairs) {
      const re = new RegExp(`step=\\{${step}\\}\\s*\\n\\s*title="${title}"`);
      expect(re.test(src), `步骤 ${step} 应为「${title}」`).toBe(true);
    }
  });

  it("facts/outline 的「下一步」走 NEXT_TAB，不再硬编码跳步", () => {
    expect(src).toContain("setActiveTab(NEXT_TAB.facts)");
    expect(src).toContain("setActiveTab(NEXT_TAB.outline)");
    expect(src).not.toContain("下一步：生成目录");
    expect(src).not.toContain("下一步：生成正文");
  });

  it("两个组件（UploadParseTab + BidAnalysisTab）都接入到 import 顶层 Tab 中", () => {
    expect(src).toContain("<UploadParseTab");
    expect(src).toContain("<BidAnalysisTab");
  });

  it("内嵌子 Tab 结构完整：importSubTab state + ImportSubTabBar 接线 + 组件含 docs/extract 两个 key", () => {
    // 页面侧：state 存在，且标签栏已抽成受控组件 ImportSubTabBar 并接线
    expect(src).toContain('const [importSubTab, setImportSubTab]');
    expect(src).toContain("import ImportSubTabBar");
    expect(src).toContain("<ImportSubTabBar");
    // 组件侧：两个子 Tab key 定义在 ImportSubTabBar 内（2026-09-24 T2 抽组件后）
    expect(subSrc).toContain('key: "docs"');
    expect(subSrc).toContain('key: "extract"');
  });

  it("WorkflowTabLabel 保留 swb-tab-title/swb-tab-hint 类名（窄屏隐藏标题自适应依赖）", () => {
    expect(src).toContain('className="swb-tab-title"');
    expect(src).toContain('className="swb-tab-hint"');
  });
});
