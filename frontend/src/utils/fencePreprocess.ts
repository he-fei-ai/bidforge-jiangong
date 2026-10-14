/**
 * 代码围栏判据（前端预览侧）—— 与后端 `app/services/fence_common.py` 同一语义。
 *
 * 为什么要有这个文件（R76 · 2026-10-13）：
 * 「这一行是不是某个围栏的闭合行」这条判据此前在后端有三套语义、前端另有一套：
 *   · 后端严格 CommonMark（预检 DLV-07 / 字数剔除 / 压缩禁区）
 *   · 后端容错（图表登记 / 改写 / 导出渲染：同种字符即可，不看长度）
 *   · 前端 MarkdownRenderer：**只认三个反引号** + 非贪婪任意 ``` 收尾
 * 三套同时作用于同一份正文时，实测出现「导出把后续正文吞进代码块、预览却把正文
 * 保留」的反向分叉（探针 artifacts/_probe_fence_matrix_20261013.py +
 * marked 实测）。本文件把前端侧也改成**行锚定 + 同种字符 + 信息串必须为空 +
 * 长度容错**，与后端渲染侧口径逐字一致。
 *
 * 设计约束：
 *   · 纯函数、零依赖、可单测（不引 marked，避免测试环境依赖 DOM）；
 *   · 长度维度**刻意容错**（与后端 `tolerant_length=True` 同源）：AI 高频输出
 *     「4 反引号开 + 3 反引号闭」，严格要求长度相等会让整块图表在预览里消失；
 *     补齐动作（`normalizeFenceCloses`）负责把文本改成两种口径结论一致的形态；
 *   · 信息串维度**不容错**：` ```js ` 是内容不是闭合（CommonMark §4.7）。
 */

/** 与后端 content_blocks.INLINE_CHART_FENCE_LANGS 同词表（图表家族围栏语言标签）。 */
export const CHART_FENCE_LANGS = ["mermaid", "chart-json", "ai_image"] as const;
export type ChartFenceLang = (typeof CHART_FENCE_LANGS)[number];

/**
 * 独立成行的围栏标记：≤3 前导空白 + 同种字符（反引号或波浪号）≥3 个 + 可选信息串。
 * 与后端 `fence_common.FENCE_LINE_RE` 逐字同构（JS 无 `\Z`，用 `$` + 非贪婪组）。
 */
const FENCE_LINE_RE = /^[ \t]{0,3}(`{3,}|~{3,})([^`~\n]*)$/;

export interface FenceLine {
  /** 围栏字符："`" 或 "~" */
  char: "`" | "~";
  /** 标记长度（≥3）。改写/补齐时必须原样携带，不得统一降级成 3 */
  length: number;
  /** 信息串（已 trim，可能为空串） */
  info: string;
}

/** 解析一行是否为围栏行；非围栏行返回 null。 */
export function parseFenceLine(line: string): FenceLine | null {
  const m = FENCE_LINE_RE.exec(line);
  if (!m) return null;
  const marker = m[1];
  return {
    char: marker[0] as "`" | "~",
    length: marker.length,
    info: (m[2] || "").trim(),
  };
}

/**
 * `line` 是否为 `open` 围栏的**闭合行**（与后端 `is_fence_close(tolerant_length=True)` 同语义）：
 *   1. 必须是围栏行；
 *   2. 与开围栏**同种字符**（``` 不能闭合 ~~~）；
 *   3. 信息串**必须为空**（带标签是内容，不是闭合 —— BUG-F2 的前端侧）；
 *   4. 长度容错：不要求 ≥ 开围栏（补齐由 normalizeFenceCloses 负责）。
 */
export function isFenceClose(line: string, open: FenceLine): boolean {
  const pf = parseFenceLine(line);
  if (!pf) return false;
  return pf.char === open.char && pf.info === "";
}

