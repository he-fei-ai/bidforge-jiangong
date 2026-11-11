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
// ✅ 页面级重渲染测试：jsdom + 完整 React 树 + 大量 waitFor；单文件独占
//    34 用例 / 167.8s tests + 87.9s collect（418KB 页面模块 import 热点）。
//    R213/R214 实测并行跑 pytest+vitest 时 ~12s 的用例被放大到 >60s ⇒ 假红
//    `Test timed out in 60000ms`（独占串行复跑 34/34 全绿，非代码回归）。
//    R210 登记的触发判据已达成 ⇒ **文件级**放宽到 120s（不动全局
//    vitest.config.ts，其余 92 个测试文件的失败反馈速度不变）；本文件无
//    beforeAll，仅同步 beforeEach，放宽 hookTimeout 不会掩盖 hook 卡死。
vi.setConfig({ testTimeout: 120000, hookTimeout: 120000 });
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import React from "react";
import { App as AntdApp } from "antd";
import SchemeWorkbenchPage from "../pages/SchemeWorkbenchPage";
import { getActivityItems, clearActivity } from "../utils/activityCenter";

// ---------- ../api 自动假实现 ----------
// ⚠️ vi.mock 会被提升到文件顶部，工厂里不能引用普通顶层变量（TDZ）。
//    所有 mock 状态必须经 vi.hoisted 创建。
const apiMock = vi.hoisted(() => {
  const calls: Record<string, any[][]> = {};
  const defaults: Record<string, any> = {};
  const cache = new Map<string, any>();
  // 真实模块的数值导出（超时常量）由 vi.mock 工厂通过 importOriginal 填充。
  // 用例内的页面/hook 在模块初始化时就会做 Math.round(常量 / 1000)，纯动态
  // 可调用代理无法参与算术（会抛 Cannot convert object to primitive value）。
  const numericExports: Record<string, number> = {};

  /** 可调用节点：既能当方法调用（记录参数 + 返回默认响应），也能继续取子属性 */
  function makeCallable(path: string[]): any {
    const key = path.join(".");
    const cached = cache.get(key);
    if (cached) return cached;
    const fn: any = (...args: any[]) => {
      (calls[key] ||= []).push(args);
      const d = defaults[key];
      // 约定：__reject__ 标记的默认值返回 rejected Promise（用于模拟接口失败）
      if (d && typeof d === "object" && "__reject__" in d) {
        return Promise.reject(new Error(String((d as any).__reject__)));
      }
      // 约定：__reject_with__ 标记的默认值按**原样** reject（带 response/status
      // 的 Axios 错误形态 —— 预检 503 降级态必须能被 catch 读到 read_degraded）
      if (d && typeof d === "object" && "__reject_with__" in d) {
        return Promise.reject((d as any).__reject_with__);
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
        // 数值导出（页面/hook 会参与算术与文案拼接）：纯动态可调用代理无法参与运算
        const numeric = numericExports[prop];
        if (typeof numeric === "number") return numeric;
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

  return { calls, defaults, root: makeNamespace(), numericExports };
});

vi.mock("../api", async (importOriginal) => {
  // 只合并真实模块的「数值导出」（超时常量），其余仍走动态代理。
  // 不能整体 ...actual：factsApi / docPipelineApi 等是真实命名空间对象，
  // 展开后页面会打到真实 HTTP；而逐个列字面量在新增超时常量时会再次漏配。
  const actual = (await importOriginal()) as Record<string, unknown>;
  for (const [key, value] of Object.entries(actual)) {
    if (typeof value === "number") apiMock.numericExports[key] = value;
  }
  return apiMock.root;
});

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
  {
    id: "sec-1",
    title: "工程概况",
    level: 1,
    status: "empty",
    word_count: 0,
    word_budget: 1500,
    children: [],
  },
  {
    id: "sec-2",
    title: "施工计划",
    level: 1,
    status: "empty",
    word_count: 0,
    word_budget: 1500,
    children: [],
  },
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
    </AntdApp>,
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
  clearActivity();
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

  it("载入上次一致性结果 → 展示「多批取最小」评分口径说明", async () => {
    // ✅ R181（遗留问题 6）：后端评分口径为 min_across_batches（有意保守）。
    //    前端必须显式展示 score_note，否则用户会误以为分数无故下降。
    apiDefaults["complianceApi.consistencyLatest"] = {
      data: {
        exists: true,
        score: 82,
        issues: [],
        created_at: "2026-11-06T00:00:00",
        score_rule: "min_across_batches",
        score_note: "一致性分数取各批审计的最低分（本次 3 批），以保守口径呈现整体风险。",
      },
    };
    renderPage();
    const label = await screen.findByText("审核与预检");
    fireEvent.click(label.closest('[role="tab"]') || label);
    const load = await screen.findByText("载入上次结果");
    fireEvent.click(load.closest("button") || load);
    await waitFor(() => expect(screen.getByTestId("consistency-score-note")).toBeTruthy());
    expect(screen.getByTestId("consistency-score-note").textContent || "").toContain(
      "取各批审计的最低分",
    );
  });

  it("渲染冒烟：六个工作流 Tab 全部存在，点击可切换", async () => {
    renderPage();
    await gotoOutlineTab();
    for (const t of ["解析提取", "目录生成", "全局事实", "正文生成", "审核与预检", "导出文档"]) {
      expect(screen.getByText(t)).toBeTruthy();
    }
  });

  /** ✅ 2026-10-01：导出不再受审核 / 预检门禁限制，未预检也必须能导出。 */
  it("导出不受预检门禁限制：未预检时按钮仍可用且点击发起导出请求", async () => {
    renderPage();
    const label = await screen.findByText("导出文档");
    const tab = label.closest('[role="tab"]') || label;
    fireEvent.click(tab);

    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => (button.textContent || "").includes("导出 DOCX"),
    );
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  /** 门禁测试通用流：切到「审核与预检」Tab 点「开始预检」→ 再切到「导出文档」Tab。
   *  导出预检的「开始预检」按钮位于审核与预检页（导出页只有门禁 Alert + 导出按钮），
   *  两者共享同一份 exportIssues/exportStats/exportPreflight 状态。
   *  ⚠️ 页面上「专家论证预检」卡片也有同名「开始预检」按钮，必须精确点击
   *  「导出预检（章节完整性）」卡片内的那个（DOM 结构上离该卡片标题最近的）。 */
  async function runExportCheck(opts?: { expectPlan?: boolean }) {
    const reviewLabel = await screen.findByText("审核与预检");
    fireEvent.click(reviewLabel.closest('[role="tab"]') || reviewLabel);
    const precheckBtn = await waitFor(() => {
      const all = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).filter((b) =>
        (b.textContent || "").includes("开始预检"),
      );
      // 导出预检卡片的按钮带 loading={checkingExport}（antd 渲染为 primary 小按钮）；
      // 专家论证预检卡片的是 default 按钮。精确匹配 primary 型小按钮。
      const primary = all.filter((b) => b.className.includes("ant-btn-primary"));
      if (primary.length !== 1) throw new Error("未找到导出预检的「开始预检」按钮");
      return primary[0]!;
    });
    fireEvent.click(precheckBtn);
    // 等待 placeholderRerunPlan 返回（handleExportCheck 末尾的非致命分支）后再断言。
    // 预检失败/降级时该分支不会触发（请求在 catch 处结束），故允许跳过等待。
    await waitFor(() => expect(apiCalls["exportApi.check"]?.length).toBeGreaterThan(0));
    if (opts?.expectPlan !== false) {
      await waitFor(() =>
        expect(apiCalls["exportApi.placeholderRerunPlan"]?.length).toBeGreaterThan(0),
      );
    }
    const exportLabel = await screen.findByText("导出文档");
    fireEvent.click(exportLabel.closest('[role="tab"]') || exportLabel);
  }

  it("导出-放行态：预检通过 + 就绪度放行 → 导出按钮可用，点导出 DOCX 发请求", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: {
          has_run: true,
          total: 92,
          grade: "A",
          verdict: "可交付",
          released: true,
          stale: false,
        },
      },
    };
    renderPage();
    await runExportCheck();

    // 无 high 问题 → 不展示阻断式提示，按钮可用
    expect(screen.queryByText(/高风险问题/)).toBeNull();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => (button.textContent || "").includes("导出 DOCX"),
    );
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  it("导出-高风险：预检存在 high 问题 → 仅提示问题数，按钮仍可用", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [{ type: "empty_section", severity: "high", section_id: "sec-1", title: "空章节" }],
        section_count: 2,
        generated_count: 1,
        total_words: 1200,
        empty_ratio: 50,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: {
          has_run: true,
          total: 60,
          grade: "C",
          verdict: "需整改",
          released: false,
          stale: false,
        },
      },
    };
    renderPage();
    await runExportCheck();

    expect(await screen.findByText(/仍有 1 个高风险问题/)).toBeTruthy();
    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => (button.textContent || "").includes("导出 DOCX"),
    );
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  it("导出-未放行：预检无 high 但就绪度未放行 → 按钮仍可用", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: {
          has_run: true,
          total: 60,
          grade: "C",
          verdict: "需整改",
          released: false,
          stale: false,
        },
      },
    };
    renderPage();
    await runExportCheck();

    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => (button.textContent || "").includes("导出 DOCX"),
    );
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  it("导出-结论过期：就绪度 stale=true → 不影响导出，按钮仍可用", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 0,
        chart_done: 0,
        preflight_summary: {
          has_run: true,
          total: 92,
          grade: "A",
          verdict: "可交付",
          released: true,
          stale: true,
        },
      },
    };
    renderPage();
    await runExportCheck();

    const docxButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(
      (button) => (button.textContent || "").includes("导出 DOCX"),
    );
    expect(docxButton?.disabled).toBe(false);
    fireEvent.click(docxButton!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
  });

  /* ==========================================================================
     R178 补强 · 导出预检三态（后端审计 → 前端消费的可见出口）
     之前只有「放行/未放行/stale」三条门禁用例，**没有**任何用例断言预检结果
     本身是否真的被展示：无问题、有问题、后端降级三态从无护栏。最危险的是
     第三态 —— 后端 503 没读到数据，前端若渲染成空态，就是「预检假通过」。
     ========================================================================== */

  const exportCheckActivity = () =>
    getActivityItems()
      .map((a) => a.text)
      .join("\n");

  it("导出预检-无问题：消息中心报「预检通过」，导出页显示已就绪", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 1,
        chart_done: 1,
        preflight_summary: { has_run: false },
      },
    };
    renderPage();
    await runExportCheck();

    await waitFor(() => expect(exportCheckActivity()).toContain("预检通过：2 个章节均无异常"));
    expect(await screen.findByText(/章节与图表均已就绪/)).toBeTruthy();
    expect(screen.queryByText(/导出预检发现/)).toBeNull();
  });

  it("导出预检-有问题：问题计数 + 类型标签 + 具体描述都可见", async () => {
    apiDefaults["exportApi.check"] = {
      data: {
        issues: [
          {
            // 后端 chart_ungenerated 的真实形态（type 必须存在，否则标签回退成裸代号）
            type: "chart_ungenerated",
            chart_type: "layout",
            status: "pending",
            detail: "该图表（AI 配图）正文仅占位",
          },
        ],
        section_count: 2,
        generated_count: 2,
        total_words: 3200,
        empty_ratio: 0,
        chart_total: 1,
        chart_done: 0,
        preflight_summary: { has_run: false },
      },
    };
    renderPage();
    await runExportCheck();

    // ✅ R213 重锚（意图保留、强度不减）：R212 FE-6 把导出预检文案升级为
    //    「发现 1 个问题（共 1 处），建议处理后再导出」，旧断言把整句文案逐字
    //    锁死（锁实现而非锁意图）⇒ 产品文案一改就假红。现锁「问题计数」与
    //    「建议处理」两个意图片段，两个信号都必须在活动记录里出现。
    await waitFor(() => expect(exportCheckActivity()).toContain("发现 1 个问题"));
    expect(exportCheckActivity()).toContain("建议处理后再导出");
    expect(screen.getAllByText(/导出预检发现 1 个问题/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/图表未生成/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/正文仅占位/).length).toBeGreaterThan(0);
    // 有问题态不得同时出现「已就绪」的绿色结论
    expect(screen.queryByText(/章节与图表均已就绪/)).toBeNull();
  });

  it("导出预检-后端降级：显示「预检未完成」，绝不显示成无问题", async () => {
    apiDefaults["exportApi.check"] = {
      __reject_with__: {
        name: "AxiosError",
        message: "预检未能读取方案数据（数据库连接异常），本次结果不代表「无问题」，请稍后重试",
        response: {
          status: 503,
          data: {
            detail:
              "导出预检未能读取方案数据（数据库连接异常），本次结果不代表「无问题」，请稍后重试",
            error: "export_check_read_failed",
            read_degraded: true,
            retry_after: 5,
          },
        },
      },
    };
    renderPage();
    // 降级在 catch 结束，不会走到 placeholderRerunPlan 分支
    await runExportCheck({ expectPlan: false });

    await waitFor(() => expect(exportCheckActivity()).toContain("不代表「无问题」"));
    expect((await screen.findAllByText(/预检未完成/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/不能视为「无问题」/).length).toBeGreaterThan(0);
    // 关键回归锁：降级态**不得**出现任何「通过/已就绪」措辞
    expect(screen.queryByText(/预检通过/)).toBeNull();
    expect(screen.queryByText(/章节与图表均已就绪/)).toBeNull();
  });

  it("import 子 Tab：文档解析 / 项目提取 切换均渲染且不崩溃（2026-09-25 补充）", async () => {
    // ✅ 2026-10-05：左面板标题按已加载的提取项定义**实报条数**（旧实现写死
    //    「18 项结构化提取」，后端增减项后标题即失真）。此处给 3 项定义，
    //    断言渲染出「3 项…」= 证明口径是动态的，而不是碰巧等于 18。
    apiDefaults["bidAnalysisApi.items"] = {
      data: {
        items: [
          { item_id: "schemeType", label: "方案类型", required: 1, output_type: "json" },
          { item_id: "orgStructure", label: "组织机构", required: 1, output_type: "json" },
          { item_id: "others", label: "其他说明", required: 0, output_type: "markdown" },
        ],
        groups: [
          { key: "basic", label: "基本信息", item_ids: ["schemeType", "orgStructure", "others"] },
        ],
      },
    };
    renderPage();
    // 起点应在「解析提取」Tab（pickInitialTab 默认 import），否则先切过去
    const importLabel = await screen.findByText("解析提取");
    const importTab = importLabel.closest('[role="tab"]') || importLabel;
    fireEvent.click(importTab);
    // docs 子 Tab：上传解析主体 + 经 props 接入的「信息显示窗口」详情面板
    await waitFor(() => expect(screen.getByText("信息显示窗口")).toBeTruthy());
    // 切到 extract 子 Tab：左栏标题（按定义条数动态） + 右侧 BidAnalysisTab
    const extractLabel = screen.getByText("项目提取");
    const extractTab = extractLabel.closest('[role="tab"]') || extractLabel;
    fireEvent.click(extractTab);
    await waitFor(() => expect(screen.getByText("3 项结构化提取")).toBeTruthy());
    // 回归锁：不得再出现写死的「18 项」（定义只有 3 条时任何 18 都是硬编码）
    expect(screen.queryByText("18 项结构化提取")).toBeNull();
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
        truncated: true, // 解析级截断：内容真的缺了
        preview_truncated: false, // 预览级未截取：正文本来就短
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
        preview_truncated: true, // 只是本弹窗没显示全，完整内容仍在库里
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
      (el) =>
        (el.textContent || "").includes("工程概况") && (el.textContent || "").includes("L1 ·"),
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
        (i) => i.value === "工程概况",
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
      (el) =>
        (el.textContent || "").includes("工程概况") && (el.textContent || "").includes("L1 ·"),
    );
    expect(editTitles.length).toBeGreaterThan(0);
    // 焦点桩：同重命名主用例，避免 jsdom focus-fixup 把 Input 立刻 blur
    const focusSpy = vi.spyOn(HTMLInputElement.prototype, "focus").mockImplementation(() => {});
    fireEvent.doubleClick(editTitles[0]!.firstElementChild!);

    const input = await waitFor(() => {
      const el = Array.from(document.querySelectorAll<HTMLInputElement>("input")).find(
        (i) => i.value === "工程概况",
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
      const found = Array.from(document.querySelectorAll("button")).some((b) =>
        (b.textContent || "").includes("💾"),
      );
      expect(found).toBe(true);
    });
    const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent || "").includes("💾"),
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
    // ✅ R175 G-3：默认保存不带逃生舱键（请求体与引入前一致，后端闸门零豁免）
    expect(calls[0][1].allow_empty_chapters).toBeUndefined();
  });

  it("✅ R175 空章闸门：手动保存 400 → 逃生舱确认框 → 确认后带 allow_empty_chapters 重发", async () => {
    renderPage();
    await gotoOutlineTab();
    // 首次保存被后端闸门拒绝（detail 经 axios 拦截器写入 error.message）
    apiDefaults["sectionsApi.saveOutline"] = {
      __reject__:
        "上传目录存在 1 个无子章节的一级章（第2章「施工计划」）：请为其补充二级章节" +
        "或删除该章后重新保存；如确需按此结构保存，请在请求体中传 allow_empty_chapters=true。",
    };
    const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent || "").includes("💾"),
    )!;
    fireEvent.click(btn);
    // 闸门确认框出现（非普通错误 toast）
    await waitFor(() => {
      expect(document.querySelector(".ant-modal-confirm")).not.toBeNull();
    });
    expect(document.body.textContent).toContain("检出无子章节的一级章节");
    expect(document.body.textContent).toContain("仍要保存（保留空章）");
    // 首次失败后不自动重发（等用户决策）
    expect(apiCalls["sectionsApi.saveOutline"]!.length).toBe(1);
    // 放行后二次调用成功
    delete apiDefaults["sectionsApi.saveOutline"];
    const btns = document.querySelectorAll<HTMLButtonElement>(".ant-modal-confirm-btns button");
    fireEvent.click(btns[btns.length - 1]); // ok 按钮
    await waitFor(() => {
      expect((apiCalls["sectionsApi.saveOutline"] || []).length).toBe(2);
    });
    const retryPayload = apiCalls["sectionsApi.saveOutline"]![1][1];
    expect(retryPayload.allow_empty_chapters).toBe(true);
    expect(retryPayload.source).toBe("手动编辑");
    delete apiDefaults["sectionsApi.saveOutline"];
  }, 60000);

  it("✅ R175 空章通知：保存成功但响应带 empty_top_chapters → 出现空章 info 提示", async () => {
    renderPage();
    await gotoOutlineTab();
    apiDefaults["sectionsApi.saveOutline"] = {
      data: { count: 2, empty_top_chapters: [{ index: 1, title: "施工计划" }] },
    };
    const btn = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent || "").includes("💾"),
    )!;
    // ✅ R178（2026-11-04）：自 2026-09-16 起 useAntdMessageHub 只把提示写入
    // 消息中心、不再弹原生 toast，此断言原先锁的是「document.body 里出现 toast 文案」，
    // 机制已作废（永远不可能绿）。改为锁真实承载渠道：消息中心记录里必须出现
    // 空章提示原文（含章号与章名），比原来的 DOM 文本断言更贴近行为。
    const notices = () =>
      getActivityItems()
        .map((a) => a.text)
        .join("\n");
    clearActivity();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(notices()).toContain("1 个一级章暂无子章节");
    });
    expect(notices()).toContain("第2章「施工计划」");
    delete apiDefaults["sectionsApi.saveOutline"];
  }, 60000);
});

