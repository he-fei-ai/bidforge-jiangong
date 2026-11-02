/**
 * 两轮次导出 Hook（R147 · 2026-10-30）。
 *
 * 契约（与后端 app/routers/export_flow.py 字段级对称）：
 * - POST /schemes/{id}/export/docx/v2 → 202 {task_id, events, download}；
 * - GET  /schemes/{id}/export/flow/{task_id}/events（SSE）：
 *     {stage:"draft"|"repair", phase, progress:0-100, message?,
 *      finding?:{category,code,severity,location,message},
 *      action?:{category,code,location,message,result},
 *      terminal?:bool, status?, report?, download?}
 * - 下载 / 取消走 exportApi.flowDownload / flowCancel。
 *
 * 断线兜底：SSE 异常中断且未收到 terminal → 收敛到
 * pollTaskUntilTerminalImpl（maxPolls 硬上限，R139 同款纪律，
 * 绝不无限递归等待），终态后尝试下载（404 则报错提示）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { exportApi, tasksApi } from "../api";
import { sseGetStream } from "../api";
import { pollTaskUntilTerminalImpl } from "../utils/taskPolling";
import { resolveExportFilename } from "../utils/exportResponse";

export interface FlowLogItem {
  kind: "finding" | "action" | "info";
  category: string;
  code: string;
  severity?: string;
  location?: string;
  message: string;
  result?: string;
}

export interface UnresolvedByCode {
  code: string;
  category: string;
  count: number;
  samples: { location: string; message: string }[];
  suggestion: string;
}

export interface FlowReportSummary {
  mode: string;
  /** 检出（初检 finding 总数） */
  detected?: number;
  /** 已修复（初检存在、终检消失的 finding 覆盖数） */
  fixed: number;
  /** 跳过（warning 级残留） */
  skipped?: number;
  /** 需人工（error 级残留，按 finding 计数） */
  manual?: number;
  /** 需人工项按 code 聚合（count + 3 条样本 + 处置建议） */
  unresolved_by_code?: UnresolvedByCode[];
  total_findings: number;
  total_actions: number;
  unresolved: number;
  by_category: Record<string, {
    found: number; error: number; warning: number;
    fixed: number; skipped?: number; manual?: number;
  }>;
  duration_ms: number;
}

export interface FlowReport {
  mode: string;
  summary: FlowReportSummary;
  findings: any[];
  actions: any[];
  unresolved: any[];
  checks: Record<string, any>;
  error?: string;
}

export interface ExportFlowState {
  running: boolean;
  stage: "draft" | "repair" | "packaging" | "";
  progress: number;
  stageMsg: string;
  logs: FlowLogItem[];
  report: FlowReport | null;
  taskId: string | null;
  fileName: string | null;
  error: string | null;
}

const INITIAL: ExportFlowState = {
  running: false,
  stage: "",
  progress: 0,
  stageMsg: "",
  logs: [],
  report: null,
  taskId: null,
  fileName: null,
  error: null,
};