/** 语言标签是否属于图表家族（小写、去多余空白后比对）。 */
export function isChartFenceLang(info: string): boolean {
  const lang = info.trim().toLowerCase().split(/\s+/)[0] || "";
  return (CHART_FENCE_LANGS as readonly string[]).includes(lang);
}

/** 图表家族语言标签（已小写首词），非图表围栏返回空串。 */
export function chartFenceLang(info: string): string {
  return info.trim().toLowerCase().split(/\s+/)[0] || "";
}

export interface FenceBlock {
  /** 开围栏行号（0-based） */
  openLine: number;
  /** 闭合行号（0-based）；未闭合时为 -1 */
  closeLine: number;
  /** 开围栏标记长度（补齐闭合行时用） */
  openLength: number;
  char: "`" | "~";
  /** 小写语言标签首词（可能为空串） */
  lang: string;
  /** 块内代码行（不含开/闭围栏行） */
  codeLines: string[];
  /** 是否为图表家族围栏 */
  isChart: boolean;
}

/**
 * 行锚定扫描所有围栏块（与后端 `iter_closed_blocks` / `_scan_chart_fences_full` 同一
 * 遍历语义）：**块内不再解析新围栏**，首个未闭合处即停止（其后所有行都是代码内容）。
 *
 * 与旧的「一条全局正则搞定」的本质差别：旧正则 `/```(mermaid|...)\s*\n([\s\S]*?)```/g`
 * 只认恰好 3 个反引号、且任意 ``` 都算收尾 —— 于是
 *   · ````mermaid（4 反引号开）在预览里**完全不识别**，源码原样印在页面上；
 *   · ~~~mermaid（波浪围栏）同样不识别，而后端三侧都认；
 *   · 块内出现带标签的围栏行会被当成收尾，把其后正文吞进图块。
 */
export function scanFenceBlocks(text: string): FenceBlock[] {
  if (!text) return [];
  const lines = text.split("\n");
  const blocks: FenceBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const open = parseFenceLine(lines[i]);
    if (!open) {
      i += 1;
      continue;
    }
    const lang = chartFenceLang(open.info);
    const start = i + 1;
    let closeLine = -1;
    let j = i + 1;
    while (j < lines.length) {
      if (isFenceClose(lines[j], open)) {
        closeLine = j;
        break;
      }
      j += 1;
    }
    blocks.push({
      openLine: i,
      closeLine,
      openLength: open.length,
      char: open.char,
      lang,
      codeLines: lines.slice(start, closeLine < 0 ? lines.length : closeLine),
      isChart: isChartFenceLang(open.info),
    });
    if (closeLine < 0) break; // 未闭合：其后都在块内，不再是新围栏
    i = closeLine + 1;
  }
  return blocks;
}

export interface FenceRepair {
  /** normalize_close_length：补齐闭合行标记长度；append_close：文末追加闭合 */
  action: "normalize_close_length" | "append_close";
  /** 触发起始行（1-based，与后端日志口径一致，便于用户定位） */
  line: number;
  char: "`" | "~";
  lang: string;
  /** 修复后标记长度 */
  length: number;
  /** 仅 normalize：原标记长度 */
  oldLength?: number;
}

/**
 * 归一 + 补齐围栏缺陷（与后端 `auto_fix_unclosed_fences` 两步同语义）。
 *
 * 与后端的**一处刻意差异**：前端只处理**图表家族**块。理由是预览路径上普通代码块
 * （如 `python`）即便未闭合，marked 也会把其后内容继续当代码显示，不产生「正文被
 * 吞」的可见错位；而图表块判错会直接让图消失或源码外泄。补齐非图表块会改写用户
 * 正在编辑的文本，超出预览组件的职责。落库防线（后端）仍覆盖全部围栏。
 *
 * 性质与后端一致：只改闭合行标记长度 / 在文末追加闭合，不增删行、不动块内内容、幂等。
 */
