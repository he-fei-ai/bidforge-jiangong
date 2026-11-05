/** R175 G-3：空壳一级章提示纯函数 + 页面接线锚点测试（2026-11-03）
 *
 * 覆盖 utils/emptyTopChapters.ts（派生纯函数层）与
 * SchemeWorkbenchPage 的接线（notifyGenericResidual 折入 + 手动保存逃生舱）。
 * 仿 genericResidualR86.test.ts 结构：纯函数矩阵 + 源码锚点断言。
 */
import { describe, expect, it } from "vitest";
import { emptyTopChapterHits, emptyTopChaptersNotice } from "../utils/emptyTopChapters";

describe("emptyTopChapterHits · 响应片段提取", () => {
  it("正常数组原样返回", () => {
    const hits = [{ index: 1, title: "施工部署" }];
    expect(emptyTopChapterHits({ empty_top_chapters: hits })).toEqual(hits);
  });
  it("旧后端 / 干净目录（无键）⇒ 空数组（向后兼容）", () => {
    expect(emptyTopChapterHits({ count: 2 })).toEqual([]);
    expect(emptyTopChapterHits({})).toEqual([]);
    expect(emptyTopChapterHits(null)).toEqual([]);
    expect(emptyTopChapterHits(undefined)).toEqual([]);
  });
  it("脏数据（非数组）⇒ 空数组，不抛异常", () => {
    expect(emptyTopChapterHits({ empty_top_chapters: "x" })).toEqual([]);
    expect(emptyTopChapterHits({ empty_top_chapters: {} })).toEqual([]);
    expect(emptyTopChapterHits({ empty_top_chapters: 3 })).toEqual([]);
  });
});

describe("emptyTopChaptersNotice · 提示文案", () => {
  it("空数组 / 无键 ⇒ 空串（无提示，与旧版本一致）", () => {
    expect(emptyTopChaptersNotice({ empty_top_chapters: [] })).toBe("");
    expect(emptyTopChaptersNotice({})).toBe("");
  });
  it("单个空壳章：章号（1 起算）+ 标题 + 处置建议", () => {
    const text = emptyTopChaptersNotice({
      empty_top_chapters: [{ index: 1, title: "施工部署" }],
    });
    expect(text).toContain("1 个一级章暂无子章节");
    expect(text).toContain("第2章「施工部署」");
    expect(text).toContain("补充二级章节");
  });
  it("无标题空壳章回退「第N章」，脏 index 回退「某一级章」", () => {
    const text = emptyTopChaptersNotice({
      empty_top_chapters: [{ index: 0, title: "" }, { title: "乙" }],
    });
    expect(text).toContain("第1章");
    expect(text).toContain("某一级章");
  });
  it("超过 5 章折叠为「等 N 章」", () => {
    const hits = Array.from({ length: 7 }, (_, i) => ({ index: i, title: `章${i}` }));
    const text = emptyTopChaptersNotice({ empty_top_chapters: hits });
    expect(text).toContain("7 个一级章");
    expect(text).toContain("等 7 章");
    expect(text).not.toContain("章6」"); // 第 6 个（0 起）不展示
  });
  it("index 为 0（第一章）正确显示第1章，不做 0 值误判", () => {
    const text = emptyTopChaptersNotice({
      empty_top_chapters: [{ index: 0, title: "甲" }],
    });
    expect(text).toContain("第1章「甲」");
  });
});

describe("页面接线锚点（源码断言，防接线回退）", () => {
  // 仿 genericResidualR86：import.meta.glob ?raw 读页面源码（不依赖 node:fs）
  // ✅ 2026-11-04 拆分（G4 续）：目录闸门与写入提示迁至
  //    pages/schemeWorkbench/useOutlineGate.tsx，判据语义不变，故纳入 glob 范围。
  const pages = import.meta.glob(
    ["../pages/*.tsx", "../pages/schemeWorkbench/*.tsx", "../pages/schemeWorkbench/tabs/*.tsx"],
    { query: "?raw", import: "default" },
  );

  it("notifyGenericResidual 折入 emptyTopChaptersNotice（7 个调用点零改动生效）", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    expect(src).toContain('utils/emptyTopChapters"');
    expect(src).toContain("emptyTopChaptersNotice(data)");
  });

  it("手动保存 catch 接空章闸门逃生舱（稳定标记 + allow_empty_chapters 重发）", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    expect(src).toContain('message.includes("无子章节的一级章")');
    expect(src).toContain("{ allowEmptyChapters: true }");
    expect(src).toContain("allow_empty_chapters: true");
  });

  it("AI 链路（doSaveGate）不加逃生舱 —— 部分成果保存不得被二次阻断", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    // doSaveGate 函数体内不得出现闸门标记判断（仅手动保存路径有）
    const fnStart = src.indexOf("async function doSaveGate");
    const fnEnd = src.indexOf("load();", fnStart);
    const body = src.slice(fnStart, fnEnd + 8);
    expect(body).not.toContain("无子章节的一级章");
  });
});
