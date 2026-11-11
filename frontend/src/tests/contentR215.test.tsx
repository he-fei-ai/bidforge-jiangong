// @vitest-environment jsdom
/**
 * R215 · 正文生成模块前端护栏（2026-11-11）。
 *
 * 覆盖：
 * - F1  切方案不复位 selectedSection / crSummary ⇒ 跨方案串台 + 跨方案写库（P1）
 * - F2  轻量轮询整树替换丢 content ⇒ 「已生成」与「尚未生成」同卡矛盾（P2）
 * - F3  section_done 体积闸门降级握手 truncated 零消费（P2）
 * - F5  终态 order_check（R105 顺序失守信号）零消费（P3）
 * - F6  ElapsedTimer 恒裸秒，与进度卡 fmtDuration 口径割裂（P3）
 * - 字节体检：本轮触碰的 4 个前端源文件 LF / 无 U+FFFD（.gitattributes eol=lf）
 *
 * 源码级断言走 vite `?raw`（不执行被读模块、零新增依赖），防回流。
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { ElapsedTimer } from "../pages/schemeWorkbench/ElapsedTimer";
import { mergeTreeContents, type TreeNode } from "../pages/schemeWorkbench/outlineTreeModel";
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import typesSrc from "../pages/schemeWorkbench/workbenchTypes.ts?raw";
import timerSrc from "../pages/schemeWorkbench/ElapsedTimer.tsx?raw";
import treeModelSrc from "../pages/schemeWorkbench/outlineTreeModel.ts?raw";

// ---------------------------------------------------------------------------
// F2 · mergeTreeContents（纯函数行为级）
// ---------------------------------------------------------------------------

function n(key: string, over: Partial<TreeNode> = {}): TreeNode {
  return { key, title: key, ...over } as TreeNode;
}

describe("mergeTreeContents（R215 · 轻量轮询 content 回填）", () => {
  it("旧树有 content 而新树缺失 → 回填", () => {
    const prev = [n("1", { content: "旧正文" })];
    const next = [n("1")]; // 轻量轮询节点无 content
    const merged = mergeTreeContents(prev, next);
    expect(merged[0].content).toBe("旧正文");
  });

  it("新树已有 content → 以新树为准（完整 load 树权威）", () => {
    const prev = [n("1", { content: "旧正文" })];
    const next = [n("1", { content: "新正文" })];
    expect(mergeTreeContents(prev, next)[0].content).toBe("新正文");
  });

  it("旧树整体无 content → 直接返回 next 引用（轻量轮询首启零拷贝）", () => {
    const prev = [n("1")];
    const next = [n("1", { word_count: 100 })];
    expect(mergeTreeContents(prev, next)).toBe(next);
  });

  it("递归回填子树", () => {
    const prev = [n("1", { content: "父", children: [n("1.1", { content: "子正文" })] })];
    const next = [n("1", { children: [n("1.1")] })];
    const merged = mergeTreeContents(prev, next);
    expect(merged[0].children?.[0].content).toBe("子正文");
  });
});

// ---------------------------------------------------------------------------
// F6 · ElapsedTimer 进位口径（组件级）
// ---------------------------------------------------------------------------

describe("ElapsedTimer（R215 · fmtDuration 进位）", () => {
  it("超过 60 秒显示「N分N秒」而非裸秒", () => {
    const start = Date.now() - 247_000; // 4分7秒
    const { container } = render(<ElapsedTimer startTime={start} />);
    expect(container.textContent).toContain("4分7秒");
    expect(container.textContent).not.toContain("247s");
  });

  it("60 秒内仍显示秒级（口径不回归）", () => {
    const start = Date.now() - 42_000;
    const { container } = render(<ElapsedTimer startTime={start} />);
    expect(container.textContent).toContain("42秒");
  });
});

// ---------------------------------------------------------------------------
// F1 / F3 / F5 · 源码锁（?raw，防回流）
// ---------------------------------------------------------------------------

describe("SchemeWorkbenchPage 源码锁（R215）", () => {
  it("F1：切方案复位块必须包含 selectedSection / crSummary 清空", () => {
    const anchor = pageSrc.indexOf("正文选中章与生成摘要跨方案复位");
    expect(anchor).toBeGreaterThan(-1);
    const block = pageSrc.slice(anchor, anchor + 1200);
    expect(block).toContain("setSelectedSection(null);");
    expect(block).toContain("setCrSummary(null);");
  });

  it("F1：load() 不再把旧 key 找不到时的残留当作有效选中（复位块在切方案 effect 内）", () => {
    // 复位块必须先于 R191 列表清空注释出现（切方案 effect body 顺序锚）
    const resetAt = pageSrc.indexOf("setSelectedSection(null);");
    const listClearAt = pageSrc.indexOf("✅ R191 修复（切方案列表残留）");
    expect(resetAt).toBeGreaterThan(-1);
    expect(listClearAt).toBeGreaterThan(-1);
    expect(resetAt).toBeLessThan(listClearAt);
  });

  it("F3：section_done 分支必须消费 truncated（不落截断内容）", () => {
    expect(pageSrc).toContain("evt.truncated === true");
    expect(pageSrc).toContain("正文超长，预览已截断显示");
  });

  it("F5：终态分支必须消费 order_check.ok === false（失守才说话）", () => {
    expect(pageSrc).toContain("evt.order_check && evt.order_check.ok === false");
  });

  it("F2：轻量轮询必须经 mergeTreeContents 回填 content", () => {
    expect(pageSrc).toContain("mergeTreeContents(prev, newTree)");
  });
});

describe("workbenchTypes 类型契约（R215）", () => {
  it("F3/F5：ContentGenEvent 必须声明 truncated 与 order_check", () => {
    expect(typesSrc).toContain("truncated?: boolean");
    expect(typesSrc).toContain("order_check?: { ok?: boolean; [k: string]: unknown }");
  });
});

describe("ElapsedTimer / outlineTreeModel 源码锚（R215）", () => {
  it("F6：ElapsedTimer 不得回流裸秒形态", () => {
    expect(timerSrc).toContain("fmtDuration(");
    expect(timerSrc).not.toContain("}s\n");
    expect(timerSrc).not.toContain("/ 1000)}s");
  });

  it("F2：mergeTreeContents 必须是纯函数（不改入参）", () => {
    expect(treeModelSrc).toContain("export function mergeTreeContents(prev: TreeNode[], next: TreeNode[]): TreeNode[]");
    // 旧树整体无 content 时零拷贝直返 next
    expect(treeModelSrc).toContain("if (!prevContent.size) return next;");
  });
});

// ---------------------------------------------------------------------------
// 字节体检（前端 LF 规范，.gitattributes eol=lf）
// ---------------------------------------------------------------------------

describe("字节体检（R215 前端触碰文件）", () => {
  const cases: Array<[string, string]> = [
    ["SchemeWorkbenchPage.tsx", pageSrc],
    ["workbenchTypes.ts", typesSrc],
    ["ElapsedTimer.tsx", timerSrc],
    ["outlineTreeModel.ts", treeModelSrc],
  ];
  it.each(cases)("%s 纯 LF / 无 BOM / 无 U+FFFD", (_name, src) => {
    expect(src.includes("\r")).toBe(false);
    expect(src.includes("\uFFFD")).toBe(false);
    expect(src.charCodeAt(0)).not.toBe(0xfeff);
  });
});
