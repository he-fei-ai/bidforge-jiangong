/**
 * 统一渲染器（导出侧）：
 * 用与预览完全一致的 mermaid.js（mermaidRuntime.ensureMermaid）把图表渲染成 PNG，
 * 随导出请求提交给后端嵌入 DOCX —— 保证导出与预览所见即所得。
 *
 * JSON 数据载荷（layout/timeline 的 zones/milestones 等）mermaid.js 无法渲染，
 * 预览中它们本来就走原生图表 fallback，导出时跳过、由后端 PIL 渲染兜底。
 *
 * P1-5 性能优化：
 * 1) 相同 mermaid_code 去重——同一张图只渲染一次，PNG 结果复用；
 * 2) 并发上限 4——串行逐张改为小并发，导出大文档时显著缩短耗时。
 *    进度回调保持原口径：done = 已处理条目数，total = items.length。
 */
import { ensureMermaid, nextMermaidId } from "../components/mermaidRuntime";

export type ExportChartItem = { chart_type: string; mermaid_code: string };
export type ExportChartImage = { chart_type: string; mermaid_code: string; png: string };

export type ExportGateInput = {
  /** 本方案是否已经成功执行过导出预检。 */
  hasPreflight: boolean;
  /** 导出预检问题；severity 缺失时按后端导出口径映射。 */
  issues?: Array<{ type?: string; severity?: string }> | null;
  /** 就绪度总检的最近结论。 */
  readiness?: { has_run?: boolean; released?: boolean; stale?: boolean } | null;
};

export type ExportGateResult = {
  /** 恒为 true：导出不受审核 / 预检完成度限制（2026-10-01 起）。
   *  保留字段仅为兼容既有调用点，任何新代码都不得再用它做禁用 / return。 */
  allowed: boolean;
  /** 提示文案（仅供展示，不构成阻断理由）。 */
  reason: string;
  highIssueCount: number;
};

/** 与后端 _EXPORT_ISSUE_RULE_MAP 保持一致，用于统计高风险问题数量（仅提示，不阻断导出）。 */
const HIGH_EXPORT_ISSUE_TYPES = new Set([
  "orphan_node",
  "empty_section",
  "review_pending",
  "review_rejected",
  "review_missing",
  "global_facts_blocked",
  "body_subheading_namespace_conflict",
]);

/**
 * 导出门禁（前端只提示、按钮不置灰；后端已于 R182 起默认阻断 high 级问题）：
 * 审核与预检属于质量辅助流程，不作为导出按钮的前置条件 —— 未预检、存在 high
 * 问题、就绪度未跑 / 过期 / 未放行，按钮均保持可点击。
 *
 * ✅ B-15（R182 · 2026-11-06）：后端在导出前按 severity 拦截 high 级 issue，
 * 返回 409 并要求前端「已知悉并继续」显式覆写。因此这里保留 highIssueCount
 * 统计，但文案必须与后端行为一致（不再声称"不影响导出"）。
 */
export function deriveExportGate(input: ExportGateInput): ExportGateResult {
  const issues = input.issues || [];
  const highIssueCount = issues.filter((issue) => {
    const severity = String(issue.severity || "").toLowerCase();
    return (
      severity === "high" ||
      severity === "block" ||
      (!severity && HIGH_EXPORT_ISSUE_TYPES.has(issue.type || ""))
    );
  }).length;

  const reason =
    highIssueCount > 0
      ? `导出预检提示：仍有 ${highIssueCount} 个高风险问题，导出前将被拦截（确认已知悉后可覆写继续，覆写会留痕）`
      : "导出按钮不受审核与预检状态限制；若预检发现高风险问题，导出前会拦截并允许确认覆写";
  return { allowed: true, reason, highIssueCount };
}

function createExportAbortError(): Error {
  const error = new Error("导出已取消");
  error.name = "AbortError";
  return error;
}

function throwIfExportAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw createExportAbortError();
}

