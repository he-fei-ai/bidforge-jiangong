import { useRef } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { sectionsApi, sseFetch, tasksApi } from "../../api";
import { summarizeOutlineQuality } from "./reviewShared";
import { pickStats, statsEqual } from "./statsFormat";
import { createSseBatcher } from "../../utils/sseBatcher";
import {
  buildPartialOutlineHint,
  buildTree,
  countOutline,
  hasAnyContent,
  type TreeNode,
} from "./outlineTreeModel";
import type { OutlineLogItem } from "./logTypes";
import { outlineTruncationNotice } from "./outlineNotices";
import { reattachTaskImpl, type TaskTerminalInfo, type UnknownTaskInfo } from "./taskPolling";
import type { OutlineGenEvent, OutlineNodeInput, SseBaseEvent } from "./workbenchTypes";
import type { GenStats } from "../../utils/contentEvents";
import type { WorkflowTabKey } from "../../utils/workflowDerived";

/**
 * ✅ 2026-11-04 拆分（G12）：目录生成主链自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：`doGenerateOutline` / `handleGenerateOutline` 函数体
 * 逐字搬运（含 R106 FIX-8 清空 activeTaskIdRef、R174 未知态守卫、断线重挂后
 * outline_result 成果恢复与部分保存确认）。`onSseEvent`（R72 解析提示消费点）
 * 与 `pushOutlineLog` 仍留在页面（正文链复用），经 deps 注入。
 */
export interface OutlineGenerationDeps {
  id: string | undefined;
  msg: MessageInstance;
  modal: { confirm: (cfg: ModalFuncProps) => void };
  tree: TreeNode[];
  setTree: Dispatch<SetStateAction<TreeNode[]>>;
  hasContentRef: MutableRefObject<boolean>;
  openOutlineGate: (outline: OutlineNodeInput[]) => void;
  /** ✅ R179 N1（2026-11-05）：自动确认开关（默认 false）。
   *  开启时 completed/重挂路径改走 `handleOutlineReady`；关闭时**逐字走原
   *  `openOutlineGate(...)`**，人工路径零行为变化。 */
  autoConfirmEnabled: boolean;
  /** 自动确认编排入口（开关关闭时内部逐字委托 openOutlineGate）。 */
  handleOutlineReady: (outline: OutlineNodeInput[], rawSignal?: unknown) => void;
  notifyClearedContent: (data: { cleared_content_sections?: number } | null | undefined) => void;
  notifyGenericResidual: (data: unknown) => void;
  load: () => Promise<void>;
  pushOutlineLog: (p: number, m: string, done?: number, total?: number) => void;
  onSseEvent: (evt: SseBaseEvent) => boolean;
  summarizeOutlineReviewBasis: (review: unknown) => {
    level: "info" | "none";
    message: string;
    logs: string[];
  };
  setActiveTab: (key: WorkflowTabKey) => void;
  setGenerating: (v: boolean) => void;
  setGenType: (v: string) => void;
  setProgress: (v: number) => void;
  setProgressMsg: (v: string) => void;
  setGenStats: Dispatch<SetStateAction<GenStats>>;
  setOutlineLogs: Dispatch<SetStateAction<OutlineLogItem[]>>;
  outlineLogsRef: MutableRefObject<OutlineLogItem[]>;
  setFailedOutlineChapters: Dispatch<SetStateAction<string[]>>;
  genSeqRef: MutableRefObject<number>;
  activeTaskIdRef: MutableRefObject<string>;
  abortControllerRef: MutableRefObject<AbortController | null>;
  sseBatcherRef: MutableRefObject<ReturnType<typeof createSseBatcher> | null>;
  markUnknownTask: (ut: UnknownTaskInfo) => void;
  guardUnknownTaskGeneration: (label: string, proceed: () => void) => void;
  hasUnknownTask: () => boolean;
  pollTaskUntilTerminal: (taskId: string) => Promise<TaskTerminalInfo | null>;
}

