/**
 * mermaid 运行时单例的单元测试。
 *
 * nextMermaidId 的唯一性约束是 DiagramPreview / MarkdownRenderer / exportCharts
 * 三处并发渲染互不污染节点 ID 的前提；ensureMermaid 单例语义保证 mermaid
 * 只被 initialize 一次、不被重复加载进主包。
 */
import { describe, it, expect } from "vitest";
import { nextMermaidId } from "../components/mermaidRuntime";

describe("nextMermaidId", () => {
  it("使用指定前缀并包含时间戳与随机段", () => {
    const id = nextMermaidId("mmd");
    expect(id.startsWith("mmd-")).toBe(true);
    expect(id.length).toBeGreaterThan("mmd-".length + 8);
  });

  it("默认前缀为 mmd", () => {
    expect(nextMermaidId().startsWith("mmd-")).toBe(true);
  });

  it("连续 1000 次调用无重复（并发渲染防节点冲突）", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const id = nextMermaidId("export");
      expect(seen.has(id)).toBe(false);
      seen.add(id);
    }
  });
});
