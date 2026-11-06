import type { Dispatch, SetStateAction } from "react";
import { exportApi, sectionsApi } from "../../api";
import type { ExportPreflight, ExportRerunPlan, ExportStats } from "./useExportPresets";
import type { PlaceholderReport } from "../../utils/placeholderReport";
import type { ExportIssueGate, ExportPreflightSummary, QualityCheckResult } from "./workbenchTypes";

/**
 * ✅ 2026-11-04 拆分（G7）：导出预检 + 质量自检自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：
 *   `handleExportCheck` / `handleQualityCheck` 函数体逐字搬运（含 R-3 同步锁、
 *   B1-1 降级判据、R168 重入锁）。导出主链路 `handleExport`（`exportResponse`
 *   红线锚点）仍留在页面，未随本组搬迁。
 */
export interface ExportCheckDeps {
  id: string | undefined;
  msg: {
    success: (s: string, d?: number) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    /** ✅ B-17：无图表集合时用 info 播报「无法判定」，不得借用 success 语义。 */
    info: (s: string, d?: number) => void;
  };
  currentSchemeIdRef: { current: string };
  exportCheckAbortRef: { current: AbortController | null };
  checkingExportRef: { current: boolean };
  setCheckingExport: (v: boolean) => void;
  setExportIssues: Dispatch<SetStateAction<ExportIssueGate[]>>;
  setExportCheckIncomplete: (v: boolean) => void;
  setExportStats: Dispatch<SetStateAction<ExportStats | null>>;
  setExportPreflight: Dispatch<SetStateAction<ExportPreflight | null>>;
  setPlaceholderReport: Dispatch<SetStateAction<PlaceholderReport | null>>;
  setRerunPlan: Dispatch<SetStateAction<ExportRerunPlan | null>>;
  checkingQuality: boolean;
  setCheckingQuality: (v: boolean) => void;
  setQualityResult: Dispatch<SetStateAction<QualityCheckResult | null>>;
}

