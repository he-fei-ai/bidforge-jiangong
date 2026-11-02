// @vitest-environment jsdom
/**
 * 侧栏「项目列表」默认全部折叠收缩（2026-10-30 · 用户指令）。
 *
 * 需求：左侧菜单栏的项目列表**只显示工程名称**，所有方案默认折叠收缩。
 *
 * 修复前：`ProjectTreeMenu.loadProjects` 挂载时把所有 `scheme_count > 0` 的项目
 * 一次性 `setExpandedKeys` 展开，并逐个预拉方案列表（schemesApi.list × N）——
 * 项目/方案一多，整列首屏被方案名淹没，要滚动才能看到其它工程；且首屏就打出
 * N 个方案请求。
 *
 * 修复后：
 *   ① 挂载时不展开任何项目、不预拉任何方案（懒加载到 onOpenChange）；
 *   ② 只有进入**方案**页才自动展开其所属项目（否则当前方案在折叠树里不可见），
 *      进入「项目详情」页不展开。
 *
 * 观测点（不依赖 jest-dom 匹配器，沿用本仓结构断言风格）：
 *   · `.ant-menu-submenu-open` 的实例数 = 当前展开的项目数；
 *   · `schemesApi.list` 的调用记录 = 是否发生「预拉 / 懒加载」。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "../components/Layout";

const P1 = "上海市市级机关第二幼儿园整体维修工程";
const P2 = "生活楼新增电梯井工程";
const S1 = "精装修工程专项施工方案";
const S2 = "落地脚手架专项施工方案";
const S3 = "模板工程专项施工方案";

const state = vi.hoisted(() => ({
  projects: [
    { id: "p1", name: "上海市市级机关第二幼儿园整体维修工程", scheme_count: 2 },
    { id: "p2", name: "生活楼新增电梯井工程", scheme_count: 1 },
  ],
  schemes: {
    p1: [
      { id: "s1", name: "精装修工程专项施工方案", status: "审核中" },
      { id: "s2", name: "落地脚手架专项施工方案", status: "审核中" },
    ],
    p2: [{ id: "s3", name: "模板工程专项施工方案", status: "审核中" }],
  } as Record<string, Array<{ id: string; name: string; status?: string }>>,
  listCalls: [] as string[],
}));

// 侧栏会拉取项目/方案列表：只隔离这两个列表接口，其余 API 保持真实
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    projectsApi: {
      ...actual.projectsApi,
      list: vi.fn(async () => ({ data: { items: state.projects } })),
    },
    schemesApi: {
      ...actual.schemesApi,
      list: vi.fn(async (projectId: string) => {
        state.listCalls.push(projectId);
        return { data: { items: state.schemes[projectId] || [] } };
      }),
    },
  };
});

// TaskStatusBar 内含常驻 SSE + 轮询，jsdom 下不可用 → 桩掉
vi.mock("../components/TaskStatusBar", async () => {
  const R = await import("react");
  return { default: () => R.createElement("div", { "data-testid": "task-status-bar" }) };
});

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Drawer / Menu）会用到
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
if (!(window as any).ResizeObserver) {
  (window as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

/** 当前展开的项目数（antd Menu 折叠时无 ant-menu-submenu-open 类） */
const openProjects = () => document.querySelectorAll(".ant-menu-submenu-open").length;

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Layout>
        <div>内容区</div>
      </Layout>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  state.listCalls.length = 0;
  // 桌面宽度：避免 Layout 的响应式逻辑把整条侧栏自动折叠掉
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: 1440,
  });
});

afterEach(() => {
  cleanup();
});

describe("侧栏项目列表 · 默认折叠（只显示工程名称）", () => {
  it("首页：只渲染工程名称，所有方案折叠且不预拉方案（懒加载）", async () => {
    renderAt("/");
    expect(await screen.findByText(P1)).toBeTruthy();
    expect(screen.getByText(P2)).toBeTruthy();

    // ① 所有项目默认折叠
    expect(openProjects()).toBe(0);
    // ② 方案名不在列表中出现
    expect(screen.queryByText(S1)).toBeNull();
    expect(screen.queryByText(S2)).toBeNull();
    expect(screen.queryByText(S3)).toBeNull();
    // ③ 没有预拉任何项目的方案列表
    expect(state.listCalls).toEqual([]);
  });

  it("点击工程节点才展开并懒加载该项目的方案（其余项目仍折叠）", async () => {
    renderAt("/");
    fireEvent.click(await screen.findByText(P1));

    await waitFor(() => expect(openProjects()).toBe(1));
    // 只请求被展开的那个项目
    expect(state.listCalls).toEqual(["p1"]);
    expect(await screen.findByText(S1)).toBeTruthy();
    expect(screen.getByText(S2)).toBeTruthy();
    // 未展开的项目其方案仍不在列表里
    expect(screen.queryByText(S3)).toBeNull();
  });

  it("项目详情页：不自动展开该项目（保持只显示工程名称）", async () => {
    renderAt("/project/p1");
    expect(await screen.findByText(P1)).toBeTruthy();
    expect(openProjects()).toBe(0);
    expect(state.listCalls).toEqual([]);
  });

  it("方案页：自动展开其所属项目（当前方案在树中可见），其它项目仍折叠", async () => {
    renderAt("/scheme/s1");
    // 深链/刷新直达：先并发补齐各项目方案 → 定位到 p1 → 展开 p1
    expect(await screen.findByText(S1)).toBeTruthy();
    expect(openProjects()).toBe(1);
    expect(screen.queryByText(S3)).toBeNull();
  });
});
