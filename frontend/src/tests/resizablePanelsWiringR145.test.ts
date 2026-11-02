/**
 * R145（2026-10-30）可拖拽面板 · 真实页面接线护栏
 *
 * 为什么需要本文件：
 *  `resizablePanelsR145.test.tsx` 用 mock 面板证明了 SplitPane / ResizeHandle 的
 *  拖拽 / 持久化 / 防坍塌 / 降级 / 重置语义，但它**证明不了「用户界面上那 5 条手柄
 *  真的各自挂上了」** —— 渲染的是桩，不是 Layout 与 SchemeWorkbenchPage。
 *  本轮（逐行静态审计）把 5 个区域的接线事实固化为可执行断言：
 *
 *   region 1  Layout.tsx                侧边栏 ↔ 主工作区（宽度 200–400px）
 *   region 2  SchemeWorkbenchPage.tsx   上半监视区 ↔ 下半工作区（高度）
 *   region 3  SchemeWorkbenchPage.tsx   生成进度卡 ↔ 后台任务列表（宽度）
 *   region 4  SchemeWorkbenchPage.tsx   左侧面板 ↔ 右侧 Tab 内容（宽度）
 *   region 5  Layout.tsx                侧栏导航列表 ↔ 底部后台任务（高度）
 *
 * 「独立可调整」的关键判据 = **5 个 localStorage 键互不相同**（同键即两块面板互相踩）；
 * 窄屏降级（<1024px）与「重置布局」广播也必须真实接到每一块区域。
 *
 * 判据范式：Vite `import.meta.glob(..., { query: "?raw" })` 读源码文本 ——
 * 本仓未装 @types/node（tests 也在 tsc 范围内），不引 node:fs / __dirname。
 */
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const CSS: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../index.css", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ),
);

const LAYOUT = RAW["components/Layout.tsx"] ?? "";
const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"] ?? "";
const SPLIT_PANE = RAW["components/resizable/SplitPane.tsx"] ?? "";
const RESIZE_RUNTIME = RAW["components/resizable/resizableLayout.tsx"] ?? "";

// 行尾免疫（R161 认领 R159 遗留孤儿失败）：index.css 曾被 stash/autocrlf 整文件
// 重写为 CRLF（HEAD blob 为 LF），本文件所有跨行锚因此失配 —— 样式本体与机制
// 不变量完好，失败纯属行尾字节噪声；autocrlf=true 下把 CSS 改回 LF 不可持久
// （下次 checkout 仍会翻回 CRLF），故在读取层统一归一：断言锁机制形态，不锁行尾字节。
const normEol = (s: string) => s.replace(/\r\n/g, "\n");
const STYLES = normEol(Object.values(CSS).join("\n"));

/** 防「锚点静默失效」：源码必须真的被读到了（本仓既有教训：判据失灵比判据错更危险） */
describe("源码读取自检（防空转）", () => {
  it("Layout / SchemeWorkbenchPage / 两个 resizable 模块 / index.css 均在册", () => {
    expect(LAYOUT.length).toBeGreaterThan(1000);
    expect(PAGE.length).toBeGreaterThan(1000);
    expect(SPLIT_PANE.length).toBeGreaterThan(1000);
    expect(RESIZE_RUNTIME.length).toBeGreaterThan(500);
    expect(STYLES.length).toBeGreaterThan(1000);
  });
});

