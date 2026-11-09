/**
 * R192（2026-11-09）目录生成模块写入口完备性收口 —— 前端护栏
 *  A degraded 终态重挂分支（FIX-R12：三支全不命中 → 哑态）
 *  B stopped 分支消费 failed_chapters（FIX-R13：在线 + 重挂两路）
 *  C 重挂 stopped 透传 fin.message（FIX-R14：检查点失败原因不丢）
 *  D buildTree 编号提取收敛 storedIdParts（FIX-R15：全角分叉闭合）
 *
 * 判据沿用本仓既有范式（aiConfigR169 / stopControlR106 / titleSanitizedR190）：
 * import.meta.glob 读源码文本，不引 node:fs。
 */
import { describe, expect, it } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v] as const),
);

const genSrc = RAW["pages/schemeWorkbench/useOutlineGeneration.tsx"] ?? "";
const treeSrc = RAW["pages/schemeWorkbench/outlineTreeModel.ts"] ?? "";

describe("degraded 终态重挂分支（R192 FIX-R12 源码锁）", () => {
  it("重挂终态分发存在 degraded 分支且带如实告警与收尾刷新", () => {
    expect(genSrc).toContain('fin?.status === "degraded"');
    expect(genSrc).toContain("成果未能写入检查点");
    expect(genSrc).toContain("检查点写入失败，后台成果不可恢复");
  });

  it("degraded 分支位于 stopped 分支之后（终态链完整）", () => {
    const iStopped = genSrc.indexOf('fin?.status === "stopped"');
    const iDegraded = genSrc.indexOf('fin?.status === "degraded"');
    expect(iStopped).toBeGreaterThan(-1);
    expect(iDegraded).toBeGreaterThan(iStopped);
  });
});

describe("stopped 分支消费 failed_chapters（R192 FIX-R13 源码锁）", () => {
  it("在线 stopped 分支：setFailedOutlineChapters + warning 双接线", () => {
    const iOnline = genSrc.indexOf('evt.event === "stopped"');
    expect(iOnline).toBeGreaterThan(-1);
    const seg = genSrc.slice(iOnline, iOnline + 2400);
    expect(seg).toContain("Array.isArray(evt.failed_chapters)");
    expect(seg).toContain("setFailedOutlineChapters(evt.failed_chapters)");
    expect(seg).toContain("章子目录生成失败");
  });

  it("重挂 stopped 分支：ckpt.failed_chapters 同口径接线", () => {
    const iReattach = genSrc.indexOf('fin?.status === "stopped"');
    expect(iReattach).toBeGreaterThan(-1);
    const seg = genSrc.slice(iReattach, iReattach + 2400);
    expect(seg).toContain("Array.isArray(ckpt.failed_chapters)");
    expect(seg).toContain("setFailedOutlineChapters(ckpt.failed_chapters)");
  });
});

describe("重挂 stopped 透传 fin.message（R192 FIX-R14 源码锁）", () => {
  it("无 checkpoint 的 stopped 文案拼接 fin.message（对齐刷新接管路径）", () => {
    const iReattach = genSrc.indexOf('fin?.status === "stopped"');
    const seg = genSrc.slice(iReattach, iReattach + 3200);
    expect(seg).toContain("fin.message");
  });
});

describe("buildTree 编号提取收敛 storedIdParts（R192 FIX-R15 源码锁）", () => {
  it("outlineTreeModel 引入 hasRenderableStoredId 且 ASCII 正则已摘除", () => {
    expect(treeSrc).toContain('from "../../utils/outlineNumbering"');
    expect(treeSrc).toContain("hasRenderableStoredId(oj.id)");
  });

  it("旧 ASCII 正则字面量不再出现（防回流）", () => {
    expect(treeSrc.includes("/^\\d+(\\.\\d+)*$/")).toBe(false);
  });
});
