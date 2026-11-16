// @vitest-environment jsdom
/**
 * R251 护栏：六大模块「右侧信息窗口」独立垂直滚动条契约。
 *
 * 【背景】
 *   2026-11-16 六模块上方新增了可拖拽分隔面板（SplitPane）后，右窗格滚动暴露
 *   四类真问题：
 *     P1 硬编码高度：目录树 / 正文预览写死 `maxHeight: calc(100vh - 420px)`，
 *        隐含「上半区固定 420px」假设 —— 上半区现已可拖拽（目录生成 120~720px、
 *        正文生成 180~720px）⇒ 上半区拖高后下半区被裁切，底部内容看不全。
 *     P2 原生滚动条泄漏：多处详情区用内联 `overflow:"auto"` 而未挂统一契约类，
 *        出现浏览器默认粗滚动条（非 6px 圆角半透明）。
 *     P3 嵌套双滚动条：解析提取外层滚动容器又包住内部 SplitPane 的两个滚动容器
 *        ⇒ 三段滚动条叠加。
 *     P4 规格不符：既有滚动条 8px 槽（视觉滑块仅 4px），非需求要求的 6px 圆角。
 *
 * 【本护栏锁定】
 *   A 组 · 全局滚动契约（index.css 源码级）
 *   B 组 · 六模块右窗格接线（模块源码级）
 *   C 组 · 行为实证（jsdom 渲染真实组件，验证渲染出的 DOM 属性）
 *   D 组 · 边界（Tab 切换保滚动位置 / 零硬编码高度）
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import { App as AntdApp } from "antd";
// ⚠️ index.css 不能用 `?raw` / `import.meta.glob({as:"raw"})` 导入：vitest 默认
//    css:false，CSS 文件一律被桩成空串（实测为 ""）⇒ 只能用 node fs 直读磁盘。
//    node 类型见 src/types/node-runtime.d.ts（前端仓未装 @types/node）。
import fs from "node:fs";
import path from "node:path";

import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import importTabSource from "../pages/schemeWorkbench/tabs/ImportTab.tsx?raw";
import outlineTabSource from "../pages/schemeWorkbench/tabs/OutlineTab.tsx?raw";
import factsTabSource from "../pages/schemeWorkbench/tabs/FactsTab.tsx?raw";
import reviewTabSource from "../pages/schemeWorkbench/tabs/ReviewTab.tsx?raw";
import exportTabSource from "../pages/schemeWorkbench/tabs/ExportTab.tsx?raw";
import uploadParseSource from "../components/UploadParseTab.tsx?raw";
import contentGenSource from "../components/ContentGenerationTab.tsx?raw";
import parseCatSource from "../components/ParseResultCategoryPanel.tsx?raw";
import sectionCardSource from "../components/SectionContentCard.tsx?raw";
import factsExtractProgressCardSource from "../pages/schemeWorkbench/FactsExtractProgressCard.tsx?raw";
import generationProgressCardSource from "../pages/schemeWorkbench/GenerationProgressCard.tsx?raw";
import outlineGateListSource from "../pages/schemeWorkbench/OutlineGateList.tsx?raw";
import sectionLogListSource from "../pages/schemeWorkbench/SectionLogList.tsx?raw";
import complianceRepairDrawerSource from "../pages/schemeWorkbench/ComplianceRepairDrawer.tsx?raw";
import workbenchModalsSource from "../pages/schemeWorkbench/WorkbenchModals.tsx?raw";
import autoFixModalSource from "../components/review/AutoFixModal.tsx?raw";
import batchFixModalSource from "../components/review/BatchFixModal.tsx?raw";
import projectDetailPageSource from "../pages/ProjectDetailPage.tsx?raw";
import aiConfigPageSource from "../pages/AIConfigPage.tsx?raw";
import SectionContentCard from "../components/SectionContentCard";
import ParseResultCategoryPanel from "../components/ParseResultCategoryPanel";

/**
 * 直读磁盘上的 index.css 源码。
 * ⚠️ 不能用 CSS 导入（?raw / glob）拿源码：vitest 默认 css:false，一律桩空串。
 *    路径按候选逐个探测，兼容「从 frontend/ 启动」与「从仓库根启动」两种 cwd。
 */
function readFrontendFile(rel: string): string {
  const candidates = [
    path.resolve(process.cwd(), "src", rel),
    path.resolve(process.cwd(), "frontend", "src", rel),
    path.resolve(process.cwd(), "..", "src", rel),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return fs.readFileSync(p, "utf8");
  }
  throw new Error(`未找到前端源文件：${rel}（已尝试 ${candidates.join(" / ")}）`);
}