describe("五个区域各自挂上了手柄（region 1/5 在 Layout，region 2/3/4 在工作台）", () => {
  it("region 1：侧边栏 ↔ 主区间有 ResizeHandle，且由 sidebarWidth 驱动 Sidebar", () => {
    expect(LAYOUT).toContain("<ResizeHandle");
    expect(LAYOUT).toContain('testId="sidebar-resize-handle"');
    expect(LAYOUT).toContain('ariaLabel="调整侧边栏宽度"');
    // 手柄增量必须 clamp 在 min/max 内（防坍塌），且宽度真的传给 Sidebar
    expect(LAYOUT).toContain("Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, w + dx))");
    expect(LAYOUT).toContain("width={sidebarWidth}");
  });

  it("region 1 的 px 护栏为 200–400、默认 260", () => {
    expect(LAYOUT).toContain("const SIDEBAR_WIDTH_MIN = 200;");
    expect(LAYOUT).toContain("const SIDEBAR_WIDTH_MAX = 400;");
    expect(LAYOUT).toContain("const SIDEBAR_WIDTH_DEFAULT = 260;");
  });

  it("region 5：侧栏导航列表 ↔ 底部后台任务是 SplitPane（仅展开态）", () => {
    expect(LAYOUT).toContain('storageKey="sidebar-tasks"');
    // 折叠态保持原 flex 结构、不挂手柄（窄空间不可拖）
    const split = LAYOUT.indexOf('storageKey="sidebar-tasks"');
    const collapsedBranch = LAYOUT.lastIndexOf("collapsed ? (", split);
    expect(collapsedBranch).toBeGreaterThan(-1);
    expect(collapsedBranch).toBeLessThan(split);
  });

  it("region 2/3/4：工作台 3 个页面级 SplitPane + R146 各 Tab 内 5 个分栏", () => {
    // R146（2026-10-30）把「解析提取/目录生成/全局事实/审核预检/导出文档」的
    // 左右、上下分栏也收进 SplitPane，总数由 3 变为 8（每块 storageKey 独立）。
    expect((PAGE.match(/<SplitPane\b/g) || []).length).toBe(8);
  });

  it("三个区域的语义锚点齐备（默认比例 / px 护栏 / aria 文案）", () => {
    expect(PAGE).toContain('storageKey="wb-monitor"');
    expect(PAGE).toContain('handleAriaLabel="调整生成监视区与下方工作区的高度"');
    expect(PAGE).toContain('storageKey="wb-progress"');
    expect(PAGE).toContain('handleAriaLabel="调整生成进度卡与后台任务列表的宽度"');
    expect(PAGE).toContain('storageKey="wb-tree"');
    expect(PAGE).toContain('handleAriaLabel="调整左侧面板与工作区内容的宽度"');
  });
});

