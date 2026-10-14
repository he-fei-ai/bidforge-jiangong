/**
 * Mermaid 运行时单例。
 *
 * 背景：DiagramPreview 与 MarkdownRenderer 此前各自维护了一份几乎相同的
 * mermaid 初始化逻辑（同一套 themeVariables / securityLevel / 中文字体），
 * 差异仅在于一处多了 `flowchart.useMaxWidth`。两份配置各自漂移会导致
 * 「同一个 Mermaid 代码在预览面板与正文里长得不一样」，因此收敛为唯一入口。
 *
 * 要点：
 * - mermaid 一律通过**动态 import** 加载，保证被 code-split 到独立 chunk，
 *   不会被拖进主包（mermaid 及其图类型插件体积达数百 KB）。
 * - `initialize()` 会改写 mermaid 的全局配置，因此全应用只执行一次。
 * - 并发调用共享同一个 pending Promise，避免重复加载与重复 initialize。
 */

let mermaidReady = false;
let mermaidMod: any = null;
let mermaidPending: Promise<any> | null = null;

export async function ensureMermaid(): Promise<any> {
  if (mermaidReady && mermaidMod) return mermaidMod;
  if (mermaidPending) return mermaidPending;

  mermaidPending = (async () => {
    const raw: any = await import("mermaid");
    const m: any = raw?.default ?? raw;
    m.initialize({
      startOnLoad: false,
      theme: "neutral",
      securityLevel: "loose",
      fontFamily: "'Microsoft YaHei', sans-serif",
      flowchart: { useMaxWidth: true },
      // ✅ 2026-10-11 R66 流程图统一样式规范（双模式）：淡紫底 #f0edff + 紫边框 #a89aff
      //    + 深灰文字，与后端 PIL 流程图渲染器（mermaid_flowchart.py）同一套配色，
      //    预览 / 导出 PNG / 后端兜底三条渲染轨所见一致。
      themeVariables: {
        primaryColor: "#f0edff",
        primaryTextColor: "#333333",
        primaryBorderColor: "#a89aff",
        lineColor: "#333333",
        secondaryColor: "#F3F0FF",
        tertiaryColor: "#F7F7FD",
      },
    });
    mermaidMod = m;
    mermaidReady = true;
    mermaidPending = null;
    return m;
  })();

  return mermaidPending;
}

/** 生成全局唯一的渲染 id。mermaid.render 要求 id 唯一，复用会引发节点冲突。 */
export function nextMermaidId(prefix = "mmd"): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