// =====================================================================
// ✅ 2026-10-04：四层管线动作 + 删除文档 · 页面级交互测试
//    此前 handlePipelineAction 的 4 个分支（重新解析 / 物化提取层 /
//    交叉校验 / 刷新质量）与 handleDeleteDocument 在页面级**零覆盖**。
//    其中「刷新质量」存在真实 BUG：refreshPipelineQuality 内部吞掉异常，
//    调用方却无条件 msg.success("质量分已刷新") —— 接口失败时用户仍看到
//    「已刷新」。本组测试钉住：失败必须报错、成功才提示成功；
//    其余三个分支的请求参数/成功反馈；删除确认→请求→列表刷新链路。
// =====================================================================
describe("SchemeWorkbenchPage · 四层管线动作与删除（页面级交互）", () => {
  const DOC = { id: "d1", file_name: "招标文件.pdf", parse_status: "success" };

  function seedDoc() {
    apiDefaults["factsApi.listDocuments"] = { data: { documents: [DOC] } };
    apiDefaults["factsApi.previewDocument"] = {
      data: { is_parsed: true, preview: "第一章 工程概况", text_len: 30, parse_warnings: [] },
    };
    apiDefaults["docPipelineApi.status"] = {
      data: { doc_id: "d1", parse_status: "success", layers: {} },
    };
  }

  async function openPreview() {
    const importLabel = await screen.findByText("解析提取");
    fireEvent.click(importLabel.closest('[role="tab"]') || importLabel);
    const previewBtn = await screen.findByRole("button", { name: /预览/ });
    fireEvent.click(previewBtn);
    // 等待管线摘要渲染出操作按钮（加载完成后才出现）
    await waitFor(() => expect(screen.getByRole("button", { name: /刷新质量/ })).toBeTruthy());
  }

  /**
   * 在「可见的」预览弹窗内按精确文案找按钮。
   * 页面常驻多个 Modal（含隐藏残留），document.querySelector 会命中第一个隐藏 body，
   * 因此必须排除 display:none 的 wrap，再在可见 body 内匹配。
   */
  function modalActionButton(label: string): HTMLButtonElement {
    const wraps = Array.from(document.querySelectorAll<HTMLElement>(".ant-modal-wrap"));
    const visibleWrap =
      wraps.find((w) => w.style.display !== "none") || wraps.find((w) => !w.style.display);
    const scope = visibleWrap?.querySelector(".ant-modal-body") || document;
    const btn = Array.from(scope.querySelectorAll("button")).find((b) =>
      (b.textContent || "").replace(/\s+/g, "").includes(label),
    );
    if (!btn) throw new Error(`弹窗内未找到按钮：${label}`);
    return btn as HTMLButtonElement;
  }

  /** 等待消息中心出现满足断言的记录（提示统一进消息中心，不弹原生 toast） */
  async function waitForActivity(assertFn: (texts: string[]) => boolean) {
    await waitFor(() => {
      const texts = getActivityItems().map((a) => a.text);
      expect(assertFn(texts)).toBe(true);
    });
  }

  function lastCall(key: string): any[] {
    const calls = apiCalls[key] || [];
    return calls[calls.length - 1];
  }

  it("刷新质量：接口失败时不得提示成功，必须给出失败反馈（BUG 回归）", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.completeness"] = { __reject__: "网络异常" };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("刷新质量"));

    // 关键回归断言：失败时出现错误提示，且绝不出现「质量分已刷新」
    await waitForActivity(
      (t) => t.some((x) => /质量分刷新失败/.test(x)) && !t.some((x) => /质量分已刷新/.test(x)),
    );
    // 确实发起过请求（refresh=true）
    expect(lastCall("docPipelineApi.completeness")[1]).toBe(true);
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("刷新质量：接口成功时回填并提示「质量分已刷新」", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.completeness"] = {
      data: { quality_score: 0.85, completeness: {} },
    };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("刷新质量"));

    await waitForActivity(
      (t) => t.some((x) => /质量分已刷新/.test(x)) && !t.some((x) => /质量分刷新失败/.test(x)),
    );
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("重新解析：调用 reparse(force) 并按返回内容反馈，随后重拉权威状态", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.reparse"] = { data: { skipped: false } };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("重新解析"));

    await waitForActivity((t) => t.some((x) => /已重新解析并刷新四层产物/.test(x)));
    expect(lastCall("docPipelineApi.reparse")).toEqual(["d1", "force"]);
    // 非 refreshQuality 分支动作后必须重拉状态（loadPipelineStatus → status）
    await waitFor(() =>
      expect((apiCalls["docPipelineApi.status"] || []).length).toBeGreaterThan(1),
    );
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("物化提取层：反馈物化类别数", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.syncExtractions"] = {
      data: { written_types: ["人员", "资质", "业绩"] },
    };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("物化提取层"));

    await waitForActivity((t) => t.some((x) => /提取层已物化（3 类）/.test(x)));
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("交叉校验：反馈冲突数", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.crossCheck"] = { data: { stored_conflicts: 2 } };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("交叉校验"));

    await waitForActivity((t) => t.some((x) => /交叉校验完成（冲突 2）/.test(x)));
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("管线动作失败：reparse 抛错时提示错误且不残留 loading", async () => {
    seedDoc();
    apiDefaults["docPipelineApi.reparse"] = { __reject__: "解析服务不可用" };
    renderPage();
    await openPreview();
    fireEvent.click(modalActionButton("重新解析"));

    await waitForActivity((t) => t.some((x) => /解析服务不可用/.test(x)));
    // 动作结束（finally）后按钮恢复可点
    await waitFor(() => expect(modalActionButton("重新解析").hasAttribute("disabled")).toBe(false));
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动

  it("删除文档：确认后发起删除请求、提示成功并重拉文档列表", async () => {
    seedDoc();
    renderPage();
    const importLabel = await screen.findByText("解析提取");
    fireEvent.click(importLabel.closest('[role="tab"]') || importLabel);
    // 用文本匹配删除按钮（文档列表 actions 区，type=link），避免图标干扰 accessible name
    const delBtn = await waitFor(() => {
      const btn = Array.from(document.querySelectorAll("button")).find(
        (b) => (b.textContent || "").trim() === "删除",
      );
      expect(btn).toBeTruthy();
      return btn as HTMLButtonElement;
    });
    await act(async () => {
      fireEvent.click(delBtn);
    });

    // 出现确认框并确认（antd Modal.confirm 默认 OK 按钮文案为 OK；okButtonProps.danger）
    const okBtn = await screen.findByRole("button", { name: /OK/ });
    await act(async () => {
      fireEvent.click(okBtn);
    });

    await waitForActivity((t) => t.some((x) => /文件已删除/.test(x)));
    expect(lastCall("factsApi.deleteDocument")[0]).toBe("d1");
    // 删除成功后重新加载文档列表
    await waitFor(() =>
      expect((apiCalls["factsApi.listDocuments"] || []).length).toBeGreaterThan(1),
    );
  }, 60000); // ✅ 2026-10-10（TD-2）：与 L208 同口径，全量并发防超时抖动
});

// =====================================================================
// ✅ 2026-10-03：导出「成功路径」组件级交互测试（此前仅覆盖门禁/按钮可用，
//    **实际下载链路**从未组件级验证）。后端已对 X-Export-Filename / X-Cache-Status
//    / X-Chart-Render-Stats / X-Fix-Stats 等响应头做了完整处理（export.py 7244-7363），
//    但前端这些「拿头 → 解文件名 → 触发下载 → 刷新缓存状态 → 解析统计」逻辑
//    只在 SchemeWorkbenchPage.handleExport 里消费，缺组件级回归网，改错只能靠手点。
//    本组测试把这条链路钉住：① 后端百分号编码的文件名被 decodeURIComponent 还原
//    并用作 a.download；② 成功路径触发 URL.createObjectURL（下载副作用）；
//    ③ 成功后调用 exportApi.cacheStatus 刷新缓存状态；④ 统计头被安全解析不抛异常。
// =====================================================================
describe("SchemeWorkbenchPage · 导出成功路径（组件级交互）", () => {
  function findExportButton(label: string): HTMLButtonElement | undefined {
    return Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((b) =>
      (b.textContent || "").includes(label),
    );
  }

  async function gotoExportTab() {
    const tabLabel = await screen.findByText("导出文档");
    const tab = tabLabel.closest('[role="tab"]') || tabLabel;
    fireEvent.click(tab);
    await waitFor(() => expect(findExportButton("导出 DOCX")).toBeTruthy());
  }

  /** 捕获下载锚点 + createObjectURL 调用，验证成功路径的「下载副作用」。 */
  function installDownloadProbes(): {
    anchors: HTMLAnchorElement[];
    // ✅ vi.spyOn 在静态方法上的精确类型较繁琐，测试辅助函数用 any 收口足矣。
    createObjUrl: any;
    restore: () => void;
  } {
    const anchors: HTMLAnchorElement[] = [];
    const origCreate = document.createElement.bind(document);
    const createSpy = vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = origCreate(tag as any);
      if (tag === "a") anchors.push(el as HTMLAnchorElement);
      return el;
    });
    const createObjUrl = vi.spyOn(URL, "createObjectURL");
    return {
      anchors,
      createObjUrl,
      restore: () => {
        createSpy.mockRestore();
        createObjUrl.mockRestore();
      },
    };
  }

  it("导出 DOCX 成功：解码后端文件名作下载名 + 刷新缓存状态 + 解析统计头", async () => {
    const expectedName = "测试方案 2026-10-03 第1轮.docx";
    apiDefaults["exportApi.docx"] = {
      // ✅ 伪造 docx 字节（PK 头），Blob 构造不抛
      data: new Uint8Array([80, 75, 3, 4]).buffer,
      headers: {
        "x-export-filename": encodeURIComponent(expectedName),
        "x-chart-render-stats": JSON.stringify({ fe: 2, backend_ok: 1, failed: 0 }),
        "x-fix-stats": JSON.stringify({ formulas: 1, replacement_chars: 0, control_chars: 0 }),
      },
    };
    const probe = installDownloadProbes();

    renderPage();
    await gotoExportTab();
    fireEvent.click(findExportButton("导出 DOCX")!);

    // ① 真正发起了 DOCX 导出请求
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
    // ③ 成功后刷新缓存状态（exportApi.cacheStatus 被调）
    await waitFor(() =>
      expect(apiCalls["exportApi.cacheStatus"]?.length).toBeGreaterThanOrEqual(1),
    );
    // ① 后端百分号编码文件名被解码并用作 a.download（而非兜底 `方案.docx`）
    expect(probe.anchors.some((a) => a.download === expectedName)).toBe(true);
    // ② 触发了下载副作用（Blob → URL.createObjectURL）
    expect(probe.createObjUrl).toHaveBeenCalled();
    probe.restore();
  });

  it("导出 PDF 成功：同样解码文件名 + 刷新缓存状态（与 DOCX 共用命名轨）", async () => {
    const expectedName = "测试方案 2026-10-03 第1轮.pdf";
    apiDefaults["exportApi.pdf"] = {
      data: new Uint8Array([37, 80, 68, 70]).buffer, // %PDF 头
      headers: {
        "x-export-filename": encodeURIComponent(expectedName),
        "x-chart-render-stats": JSON.stringify({ fe: 0, backend_ok: 3, failed: 0 }),
      },
    };
    const probe = installDownloadProbes();

    renderPage();
    await gotoExportTab();
    fireEvent.click(findExportButton("导出 PDF")!);

    await waitFor(() => expect(apiCalls["exportApi.pdf"]?.length).toBe(1));
    await waitFor(() =>
      expect(apiCalls["exportApi.cacheStatus"]?.length).toBeGreaterThanOrEqual(1),
    );
    expect(probe.anchors.some((a) => a.download === expectedName)).toBe(true);
    expect(probe.createObjUrl).toHaveBeenCalled();
    probe.restore();
  });

  it("导出成功：统计头包含失败图也不抛异常（failed>0 走 warning 分支）", async () => {
    apiDefaults["exportApi.docx"] = {
      data: new Uint8Array([80, 75, 3, 4]).buffer,
      headers: {
        "x-export-filename": encodeURIComponent("测试方案 2026-10-03 第1轮.docx"),
        // ✅ failed>0：前端应解析成功并调用 msg.warning，而非抛错中断下载
        "x-chart-render-stats": JSON.stringify({ fe: 1, backend_ok: 2, failed: 3 }),
      },
    };
    const probe = installDownloadProbes();
    renderPage();
    await gotoExportTab();
    fireEvent.click(findExportButton("导出 DOCX")!);
    await waitFor(() => expect(apiCalls["exportApi.docx"]?.length).toBe(1));
    // 失败图分支不应阻断下载：仍刷新缓存状态 + 触发下载
    await waitFor(() =>
      expect(apiCalls["exportApi.cacheStatus"]?.length).toBeGreaterThanOrEqual(1),
    );
    expect(probe.anchors.length).toBeGreaterThan(0);
    probe.restore();
  });
});
