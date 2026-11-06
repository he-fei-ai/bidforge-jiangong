import { useRef, useState } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import { complianceApi, consistencyRepairApi } from "../../api";
import type { AuditRule } from "../../types/audit";

// =========================================================================
// ✅ any 清理专项（2026-11-02）：本域消费的后端 REST 响应契约。
//    字段按 hook 内实际读取 + 页面（SchemeWorkbenchPage）实际渲染字段推导，
//    未知字段一律 ? 修饰，不虚构。
// =========================================================================

/** 一致性修复确认结果（POST /consistency/confirm；R179 消费恢复三态字段） */
export interface ConsistencyConfirmResult {
  repair_id?: string;
  status?: string;
  accepted?: string[];
  rejected?: string[];
  restored_sections?: string[];
  /** R179 加法式：快照缺失、正文未真正回滚的章节 */
  restore_skipped_sections?: string[];
  rescan_required?: boolean;
}

/** 合规检查单条结果（POST /compliance/check 响应 results[]；本域读 hit/severity） */
export interface ComplianceResultItem {
  rule_id?: string;
  hit?: boolean;
  severity?: string;
}

/** 一致性审计单条问题（POST /compliance/consistency-audit/{id} 响应 issues[]；
 *  页面审核 Tab 表格按 dataIndex 读 severity/dimension/section_title/fact/content_quote/suggestion） */
export interface ConsistencyIssueItem {
  severity?: string;
  dimension?: string;
  section_title?: string;
  fact?: string;
  content_quote?: string;
  suggestion?: string;
}

/** 一致性审计结果（本 hook consistencyResult state；页面读 score/issues/created_at） */
export interface ConsistencyAuditResult {
  score: number;
  issues: ConsistencyIssueItem[];
  created_at?: string;
  // ✅ R181（遗留问题 6）：后端加法式回传评分口径，前端原样展示，
  //    避免用户误以为「分数比单批时低」是正文变差。
  score_rule?: string;
  score_note?: string;
}

/** 专家论证预检结果（POST /compliance/expert-review 响应；页面读 ready/missing/score/suggestions） */
export interface ExpertReviewResult {
  ready?: string[];
  missing?: string[];
  suggestions?: string[];
  score?: number;
}

/** 一致性冲突单条 occurrence（GET /consistency/conflicts 响应 conflicts[].occurrences[]） */
export interface CrConflictOccurrence {
  section_title?: string;
  section_id?: string;
  value?: string;
  text?: string;
}

/** 一致性冲突单条（GET /consistency/conflicts 响应 conflicts[]；修复工作台表格/展开行消费） */
export interface CrConflictItem {
  id?: string;
  conflict_id?: string;
  conflict_type?: string;
  severity?: string;
  topic?: string;
  status?: string;
  authoritative_value?: string;
  repair_instruction?: string;
  reason?: string;
  occurrences?: CrConflictOccurrence[];
}

/** 一致性冲突概要（conflicts/scan 响应 summary；页面读 total/high/medium/low/pending/skipped） */
export interface CrSummary {
  total?: number;
  high?: number;
  medium?: number;
  low?: number;
  pending?: number;
  skipped?: number;
}

/** 一致性修复结果单条（POST /consistency/repair 响应 items[]；逐条确认 Collapse 消费） */
export interface CrRepairItem {
  conflict_id?: string;
  section_id?: string;
  section_title?: string;
  topic?: string;
  status?: string;
  before?: string;
  after?: string;
  problems?: string[];
}

/** 一致性修复批次结果（POST /consistency/repair、confirm 响应；crRepair state） */
export interface CrRepairResult {
  repair_id?: string;
  status?: string;
  scan_id?: string;
  total?: number;
  repaired?: number;
  failed?: number;
  skipped?: number;
  items?: CrRepairItem[];
}

/** 修复历史单条（GET /consistency/repairs 响应 items[]；回滚读 id/snapshot_id） */
export interface CrRepairHistoryItem {
  id?: string;
  snapshot_id?: string;
  status?: string;
}

/** 当前选中章节的最小形状（本域读 key，回写 content/status；TreeNode 结构兼容） */
export interface SelectedSectionLike {
  key?: string;
  content?: string;
  status?: string;
}

/**
 * R-P1 拆分（纯结构重构）：审核与预检域处理器，自 SchemeWorkbenchPage 原位搬迁：
 *   · 专家论证预检（R106 FIX-5 取消链路）
 *   · 规范符合性检查（complianceApi.check）
 *   · 全文一致性审计（§3.12.1）—— auditRunning / consistencyResult 两个 useState
 *     随域迁入本 hook，调用点保持在页面原位置 ⇒ effect/渲染顺序不变
 *   · 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR，CR 域全量处理器）
 */
