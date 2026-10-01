// @vitest-environment jsdom
/**
 * 方案工作台（SchemeWorkbenchPage）· 组件级测试
 * ==============================================
 * 为什么补这个文件：
 *   工作台是 9400 行 / 418KB 的单体巨组件，此前**没有任何组件级测试**
 *   （只有 outlineTreeLogic / outlineTab 等纯逻辑与子组件测试）。这意味着
 *   对它做任何重构（巨组件拆分、轮询统一、目录树虚拟化、树回调稳定化）都
 *   没有回归网——改错了只能靠人肉点页面发现。
 *   本文件先把「目录树编辑」这条最容易被重构打断的主干行为钉住：
 *     渲染冒烟 / 新增子章节 / 新增同级 / 重命名 / 上移下移 / 删除 / 保存目录，
 *   然后再动重构。
 *
 * 夹具策略：
 *   ../api 用「自动 Proxy 假实现」整体替换——任意 xxxApi.yyy(...) 都返回
 *   Promise.resolve(默认响应)，并把调用参数按 "模块.方法" 记录到 apiCalls，
 *   既避免 jsdom 下真实网络，又能精确断言「点了按钮到底发了什么请求」。
 *   ../hooks/useSchemeLiveTask 替换为不轮询的静态对象（否则后台轮询会持续
 *   触发 act 警告与状态更新）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import React from "react";
import { App as AntdApp } from "antd";
import SchemeWorkbenchPage from "../pages/SchemeWorkbenchPage";

// ---------- ../api 自动假实现 ----------
// ⚠️ vi.mock 会被提升到文件顶部，工厂里不能引用普通顶层变量（TDZ）。
//    所有 mock 状态必须经 vi.hoisted 创建。
const apiMock = vi.hoisted(() => {
  const calls: Record<string, any[][]> = {};
  const defaults: Record<string, any> = {};
  const cache = new Map<string, any>();

  /** 可调用节点：既能当方法调用（记录参数 + 返回默认响应），也能继续取子属性 */
  function makeCallable(path: string[]): any {
    const key = path.join(".");
    const cached = cache.get(key);
    if (cached) return cached;
    const fn: any = (...args: any[]) => {
      (calls[key] ||= []).push(args);
      const d = defaults[key];
      return Promise.resolve(d !== undefined ? d : { data: {} });
    };
    const proxy = new Proxy(fn, {
      get(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        if (prop === "__esModule") return true;
        if (prop === "then") return undefined;
        return makeCallable([...path, prop]);
      },
    });
    cache.set(key, proxy);
    return proxy;
  }

  /**
   * 模块命名空间：必须是「对象型」Proxy —— vitest 会校验 vi.mock 工厂返回对象，
   * 直接返回函数型 Proxy 会报 "is not returning an object"。
   */
  function makeNamespace(): any {
    return new Proxy({} as any, {
      get(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        if (prop === "__esModule") return true;
        if (prop === "then") return undefined;
        return makeCallable([prop]);
      },
      // vitest 会用自己的代理包裹工厂返回值，并校验「导出名是否定义」；
      // 纯动态 Proxy 必须让 has / getOwnPropertyDescriptor 也返回真，否则报
      // ‘No "xxxApi" export is defined on the mock’。
      has(_t, prop: any) {
        return typeof prop === "string" ? true : false;
      },
      getOwnPropertyDescriptor(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        return {
          configurable: true,
          enumerable: true,
          value: makeCallable([prop]),
        };
      },
      ownKeys() {
        return [];
      },
    });
  }

  return { calls, defaults, root: makeNamespace() };
});

vi.mock("../api", () => apiMock.root);

// 测试内使用的别名（工厂只依赖 vi.hoisted 的 apiMock，不受提升影响）
const apiCalls = apiMock.calls;
const apiDefaults = apiMock.defaults;

// ---------- 后台任务轮询：替换为静态对象（不产生定时器） ----------
vi.mock("../hooks/useSchemeLiveTask", () => ({
  useSchemeLiveTask: () => ({
    id: null,
    status: "idle",
    progress: 0,
    message: "",
    task_type: "",
    control: vi.fn(),
    refresh: vi.fn(),
    stop: vi.fn(),
  }),
}));

