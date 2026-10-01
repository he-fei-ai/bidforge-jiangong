import { memo, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { chartsApi } from "../api";
import { ensureMermaid, nextMermaidId } from "./mermaidRuntime";
import { CHART_TITLE_FALLBACK, RENDERABLE_CHART_TYPES } from "../utils/chartTypes";

// ✅ KaTeX 公式渲染：动态加载 katex（模块级缓存，多实例共享一次加载）
let _katexPromise: Promise<any> | null = null;
function ensureKatex(): Promise<any> {
  if (!_katexPromise) {
    _katexPromise = import("katex").then((m) => m.default ?? m);
  }
  return _katexPromise;
}

// ✅ 性能优化：marked 配置提到模块级一次性设置（原实现在 useMemo 里每次解析都
// 调用 marked.setOptions 重写全局配置，无谓开销且属于「渲染期改全局状态」）。
marked.setOptions({ gfm: true, breaks: false });

// ✅ P1-6：mermaid SVG 按代码内容复用——同一图表在编辑/切换中不重复调用 mermaid.render
const _svgCache = new Map<string, string>();
// ✅ 修复：SVG 文本单张可达数十 KB，而缓存此前无任何上限 —— 长时间编辑同一方案
//    （每次改正文都会换一份图表代码）会让键无限增长，直到刷新页面才释放。
// ✅ 性能优化：由「FIFO 淘汰」升级为「LRU + 字节配额」。FIFO 会先淘汰最旧的条目，
//    而热点图表往往是反复查看的早期图表，被淘汰后再次查看要重新 render 数百 ms。
//    LRU 命中时刷新位置，配合总字节上限（防止单张巨大 SVG 撑爆内存）。
const _SVG_CACHE_MAX = 120;
const _SVG_CACHE_MAX_BYTES = 4 * 1024 * 1024; // 4 MB
let _svgCacheBytes = 0;
function _svgCacheGet(code: string): string | undefined {
  const v = _svgCache.get(code);
  if (v !== undefined) {
    // Map 保持插入顺序：delete + set 即刷新为最新
    _svgCache.delete(code);
    _svgCache.set(code, v);
  }
  return v;
}
function _svgCacheSet(code: string, svg: string) {
  const prev = _svgCache.get(code);
  if (prev !== undefined) {
    _svgCacheBytes -= prev.length;
    _svgCache.delete(code);
  }
  _svgCache.set(code, svg);
  _svgCacheBytes += svg.length;
  while (_svgCache.size > _SVG_CACHE_MAX || _svgCacheBytes > _SVG_CACHE_MAX_BYTES) {
    const oldest = _svgCache.keys().next().value;
    if (oldest === undefined) break;
    _svgCacheBytes -= _svgCache.get(oldest)?.length ?? 0;
    _svgCache.delete(oldest);
  }
  _svgCacheBytes = Math.max(0, _svgCacheBytes);
}

interface Placeholder {
  marker: string;
  code: string;
  id: string;
  /** chart-json 数据块（labor/layout 等，走后端渲染引擎） */
  json?: { chart_type: string; title: string };
  /** KaTeX 公式（displayMode 由 value 判定） */
  math?: { tex: string; displayMode: boolean };
  /** AI 文生图块（ai_image，生成前为占位卡片）
   *  ✅ BUG 修复：旧类型声明多了一个必填 `code`，但构造处从未传入、消费处只读
   *  title/prompt（源码统一取顶层 ph.code）——类型与实现不符导致 tsc 报错、构建失败。
   *  这里把 code 去掉，使 aiImage 契约与实际使用一致。 */
  aiImage?: { prompt: string; style: string; title: string };
}

interface MarkdownRendererProps {
  content: string;
  /** 章节 ID：传入后 AI 修复结果会写回 chart_predictions 并同步重写章节正文（导出一致） */
  sectionId?: string;
  /** 后端重写正文成功后回调（参数为修复后的完整章节正文），父组件据此同步预览状态 */
  onContentReplaced?: (newContent: string) => void;
}

// ✅ 性能优化：memo 化组件本体（解析结果另有 useMemo 记忆化），父页任何无关
//    状态变化不再驱动 Markdown/mermaid 链路重渲；回调请传稳定引用（内部已用 ref 兜底）
function MarkdownRenderer({ content, sectionId, onContentReplaced }: MarkdownRendererProps) {
  const ref = useRef<HTMLDivElement>(null);
  // ✅ 事件委托的 handleClick 挂载在 effect 内（依赖 parsed），props 变化不一定触发
  //    effect 重跑 —— 用 ref 保证闭包内始终读到最新值，避免修复请求带旧 section_id
  const sectionIdRef = useRef(sectionId);
  sectionIdRef.current = sectionId;
  const onContentReplacedRef = useRef(onContentReplaced);
  onContentReplacedRef.current = onContentReplaced;
  const [html, setHtml] = useState("");
  const [errMsg, setErrMsg] = useState<string | null>(null);
  // ✅ P1-6 性能优化：内容变化 500ms 防抖后再解析渲染（编辑/切换过程不卡顿）
  const [debouncedContent, setDebouncedContent] = useState(content);
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedContent(content), 500);
    return () => window.clearTimeout(t);
  }, [content]);

  // ✅ P1-6：解析结果按内容记忆化（围栏预处理 → marked → DOMPurify → 占位符提取），
  //    仅在内容真正变化时重算；mermaid 渲染（重开销）与 DOM 写入留在 effect
  const parsed = useMemo(() => {
    const placeholders: Placeholder[] = [];
    const markerPrefix = `__MMD_PH_${Date.now()}_`;
    let counter = 0;

    // ✅ BUG修复：未闭合的 mermaid/chart-json 代码块会让非贪婪正则匹配到
    // 下一个 ```（可能是其他代码块的开始），导致内容错位/吞噬。
    // 预处理：逐行检测未闭合的图表代码块，在文末补闭合围栏。
    const _lines = debouncedContent.split("\n");
    let _inChartBlock = false;
    let _chartBlockStart = -1;
    for (let _i = 0; _i < _lines.length; _i++) {
      const _t = _lines[_i].trim();
      if (_t.startsWith("```mermaid") || _t.startsWith("```chart-json") || _t.startsWith("```ai_image")) {
        _inChartBlock = true;
        _chartBlockStart = _i;
      } else if (_inChartBlock && _t.startsWith("```")) {
        _inChartBlock = false;
      }
    }
    let _safeContent = debouncedContent;
    if (_inChartBlock) {
      console.warn("MarkdownRenderer: 检测到未闭合的图表代码块（起始行 %d），已自动补闭合", _chartBlockStart + 1);
      _safeContent = debouncedContent + "\n```\n";
    }

    const processed = _safeContent.replace(
      /```(mermaid|chart-json|ai_image)\s*\n([\s\S]*?)```/g,
      (_m, lang: string, code: string) => {
        const id = nextMermaidId("mmd");
        const marker = `${markerPrefix}${counter}__`;
        if (lang === "chart-json") {
          // ✅ 图表同步生成：JSON 数据块调后端渲染引擎出图（与导出一致）
          // ✅ BUG 修复（2026-09-27）：旧实现 `obj.type || "labor"` —— 载荷省略
          //    type 时会**凭空捏造**一个 labor 类型发给后端，把甘特图/架构图载荷
          //    送进 labor 渲染器必然失败，预览显示"渲染引擎暂不可用"，
          //    而导出 DOCX 却是对的（导出侧按载荷结构推断）——预览/导出错配。
          //    此处改为：解析不出合法类型就传空串，交给后端按结构推断
          //    （与登记侧、导出侧同一个判据），不再在前端复制一份推断逻辑。
          let chartType = "";
          let title = "";
          try {
            const obj = JSON.parse(code.trim());
            if (obj && typeof obj === "object" && !Array.isArray(obj)) {
              const declared = String(obj.type || "").trim().toLowerCase();
              if (RENDERABLE_CHART_TYPES.has(declared)) chartType = declared;
              title = String(obj.title || "");
            }
          } catch { /* 解析失败按占位卡片显示 */ }
          placeholders.push({
            marker, code: code.trim(), id,
            json: { chart_type: chartType, title },
          });
        } else if (lang === "ai_image") {
          // ✅ AI 文生图（对齐 OpenBidKit 的 ai 插图）：生成前显示占位卡片 + 生成按钮
          let prompt = ""; let style = "engineering_diagram"; let title = "";
          try {
            const obj = JSON.parse(code.trim());
            prompt = String(obj.prompt || "");
            style = String(obj.style || "engineering_diagram");
            title = String(obj.title || "AI 配图");
          } catch { /* 解析失败按占位卡片显示 */ }
          placeholders.push({ marker, code: code.trim(), id, aiImage: { prompt, style, title } });
        } else {
          placeholders.push({ marker, code: code.trim(), id });
        }
        counter++;
        return `\n<div class="mmd-ph" data-marker="${marker}"></div>\n`;
      }
    );

    // ✅ KaTeX 公式提取：在 marked 解析前把 $$...$$ / $...$ 替换为占位符，
    //    避免被 marked 当作普通文本转义。逐行扫描跳过 ``` 代码围栏内的内容，
    //    行内公式要求 $ 紧贴内容（排除 "100$ 以及" 这类货币误判）。
    let mathContent = processed;
    const _extractMath = (src: string): string => {
      const lines = src.split("\n");
      let inFence = false;
      for (let i = 0; i < lines.length; i++) {
        const t = lines[i].trim();
        if (t.startsWith("```")) {
          inFence = !inFence;
          continue;
        }
        if (inFence) continue;
        lines[i] = lines[i]
          // 块级公式 $$...$$（可跨行不行——按行内非贪婪处理，跨行块级公式罕见）
          .replace(/\$\$([^$\n]+?)\$\$/g, (_m, tex: string) => {
            const marker = `${markerPrefix}m${counter++}__`;
            placeholders.push({ marker, code: "", id: nextMermaidId("math"), math: { tex: tex.trim(), displayMode: true } });
            return `<span class="math-ph" data-marker="${marker}"></span>`;
          })
          // 行内公式 $...$（内容非空、两端不紧邻空格、不含 $）
          .replace(/(^|[^\$\\])\$([^$\n]+?)\$(?!\d)/g, (m, pre: string, tex: string) => {
            const trimmed = tex.trim();
            if (!trimmed || trimmed.startsWith(" ") || trimmed.endsWith(" ")) return m;
            const marker = `${markerPrefix}m${counter++}__`;
            placeholders.push({ marker, code: "", id: nextMermaidId("math"), math: { tex: trimmed, displayMode: false } });
            return `${pre}<span class="math-ph" data-marker="${marker}"></span>`;
          });
      }
      return lines.join("\n");
    };
    mathContent = _extractMath(mathContent);

    let rawHtml = "";
    try {
      rawHtml = marked.parse(mathContent) as string;  // ✅ BUG修复：用 mathContent（含公式占位符）而非 processed（无公式占位符）
    } catch (e: any) {
      rawHtml = `<pre style="color:red">Markdown 解析失败: ${e.message}</pre>`;
    }
    const clean = DOMPurify.sanitize(rawHtml, {
      ADD_ATTR: ["data-marker"],
      WHOLE_DOCUMENT: false,
    });
    return { clean, placeholders };
  }, [debouncedContent]);

  // ✅ P1-6：主 effect 只负责 DOM 写入 + mermaid 渲染 + AI 修复监听；
  //    无图表代码块时跳过 mermaid 阶段（不加载渲染引擎）
  useEffect(() => {
    let cancelled = false;
    // ✅ 修复：chart-json 图表（labor/layout 等）走后端渲染返回 PNG blob，
    //    createObjectURL 生成的 URL 此前从不回收 —— 每次正文变化都会新渲染一轮
    //    图表，泄漏的 blob 与浏览器内存只增不减。统一在 cleanup 里回收。
    const objectUrls: string[] = [];
    setErrMsg(null);
    setHtml(parsed.clean);
    if (parsed.placeholders.length === 0) return;

    // ✅ BUG 修复：AI 修复按钮的事件委托改为**同步挂载**，不再放在 async 渲染循环之后。
    // 原实现挂载点在 await 之后：parsed 在 500ms 防抖窗口内连续变化、或组件快速卸载时，
    // cleanup 会先于挂载执行 —— 监听器被挂到已被替换的 DOM 上（泄漏），或挂载本身丢失，
    // 表现为「AI 自动修复」按钮点击无响应。事件委托挂在稳定的容器 ref 上，同步挂载即可。
    const handleClick = async (ev: MouseEvent) => {
      const target = (ev.target as HTMLElement)?.closest("button[data-fix-code]") as HTMLButtonElement | null;
      if (!target) return;
      ev.preventDefault();
      if (target.disabled) return;
      const code = decodeURIComponent(target.getAttribute("data-fix-code") || "");
      const error = decodeURIComponent(target.getAttribute("data-fix-error") || "渲染失败");
      if (!code) return;
      target.disabled = true;
      target.textContent = "AI 修复中...";
      try {
        // ✅ BUG 修复：不再硬编码 chart_type="flowchart" —— gantt/sequence 等图表
        //    类型不符时，后端按 (section_id, chart_type) 定位 chart_predictions 必然
        //    查不到行，回写与正文同步全部静默失效。类型由后端按代码本体推断。
        const sid = sectionIdRef.current;
        const resp = await chartsApi.fixMermaid({
          code, error,
          ...(sid ? { section_id: sid } : {}),
        });
        const fixed = resp?.data?.code;
        if (fixed && fixed.trim()) {
          const mermaid = await ensureMermaid();
          const parent = target.closest<HTMLElement>(".mmd-err");
          if (parent) {
            try {
              const renderId = nextMermaidId("mmd-fix");
              const { svg } = await mermaid.render(renderId, fixed);
              parent.innerHTML = svg;
              parent.classList.remove("mmd-err");
              parent.classList.add("mmd-rendered");
            } catch {
              target.textContent = "修复后仍渲染失败，请手动检查";
              target.disabled = false;
            }
          }
          // ✅ 后端已同步重写章节正文：通知父组件刷新预览状态，
          // 否则切走再切回 / 重渲染后旧坏代码会再次出现，且与导出内容不一致
          if (resp?.data?.content_updated && resp?.data?.new_content) {
            onContentReplacedRef.current?.(resp.data.new_content as string);
          }
        } else {
          target.textContent = "修复返回空代码";
          target.disabled = false;
        }
      } catch (e: any) {
        target.textContent = `修复失败: ${e?.message || "未知错误"}`;
        target.disabled = false;
      }
    };
    const container = ref.current;
    container?.addEventListener("click", handleClick);

    // ✅ AI 文生图：v17 起配图在导出 DOCX 时由后端自动生成（图表全自动生成、
    //    无人工生图按钮），占位卡片仅作信息展示。

    // 4. 渲染 mermaid
    (async () => {
      if (!ref.current) return;
      // 等待 setHtml 的新 DOM 渲染到 ref（避免无 await 时同步查询到旧占位结构）
      await new Promise<void>((r) => setTimeout(r, 0));
      if (cancelled || !ref.current) return;
      try {
        // ✅ KaTeX：先渲染公式占位符（math 占位符不应触发 mermaid 引擎加载）
        const mathPhs = parsed.placeholders.filter((p) => p.math);
        if (mathPhs.length > 0 && ref.current) {
          const katex = await ensureKatex();
          if (cancelled || !ref.current) return;
          for (const ph of mathPhs) {
            const div = ref.current.querySelector<HTMLSpanElement>(`span.math-ph[data-marker="${ph.marker}"]`);
            if (!div) continue;
            try {
              div.innerHTML = katex.renderToString(ph.math!.tex, {
                displayMode: ph.math!.displayMode,
                throwOnError: false,
                strict: false,
              });
            } catch {
              div.textContent = ph.math!.tex; // 渲染失败回退显示原文
            }
          }
        }

        // ✅ P1-6：全部 mermaid 块命中 SVG 缓存时，无需加载渲染引擎
        const chartPhs = parsed.placeholders.filter((p) => !p.json && !p.math);
        const needEngine = chartPhs.length > 0 && chartPhs.some((p) => !_svgCache.has(p.code));
        const mermaid = needEngine ? await ensureMermaid() : null;
        if (needEngine && (cancelled || !ref.current)) return;

        // 找到所有占位 div
        const phDivs = ref.current.querySelectorAll<HTMLDivElement>(".mmd-ph");

        // ✅ 性能修复：mermaid 块旧实现串行 await 渲染，一张图（弱引擎下数百 ms）
        // 会阻塞后续所有图，长正文中图表逐张出现、总耗时线性叠加。改为并发池（≤4）。
        const MERMAID_POOL = 4;
        const mermaidJobs: { div: HTMLDivElement; ph: Placeholder }[] = [];
        for (const div of phDivs) {
          const marker = div.getAttribute("data-marker");
          const ph = parsed.placeholders.find((p) => p.marker === marker);
          if (!ph) continue;
          // ✅ AI 文生图占位卡片由下方独立循环渲染，主循环跳过避免误当 mermaid 渲染
          if (ph.aiImage) continue;
          // ✅ chart-json 数据块：调后端渲染引擎渲染 PNG（labor/layout 等
          // Mermaid 无法表达的类型，与导出 DOCX 同一渲染轨）——各自独立 IIFE 并发
          if (ph.json) {
            (async () => {
              div.innerHTML = `<div style="padding:12px;color:#999;font-size:12px">📊 图表渲染中…（${DOMPurify.sanitize(ph.json!.title || ph.json!.chart_type || CHART_TITLE_FALLBACK)}）</div>`;
              try {
                const resp = await chartsApi.render({
                  chart_type: ph.json!.chart_type,
                  code: ph.code,
                  skip_http: true,
                });
                if (cancelled) return;
                const blob = new Blob([resp.data], { type: "image/png" });
                const url = URL.createObjectURL(blob);
                objectUrls.push(url);
                div.innerHTML = `<img src="${url}" alt="${DOMPurify.sanitize(ph.json!.title)}" style="max-width:100%" />`;
                div.classList.remove("mmd-ph");
                div.classList.add("mmd-rendered");
              } catch {
                if (cancelled) return;
                div.innerHTML = `<div style="padding:12px;border:1px dashed #faad14;border-radius:4px;color:#ad6800;background:#fffbe6;font-size:12px">📊 ${DOMPurify.sanitize(ph.json!.title || CHART_TITLE_FALLBACK)}${ph.json!.chart_type ? `（${DOMPurify.sanitize(ph.json!.chart_type)}）` : ""}<br/><span style="color:#999">渲染引擎暂不可用，导出 DOCX 时将再次尝试</span></div>`;
                div.classList.remove("mmd-ph");
              }
            })();
            continue;
          }
          mermaidJobs.push({ div, ph });
        }

        // mermaid 渲染并发池：每次 await 后检查 cancelled，组件卸载/内容刷新即放弃写 DOM
        let jobCursor = 0;
        const renderOneMermaid = async () => {
          while (true) {
            if (cancelled || !ref.current) return;
            const job = mermaidJobs[jobCursor++];
            if (!job) return;
            const { div, ph } = job;
            // 占位 div 可能已被新一轮渲染替换/移除
            if (!div.isConnected) continue;
            try {
              // ✅ P1-6：SVG 按代码内容复用——同一图表在编辑/切换中不重复渲染
              // ✅ 命中即刷新 LRU 位置（热点图表不被误淘汰）
              let svg = _svgCacheGet(ph.code) || "";
              if (!svg) {
                if (!mermaid) return;
                const { svg: _svg } = await mermaid.render(ph.id, ph.code);
                if (cancelled) return;
                svg = _svg;
                _svgCacheSet(ph.code, svg);
              }
              if (!div.isConnected) continue;
              div.innerHTML = svg;
              div.classList.remove("mmd-ph");
              div.classList.add("mmd-rendered");
            } catch (renderErr: any) {
              if (cancelled || !div.isConnected) continue;
              // 渲染失败：显示代码 + 错误提示 + AI修复按钮
              const errId = `mmd-err-${ph.id}`;
              div.innerHTML = `<pre style="color:#a00;background:#fff0f0;padding:8px;border-radius:4px;font-size:12px"><b>Mermaid 渲染错误：</b>${DOMPurify.sanitize(
                renderErr?.message || String(renderErr)
              )}\n\n${DOMPurify.sanitize(ph.code)}</pre>
              <div style="margin-top:6px"><button id="${errId}" data-fix-code="${encodeURIComponent(ph.code)}" data-fix-error="${encodeURIComponent(renderErr?.message || '渲染失败')}" style="padding:4px 12px;font-size:12px;border:1px solid #1677ff;border-radius:4px;background:#1677ff;color:#fff;cursor:pointer">AI 自动修复</button></div>`;
              div.classList.remove("mmd-ph");
              div.classList.add("mmd-err");
            }
          }
        };
        await Promise.all(
          Array.from({ length: Math.min(MERMAID_POOL, mermaidJobs.length) }, () => renderOneMermaid())
        );

        // ✅ AI 文生图（ai_image）占位卡片（信息展示；导出时后端自动生成真实图片）
        for (const ph of parsed.placeholders.filter((p) => p.aiImage)) {
          if (cancelled || !ref.current) return;
          const div = ref.current.querySelector<HTMLDivElement>(`.mmd-ph[data-marker="${ph.marker}"]`);
          if (!div) continue;
          const info = ph.aiImage!;
          div.innerHTML = `<div style="padding:14px;border:1px dashed #1677ff;border-radius:6px;background:#f0f7ff;font-size:13px">
            <div style="margin-bottom:8px">🖼️ <b>${DOMPurify.sanitize(info.title || "AI 配图")}</b><br/>
            <span style="color:#666">${DOMPurify.sanitize((info.prompt || "").slice(0, 90))}</span></div>
            <span style="color:#1677ff;font-size:12px">该配图将在导出 DOCX 时自动生成</span>
          </div>`;
          div.classList.remove("mmd-ph");
        }
      } catch (e: any) {
        setErrMsg(`Mermaid 加载失败: ${e.message}`);
      }

    })();

    return () => {
      cancelled = true;
      container?.removeEventListener("click", handleClick);
      objectUrls.forEach((u) => URL.revokeObjectURL(u));
      objectUrls.length = 0;
    };
  }, [parsed]);

  return (
    <div className="markdown-body">
      <div ref={ref} dangerouslySetInnerHTML={{ __html: html }} />
      {errMsg && (
        <div style={{ color: "red", padding: 8, background: "#fff0f0" }}>{errMsg}</div>
      )}
    </div>
  );
}

