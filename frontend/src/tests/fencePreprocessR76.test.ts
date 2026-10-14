/**
 * R76（2026-10-13）· 前端围栏判据护栏
 *
 * 锁三件事（全部由后端探针 artifacts/_probe_fence_matrix_20261013.py + marked 实测
 * 取证过的真实分叉形态驱动，不是凭空构造）：
 *   A 谓词语义：同种字符 + **信息串必须为空** + 长度容错（与后端 fence_common 同源）
 *   B 块边界：````mermaid（4 反引号）与 ~~~mermaid（波浪）必须识别；块内出现
 *     ```python 这类带标签行**不得**当闭合（否则其后正文被吞进图块 = BUG-F2 前端侧）
 *   C 修复动作：归一（补齐闭合行长度）与文末追加，且幂等
 */
import { describe, expect, it } from "vitest";
import {
  isChartFenceLang,
  isFenceClose,
  normalizeFenceCloses,
  parseFenceLine,
  replaceChartFences,
  scanFenceBlocks,
} from "../utils/fencePreprocess";

describe("R76-A 围栏谓词语义（与后端 fence_common 同口径）", () => {
  it("识别 3/4 反引号与波浪围栏，前导空白 ≤3", () => {
    expect(parseFenceLine("```mermaid")).toMatchObject({ char: "`", length: 3, info: "mermaid" });
    expect(parseFenceLine("````mermaid")).toMatchObject({ char: "`", length: 4 });
    expect(parseFenceLine("~~~python")).toMatchObject({ char: "~", length: 3, info: "python" });
    expect(parseFenceLine("   ```")).toMatchObject({ length: 3 });
    // 4 个前导空格属于缩进代码，不是围栏（CommonMark §4.7）
    expect(parseFenceLine("    ```")).toBeNull();
    expect(parseFenceLine("普通正文。")).toBeNull();
    expect(parseFenceLine("行内有 ``` 不是独立成行")).toBeNull();
  });

  it("闭合行必须同种字符且信息串为空", () => {
    const open = parseFenceLine("```mermaid")!;
    expect(isFenceClose("```", open)).toBe(true);
    expect(isFenceClose("````", open)).toBe(true); // 长度容错：长于开围栏也算闭合
    expect(isFenceClose("~~~", open)).toBe(false); // 异种字符
    expect(isFenceClose("```python", open)).toBe(false); // 带标签 = 内容，不是闭合
    const tildeOpen = parseFenceLine("~~~mermaid")!;
    expect(isFenceClose("```", tildeOpen)).toBe(false);
    expect(isFenceClose("~~~", tildeOpen)).toBe(true);
  });

  it("图表家族语言标签判据与后端同词表", () => {
    expect(isChartFenceLang("mermaid")).toBe(true);
    expect(isChartFenceLang("MERMAID")).toBe(true);
    expect(isChartFenceLang("chart-json")).toBe(true);
    expect(isChartFenceLang("ai_image")).toBe(true);
    expect(isChartFenceLang("python")).toBe(false);
    expect(isChartFenceLang("")).toBe(false);
  });
});

describe("R76-B 块边界：4 反引号 / 波浪围栏 / 块内带标签行", () => {
  it("````mermaid（4 反引号）必须被识别为图表块", () => {
    const blocks = scanFenceBlocks("````mermaid\ngraph TD; A-->B\n````\n尾段。");
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ isChart: true, lang: "mermaid", closeLine: 2 });
    expect(blocks[0].codeLines).toEqual(["graph TD; A-->B"]);
  });

  it("~~~mermaid（波浪围栏）必须被识别（旧实现只认三个反引号 → 预览源码外泄）", () => {
    const blocks = scanFenceBlocks("~~~mermaid\ngraph TD\n~~~\n尾段。");
    expect(blocks).toHaveLength(1);
    expect(blocks[0].isChart).toBe(true);
  });

  it("块内出现带标签围栏行不得提前闭合，正文不被吞（BUG-F2 前端侧）", () => {
    const src = [
      "```mermaid",
      "graph TD",
      "```python", // 旧实现把它当闭合：其后正文被吞进图块
      "console.log(1)",
      "```",
      "后续正文。",
    ].join("\n");
    const blocks = scanFenceBlocks(src);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].closeLine).toBe(4); // 真正的闭合在第 4 行（0-based）
    expect(blocks[0].codeLines).toEqual([
      "graph TD",
      "```python",
      "console.log(1)",
    ]);
    // 占位替换后，块外正文必须仍在
    const { text } = replaceChartFences(src, (lang, code) => `[${lang}:${code.length}]`);
    expect(text).toContain("后续正文。");
    expect(text).toContain("[mermaid:");
  });

  it("未闭合块只报一个边界，其后都算代码内容", () => {
    const blocks = scanFenceBlocks("前段。\n```mermaid\ngraph TD\n```\n```python\nx=1");
    // ```mermaid 已由 ``` 闭合；随后的 ```python 未闭合 → closeLine=-1
    expect(blocks).toHaveLength(2);
    expect(blocks[1]).toMatchObject({ lang: "python", closeLine: -1 });
  });
});