export interface ReviewAuditDeps {
  id: string | undefined;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  modal: { confirm: (cfg: ModalFuncProps) => void };
  // 专家论证
  expertAbortRef: MutableRefObject<AbortController | null>;
  expertRunning: boolean;
  setExpertRunning: (v: boolean) => void;
  setExpertResult: (v: ExpertReviewResult | null) => void;
  // 合规检查
  checkAbortRef: MutableRefObject<AbortController | null>;
  setCheckingCompliance: (v: boolean) => void;
  checklistText: string;
  checklistDirty: boolean;
  aiRules: AuditRule[];
  setComplianceResults: (v: ComplianceResultItem[]) => void;
  loadComplianceHistory: () => void | Promise<void>;
  // 一致性审计（abort ref 在页面，状态在本 hook）
  auditAbortRef: MutableRefObject<AbortController | null>;
  // CR 修复工作台
  consistencySeverity: string;
  forceFullRepair: boolean;
  crScanning: boolean;
  setCrScanning: (v: boolean) => void;
  crRepairing: boolean;
  setCrRepairing: (v: boolean) => void;
  crScanId: string;
  setCrRepair: Dispatch<SetStateAction<CrRepairResult | null>>;
  crRepair: CrRepairResult | null;
  setCrConflicts: (v: CrConflictItem[]) => void;
  setCrSummary: (v: CrSummary | null) => void;
  setCrScanId: (v: string) => void;
  setCrHistory: (v: CrRepairHistoryItem[]) => void;
  setCrOpen: (v: boolean) => void;
  setCrLoading: (v: boolean) => void;
  selectedSectionRef: MutableRefObject<SelectedSectionLike | null>;
  setSelectedSection: Dispatch<SetStateAction<SelectedSectionLike | null>>;
  load: () => void | Promise<void>;
  setReviewTick: Dispatch<SetStateAction<number>>;
}

