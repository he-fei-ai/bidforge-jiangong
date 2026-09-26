/**
 * 目录生成 SSE 收尾「stopped 无成果」分支可选链护栏（2026-09-25 BUG 修复）。
 *
 * 历史背景：doGenerateOutline 的 fin.status === "stopped" 收尾分支中，
 * else 路径（checkpoint 缺失时 ckpt = fin.outline_result 为 undefined）
 * 曾直接访问 ckpt.event 抛 TypeError —— 收尾逻辑中断，用户既看不到
 * 「已停止」提示也可能让整个 finally 世代复位不执行。同函数其它分支
 * （completed / stopped 有成果路径）均先经 `ckpt?.outline &&` 短路守护，
 * 唯独此处漏了可选链，且此前无任何测试覆盖。
 *
 * 本护栏锁定（源码级断言，参照 workflowTabsGuard.test.ts 做法）：
 *   1. 无成果提示必须写成 ckpt?.event === "error"（可选链）；
 *   2. msg.info 兜底提示处禁止裸 ckpt.event 写法回潮
 *      （守护块内的 ckpt.event 已由 if (ckpt?.outline && …) 短路保护，不在禁止之列）。
 */
import { describe, it, expect } from "vitest";
import pageSource from "../pages/SchemeWorkbenchPage.tsx?raw";

const src: string = pageSource;

describe("SchemeWorkbenchPage stopped 收尾分支可选链护栏", () => {
  it("无成果提示必须使用可选链 ckpt?.event", () => {
    expect(src).toContain('msg.info(ckpt?.event === "error"');
  });

  it("禁止裸 ckpt.event 访问回潮（ckpt 可能为 undefined）", () => {
    // 守护块（if (ckpt?.outline && …)）内的 ckpt.event 是安全的，
    // 但 else 兜底提示处 ckpt 必为 undefined/无 outline，必须用可选链
    expect(src).not.toMatch(/msg\.info\(ckpt\.event/);
  });

  it("stopped 收尾分支的 ckpt 取自 fin.outline_result 且先经 outline 非空守护", () => {
    // 锁定该分支结构：const ckpt = fin.outline_result → if (ckpt?.outline && …) → else 提示
    const re = /fin\?\.status === "stopped"[\s\S]{0,400}?const ckpt = fin\.outline_result;[\s\S]{0,120}?if \(ckpt\?\.outline &&/;
    expect(re.test(src), "stopped 分支必须保持「先取 checkpoint、非空守护、else 兜底提示」结构").toBe(true);
  });
});