/** SVG 字符串 → PNG dataURL（2.5x 超采样，与后端高清标准一致） */
async function svgToPng(svg: string, scale = 2.5, signal?: AbortSignal): Promise<string> {
  throwIfExportAborted(signal);
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  const el = doc.documentElement;
  if (!el.getAttribute("xmlns")) {
    el.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  }
  // ✅ BUG 修复：mermaid 在 useMaxWidth 下输出的 svg 宽度可能是百分比
  // （width="100%" / style 拉伸），parseFloat("100%") === 100 会被误当成 100px，
  // 导致导出 PNG 被压成 250×… 的迷你图（正文与预览不一致）。
  // 仅当 width/height 是绝对像素值时才采用，否则一律回退 viewBox（图形真实尺寸）。
  const parseAbsoluteLength = (v: string | null): number => {
    if (!v || v.includes("%")) return NaN;
    const n = parseFloat(v);
    return Number.isFinite(n) && n > 0 ? n : NaN;
  };
  let w = parseAbsoluteLength(el.getAttribute("width"));
  let h = parseAbsoluteLength(el.getAttribute("height"));
  if (!w || !h) {
    const vb = (el.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
    // ✅ R233（2026-11-13 · letterbox 静默失真修复）：旧谓词 `every(n => n > 0)`
    //    要求 viewBox **四个分量全部 > 0**，但 mermaid v11（setupGraphViewbox，
    //    node_modules 实测 11.17.2）输出 `${svgBounds.x - padding} ${svgBounds.y - padding} W H`
    //    —— minX/minY 被减去 padding 后通常为 0 或负值（如 "0 0 875 332"、
    //    "-15 -15 905 362"），0 不满足 > 0，谓词恒假。而 useMaxWidth:true
    //    （mermaidRuntime 配置）下 svg 无 height 属性、width="100%"，
    //    parseAbsoluteLength 两路全 NaN → 必然进 viewBox 分支 → 恒走
    //    1200×800 兜底：成稿 PNG 被 letterbox（宽扁甘特图上下大留白、竖高图
    //    左右大留白），与预览「所见即所得」承诺失真，且完全静默。
    //    修法：viewBox 语义 = "minX minY W H"，映射到图像视口的只需**宽高**
    //    两个分量有效（minX/minY 决定内容在视口内的映射起点，允许 0/负值）。
    if (vb.length === 4 && Number.isFinite(vb[2]) && vb[2] > 0 && Number.isFinite(vb[3]) && vb[3] > 0) {
      w = vb[2];
      h = vb[3];
    } else {
      w = 1200;
      h = 800;
    }
  }
  el.setAttribute("width", String(w));
  el.setAttribute("height", String(h));

  const svgStr = new XMLSerializer().serializeToString(el);
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgStr);

  const img = new Image();
  let onAbort: (() => void) | null = null;
  try {
    await new Promise<void>((resolve, reject) => {
      onAbort = () => {
        img.onload = null;
        img.onerror = null;
        img.src = "";
        reject(createExportAbortError());
      };
      if (signal?.aborted) {
        onAbort();
        return;
      }
      img.onload = () => resolve();
      img.onerror = () => reject(new Error("SVG 图像加载失败"));
      signal?.addEventListener("abort", onAbort, { once: true });
      img.src = url;
    });
  } finally {
    if (onAbort) signal?.removeEventListener("abort", onAbort);
  }
  throwIfExportAborted(signal);

  // ✅ BUG 修复：mermaid 异常输出（超长甘特/巨型流程图）可能给出数万 px 的
  // viewBox，直接按 2.5x 建 canvas 会超出浏览器单边/面积上限
  // （Chrome 约 16384px 单边、~268M 像素总面积），toDataURL 抛异常导致整批导出失败。
  // 超限时等比缩小 scale 到安全范围。
  const MAX_CANVAS_EDGE = 16000;
  const MAX_CANVAS_AREA = 240_000_000;
  let effScale = scale;
  const maxByEdge = MAX_CANVAS_EDGE / Math.max(w, h);
  const maxByArea = Math.sqrt(MAX_CANVAS_AREA / (w * h));
  effScale = Math.min(effScale, maxByEdge, maxByArea);
  if (!(effScale > 0) || !Number.isFinite(effScale)) effScale = 1;

  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(w * effScale));
  canvas.height = Math.max(1, Math.round(h * effScale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas 2d 上下文不可用");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.scale(effScale, effScale);
  ctx.drawImage(img, 0, 0, w, h);
  return canvas.toDataURL("image/png");
}

/**
 * 用预览同款 mermaid.js 渲染全部图表为 PNG。
 * 单图失败仅跳过（该图由后端渲染兜底），不阻塞导出。
 * P1-5：并发上限 4 + 相同 code 去重（渲染一次、PNG 复用）。
 */
export async function renderChartsForExport(
  items: ExportChartItem[],
  onProgress?: (done: number, total: number) => void,
  signal?: AbortSignal,
  /**
   * ✅ R230（F8 · R175 BUG-4 残余收口）：可选统计出口 —— 单图渲染失败旧实现
   *    `catch {}` 静默吞掉且不计任何数，「渲染 3/5」与设计跳过（JSON 载荷走
   *    后端兜底）不可区分，坏图无任何告警。传入后累计 `failed`（设计跳过不
   *    计入），调用方据此区分提示。缺省不传 = 旧行为逐字不变。
   * ✅ R233：`skipped_json`（可选）= 设计跳过的 JSON 载荷数 —— 「渲染 X/Y」的
   *    Y 含 JSON 载荷与重复码条目，去重后 X < Y 的差值若不拆分，用户会误读
   *    为「Y-X 张失败」（提示口径失真）。
   * ✅ R241：`dedup_skipped`（可选）= 同码去重跳过的条目数 —— R233 注释自认
   *    Y 的两半缺口（JSON 载荷 + 重复码条目）但只修了 JSON 一半；多章共用
   *    同图（exportResponse D1 明文合理场景）时「渲染 1/10」误读为 9 张失败。
   *    调用方分母应剔除 skipped_json + dedup_skipped 两项。
   */
  stats?: { failed: number; skipped_json?: number; dedup_skipped?: number },
): Promise<ExportChartImage[]> {
  throwIfExportAborted(signal);
  // ✅ 优化（2026-09-30）：无图表时不加载 mermaid.js。旧实现即使 items 为空也会
  //    await ensureMermaid()（动态 import 整个 mermaid 库，浏览器里 ~1-2s、jsdom
  //    测试环境可能挂起），白白拖慢导出准备阶段。
  if (!items || items.length === 0) return [];
  const out: ExportChartImage[] = [];
  const mermaid = await ensureMermaid();
  throwIfExportAborted(signal);
  let done = 0;

  // 相同 (chart_type, code) 只渲染一次（成功写 PNG 缓存、失败标记已尝试）；
  // 空/JSON 载荷跳过（后端 PIL 兜底）
  const pngCache = new Map<string, string>();
  const attempted = new Set<string>();

  const renderOne = async (it: ExportChartItem): Promise<void> => {
    throwIfExportAborted(signal);
    const code = (it.mermaid_code || "").trim();
    if (!code || code.startsWith("{")) {
      // JSON 数据载荷 → 后端 PIL 兜底（设计跳过，计入 skipped_json 口径）
      if (stats) stats.skipped_json = (stats.skipped_json || 0) + 1;
      return;
    }
    const cacheKey = `${it.chart_type}\u0000${code}`;
    if (attempted.has(cacheKey)) {
      // ✅ R241：同 key 已渲染（成功或失败）—— 跳过但**计数**（多章共用同图是
      //    明文合理场景，分母不剔除会把去重缩水误读成渲染失败）。
      if (stats) stats.dedup_skipped = (stats.dedup_skipped || 0) + 1;
      return;
    }
    attempted.add(cacheKey); // 同步段无 await，并发安全
    try {
      let png = pngCache.get(cacheKey) || "";
      if (!png) {
        // 第一优先方案：与预览一致的 mermaid.js 渲染；失败重试 1 次（换 id 防节点冲突）
        for (let attempt = 0; attempt < 2 && !png; attempt++) {
          try {
            throwIfExportAborted(signal);
            const renderId = nextMermaidId("export");
            const { svg } = await mermaid.render(renderId, code);
            throwIfExportAborted(signal);
            png = await svgToPng(svg, 2.5, signal);
          } catch (e) {
            throwIfExportAborted(signal);
            if (attempt === 1) throw e;
          }
        }
        if (png) pngCache.set(cacheKey, png);
      }
      throwIfExportAborted(signal);
      if (png) out.push({ chart_type: it.chart_type, mermaid_code: it.mermaid_code, png });
    } catch (e) {
      throwIfExportAborted(signal);
      // 单图失败不阻塞导出；✅ R230（F8）：不再静默 —— console.warn 留痕 +
      // failed 计数（设计跳过的 JSON 载荷不在此列，不含误导）。
      console.warn("exportCharts: 单图渲染失败（交后端兜底渲染轨）:", e);
      if (stats) stats.failed += 1;
    }
  };

  // 并发上限 4 的简易工作池
  const CONCURRENCY = 4;
  let idx = 0;
  const worker = async (): Promise<void> => {
    while (idx < items.length) {
      throwIfExportAborted(signal);
      const it = items[idx++];
      await renderOne(it);
      throwIfExportAborted(signal);
      done++;
      onProgress?.(done, items.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, items.length) }, () => worker()));

  return out;
}
