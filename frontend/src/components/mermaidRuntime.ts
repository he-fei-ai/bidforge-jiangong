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

/**
 * ✅ R196（2026-11-10）：mermaid 渲染前的输入侧安全闸（唯一出口）。
 *
 * 背景：initialize 用 securityLevel:"loose"（htmlLabels/<br/> 等富标签渲染
 * 依赖它，R66 三轨同款配色与渲染保真的刻意选择），LLM 产出的图表代码在
 * loose 模式下允许内嵌 HTML。首轮方案（DOMPurify 清洗 render 产出的 SVG）
 * 经真实浏览器实测被否决：DOMPurify 对 foreignObject 内的 htmlLabels 内容
 * **确定性清空**（jsdom 与 Chrome 行为一致，labelKept=false）—— 所有流程图
 * 节点文字消失，渲染保真代价不可接受。
 *
 * 现改为**渲染前拦截**：含危险特征的代码拒绝进 mermaid.render（走既有
 * 渲染失败占位：红框 + 原因 + AI 修复按钮），产出 SVG 即不含可执行面；
 * 合法富标签（<br/>、<b>、<img src> 等无事件形态）不受影响。
 * 注：导出 PNG 轨（exportCharts svgToPng）经 img→canvas 绘制，事件属性
 * 不执行、无注入面，无需过闸。
 */
export interface MermaidGate {
  safe: boolean;
  reason: string;
}

const _MERMAID_DANGER_RE =
  /<\s*script|<\s*iframe|<\s*object|<\s*embed|javascript\s*:|data\s*:\s*text\s*\/\s*html/i;

/**
 * ✅ R211（2026-11-10 · P2）：事件属性必须带**左边界**。
 *
 * 旧实现把 `on\w+\s*=` 直接并进主危险正则（无左边界）⇒ 只要「on」后还有词字符
 * 再接 `=` 就命中：AI 生成的工程图标签 `Connection=` / `Condition=` /
 * `Monitoring=` / `Consumption=` 全部被判危险 ⇒ 合法图被拒渲染（红框 + AI 修复
 * 按钮），而用户点修复后**再过同一闸**（MarkdownRenderer 的修复回填同样调用
 * assertMermaidCodeSafe）⇒ 若修复结果保留该标签即「修复后仍渲染失败」⇒
 * **无出路**（闸门把合法标签变成死路）。
 *
 * 现把事件特征拆成独立正则并要求左边界（前面不是字母/数字/下划线）：
 * `onclick=` / ` onerror =` / `x-onload=` 仍被拒；`Connection=` / `Condition=`
 * 放行。安全方向不变（真正的内联事件属性必带分隔符或标签边界）。
 */
const _MERMAID_EVENT_RE = /(^|[^A-Za-z0-9_])(on\w+\s*=)/i;

export function assertMermaidCodeSafe(code: string): MermaidGate {
  if (!code || !code.trim()) return { safe: false, reason: "图表代码为空" };
  const m = _MERMAID_DANGER_RE.exec(code);
  if (m) {
    return { safe: false, reason: "图表代码包含不允许的脚本/事件特征（" + m[0] + "），已拒绝渲染" };
  }
  const ev = _MERMAID_EVENT_RE.exec(code);
  if (ev) {
    return { safe: false, reason: "图表代码包含不允许的脚本/事件特征（" + ev[2] + "），已拒绝渲染" };
  }
  return { safe: true, reason: "" };
}