export function useExportFlow(opts: {
  schemeId: string;
  msg: { success: (t: string) => void; warning: (t: string) => void; error: (t: string) => void; info: (t: string) => void };
  /** 下载完成后的回调（页面刷新列表等） */
  onDownloaded?: () => void;
}) {
  const { schemeId, msg, onDownloaded } = opts;
  const [state, setState] = useState<ExportFlowState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  useEffect(() => () => {
    // 卸载：断流（R144 教训：卸载后 setState 必须杜绝）
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const applyEvent = useCallback((evt: any, mySeq: number) => {
    setState((s) => {
      if (seqRef.current !== mySeq) return s; // 过期流（新导出已启动）
      const next: ExportFlowState = { ...s };
      if (typeof evt.progress === "number") next.progress = evt.progress;
      if (evt.stage === "draft" || evt.stage === "repair"
        || evt.stage === "packaging") next.stage = evt.stage;
      if (evt.message) next.stageMsg = evt.message;
      if (evt.finding) {
        next.logs = [...next.logs, {
          kind: "finding", category: evt.finding.category, code: evt.finding.code,
          severity: evt.finding.severity, location: evt.finding.location,
          message: evt.finding.message,
        }];
      }
      if (evt.action) {
        next.logs = [...next.logs, {
          kind: "action", category: evt.action.category, code: evt.action.code,
          location: evt.action.location, message: evt.action.message,
          result: evt.action.result,
        }];
      }
      if (evt.report) next.report = evt.report;
      if (evt.terminal) {
        next.running = false;
        if (evt.status === "failed") next.error = evt.message || "导出失败";
      }
      return next;
    });
  }, []);

  const download = useCallback(async (taskId: string, fallbackName: string) => {
    const resp = await exportApi.flowDownload(schemeId, taskId);
    const filename = resolveExportFilename(resp?.headers, fallbackName);
    const url = URL.createObjectURL(resp.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    onDownloaded?.();
  }, [schemeId, onDownloaded]);

  const start = useCallback(async (
    config: any,
    chartImages?: { chart_type: string; mermaid_code: string; png: string }[],
  ) => {
    if (state.running) return;
    const mySeq = ++seqRef.current;
    const ac = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ac;
    setState({ ...INITIAL, running: true, stageMsg: "正在启动导出任务…" });
    let taskId = "";
    try {
      const resp = await exportApi.docxV2(schemeId, config, chartImages, ac.signal);
      taskId = resp?.data?.task_id || "";
      if (!taskId) throw new Error("后端未返回 task_id");
      setState((s) => (seqRef.current === mySeq ? { ...s, taskId } : s));
      for await (const evt of sseGetStream(
        `/schemes/${schemeId}/export/flow/${taskId}/events`, { signal: ac.signal })) {
        if (seqRef.current !== mySeq) return;
        applyEvent(evt, mySeq);
        if (evt?.terminal) {
          if (evt.status === "failed") {
            msg.error(evt.message || "导出失败");
          } else {
            const mode = evt?.report?.mode;
            if (mode === "fallback_draft") {
              msg.warning("修复失败，已回滚交付初稿");
            } else {
              msg.success("导出完成（初稿 + 检查修复）");
            }
            try {
              await download(taskId, "导出文档.docx");
            } catch (e: any) {
              msg.error(`下载失败：${e?.message || e}`);
            }
          }
          return;
        }
      }
      // SSE 流结束但未收到 terminal（断线）→ 收敛轮询兜底（maxPolls 硬上限）
      const fin = await pollTaskUntilTerminalImpl(
        taskId,
        () => tasksApi.status(taskId).then((r: any) => r?.data ?? r),
        undefined,
        { intervalMs: 2000, maxPolls: 150 },
      );
      if (seqRef.current !== mySeq) return;
      if (!fin) {
        msg.warning("导出进度连接中断，请稍后在任务栏查看结果");
        setState((s) => (seqRef.current === mySeq
          ? { ...s, running: false, error: "连接中断" } : s));
        return;
      }
      setState((s) => (seqRef.current === mySeq ? { ...s, running: false } : s));
      if (fin.status === "failed") {
        msg.error(fin.message || "导出失败");
        return;
      }
      msg.warning("进度连接中断，导出已完成，正在重新拉取文件…");
      try {
        await download(taskId, "导出文档.docx");
      } catch (e: any) {
        msg.error(`下载失败：${e?.message || e}`);
      }
    } catch (e: any) {
      if (ac.signal.aborted) return;
      if (seqRef.current === mySeq) {
        setState((s) => ({ ...s, running: false, error: String(e?.message || e) }));
      }
      msg.error(`两轮次导出失败：${e?.message || e}`);
    }
  }, [schemeId, state.running, applyEvent, download, msg]);


  const cancel = useCallback(async () => {
    const taskId = state.taskId;
    abortRef.current?.abort();
    abortRef.current = null;
    seqRef.current++; // 使在途流失效
    setState((s) => ({ ...s, running: false, stageMsg: "已取消" }));
    if (taskId) {
      try { await exportApi.flowCancel(schemeId, taskId); } catch { /* 幂等 */ }
    }
    msg.info("已停止两轮次导出");
  }, [schemeId, state.taskId, msg]);

  const reset = useCallback(() => {
    seqRef.current++;
    setState(INITIAL);
  }, []);

  return { ...state, start, cancel, reset, download };
}
