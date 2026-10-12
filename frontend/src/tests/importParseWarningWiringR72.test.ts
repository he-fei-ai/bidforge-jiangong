/// <reference types="vite/client" />
/**
 * 解析提取链 · R72 前端 warning 接线护栏（2026-10-12）。
 *
 * 背景：后端 generate_outline / generate_content 会把「文档解析被截断、本次生成
 * 仅基于已解析到的部分」以 `event:'warning'` 下发（R72 A/B 修复）。目录链与正文链
 * 共用前端 `onSseEvent` 作为事件入口，但旧实现**只认 error、无 warning 分支** →
 * 后端发了、前端静默丢弃，用户看不见「依据不完整」（事实链早已本地 pushLog 消费，
 * 目录/正文两链却没有）。这正是 AGENTS.md §5 说的「实现了但链路不生效」缺陷，
 * 只有静态接线护栏能稳定守住。
 *
 * 判据实现沿用本仓既有范式（contentTerminalParity20261011.test.ts）：
 * 用 Vite `import.meta.glob(..., { query: "?raw" })` 读源码文本，不引 node:fs。
 */
import { describe, it, expect } from "vitest";

const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw", import: "default", eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, "tests/"), v]),
);

const PAGE = RAW["pages/SchemeWorkbenchPage.tsx"];

/**
 * 截取 `onSseEvent` 回调函数体源码（从声明到下一个 useCallback 注释块之前）。
 * 只取这段，避免把别处的 `evt.event === "warning"`（如事实链本地分支）混进来。
 */
function onSseEventBody(src: string): string {
  const start = src.indexOf("const onSseEvent = useCallback");
  if (start < 0) return "";
  // 该回调以 "  }, []);\n" 收尾；取到第一次出现的最短片段即可覆盖 error+warning 分支
  const end = src.indexOf("}, []);", start);
  return src.slice(start, end < 0 ? src.length : end);
}

describe("R72 · 前端 onSseEvent 必须消费 warning 事件（不静默丢弃）", () => {
  it("前提：能取到被测文件与 onSseEvent 函数体（防 glob 空跑成常绿）", () => {
    expect(PAGE).toBeTruthy();
    expect(onSseEventBody(PAGE).length).toBeGreaterThan(50);
  });

  it("onSseEvent 体内必须有 warning 分支并 toast 告知", () => {
    const body = onSseEventBody(PAGE);
    expect(body).toContain('evt.event === "warning"');
    expect(body).toContain("msg.warning");
  });

  it("warning 分支不得中断流（return false，与 error 的 return true 相反）", () => {
    const body = onSseEventBody(PAGE);
    const wi = body.indexOf('evt.event === "warning"');
    expect(wi).toBeGreaterThan(-1);
    // 取 warning 分支到 error 分支之间的片段，必须含 return false、不得含 return true
    const ei = body.indexOf('evt.event === "error"', wi);
    const seg = body.slice(wi, ei < 0 ? body.length : ei);
    expect(seg).toContain("return false");
    expect(seg).not.toContain("return true");
  });

  it("反向锚：若摘除 warning 分支，判据必须变红（证明扫描真的在守）", () => {
    const stripped = PAGE.replace(/if \(evt\.event === "warning"\)[\s\S]*?\n    \}\n/, "");
    const body = onSseEventBody(stripped);
    expect(body).not.toContain('evt.event === "warning"');
  });
});
