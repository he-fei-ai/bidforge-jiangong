/**
 * R227 目录生成模块遗留收口 · 前端护栏（2026-11-13）。
 *
 * 覆盖：
 *   1. F5  buildPartialOutlineHint hasContent 警告变体（行为实证：error/stopped ×
 *      有正文/无正文，danger/okText 统一由纯函数生成，消费分支不再手拼）；
 *   2. F10 目录库编辑弹窗导入/套模板覆盖确认（源码锁：tree.length 时 confirm）；
 *   3. F11 目录库列表/详情请求无序守卫（源码锁：loadSeqRef/historySeqRef/detailSeqRef）；
 *   4. F12 版本回滚防双击（源码锁：restoreConfirmOpenRef 入口锁）；
 *   5. F14 目录树未保存修改 beforeunload（源码锁：hasUnsavedLocalNodes 纳入守卫）；
 *   6. D2  缺陷账本 stale 徽标（源码锁：summary-stale + defect-stale- 行徽标）。
 */
import { describe, expect, it } from "vitest";
import { buildPartialOutlineHint } from "../pages/schemeWorkbench/outlineTreeModel";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";
import editModalSource from "../components/OutlineLibraryEditModal.tsx?raw";
import libraryPageSource from "../pages/OutlineLibraryPage.tsx?raw";
import defectPanelSource from "../components/review/DefectLedgerPanel.tsx?raw";

const pageSrc: string = pageSource;
const editModalSrc: string = editModalSource;
const librarySrc: string = libraryPageSource;
const defectSrc: string = defectPanelSource;

// ---------------------------------------------------------------------------
// F5 · buildPartialOutlineHint hasContent 警告变体
// ---------------------------------------------------------------------------

describe("R227 F5 · buildPartialOutlineHint hasContent 变体（行为实证）", () => {
  it("hasContent=true + error：警告「重建章节结构/可能删除正文」+ okText danger", () => {
    const h = buildPartialOutlineHint({ event: "error", nodeCount: 5, hasContent: true });
    expect(h.title).toBe("目录生成中断，但有部分成果");
    expect(h.content).toContain("重建章节结构");
    expect(h.content).toContain("可能因章节无法匹配而被删除");
    expect(h.okText).toBe("仍要保存（可能删除正文）");
    expect(h.danger).toBe(true);
  });

  it("hasContent=true + stopped：保存可能删除正文 → danger=true", () => {
    const h = buildPartialOutlineHint({ event: "stopped", nodeCount: 3, hasContent: true });
    expect(h.title).toBe("目录生成已停止，但有部分成果");
    expect(h.content).toContain("重建章节结构");
    expect(h.danger).toBe(true);
  });

  it("hasContent=false/缺省：原行为（danger=false，okText=保存已生成部分）", () => {
    const h = buildPartialOutlineHint({ event: "error", nodeCount: 5 });
    expect(h.okText).toBe("保存已生成部分");
    expect(h.danger).toBe(false);
    expect(h.content).not.toContain("重建章节结构");
    expect(buildPartialOutlineHint({ event: "stopped", nodeCount: 2, hasContent: false }).danger).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// F10 · 目录库编辑弹窗导入/套模板覆盖确认
// ---------------------------------------------------------------------------

describe("R227 F10 · 目录库导入/套模板覆盖确认", () => {
  it("导入识别结果：当前树非空时弹覆盖确认（tree.length 前置）", () => {
    expect(editModalSrc).toContain("导入将替换当前目录");
    expect(editModalSrc).toContain("if (tree.length) {");
    expect(editModalSrc).toContain("if (!confirmed) return;");
  });

  it("套用标准模板：当前树非空时弹覆盖确认", () => {
    expect(editModalSrc).toContain("套用模板将替换当前目录");
    // 两处确认都用 countNodes(tree) 展示将被替换的节点数
    expect((editModalSrc.match(/countNodes\(tree\)/g) || []).length).toBeGreaterThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------------
// F11 · 目录库列表/详情请求无序守卫
// ---------------------------------------------------------------------------

describe("R227 F11 · 目录库请求无序守卫", () => {
  it("列表请求有 seq 守卫（快速改筛选/翻页丢弃旧响应）", () => {
    expect(librarySrc).toContain("const loadSeqRef = useRef(0);");
    expect(librarySrc).toContain("if (seq !== loadSeqRef.current) return;");
    expect(librarySrc).toContain("if (seq === loadSeqRef.current) setLoading(false);");
  });

  it("版本历史详情请求有 seq 守卫", () => {
    expect(librarySrc).toContain("const historySeqRef = useRef(0);");
    expect(librarySrc).toContain("if (seq !== historySeqRef.current) return;");
  });

  it("编辑弹窗详情请求有 seq 守卫（快速切换 libraryId）", () => {
    expect(editModalSrc).toContain("const detailSeqRef = useRef(0);");
    expect(editModalSrc).toContain("if (seq !== detailSeqRef.current) return;");
  });
});

// ---------------------------------------------------------------------------
// F12 · 版本回滚防双击
// ---------------------------------------------------------------------------

describe("R227 F12 · 版本回滚防双击双 undo 快照", () => {
  it("handleRestore 入口有 ref 锁（弹窗入口置位，onOk finally/onCancel 重置）", () => {
    expect(librarySrc).toContain("const restoreConfirmOpenRef = useRef(false);");
    expect(librarySrc).toContain("if (!historyData || restoreConfirmOpenRef.current) return;");
    expect(librarySrc).toContain("restoreConfirmOpenRef.current = true;");
    expect((librarySrc.match(/restoreConfirmOpenRef\.current = false;/g) || []).length).toBeGreaterThanOrEqual(2);
  });
});

// ---------------------------------------------------------------------------
// F14 · 目录树未保存修改 beforeunload
// ---------------------------------------------------------------------------

describe("R227 F14 · 目录树未保存修改 beforeunload", () => {
  it("beforeunload handler 纳入 hasUnsavedLocalNodes(tree)（正文与目录树未保存同提示）", () => {
    expect(pageSrc).toContain("const outlineDirty = hasUnsavedLocalNodes(tree);");
    expect(pageSrc).toContain("if ((isEditingSection && editDirty) || outlineDirty)");
    expect(pageSrc).toContain("}, [isEditingSection, editDirty, tree]);");
  });
});

// ---------------------------------------------------------------------------
// D2 · 缺陷账本 stale 徽标
// ---------------------------------------------------------------------------

describe("R227 D2 · 缺陷账本 stale 徽标", () => {
  it("summary 区有「章节已删除」计数 Tag（stale>0 时显示）", () => {
    expect(defectSrc).toContain("defect-summary-stale");
    expect(defectSrc).toContain("章节已删除 {num(summary.stale)}");
  });

  it("行状态/策略列有「章节已删除」徽标（row.stale === 1）", () => {
    expect(defectSrc).toContain("row.stale === 1");
    expect(defectSrc).toContain("defect-stale-");
  });

  it("stale 行操作禁用（执行/验证/人工接受/驳回）", () => {
    expect(defectSrc).toContain("const isStale = row.stale === 1;");
    expect(defectSrc).toContain("disabled={isStale}");
  });
});
