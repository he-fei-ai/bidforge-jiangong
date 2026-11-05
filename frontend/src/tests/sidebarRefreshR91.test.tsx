/// <reference types="vite/client" />
/**
 * R91 · 侧栏项目树实时刷新与新建方案入口护栏（2026-10-17）。
 *
 * 背景（用户报障 + 浏览器实测复现）：
 * ① 新建项目、在新建项目下新建/删除方案后，左侧项目列表不及时更新 ——
 *    根因：Layout 的 ProjectTreeMenu 只在挂载时 loadProjects 一次，
 *    loadedProjectIdsRef 对已加载项目**永久去重**，且 Layout 包裹整个
 *    Routes（应用内跳转不重挂载）→ 只有 F5 整页刷新才更新。
 * ② 点击侧栏「进入项目新建方案」占位项无任何反应 —— 根因：占位项
 *    disabled:true 且 onClick 显式排除 /create-scheme、/empty 结尾 key，
 *    是「看着能点、点了没反应」的死占位（用户表述「未能进入编辑状态」）。
 *
 * 修复：sidebarEvents 最小 pub/sub + ProjectTreeMenu 订阅重取（绕过去重）；
 * 占位项改可点并带 ?create=1 直达项目详情自动打开新建方案弹窗。
 *
 * 判据实现沿用本仓既有范式（importParseWarningWiringR72）：
 * 行为逻辑收敛在纯函数（computeRefreshTargets / resolveTreeClick）直接测，
 * 接线部分用 import.meta.glob(..., { query: "?raw" }) 静态源码锁。
 */
import { describe, it, expect, vi } from "vitest";
import {
  subscribeSidebarTreeRefresh,
  notifySidebarTreeRefresh,
  computeRefreshTargets,
  resolveTreeClick,
} from "../utils/sidebarEvents";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const LAYOUT = RAW["components/Layout.tsx"];
const DETAIL = RAW["pages/ProjectDetailPage.tsx"];
const LIST = RAW["pages/ProjectListPage.tsx"];

describe("R91 · sidebarEvents 事件总线（订阅/通知/取消订阅）", () => {
  it("notify 携带 projectId 透传给订阅者；unsubscribe 后不再收", () => {
    const seen: Array<string | undefined> = [];
    const off = subscribeSidebarTreeRefresh((pid) => {
      seen.push(pid);
    });
    notifySidebarTreeRefresh("p1");
    notifySidebarTreeRefresh();
    off();
    notifySidebarTreeRefresh("p2");
    expect(seen).toEqual(["p1", undefined]);
  });

  it("单个订阅者抛错不影响其它订阅者（fail-soft 隔离）", () => {
    const good = vi.fn();
    const offBad = subscribeSidebarTreeRefresh(() => {
      throw new Error("boom");
    });
    const offGood = subscribeSidebarTreeRefresh(good);
    expect(() => notifySidebarTreeRefresh("x")).not.toThrow();
    expect(good).toHaveBeenCalledWith("x");
    offBad();
    offGood();
  });
});

describe("R91 · computeRefreshTargets（绕过永久去重的重取集合）", () => {
  it("并集：已加载 ∪ 展开键项目 ∪ 事件目标；去重且非法展开键被忽略", () => {
    const t = computeRefreshTargets(
      ["a", "b"],
      ["/project/b", "/project/c", "/outline-library", "/project/d/loading"],
      "e",
    );
    expect(new Set(t)).toEqual(new Set(["a", "b", "c", "d", "e"]));
    expect(t.length).toBe(5); // 无重复
  });

  it("全空输入返回空数组（不猜测、不放大请求面）", () => {
    expect(computeRefreshTargets([], [])).toEqual([]);
  });
});

