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

/**
 * mermaid 动态导入实例的最小 API 面。
 * mermaid 为 ESM/CJS 双构建且版本演进快（第三方库版本不可控），完整类型面不稳定；
 * 本项目消费方（MarkdownRenderer / exportCharts）只使用 initialize 与 render 两个方法。
 */
interface MermaidRuntime {
  initialize(config: Record<string, unknown>): void;
  render(id: string, text: string): Promise<{ svg: string }>;
}

let mermaidReady = false;
let mermaidMod: MermaidRuntime | null = null;
let mermaidPending: Promise<MermaidRuntime> | null = null;

export async function ensureMermaid(): Promise<MermaidRuntime> {
  if (mermaidReady && mermaidMod) return mermaidMod;
  if (mermaidPending) return mermaidPending;

  mermaidPending = (async () => {
    // mermaid 的 ESM 入口带 default，CJS 互操作下整体即实例（第三方版本不可控）；
    // 历史实现按「有 default 取 default，否则整体即模块」兜底，运行时行为不变。
    const raw: unknown = await import("mermaid");
    const holder = raw as { default?: unknown };
    const m = (holder.default ?? holder) as MermaidRuntime;
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