export function useReviewAudit(deps: ReviewAuditDeps) {
  const {
    id,
    msg,
    modal,
    expertAbortRef,
    expertRunning,
    setExpertRunning,
    setExpertResult,
    checkAbortRef,
    setCheckingCompliance,
    checklistText,
    checklistDirty,
    aiRules,
    setComplianceResults,
    loadComplianceHistory,
    auditAbortRef,
    consistencySeverity,
    forceFullRepair,
    crScanning,
    setCrScanning,
    crRepairing,
    setCrRepairing,
    crScanId,
    setCrRepair,
    crRepair,
    setCrConflicts,
    setCrSummary,
    setCrScanId,
    setCrHistory,
    setCrOpen,
    setCrLoading,
    selectedSectionRef,
    setSelectedSection,
    load,
    setReviewTick,
  } = deps;

  // ===== ✅ 全文一致性审计（§3.12.1）：事实 vs 正文比对，0-100 评分 + 不一致项 =====
  // R-P1 拆分：两个状态随域迁入本 hook（页面原位声明 → hook 原位调用，行为不变）
  const [auditRunning, setAuditRunning] = useState(false);
  const [consistencyResult, setConsistencyResult] = useState<ConsistencyAuditResult | null>(null);

  // ✅ R168（FIX-10）：CR 扫描/修复是 10 分钟级 AI 长任务，此前与同域三个预检端点
  //    （R106 FIX-5）不同，没有任何取消入口。AbortController 由本 hook 内部持有
  //    （仅 hook 消费，不必经 deps 从页面下发），取消按钮经返回的 handler 触发。
  const crScanAbortRef = useRef<AbortController | null>(null);
  const crRepairAbortRef = useRef<AbortController | null>(null);

  const handleExpertReview = async () => {
    if (!id || expertRunning) return;
    expertAbortRef.current?.abort();
    const controller = new AbortController();
    expertAbortRef.current = controller;
    setExpertRunning(true);
    try {
      const { data } = await complianceApi.expertReview({ scheme_id: id }, controller.signal);
      if (controller.signal.aborted) return;
      setExpertResult(data);
      msg.success("预检完成");
    } catch (e: unknown) {
      // 结构化窄断言：仅读取 axios 错误对象上真实存在的 name / code / message
      const err = e as { name?: string; code?: string; message?: string };
      if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") msg.info("已取消预检");
      else msg.error(err.message || "预检失败");
    } finally {
      if (expertAbortRef.current === controller) {
        expertAbortRef.current = null;
        setExpertRunning(false);
      }
    }
  };

  /** ✅ R106 FIX-5：取消专家论证预检 */
  const handleCancelExpertReview = () => {
    expertAbortRef.current?.abort();
  };

  // ================== ✅ 规范符合性检查（complianceApi.check）==================
  /** 把多行文本切成检查清单并提交 AI 逐条判定 */
  const handleComplianceCheck = async () => {
    if (!id) return;
    const checklist = checklistText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (checklist.length === 0) {
      msg.warning("请至少输入一条检查项");
      return;
    }
    // ✅ R106 FIX-5：接 AbortController，运行中可取消（旧实现只能等 AI 跑完）
    checkAbortRef.current?.abort();
    const controller = new AbortController();
    checkAbortRef.current = controller;
    setCheckingCompliance(true);
    try {
      // ✅ 清单未被手工改动时携带 rule_ids 提交 —— 让落库的 rule_id 指向规则注册表，
      //    历史结果才能跨版本比对并归并进六维评分（否则 AI 自造的 "R1" 无语义）
      const useRules = !checklistDirty && aiRules.length > 0 && checklist.length === aiRules.length;
      const payload = useRules
        ? { scheme_id: id, rule_ids: aiRules.map((r) => r.rule_id) }
        : { scheme_id: id, checklist };
      const { data } = await complianceApi.check(payload, controller.signal);
      if (controller.signal.aborted) return;
      const results = data.results || [];
      setComplianceResults(results);
      // ✅ D-41（R176 · 2026-11-04）：合规检查截断此后端已标记（`partial` +
      //    `truncated_sections`），但前端全仓零消费 —— 用户看到「检查完成」，
      //    实际后几章根本没送审（静默失真）。现显式告警并写入消息中心
      //    （历史可查、不会因气泡消失而丢失）。
      if (data.partial === true) {
        const truncated: { section_title?: string }[] = Array.isArray(data.truncated_sections)
          ? data.truncated_sections
          : [];
        const titles = truncated
          .slice(0, 5)
          .map((s) => s.section_title || "未命名章节")
          .join("、");
        const checked = Number(data.checked_chars || 0);
        const total = Number(data.total_chars || 0);
        msg.warning(
          `合规检查覆盖不完整：仅检查全文前 ${checked}/${total} 字，` +
            `${truncated.length} 个章节未完整送审（${titles}` +
            `${truncated.length > 5 ? " 等" : ""}）。结果不代表全书已合规。`,
        );
      }
      const miss = results.filter((r: ComplianceResultItem) => r.hit === false).length;
      const high = results.filter((r: ComplianceResultItem) => r.severity === "high").length;
      msg.success(`检查完成：${results.length} 项，疑似缺失 ${miss} 项，高风险 ${high} 项`);
      // 结果已落库，刷新历史记录
      loadComplianceHistory();
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") msg.info("已取消合规检查");
      else msg.error(err.message || "合规检查失败（请确认已配置可用的 AI 供应商）");
    } finally {
      if (checkAbortRef.current === controller) checkAbortRef.current = null;
      setCheckingCompliance(false);
    }
  };

  /** ✅ R106 FIX-5：取消合规检查（abort → 服务端断连即在下一 await 边界取消 AI 调用） */
  const handleCancelComplianceCheck = () => {
    checkAbortRef.current?.abort();
  };

  const handleConsistencyAudit = async () => {
    if (!id || auditRunning) return;
    // ✅ R106 FIX-5：审计是最长的同步 AI 调用（长方案 1-3 分钟），此前无任何取消入口
    auditAbortRef.current?.abort();
    const controller = new AbortController();
    auditAbortRef.current = controller;
    setAuditRunning(true);
    setConsistencyResult(null);
    try {
      const { data } = await complianceApi.consistencyAudit(id, controller.signal);
      if (controller.signal.aborted) return;
      setConsistencyResult({
        score: data.score,
        issues: data.issues || [],
        score_rule: data.score_rule,
        score_note: data.score_note,
      });
      const high = (data.issues || []).filter(
        (i: ConsistencyIssueItem) => i.severity === "high",
      ).length;
      msg.success(
        `一致性审计完成：评分 ${data.score}，不一致项 ${data.issues?.length || 0}（高危 ${high}）`,
      );
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") msg.info("已取消一致性审计");
      else msg.error(err.message || "一致性审计失败");
    } finally {
      if (auditAbortRef.current === controller) auditAbortRef.current = null;
      setAuditRunning(false);
    }
  };

  /** ✅ R106 FIX-5：取消一致性审计 */
  const handleCancelConsistencyAudit = () => {
    auditAbortRef.current?.abort();
  };

  // ===== ✅ 全文一致性 Agent 修复工作台（F-AGENT-CONSISTENCY-REPAIR）=====
  const loadCrConflicts = async (scanId?: string) => {
    if (!id) return;
    try {
      const { data } = await consistencyRepairApi.conflicts(id, scanId);
      if (data?.exists) {
        setCrConflicts(data.conflicts || []);
        setCrSummary(data.summary || null);
        setCrScanId(data.scan_id || scanId || "");
      } else {
        setCrConflicts([]);
        setCrSummary(null);
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.warning(err.message || "加载冲突清单失败");
    }
  };

  const loadCrHistory = async () => {
    if (!id) return;
    try {
      const { data } = await consistencyRepairApi.repairs(id, 10);
      setCrHistory(data.items || []);
    } catch {
      // 历史修复记录加载失败不阻塞工作台
    }
  };

  const openConsistencyWorkbench = () => {
    setCrOpen(true);
    setCrLoading(true);
    Promise.all([loadCrConflicts(), loadCrHistory()]).finally(() => setCrLoading(false));
  };

  const handleCrScan = async () => {
    if (!id || crScanning) return;
    // ✅ R168（FIX-10）：接 AbortController，运行中可取消（口径与 R106 FIX-5 一致）
    crScanAbortRef.current?.abort();
    const controller = new AbortController();
    crScanAbortRef.current = controller;
    setCrScanning(true);
    setCrRepair(null);
    try {
      const { data } = await consistencyRepairApi.scan(
        id,
        {
          scope: "full",
          include_global_facts: true,
          include_project_docs: true,
          include_design_docs: true,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setCrConflicts(data.conflicts || []);
      setCrSummary(data.summary || null);
      setCrScanId(data.scan_id || "");
      const s: CrSummary = data.summary || {};
      msg.success(
        `扫描完成：发现 ${s.total ?? 0} 处冲突（高 ${s.high ?? 0} / 中 ${s.medium ?? 0} / 低 ${s.low ?? 0}）`,
      );
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") msg.info("已取消一致性扫描");
      else msg.error(err.message || "全文一致性扫描失败");
    } finally {
      if (crScanAbortRef.current === controller) crScanAbortRef.current = null;
      setCrScanning(false);
    }
  };

  /** ✅ R168（FIX-10）：取消一致性扫描 */
  const handleCancelCrScan = () => {
    crScanAbortRef.current?.abort();
  };

  const handleCrRepair = async (opts?: { conflictIds?: string[]; severity?: string }) => {
    if (!id || crRepairing) return;
    // ✅ R168（FIX-10）：接 AbortController，运行中可取消（口径与 R106 FIX-5 一致）
    crRepairAbortRef.current?.abort();
    const controller = new AbortController();
    crRepairAbortRef.current = controller;
    setCrRepairing(true);
    try {
      const { data } = await consistencyRepairApi.repair(
        id,
        {
          scan_id: crScanId || undefined,
          mode: "auto",
          severity_threshold: opts?.severity || consistencySeverity,
          conflict_ids: opts?.conflictIds,
          // ✅ 强制全量重修：true 时不跳过「已修复且修复成果仍在正文里」的冲突
          force_full_repair: forceFullRepair,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setCrRepair(data);
      await loadCrConflicts();
      await loadCrHistory();
      // 若当前选中章节被修复，同步预览正文
      const curKey = selectedSectionRef.current?.key;
      if (curKey) {
        const hit = (data.items || []).find(
          (x: CrRepairItem) => x.section_id === curKey && x.status === "repaired" && x.after,
        );
        if (hit?.after) {
          // ✅ F2（R71）：不再用 hit.after.length 伪造字符口径当「字」；
          //    保留章节原 word_count，下方 load() 随即回拉后端权威字数（一帧自纠）。
          setSelectedSection((prev) =>
            prev && prev.key === curKey
              ? { ...prev, content: hit.after, status: "generated" }
              : prev,
          );
        }
      }
      load();
      msg.success(
        `修复完成：成功 ${data.repaired ?? 0} 处，失败 ${data.failed ?? 0} 处，跳过 ${data.skipped ?? 0} 处`,
      );
      // ✅ G11（2026-09-21）：修复改写了正文 → 审核结论与总检结论都已失效。
      // 旧实现只报"修复完成"，不提示重跑总检：用户以为分数没变即可交付，
      // 实际按旧结论放行会把新写进去的问题一起带出。
      if ((data.repaired ?? 0) > 0) {
        setReviewTick((t) => t + 1);
        msg.warning("正文已被修复改写，审核结论已失效；请重新送审并重新执行一键总检");
      }
    } catch (e: unknown) {
      const err = e as { name?: string; code?: string; message?: string };
      if (err?.name === "AbortError" || err?.code === "ERR_CANCELED") msg.info("已取消一致性修复");
      else msg.error(err.message || "全文一致性修复失败");
    } finally {
      if (crRepairAbortRef.current === controller) crRepairAbortRef.current = null;
      setCrRepairing(false);
    }
  };

  /** ✅ R168（FIX-10）：取消一致性修复（abort → 服务端断连即在下一 await 边界取消） */
  const handleCancelCrRepair = () => {
    crRepairAbortRef.current?.abort();
  };

  const crRepairItemIds = (status: string) =>
    (crRepair?.items || [])
      .filter((i) => i.status === status && i.conflict_id)
      .map((i) => i.conflict_id as string);

  const handleCrConfirm = async (accepted: string[], rejected: string[]) => {
    if (!id || !crRepair?.repair_id) return;
    try {
      const { data } = await consistencyRepairApi.confirm(id, {
        repair_id: crRepair.repair_id,
        accepted,
        rejected,
      });
      const result = data as ConsistencyConfirmResult;
      // ✅ R179（E4/E13）：以后端实际受理结果为准展示，而不是回显请求条数 ——
      //    快照缺失时正文并没有回滚，旧文案「已恢复原文」会误导用户放心导出。
      const acceptedCount = (result.accepted ?? accepted).length;
      const rejectedCount = (result.rejected ?? rejected).length;
      const restored = result.restored_sections ?? [];
      const restoreSkipped = result.restore_skipped_sections ?? [];
      msg.success(`已确认：接受 ${acceptedCount} 条，拒绝 ${rejectedCount} 条`);
      await loadCrConflicts();
      await loadCrHistory();
      load();
      setCrRepair((prev) => (prev ? { ...prev, status: result.status ?? prev.status } : prev));
      if (restoreSkipped.length > 0) {
        msg.warning(
          `${restoreSkipped.length} 章因修复快照缺失未回滚正文（仅回写冲突状态），请人工核对后再导出`,
        );
      }
      // ✅ G11：拒绝回滚会把章节正文恢复为修复前版本 → 审核结论失效；
      //    R179 起仅在**真的发生了正文回滚**时才宣称「已恢复原文」。
      if (acceptedCount + rejectedCount > 0) {
        setReviewTick((t) => t + 1);
        if (restored.length > 0) {
          msg.warning("已拒绝部分修复并恢复原文，相关章节审核结论已失效，需重新送审并重新总检");
        } else if (result.rescan_required) {
          msg.warning("一致性结论需重新扫描后再判定");
        }
      }
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err.message || "确认修复结果失败");
    }
  };

  const handleCrRollback = (repair: CrRepairHistoryItem) => {
    if (!id) return;
    modal.confirm({
      title: "回滚本次修复？",
      content: `将把本次修复涉及的章节恢复为修复前正文（快照 ${repair.snapshot_id || "—"}）。该操作会生成一个可再次撤销的快照。`,
      okText: "确认回滚",
      cancelText: "取消",
      onOk: async () => {
        try {
          await consistencyRepairApi.rollback(id, { repair_id: repair.id });
          msg.success("已回滚到修复前版本");
          // ✅ G11：回滚改写了正文 → 审核结论失效，需要重新送审 + 重跑总检
          setReviewTick((t) => t + 1);
          msg.warning("正文已回滚到修复前版本，相关章节审核结论已失效，需重新送审并重新总检");
          await loadCrConflicts();
          await loadCrHistory();
          load();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err.message || "回滚失败");
        }
      },
    });
  };

  return {
    handleExpertReview,
    handleCancelExpertReview,
    handleComplianceCheck,
    handleCancelComplianceCheck,
    auditRunning,
    consistencyResult,
    setConsistencyResult,
    handleConsistencyAudit,
    handleCancelConsistencyAudit,
    loadCrConflicts,
    loadCrHistory,
    openConsistencyWorkbench,
    handleCrScan,
    handleCancelCrScan,
    handleCrRepair,
    handleCancelCrRepair,
    crRepairItemIds,
    handleCrConfirm,
    handleCrRollback,
  };
}
