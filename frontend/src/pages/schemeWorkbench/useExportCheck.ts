import { useEffect, useRef } from "react";
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
          // ✅ R183：区分「全书无图表」与「只有表/只有图」两种无法判定原因。
          const _reason = String(data.crossref_check?.reason || "");
          const _why =
            _reason === "no_fig_set"
              ? "方案无图（仅有表），正文「图 X-X」引用"
              : _reason === "no_tbl_set"
                ? "方案无表（仅有图），正文「表 X-X」引用"
                : "方案无图表，交叉引用";
          msg.info(`预检完成：${_why}无法判定（不代表通过）`);
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
  // ✅ R195（FIX-9 · 2026-11-09）：质量自检专用 abort —— 质量自检是后端全量
  //    扫描正文的长请求，此前无 abort、无跨方案守卫：请求在飞时切方案 →
  //    await 返回后旧方案的统计与问题清单 setQualityResult 落到**新方案页**，
  //    toast 归属错乱；卸载后 setState。与 handleExportCheck 的
  //    exportCheckAbortRef 同型（独立 ref，不与预检互相取消）。
  const qualityAbortRef = useRef<AbortController | null>(null);
  const qualityInFlightRef = useRef(false);
  // ✅ R207：切方案时中止本域在飞的质量自检（ref 是 hook 私有，页面清单拿不到，
  //    与 useReviewAudit 的 [id] effect 同型）；卸载兜底缺一即复活。
  useEffect(
    () => () => {
      qualityAbortRef.current?.abort();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  useEffect(() => {
    qualityAbortRef.current?.abort();
    qualityInFlightRef.current = false;
    setCheckingQuality(false);
    setQualityResult(null);
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleQualityCheck = async () => {
    // ✅ R168（FIX-10）+ R207：同步 ref 重入锁 —— checkingQuality 是异步 state，
    //    同帧双击挡不住；并补上跨方案切换后的锁复位。
    if (!id || checkingQuality || qualityInFlightRef.current) return;
    const sid = id;
    qualityInFlightRef.current = true;
    const controller = new AbortController();
    qualityAbortRef.current?.abort();
    qualityAbortRef.current = controller;
    setCheckingQuality(true);
    try {
      const { data } = await sectionsApi.quality(sid, { signal: controller.signal });
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      setQualityResult(data);
      const s = data.summary || {};
      // ✅ R183（2026-11-07 · 三态口径修复）：checked=0 表示**没有可检查的正文**
      //    （方案尚未生成 / 全部章节为空），旧实现照样提示「质量自检通过：已扫描
      //    0 个章节」——把「没检查」显示成「没问题」。现单独给出「无法判定」。
      // ✅ R184：分母/未覆盖数直接用后端 section_total/unchecked；仍有章节没正文
      //    参与检查时同样判「无法判定」，不得提示通过。
      const qChecked = s.checked ?? 0;
      const qUnchecked = s.unchecked ?? 0;
      const qTotalSuffix = s.section_total != null ? ` / 共 ${s.section_total} 章` : "";
      if (qChecked === 0) {
        msg.info(`质量自检未执行：没有可检查的正文（已扫描 0${qTotalSuffix}，无法判定，不代表通过）`);
      } else if (!s.problem_sections && qUnchecked > 0) {
        msg.warning(
          `已扫描 ${qChecked}${qTotalSuffix}，仍有 ${qUnchecked} 章没有正文、未纳入检查（无法判定，不代表通过）`,
        );
      } else if (!s.problem_sections) {
        msg.success(`质量自检通过：已扫描 ${qChecked}${qTotalSuffix}，无口语化残留、无废止标准`);
      } else {
        msg.warning(`${s.problem_sections} 个章节存在质量问题，详见清单`);
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      // ✅ R195：中止/跨方案到达的错误不弹（旧请求的失败不属于当前方案）。
      if (controller.signal.aborted || currentSchemeIdRef.current !== sid) return;
      msg.error(err.message || "质量自检失败");
    } finally {
      // ✅ R207：旧请求完成不得关掉新方案/新请求的 spinner（与 R197 F3
      //    「finally 复位收敛进 ref 门禁」同口径）。
      if (qualityAbortRef.current === controller && currentSchemeIdRef.current === sid) {
        qualityAbortRef.current = null;
        setCheckingQuality(false);
      }
      if (qualityAbortRef.current === controller) qualityInFlightRef.current = false;
      else if (currentSchemeIdRef.current === sid) qualityInFlightRef.current = false;
    }
  };

  return { handleExportCheck, handleQualityCheck };
}