export function useExportCheck(deps: ExportCheckDeps) {
  const {
    id,
    msg,
    currentSchemeIdRef,
    exportCheckAbortRef,
    checkingExportRef,
    setCheckingExport,
    setExportIssues,
    setExportCheckIncomplete,
    setExportStats,
    setExportPreflight,
    setPlaceholderReport,
    setRerunPlan,
    checkingQuality,
    setCheckingQuality,
    setQualityResult,
  } = deps;

  const handleExportCheck = async () => {
    // ✅ R-3（2026-10-04）：同步锁，与 handleExport 的 exportingRef 同型。
    //    React 状态更新异步批处理，仅靠 exportCheckAbortRef 中止旧请求不够——
    //    快速双击会在同一帧内并发进入，两次 await 交错 setState 造成 UI 状态撕裂。
    if (!id || checkingExportRef.current) return;
    const sid = id;
    checkingExportRef.current = true;
    exportCheckAbortRef.current?.abort();
    const controller = new AbortController();
    exportCheckAbortRef.current = controller;
    setCheckingExport(true);
    // 新一轮预检开始即撤销旧结论；失败时保持阻断，避免旧方案的放行状态继续可用。
    setExportIssues([]);
    setExportCheckIncomplete(false);
    setExportStats(null);
    setExportPreflight(null);
    setPlaceholderReport(null);
    setRerunPlan(null);
    try {
      const { data } = await exportApi.check(sid, controller.signal);
      if (
        controller.signal.aborted ||
        currentSchemeIdRef.current !== sid ||
        exportCheckAbortRef.current !== controller
      )
        return;
      setExportIssues(data.issues || []);
      setExportStats({
        section_count: data.section_count,
        generated_count: data.generated_count,
        total_words: data.total_words,
        empty_ratio: data.empty_ratio,
        chart_total: data.chart_total,
        chart_done: data.chart_done,
        // ✅ B-17（R182 · 2026-11-06）：交叉引用三态（无图表集合 → 无法判定）。
        crossref_check: data.crossref_check,
      });
      // ✅ G1：导出预检反向打通就绪度总检 —— 同一份数据、同一套规则词表，
      // 用户不用在两个页面之间来回比对「到底能不能交付」
      // 后端 SSE 契约：export/check 响应的 preflight_summary（ExportPreflight + has_run）
      const ps = (data.preflight_summary || null) as ExportPreflightSummary | null;
      setExportPreflight(ps && ps.has_run ? ps : null);
      // ✅ 《待补充清单》（2026-09-24）：预检一并返回占位符聚合，供面板展示
      setPlaceholderReport(data.placeholder_report || null);
      // ✅ 重跑计划：对照当前数据源判定哪些章节可自动重跑（失败静默为无计划）
      try {
        const { data: plan } = await exportApi.placeholderRerunPlan(sid, controller.signal);
        if (
          !controller.signal.aborted &&
          currentSchemeIdRef.current === sid &&
          exportCheckAbortRef.current === controller
        )
          setRerunPlan(plan || null);
      } catch {
        /* 非致命：重跑计划失败不影响预检结论 */
      }
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      if (ps && ps.has_run) {
        if (ps.stale) {
          msg.warning(
            `就绪度总检结论已过期（${ps.total ?? 0} 分 / ${ps.grade} 级），正文变更后建议重新总检`,
          );
        } else if (!ps.released) {
          msg.warning(
            `导出预检之外，就绪度总检也不建议放行：${ps.total ?? 0} 分 / ${ps.grade} 级（${ps.verdict || "未达放行线"}）`,
          );
        }
      }
      const crossrefUndetermined = Boolean(data.crossref_check?.undetermined);
      if (!data.issues?.length) {
        if (crossrefUndetermined) {
          // ✅ B-17：无图表集合时交叉引用「无法判定」，不得提示成「预检通过」。
          msg.info("预检完成：方案无图表，交叉引用无法判定（不代表通过）");
        } else {
          msg.success(`预检通过：${data.section_count} 个章节均无异常`);
        }
      } else {
        msg.warning(`发现 ${data.issues.length} 个问题，建议处理后再导出`);
      }
    } catch (e: unknown) {
      const err = e as {
        name?: string;
        code?: string;
        message?: string;
        response?: { data?: { read_degraded?: boolean } };
      };
      const canceled = err.name === "AbortError" || err.code === "ERR_CANCELED";
      if (!canceled && currentSchemeIdRef.current === sid) {
        msg.error(err.message || "预检失败");
        // ✅ B1-1（R176）：503 + read_degraded=true = 没读到数据，绝不能当「无问题」
        setExportCheckIncomplete(err.response?.data?.read_degraded === true);
      }
    } finally {
      if (exportCheckAbortRef.current === controller) {
        exportCheckAbortRef.current = null;
        setCheckingExport(false);
      }
      // R-3：无论成功/失败/取消都释放同步锁，避免死锁
      checkingExportRef.current = false;
    }
  };

  /**
   * ✅ 接线 sectionsApi.quality：正文质量自检（交付前最后一道关）。
   * 与同页另两项预检互补，三者覆盖三个不同层次：
   *   - 导出预检：结构层 —— 章节完整性 / 字数达标度 / 图表完成率
   *   - 规范符合性检查：内容层 —— 是否覆盖规范条目（AI 逐条判定）
   *   - 质量自检：文字层 —— 口语化/AI 腔残留、引用了已废止或被替代的标准编号
   */
  const handleQualityCheck = async () => {
    // ✅ R168（FIX-10）：重入锁 —— 同帧双击此前可并发两个请求（checkingQuality 是
    //    异步置位，挡不住连点），结果相互覆盖；与同页其它预检入口的 running 态口径一致。
    if (!id || checkingQuality) return;
    setCheckingQuality(true);
    try {
      const { data } = await sectionsApi.quality(id);
      setQualityResult(data);
      const s = data.summary || {};
      if (!s.problem_sections) {
        msg.success(`质量自检通过：已扫描 ${s.checked ?? 0} 个章节，无口语化残留、无废止标准`);
      } else {
        msg.warning(`${s.problem_sections} 个章节存在质量问题，详见清单`);
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "质量自检失败");
    } finally {
      setCheckingQuality(false);
    }
  };

  return { handleExportCheck, handleQualityCheck };
}