// jsdom 缺 matchMedia / ResizeObserver / getComputedStyle(pseudo)，antd 会用到
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

/** 目录树 fixture：两个一级章节（保证「下移」按钮可用） */
const TREE = [
  { id: "sec-1", title: "工程概况", level: 1, status: "empty", word_count: 0, word_budget: 1500, children: [] },
  { id: "sec-2", title: "施工计划", level: 1, status: "empty", word_count: 0, word_budget: 1500, children: [] },
];

function setupDefaults() {
  apiDefaults["sectionsApi.list"] = {
    data: { scheme: { id: "s1", project_id: "p1", name: "测试方案" }, tree: TREE },
  };
  apiDefaults["schemesApi.get"] = { data: { config_json: "{}" } };
  apiDefaults["factsApi.list"] = { data: { groups: [], stats: {} } };
  apiDefaults["factsApi.listDocuments"] = { data: { documents: [] } };
  apiDefaults["complianceApi.expertItems"] = { data: { items: [] } };
  apiDefaults["complianceApi.rules"] = { data: { rules: [] } };
  apiDefaults["factsApi.categoryOptions"] = { data: { options: [] } };
  apiDefaults["factsApi.categories"] = { data: { categories: [] } };
  apiDefaults["bidAnalysisApi.items"] = { data: { items: [], groups: [] } };
  apiDefaults["bidAnalysisApi.results"] = { data: { items: [], summary: null } };
}

function renderPage() {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={["/scheme/s1"]}>
        <Routes>
          <Route path="/scheme/:id" element={<SchemeWorkbenchPage />} />
        </Routes>
      </MemoryRouter>
    </AntdApp>
  );
}

/** 切到「目录生成」Tab —— 起始 Tab 可能被 pickInitialTab 自动切到「解析提取」，
 *  而 import/facts 页会隐藏左侧目录树，树操作测试必须先在目录可见的 Tab 上。 */
async function gotoOutlineTab() {
  const label = await screen.findByText("目录生成");
  const tab = label.closest('[role="tab"]') || label;
  fireEvent.click(tab);
  // ⚠️ 用 getAllByText：目录树同时出现在「左侧导航树」与「右侧编辑树」两处，
  //    getByText 会因匹配到多个元素而报错。
  await waitFor(() => {
    expect(screen.getAllByText("工程概况").length).toBeGreaterThan(0);
  });
}

