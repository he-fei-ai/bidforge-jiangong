import { useCallback, useRef, useState } from "react";
import {
  upsertSectionLog, finalizeRunningLogsIn, mergeFailedSectionsInto,
  contentResultFailedSections, contentResultSummary,
  type SectionLogItem, type GenStats,
} from "../utils/contentEvents";
import { tasksApi, sectionsApi } from "../api";
// F-CONTENT-STANDARD(2026-09-26): 生成标准请求体映射（与页面共用同一纯函数）
import {
  buildStandardRequestFields, standardSummaryHint,
  type GenerationStandardChoice,
} from "../utils/contentStandard";

/**
 * 正文生成状态与操作 Hook（从 SchemeWorkbenchPage.tsx 抽离，2026-09-21）。
 *
 * 承载内容生成的 SSE 事件处理、任务控制（暂停/恢复/停止）、
 * 重置正文、章节日志状态机。页面侧仅保留 setState 胶水与 UI 绑定。
 *
 * 抽离动机：正文 Tab 逻辑此前内联在 7356 行工作台页面里，
 * 无法独立测试。本 Hook 将状态与副作用收口，页面通过返回值
 * 绑定 ContentGenerationTab / SectionContentCard 的回调。
 */
export interface ContentGenerationState {
  generating: boolean;
  genType: string;
  taskPaused: boolean;
  progress: number;
  progressMsg: string;
  sectionLogs: SectionLogItem[];
  genStats: GenStats;
  crSummary: { total?: number; repaired?: number } | null;
  crScanId: string | null;
}
export interface ContentGenerationActions {
  generate: (extra?: { section_id?: string; mode?: string; force_rewrite?: boolean }) => void;
  generateCurrent: (sectionId: string) => void;
  generateMissing: () => void;
  continueCurrent: (sectionId: string) => void;
  control: (action: "pause" | "resume" | "stop") => void;
  reset: () => void;
  setProgress: (v: number) => void;
  setProgressMsg: (v: string) => void;
}
export interface UseContentGenerationOpts {
  schemeId: string | null;
  concurrencyOption: string;
  resolveWordBudgetOverride: () => number | undefined;
  autoConsistencyRepair: boolean;
  consistencySeverity: string;
  autoShrinkOver: boolean;
  /** 强制全量重修：true 时收尾的一致性修复不跳过「已修复且成果仍在」的冲突 */
  forceFullRepair: boolean;
  /**
   * F-CONTENT-STANDARD(2026-09-26)：任务级生成标准选择。
   * precise/fuzzy → 强覆盖本次全部章节；inherit → 不传字段，逐章回落
   * （章节级 → 方案级 → 默认精准）。
   */
  generationStandard?: GenerationStandardChoice;
  load: () => Promise<unknown> | void;
  getActiveTaskId: () => string | null;
  setActiveTab: (tab: string) => void;
  onSseEvent?: (evt: any) => boolean;
  msg: { success: (t: string) => void; warning: (t: string) => void; error: (t: string) => void; info: (t: string) => void };
  sseFetch: (url: string, body?: Record<string, unknown>, opts?: { signal?: AbortSignal }) => AsyncIterable<any>;
}

