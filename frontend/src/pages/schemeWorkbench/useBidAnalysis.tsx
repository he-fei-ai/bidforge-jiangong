import { useCallback } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { tasksApi, bidAnalysisApi, sseGetStream } from "../../api";
import { sourceNotice } from "../../utils/parseSourceNotice";
import { isTerminalTaskStatus } from "../../utils/contentEvents";
import {
  recomputeBaSummary,
  type BaItemDef,
  type BaStoredItem,
  type BaSummary,
  type BaTextStats,
} from "../../utils/bidAnalysis";
import type { WorkflowTabKey } from "../../utils/workflowDerived";
import { pollTaskUntilTerminalImpl, type TaskTerminalInfo } from "./taskPolling.ts";

/** 人工校正弹窗条目（页面 BaEditItemLike 的同构最小集；字段全可选兼容任意来源条目） */
export interface BaEditItemLike {
  item_id?: string;
  label?: string;
  output_type?: string;
  content?: string;
  [key: string]: unknown;
}

/** 后端 completed 事件 result 载荷（汇总兜底 + 截断报告；字段缺省可选） */
type BaSseCompletedResult = {
  ok?: boolean;
  completed?: number;
  total?: number;
  missing_required?: string[];
  source_truncated?: boolean;
  truncated_docs?: Array<{ name?: string; category?: string; chars?: number }>;
  used_doc_count?: number;
  input_doc_count?: number;
  dropped_doc_count?: number;
};

/** 后端 SSE 事件形态（backend routers/bid_analysis.py，按 type 判别；字段按本 hook 消费值推导） */
type BaSseEvent =
  | {
      type: "item_update";
      item_id: string;
      status?: string;
      content?: string;
      error?: string;
      progress?: number;
    }
  | { type: "heartbeat"; progress?: number; message?: string }
  | {
      type: "text_stats";
      total_chars?: number;
      segment_count?: number;
      item_count?: number;
      est_model_calls?: number;
      chunk_size?: number;
      source_truncated?: boolean;
      truncated_docs?: Array<{ name?: string; category?: string; chars?: number }>;
      used_doc_count?: number;
      input_doc_count?: number;
      dropped_doc_count?: number;
    }
  | { type: "completed"; result?: BaSseCompletedResult }
  | { type: "error"; error?: string }
  | { type: "cancelled"; message?: string };

/**
 * R-P1 拆分（纯结构重构）：「提取项目」(bidAnalysis) 域处理器，自
 * SchemeWorkbenchPage 原位搬迁。SSE 流 / 停止轮询 / 多标段检测 / 人工校正闭环 /
 * 配置启动全部保持 useCallback 形态与依赖数组原样，仅搬迁宿主。
 */
export interface BidAnalysisDeps {
  id: string | undefined;
  schemeProjectId: string | undefined;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  baSseAbortRef: MutableRefObject<AbortController | null>;
  setBaRunning: (v: boolean) => void;
  setBaProgress: (v: number) => void;
  setBaProgressMsg: (v: string) => void;
  setBaTextStats: (v: BaTextStats | null) => void;
  setBaItems: Dispatch<SetStateAction<BaStoredItem[]>>;
  baItems: BaStoredItem[];
  baDefsRef: MutableRefObject<BaItemDef[]>;
  setBaSummary: (v: BaSummary | null) => void;
  setSelectedBaItem: Dispatch<SetStateAction<BaStoredItem | null>>;
  loadBaResults: () => void | Promise<void>;
  autoJumpFrom: (origin: WorkflowTabKey, target: WorkflowTabKey) => void;
  baStoppingRef: MutableRefObject<boolean>;
  setBaStopping: (v: boolean) => void;
  sectionChecking: boolean;
  setSectionChecking: (v: boolean) => void;
  setSectionCheckResult: (v: Record<string, unknown> | null) => void;
  baEditItem: BaEditItemLike | null;
  setBaEditItem: (v: BaEditItemLike | null) => void;
  baEditValue: string;
  setBaEditValue: (v: string) => void;
  setBaEditSaving: (v: boolean) => void;
  setBaConfigOpen: (v: boolean) => void;
  baModeOpt: "key" | "full" | "custom";
  baSelectedIds: string[];
  baForceRerun: boolean;
}