beforeEach(() => {
  for (const k of Object.keys(apiCalls)) delete apiCalls[k];
  setupDefaults();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("SchemeWorkbenchPage · 组件级冒烟与目录树编辑", () => {
  it("渲染冒烟：加载目录树并渲染章节节点", async () => {
    renderPage();
    await gotoOutlineTab();
    // 两个一级章节都在目录树里（左侧导航树 + 右侧编辑树，故用 getAll）
    expect(screen.getAllByText("工程概况").length).toBeGreaterThan(0);
    expect(screen.getAllByText("施工计划").length).toBeGreaterThan(0);
    // 加载完成后不再显示「加载中...」
    expect(screen.queryByText("加载中...")).toBeNull();
  }, 60000); // ✅ 后续修复（2026-09-28）：重型组件全量并行时偶发 20s 超时，放大到 60s

  it("渲染冒烟：六个工作流 Tab 全部存在，点击可切换", async () => {
    renderPage();
    await gotoOutlineTab();
    for (const t of ["解析提取", "目录生成", "全局事实", "正文生成", "审核与预检", "导出文档"]) {
      expect(screen.getByText(t)).toBeTruthy();
    }
  });

  it("导出文档绑定预检门禁：未预检时按钮禁用且不发导出请求", async () => {
    renderPage();
    const label = await screen.findByText("导出文档");
    const tab = label.closest('[role="tab"]') || label;
    fireEvent.click(tab);

    expect(await screen.findByText("尚未对当前方案完成导出预检")).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => (button.textContent || "").includes("导出 DOCX"));
    expect(docxButton?.disabled).toBe(true);
    fireEvent.click(docxButton!);
    expect(apiCalls["exportApi.docx"]).toBeUndefined();
  });

  /** 门禁测试通用流：切到「审核与预检」Tab 点「开始预检」→ 再切到「导出文档」Tab。
 *  导出预检的「开始预检」按钮位于审核与预检页（导出页只有门禁 Alert + 导出按钮），
 *  两者共享同一份 exportIssues/exportStats/exportPreflight 状态。
 *  ⚠️ 页面上「专家论证预检」卡片也有同名「开始预检」按钮，必须精确点击
 *  「导出预检（章节完整性）」卡片内的那个（DOM 结构上离该卡片标题最近的）。 */
  async function runExportCheck() {
    const reviewLabel = await screen.findByText("审核与预检");
    fireEvent.click(reviewLabel.closest('[role="tab"]') || reviewLabel);
    const precheckBtn = await waitFor(() => {
      const all = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .filter((b) => (b.textContent || "").includes("开始预检"));
      // 导出预检卡片的按钮带 loading={checkingExport}（antd 渲染为 primary 小按钮）；
      // 专家论证预检卡片的是 default 按钮。精确匹配 primary 型小按钮。
      const primary = all.filter((b) => b.className.includes("ant-btn-primary"));
      if (primary.length !== 1) throw new Error("未找到导出预检的「开始预检」按钮");
      return primary[0]!;
    });
    fireEvent.click(precheckBtn);
    // 等待 placeholderRerunPlan 返回（handleExportCheck 末尾的非致命分支）后再断言
    await waitFor(() => expect(apiCalls["exportApi.check"]?.length).toBeGreaterThan(0));
    await waitFor(() => expect(apiCalls["exportApi.placeholderRerunPlan"]?.length).toBeGreaterThan(0));
    const exportLabel = await screen.findByText("导出文档");
    fireEvent.click(exportLabel.closest('[role="tab"]') || exportLabel);
  }

  it("导出门禁-放行态：预检通过 + 就绪度放行 → 导出按钮可用，点导出 DOCX 发请求", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: { has_run: true, total: 92, grade: "A", verdict: "可交付", released: true, stale: false },
      },
    };
    renderPage();
    await runExportCheck();

    // 门禁放行：Alert 显示通过文案
    expect(await screen.findByText("导出预检通过，就绪度总检已放行")).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => (button.textContent || "").includes("导出 DOCX"));
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  it("导出门禁-高风险：预检存在 high 问题 → 按钮禁用并展示问题数", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [
          { type: "empty_section", severity: "high", section_id: "sec-1", title: "空章节" },
        ],
        section_count: 2,
        generated_count: 1,
        total_words: 1200,
        empty_ratio: 50,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: { has_run: true, total: 60, grade: "C", verdict: "需整改", released: false, stale: false },
      },
    };
    renderPage();
    await runExportCheck();

    expect(await screen.findByText(/导出预检仍有 1 个高风险问题/)).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => (button.textContent || "").includes("导出 DOCX"));
    expect(docxButton?.disabled).toBe(true);
  });

  it("导出门禁-未放行：预检无 high 但就绪度未放行 → 按钮禁用", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: { has_run: true, total: 60, grade: "C", verdict: "需整改", released: false, stale: false },
      },
    };
    renderPage();
    await runExportCheck();

    expect(await screen.findByText("就绪度总检尚未放行")).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => (button.textContent || "").includes("导出 DOCX"));
    expect(docxButton?.disabled).toBe(true);
  });

  it("导出门禁-结论过期：就绪度 stale=true → 按钮禁用并提示重新总检", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: { has_run: true, total: 92, grade: "A", verdict: "可交付", released: true, stale: true },
      },
    };
    renderPage();
    await runExportCheck();

    expect(await screen.findByText("就绪度总检结论已过期，请重新总检")).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => (button.textContent || "").includes("导出 DOCX"));
    expect(docxButton?.disabled).toBe(true);
  });

  it("import 子 Tab：文档解析 / 项目提取 切换均渲染且不崩溃（2026-09-25 补充）", async () => {
    renderPage();
    // 起点应在「解析提取」Tab（pickInitialTab 默认 import），否则先切过去
    const importLabel = await screen.findByText("解析提取");
    const importTab = importLabel.closest('[role="tab"]') || importLabel;
    fireEvent.click(importTab);
    // docs 子 Tab：上传解析主体 + 经 props 接入的「信息显示窗口」详情面板
    await waitFor(() => expect(screen.getByText("信息显示窗口")).toBeTruthy());
    // 切到 extract 子 Tab：左栏「18 项结构化提取」标题 + 右侧 BidAnalysisTab
    const extractLabel = screen.getByText("项目提取");
    const extractTab = extractLabel.closest('[role="tab"]') || extractLabel;
    fireEvent.click(extractTab);
    await waitFor(() => expect(screen.getByText("18 项结构化提取")).toBeTruthy());
  });

  it("解析内容预览：文档内容不完整时显示 warning，且不被「仅预览截取」覆盖", async () => {
    // 复现场景：50 页 PDF 解析到 30000 字后被截断，正文很短（远小于预览上限），
    // 于是 preview_truncated=false。旧实现用户在预览弹窗里看到的是一段看起来
    // 正常的短正文，完全不知道招标文件只读了一半。
    apiDefaults["factsApi.listDocuments"] = {
      data: { documents: [{ id: "d1", file_name: "招标文件.pdf", parse_status: "success" }] },
    };
    apiDefaults["factsApi.previewDocument"] = {
      data: {
        is_parsed: true,
        preview: "第一章 工程概况……（正文片段）",
        text_len: 300,
        truncated: true,            // 解析级截断：内容真的缺了
        preview_truncated: false,  // 预览级未截取：正文本来就短
        parse_warnings: [],
      },
    };
    renderPage();
    const importLabel = await screen.findByText("解析提取");
    fireEvent.click(importLabel.closest('[role="tab"]') || importLabel);
    const previewBtn = await screen.findByRole("button", { name: /预览/ });
    fireEvent.click(previewBtn);

    // 解析级截断必须显性提示，并给出可执行建议
    expect(await screen.findByText(/该文档内容不完整/)).toBeTruthy();
    expect(screen.getByText(/建议拆分文件后重新上传/)).toBeTruthy();
    // 关键回归：不得被预览级截取文案覆盖成「只是没显示全」
    expect(screen.queryByText(/预览仅显示前部分内容/)).toBeNull();
    // 正文本身仍正常渲染
    expect(screen.getByText(/第一章 工程概况/)).toBeTruthy();
  });

  it("解析内容预览：解析完整但被预览截取 → info 提示总字数，不报「不完整」", async () => {
    apiDefaults["factsApi.listDocuments"] = {
      data: { documents: [{ id: "d2", file_name: "施工组织设计.docx", parse_status: "success" }] },
    };
    apiDefaults["factsApi.previewDocument"] = {
      data: {
        is_parsed: true,
        preview: "（正文片段）",
        text_len: 20000,
        truncated: false,
        preview_truncated: true,   // 只是本弹窗没显示全，完整内容仍在库里
        parse_warnings: [],
      },
    };
    renderPage();
    const importLabel = await screen.findByText("解析提取");
    fireEvent.click(importLabel.closest('[role="tab"]') || importLabel);
    const previewBtn = await screen.findByRole("button", { name: /预览/ });
    fireEvent.click(previewBtn);

    expect(await screen.findByText(/预览仅显示前部分内容/)).toBeTruthy();
    expect(screen.getByText(/20,000 字/)).toBeTruthy();
    // 解析完整时不得误报内容缺失
    expect(screen.queryByText(/该文档内容不完整/)).toBeNull();
  });

  it("解析内容预览：解析告警（编码降级）必须知情可见", async () => {
    apiDefaults["factsApi.listDocuments"] = {
      data: { documents: [{ id: "d3", file_name: "旧编码合同.txt", parse_status: "success" }] },
    };
    apiDefaults["factsApi.previewDocument"] = {
      data: {
        is_parsed: true,
        preview: "（正文片段）",
        text_len: 120,
        truncated: false,
        preview_truncated: false,
        parse_warnings: ["源文件编码无法识别，已按 UTF-8 容错解码"],
      },
    };
    renderPage();
    const importLabel = await screen.findByText("解析提取");
    fireEvent.click(importLabel.closest('[role="tab"]') || importLabel);
    const previewBtn = await screen.findByRole("button", { name: /预览/ });
    fireEvent.click(previewBtn);

    expect(await screen.findByText(/解析过程提示 1 条/)).toBeTruthy();
    expect(screen.getByText(/编码无法识别/)).toBeTruthy();
  });

  it("新增子章节：树中出现「新章节」节点（本地新增，不发写库请求）", async () => {
    renderPage();
    await gotoOutlineTab();
    const icons = document.querySelectorAll('[aria-label="caret-right"]');
    expect(icons.length).toBeGreaterThan(0);
    fireEvent.click(icons[0]);
    await waitFor(() => expect(screen.getAllByText("新章节").length).toBeGreaterThan(0));
    // 本地新增节点不应立即写库（等「保存目录」统一落库）
    expect(apiCalls["sectionsApi.saveOutline"]).toBeUndefined();
  });

  it("新增同级章节：树中新增一个「新章节」", async () => {
    renderPage();
    await gotoOutlineTab();
    const icons = document.querySelectorAll('[aria-label="plus"]');
    expect(icons.length).toBeGreaterThan(0);
    fireEvent.click(icons[0]);
    await waitFor(() => expect(screen.getAllByText("新章节").length).toBeGreaterThan(0));
  });

  it("重命名：回车后 PATCH /sections/{id} 且携带新标题", async () => {
    renderPage();
    await gotoOutlineTab();
    // ⚠️ 走「右侧编辑树」的重命名入口（行尾带「(L1 · 字数)」标记，左树无此标记）：
    //    双击节点行进入重命名，预填 TreeNode.title（字符串）。
    const editTitles = Array.from(document.querySelectorAll<HTMLElement>(".ant-tree-title")).filter(
      (el) => (el.textContent || "").includes("工程概况") && (el.textContent || "").includes("L1 ·")
    );
    expect(editTitles.length).toBeGreaterThan(0);
    // ⚠️ jsdom 焦点桩：双击派发时原标题 span 被替换成 autoFocus 的 Input，jsdom 的
    //    焦点修复会立刻把焦点挪回 body 并触发 onBlur → 重命名同步提交并关闭（真实
    //    浏览器 dblclick 不执行 focus 修复，线上无此问题）。屏蔽 autoFocus 的 focus
    //    副作用后，重命名态保持打开；提交改走 onPressEnter（与失焦同一条 renameNode）。
    const focusSpy = vi.spyOn(HTMLInputElement.prototype, "focus").mockImplementation(() => {});
    // ⚠️ onDoubleClick 绑在 .ant-tree-title 内部的行容器 div 上（事件只冒泡不下降，
    //    必须点其子节点），取该 div 本身作为事件目标。
    fireEvent.doubleClick(editTitles[0]!.firstElementChild!);

    const input = await waitFor(() => {
      const el = Array.from(document.querySelectorAll<HTMLInputElement>("input")).find(
        (i) => i.value === "工程概况"
      );
      if (!el) throw new Error("重命名输入框未出现");
      return el;
    });
    fireEvent.change(input, { target: { value: "改名后的章节" } });
    // 回车提交（onPressEnter，与失焦走同一条 renameNode 路径）
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
    focusSpy.mockRestore();

    await waitFor(() => {
      const calls = apiCalls["sectionsApi.update"] || [];
      expect(calls.length).toBeGreaterThan(0);
    });
    const calls = apiCalls["sectionsApi.update"] || [];
    // 参数：(schemeId, sectionKey, { title })
    expect(calls[0][0]).toBe("s1");
    expect(calls[0][1]).toBe("sec-1");
    expect(calls[0][2]).toEqual({ title: "改名后的章节" });
  });

  it("重命名（未改即提交）：标题未变直接回车/失焦不发 PATCH", async () => {
    renderPage();
    await gotoOutlineTab();
    const editTitles = Array.from(document.querySelectorAll<HTMLElement>(".ant-tree-title")).filter(
      (el) => (el.textContent || "").includes("工程概况") && (el.textContent || "").includes("L1 ·")
    );
    expect(editTitles.length).toBeGreaterThan(0);
    // 焦点桩：同重命名主用例，避免 jsdom focus-fixup 把 Input 立刻 blur
    const focusSpy = vi.spyOn(HTMLInputElement.prototype, "focus").mockImplementation(() => {});
    fireEvent.doubleClick(editTitles[0]!.firstElementChild!);

    const input = await waitFor(() => {
      const el = Array.from(document.querySelectorAll<HTMLInputElement>("input")).find(
        (i) => i.value === "工程概况"
      );
      if (!el) throw new Error("重命名输入框未出现");
      return el;
    });
    // ❗ 不改动标题值，直接回车提交
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
    focusSpy.mockRestore();

    // 零请求：标题未变应被等值短路吞掉
    await waitFor(() => expect(apiCalls["sectionsApi.update"]?.length ?? 0).toBe(0));
  });

  it("删除章节：确认弹窗后 DELETE /sections/{id}/{key}", async () => {
    renderPage();
    await gotoOutlineTab();
    const delIcons = document.querySelectorAll('[aria-label="delete"]');
    expect(delIcons.length).toBeGreaterThan(0);
    fireEvent.click(delIcons[0]);

    // antd modal.confirm 挂在 body 的 portal 上，最后一个按钮是「确定」
    await waitFor(() => {
      const btns = document.querySelectorAll(".ant-modal-confirm-btns button");
      expect(btns.length).toBeGreaterThan(0);
    });
    const btns = document.querySelectorAll<HTMLButtonElement>(".ant-modal-confirm-btns button");
    fireEvent.click(btns[btns.length - 1]);

    await waitFor(() => {
      const calls = apiCalls["sectionsApi.delete"] || [];
      expect(calls.length).toBeGreaterThan(0);
    });
    const calls = apiCalls["sectionsApi.delete"] || [];
    expect(calls[0][0]).toBe("s1");
    expect(calls[0][1]).toBe("sec-1");
  });

  it("下移章节：调用 /reorder 并带上整树顺序", async () => {
    renderPage();
    await gotoOutlineTab();
    const downIcons = document.querySelectorAll('[aria-label="arrow-down"]');
    expect(downIcons.length).toBeGreaterThan(0);
    // ⚠️ aria-label 落在 antd 图标 span 上，disabled 在包裹的 button 上
    const downBtn = downIcons[0].closest("button") as HTMLButtonElement;
    // 第一个节点有同级后继 → 下移按钮应可用
    expect(downBtn.disabled).toBe(false);
    fireEvent.click(downBtn);

    await waitFor(() => {
      const calls = apiCalls["sectionsApi.reorder"] || [];
      expect(calls.length).toBeGreaterThan(0);
    });
    const calls = apiCalls["sectionsApi.reorder"] || [];
    expect(calls[0][0]).toBe("s1");
    // 顺序里应包含方案的两个章节 key
    expect(calls[0][1].order).toEqual(expect.arrayContaining(["sec-1", "sec-2"]));
  });

  it("保存目录：调用 saveOutline 且带上分级后的目录", async () => {
    renderPage();
    await gotoOutlineTab();
    // ⚠️ 按钮文案是「💾 保存目录」（带图标前缀），不能按纯文本精确匹配；
    //    且页面上还有多处含「保存目录」的说明文字，必须在 button 里找。
    await waitFor(() => {
      const found = Array.from(document.querySelectorAll("button")).some(
        (b) => (b.textContent || "").includes("💾")
      );
      expect(found).toBe(true);
    });
    const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent || "").includes("💾")
    )!;
    fireEvent.click(btn);

    await waitFor(() => {
      const calls = apiCalls["sectionsApi.saveOutline"] || [];
      expect(calls.length).toBeGreaterThan(0);
    });
    const calls = apiCalls["sectionsApi.saveOutline"] || [];
    expect(calls[0][0]).toBe("s1");
    expect(Array.isArray(calls[0][1].outline)).toBe(true);
    expect(calls[0][1].outline.length).toBe(2);
  });
});
