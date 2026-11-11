// @vitest-environment jsdom
/**
 * R217 · 正文生成模块第三轮深度审计前端护栏（2026-11-11）。
 *
 * 覆盖：
 * - F9  statsEqual 对 running 用引用比较 ⇒ 心跳去重永久失效（P2，行为级）
 * - F10 consistency_repair_done / consistency_skipped 提前把进度写死 100%（P2）
 * - F11 页面级「切换章节 → 放弃更改」不清 localStorage 草稿（P2）
 * - F12 正文链断线重挂无世代/方案校验 ⇒ 旧协程跨方案串台（P1）
 * - 跨端契约：section_error 必须是 data: 帧（前端解析器只收 data: 行）
 * - 字节体检：本轮触碰源文件 LF / 无 U+FFFD（.gitattributes eol=lf）
 *
 * 源码级断言走 vite `?raw`（不执行被读模块、零新增依赖），防回流。
 */
import { describe, expect, it } from "vitest";
import { pickStats, statsEqual } from "../pages/schemeWorkbench/statsFormat";
import pageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import statsSrc from "../pages/schemeWorkbench/statsFormat.ts?raw";
import apiSrc from "../api/index.ts?raw";
import schemaSrc from "../types/checkpointSchema.ts?raw";

function slice(src: string, anchor: string, len = 900): string {
  const i = src.indexOf(anchor);
  return i < 0 ? "" : src.slice(i, i + len);
}

// ---------------------------------------------------------------------------
// F9 · statsEqual（行为级）
// ---------------------------------------------------------------------------
describe("statsEqual · 心跳去重（R217 F9）", () => {
  it("同内容、不同引用的 running 数组判为相等", () => {
    const a = pickStats({ running: [{ section_id: "s1", title: "一" }], done: 1 });
    const b = pickStats({ running: [{ section_id: "s1", title: "一" }], done: 1 });
    expect(a.running).not.toBe(b.running);
    expect(statsEqual(a, b)).toBe(true);
  });

  it("running 内容变化仍判为不等（不得把比较做成恒真）", () => {
    const a = pickStats({ running: [{ section_id: "s1" }] });
    const b = pickStats({ running: [{ section_id: "s2" }] });
    expect(statsEqual(a, b)).toBe(false);
  });

  it("running 长度变化判为不等", () => {
    const a = pickStats({ running: [] });
    const b = pickStats({ running: [{ section_id: "s1" }] });
    expect(statsEqual(a, b)).toBe(false);
  });

  it("源码走内容比较，旧引用比较形态不得回流", () => {
    expect(statsSrc).toContain("runningEqual(a.running, b.running)");
    expect(statsSrc).toContain("function runningEqual(");
    expect(statsSrc).not.toContain("a.running === b.running");
  });
});

// ---------------------------------------------------------------------------
// F10 · 阶段性事件不得提前封顶
// ---------------------------------------------------------------------------
describe("一致性阶段进度（R217 F10）", () => {
  it("consistency_repair_done 走单调护栏且停在阶段值 0.98", () => {
    const blk = slice(pageSrc, 'evt.event === "consistency_repair_done"', 700);
    expect(blk).toContain("nextProgress(prev, 0.98)");
    expect(blk).not.toContain("setProgress(1)");
  });

  it("consistency_skipped 同族同修（一次收口，不留第二处提前封顶）", () => {
    const blk = slice(pageSrc, 'evt.event === "consistency_skipped"', 500);
    expect(blk).toContain("nextProgress(prev, 0.98)");
    expect(blk).not.toContain("setProgress(1)");
  });

  it("终态仍是唯一封顶点（全页仅一处 setProgress(1)）", () => {
    expect(pageSrc.match(/setProgress\(1\)/g)?.length).toBe(1);
    expect(pageSrc).toContain('fin?.status === "completed"');
  });
});

// ---------------------------------------------------------------------------
// F11 · 页面级放弃草稿
// ---------------------------------------------------------------------------
describe("放弃更改清草稿（R217 F11）", () => {
  it("页面级放弃入口按旧章 prevKey 清 localStorage", () => {
    const blk = slice(pageSrc, 'title: "有未保存的更改"', 1800);
    expect(blk).toContain("localStorage.removeItem(");
    expect(blk).toContain("scheme_draft_${id}_${prevKey}");
  });

  it("草稿 key 格式与 draftKey 单一来源逐字同源", () => {
    expect(pageSrc).toContain("scheme_draft_${id}_${selectedSection.key}");
    expect(pageSrc).toContain("scheme_draft_${id}_${prevKey}");
  });
});

// ---------------------------------------------------------------------------
// F12 · 断线重挂世代校验
// ---------------------------------------------------------------------------
describe("断线重挂世代校验（R217 F12）", () => {
  it("重挂结果消费带世代判据", () => {
    expect(pageSrc).toContain("const reattachStillCurrent = genSeqRef.current === mySeq;");
    expect(pageSrc).toContain("if (hit && reattachStillCurrent)");
    expect(pageSrc).toContain("if (!hit && reattachStillCurrent)");
    expect(pageSrc).toContain("else if (hit && !hit.final && reattachStillCurrent)");
  });

  it("finally 的重连态复位也被世代校验包住", () => {
    const blk = slice(pageSrc, "finally {\n          // ✅ R217", 260);
    expect(blk).toContain("genSeqRef.current === mySeq");
    expect(blk).toContain("setSseReconnecting(false)");
  });

  it("旧形态（无校验直接消费 hit）不得回流", () => {
    expect(pageSrc).not.toContain("        if (hit) {\n          taskId = hit.taskId;");
  });
});

// ---------------------------------------------------------------------------
// 跨端契约 + 字节体检
// ---------------------------------------------------------------------------
describe("跨端契约（R217）", () => {
  it("SSE 解析器只收 data: 行 ⇒ 后端异常补发必须是 data: 帧", () => {
    const blk = slice(apiSrc, "export function parseSseEventBlock", 800);
    expect(blk).toContain('rawLine.startsWith("data:")');
    expect(blk).toContain("data: data.length ? data.join");
  });

  it("checkpointSchema.ts 登记三个统计失败留痕键", () => {
    expect(schemaSrc).toContain("word_audit_error?");
    expect(schemaSrc).toContain("order_check_error?");
    expect(schemaSrc).toContain("fact_injection_summary_error?");
    expect(schemaSrc).toContain("AUTO-GENERATED");
  });

  it("本轮触碰源文件 LF / 无 U+FFFD", () => {
    for (const src of [pageSrc, statsSrc, schemaSrc]) {
      expect(src).not.toContain("\r\n");
      expect(src).not.toContain("\uFFFD");
    }
  });
});