export function useBidAnalysis(deps: BidAnalysisDeps) {
  const {
    id,
    schemeProjectId,
    msg,
    baSseAbortRef,
    setBaRunning,
    setBaProgress,
    setBaProgressMsg,
    setBaTextStats,
    setBaItems,
    baItems,
    baDefsRef,
    setBaSummary,
    setSelectedBaItem,
    loadBaResults,
    autoJumpFrom,
    baStoppingRef,
    setBaStopping,
    sectionChecking,
    setSectionChecking,
    setSectionCheckResult,
    baEditItem,
    setBaEditItem,
    baEditValue,
    setBaEditValue,
    setBaEditSaving,
    setBaConfigOpen,
    baModeOpt,
    baSelectedIds,
    baForceRerun,
  } = deps;

  /**
   * 启动/单项重跑结构化提取（SSE）。
   * 事件契约（backend routers/bid_analysis.py）：
   *   item_update {item_id,status,progress,content?,error?} / heartbeat / completed / error / cancelled
   */
  const runBaSse = useCallback(
    async (opts: {
      mode: "key" | "full" | "custom" | "item";
      selectedItemIds?: string[];
      forceRerun?: boolean;
    }) => {
      if (!id || baSseAbortRef.current) return;
      const ac = new AbortController();
      baSseAbortRef.current = ac;
      setBaRunning(true);
      setBaProgress(0);
      setBaProgressMsg("正在连接 AI...");
      // 清空上一轮提取规模（避免「本轮尚未切段却显示上轮调用数」的误导）
      setBaTextStats(null);
      try {
        const { path, params } = bidAnalysisApi.startSse(id, schemeProjectId, opts);
        for await (const raw of sseGetStream(path, { params, signal: ac.signal })) {
          if (ac.signal.aborted) break;
          // 后端 SSE 契约（backend routers/bid_analysis.py）：循环体内一次性断言到已知事件形态
          const evt = raw as BaSseEvent;
          const t = evt?.type;
          if (t === "item_update") {
            setBaItems((prev) => {
              const idx = prev.findIndex((i) => i.item_id === evt.item_id);
              const patch: BaStoredItem = { item_id: evt.item_id, status: evt.status };
              if (evt.content) patch.content = evt.content;
              if (evt.error) patch.error = evt.error;
              if (idx >= 0) {
                const cp = [...prev];
                cp[idx] = { ...cp[idx], ...patch };
                return cp;
              }
              return [...prev, patch];
            });
            if (typeof evt.progress === "number") setBaProgress(evt.progress);
          } else if (t === "heartbeat") {
            if (typeof evt.progress === "number") setBaProgress(evt.progress);
            if (evt.message) setBaProgressMsg(evt.message);
          } else if (t === "text_stats") {
            // 提取规模（切段后立即推送）：超长文档的真实消耗对用户必须可见
            setBaTextStats({
              total_chars: evt.total_chars ?? 0,
              segment_count: evt.segment_count ?? 0,
              item_count: evt.item_count ?? 0,
              est_model_calls: evt.est_model_calls ?? 0,
              chunk_size: evt.chunk_size ?? 0,
              // 提取依据完整性：任一文档被截断/预算不足被跳过时，
              // 「提取结果不可信」必须在提取结果区显性提示，而不是只藏在后端日志里
              source_truncated: !!evt.source_truncated,
              truncated_docs: evt.truncated_docs ?? [],
              used_doc_count: evt.used_doc_count ?? 0,
              input_doc_count: evt.input_doc_count ?? 0,
              dropped_doc_count: evt.dropped_doc_count ?? 0,
            });
          } else if (t === "completed") {
            // 兜底前先断言到含可选字段的 result 契约，{} 兜底与取属性逻辑保持原样
            const r: BaSseCompletedResult = evt.result || {};
            // 后端把截断报告一并放进 completed 的 result，作为 SSE 中断时的兜底
            if (r.source_truncated) {
              const n = sourceNotice(r);
              if (n) msg.warning(n.title + (n.detail ? `（${n.detail}）` : ""));
            }
            if (r.ok) {
              msg.success(`结构化提取完成（${r.completed || 0}/${r.total || 0} 项）`);
              // ✅ 自动跳目录生成收紧为「key/full 且必选项齐备」（停止/报错/单项重跑不跳）
              if (opts.mode === "key" || opts.mode === "full")
                autoJumpFrom("import" as WorkflowTabKey, "outline");
            } else {
              const missing = (r.missing_required || []).join("、");
              // 「缺失」有两种处置：AI 调用失败→等配额/网络恢复后单项重试；
              // 资料确实缺失→补料后单项重试。因此提示必须指向「单项重新提取」，
              // 而不是笼统的「提取完成但未全部完成」。
              msg.warning(
                `提取完成，但必选项未全部完成${missing ? `：${missing}` : ""}（可对缺项单项重新提取）`,
              );
            }
          } else if (t === "error") {
            msg.error(evt.error || "结构化提取失败");
          } else if (t === "cancelled") {
            msg.info(evt.message || "任务已取消");
          }
        }
      } catch (e) {
        const err = e as { name?: string; message?: string };
        if (err?.name !== "AbortError" && !ac.signal.aborted) {
          msg.error(err.message || "结构化提取连接中断");
        }
      } finally {
        if (baSseAbortRef.current === ac) baSseAbortRef.current = null;
        setBaRunning(false);
        // 收尾以服务端权威结果为准（item_update 只是乐观视图）
        loadBaResults();
      }
    },
    [id, schemeProjectId, loadBaResults, msg],
  );

  /** 停止：四类任务共用 task_registry 控制链路（POST /sse/task/{id}/control） */
  const handleStopBa = useCallback(async () => {
    if (!id) return;
    // ✅ R106 FIX-4：同步锁 —— 旧实现可连点，且发完指令就乐观提示，用户不知道
    //    任务是否真的退出了。现置灰按钮并轮询到终态才报「已停止」。
    if (baStoppingRef.current) return;
    baStoppingRef.current = true;
    setBaStopping(true);
    try {
      const { data } = await tasksApi.list(id, 10);
      const t = (data.tasks || []).find(
        // 后端 /sse/tasks 任务项（api 层未泛型化，data 为 any）：按本处读取字段标注
        (x: { id?: string; task_type?: string; status?: string }) =>
          x.task_type === "bid_analysis" && (x.status === "running" || x.status === "paused"),
      );
      if (!t) {
        msg.info("没有正在运行的结构化提取任务");
        // ✅ 列表可能滞后（任务刚结束）：顺带刷新结果，避免按钮一直显示可点
        loadBaResults();
        return;
      }
      const { data: ctl } = await tasksApi.control(t.id, "stop");
      if (ctl?.ok === false) {
        msg.error(ctl.message || "停止指令未被接受，任务可能已结束");
        loadBaResults();
        return;
      }
      if (ctl?.orphaned) {
        msg.info(ctl.message || "任务已无运行实例，已标记为已停止");
        loadBaResults();
        return;
      }
      msg.info("停止指令已发送，正在等待在途解析项收口...");
      // ✅ 轮询至终态（3s × 20 ≈ 1 分钟上限）：协作式停止 = 在途项跑完、未启动项不再启动
      const fin = await pollTaskUntilTerminalImpl(
        t.id,
        async (tid) => (await tasksApi.status(tid)).data as TaskTerminalInfo,
        undefined,
        { intervalMs: 3000, maxPolls: 20, netFailTolerance: 3 },
      );
      if (fin && isTerminalTaskStatus(fin.status)) msg.success("已停止");
      else msg.warning("停止指令已发送，任务未在预期内退出，可在任务栏查看");
      loadBaResults();
    } catch (e) {
      const err = e as { message?: string };
      msg.error(err.message || "停止失败");
    } finally {
      baStoppingRef.current = false;
      setBaStopping(false);
    }
  }, [id, msg, loadBaResults]);

  /** 多标段检测（规则检测，非 AI，秒级） */
  const handleCheckSections = useCallback(async () => {
    if (!id || sectionChecking) return;
    setSectionChecking(true);
    try {
      const { data } = await bidAnalysisApi.checkSections(id, schemeProjectId);
      setSectionCheckResult(data);
    } catch (e) {
      const err = e as { message?: string };
      msg.error(err.message || "多标段检测失败");
    } finally {
      setSectionChecking(false);
    }
  }, [id, schemeProjectId, sectionChecking, msg]);

  /** 单项重跑：mode=item 严格按勾选、不补全必选项、forceRerun=false（直接覆盖该项） */
  const handleRerunItem = useCallback(
    (def: BaItemDef) => {
      runBaSse({ mode: "item", selectedItemIds: [def.item_id], forceRerun: false });
    },
    [runBaSse],
  );

  // ===== ✅ 人工校正闭环（2026-09-23 复原，供「提取项目」与「解析信息分类显示栏」共用）=====
  /** 打开校正弹窗（载入当前项内容，json/markdown 均可编辑） */
  const openBaEdit = useCallback((item: BaEditItemLike) => {
    if (!item) return;
    setBaEditItem(item);
    setBaEditValue(item?.content || "");
  }, []);

  /** 保存校正（PUT /results/{item_id}，后端返回权威 item + summary）
   *  AI 抽错关键参数时唯一可靠出口；校正值标记 source='manual'，下游优先使用。 */
  const handleBaSaveEdit = useCallback(async () => {
    const item = baEditItem;
    if (!item || !id) return;
    const content = (baEditValue ?? "").trim();
    if (!content) {
      msg.warning("内容不能为空（如需清空请点「撤销校正」）");
      return;
    }
    // ✅ 前端预校验 json 项（与后端 422 对齐，避免一次往返）
    if (item.output_type === "json") {
      try {
        const parsed = JSON.parse(content);
        if (typeof parsed === "string") {
          msg.error("项目级基本信息为 JSON 对象/数组，不能是裸字符串");
          return;
        }
      } catch {
        msg.error("项目级基本信息为 JSON 结构，请提交合法 JSON");
        return;
      }
    }
    setBaEditSaving(true);
    try {
      const { data } = await bidAnalysisApi.updateResult(
        item.item_id!,
        id,
        content,
        schemeProjectId,
      );
      msg.success("已保存人工校正结果（优先于 AI 输出，下游目录/正文将使用该校正值）");
      // 以后端权威返回更新（item + summary），避免乐观更新与 DB 漂移
      if (data?.item) {
        const updated = { ...item, ...data.item };
        setBaItems((prev: BaStoredItem[]) => {
          const idx = prev.findIndex((i) => i.item_id === item.item_id);
          if (idx < 0) return [...prev, updated];
          const cp = [...prev];
          cp[idx] = { ...cp[idx], ...updated };
          return cp;
        });
        setSelectedBaItem((prev) =>
          prev && prev.item_id === item.item_id ? { ...prev, ...updated } : prev,
        );
      }
      if (data?.summary) setBaSummary(data.summary);
      else setBaSummary(recomputeBaSummary(baItems, baDefsRef.current));
      setBaEditItem(null);
    } catch (e) {
      const err = e as { message?: string; response?: { data?: { detail?: string } } };
      msg.error(err?.response?.data?.detail || err.message || "保存失败");
    } finally {
      setBaEditSaving(false);
    }
  }, [baEditItem, baEditValue, id, schemeProjectId, baItems, msg]);

  /** 撤销校正（DELETE /results/{item_id}，回到 idle + source='ai'）
   *  不删行本身 —— 行是「解析项槽位」，删掉会破坏 sort_order 展示顺序与 upsert 语义。 */
  const handleBaClearEdit = useCallback(async () => {
    const item = baEditItem;
    if (!item || !id) return;
    setBaEditSaving(true);
    try {
      const { data } = await bidAnalysisApi.clearResult(item.item_id!, id, schemeProjectId);
      msg.success("已清空该项结果（回到待提取状态）");
      if (data?.item) {
        const updated = { ...item, ...data.item };
        setBaItems((prev: BaStoredItem[]) => {
          const idx = prev.findIndex((i) => i.item_id === item.item_id);
          if (idx < 0) return [...prev, updated];
          const cp = [...prev];
          cp[idx] = { ...cp[idx], ...updated };
          return cp;
        });
        setSelectedBaItem((prev) =>
          prev && prev.item_id === item.item_id ? { ...prev, ...updated } : prev,
        );
      }
      if (data?.summary) setBaSummary(data.summary);
      else setBaSummary(recomputeBaSummary(baItems, baDefsRef.current));
      setBaEditItem(null);
    } catch (e) {
      const err = e as { message?: string; response?: { data?: { detail?: string } } };
      msg.error(err?.response?.data?.detail || err.message || "清空失败");
    } finally {
      setBaEditSaving(false);
    }
  }, [baEditItem, id, schemeProjectId, baItems, msg]);

  /** 配置弹窗确认 → 按模式启动 */
  const handleStartBaFromConfig = useCallback(() => {
    const selected = baModeOpt === "custom" ? baSelectedIds : undefined;
    if (baModeOpt === "custom" && (!selected || selected.length === 0)) {
      msg.warning("自定义模式至少勾选一个解析项");
      return;
    }
    setBaConfigOpen(false);
    runBaSse({
      mode: baModeOpt,
      selectedItemIds: selected,
      forceRerun: baForceRerun,
    });
  }, [baModeOpt, baSelectedIds, baForceRerun, runBaSse, msg]);

  return {
    runBaSse,
    handleStopBa,
    handleCheckSections,
    handleRerunItem,
    openBaEdit,
    handleBaSaveEdit,
    handleBaClearEdit,
    handleStartBaFromConfig,
  };
}
