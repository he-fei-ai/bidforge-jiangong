/**
 * R231 目录生成模块深度探索增强 · OMP 旁路接缝前端护栏（2026-11-13）。
 *
 * 本轮定罪并修复的三处前端缺口（不锁就会被后续顺手改回去）：
 *   1. D-02 复位块漏 setGeneratingOmp(false) —— omp 态是页面级布尔（R193 D-02
 *      家族新成员，omp 状态晚于复位块诞生而漏登记），切方案后旧方案的 omp 在飞
 *      窗口内新方案全部目录按钮被禁用数分钟；
 *   2. 树行内结构操作（新增/上移/下移/删除/拖拽/改名/预算面板）未接 omp 门禁 ——
 *      后端双 409 感知不到 omp 旁路（可行性报告 §11.4 局限 B），omp 整表重建前
 *      的用户操作会被静默覆盖（改名的标题匹配不到旧路径还有正文丢失面）；
 *   3. convertToEditTree 闭包引用 ompBusy，deps 漏登记会捕获初值（stale closure：
 *      omp 结束后门禁不解除）。
 */
import { describe, expect, it } from "vitest";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import outlineTabSource from "../pages/schemeWorkbench/tabs/OutlineTab.tsx?raw";
import treeActionsSource from "../pages/schemeWorkbench/OutlineTreeActions.tsx?raw";
import editTreeRenderSource from "../pages/schemeWorkbench/useOutlineEditTreeRender.tsx?raw";

const pageSrc: string = pageSource;
const outlineSrc: string = outlineTabSource;
const actionsSrc: string = treeActionsSource;
const renderSrc: string = editTreeRenderSource;

describe("R231 omp 目录旁路 · D-02 复位块 omp 态", () => {
  it("切方案复位块包含 setGeneratingOmp(false)（handler finally + 复位块两处）", () => {
    expect(
      (pageSrc.match(/setGeneratingOmp\(false\)/g) || []).length,
      "omp 生成态复位点不足（handler finally + 切方案复位块）",
    ).toBeGreaterThanOrEqual(2);
  });

  it("编辑树渲染钩子接线 ompBusy（页面传入 generatingOmp）", () => {
    expect(pageSrc).toContain("ompBusy: generatingOmp");
  });
});

describe("R231 omp 目录旁路 · 树行内操作门禁", () => {
  it("OutlineTreeActions 支持 disabled 形参且四个结构按钮全部接线", () => {
    expect(actionsSrc).toContain("disabled?: boolean");
    // 新增 / 上移 / 下移 / 删除 四按钮判据都含 disabled 前置（缺省 false 既有调用零变化）
    expect((actionsSrc.match(/disabled=\{disabled \|\|/g) || []).length).toBeGreaterThanOrEqual(4);
  });

  it("OutlineTab 把 generatingOmp 传给树操作区并禁用拖拽", () => {
    expect(outlineSrc).toContain("disabled={!!generatingOmp}");
    expect(outlineSrc).toContain("draggable={!generatingOmp}");
  });

  it("omp 生成期隐藏字数预算面板（章节可能被整表重建删除，PATCH 落 0 行）", () => {
    expect(outlineSrc).toContain("!generatingOmp && (");
  });

  it("改名入口与行内上/下移按钮接 ompBusy 门禁", () => {
    expect(renderSrc).toContain("ompBusy?: boolean");
    expect(renderSrc).toContain("if (ompBusy) return;");
    expect((renderSrc.match(/ompBusy \|\| !moveFlags/g) || []).length).toBeGreaterThanOrEqual(2);
  });

  it("convertToEditTree 的 useCallback deps 包含 ompBusy（防 stale closure）", () => {
    expect(
      renderSrc,
      "useCallback deps 漏 ompBusy ⇒ 闭包捕获初值，omp 结束后门禁不解除",
    ).toContain("renamingKey, ompBusy]");
  });
});

describe("R231 SSE 迟到代理回收 · 接管路径 status 分流", () => {
  it("刷新接管与未知态接管对 stopped 走部分成果手动确认（buildPartialOutlineHint）", () => {
    // 两条接管路径各一个 stopped 分流分支（buildPartialOutlineHint + modal.confirm）
    expect((pageSrc.match(/status === "stopped"\) \{/g) || []).length).toBeGreaterThanOrEqual(2);
    // 未知态接管分流（id 收窄形态）
    expect(pageSrc).toContain('fin.status === "stopped" && id');
    expect((pageSrc.match(/buildPartialOutlineHint\(\{/g) || []).length).toBeGreaterThanOrEqual(2);
  });

  it("stopped 分支文案不再声称「已完成」", () => {
    expect((pageSrc.match(/后台目录生成已停止，已生成部分成果/g) || []).length).toBe(2);
  });

  it("接管保存走 sectionsApi.saveOutline（source=ai 与重挂 stopped 分支同口径）", () => {
    // 刷新接管闭包用 id；未知态接管经 fin.status 分流 + id 收窄为 sid
    expect((pageSrc.match(/sectionsApi\.saveOutline\((?:id|sid), \{\s*outline: (?:ckpt\.outline|restoredOutline),\s*source: "ai",/g) || []).length)
      .toBeGreaterThanOrEqual(2);
  });
});
