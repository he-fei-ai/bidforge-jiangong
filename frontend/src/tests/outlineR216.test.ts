/**
 * R216（2026-11-11）· 大纲/SSE 方向深度探索增强 —— 前端加固护栏。
 *
 * 覆盖：D-1（outline 任务终态消费 outline_result / 刷新接管补截断·复用信号）、
 * D-2（断线重挂回填 activeTaskIdRef）、D-5（切方案复位 failedOutlineChapters）、
 * D-6（「AI 调整目录」不存在的入口改为可执行指引）、D-7（保存/导入/生成同帧双击
 * 在飞锁）、D-8（套用消费 clamped_* 信号）、D-10（error 事件消费 failed_chapters）、
 * D-11（存量库 children 判空）。
 *
 * 源码读取一律走 vite `?raw`，不执行被读模块。
 */
import { describe, expect, it } from "vitest";
import useOutlineActionsSrc from "../pages/schemeWorkbench/useOutlineActions.tsx?raw";
import useOutlineGateSrc from "../pages/schemeWorkbench/useOutlineGate.tsx?raw";
import useOutlineGenerationSrc from "../pages/schemeWorkbench/useOutlineGeneration.tsx?raw";
import useUnknownTaskRecoverySrc from "../pages/schemeWorkbench/useUnknownTaskRecovery.tsx?raw";
import schemeWorkbenchPageSrc from "../pages/SchemeWorkbenchPage.tsx?raw";
import outlineLibraryEditModalSrc from "../components/OutlineLibraryEditModal.tsx?raw";

describe("R216 · 同步在飞锁（D-7 同帧双击防护）", () => {
  it("保存目录 / 导入目录在飞锁", () => {
    expect(useOutlineActionsSrc).toContain("saveInFlightRef");
    expect(useOutlineActionsSrc).toContain("importInFlightRef");
  });
  it("目录生成在飞锁", () => {
    expect(useOutlineGenerationSrc).toContain("genInFlightRef");
  });
});

describe("R216 · 断线重挂回填 activeTaskIdRef（D-2）", () => {
  it("SSE 首事件未到即断时回填任务 id，停止按钮可读", () => {
    expect(useOutlineGenerationSrc).toContain("_reattachBackfilled");
    expect(useOutlineGenerationSrc).toContain("activeTaskIdRef.current = hit.taskId");
  });
});

describe("R216 · error 事件消费 failed_chapters（D-10）", () => {
  it("全章失败目录树橙色高亮", () => {
    expect(useOutlineGenerationSrc).toContain("setFailedOutlineChapters(evt.failed_chapters)");
  });
});

describe("R216 · outline 任务终态消费 outline_result（D-1）", () => {
  it("未知态手动接管 outline 终态", () => {
    expect(useUnknownTaskRecoverySrc).toContain(
      'if (ut.genType === "outline") onOutlineResult?.(fin)',
    );
  });
  it("页面级恢复回调经 ref 间接注入", () => {
    expect(schemeWorkbenchPageSrc).toContain(
      "onOutlineResult: (fin) => outlineRestoreFnRef.current(fin)",
    );
  });
});

describe("R216 · 刷新接管补消费截断/复用信号（D-3）", () => {
  it("checkpoint 白名单 level1_* / resumed_chapters 不再零感知", () => {
    expect(schemeWorkbenchPageSrc).toContain("outlineTruncationNotice(ckpt)");
    expect(schemeWorkbenchPageSrc).toContain("ckpt.resumed_chapters");
  });
});

describe("R216 · 切方案复位 failedOutlineChapters（D-5）", () => {
  it("通用章名（施工准备）跨方案不再误高亮", () => {
    expect(schemeWorkbenchPageSrc).toContain("setFailedOutlineChapters([])");
  });
});

describe("R216 · 文案可执行化（D-6，AI 调整目录入口不存在）", () => {
  it("正文清除风险提示改为可执行指引", () => {
    expect(useOutlineGateSrc).toContain(
      "如需保留，请在重新生成或保存目录前先自行备份已生成正文",
    );
    expect(useOutlineGateSrc).toContain("先备份所需章节的正文再操作");
  });
});

describe("R216 · 套用消费 clamped_* 信号（D-8）", () => {
  it("存量深树套用折叠经 passthrough 透传后前端消费", () => {
    expect(useOutlineActionsSrc).toContain("outlineClampNotice(data)");
  });
});

describe("R216 · 存量库 children 判空（D-11）", () => {
  it("insertIntoNode 守卫缺失 children 键", () => {
    expect(outlineLibraryEditModalSrc).toContain("children: [...(node.children || [])");
  });
});
