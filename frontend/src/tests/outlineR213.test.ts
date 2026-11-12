/**
 * R213（2026-11-10）· 目录生成模块「可见性 / 契约 parity」前端护栏。
 *
 * 覆盖：
 * - FIX-14 一级章截断信号在 completed / stopped / 断线重挂三条路径同文案出口；
 * - FIX-13 超三级折叠信号在保存目录路径被消费；
 * - FIX-12 不可达节点信号在章节加载路径被消费；
 * - FIX-6  checkpoint 白名单新增的两个键由生成器产出 number 类型；
 * - D5     taskPolling 的本地契约与生成类型交叉（不再有第二份独立手抄）。
 *
 * 源码级断言走 vite `?raw`（不执行被读模块、零新增依赖），防「改回内联」回流。
 */
import { describe, expect, it } from "vitest";
import {
  outlineClampNotice,
  outlineTruncationNotice,
  outlineUnmountedNotice,
} from "../pages/schemeWorkbench/outlineNotices";
import generationSrc from "../pages/schemeWorkbench/useOutlineGeneration.tsx?raw";
import actionsSrc from "../pages/schemeWorkbench/useOutlineActions.tsx?raw";
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import pollingSrc from "../pages/schemeWorkbench/taskPolling.ts?raw";
import checkpointSrc from "../types/checkpointSchema.ts?raw";

describe("R213 · 目录信号文案（唯一出口）", () => {
  it("无信号 / 0 / 负数 / 非数字一律不提示", () => {
    expect(outlineTruncationNotice(null)).toBeNull();
    expect(outlineTruncationNotice({})).toBeNull();
    expect(outlineTruncationNotice({ level1_truncated: 0, level1_total: 50 })).toBeNull();
    expect(outlineTruncationNotice({ level1_truncated: -3, level1_total: 50 })).toBeNull();
    expect(outlineTruncationNotice({ level1_truncated: "abc" })).toBeNull();
    expect(outlineClampNotice({ clamped_nodes: 0 })).toBeNull();
    expect(outlineUnmountedNotice({ unmounted_count: "x" })).toBeNull();
  });

  it("截断文案同时给出原始总数 / 保留数 / 丢弃数", () => {
    const text = outlineTruncationNotice({ level1_total: 50, level1_truncated: 10 });
    expect(text).toContain("50 章");
    expect(text).toContain("保留前 40 章");
    expect(text).toContain("丢弃 10 章");
    expect(text).toContain("请检查目录是否完整");
  });

  it("折叠 / 不可达文案量化且给出补救方向", () => {
    expect(outlineClampNotice({ clamped_depth: 4, clamped_nodes: 7 })).toContain("7 个深层节点");
    expect(outlineClampNotice({ clamped_depth: 4, clamped_nodes: 7 })).toContain("4 级");
    expect(outlineUnmountedNotice({ unmounted_count: 2 })).toContain("2 个不可达章节");
    expect(outlineUnmountedNotice({ unmounted_count: 2 })).toContain("恢复快照");
  });
});

describe("R213 · 三路径接线与契约 parity", () => {
  it("截断提示在全部终态路径共用纯函数", () => {
    const calls = generationSrc.match(/outlineTruncationNotice\(/g) || [];
    // ✅ R223 重锚（意图保留、强度不减）：R213 首版把消费点计数锁死为 3
    //    （在线 completed / 在线 stopped / 断线重挂）；R223 FE-5 补齐在线 error
    //    分支成为第 4 处（第 5 处=刷新接管，消费点在 SchemeWorkbenchPage 的
    //    outlineRestoreFnRef，由 outlineR216 护栏锁定）——意图是「全部终态路径
    //    都走同一纯函数出口、内联模板串不得回流」，消费点只增不减 ⇒ 收紧为
    //    下界断言 + 三处出口变量逐字在场 + 反向断言保留。
    expect(calls.length).toBeGreaterThanOrEqual(4);
    expect(generationSrc).toContain("_stoppedTruncation");
    expect(generationSrc).toContain("_reattachTruncation");
    expect(generationSrc).toContain("_errTruncation");
    expect(generationSrc).not.toContain("已截断保留前 ${");
  });

  it("保存目录 / 章节加载两处分别消费折叠与不可达信号", () => {
    expect(actionsSrc).toContain("outlineClampNotice(data)");
    expect(pageSrc).toContain("outlineUnmountedNotice(data)");
  });

  it("生成的 checkpoint 契约含两个 number 截断键", () => {
    expect(checkpointSrc).toContain("level1_truncated?: number;");
    expect(checkpointSrc).toContain("level1_total?: number;");
  });

  it("taskPolling 本地副本与生成契约交叉，后端加键自动可见", () => {
    expect(pollingSrc).toContain("outline_result?: GeneratedOutlineResult & {");
    expect(pollingSrc).toContain("content_result?: GeneratedContentResult & {");
    expect(pollingSrc).toContain("outline_result as GeneratedOutlineResult");
  });
});