const cssSource: string = readFrontendFile("index.css");

/** 硬编码高度的「样式值」形态 —— 注释里可以提及该常量，但不得再作为样式值使用。 */
const HARDCODED_MAX_HEIGHT = 'maxHeight: "calc(100vh - 420px)"';

// ===== jsdom 环境补齐（与既有组件测试同口径）=====
type Globals = { ResizeObserver?: unknown };
type Win = { matchMedia?: unknown };
const g = globalThis as unknown as Globals;
if (!g.ResizeObserver) {
  g.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
const w = window as unknown as Win;
if (!w.matchMedia) {
  w.matchMedia = (query: string) => ({
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

afterEach(() => {
  cleanup();
});

/** 统计 className 属性中出现该 class token 的次数（token 可与其它类共存）。 */
function countClassTokens(src: string, token: string): number {
  const re = /className="([^"]*)"/g;
  let n = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    if (m[1].split(/\s+/).includes(token)) n += 1;
  }
  return n;
}

/** 该 className 属性是否出现（含与其它类共存的情形）。 */
function hasClassToken(src: string, token: string): boolean {
  return countClassTokens(src, token) > 0;
}

/**
 * 拆出 CSS 规则表（selector 组 → body）。
 * ⚠️ 必须先剥离 CSS 注释：否则「注释 + 选择器组」的 selector 会被捕获成
 *   「注释文本 + 选择器」，导致按精确选择器匹配时首个选择器失配。
 */
function cssRules(src: string): Array<{ selector: string; body: string }> {
  const out: Array<{ selector: string; body: string }> = [];
  const clean = src.replace(/\/\*[\s\S]*?\*\//g, "");
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(clean)) !== null) {
    out.push({ selector: m[1].trim(), body: m[2] });
  }
  return out;
}

/** 取所有「选择器组中精确含该选择器」的规则体，拼成一段文本供断言。 */
function ruleBodiesFor(needle: string): string {
  return cssRules(cssSource)
    .filter((r) => r.selector.split(",").some((s) => s.trim() === needle))
    .map((r) => r.body)
    .join("\n");
}

/** 取某个 @media 查询块的内容（按大括号配对）。 */
function mediaBlock(query: string): string {
  const idx = cssSource.indexOf(query);
  if (idx < 0) return "";
  const open = cssSource.indexOf("{", idx);
  if (open < 0) return "";
  let depth = 0;
  for (let i = open; i < cssSource.length; i += 1) {
    const ch = cssSource[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return cssSource.slice(open + 1, i);
    }
  }
  return "";
}

// ============================================================================
// A 组 · 全局滚动契约
// ============================================================================
describe("A 组 · 右窗格滚动契约（index.css）", () => {
  it("A1. .swb-pane-scroll 与 .scroll-area 共用同一条契约（纵向滚动 + 穿透隔离 + 槽位稳定）", () => {
    const contract = ruleBodiesFor(".swb-pane-scroll");
    expect(contract, "index.css 必须存在 .swb-pane-scroll 滚动契约类").toContain("overflow-y: auto");
    expect(contract, "滚动穿透防范：滚到边界止于本容器").toContain("overscroll-behavior: contain");
    expect(contract, "滚动条占位不闪烁").toContain("scrollbar-gutter: stable");
    // .scroll-area 必须与新类同源（同一份契约，不得两套规格漂移）
    expect(ruleBodiesFor(".scroll-area")).toContain("overscroll-behavior: contain");
  });

  it("A2. Webkit 滚动条 6px 宽（需求：6px 圆角半透明）", () => {
    const bar = ruleBodiesFor(".swb-pane-scroll::-webkit-scrollbar");
    expect(bar).toContain("width: 6px");
    expect(ruleBodiesFor(".scroll-area::-webkit-scrollbar")).toContain("width: 6px");
  });

  it("A3. 滑块圆角 + 三档透明度（默认半透明 / 悬停加深 / 按下最深）", () => {
    expect(ruleBodiesFor(".swb-pane-scroll::-webkit-scrollbar-thumb")).toContain("border-radius: 4px");
    expect(ruleBodiesFor(".swb-pane-scroll::-webkit-scrollbar-thumb")).toContain("rgba(144, 147, 153, 0.3);");
    expect(ruleBodiesFor(".swb-pane-scroll::-webkit-scrollbar-thumb:hover")).toContain("rgba(144, 147, 153, 0.5);");
    expect(ruleBodiesFor(".swb-pane-scroll::-webkit-scrollbar-thumb:active")).toContain("0.75");
  });

  it("A4. 长表格表头吸顶（position: sticky; top: 0）", () => {
    const sticky = ruleBodiesFor(".swb-pane-scroll .ant-table-thead > tr > th");
    expect(sticky).toContain("position: sticky");
    expect(sticky).toContain("top: 0");
  });

  it("A5. 暗色主题下滚动条切换为深色系", () => {
    expect(cssSource).toContain('[data-bp-mode="dark"] .swb-pane-scroll');
  });

  it("A6. 窄屏（<1024px）堆叠后首窗格限高 42vh，必须自带滚动否则内容被裁", () => {
    const block = mediaBlock("@media (max-width: 1024px)");
    expect(block, "窄屏媒体查询必须存在").toBeTruthy();
    expect(block).toContain(".swb-split > .swb-split-first");
    expect(block).toContain("overflow-y: auto");
  });

  it("A7. 滚动容器内的 Tree 行保留长列表渲染隔离（content-visibility）", () => {
    expect(ruleBodiesFor(".swb-pane-scroll .ant-tree-treenode")).toContain("content-visibility: auto");
  });
});

