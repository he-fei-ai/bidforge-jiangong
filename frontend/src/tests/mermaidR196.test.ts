/**
 * R196（2026-11-10）图表前端加固护栏。
 *
 * 背景：mermaid initialize 用 securityLevel:"loose"（htmlLabels/<br/> 富标签
 * 渲染依赖），LLM 产出的图表代码在 loose 模式下允许内嵌 HTML。
 *
 * 方案演进（⚠️ 首轮方案经真实浏览器实测否决，教训须留痕）：
 *   首轮 = DOMPurify 清洗 render 产出的 SVG —— jsdom 与 Chrome（真实浏览器
 *   http 页验证）均确认 DOMPurify 对 foreignObject 内的 htmlLabels 内容
 *   **确定性清空**（labelKept=false）→ 所有流程图节点文字消失，保真代价
 *   不可接受。
 *   定稿 = **渲染前输入侧闸** `assertMermaidCodeSafe`（mermaidRuntime 唯一
 *   导出）：含 script / iframe / 内联事件（on 开头属性）/ javascript: 等危险
 *   特征的代码拒绝进
 *   mermaid.render（走既有渲染失败占位：红框 + 原因 + AI 修复按钮），
 *   产出 SVG 即不含可执行面；合法富标签图不受影响。
 *
 * 锁三层：
 * 1. 闸行为实证：危险特征拒绝、合法富标签放行、空代码拒绝；
 * 2. 消费点锁：MarkdownRenderer 两处渲染入口（正文 + 修复回填）必须过闸；
 * 3. 配置锁：securityLevel:"loose" 是 R66 渲染保真的刻意选择，勿顺手改 strict。
 */
// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { assertMermaidCodeSafe } from "../components/mermaidRuntime";

// Vite import.meta.glob(..., { query: "?raw" }) 读源码文本，不引 node:fs（tsconfig 无 node types）。
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../components/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, "").replace(/^\.\//, ""), v]),
);

const MDR_SRC = RAW["components/MarkdownRenderer.tsx"] || "";
const RUNTIME_SRC = RAW["components/mermaidRuntime.ts"] || "";

describe("assertMermaidCodeSafe 行为实证", () => {
  it("拒绝 <script> 特征", () => {
    const g = assertMermaidCodeSafe('graph TD\nA["<script>alert(1)</script>"] --> B');
    expect(g.safe).toBe(false);
    expect(g.reason).toContain("script");
  });

  it("拒绝内联事件特征（onerror=/onclick= 等）", () => {
    expect(assertMermaidCodeSafe('graph TD\nA["<img src=x onerror=alert(1)>"]').safe).toBe(false);
    expect(assertMermaidCodeSafe('graph TD\nA["<b onclick=alert(1)>x</b>"]').safe).toBe(false);
  });

  it("拒绝 javascript: 与 data:text/html 特征", () => {
    expect(assertMermaidCodeSafe('graph TD\nA["<a href=javascript:alert(1)>x</a>"]').safe).toBe(false);
    expect(assertMermaidCodeSafe('graph TD\nA["<iframe src=data:text/html;base64,xx>"]').safe).toBe(false);
  });

  it("放行合法富标签图（<br/> 换行 / <b> / <img src> 无事件形态）", () => {
    const ok = 'graph TD\nA["施工准备<br/>阶段"] --> B["<b>主体施工</b>"] --> C["<img src=icon.png>"]';
    const g = assertMermaidCodeSafe(ok);
    expect(g.safe).toBe(true);
    expect(g.reason).toBe("");
  });

  it("拒绝空代码", () => {
    expect(assertMermaidCodeSafe("").safe).toBe(false);
    expect(assertMermaidCodeSafe("   ").safe).toBe(false);
  });
});

describe("消费点锁（渲染入口必须过闸）", () => {
  it("正文渲染循环：render 前过 assertMermaidCodeSafe", () => {
    expect(MDR_SRC).toContain("const gate = assertMermaidCodeSafe(ph.code)");
    expect(MDR_SRC).toContain("if (!gate.safe) throw new Error(gate.reason)");
  });

  it("修复回填：AI 修复产出同样过闸", () => {
    expect(MDR_SRC).toContain("const gate = assertMermaidCodeSafe(fixed)");
  });

  it("闸由 mermaidRuntime 唯一导出（出口收敛）", () => {
    expect(RUNTIME_SRC).toContain("export function assertMermaidCodeSafe");
    expect(RUNTIME_SRC).toContain("export interface MermaidGate");
  });
});

describe("initialize 配置锁", () => {
  it("securityLevel 保持 loose（R66 富标签渲染保真的刻意选择，勿顺手改 strict）", () => {
    expect(RUNTIME_SRC).toContain('securityLevel: "loose"');
  });
});