export function normalizeFenceCloses(
  text: string,
  opts: { chartOnly?: boolean } = {},
): { text: string; repairs: FenceRepair[] } {
  if (!text) return { text, repairs: [] };
  const chartOnly = opts.chartOnly !== false;
  const lines = text.split("\n");
  const repairs: FenceRepair[] = [];
  for (const b of scanFenceBlocks(text)) {
    if (chartOnly && !b.isChart) continue;
    if (b.closeLine < 0) continue;
    const pf = parseFenceLine(lines[b.closeLine]);
    if (!pf || pf.length >= b.openLength) continue;
    const raw = lines[b.closeLine];
    const indent = raw.slice(0, raw.length - raw.replace(/^[ \t]+/, "").length);
    lines[b.closeLine] = `${indent}${b.char.repeat(b.openLength)}`;
    repairs.push({
      action: "normalize_close_length",
      line: b.closeLine + 1,
      char: b.char,
      lang: b.lang,
      length: b.openLength,
      oldLength: pf.length,
    });
  }
  // 归一后再看是否仍有未闭合（在同一份 lines 上重扫，避免两步各跑一份旧文本）
  const rescan = scanFenceBlocks(lines.join("\n")).filter(
    (b) => b.closeLine < 0 && (!chartOnly || b.isChart),
  );
  let out = lines.join("\n");
  for (const b of rescan) {
    out = `${out.replace(/\s+$/, "")}\n${b.char.repeat(b.openLength)}\n`;
    repairs.push({
      action: "append_close",
      line: b.openLine + 1,
      char: b.char,
      lang: b.lang,
      length: b.openLength,
    });
  }
  return { text: out, repairs };
}

/**
 * 用行锚定扫描把图表家族围栏替换成占位标记（取代旧的单条全局正则）。
 *
 * `replacer` 收到语言标签与块内代码，返回用于替换整段围栏的字符串。未闭合块
 * **不替换**（交回 marked 按代码显示），与后端导出侧「未闭合块原样保留」一致。
 */
export function replaceChartFences(
  text: string,
  replacer: (lang: string, code: string) => string,
): { text: string; count: number } {
  if (!text) return { text, count: 0 };
  const blocks = scanFenceBlocks(text).filter(
    (b) => b.isChart && b.closeLine >= 0,
  );
  if (!blocks.length) return { text, count: 0 };
  const lines = text.split("\n");
  // 逐段拼接：块前正文 + 占位 + 其后正文，段间用**空行**分隔。
  // ⚠️ 必须是空行（两个换行）而不是单个换行 —— `<div>` 在 CommonMark 里开启一个
  //    HTML block，只有**空行**才结束它。用单换行时，紧跟围栏的那行正文会被吸进
  //    HTML block（marked 不再产出 `<p>`，段落样式与「图表插入位置」都跟旧实现不同）；
  //    HEAD 旧实现靠正则替换串自带 `"\n…\n"` 恰好留了空行，本函数改为显式保证。
  // ⚠️ 同时不能像旧实现那样依赖正则「顺手吃掉」围栏行与换行 —— 少算一个 \n 就会
  //    让占位 div 与上一段正文粘成同一 block。
  const parts: string[] = [];
  let cursor = 0;
  for (const b of blocks) {
    if (b.openLine > cursor) parts.push(lines.slice(cursor, b.openLine).join("\n"));
    parts.push(replacer(b.lang, b.codeLines.join("\n")));
    cursor = b.closeLine + 1;
  }
  parts.push(lines.slice(cursor).join("\n"));
  // 段内原有的首尾空行先剥掉，再统一以空行相接：既保证 HTML block 边界，
  // 又不会叠加出 `\n\n\n`（多余空行会让 marked 产出空白 <p>）。
  const stripBlankEdges = (s: string): string =>
    s.replace(/^(?:[ \t]*\n)+/, "").replace(/(?:\n[ \t]*)+$/, "");
  const segs = parts.map(stripBlankEdges).filter((s) => s.length > 0);
  return { text: segs.join("\n\n"), count: blocks.length };
}