describe("R76-C 修复动作：归一 + 文末追加，且幂等", () => {
  it("4 开 3 闭 → 补齐闭合行到开围栏长度（不增删行、不动块内内容）", () => {
    const src = "````mermaid\ngraph TD; A-->B\n```\n尾段。";
    const { text, repairs } = normalizeFenceCloses(src);
    expect(repairs).toHaveLength(1);
    expect(repairs[0]).toMatchObject({
      action: "normalize_close_length",
      oldLength: 3,
      length: 4,
      char: "`",
    });
    expect(text).toBe("````mermaid\ngraph TD; A-->B\n````\n尾段。");
    expect(text.split("\n")).toHaveLength(src.split("\n").length);
    // 归一后两种口径结论一致：闭合行长度已足够，再跑一次零修复
    expect(normalizeFenceCloses(text).repairs).toEqual([]);
  });

  it("真未闭合的图表块 → 文末追加同种等长闭合", () => {
    const src = "```mermaid\ngraph TD; A-->B";
    const { text, repairs } = normalizeFenceCloses(src);
    expect(repairs[0]).toMatchObject({ action: "append_close", char: "`", length: 3 });
    expect(text.endsWith("```\n")).toBe(true);
    expect(normalizeFenceCloses(text).repairs).toEqual([]);
  });

  it("波浪块追加波浪闭合（不得用反引号混种闭合）", () => {
    const { text, repairs } = normalizeFenceCloses("~~~mermaid\ngraph TD");
    expect(repairs[0]).toMatchObject({ action: "append_close", char: "~", length: 3 });
    expect(text).toBe("~~~mermaid\ngraph TD\n~~~\n");
  });

  it("非图表围栏不在预览路径被改写（chartOnly 默认开）", () => {
    const src = "```python\nprint(1)";
    expect(normalizeFenceCloses(src).repairs).toEqual([]);
    expect(normalizeFenceCloses(src, { chartOnly: false }).repairs).toHaveLength(1);
  });

  it("空输入与无围栏输入安全", () => {
    expect(normalizeFenceCloses("").text).toBe("");
    expect(scanFenceBlocks("")).toEqual([]);
    expect(replaceChartFences("普通正文。", () => "X").text).toBe("普通正文。");
  });

  it("replaceChartFences 保持块外正文与段间单换行", () => {
    const src = "前段。\n\n```mermaid\ngraph TD\n```\n\n后段。";
    const { text, count } = replaceChartFences(src, () => "<div class='ph'></div>");
    expect(count).toBe(1);
    expect(text).toContain("前段。");
    expect(text).toContain("后段。");
    expect(text).not.toContain("graph TD"); // 源码不外泄
    expect(text).not.toContain("\n\n\n"); // 不产生多余空行
  });

  it("图表块紧随正文（源里本来就无空行）→ 段间必须补出空行", () => {
    // 回归锁（R76 端到端实测抓到的**本轮自引入**缺陷）：拼接若只用单个 \n，
    // `<div>` 在 CommonMark 里开启的 HTML block 不会在单换行处结束，紧随的
    // 「尾段。」会被吸进同一 block、丢掉 <p> —— 段落结构与 HEAD 旧实现（替换串
    // 自带 "\n…\n"）不一致，正是「图表插入位置与格式」类缺陷。
    const src = "```mermaid\ngraph TD; A-->B\n```\n尾段。";
    const { text } = replaceChartFences(src, () => "<div class='ph'></div>");
    expect(text).toBe("<div class='ph'></div>\n\n尾段。");
    expect(text).not.toMatch(/<\/div>\n[^ \t\n]/); // 占位后不得只隔一个换行就接正文

    const src2 = "前段。\n```mermaid\ngraph TD\n```";
    expect(replaceChartFences(src2, () => "<div class='ph'></div>").text).toBe(
      "前段。\n\n<div class='ph'></div>",
    );

    // 相邻两个图表块：不得叠加出三连续行（空白 <p> 的来源）
    const src3 = "```mermaid\nA\n```\n```chart-json\n{}\n```\n尾段。";
    const three = replaceChartFences(src3, () => "<div class='ph'></div>").text;
    expect(three).not.toContain("\n\n\n");
    expect(three.split("\n\n")).toHaveLength(3);
  });
});
