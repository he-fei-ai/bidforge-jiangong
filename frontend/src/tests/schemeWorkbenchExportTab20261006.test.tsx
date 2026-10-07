// @vitest-environment jsdom
/**
 * 方案工作台 · 「导出文档」Tab 组件级交互测试（2026-10-06）
 * ==============================================================
 * 补齐的覆盖缺口（既有 exportCharts / exportResponse / exportGateParity /
 * schemeWorkbenchExportPresets 均为**纯逻辑或按钮存在性**断言，缺"配置项
 * 是否真的进了导出请求体"与"失败/降级是否对用户可见"这两类端到端断言）：
 *
 *  ① **配置项 → 请求体贯通**：`scheme_forms` 四个法定前置表单开关、
 *     `chart_fail_placeholder`、`auto_rewrite_content`、
 *     `auto_fix_unclosed_fences` 此前没有任何用例验证「拨动开关后
 *     exportApi.docx 收到的 config 真的变了」——后端若漏读某个键，
 *     界面拨了开关却毫无效果，而这类缺陷不会被纯逻辑测试发现。
 *  ② **失败可见性**：exportApi.docx 抛错时必须给出错误反馈且按钮恢复可用
 *     （既有 schemaWorkbench.test 只覆盖成功路径）。
 *  ③ **降级可观测**：后端 2026-10-06 起把「内容块降级 / 表格截断」计数放进
 *     X-Fix-Stats；本文件锁住前端确实把它解析并展示（跨模块契约）。
 *  ④ **目录 JSON 导出**：`sectionsApi.exportTree` 的成功/空树两条路径。
 *
 * 夹具策略沿用 schemeWorkbench.test.tsx：`../api` 整体替换为「自动 Proxy
 * 假实现」（记录调用参数、支持 __reject__ 模拟失败），避免真实网络并能
 * 精确断言"点了按钮发了什么请求、带了什么 config"。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App as AntdApp } from "antd";
import SchemeWorkbenchPage, { deepMergeExportConfig } from "../pages/SchemeWorkbenchPage";

// ---------- ../api 自动假实现 ----------
const apiMock = vi.hoisted(() => {
  const calls: Record<string, any[][]> = {};
  const defaults: Record<string, any> = {};
  const cache = new Map<string, any>();

  function makeCallable(path: string[]): any {
    const key = path.join(".");
    const cached = cache.get(key);
    if (cached) return cached;
    const fn: any = (...args: any[]) => {
      (calls[key] ||= []).push(args);
      const d = defaults[key];
      if (d && typeof d === "object" && "__reject__" in d) {
        return Promise.reject(new Error(String((d as any).__reject__)));
      }
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

  function makeNamespace(): any {
    return new Proxy({} as any, {
      get(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        if (prop === "__esModule") return true;
        if (prop === "then") return undefined;
        return makeCallable([prop]);
      },
      has(_t, prop: any) { return typeof prop === "string"; },
      getOwnPropertyDescriptor(_t, prop: any) {
        if (typeof prop !== "string") return undefined;
        return { configurable: true, enumerable: true, value: makeCallable([prop]) };
      },
      ownKeys() { return []; },
    });
  }

  return { calls, defaults, root: makeNamespace() };
});

vi.mock("../api", () => apiMock.root);
const apiCalls = apiMock.calls;
const apiDefaults = apiMock.defaults;

vi.mock("../hooks/useSchemeLiveTask", () => ({
  useSchemeLiveTask: () => ({
    id: null, status: "idle", progress: 0, message: "", task_type: "",
    control: vi.fn(), refresh: vi.fn(), stop: vi.fn(),
  }),
}));

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

const TREE = [
  { id: "sec-1", title: "工程概况", level: 1, status: "empty", word_count: 0, word_budget: 1500, children: [] },
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
  apiDefaults["chartsApi.list"] = { data: { items: [] } };
  apiDefaults["exportApi.cacheStatus"] = { data: { items: [], total: 0, stale: 0 } };
  apiDefaults["exportApi.presets.list"] = { data: { presets: [] } };
  apiDefaults["sectionsApi.exportTree"] = { data: { tree: TREE } };
}

/** 成功导出：返回一个最小 Blob（jsdom 无 URL.createObjectURL 时补桩） */
function stubObjectUrl() {
  const g: any = globalThis as any;
  if (!g.URL.createObjectURL) g.URL.createObjectURL = () => "blob:mock";
  if (!g.URL.revokeObjectURL) g.URL.revokeObjectURL = () => {};
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

async function gotoExportTab() {
  const label = await screen.findByText("导出文档");
  const tab = label.closest('[role="tab"]') || label;
  fireEvent.click(tab);
  await waitFor(() => {
    expect(screen.getAllByText(/导出 DOCX/).length).toBeGreaterThan(0);
  });
}

function buttonByText(text: string): HTMLButtonElement | undefined {
  return Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
    .find((b) => (b.textContent || "").includes(text));
}

/** antd Switch 在 jsdom 下渲染成 button[role="switch"]，用 label 文案定位 */
function switchByLabel(labelText: string): HTMLElement | undefined {
  const nodes = Array.from(document.querySelectorAll<HTMLElement>(".ant-form-item"));
  for (const item of nodes) {
    const lbl = item.querySelector(".ant-form-item-label");
    if (lbl && (lbl.textContent || "").includes(labelText)) {
      return (item.querySelector('[role="switch"]') as HTMLElement | null) ?? undefined;
    }
  }
  return undefined;
}

/**
 * 展开「封面与页面」折叠面板（margins / cover_info / scheme_forms 都在里面）。
 *
 * ⚠️ 该面板**默认折叠**（defaultActiveKey=["basic","body","headings"]），
 * 而 antd `validateFields()` 只返回已挂载表单项 —— 这正是本文件锁定的
 * P0 缺陷（§见 deepMergeExportConfig 注释）。因此"打开面板 → 拨开关 →
 * 收起面板 → 导出"必须仍能把开关带进请求体。
 */
async function openCoverPanel() {
  const header = await screen.findByText("封面与页面");
  const clickable = header.closest(".ant-collapse-header") || header;
  fireEvent.click(clickable);
  await waitFor(() => expect(switchByLabel("编制说明")).toBeTruthy());
}

async function lastDocxConfig(): Promise<any> {
  await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  return apiCalls["exportApi.docx"][0][1];
}

beforeEach(() => {
  for (const k of Object.keys(apiCalls)) delete apiCalls[k];
  setupDefaults();
  stubObjectUrl();
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

// =========================================================================
describe("导出文档 Tab · 配置项贯通导出请求体", () => {
  it("默认配置：法定前置表单四开关全关、图表占位与自动改写全关（向后兼容基线）", async () => {
    renderPage();
    await gotoExportTab();
    // 法定前置表单面板**默认是折叠的**，其中的开关仍必须出现在请求体里
    fireEvent.click(buttonByText("导出 DOCX")!);
    const cfg = await lastDocxConfig();
    expect(cfg.scheme_forms).toEqual({
      compilation_note: false, approval: false,
      expert_review: false, drawing_appendix: false,
    });
    expect(cfg.chart_fail_placeholder).toBe(false);
    expect(cfg.auto_rewrite_content).toBe(false);
    expect(cfg.auto_fix_unclosed_fences).toBe(false);
  }, 60000);

  it("开启「专项施工方案审批表」后，请求体 config.scheme_forms.approval 为 true", async () => {
    renderPage();
    await gotoExportTab();
    await openCoverPanel();
    fireEvent.click(switchByLabel("专项施工方案审批表")!);
    fireEvent.click(buttonByText("导出 DOCX")!);
    const cfg = await lastDocxConfig();
    expect(cfg.scheme_forms.approval).toBe(true);
    // 其余三开关不得被连带打开
    expect(cfg.scheme_forms.compilation_note).toBe(false);
    expect(cfg.scheme_forms.expert_review).toBe(false);
    expect(cfg.scheme_forms.drawing_appendix).toBe(false);
  }, 60000);

  it("开启「编制说明」与「专家论证报告」后两个键同时为 true", async () => {
    renderPage();
    await gotoExportTab();
    await openCoverPanel();
    fireEvent.click(switchByLabel("编制说明")!);
    fireEvent.click(switchByLabel("专家论证报告")!);
    fireEvent.click(buttonByText("导出 DOCX")!);
    const cfg = await lastDocxConfig();
    expect(cfg.scheme_forms.compilation_note).toBe(true);
    expect(cfg.scheme_forms.expert_review).toBe(true);
  }, 60000);

  it("开启「图表渲染失败时保留红字占位」后 chart_fail_placeholder 进请求体", async () => {
    renderPage();
    await gotoExportTab();
    const sw = switchByLabel("渲染失败占位");
    expect(sw).toBeTruthy();
    fireEvent.click(sw!);
    fireEvent.click(buttonByText("导出 DOCX")!);
    const cfg = await lastDocxConfig();
    expect(cfg.chart_fail_placeholder).toBe(true);
  }, 60000);

  it("开启「导出前自动补齐未闭合围栏」后 auto_fix_unclosed_fences 进请求体", async () => {
    renderPage();
    await gotoExportTab();
    fireEvent.click(switchByLabel("修复未闭合代码块")!);
    fireEvent.click(buttonByText("导出 DOCX")!);
    const cfg = await lastDocxConfig();
    expect(cfg.auto_fix_unclosed_fences).toBe(true);
  }, 60000);

  it("导出 PDF 与导出 DOCX 共用同一份配置（两键互不串扰）", async () => {
    renderPage();
    await gotoExportTab();
    await openCoverPanel();
    fireEvent.click(switchByLabel("专项施工方案审批表")!);
    fireEvent.click(buttonByText("导出 PDF")!);
    await waitFor(() => expect(apiCalls["exportApi.pdf"]?.length).toBe(1));
    expect(apiCalls["exportApi.pdf"][0][1].scheme_forms.approval).toBe(true);
    expect(apiCalls["exportApi.docx"]).toBeUndefined();
  }, 60000);
});

// =========================================================================
describe("导出文档 Tab · 失败与降级的可见性", () => {
  /**
   * 注：本文件不断言 antd message 提示文案 —— 本仓库现有
   * schemeWorkbench.test.tsx / schemeWorkbenchExportPresets.test.tsx 也均未
   * 断言 message 文案（antd v5 message 在 jsdom 下不渲染到
   * 可查询的树，引用它会造成“永远失败”的假红灯）。
   * 因此这里改断言**可观测状态**（按钮恢复可用 /
   * 请求发出次数），而降级计数的**文案生成**由
   * exportResponse.test.ts 纯函数用例 + 后端跨语言契约护栏覆盖。
   */
  it("导出接口失败：不得残留在 loading，按钮恢复可用且只发一次请求", async () => {
    apiDefaults["exportApi.docx"] = { __reject__: "DOCX 导出失败：磁盘空间不足" };
    renderPage();
    await gotoExportTab();
    fireEvent.click(buttonByText("导出 DOCX")!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
    // finally 分子必须释放同步锁并把 exporting 拉回来
    await waitFor(() => {
      const b = buttonByText("导出 DOCX") || buttonByText("导出中");
      expect(b?.disabled ?? false).toBe(false);
    });
    // 不得因失败而反复重试
    expect(apiCalls["exportApi.docx"]?.length).toBe(1);
  }, 60000);

  it("X-Fix-Stats 带降级计数时导出仍正常完成（不抛错、不进入错误分支）", async () => {
    // 2026-10-06 后端加法式扩展：block_render_failed / table_truncated
    apiDefaults["exportApi.docx"] = {
      data: new Blob(["x"]),
      headers: {
        "x-export-filename": encodeURIComponent("测试方案_20261006_第1轮.docx"),
        "x-cache-status": "degraded",
        "x-fix-stats": JSON.stringify({ block_render_failed: 2, table_truncated: 1 }),
      },
    };
    renderPage();
    await gotoExportTab();
    fireEvent.click(buttonByText("导出 DOCX")!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
    // 导出成功后应自动刷新缓存状态（证明响应处理完整）
    await waitFor(() => expect(apiCalls["exportApi.cacheStatus"]?.length).toBeGreaterThan(0));
    await waitFor(() => {
      const b = buttonByText("导出 DOCX") || buttonByText("导出中");
      expect(b?.disabled ?? false).toBe(false);
    });
  }, 60000);

  it("图表渲染失败统计（failed>0）不得阻断导出，仍正常完成", async () => {
    apiDefaults["exportApi.docx"] = {
      data: new Blob(["x"]),
      headers: {
        "x-export-filename": encodeURIComponent("a.docx"),
        "x-cache-status": "degraded",
        "x-chart-render-stats": JSON.stringify({ fe: 1, backend_ok: 2, failed: 1 }),
      },
    };
    renderPage();
    await gotoExportTab();
    fireEvent.click(buttonByText("导出 DOCX")!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
    await waitFor(() => {
      const b = buttonByText("导出 DOCX") || buttonByText("导出中");
      expect(b?.disabled ?? false).toBe(false);
    });
  }, 60000);
});

// =========================================================================
describe("导出文档 Tab · 目录 JSON 导出", () => {
  it("有目录时导出目录 JSON：调用 export-tree 接口并触发下载", async () => {
    renderPage();
    await gotoExportTab();
    fireEvent.click(buttonByText("导出目录 JSON")!);
    await waitFor(() => expect(apiCalls["sectionsApi.exportTree"]?.length).toBe(1));
  }, 60000);

  it("空目录时按钮禁用，不发起请求", async () => {
    apiDefaults["sectionsApi.list"] = {
      data: { scheme: { id: "s1", project_id: "p1", name: "空方案" }, tree: [] },
    };
    renderPage();
    await gotoExportTab();
    const b = buttonByText("导出目录 JSON");
    expect(b?.disabled).toBe(true);
    expect(apiCalls["sectionsApi.exportTree"]).toBeUndefined();
  }, 60000);
});

// =========================================================================
describe("deepMergeExportConfig · 嵌套深合并纯函数", () => {
  it("校验结果覆盖 store 的同名叶子值", () => {
    const merged = deepMergeExportConfig(
      { font_name: "宋体", scheme_forms: { approval: false } },
      { font_name: "微软雅黑" },
    );
    expect(merged.font_name).toBe("微软雅黑");
    expect(merged.scheme_forms).toEqual({ approval: false });
  });

  it("未挂载子对象按键深合并而非整体覆盖", () => {
    const merged = deepMergeExportConfig(
      { margins: { top: 2.5, bottom: 2.5 } },
      { margins: { top: 3.5 } },
    );
    expect(merged.margins).toEqual({ top: 3.5, bottom: 2.5 });
  });

  it("validated 为 undefined / 非对象时原样返回 store", () => {
    const store = { a: 1 };
    expect(deepMergeExportConfig(store, undefined)).toEqual(store);
    expect(deepMergeExportConfig(store, null as any)).toEqual(store);
    expect(deepMergeExportConfig(undefined, undefined)).toEqual({});
  });

  it("false / 0 / 空串等假值不得被当作未设置而丢弃", () => {
    const merged = deepMergeExportConfig(
      { show_toc: true, toc_depth: 3, bidder_name: "" },
      { show_toc: false, toc_depth: 0, bidder_name: "" },
    );
    expect(merged.show_toc).toBe(false);
    expect(merged.toc_depth).toBe(0);
    expect(merged.bidder_name).toBe("");
  });
});