export function useOutlineGeneration(deps: OutlineGenerationDeps) {
  const {
    id,
    msg,
    modal,
    tree,
    setTree,
    hasContentRef,
    openOutlineGate,
    autoConfirmEnabled,
    handleOutlineReady,
    notifyClearedContent,
    notifyGenericResidual,
    load,
    pushOutlineLog,
    onSseEvent,
    summarizeOutlineReviewBasis,
    setActiveTab,
    setGenerating,
    setGenType,
    setProgress,
    setProgressMsg,
    setGenStats,
    setOutlineLogs,
    outlineLogsRef,
    setFailedOutlineChapters,
    genSeqRef,
    activeTaskIdRef,
    abortControllerRef,
    sseBatcherRef,
    markUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
    pollTaskUntilTerminal,
  } = deps;

  // ✅ R216（2026-11-11 · P3 · D-7）：同步在飞锁（ref 判据，非异步 state）——
  //    生成按钮 disabled={generating} 是异步 state，同帧双击可双发 SSE
  //    （第二代会 abort 第一代，但两个后端任务都可能已注册并开始装配计费）。
  //    与 R212 专家论证/一致性审计同帧双击修法同型。
  const genInFlightRef = useRef(false);
  const doGenerateOutline = async () => {
    if (!id) return;
    if (genInFlightRef.current) {
      msg.info("目录生成已在进行中，请等待完成或先停止当前任务");
      return;
    }
    genInFlightRef.current = true;
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = ""; // R106 FIX-8 · 目录链
    // 每代生成重新武装「正文覆盖风险」二次确认
    hasContentRef.current = false;
    // 创建新的 AbortController，取消上一次未完成的 SSE
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：每次目录生成任务创建独立的 rAF 批处理器。
    sseBatcherRef.current?.stop();
    sseBatcherRef.current?.flushNow();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    setGenType("outline");
    setProgress(0);
    setProgressMsg("正在连接...");
    setActiveTab("outline");
    // 清空本次目录生成日志
    setOutlineLogs([]);
    outlineLogsRef.current = [];
    // ✅ 进度增强：同时清空运行统计，避免上一次正文生成的统计（字数/并发）
    //    或上一次目录任务的阶段标签残留在本次进度卡上
    setGenStats({});
    let lastP = 0;
    let taskId = "";
    // ✅ R216（P2 · D-2）：断线重挂回填 activeTaskIdRef 的跟踪（退出只清自己写的值）
    let _reattachBackfilled = false;
    let _reattachTaskId = "";
    try {
      for await (const raw of sseFetch(`/sse/generate-outline/${id}`, undefined, {
        signal: ac.signal,
      })) {
        // 后端 SSE 契约：目录生成链事件，一次性断言（字段见 OutlineGenEvent）
        const evt = raw as OutlineGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (evt.event === "error") {
          onSseEvent(evt);
          // ✅ R216（2026-11-11 · P3 · D-10）：error 事件消费 failed_chapters ——
          //    后端 error 载荷携带该键（sse_handlers 异常/全章失败出口），
          //    R192 FIX-R13 只对齐了 stopped 两条路径 ⇒ 全章失败时目录树
          //    无橙色高亮。与在线 completed 分支的 failed_chapters 消费同口径。
          if (Array.isArray(evt.failed_chapters) && evt.failed_chapters.length) {
            setFailedOutlineChapters(evt.failed_chapters);
          }
          // ✅ R223（2026-11-12 · P2 · FE-5）：error 分支补截断/复用信号 —— 后端
          //    error 事件已携带该键（R216 SSE-5 从 holder 补齐），其余终态路径
          //    （completed/degraded/stopped/断线重挂/刷新接管）都消费，全章失败
          //    路径独漏 ⇒ 用户把被截断/复用过的目录当完整成果。
          const _errTruncation = outlineTruncationNotice(evt);
          if (_errTruncation) {
            msg.warning(_errTruncation);
            pushOutlineLog(1, `⚠️ 一级目录超上限：${_errTruncation}`);
          }
          if (typeof evt.resumed_chapters === "number" && evt.resumed_chapters > 0) {
            msg.info(`已复用上次中断生成的 ${evt.resumed_chapters} 章（跳过对应 AI 调用）`);
            pushOutlineLog(1, `♻️ 复用上次成果 ${evt.resumed_chapters} 章`);
          }
          // ✅ 遗留修复：全部章节子目录失败时，后端在 error 事件中携带已生成的
          //    一级章标题（outline + partial）。询问用户是否保存这部分成果，
          //    与 stopped 分支同策略（save-outline 智能保留已有正文）。
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const partialCount = countOutline(evt.outline);
            const hasContent = hasAnyContent(tree);
            modal.confirm({
              title: "目录生成失败，但有部分成果",
              okButtonProps: hasContent ? { danger: true } : undefined,
              content: hasContent ? (
                <div>
                  已生成 {partialCount} 个章节节点尚未保存。
                  <b style={{ color: "#cf1322" }}>
                    保存会按新目录重建章节结构，当前已有正文可能因章节无法匹配而被删除
                  </b>
                  ，且不可恢复。确定保存吗？
                </div>
              ) : (
                `已生成 ${partialCount} 个章节节点尚未保存到数据库。是否保存这部分已生成的目录？`
              ),
              okText: hasContent ? "仍要保存（可能删除正文）" : "保存已生成部分",
              cancelText: "不保存",
              onOk: async () => {
                try {
                  const { data } = await sectionsApi.saveOutline(id, {
                    outline: evt.outline!,
                    source: "ai",
                  });
                  msg.success(`已保存 ${partialCount} 个章节节点`);
                  notifyClearedContent(data);
                  notifyGenericResidual(data);
                  load();
                } catch (saveErr: unknown) {
                  const err = saveErr as { message?: string };
                  msg.error(err?.message || "保存失败");
                }
              },
            });
          }
          break;
        }
        if (onSseEvent(evt)) break;
        // ✅ R205（P0 · 跨模块 SSE 断点）：后端 R204 起在有失败章 / 检查点写失败
        //    时把终态 event 从 completed 降为 degraded（sse_handlers.py:2372），
        //    载荷与 completed 完全同构（outline / review / failed_chapters /
        //    auto_confirm / checkpoint_failed）。旧实现在线循环只认 completed /
        //    stopped ⇒ degraded 落到通用 else：setTree 不执行、一级目录闸门不弹、
        //    进度不收口、循环不 break（degraded 只在断线重挂分支有处理）。
        //    现与 completed 共用同一收尾路径，仅文案区分降级语义。
        const _isDegradedOutline = evt.event === "degraded";
        if (evt.event === "completed" || _isDegradedOutline) {
          // ✅ R210（L1-8）：断点续跑可见信号 —— 后端日志早有「复用 N 章」，
          //    但界面零信号（用户不知道这次省了多少配额、也看不出目录是接着上次跑的）。
          if (typeof evt.resumed_chapters === "number" && evt.resumed_chapters > 0) {
            msg.info(`已复用上次中断生成的 ${evt.resumed_chapters} 章（跳过对应 AI 调用），其余章节本次重新生成`);
            pushOutlineLog(1, `♻️ 复用上次成果 ${evt.resumed_chapters} 章`);
          }
          // ✅ R210（L1-2）：一级目录被上限截断时如实告知（绝不静默丢弃）。
          // ✅ R213（FIX-14）：截断文案收口到唯一纯函数（completed / stopped /
          //    断线重挂三条路径同口径），不再各写一份内联模板串。
          const _truncation = outlineTruncationNotice(evt);
          if (_truncation) {
            msg.warning(_truncation);
            pushOutlineLog(1, `⚠️ 一级目录超上限：${_truncation}`);
          }
          // ✅ 性能优化：终态事件立即 flush，确保 UI 与后续操作一致
          batcher.flushNow();
          setProgress(1);
          setProgressMsg(_isDegradedOutline ? "目录生成降级完成（请查看失败章节与检查点提示）" : "目录生成完成");
          pushOutlineLog(1, _isDegradedOutline ? "⚠️ 目录生成降级完成" : "✅ 目录生成完成");
          // ✅ R188（L9）：R187 FIX-2 短方案检查点写失败会终态降级并回传
          //    checkpoint_failed，此前前端零消费 —— 界面显示「完成」但刷新后
          //    成果不可恢复。现明确告警并引导立即手动保存。
          if (evt.checkpoint_failed) {
            msg.warning("成果未能写入检查点：刷新或断线后可能无法恢复，请立即手动保存目录");
            pushOutlineLog(1, "⚠️ 检查点写入失败，请立即手动保存");
          }
          // 长方案分步生成：记录失败章节，前端高亮提示
          if (
            evt.failed_chapters &&
            Array.isArray(evt.failed_chapters) &&
            evt.failed_chapters.length > 0
          ) {
            setFailedOutlineChapters(evt.failed_chapters);
            const fcList = evt.failed_chapters.slice(0, 5).join("、");
            const more = evt.failed_chapters.length > 5 ? `等${evt.failed_chapters.length}章` : "";
            msg.warning(
              `目录生成完成，但 ${evt.failed_count || evt.failed_chapters.length} 章子目录生成失败（${fcList}${more}），已在目录树中高亮标注，建议手动补充或重新生成`,
            );
            pushOutlineLog(
              1,
              `⚠️ ${evt.failed_count || evt.failed_chapters.length} 章子目录生成失败：${fcList}${more}`,
            );
          } else {
            setFailedOutlineChapters([]);
          }
          if (evt.outline && evt.outline.length > 0) {
            // ✅ 立即用已生成的 3 级目录刷新目录树（不依赖 load 时序，避免「目录生成至三级但树未更新」）
            setTree(buildTree(evt.outline));
            // ✅ P1-5：把后端「生成后自动校验」报告（全面性缺口 / 连续性问题 /
            //    冗余候选）呈现在生成日志与提示条里 —— 此前 review.quality
            //    没有任何消费方，用户看不到"方案名称里的工序没落到章节"。
            //    只提示、绝不自动删章（删章不可逆）。
            const _q = summarizeOutlineQuality(evt.review);
            if (_q.level !== "none") {
              _q.logs.forEach((line) => pushOutlineLog(1, line));
              if (_q.level === "warn") msg.warning(_q.message);
            }
            // ✅ R74（G1）：审核依据与已执行动作同样必须呈现 —— 后端每次
            //    程序化出口都带了 review_mode/suggestions，此前前端静默丢弃。
            summarizeOutlineReviewBasis(evt.review).logs.forEach((line) => pushOutlineLog(1, line));
            // 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
            //    旧实现 completed 即自动入库，用户没有确认一级结构的机会；
            //    现改为弹闸门勾选确认后再保存（lock_roots=true 锁定一级章节）。
            // ✅ R179 N1：开关开启时交给自动确认编排；关闭时保留原路径一字不变。
            if (autoConfirmEnabled) {
              handleOutlineReady(evt.outline, evt.auto_confirm);
            } else {
              openOutlineGate(evt.outline);
            }
          } else {
            msg.warning("AI 返回空目录，未保存");
          }
          break;
        } else if (evt.event === "stopped") {
          // ✅ 性能优化：终态事件立即 flush
          batcher.flushNow();
          // ✅ R205：后端 stopped 载荷补了 message（检查点失败时含具体原因），
          //    旧实现硬编码「已停止」把原因吞掉。
          setProgressMsg(evt.message || "已停止");
          pushOutlineLog(lastP, `⏹ ${evt.message || "已停止"}`);
          // ✅ R188（L8/L9）：停止态检查点写失败时后端回传 checkpoint_failed
          //    （payload 仍自带 outline），现如实提示「检查点不可恢复」。
          if (evt.checkpoint_failed) {
            msg.warning("检查点写入失败：刷新或断线后无法恢复，请立即保存已生成目录");
            pushOutlineLog(lastP, "⚠️ 检查点写入失败，请立即手动保存");
          }
          // ✅ R210（L1-8）：停止态同样回传复用章数（用户据此知道这次接着上次跑）。
          if (typeof evt.resumed_chapters === "number" && evt.resumed_chapters > 0) {
            msg.info(`已复用上次中断生成的 ${evt.resumed_chapters} 章（跳过对应 AI 调用）`);
            pushOutlineLog(lastP, `♻️ 复用上次成果 ${evt.resumed_chapters} 章`);
          }
          // ✅ R213（FIX-14）：停止态同样回传截断信号（后端 _finish_stopped_with_partial
          //    已补），旧实现只有在线 completed 会提示 ⇒ 停止并保存半份成果时零感知。
          const _stoppedTruncation = outlineTruncationNotice(evt);
          if (_stoppedTruncation) {
            msg.warning(_stoppedTruncation);
            pushOutlineLog(lastP, `⚠️ 一级目录超上限：${_stoppedTruncation}`);
          }
          // ✅ R192（FIX-R13）：后端停止态明确携带失败章清单（_finish_stopped_with_partial
          //    的 failed_chapters/failed_count），旧实现在线/重挂两条 stopped 路径都只读
          //    outline/checkpoint_failed——已失败章节不在目录树高亮，用户只看到「已停止」，
          //    与 completed 分支（setFailedOutlineChapters + warning）的提示强度不一致。
          if (Array.isArray(evt.failed_chapters) && evt.failed_chapters.length) {
            setFailedOutlineChapters(evt.failed_chapters);
            const failedCount = evt.failed_count || evt.failed_chapters.length;
            msg.warning(`已停止：${failedCount} 章子目录生成失败，保存部分成果后请手动补充`);
            pushOutlineLog(lastP, `⚠️ ${failedCount} 章子目录失败`);
          }
          // ✅ BUG 修复：长方案分步生成被停止时，已生成的部分目录此前只更新到
          // 前端内存 tree，刷新页面即丢失。现询问用户是否保存这部分成果
          //（后端 save-outline 智能保留已有正文，按需重建结构）。
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const partialCount = countOutline(evt.outline);
            const hasContent = hasAnyContent(tree);
            modal.confirm({
              title: "任务已停止",
              okButtonProps: hasContent ? { danger: true } : undefined,
              content: hasContent ? (
                <div>
                  已生成 {partialCount} 个章节的目录尚未保存。
                  <b style={{ color: "#cf1322" }}>
                    保存会按新目录重建章节结构，当前已有正文可能因章节无法匹配而被删除
                  </b>
                  ，且不可恢复。确定保存吗？
                </div>
              ) : (
                `已生成 ${partialCount} 个章节的目录尚未保存到数据库。是否保存这部分已生成的目录？`
              ),
              okText: hasContent ? "仍要保存（可能删除正文）" : "保存已生成部分",
              cancelText: "不保存",
              onOk: async () => {
                try {
                  const { data } = await sectionsApi.saveOutline(id, {
                    outline: evt.outline!,
                    source: "ai",
                  });
                  msg.success(`已保存 ${partialCount} 个章节的目录`);
                  notifyClearedContent(data);
                  notifyGenericResidual(data);
                  load();
                } catch (saveErr: unknown) {
                  const err = saveErr as { message?: string };
                  msg.error(err?.message || "保存失败");
                }
              },
            });
          } else {
            msg.info("目录生成已停止");
          }
          break;
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ 进度增强：长 AI 调用（单次最长 180s）与审核+修复期间，
          //    后端由 _await_with_stats / 心跳通道持续推送运行统计。
          //    这里只更新统计与进度条，不写日志、不清空当前提示文案
          //    —— stats 事件本身不带 message，落到下面的通用分支会把
          //    progressMsg 覆盖成空串。
          // ✅ 性能优化：rAF 批处理 + 相同值去重，避免高频事件引发多余渲染。
          const statsSnapshot = pickStats(evt);
          batcher.schedule("genStats", () =>
            setGenStats((prev) => (statsEqual(prev, statsSnapshot) ? prev : statsSnapshot)),
          );
          if (typeof evt.progress === "number" && evt.progress > lastP) {
            lastP = evt.progress;
            const p = evt.progress;
            batcher.schedule("progress", () => setProgress(p));
            // ✅ 实时增强：日志列表最新一行的百分比同步跟随主进度渐近填充，
            //    不再冻结在阶段进入值（如「正在生成目录... 10%」静止数分钟）
            const logs = outlineLogsRef.current;
            if (logs.length && logs[logs.length - 1].progress < lastP) {
              const next = [...logs];
              next[next.length - 1] = { ...next[next.length - 1], progress: lastP };
              outlineLogsRef.current = next;
              batcher.schedule("outlineLogs", () => setOutlineLogs(next));
            }
          }
        } else {
          lastP = evt.progress || 0;
          const p = lastP;
          batcher.schedule("progress", () => setProgress(p));
          batcher.schedule("progressMsg", () => setProgressMsg(evt.message || ""));
          pushOutlineLog(lastP, evt.message || "", evt.done, evt.total);
          // progress 事件内嵌运行统计（阶段/已耗时/ETA/章节数）
          if (evt.phase || typeof evt.elapsed_ms === "number") {
            const statsSnapshot = pickStats(evt);
            batcher.schedule("genStats", () =>
              setGenStats((prev) => (statsEqual(prev, statsSnapshot) ? prev : statsSnapshot)),
            );
          }
          // ✅ 长方案分步生成：progress 事件携带已生成 outline 部分，实时更新目录树
          if (evt.outline && Array.isArray(evt.outline) && evt.outline.length > 0) {
            const newTree = buildTree(evt.outline);
            batcher.schedule("tree", () => setTree(newTree));
          }
        }
      }
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err.name !== "AbortError") {
        msg.warning(err.message || "SSE 连接中断");
        // ✅ BUG 修复（2026-09-23 · 「30s 连接超时后任务孤儿运行」）：
        //    建连阶段（首个事件到达前）超时/失败时 taskId 尚未产生，旧实现
        //    直接跳过重挂 —— 但后端任务可能已注册并继续生成（AI 照常计费），
        //    成果只躺在 checkpoint。此处按方案检索最近仍在运行的
        //    outline_generation 任务重挂（检索不到才维持旧行为：仅提示）。
        // ✅ 收口（2026-10-14）：检索 → 重挂 → 轮询三段收敛到 reattachTaskImpl
        //    （与正文/事实同源；目录链默认 typeFilter=outline_generation）。
        //    终态后的目录成果恢复（outline_result / failed_chapters / review）
        //    各链语义不同，仍由本块处理。
        const hit = await reattachTaskImpl(
          async () => (await tasksApi.list(id)).data,
          pollTaskUntilTerminal,
          {
            taskId,
            onReattach: () => setProgressMsg("连接中断，正在重新挂接后台任务..."),
          },
        );
        // ✅ R216（2026-11-11 · P2 · D-2）：重挂拿到同一任务 id 后回填
        //    activeTaskIdRef —— R194 FIX-13 已在正文链与未知态手动接管修过同款，
        //    本链漏改（同族漏改第三次实证）。R106 FIX-8 已清空 ref，SSE 首事件
        //    未到即断 ⇒ taskId=""，重挂窗口（最长 200×3s≈10 分钟）内停止按钮
        //    handleControl 读空 id 永远提示「请稍等」，无法停止正在计费的任务。
        if (hit?.taskId && !activeTaskIdRef.current) {
          activeTaskIdRef.current = hit.taskId;
          _reattachBackfilled = true;
          _reattachTaskId = hit.taskId;
        }
        // ✅ R174 F2：轮询耗尽/连续网络失败（final=null）→ 显式进入可恢复未知态。
        // ✅ R223（2026-11-12 · P2 · FE-2）：markUnknownTask 补世代守卫 + 归属 ——
        //    D-4 只护住了 restored 分支（下方 genSeq 校验处）；本分支漏守卫 ⇒
        //    重挂轮询在飞时切方案，耗尽后把**旧方案**任务标成未知态：横幅/toast
        //    落在新方案页，且 hasUnknownTask 拦截新方案的正常生成。与 restored
        //    分支同口径（不提前 return —— post-finally 的批处理器清理与世代复位
        //    必须继续执行）；schemeId 供手动接管校验任务归属（跨方案接管会把
        //    旧方案目录写进新方案，见 UnknownTaskInfo.schemeId 注释）。
        if (hit && !hit.final && genSeqRef.current === mySeq) {
          markUnknownTask({
            taskId: hit.taskId,
            genType: "outline",
            schemeId: id,
            reason: "重挂轮询超限（200×3s）或连续网络失败，未获得任务终态",
          });
        }
        // ✅ R216（2026-11-11 · P2 · D-4）：重挂终态应用加世代守卫 —— finally 的
        //    状态复位有世代校验（:586），但 restored 分支（setTree / 一级目录闸门 /
        //    部分成果确认弹窗）没有：切方案后 D-02 复位块只 abort SSE controller，
        //    拦不住已在飞的重挂轮询 ⇒ 旧方案的目录被恢复到新方案页面、闸门弹窗
        //    指向旧方案 id（数据不写错方案，但 UI 全错位）。与刷新接管循环的
        //    cancelled 标志同口径；不用提前 return —— post-finally 的批处理器
        //    清理与世代复位必须继续执行。
        if (hit && genSeqRef.current === mySeq) {
          const fin = hit.final;
          // ✅ R213（FIX-14）：checkpoint 白名单已登记 level1_*（后端 FIX-6），
          //    断线重挂（completed / failed / stopped 全终态）统一提示一次，
          //    与在线两条路径共用同一文案出口。
          const _reattachTruncation = outlineTruncationNotice(fin?.outline_result);
          if (_reattachTruncation) {
            msg.warning(_reattachTruncation);
            pushOutlineLog(1, `⚠️ 一级目录超上限（重挂）：${_reattachTruncation}`);
          }
          if (fin?.status === "completed") {
            // ✅ 成果恢复：后台完成的目录只存在于任务 checkpoint 中，
            // 仅 load() 刷不到任何东西（目录需用户闸门确认才入库）。
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），经 Array.isArray 判别后收窄
              const restoredOutline = ckpt.outline as OutlineNodeInput[];
              setTree(buildTree(restoredOutline));
              // ✅ R210（L1-8）：断线重挂路径同样回传复用章数（checkpoint 白名单已登记）。
              if (typeof ckpt.resumed_chapters === "number" && ckpt.resumed_chapters > 0) {
                msg.info(`已复用上次中断生成的 ${ckpt.resumed_chapters} 章（跳过对应 AI 调用）`);
                pushOutlineLog(1, `♻️ 复用上次成果 ${ckpt.resumed_chapters} 章`);
              }
              if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                setFailedOutlineChapters(ckpt.failed_chapters);
                msg.warning(
                  `后台目录生成完成，但 ${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录失败，请确认后保存`,
                );
              } else {
                msg.success("目录生成已在后台完成，请确认保存");
              }
              // ✅ R74（G1）：断线重挂路径同样呈现审核依据（与 completed 同口径）
              summarizeOutlineReviewBasis(ckpt.review).logs.forEach((line) =>
                pushOutlineLog(1, line),
              );
              if (autoConfirmEnabled) {
                handleOutlineReady(restoredOutline, ckpt.auto_confirm);
              } else {
                openOutlineGate(restoredOutline);
              }
            } else {
              msg.success("目录生成已在后台完成，正在刷新");
              load();
            }
          } else if (fin?.status === "failed") {
            msg.error(fin.message || "后台任务失败");
            // ✅ 遗留修复：全部章节子目录失败时后端也写了 checkpoint（仅一级章标题），
            //    断线重连后同样提供部分成果保存入口，与在线 error 分支对齐。
            const failCkpt = fin.outline_result;
            if (failCkpt?.outline && Array.isArray(failCkpt.outline) && failCkpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），收窄一次供闭包复用
              const partialOutline = failCkpt.outline as OutlineNodeInput[];
              setTree(buildTree(partialOutline));
              // ✅ R216（2026-11-11 · P3 · D-9）：failed 分支补消费 resumed_chapters
              //    （此前只覆盖 completed 分支；后端 failed checkpoint 同样携带该键，
              //    信息级提示「已复用 N 章」不应只出现一条路径）。
              if (typeof failCkpt.resumed_chapters === "number" && failCkpt.resumed_chapters > 0) {
                pushOutlineLog(1, `♻️ 复用上次成果 ${failCkpt.resumed_chapters} 章`);
              }
              const partialCount = countOutline(partialOutline);
              // ✅ 文案收口到 buildPartialOutlineHint（旧实现两处各写一份，且不说中断原因）
              const hint = buildPartialOutlineHint({
                event: failCkpt.event,
                partial: failCkpt.partial,
                nodeCount: partialCount,
              });
              modal.confirm({
                title: hint.title,
                content: hint.content,
                okText: hint.okText,
                cancelText: hint.cancelText,
                okButtonProps: hint.tone === "warning" ? { danger: true } : undefined,
                onOk: async () => {
                  try {
                    const { data } = await sectionsApi.saveOutline(id, {
                      outline: partialOutline,
                      source: "ai",
                    });
                    msg.success(`已保存 ${partialCount} 个章节节点`);
                    notifyClearedContent(data);
                    notifyGenericResidual(data);
                    load();
                  } catch (saveErr: unknown) {
                    const err = saveErr as { message?: string };
                    msg.error(err?.message || "保存失败");
                  }
                },
              });
            }
          } else if (fin?.status === "stopped") {
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），收窄一次供闭包复用
              const stoppedOutline = ckpt.outline as OutlineNodeInput[];
              setTree(buildTree(stoppedOutline));
              // ✅ R216（2026-11-11 · P3 · D-9）：stopped 分支补消费 resumed_chapters
              //    （同 failed 分支口径；后端 stopped checkpoint 携带该键）。
              if (typeof ckpt.resumed_chapters === "number" && ckpt.resumed_chapters > 0) {
                pushOutlineLog(1, `♻️ 复用上次成果 ${ckpt.resumed_chapters} 章`);
              }
              // ✅ R192（FIX-R13）：重挂 stopped 同样消费失败章清单（与在线分支同口径）
              if (Array.isArray(ckpt.failed_chapters) && ckpt.failed_chapters.length) {
                setFailedOutlineChapters(ckpt.failed_chapters);
                msg.warning(`已停止：${ckpt.failed_count || ckpt.failed_chapters.length} 章子目录生成失败，可保存部分成果后手动补充`);
              }
              const partialCount = countOutline(stoppedOutline);
              const hint = buildPartialOutlineHint({
                event: ckpt.event,
                partial: ckpt.partial,
                nodeCount: partialCount,
              });
              modal.confirm({
                title: hint.title,
                content: hint.content,
                okText: hint.okText,
                cancelText: hint.cancelText,
                onOk: async () => {
                  try {
                    const { data } = await sectionsApi.saveOutline(id, {
                      outline: stoppedOutline,
                      source: "ai",
                    });
                    msg.success(`已保存 ${partialCount} 个章节的目录`);
                    notifyClearedContent(data);
                    notifyGenericResidual(data);
                    load();
                  } catch (saveErr: unknown) {
                    const err = saveErr as { message?: string };
                    msg.error(err?.message || "保存失败");
                  }
                },
              });
            } else {
              // ✅ BUG 修复（2026-09-25）：ckpt 可能为 undefined（stopped 且无 checkpoint
              //    成果），旧实现直接 ckpt.event 抛 TypeError 使收尾逻辑中断，用户既看不到
              //    「已停止」提示也没有弹窗。同函数其它分支均用可选链，唯独此处漏了。
              // ✅ R192（FIX-R14）：R190 FIX-5 后「停止+检查点写失败」终态保持 stopped
              //    但无 checkpoint——旧文案丢弃 fin.message（「用户已停止（检查点写入
              //    失败，刷新后可能无法恢复半份成果…）」），用户不知道半份成果已丢失。
              //    对齐刷新接管路径（SchemeWorkbenchPage `后台任务已停止${st.message…}`）。
              msg.info(
                (ckpt?.event === "error"
                  ? "后台任务因异常中断，且无可恢复的部分成果"
                  : "后台任务已停止") + (fin.message ? `：${fin.message}` : ""),
              );
            }
          } else if (fin?.status === "degraded") {
            // ✅ R192（FIX-R12）：R187/R188 引入终态 degraded（completed 但检查点写
            //    失败，sse_handlers finish_task("degraded")）。在线 completed 分支对
            //    同场景有 checkpoint_failed 告警，刷新接管路径（TERMINAL_TASK_STATUSES
            //    含 degraded）有分支——唯独 SSE 断线重挂路径的终态分发只有
            //    completed/failed/stopped 三支：degraded 落入三支全不命中，无任何
            //    提示、无 load()，进度条永久停在「正在重新挂接后台任务」，成果
            //    确已丢失（检查点没写成，重挂也取不到 outline_result）而用户毫不知情。
            //    现补分支：如实告知 + 收尾刷新（与在线告警同文案口径）。
            msg.warning(
              fin.message ||
                "目录已生成，但成果未能写入检查点：刷新或断线后无法恢复，请重新生成或立即手动保存",
            );
            pushOutlineLog(1, "⚠️ 检查点写入失败，后台成果不可恢复");
            load();
          }
        }
      }
    } finally {
      if (abortControllerRef.current === ac) {
        abortControllerRef.current = null;
      }
      // ✅ R216（P2 · D-2）：只清本协程回填的值（与 R194 FIX-13 同纪律），
      //    且仅当仍是最新一代（被重入时不越位清理新任务的 id）。
      if (
        _reattachBackfilled &&
        genSeqRef.current === mySeq &&
        activeTaskIdRef.current === _reattachTaskId
      ) {
        activeTaskIdRef.current = "";
      }
    }
    // ✅ 世代校验：被重入（abort）的旧协程不得复位新任务的状态
    if (genSeqRef.current === mySeq) {
      setGenerating(false);
      setGenType("");
    }
    // ✅ 性能优化：任务结束或异常退出后，排空残余批处理并停止
    batcher.flushNow();
    batcher.stop();
    if (sseBatcherRef.current === batcher) sseBatcherRef.current = null;
    // ✅ R216（P3 · D-7）：在飞锁释放（所有出口——正常完成 / break / catch /
    //    finally——都会流经此处的 post-finally 清理段）。
    genInFlightRef.current = false;
  };

  /**
   * ✅ 增强：重新生成目录前的覆盖风险确认。
   * 目录生成会整表重建 sections（AI 返回的编号无法与已有章节主键一一对应），
   * 已有正文不会保留；旧实现直接执行，用户容易误点导致正文丢失。
   */
  const handleGenerateOutline = async () => {
    if (!id) return;
    // ✅ R174 F2：未知态下重新生成必须先查询一次任务状态（防并发/重复计费）
    if (hasUnknownTask()) {
      guardUnknownTaskGeneration("目录生成", () => void handleGenerateOutline());
      return;
    }
    if (hasAnyContent(tree)) {
      modal.confirm({
        title: "⚠️ 确认重新生成目录？",
        content: (
          <div>
            当前方案已有生成好的正文。<b>重新生成目录会重建章节结构， 已生成的正文内容将无法保留</b>
            ，且不可恢复。
            <br />
            如需保留正文，请先取消，改用「目录」页的手动编辑或「套用目录库」。
          </div>
        ),
        okText: "仍要重新生成",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: () => doGenerateOutline(),
      });
      return;
    }
    await doGenerateOutline();
  };

  return { doGenerateOutline, handleGenerateOutline };
}