// ============================================================================
// B 组 · 六模块右窗格接线
// ============================================================================
describe("B 组 · 六模块右窗格接线", () => {
  it("B1. 解析提取：上传区与信息显示窗口两窗格均挂滚动契约类", () => {
    expect(countClassTokens(uploadParseSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });

  it("B2. 解析提取：Tab 外层不再自带滚动容器（消除嵌套双滚动条）", () => {
    expect(
      hasClassToken(importTabSource, "scroll-area"),
      "外层不得再包滚动容器，否则与内部 SplitPane 形成三重滚动",
    ).toBe(false);
  });

  it("B3. 解析提取：详情区（分类 / 解析项 / 原文对照）全部挂契约类，杜绝原生滚动条", () => {
    expect(countClassTokens(parseCatSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(4);
  });

  it("B4. 目录生成：顶部信息区与目录树编辑区均挂契约类", () => {
    expect(countClassTokens(outlineTabSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });

  it("B5. 目录生成：零硬编码高度（可拖拽上半区时下半区不得被裁）", () => {
    expect(outlineTabSource).not.toContain(HARDCODED_MAX_HEIGHT);
  });

  it("B6. 全局事实：顶部信息区与分组事实列表均挂契约类", () => {
    expect(countClassTokens(factsTabSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });

  it("B7. 正文生成：配置区与正文编辑区均挂契约类", () => {
    expect(countClassTokens(contentGenSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });

  it("B8. 正文生成：章节正文预览零硬编码高度", () => {
    expect(hasClassToken(sectionCardSource, "swb-pane-scroll")).toBe(true);
    expect(sectionCardSource).not.toContain(HARDCODED_MAX_HEIGHT);
  });

  it("B9. 审核预检：顶部总检与分项检查均挂契约类", () => {
    expect(countClassTokens(reviewTabSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });

  it("B10. 导出文档：格式预设区与导出配置区均挂契约类", () => {
    expect(countClassTokens(exportTabSource, "swb-pane-scroll")).toBeGreaterThanOrEqual(2);
  });
});

// ============================================================================
// D 组 · 边界（Tab 切换保持滚动位置 / 全局无硬编码高度）
// ============================================================================
describe("D 组 · 边界处理", () => {
  it("D1. Tab 切换保持右侧滚动位置：不得销毁已渲染面板", () => {
    // antd Tabs 默认保留已渲染 pane（scrollTop 天然保留）；
    // 一旦引入 destroyInactiveTabPane，面板被卸载 ⇒ 滚动位置丢失。
    expect(pageSource).not.toContain("destroyInactiveTabPane");
    expect(pageSource).toContain('className="swb-tabs-flex"');
  });

  it("D2. 六模块源码内零残留 calc(100vh - 420px) 硬编码高度", () => {
    const all =
      importTabSource +
      outlineTabSource +
      factsTabSource +
      reviewTabSource +
      exportTabSource +
      uploadParseSource +
      contentGenSource +
      sectionCardSource;
    expect(all).not.toContain(HARDCODED_MAX_HEIGHT);
  });
});

// ============================================================================
// C 组 · 行为实证（jsdom 渲染真实组件，断言渲染出的 DOM 属性）
// ============================================================================
describe("C 组 · 行为实证", () => {
  it("C1. 章节正文预览：渲染出契约类容器，且不再使用写死的视口偏移高度", () => {
    const { container } = render(
      <AntdApp>
        <SectionContentCard
          section={{
            key: "s1",
            title: "第一章 工程概况",
            level: 1,
            status: "generated",
            word_count: 1200,
            word_budget: 1500,
            content: "## 工程概况\n\n这是正文。",
          }}
          treeEmpty={false}
          isEditing={false}
          saving={false}
          generating={false}
          draftKey="draft:r251-s1"
          statusColor={{ generated: "green", empty: "default" }}
          statusText={{ generated: "已生成", empty: "未生成" }}
          onEditStart={() => {}}
          onSave={() => {}}
          onCancel={() => {}}
          onDirtyChange={() => {}}
          onRegenerate={() => {}}
          onChartFixed={() => {}}
        />
      </AntdApp>,
    );

    const panes = Array.from(container.querySelectorAll(".swb-pane-scroll"));
    expect(panes.length, "正文预览区应挂滚动契约类").toBeGreaterThan(0);
    for (const el of panes) {
      expect(el.getAttribute("style") || "").not.toContain("calc(100vh - 420px)");
    }
  });

  it("C2. 解析结果详情区（detail-only）：详情容器挂契约类而非裸 overflow:auto", () => {
    const { container } = render(
      <AntdApp>
        <ParseResultCategoryPanel
          variant="detail-only"
          groups={[
            {
              key: "project_info",
              label: "项目级基本信息",
              items: [
                { item_id: "projectBasicInfo", label: "项目级基本信息", required: 1, output_type: "json" },
              ],
            },
          ]}
          items={[{ item_id: "projectBasicInfo", status: "success", content: '{"a":1}' }]}
          // ✅ 传入 selectedItem 才会渲染详情区容器（.parse-cat-detail-body），
          //    否则走空态分支 —— 空态下不存在详情容器，断言无意义。
          selectedItem={{
            item_id: "projectBasicInfo",
            label: "项目级基本信息",
            required: 1,
            output_type: "json",
          }}
          loading={false}
          error={null}
          sharedState={{ expanded: {}, activeKey: null }}
          onSelectItem={() => {}}
        />
      </AntdApp>,
    );

    const detail = container.querySelector(".parse-cat-detail-body");
    expect(detail, "应渲染出详情区容器").toBeTruthy();
    expect(detail!.className).toContain("swb-pane-scroll");
  });
});

// ============================================================================
// E 组 · 残余内滚动容器统一规格（浮层 / 进度卡 / 清单 / 弹窗 / 抽屉）
// ============================================================================

/** 期望挂上滚动契约类的「内滚动容器」清单（文件 → 至少多少个契约类容器）。 */
const NATIVE_SCROLL_TARGETS: Array<[string, string, number]> = [
  ["FactsExtractProgressCard", factsExtractProgressCardSource, 1],
  ["GenerationProgressCard", generationProgressCardSource, 2],
  ["OutlineGateList", outlineGateListSource, 1],
  ["SectionLogList", sectionLogListSource, 1],
  ["ComplianceRepairDrawer", complianceRepairDrawerSource, 2],
  ["WorkbenchModals", workbenchModalsSource, 4],
  ["AutoFixModal", autoFixModalSource, 3],
  ["BatchFixModal", batchFixModalSource, 4],
  ["ProjectDetailPage", projectDetailPageSource, 1],
  ["AIConfigPage", aiConfigPageSource, 1],
];

describe("E 组 · 残余内滚动容器统一规格", () => {
  it("E1. 进度卡 / 清单 / 弹窗 / 抽屉的内滚动容器全部挂契约类（杜绝浏览器默认粗滚动条）", () => {
    for (const [name, src, min] of NATIVE_SCROLL_TARGETS) {
      expect(
        countClassTokens(src, "swb-pane-scroll"),
        `${name}：内滚动容器未挂 .swb-pane-scroll 契约类（会出现浏览器默认粗滚动条）`,
      ).toBeGreaterThanOrEqual(min);
    }
  });

  it("E2. 页面级主区域滚动条与契约同规格（6px）", () => {
    expect(ruleBodiesFor(".ant-layout-content::-webkit-scrollbar")).toContain("width: 6px");
  });

  it("E3. antd 内部滚动容器（Modal / Drawer / Collapse body）纳入 6px 规格", () => {
    expect(ruleBodiesFor(".ant-modal-body")).toContain("scrollbar-width: thin");
    expect(ruleBodiesFor(".ant-modal-body::-webkit-scrollbar")).toContain("width: 6px");
  });
});