// ✅ 性能优化：Markdown 样式提到模块级并只注入一次。
// 旧实现每个 MarkdownRenderer 实例都渲染一份内联 <style> —— 同屏 N 个实例
// 就是 N 份重复 CSS 规则进 CSSOM，样式 recalc 成本随实例数线性放大。
// 选择器与规则内容完全不变，仅去重注入次数。
const MARKDOWN_STYLE_TEXT = `
  /* 2026-09-23：行间距紧凑化（项目提取/正文均受益），原 line-height 1.7 + margin 0.5em 过于松散 */
  .markdown-body { line-height: 1.4; font-size: 14px; color: #333; }
  .markdown-body h1, .markdown-body h2, .markdown-body h3 { margin-top: 1em; margin-bottom: 0.4em; }
  .markdown-body h1 { font-size: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }
  .markdown-body h2 { font-size: 1.3em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }
  .markdown-body h3 { font-size: 1.1em; }
  .markdown-body p { margin: 0.2em 0; }
  .markdown-body ul, .markdown-body ol { padding-left: 1.5em; margin: 0.2em 0; }
  .markdown-body ul { list-style-type: disc; }
  .markdown-body ol { list-style-type: decimal; }
  .markdown-body li { margin: 0.05em 0; }
  .markdown-body table { border-collapse: collapse; margin: 0.2em 0; width: 100%; }
  .markdown-body th, .markdown-body td { border: 1px solid #ddd; padding: 4px 8px; text-align: left; }
  .markdown-body th { background: #f5f5f5; font-weight: 600; }
  .markdown-body code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
  .markdown-body pre { background: #f4f4f4; padding: 8px; border-radius: 4px; overflow-x: auto; }
  .markdown-body pre code { background: none; padding: 0; }
  .markdown-body blockquote { border-left: 4px solid #ddd; margin: 0.2em 0; padding: 0 1em; color: #666; }
  .markdown-body hr { border: none; border-top: 1px solid #eee; margin: 0.5em 0; }
  .markdown-body img { max-width: 100%; }
  .markdown-body a { color: #1677ff; }
  .mmd-rendered { text-align: center; margin: 0.5em 0; overflow-x: auto; }
  .math-ph { display: inline-block; }
  .katex-display { margin: 0.4em 0; overflow-x: auto; }
`;
let _markdownStyleInjected = false;
export function ensureMarkdownStyle(): void {
  if (_markdownStyleInjected || typeof document === "undefined") return;
  const el = document.createElement("style");
  el.setAttribute("data-markdown-body-style", "1");
  el.textContent = MARKDOWN_STYLE_TEXT;
  document.head.appendChild(el);
  _markdownStyleInjected = true;
}
ensureMarkdownStyle();

export default memo(MarkdownRenderer);
