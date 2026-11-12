// R223（2026-11-12）目录生成模块第 16 轮 —— 前端 9 项修复护栏。
//
// 范式：vite `?raw` 源码锚（零新增依赖、不执行被读模块；R216/R218 先例）。
// 行为面说明：以下修复全部位于页面级副作用流（SSE 循环 / 恢复协程 / 切方案
// effect），DOM 级行为测试需整页挂载 + 网络桩，成本与脆弱度远超收益；源码锚
// 锁「判据表达式 + 调用点 + 标记注释」三要素，任何一项漂移即红。

import { describe, expect, it } from "vitest";
// ✅ R213 先例：?raw 导入一律裸写，不加 suppress 指令（vite/client 已声明 *?raw）
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import genSrc from "../pages/schemeWorkbench/useOutlineGeneration.tsx?raw";
import recoverySrc from "../pages/schemeWorkbench/useUnknownTaskRecovery.tsx?raw";
import factsSrc from "../pages/schemeWorkbench/useFactsGeneration.ts?raw";
import pollingSrc from "../pages/schemeWorkbench/taskPolling.ts?raw";
import actionsSrc from "../pages/schemeWorkbench/useOutlineActions.tsx?raw";
import treePanelSrc from "../pages/schemeWorkbench/SchemeTreePanel.tsx?raw";

const R223 = "R223";

describe("R223 FE-1 · 未知态接管 outline 终态不得被通用 load() 冲树", () => {
  it("outline 类型跳过通用 load 分支（页面 outlineRestoreFnRef 全权负责）", () => {
    expect(recoverySrc).toContain('if (ut.genType !== "outline") {');
    expect(recoverySrc).toContain("onOutlineResult?.(fin)");
    // onOutlineResult 必须在通用分支**之前**调用（先恢复树再谈刷新）
    const restoreIdx = recoverySrc.indexOf("onOutlineResult?.(fin)");
    const guardIdx = recoverySrc.indexOf('if (ut.genType !== "outline") {');
    expect(restoreIdx).toBeGreaterThan(0);
    expect(guardIdx).toBeGreaterThan(restoreIdx);
  });

  it("content/facts 通用 load 语义保留", () => {
    expect(recoverySrc).toContain('msg.success("后台任务已完成，正在刷新")');
    expect(recoverySrc).toContain("load()");
  });
});

describe("R223 FE-2 · markUnknownTask 世代守卫（三条生成链 + 接管循环）", () => {
  it("outline 重挂链：标记必须带世代校验", () => {
    expect(genSrc).toContain("if (hit && !hit.final && genSeqRef.current === mySeq) {");
    expect(genSrc).toContain(R223);
  });

  it("facts 重挂链：同口径补守卫", () => {
    expect(factsSrc).toContain("if (hit && !hit.final && genSeqRef.current === mySeq) {");
  });

  it("刷新接管循环：耗尽与网络失败两处收尾都带取消+世代校验", () => {
    const hits = pageSrc.split("!cancelled && genSeqRef.current === takeoverSeq").length - 1;
    expect(hits).toBeGreaterThanOrEqual(2);
  });
});

describe("R223 FE-3 · 未知态方案归属", () => {
  it("UnknownTaskInfo 声明 schemeId 字段", () => {
    expect(pollingSrc).toContain("schemeId?: string");
  });

  it("接管入口校验归属，异方案任务拒绝并清除", () => {
    expect(recoverySrc).toContain("ut.schemeId && ut.schemeId !== id");
    const guardIdx = recoverySrc.indexOf("ut.schemeId && ut.schemeId !== id");
    expect(recoverySrc.slice(guardIdx, guardIdx + 200)).toContain("clearUnknownTask()");
  });

  it("各标记点携带 schemeId", () => {
    expect(genSrc).toContain("schemeId: id");
    expect(factsSrc).toContain("schemeId: id");
    // 页面 4 个标记点（接管 2 + 正文链 2）
    const hits = pageSrc.split("schemeId: id,").length - 1;
    expect(hits).toBeGreaterThanOrEqual(4);
  });
});

describe("R223 FE-4 · 切方案复位块清空目录树", () => {
  it("D-02 复位块补 setTree([])", () => {
    // 复位块锚点：setCrSummary(null) 仅出现在 D-02 复位块（页面唯一）
    const anchor = pageSrc.indexOf("setCrSummary(null);");
    expect(anchor).toBeGreaterThan(0);
    const window = pageSrc.slice(anchor, anchor + 1400);
    expect(window).toContain("setTree([])");
    expect(window).toContain("clearUnknownTask()");
  });
});

describe("R223 FE-5 · error 分支消费截断/复用信号", () => {
  it("error 事件处理补 outlineTruncationNotice + resumed_chapters", () => {
    const errIdx = genSrc.indexOf('if (evt.event === "error") {');
    expect(errIdx).toBeGreaterThan(0);
    const window = genSrc.slice(errIdx, errIdx + 2400);
    expect(window).toContain("outlineTruncationNotice(evt)");
    expect(window).toContain("evt.resumed_chapters");
  });
});

describe("R223 FE-6 · 左树重命名预填取原始标题", () => {
  it("预填回查原始模型树而非 JSX title", () => {
    expect(treePanelSrc).toContain("findSectionById(p.tree, String(node.key))");
    expect(treePanelSrc).toContain("import {");
    expect(treePanelSrc).toContain("findSectionById,");
  });
});

describe("R223 FE-7/FE-8 · 导出目录树与入库的在飞锁", () => {
  it("exportTree 专属 ref 置位并在 finally 释放", () => {
    expect(actionsSrc).toContain("const exportTreeInFlightRef = useRef(false)");
    expect(actionsSrc).toContain("if (!id || exportTreeInFlightRef.current) return;");
    expect(actionsSrc).toContain("exportTreeInFlightRef.current = true;");
    expect(actionsSrc).toContain("exportTreeInFlightRef.current = false;");
  });

  it("saveAsLibrary 在飞锁 + finally 释放", () => {
    expect(actionsSrc).toContain("const saveLibInFlightRef = useRef(false)");
    expect(actionsSrc).toContain("if (!lastUpload || saveLibInFlightRef.current) return;");
    expect(actionsSrc).toContain("saveLibInFlightRef.current = true;");
    const finallyIdx = actionsSrc.indexOf("saveLibInFlightRef.current = false;");
    expect(finallyIdx).toBeGreaterThan(0);
  });
});

describe("R223 FE-9 · 保存后幽灵选中清除", () => {
  it("load() 中 findSectionById 落空时清空 selectedSection", () => {
    const anchor = pageSrc.indexOf("const fresh = findSectionById(newTree, currentSelected.key!)");
    expect(anchor).toBeGreaterThan(0);
    const window = pageSrc.slice(anchor, anchor + 500);
    expect(window).toContain("else setSelectedSection(null);");
  });
});