export function useContentGeneration(opts: UseContentGenerationOpts): ContentGenerationState & ContentGenerationActions {
  const { schemeId, concurrencyOption, resolveWordBudgetOverride, autoConsistencyRepair, consistencySeverity, autoShrinkOver, forceFullRepair, generationStandard, load, getActiveTaskId, setActiveTab, onSseEvent, msg, sseFetch } = opts;
  const [generating, setGenerating] = useState(false);
  const [genType, setGenType] = useState("");
  const [taskPaused, setTaskPaused] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState("");
  const [sectionLogs, setSectionLogs] = useState<SectionLogItem[]>([]);
  const [genStats, setGenStats] = useState<GenStats>({});
  const [crSummary, setCrSummary] = useState<{ total?: number; repaired?: number } | null>(null);
  const [crScanId, setCrScanId] = useState<string | null>(null);
  const sectionLogsRef = useRef<SectionLogItem[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);
  const finalizeRunning = useCallback((reason: string, status: "skipped" | "failed" = "skipped") => { const next = finalizeRunningLogsIn(sectionLogsRef.current, reason, status); if (next === sectionLogsRef.current) return; sectionLogsRef.current = next; setSectionLogs([...next]); }, []);
  const mergeFailed = useCallback((list: unknown) => { const next = mergeFailedSectionsInto(sectionLogsRef.current, list); if (next === sectionLogsRef.current) return; sectionLogsRef.current = next; setSectionLogs([...next]); }, []);
  // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：section_start/section_done 携带的
  //    generation_standard / standard_report 必须落到日志项，否则模式徽标与
  //    校验告警无从展示（字段存在但被丢弃 = 用户看不到任何生成标准信息）。
  const handleSectionStart = (evt: any) => { sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, { section_id: evt.section_id, title: evt.title, status: "running", index: evt.index, total: evt.total, time: Date.now(), stage: evt.stage, stage_label: evt.stage_label, generation_standard: evt.generation_standard }); setSectionLogs([...sectionLogsRef.current]); };
  // ✅ 缺口修复（2026-09-24）：后端 section_done 一直下发 content 与
  //    quality_issues（本章程序化质检问题），前端此前**两个字段都没消费** ——
  //    用户只能在生成结束后重新拉取整篇正文才知道有没有问题。现把质检问题
  //    落到章节日志项，日志区即可直接看到「哪章有质量告警」。
  const handleSectionDone = (evt: any) => { const ex = sectionLogsRef.current.find((x) => x.section_id === evt.section_id); sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, { section_id: evt.section_id, title: evt.title, status: "success", word_count: evt.word_count, word_budget: evt.word_budget, word_status: evt.word_status, continue_failed: !!evt.continue_failed, quality_issues: Array.isArray(evt.quality_issues) ? evt.quality_issues : undefined, generation_standard: evt.generation_standard, standard_report: evt.standard_report ?? undefined, index: ex?.index, total: ex?.total, time: Date.now(), duration: ex ? Date.now() - ex.time : undefined }); setSectionLogs([...sectionLogsRef.current]); };
  const handleSectionError = (evt: any) => { const ex = sectionLogsRef.current.find((x) => x.section_id === evt.section_id); sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, { section_id: evt.section_id, title: evt.title, status: "failed", reason: evt.reason, index: ex?.index, total: ex?.total, time: Date.now(), duration: ex ? Date.now() - ex.time : undefined, stage: ex?.stage, stage_label: ex?.stage_label }); setSectionLogs([...sectionLogsRef.current]); };
  const handleSectionStage = (evt: any) => { const ex = sectionLogsRef.current.find((x) => x.section_id === evt.section_id); if (ex) { sectionLogsRef.current = upsertSectionLog(sectionLogsRef.current, { ...ex, stage: evt.stage, stage_label: evt.stage_label }); setSectionLogs([...sectionLogsRef.current]); } };
  const generate = useCallback(async (extra?: any) => {
    if (!schemeId) return;
    const mySeq = ++seqRef.current;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setGenerating(true); setGenType("content"); setTaskPaused(false);
    setProgress(0); setProgressMsg("正在连接..."); setActiveTab("content");
    setSectionLogs([]); sectionLogsRef.current = []; setGenStats({});
    const body: Record<string, unknown> = {};
    const wbOverride = resolveWordBudgetOverride();
    if (wbOverride) body.word_budget_override = wbOverride;
    if (extra?.section_id) body.section_id = extra.section_id;
    if (extra?.mode) body.mode = extra.mode;
    body.concurrency = concurrencyOption;
    body.force_rewrite = !!extra?.force_rewrite;
    body.auto_consistency_repair = autoConsistencyRepair;
    body.consistency_severity = consistencySeverity;
    body.auto_shrink_over = autoShrinkOver;
    // ✅ F-CONTENT-STANDARD(2026-09-26 · F1/F2)：生成标准进入请求体。
    //    旧实现只读 `extra.task_standard`（所有调用方都不传）→ 选项完全失效。
    //    口径与页面内联 `handleGenerateContent` **共用同一纯函数**，杜绝两处漂移：
    //      precise/fuzzy → task_standard + override_section_standard=true
    //      inherit       → 不传字段（逐章回落，与旧客户端逐字节一致）
    if (extra?.task_standard) {
      // 显式传值必须同时要求覆盖，否则后端 override=false 会静默忽略它（FR-3）
      body.task_standard = extra.task_standard;
      body.override_section_standard = true;
    } else {
      Object.assign(body, buildStandardRequestFields(generationStandard));
    }
    // ✅ 强制全量重修：true 时收尾的一致性修复不跳过「已修复且成果仍在」的冲突
    body.force_full_repair = forceFullRepair;
    let taskId = "";
    try {
      for await (const evt of sseFetch(`/sse/generate-content/${schemeId}`, body, { signal: ac.signal })) {
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (onSseEvent?.(evt)) { finalizeRunning("生成失败（任务已终止）", "failed"); break; }
        if (evt.event === "section_start") handleSectionStart(evt);
        else if (evt.event === "section_done") handleSectionDone(evt);
        else if (evt.event === "section_error") handleSectionError(evt);
        else if (evt.event === "section_stage") handleSectionStage(evt);
        else if (evt.event === "stats" || evt.event === "ping") { setGenStats(evt as GenStats); if (typeof evt.progress === "number") setProgress(evt.progress); }
        else if (evt.event === "consistency_scan_start") { setProgress(evt.progress ?? 0.95); setProgressMsg("正文完成，开始全文一致性扫描..."); }
        else if (evt.event === "consistency_scan_progress") { if (typeof evt.progress === "number") setProgress(evt.progress); setProgressMsg(evt.message || "正在扫描全文一致性..."); }
        else if (evt.event === "consistency_scan_done") { setProgress(0.98); setProgressMsg(`一致性扫描完成：发现 ${evt.summary?.total ?? 0} 处冲突`); }
        else if (evt.event === "consistency_repair_progress") { if (typeof evt.progress === "number") setProgress(evt.progress); setProgressMsg(evt.message || "正在定向修复冲突..."); }
        else if (evt.event === "consistency_repair_done") { setProgress(1); setProgressMsg(`一致性修复完成：已修复 ${evt.repaired ?? 0} 处`); }
        else if (evt.event === "consistency_skipped") { setProgress(1); setProgressMsg(evt.reason || "方案尚无正文"); }
        else if (evt.event === "consistency_failed") { setProgressMsg(`全文一致性处理失败：${evt.reason || ""}`); }
        if (evt.event === "completed") {
          setProgress(1);
          const fc = (typeof evt.failed_count === "number") ? evt.failed_count : sectionLogsRef.current.filter((l) => l.status === "failed").length;
          mergeFailed(evt.failed_sections); finalizeRunning("未完成（生成已结束）", "failed");
          const dm = evt.message || `正文生成完成，总字数 ${evt.word_count || 0}`;
          if (fc > 0) { msg.warning(dm + `（${fc} 章失败）`); setProgressMsg(dm + `（${fc} 章失败）`); }
          else { msg.success(dm); setProgressMsg(dm); }
          if (evt.consistency_summary) { setCrSummary(evt.consistency_summary); if (evt.consistency_summary.scan_id) setCrScanId(evt.consistency_summary.scan_id); }
          // ✅ F-CONTENT-STANDARD(2026-09-26 · F4)：生成标准校验汇总提示。
          //    仅在「有问题章数 > 0」时追加（无问题返回空串 → 不制造噪音）。
          const _stdHint = standardSummaryHint(evt.standard_summary);
          if (_stdHint) { msg.warning(_stdHint); setProgressMsg(dm + `　|　${_stdHint}`); }
          await load(); break;
        } else if (evt.event === "stopped") {
          if (typeof evt.progress === "number") setProgress(evt.progress);
          // ✅ 缺口修复（2026-09-24）：后端 stopped 事件现在随事件下发
          //    failed_sections / failed_count（此前只落 checkpoint）。
          //    走与 completed 相同的合并入口，停止后立刻能看到失败明细。
          mergeFailed(evt.failed_sections);
          finalizeRunning("已停止，未生成完成", "skipped");
          const _fc = typeof evt.failed_count === "number" ? evt.failed_count : 0;
          msg.info(_fc > 0 ? `已停止（${_fc} 章失败，详见下方日志）` : "已停止");
          await load(); break;
        } else {
          if (evt.progress !== undefined) setProgress(evt.progress);
          if (evt.message) setProgressMsg(evt.message);
        }
      }
    } catch (e: any) {
      if (e.name !== "AbortError") {
        msg.warning(e.message || "SSE 连接中断");
        finalizeRunning("连接中断，等待后台任务收尾", "skipped");
        if (taskId) {
          setProgressMsg("连接中断，正在重新挂接后台任务...");
          try {
            const fin = await new Promise<any>((resolve) => { const poll = async () => { try { const { data } = await tasksApi.status(taskId); if (data && ["completed","failed","stopped"].includes(data.status)) resolve(data); else setTimeout(poll, 3000); } catch { setTimeout(poll, 3000); } }; poll(); });
            const ck = contentResultFailedSections(fin?.content_result);
            if (ck.sections.length) { mergeFailed(ck.sections); msg.warning(`后台正文生成有 ${ck.failedCount} 章失败`); }
            const sm = contentResultSummary(fin?.content_result);
            if (sm.total > 0) { msg.info(`后台正文生成：已完成 ${sm.done}/${sm.total} 章，共 ${sm.words} 字`); }
            if (fin?.status === "completed") { msg.success("正文生成已在后台完成"); await load(); }
            else if (fin?.status === "failed") { msg.error(fin.message || "后台任务失败"); }
            else if (fin?.status === "stopped") { msg.info("后台任务已停止"); await load(); }
          } catch { /* 轮询失败静默 */ }
        }
      }
    } finally {
      if (abortRef.current === ac) abortRef.current = null;
    }
    if (seqRef.current === mySeq) { setGenerating(false); setGenType(""); setTaskPaused(false); }
  }, [schemeId, concurrencyOption, resolveWordBudgetOverride, autoConsistencyRepair, consistencySeverity, autoShrinkOver, forceFullRepair, generationStandard, load, setActiveTab, onSseEvent, msg, sseFetch, finalizeRunning, mergeFailed, handleSectionStart, handleSectionDone, handleSectionError, handleSectionStage]);

  return {
    generating, genType, taskPaused, progress, progressMsg, sectionLogs, genStats, crSummary, crScanId,
    generate,
    generateCurrent: useCallback((sid: string) => { generate({ section_id: sid, mode: "section", force_rewrite: false }); }, [generate]),
    generateMissing: useCallback(() => { generate({ mode: "missing" }); }, [generate]),
    continueCurrent: useCallback((sid: string) => { generate({ section_id: sid, mode: "continue", force_rewrite: false }); }, [generate]),
    control: useCallback(async (action: "pause" | "resume" | "stop") => {
      const tid = getActiveTaskId();
      if (!tid) { msg.warning("没有活跃的任务"); return; }
      try { const { data } = await tasksApi.control(tid, action); if (data.ok) { if (action === "pause") { setTaskPaused(true); msg.success("已暂停"); } else if (action === "resume") { setTaskPaused(false); msg.success("已恢复"); } else { setTaskPaused(false); msg.success("已停止"); } } }
      catch (e: any) { msg.error(e.message || "控制失败"); }
    }, [getActiveTaskId, msg]),
    reset: useCallback(async () => {
      if (!schemeId) return;
      try { const { data } = await sectionsApi.resetContent(schemeId); await load(); msg.success(`已重置：清除 ${data.cleared ?? 0} 个章节的正文`); }
      catch (e: any) { msg.error(e?.message || "重置失败"); }
    }, [schemeId, load, msg]),
    setProgress, setProgressMsg,
  };
}

