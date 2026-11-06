import { useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { FormInstance, ModalFuncProps } from "antd";
import { chartsApi, exportApi } from "../../api";
import {
  renderChartsForExport,
  type ExportChartImage,
  type ExportChartItem,
} from "../../utils/exportCharts";
import { selectPlacedExportCharts } from "../../utils/workflowDerived";
import {
  chartRenderStatsParts,
  fallbackBorrowWarning,
  fixStatsParts,
  ganttTruncatedWarning,
  globalFactsStatusWarning,
  noHttpFallbackWarning,
  noPilFallbackWarning,
  parseChartRenderStats,
  parseExportBlock,
  parseExportDegraded,
  parseExportOverride,
  parseFixStats,
  parseGlobalFactsStatus,
  resolveExportError,
  resolveExportFilename,
} from "../../utils/exportResponse";
import type { createSseBatcher } from "../../utils/sseBatcher";
import { deepMergeExportConfig } from "./exportShared";
import type { SchemeRecord } from "./workbenchTypes";

/**
 * ✅ 2026-11-04 拆分（G9）：导出主链自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：`handleExport` / `handleCancelExport` 函数体逐字搬运
 * （含 R-3 同步锁、导出世代号、图表渲染批量器、降级/事实状态告警消费）。
 * `exportPctRef` 只服务于图表渲染进度去重，随本 hook 一并落地。
 */
export interface ExportRunDeps {
  id: string | undefined;
  scheme: SchemeRecord | null;
  msg: {
    success: (s: string, d?: number) => void;
    error: (s: string) => void;
    warning: (s: string, d?: number) => void;
    info: (s: string, d?: number) => void;
  };
  exportForm: FormInstance;
  /** ✅ B-15：high 级问题阻断后的「已知悉并继续」确认弹窗（显式覆写，非静默开关）。 */
  modal: { confirm: (config: ModalFuncProps) => unknown };
  exportingRef: { current: boolean };
  exportSeqRef: { current: number };
  exportAbortRef: { current: AbortController | null };
  exportBatcherRef: { current: ReturnType<typeof createSseBatcher> | null };
  currentSchemeIdRef: { current: string };
  setExporting: (v: boolean) => void;
  setExportStage: Dispatch<SetStateAction<"idle" | "charts" | "docx" | "pdf">>;
  setExportPct: (v: number) => void;
  setExportPhase: (v: string) => void;
  loadCacheStatus: () => Promise<void>;
}

export function useExportRun(deps: ExportRunDeps) {
  const {
    id,
    scheme,
    msg,
    exportForm,
    modal,
    exportingRef,
    exportSeqRef,
    exportAbortRef,
    exportBatcherRef,
    currentSchemeIdRef,
    setExporting,
    setExportStage,
    setExportPct,
    setExportPhase,
    loadCacheStatus,
  } = deps;

  // ✅ 导出图表渲染进度：上一次已刷新的整数百分比（同百分比内不再排队刷新）
  const exportPctRef = useRef(-1);

  const handleExport = async (format: "docx" | "pdf" = "docx", overrideHighIssues = false) => {
    if (!id || exportingRef.current) return;
    const sid = id;
    const controller = new AbortController();
    const exportSeq = ++exportSeqRef.current;
    exportAbortRef.current?.abort();
    exportAbortRef.current = controller;
    exportingRef.current = true;
    exportPctRef.current = -1;
    setExporting(true);
    setExportStage("charts");
    setExportPct(0);
    setExportPhase("正在渲染图表...");
    // ✅ B-15：阻断后用户确认覆写 → 本轮结束后带着显式标记重试一次。
    let retryOverride = false;
    try {
      const validated = await exportForm.validateFields();
      const config = deepMergeExportConfig(exportForm.getFieldsValue(true), validated);
      let chartImages: ExportChartImage[] = [];
      try {
        const { data } = await chartsApi.list(sid, controller.signal);
        if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
        const items: ExportChartItem[] = selectPlacedExportCharts(data.items || []).map((p) => ({
          chart_type: p.chart_type,
          mermaid_code: p.code!,
        }));
        chartImages = await renderChartsForExport(
          items,
          (doneCount, totalCount) => {
            if (totalCount > 0) {
              const pct = Math.floor((doneCount / totalCount) * 100);
              const text = `正在渲染图表...（${doneCount}/${totalCount}）`;
              if (pct === exportPctRef.current) return;
              exportPctRef.current = pct;
              exportBatcherRef.current?.schedule("phase", () => {
                setExportPhase(text);
                setExportPct(Math.round(pct * 0.7));
              });
            }
          },
          controller.signal,
        );
        if (chartImages.length > 0) {
          msg.info(`已按预览样式渲染 ${chartImages.length}/${items.length} 张图表`, 3);
        }
      } catch (e) {
        if (controller.signal.aborted) throw e;
        chartImages = []; // 前端渲染失败不阻塞导出，图表走后端渲染轨兜底
        msg.info("图表前端渲染轨未启用（网络/浏览器异常），图表将由后端兜底渲染", 4);
      }
      exportBatcherRef.current?.flushNow();
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      setExportStage(format === "pdf" ? "pdf" : "docx");
      setExportPct(70);
      setExportPhase(format === "pdf" ? "正在生成文档并转换PDF..." : "正在生成文档...");
      const resp =
        format === "pdf"
          ? await exportApi.pdf(sid, config, chartImages, controller.signal, overrideHighIssues)
          : await exportApi.docx(sid, config, chartImages, controller.signal, overrideHighIssues);
      if (
        controller.signal.aborted ||
        exportSeqRef.current !== exportSeq ||
        currentSchemeIdRef.current !== sid
      )
        return;
      const mime =
        format === "pdf"
          ? "application/pdf"
          : "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      const blob = new Blob([resp.data], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const downloadName = resolveExportFilename(
        resp.headers,
        `${scheme?.name || "方案"}.${format}`,
      );
      a.download = downloadName;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      msg.success(format === "pdf" ? "PDF 导出成功" : "导出成功");
      if (currentSchemeIdRef.current === sid) await loadCacheStatus();
      if (
        controller.signal.aborted ||
        exportSeqRef.current !== exportSeq ||
        currentSchemeIdRef.current !== sid
      )
        return;
      const stats = parseChartRenderStats(resp.headers);
      if (stats) {
        const parts = chartRenderStatsParts(stats);
        if (parts.length) msg.info(`图表渲染：${parts.join(" / ")}`, 5);
        // ✅ R179（G-06）：泛化失败提示只覆盖「可能有本地兜底」的部分；HTTP-only
        //    类型（mindmap/sequence 等）单独走 noPilFallbackWarning，避免给出
        //    「开启允许 PIL 兜底」这种对它们完全无效的建议。
        const noPil = Math.max(0, Number(stats.no_pil_fallback || 0));
        // ✅ R181（2026-11-06）：甘特 JSON 只有本地 PIL、没有 HTTP 兜底，
        //    失败单独成类展示；从泛化 failed 中扣除，避免双重提示误导。
        const noHttp = Math.max(0, Number(stats.no_http_fallback || 0));
        const genericFailed = Math.max(0, Number(stats.failed || 0) - noPil - noHttp);
        if (genericFailed > 0) {
          msg.warning(
            `${genericFailed} 张图表渲染失败，建议检查图表代码或开启「允许 PIL 兜底」后重试`,
          );
        }
        const noPilWarn = noPilFallbackWarning(stats);
        if (noPilWarn) msg.warning(noPilWarn, 8);
        const noHttpWarn = noHttpFallbackWarning(stats);
        if (noHttpWarn) msg.warning(noHttpWarn, 8);
        const fbWarn = fallbackBorrowWarning(stats);
        if (fbWarn) msg.warning(fbWarn, 8);
        const ganttWarn = ganttTruncatedWarning(stats);
        if (ganttWarn) msg.warning(ganttWarn, 8);
      }
      const fs = parseFixStats(resp.headers);
      if (fs) {
        const parts = fixStatsParts(fs);
        if (parts.length) msg.success(`导出前已自动修复：${parts.join(" / ")}`, 6);
      }
      const degraded = parseExportDegraded(resp.headers);
      if (degraded) msg.warning(degraded.message, 8);
      // ✅ B-15：覆写放行必须让用户看得见（本次是显式动作且已写入导出留痕）。
      const overrideSignal = parseExportOverride(resp.headers);
      if (overrideSignal) {
        msg.warning(
          "本次导出已按「已知悉并继续」执行：存在未修复的高风险问题，覆写动作已写入导出留痕",
          8,
        );
      }
      const factsWarn = globalFactsStatusWarning(parseGlobalFactsStatus(resp.headers));
      if (factsWarn) msg.warning(factsWarn, 8);
    } catch (e: unknown) {
      // ✅ B-15：默认阻断 → 显式确认后才带 override 重试；非阻断错误走原路径。
      const blocked = overrideHighIssues ? null : await parseExportBlock(e);
      if (blocked && exportSeqRef.current === exportSeq && currentSchemeIdRef.current === sid) {
        retryOverride = await new Promise<boolean>((resolve) => {
          modal.confirm({
            title: "导出已被阻断：存在高风险问题",
            content: blocked.detail,
            okText: "已知悉并继续",
            cancelText: "返回修复",
            okButtonProps: { danger: true },
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!retryOverride) {
          msg.warning("已取消导出：请先修复高风险问题后再重试", 6);
        }
      } else {
        const resolved = await resolveExportError(e);
        if (
          !resolved.canceled &&
          exportSeqRef.current === exportSeq &&
          currentSchemeIdRef.current === sid
        ) {
          msg.error(resolved.message);
          const errHeaders = (e as { response?: { headers?: unknown } } | null)?.response?.headers;
          const factsWarn = globalFactsStatusWarning(parseGlobalFactsStatus(errHeaders));
          if (factsWarn) msg.warning(factsWarn, 8);
        }
      }
    } finally {
      if (exportSeqRef.current === exportSeq && exportAbortRef.current === controller) {
        exportAbortRef.current = null;
        exportingRef.current = false;
        setExporting(false);
        setExportPhase("");
        setExportStage("idle");
        setExportPct(0);
      }
    }
    if (
      retryOverride &&
      !controller.signal.aborted &&
      exportSeqRef.current === exportSeq &&
      currentSchemeIdRef.current === sid
    ) {
      await handleExport(format, true);
    }
  };

  const handleCancelExport = () => {
    if (!exportingRef.current) return;
    if (id) exportApi.cancel(id).catch(() => {});
    exportAbortRef.current?.abort();
    msg.info("已发送停止指令，服务端将在当前步骤边界退出");
  };

  return { handleExport, handleCancelExport };
}
