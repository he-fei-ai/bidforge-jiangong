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

  const doGenerateOutline = async () => {
    if (!id) return;
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
    try {
      for await (const raw of sseFetch(`/sse/generate-outline/${id}`, undefined, {
        signal: ac.signal,
      })) {
        // 后端 SSE 契约：目录生成链事件，一次性断言（字段见 OutlineGenEvent）
        const evt = raw as OutlineGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (evt.event === "error") {
          onSseEvent(evt);
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
        if (evt.event === "completed") {
          // ✅ 性能优化：终态事件立即 flush，确保 UI 与后续操作一致
          batcher.flushNow();
          setProgress(1);
          setProgressMsg("目录生成完成");
          pushOutlineLog(1, "✅ 目录生成完成");
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
            openOutlineGate(evt.outline);
          } else {
            msg.warning("AI 返回空目录，未保存");
          }
          break;
        } else if (evt.event === "stopped") {
          // ✅ 性能优化：终态事件立即 flush
          batcher.flushNow();
          setProgressMsg("已停止");
          pushOutlineLog(lastP, "⏹ 已停止");
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
        // ✅ R174 F2：轮询耗尽/连续网络失败（final=null）→ 显式进入可恢复未知态。
        if (hit && !hit.final) {
          markUnknownTask({
            taskId: hit.taskId,
            genType: "outline",
            reason: "重挂轮询超限（200×3s）或连续网络失败，未获得任务终态",
          });
        }
        if (hit) {
          const fin = hit.final;
          if (fin?.status === "completed") {
            // ✅ 成果恢复：后台完成的目录只存在于任务 checkpoint 中，
            // 仅 load() 刷不到任何东西（目录需用户闸门确认才入库）。
            const ckpt = fin.outline_result;
            if (ckpt?.outline && Array.isArray(ckpt.outline) && ckpt.outline.length) {
              // checkpoint 回传的 outline 即目录树节点（后端 SSE 契约），经 Array.isArray 判别后收窄
              const restoredOutline = ckpt.outline as OutlineNodeInput[];
              setTree(buildTree(restoredOutline));
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
              openOutlineGate(restoredOutline);
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
              msg.info(
                ckpt?.event === "error"
                  ? "后台任务因异常中断，且无可恢复的部分成果"
                  : "后台任务已停止",
              );
            }
          }
        }
      }
    } finally {
      if (abortControllerRef.current === ac) {
        abortControllerRef.current = null;
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