describe("独立可调整：布局持久化键互不相同", () => {
  it("两文件出现的布局键去重后恰为 10 个（R145 五区 + R146 五个 Tab 分栏）", () => {
    const raw = [
      ...(LAYOUT.match(/storageKey="([^"]+)"/g) || []),
      ...(PAGE.match(/storageKey="([^"]+)"/g) || []),
      ...(LAYOUT.match(/RESIZE_STORAGE_PREFIX \+ "([^"]+)"/g) || []),
    ];
    const keys = raw.map((s) =>
      s
        .replace(/^storageKey="/, "")
        .replace(/RESIZE_STORAGE_PREFIX \+ "/, "")
        .replace(/"$/, ""),
    );
    // 去重后 = 8 个页面键 + 2 个 Layout 键；同键即两块面板互相踩
    expect(new Set(keys).size).toBe(10);
  });

  it("R146：五个 Tab 分栏各自独立持久化（键不可合并，且都可双击恢复默认）", () => {
    for (const k of [
      'storageKey="wb-tab-import"',
      'storageKey="wb-tab-outline"',
      'storageKey="wb-tab-facts"',
      'storageKey="wb-tab-review"',
      'storageKey="wb-tab-export"',
    ]) {
      expect(PAGE).toContain(k);
      // 每个 Tab 分栏都必须能被双击恢复默认（aria 文案里带上提示）
      expect(PAGE).toContain("双击恢复默认");
    }
  });

  it("前缀单一事实源来自 resizableLayout，页面不自带第二份字面量", () => {
    expect(RESIZE_RUNTIME).toContain('export const RESIZE_STORAGE_PREFIX = "bp-rp:";');
    expect(LAYOUT).toContain("RESIZE_STORAGE_PREFIX");
    // 不允许再出现裸 "bp-rp:" 副本（「同一判据两份取值」的老毛病）
    expect(LAYOUT).not.toContain('"bp-rp:"');
    expect(PAGE).not.toContain('"bp-rp:"');
  });
});

describe("窄屏降级（<1024px）：工作台退回原单列、侧栏手柄禁用", () => {
  it("工作台阈值取自单一常量 RESIZE_NARROW_THRESHOLD", () => {
    expect(PAGE).toContain(
      'import { RESIZE_NARROW_THRESHOLD } from "../components/resizable/resizableLayout"',
    );
    expect(PAGE).toContain("const narrowWorkbench = viewportWidth < RESIZE_NARROW_THRESHOLD;");
    expect(RESIZE_RUNTIME).toContain("export const RESIZE_NARROW_THRESHOLD = 1024;");
  });

  it("窄屏分支结构仍在（分支体内渲染进度卡 + flex 主体，不渲染 SplitPane）", () => {
    const start = PAGE.indexOf("{narrowWorkbench ? (");
    expect(start).toBeGreaterThan(-1);
    const win = PAGE.slice(start, PAGE.indexOf("<SplitPane", start));
    expect(win).toContain("GenerationProgressCard");
    expect(win).not.toContain("<SplitPane");
  });

  it("region 1/5 手柄在抽屉 / 折叠 / 窄屏下禁用", () => {
    expect(LAYOUT).toContain(
      "const sidebarResizeDisabled = drawerMode || collapsed || width < RESIZE_NARROW_THRESHOLD;",
    );
    expect(LAYOUT).toContain("disabled={sidebarResizeDisabled}");
  });

  it("import Tab 在工作区内退化为单栏（无目录树可切）", () => {
    const treeKey = PAGE.indexOf('storageKey="wb-tree"');
    expect(PAGE.slice(treeKey - 400, treeKey + 400)).toContain('activeTab === "import"');
  });
});

describe("「重置布局」真的接到每一块区域", () => {
  it("顶栏按钮调用 useResizableLayoutReset（清全部键 + 广播信号）", () => {
    expect(LAYOUT).toContain("const resetLayout = useResizableLayoutReset();");
    expect(LAYOUT).toContain("onClick={resetLayout}");
    expect(LAYOUT).toContain('aria-label="重置布局"');
  });

  it("清空逻辑扫本应用全部 bp-rp: 键（不止某一个区域）", () => {
    expect(RESIZE_RUNTIME).toContain("export function clearAllResizableLayouts(): void {");
    expect(RESIZE_RUNTIME).toContain("k.startsWith(RESIZE_STORAGE_PREFIX)");
  });

  it("SplitPane 自重置：监听 resetSignal → 清自己的键 + 回默认比例", () => {
    expect(SPLIT_PANE).toContain("const resetSignal = useResizableResetSignal();");
    expect(SPLIT_PANE).toContain("clearStoredFraction(storageKey);");
    expect(SPLIT_PANE).toContain("setRatio(defaultRatio);");
  });

  it("region 1（非 SplitPane）有等价的重置效果器，且初始挂载不误重置", () => {
    expect(LAYOUT).toContain("const resetSignal = useResizableResetSignal();");
    expect(LAYOUT).toContain("if (resetSignal === 0) return;");
    expect(LAYOUT).toContain("setSidebarWidth(SIDEBAR_WIDTH_DEFAULT);");
  });

  it("Provider 只包一层，且包住 MainArea（工作台在其内部才能收到信号）", () => {
    expect((LAYOUT.match(/<ResizableLayoutProvider>/g) || []).length).toBe(1);
    expect((LAYOUT.match(/<\/ResizableLayoutProvider>/g) || []).length).toBe(1);
    const provider = LAYOUT.indexOf("<ResizableLayoutProvider>");
    const mainArea = LAYOUT.indexOf("<MainArea", provider);
    expect(mainArea).toBeGreaterThan(provider);
    expect(LAYOUT.indexOf("</ResizableLayoutProvider>", mainArea)).toBeGreaterThan(mainArea);
  });
});

describe("样式落地（手柄命中区 / 光标 / 拖拽期禁选）", () => {
  it("index.css 有 group / pane / handle 三类规则与轴光标", () => {
    expect(STYLES).toContain(".bp-resize-group");
    expect(STYLES).toContain(".bp-resize-pane");
    expect(STYLES).toContain(".bp-resize-handle.axis-x");
    expect(STYLES).toContain(".bp-resize-handle.axis-y");
    expect(STYLES).toContain("cursor: col-resize;");
    expect(STYLES).toContain("cursor: row-resize;");
  });

  it("拖拽期 body 类禁用文本选中（防选中页面文字 / 图表拖花）", () => {
    expect(STYLES).toContain("body.bp-resize-dragging");
    expect(STYLES).toContain("user-select: none !important;");
  });

  it("SplitPane 用的 body 类名与 CSS 同源（不出现第二份字面量拼写）", () => {
    expect(SPLIT_PANE).toContain('const BODY_DRAG_CLASS = "bp-resize-dragging";');
    expect(STYLES).toContain("body.bp-resize-dragging {");
  });
});

describe("R146：分栏独立滚动条（6px 细条 + 嵌套滚动隔离）", () => {
  it("index.css 定义 .bp-pane-scroll：6px 细条、隐藏原生按钮", () => {
    // ✅ R150：细条规则与分块 .bp-resize-pane 合并为同一组选择器（单一事实源）
    expect(STYLES).toContain(".bp-pane-scroll,\n.bp-resize-pane {");
    expect(STYLES).toContain(".bp-pane-scroll::-webkit-scrollbar,");
    // 分栏 pane 统一 6px（用户需求「自定义细滚动条 6px」）
    const idx = STYLES.indexOf(".bp-pane-scroll::-webkit-scrollbar,");
    const block = STYLES.slice(idx, idx + 120);
    expect(block).toContain("width: 6px;");
    expect(STYLES).toContain(".bp-pane-scroll::-webkit-scrollbar-button,");
  });

  it(".bp-pane-scroll 与 .scroll-area 都做滚动链隔离（overscroll-behavior: contain）", () => {
    expect(STYLES).toContain("overscroll-behavior: contain;");
    expect(STYLES).toContain(".scroll-area {");
  });

  it("工作台各 Tab 的分栏 pane 都挂了 bp-pane-scroll", () => {
    // 每个 Tab 至少一个独立滚动 pane（解析/目录/事实/正文/审核/导出）
    const count = (PAGE.match(/bp-pane-scroll/g) || []).length;
    expect(count).toBeGreaterThanOrEqual(8);
  });
});

describe("R146：页面级左栏只服务「正文生成 / 全局事实」", () => {
  it("全局目录树仅在 content Tab 渲染（其余 Tab 用自身左栏）", () => {
    expect(PAGE).toContain('{activeTab === "content" && (');
  });

  it("import / review / export 走单栏 workbenchTabsCard 分支", () => {
    expect(PAGE).toContain('activeTab === "import" || activeTab === "review" || activeTab === "export"');
  });
});

describe("嵌套互不干扰", () => {
  it("手柄 pointerdown 阻止冒泡 + 指针捕获（内层拖拽不带动外层比例）", () => {
    expect(SPLIT_PANE).toContain("e.stopPropagation();");
    expect(SPLIT_PANE).toContain("setPointerCapture(e.pointerId)");
  });

  it("拖拽中途卸载仍清 body 类并补发持久化", () => {
    expect(SPLIT_PANE).toContain("removeBodyDragClasses();");
    expect(SPLIT_PANE).toContain("dragEndRef.current?.();");
  });
});

/**
 * ✅ R150（2026-10-30）「每个分块右侧都有滑动条」
 *
 * 缺口：`.bp-resize-pane` 原为 `overflow: hidden` —— 没有内层滚动容器的分块
 * （进度卡、后台任务、单栏内容等）内容一长就被裁掉，右侧永远没有滑动条。
 * 现在分块自身即纵向滚动容器，并统一挂 6px 细条；横向仍裁掉（防拖窄后出横条）。
 */
describe("R150：每个分块自身可纵向滚动（6px 细条 + 只滚纵向）", () => {
  it(".bp-resize-pane 由 overflow:hidden 改为纵向滚动（内容不再被裁掉）", () => {
    // ⚠️ `.bp-resize-pane {` 先出现在共享滚动条组里，取最后一次 = 分块自身的布局块
    const idx = STYLES.lastIndexOf(".bp-resize-pane {");
    const open = STYLES.indexOf("{", idx);
    const block = STYLES.slice(open + 1, STYLES.indexOf("}", open));
    expect(block).toContain("overflow-y: auto;");
    expect(block).toContain("overflow-x: hidden;");
    expect(block).toContain("overscroll-behavior: contain;");
    expect(block, "不允许再回到 overflow: hidden 裁切").not.toContain("overflow: hidden;");
  });

  it("分块滚动条与分栏 pane 同口径：6px + 隐藏原生按钮", () => {
    expect(STYLES).toContain(".bp-resize-pane::-webkit-scrollbar {");
    expect(STYLES).toContain(".bp-resize-pane::-webkit-scrollbar-thumb {");
    expect(STYLES).toContain(".bp-resize-pane::-webkit-scrollbar-button {");
    // 标准属性组里也必须包含分块自身（Firefox / Chrome 121+）
    expect(STYLES).toContain(".bp-pane-scroll,\n.bp-resize-pane {");
    expect(STYLES).toContain("scrollbar-width: thin;");
  });

  it("非分栏块同样有细条：后台任务面板的滚动条挂在 Card body（root 不滚）", () => {
    const gen = RAW["components/resizable/GenTaskListPanel.tsx"] ?? "";
    expect(gen.length).toBeGreaterThan(1000);
    expect(gen).toContain('classNames={{ body: "bp-pane-scroll" }}');
  });
});

/**
 * ✅ R149（2026-10-30）上下滑动条设计 · 真实接线护栏
 *
 * 用户口径四条：①各 Tab 主内容区独立滚动且高度锁死 calc(100vh - 头部)；
 * ②6px 自定义细条、隐藏原生；③overscroll-behavior: contain 隔离滚动链；
 * ④左侧导航与顶部操作栏固定不滚动。
 *
 * 判据选择：这些是**样式与结构契约**，只能从源码锚定（jsdom 不做真实布局，
 * scrollHeight/clientHeight 恒为 0，渲染测试无法证明「有没有双滚动条」）。
 */
describe("R149：上下滑动条（独立滚动 / 6px 细条 / 滚动链隔离 / 头侧栏固定）", () => {
  it("高度链闭合：外壳 100vh 不滚 → .bp-content 锁 calc(100vh - 62px) → Tabs 面板 height:100%", () => {
    // ④ 左侧导航 + 顶部操作栏固定：整个 AntLayout 固定 100vh 且 overflow hidden，
    //    页面永不被滚动，头/侧栏因此天然固定（只有 pane 内部滚）。
    expect(LAYOUT).toContain('<AntLayout style={{ height: "100vh", overflow: "hidden" }}>');
    // ⚠️ `.bp-content {` 在 CSS 中出现多次（另有等价选择器组），必须锚定**含高度声明的那一块**
    const heightIdx = STYLES.indexOf("height: calc(100vh - 62px) !important;");
    expect(heightIdx).toBeGreaterThan(-1);
    const blockStart = STYLES.lastIndexOf(".bp-content {", heightIdx);
    expect(blockStart).toBeGreaterThan(-1);
    const block = STYLES.slice(blockStart, STYLES.indexOf("}", heightIdx));
    expect(block).toContain("overflow: hidden;");
    expect(block).toContain("display: flex;");
    // Tab 面板补齐 100% 高度（.swb-tabs-flex 既有高度链）
    expect(STYLES).toContain("height: 100%;");
  });

  it("Tab 面板自身 overflow:hidden（不成为第二个滚动容器，防双滚动条）", () => {
    const selector =
      ".ant-tabs.swb-tabs-flex > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane:not(.ant-tabs-tabpane-hidden) {";
    // 同一选择器出现两次：先有高度链（flex/height），后有 R149 防双滚（overflow:hidden）
    const idx = STYLES.lastIndexOf(selector);
    expect(idx).toBeGreaterThan(-1);
    expect(STYLES.slice(idx + selector.length, idx + selector.length + 120)).toContain(
      "overflow: hidden;",
    );
  });

  it("6px 自定义细条 + 隐藏原生按钮（WebKit 兜底）；标准属性 thin 不与 12px 口径冲突", () => {
    expect(STYLES).toContain(".bp-pane-scroll,\n.bp-resize-pane {");
    expect(STYLES).toContain("scrollbar-width: thin;");
    const barIdx = STYLES.indexOf(".bp-pane-scroll::-webkit-scrollbar,");
    expect(STYLES.slice(barIdx, barIdx + 140)).toContain("width: 6px;");
    // 原生滚动条按钮（上下箭头）必须隐藏
    expect(STYLES).toContain(".bp-pane-scroll::-webkit-scrollbar-button,");
    // 既有 .scroll-area 12px 口径保持不变（工作台外长列表页不受影响）
    const areaIdx = STYLES.indexOf(".scroll-area {");
    expect(STYLES.slice(areaIdx, areaIdx + 700)).toContain("scrollbar-width: auto;");
  });

  it("③ 滚动链隔离：所有工作台滚动容器都吃 .scroll-area/.bp-pane-scroll 的 contain", () => {
    const areaIdx = STYLES.indexOf(".scroll-area {");
    expect(STYLES.slice(areaIdx, areaIdx + 700)).toContain("overscroll-behavior: contain;");
    const paneIdx = STYLES.indexOf(".bp-pane-scroll,\n.bp-resize-pane {");
    expect(STYLES.slice(paneIdx, paneIdx + 160)).toContain("overscroll-behavior: contain;");
  });

  it("① 六个 Tab 的主内容区滚动容器全部改挂 6px 细条（页面 ≥12 处）", () => {
    // 解析(文档/项目提取) 目录(树/配置) 事实(概览/列表/分类) 正文(控制台/工作台/预览)
    // 审核(清单/详情) 导出(配置/预览) + 监视区 + 章节卡片 —— 均要求细条。
    expect((PAGE.match(/bp-pane-scroll/g) || []).length).toBeGreaterThanOrEqual(12);
    for (const key of [
      "components/UploadParseTab.tsx",
      "components/BidAnalysisTab.tsx",
      "components/ContentGenerationTab.tsx",
      "components/SectionContentCard.tsx",
    ]) {
      const src = RAW[key] ?? "";
      expect(src.length, `应能读到 ${key}`).toBeGreaterThan(1000);
      expect(src, `${key} 的滚动容器应挂 bp-pane-scroll`).toContain("bp-pane-scroll");
    }
  });
});

/**
 * ✅ R151（2026-10-30）目录树空白回归护栏
 *
 * 事故：index.css 曾用全局覆写把**所有** antd Tree 改成
 *   `.ant-tree{height:100%;overflow:hidden}` +
 *   `.ant-tree-list-holder{height:100%;position:relative;overflow:hidden}` +
 *   `.ant-tree-list-holder-inner{position:absolute;inset:0}`。
 * 父容器高度为确定值（flex 撑满 / 显式 height）时能工作；一旦父容器是 auto 高度
 * （`minHeight/maxHeight`、Card body、Drawer、Tooltip 预览），`height:100%` 解析为
 * auto，而 holder-inner 又被绝对定位抽离文档流 ⇒ 树 computed 高度恒为 0，
 * 六个 Tab 的目录树集体空白。
 *
 * 正确契约：树按内容自然高度渲染（非虚拟模式），溢出交给外层
 * `.scroll-area` / `.bp-pane-scroll` / `.bp-resize-pane`（它们都已可纵向滚动）。
 * 本 describe 固化「覆写不得回流」，防止后续有人为了「树能滚」再次加上它。
 */
describe("R151：目录树不得被全局高度覆写压成 0", () => {
  it("index.css 不再给 .ant-tree 强制 height:100% / overflow:hidden", () => {
    // 注释里提到 `.ant-tree` 是允许的，只禁止**真正生效的声明块**
    expect(STYLES).not.toMatch(/\.ant-tree\s*\{[^}]*height:\s*100%/);
    expect(STYLES).not.toMatch(/\.ant-tree\s*>\s*\.ant-tree-list\s*\{[^}]*height:\s*100%/);
  });

  it("holder / holder-inner 不再被绝对定位抽离文档流（这是高度 0 的直接来源）", () => {
    expect(STYLES).not.toMatch(/\.ant-tree-list-holder\s*\{[^}]*position:\s*relative/);
    expect(STYLES).not.toMatch(/\.ant-tree-list-holder-inner\s*\{[^}]*position:\s*absolute/);
    expect(STYLES).not.toContain("inset: 0;\n  overflow-y: auto !important;");
  });

  it("树不再有内部滚动容器：滚动职责归外层 pane（滚动链单一事实源）", () => {
    // 若树内部仍写 overflow-y，说明覆写又回来了
    expect(STYLES).not.toMatch(/\.ant-tree(?:[-\w>.\s]*)?\{[^}]*overflow-y:\s*(?:auto|scroll)/);
    // 正向契约：外层仍有可纵向滚动 + 滚动链隔离的容器
    expect(STYLES).toContain(".scroll-area {");
    expect(STYLES).toContain("overscroll-behavior: contain;");
  });

  it("工作台的目录树仍由 .scroll-area/.bp-pane-scroll 外层承担溢出（不改页面补丁）", () => {
    // SchemeTreePanel 的滚动容器锚点（若改为树内部滚动，这里会失配）
    expect(PAGE).toContain('<div ref={scrollRef} className="scroll-area bp-pane-scroll"');
    // 目录生成 Tab 的编辑树外层同样是可滚动 pane
    const idx = PAGE.indexOf("ref={editTreeScrollRef}");
    expect(idx).toBeGreaterThan(-1);
    expect(PAGE.slice(idx, idx + 200)).toContain('className="scroll-area bp-pane-scroll"');
  });
});