describe("R91 · resolveTreeClick（占位项可点直达新建方案，症状②）", () => {
  it("「进入项目新建方案 / 暂无方案」占位 → 跳项目详情带 ?create=1", () => {
    expect(resolveTreeClick("/project/p-1/create-scheme")).toBe("/project/p-1?create=1");
    expect(resolveTreeClick("/project/p-1/empty")).toBe("/project/p-1?create=1");
  });

  it("「加载中...」占位无反应；方案/菜单/项目键原样放行；非字符串安全", () => {
    expect(resolveTreeClick("/project/p-1/loading")).toBeNull();
    expect(resolveTreeClick("/project/p-1")).toBe("/project/p-1");
    expect(resolveTreeClick("/scheme/s-1")).toBe("/scheme/s-1");
    expect(resolveTreeClick("/settings/ai")).toBe("/settings/ai");
    expect(resolveTreeClick(undefined)).toBeNull();
    expect(resolveTreeClick(42)).toBeNull();
  });
});

describe("R91 · 静态接线锁（症状①：写入点必须 notify，侧栏必须订阅）", () => {
  it("前提：三个被测文件都能取到（防 glob 空跑成常绿）", () => {
    expect(LAYOUT).toBeTruthy();
    expect(DETAIL).toBeTruthy();
    expect(LIST).toBeTruthy();
  });

  it("Layout：ProjectTreeMenu 订阅总线，且重取前清空 loadedProjectIdsRef 去重", () => {
    expect(LAYOUT).toContain("subscribeSidebarTreeRefresh");
    expect(LAYOUT).toContain("computeRefreshTargets");
    expect(LAYOUT).toContain("loadedProjectIdsRef.current.clear()");
    expect(LAYOUT).toContain("resolveTreeClick");
  });

  it("Layout：两个占位项已去掉 disabled:true（死占位改可点）", () => {
    // create-scheme 与 empty 两处占位都不得再带 disabled
    const createBlock = LAYOUT.slice(
      LAYOUT.indexOf("key: `${projectKey}/create-scheme`"),
      LAYOUT.indexOf("key: `${projectKey}/loading`"),
    );
    const emptyBlock = LAYOUT.slice(
      LAYOUT.indexOf("key: `${projectKey}/empty`"),
      LAYOUT.indexOf("schemes.map"),
    );
    expect(createBlock.length).toBeGreaterThan(20);
    expect(emptyBlock.length).toBeGreaterThan(20);
    // 按属性形态检查（注释里「原 disabled:true」的字样不构成属性，不算残留）
    expect(createBlock).not.toContain("disabled: true");
    expect(emptyBlock).not.toContain("disabled: true");
  });

  it("ProjectDetailPage：五个方案写入口全部 notify(id)", () => {
    expect(DETAIL).toContain("import { notifySidebarTreeRefresh }");
    for (const anchor of [
      "schemesApi.create(id!, payload);",
      "已复制目录结构（副本正文需重新生成）",
      "归档状态徽标同步左侧项目树",
      "改名/改类型同步左侧项目树",
      "删除即时从左侧项目树消失",
    ]) {
      expect(DETAIL).toContain(anchor);
    }
    // 通知调用至少 5 处（create/duplicate/archive/update/delete）
    const calls = DETAIL.match(/notifySidebarTreeRefresh\(id\)/g) || [];
    expect(calls.length).toBeGreaterThanOrEqual(5);
  });

  it("ProjectDetailPage：?create=1 自动打开新建弹窗并 replace 清理 URL", () => {
    expect(DETAIL).toContain("useSearchParams");
    expect(DETAIL).toContain('searchParams.get("create") === "1"');
    expect(DETAIL).toContain("setModalOpen(true)");
    expect(DETAIL).toContain("navigate(`/project/${id}`, { replace: true })");
  });

  it("ProjectListPage：项目新建/改名与删除两处 notify", () => {
    expect(LIST).toContain("import { notifySidebarTreeRefresh }");
    const calls = LIST.match(/notifySidebarTreeRefresh\(\)/g) || [];
    expect(calls.length).toBeGreaterThanOrEqual(2);
  });
});
