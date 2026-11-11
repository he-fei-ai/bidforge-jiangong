import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { factsApi, sseFetch, tasksApi } from "../../api";
import { createSseBatcher } from "../../utils/sseBatcher";
import { appendBoundedLog } from "../../utils/perf/logBuffer";
import { isTerminalContentEvent, isTerminalTaskStatus } from "../../utils/contentEvents";
import { isDocFailed, isDocParsed, type DocRecord } from "../../utils/workflowDerived";
import { applyFactsCompletedEvent } from "./factsShared";
import type { FactsLogItem } from "./logTypes";
import { reattachTaskImpl, type TaskTerminalInfo, type UnknownTaskInfo } from "./taskPolling";
import type { FactsGenEvent, FactsSegmentStats, SseBaseEvent } from "./workbenchTypes";
import type { FactCrossConflict, FactsSummary } from "../../types/facts";

/**
 * ✅ 2026-11-04 拆分（G11）：全局事实提取主链自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：`runGenerateFactsSse` / `handleGenerateFacts` 函数体
 * 逐字搬运（含 R106 FIX-8 清空 activeTaskIdRef、R174 未知态守卫、断线重挂
 * facts_result 恢复）。事实日志/分段统计/矛盾清单仍由页面持有（loaders 与
 * Tab 展示共用），经 deps 注入。
 */
export interface FactsGenerationDeps {
  id: string | undefined;
  msg: MessageInstance;
  modal: { confirm: (cfg: ModalFuncProps) => void };
  onSseEvent: (evt: SseBaseEvent) => boolean;
  loadFacts: (pageOverride?: number) => Promise<void>;
  markUnknownTask: (ut: UnknownTaskInfo) => void;
  guardUnknownTaskGeneration: (label: string, proceed: () => void) => void;
  hasUnknownTask: () => boolean;
  pollTaskUntilTerminal: (taskId: string) => Promise<TaskTerminalInfo | null>;
  activeTaskIdRef: MutableRefObject<string>;
  abortControllerRef: MutableRefObject<AbortController | null>;
  sseBatcherRef: MutableRefObject<ReturnType<typeof createSseBatcher> | null>;
  genSeqRef: MutableRefObject<number>;
  setGenerating: (v: boolean) => void;
  setGenType: (v: string) => void;
  setProgress: (v: number) => void;
  setProgressMsg: (v: string) => void;
  factsLogsRef: MutableRefObject<FactsLogItem[]>;
  setFactsLogs: Dispatch<SetStateAction<FactsLogItem[]>>;
  setFactsSegmentStats: Dispatch<SetStateAction<FactsSegmentStats | null>>;
  setFactsCrossConflicts: Dispatch<SetStateAction<FactCrossConflict[]>>;
  missingValueMode: "fabricate" | "omit" | "placeholder";
  docList: DocRecord[];
  factsSummary: FactsSummary | null;
  /** ✅ R193（D-08）：页面级方案 id 引用——点击发生在旧渲染闭包里，弹窗确认/
   *  重试请求返回时方案可能已切换，以旧 id 发起 SSE 会把旧方案的进度/矛盾
   *  写入新方案共享 state。可选注入（测试替身可不传，守卫自动跳过）。 */
  currentSchemeIdRef?: MutableRefObject<string>;
}

export function useFactsGeneration(deps: FactsGenerationDeps) {
  const {
    id,
    msg,
    modal,
    onSseEvent,
    loadFacts,
    markUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
    pollTaskUntilTerminal,
    activeTaskIdRef,
    abortControllerRef,
    sseBatcherRef,
    genSeqRef,
    setGenerating,
    setGenType,
    setProgress,
    setProgressMsg,
    factsLogsRef,
    setFactsLogs,
    setFactsSegmentStats,
    setFactsCrossConflicts,
    missingValueMode,
    docList,
    factsSummary,
    currentSchemeIdRef,
  } = deps;

  const runGenerateFactsSse = async () => {
    if (!id) return;
    // ✅ R193 修复（D-08 · 跨方案污染）：点击发生在旧方案渲染闭包里，弹窗
    //    确认/重试请求返回时方案可能已切换 —— 以旧 id 发起 SSE 会把旧方案
    //    提取的 progress/logs/segmentStats/crossConflicts 全部写入当前页面
    //    共享 state（新方案视图显示旧方案的提取过程）。与 handleAckStaleOne
    //    的 sid 守卫同域（useDocumentWorkflow 已有先例）。
    const _currentScheme = currentSchemeIdRef?.current;
    if (_currentScheme && _currentScheme !== id) {
      msg.info("方案已切换，已取消本次提取");
      return;
    }
    // ✅ 世代校验：标记本代生成，收尾时仅当仍是最新一代才复位全局状态
    const mySeq = ++genSeqRef.current;
    // ✅ R106 FIX-8：立刻清空上一代 task_id。本代任务的 task_id 由 SSE
    //    首个 connecting 事件写入 activeTaskIdRef（后端 FIX-7 已把它前移到装配
    //    之前），在写入之前若仍留着上一代**已终结**的 id，用户此刻点停止就会
    //    打到死 id（后端 {ok:false,message:'任务不存在或已结束'}）—— 新任务
    //    从未收到指令，表现为「停止按钮没反应」。清空后走诚实回执，不再误发。
    activeTaskIdRef.current = ""; // R106 FIX-8 · 事实链
    // 创建新的 AbortController
    abortControllerRef.current?.abort();
    const ac = new AbortController();
    abortControllerRef.current = ac;
    // ✅ 性能优化：为事实提取 SSE 创建独立的 rAF 批处理器
    sseBatcherRef.current?.stop();
    sseBatcherRef.current?.flushNow();
    const batcher = createSseBatcher();
    sseBatcherRef.current = batcher;
    setGenerating(true);
    setGenType("facts");
    setProgress(0);
    setProgressMsg("正在提取...");
    // ✅ 修复：旧实现无条件 setActiveTab("facts")，confirm 弹窗停留期间用户
    //    切走 Tab 会被强行拽回。入口按钮就在 facts Tab 内，无需跳转。
    // 清空本次提取日志与上次的分段统计
    setFactsLogs([]);
    factsLogsRef.current = [];
    setFactsSegmentStats(null);
    setFactsCrossConflicts([]);
    let lastProgress = 0;
    const pushLog = (p: number, m: string) => {
      if (!m) return;
      const list = factsLogsRef.current;
      if (list.length && list[list.length - 1].message === m) return;
      // ✅ 2026-11-11 性能：写入端有界缓冲。旧实现 [...list, item] 无限追加，
      //    渲染层的 200 行截断只管 DOM，数据源仍随 SSE 事件线性增长并每次 O(n) 复制。
      factsLogsRef.current = appendBoundedLog(list, {
        progress: p,
        message: m,
        time: Date.now(),
      });
      batcher.schedule("factsLogs", () => setFactsLogs(factsLogsRef.current));
    };
    let taskId = "";
    try {
      for await (const raw of sseFetch(
        `/sse/generate-facts/${id}`,
        { missing_value_mode: missingValueMode },
        { signal: ac.signal },
      )) {
        // 后端 SSE 契约：全局事实提取链事件，一次性断言（字段见 FactsGenEvent）
        const evt = raw as FactsGenEvent;
        if (!taskId && evt.task_id) taskId = evt.task_id;
        if (evt.event === "error") {
          pushLog(lastProgress, `❌ ${evt.message || "提取失败"}`);
          // ✅ R205（全局事实→界面数据同步）：分段提取逐段落库，异常前可能
          //    已写入部分事实；旧实现不刷新列表，界面停在启动前的旧事实集。
          loadFacts();
        } else if (evt.event === "warning") {
          // ✅ R193（D-13）：warnings 元素按契约是 string，但类型是宽松数组——
          //    非 string 元素旧实现 join 出 "[object Object]" 噪声文案，逐项
          //    安全文本化（对象 JSON 化）。
          const wm =
            evt.message ||
            (Array.isArray(evt.warnings)
              ? evt.warnings
                  .map((w) => (typeof w === "string" ? w : JSON.stringify(w) ?? String(w)))
                  .join("；")
              : "");
          if (wm) pushLog(lastProgress, `⚠️ ${wm}`);
        }
        if (onSseEvent(evt)) break;
        // ✅ R180（2026-11-05）：部分段未成功时后端终态事件为 degraded（与
        //    completed 同语义：事实已落库、可继续生成正文）。旧实现只认 completed
        //    ⇒ 降级完成时既不收尾也不 loadFacts()，进度条停在 96% 且失败段横幅
        //    不刷新。终态判定统一走共享出口（contentEvents 单一事实源）。
        if (isTerminalContentEvent(evt)) {
          lastProgress = 1;
          const doneMsg = evt.message || "全局事实提取完成";
          // 终态也走同一批量器：按 key 覆盖挂起的旧进度，避免收尾 flushNow
          // 把已排队的 p<1 写回去造成进度条回退。
          batcher.schedule("progress", () => setProgress(1));
          batcher.schedule("progressMsg", () => setProgressMsg(doneMsg));
          // 降级完成用 ⚠️ 日志与 warning toast，避免用户误以为全部成功
          const _isDegraded = evt.event !== "completed";
          pushLog(1, `${_isDegraded ? "⚠️" : "✅"} ${doneMsg}`);
          // ✅ BUG 修复（2026-09-21）：收口到 applyFactsCompletedEvent ——
          //    增量「全部跳过（all_skipped）」的 completed 不带 segment_stats，
          //    旧实现 `if (evt.segment_stats)` 只写不清，上一次提取的失败告警 /
          //    跨段矛盾横幅会残留到本次（甚至跨方案，直到下一次提取覆盖）。
          const _done = applyFactsCompletedEvent(evt);
          setFactsSegmentStats(_done.segmentStats);
          setFactsCrossConflicts(_done.crossConflicts);
          if (_isDegraded || (evt.segment_stats?.failed || 0) > 0) {
            msg.warning(doneMsg);
          } else {
            msg.success("全局事实提取完成");
          }
          loadFacts();
          break;
        } else if (evt.event === "stopped") {
          pushLog(lastProgress, "⏹ 已停止");
          msg.info(evt.message || "已停止");
          // ✅ R205：与 error 终态同口径，停止前已落库的分段事实必须刷新，
          //    否则列表与 DB 状态脱节（用户以为「什么都没提取到」）。
          loadFacts();
          break;
        // ✅ R208：事实链后端从不推 connecting（仅目录/正文两链推），
        //    该分支是 R203 登记的死分支，删除以免误以为本链有建连阶段。
        } else if (evt.event === "progress") {
          lastProgress = evt.progress || 0;
          const p = lastProgress;
          const m = evt.message || "";
          // ✅ 2026-11-11 性能：心跳级事件必须走 rAF 批量器，避免跨 await
          //    边界退化成「每事件 2 次独立 render」。
          batcher.schedule("progress", () => setProgress(p));
          batcher.schedule("progressMsg", () => setProgressMsg(m));
          pushLog(p, m);
        } else if (evt.event === "stats" || evt.event === "ping") {
          // ✅ P1 修复（2026-09-27 · 心跳统计消费）：后端 facts 链路已接入
          //    stats_provider（sse_handlers.py generate_facts），单段 AI 调用期间
          //    event_stream 挂起无法 yield progress，靠心跳周期推 ping 携带
          //    progress/elapsed_ms。此处消费它消除「进度条长时间静止」。
          //    注意：ping **不得**推进 lastProgress 的语义之外的东西，也不写日志
          //    （否则日志行会被心跳刷屏）；仅按单调口径更新进度条与耗时提示。
          //    未知/未来事件一律落到此分支之外被静默忽略，保持向前兼容。
          //
          // ✅ 2026-10-12（技术债 P2-11 · 事件名口径对齐）：
          //    本分支原只认 "ping"，而同一文件的目录生成分支（:5200）与正文生成
          //    分支（:6022）都是 `"stats" || "ping"` 双认；且后端
          //    `services/ai/task_registry.py` 的 broadcast 确实会推
          //    `{"event": "stats", ...}` —— 三处消费同一套 SSE 协议却有两种
          //    白名单，stats 到达本链路时被静默丢弃（进度条不更新、已用时不动），
          //    且症状只在这一个入口出现，极易被误判成「后端没推」。
          //    现与其它两处同口径（纯加法：原本被忽略的事件现在被消费）。
          const p = typeof evt.progress === "number" ? evt.progress : null;
          if (p !== null && p > lastProgress) {
            lastProgress = p;
            const snapshot = p;
            batcher.schedule("progress", () => setProgress(snapshot));
          }
          const el = typeof evt.elapsed_ms === "number" ? evt.elapsed_ms : null;
          if (el !== null && el >= 0) {
            const m = `正在提取全局事实…（已用时 ${Math.round(el / 1000)}s）`;
            batcher.schedule("progressMsg", () => setProgressMsg(m));
          }
        }
      }
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err.name !== "AbortError") {
        msg.warning(err.message || "SSE 连接中断");
        // ✅ BUG 修复（2026-10-14 · P1 事实提取断线不重挂）：与目录/正文生成同口径。
        //    旧实现仅当 taskId 非空才重挂；建连阶段（首个事件前）断连时 taskId 为空
        //    → 重挂分支整体跳过，而后端任务已注册并继续跑（AI 照常计费），
        //    segment_stats / cross_conflicts 只落在 facts_result checkpoint，
        //    用户只看到「SSE 连接中断」，失败段数与跨段矛盾彻底丢失。
        //    复用 findRecentOutlineTaskImpl 的 typeFilter 参数（原设计即为此），
        //    检索不到时维持旧行为：仅提示。
        // ✅ 收口（2026-10-14）：与目录/正文同源走 reattachTaskImpl
        //    （typeFilter=facts_generation）。终态后的 segment_stats / cross_conflicts
        //    恢复仍由本块处理（facts_result 各链语义不同）。
        const hit = await reattachTaskImpl(
          async () => (await tasksApi.list(id)).data,
          pollTaskUntilTerminal,
          {
            taskId,
            typeFilter: "facts_generation",
            onReattach: () =>
              batcher.schedule("progressMsg", () =>
                setProgressMsg("连接中断，正在重新挂接后台任务..."),
              ),
          },
        );
        // ✅ R174 F2：轮询耗尽/连续网络失败（final=null）→ 显式进入可恢复未知态。
        if (hit && !hit.final) {
          markUnknownTask({
            taskId: hit.taskId,
            genType: "facts",
            reason: "重挂轮询超限（200×3s）或连续网络失败，未获得任务终态",
          });
        }
        if (hit) {
          taskId = hit.taskId;
          const fin = hit.final;
          // ✅ R180（2026-11-05）：终态判定走共享出口，覆盖 completed / degraded。
          //    旧实现只认 completed，降级完成后既不恢复 segment_stats / 矛盾横幅，
          //    也不 loadFacts()（用户只看到“连接中断”提示）。
          if (fin && isTerminalTaskStatus(fin.status)) {
            if (fin.status === "failed") {
              msg.error(fin.message || "后台任务失败");
            } else if (fin.status === "stopped") {
              msg.info("后台任务已停止");
            } else {
              // completed 或 degraded：两者都是「成果已落库」，degraded 额外提示不完整
              // ✅ P0 修复（2026-09-27 · 断线重挂成果恢复）：后端已把 completed 事件的
              //    segment_stats / cross_conflicts 落进 facts_result checkpoint 并由
              //    GET /sse/task/{id} 回传。此处复用**同一个** applyFactsCompletedEvent
              //    解析器（与在线 completed 分支 :5408 完全同源），保证重挂接后
              //    失败段数/跨段矛盾横幅照常呈现，而不是只弹一句"已完成"。
              //    缺 facts_result 时（后端版本旧 / checkpoint 写失败）回退原行为。
              const _fin = fin.facts_result;
              const _isDegraded = fin.status !== "completed";
              // ✅ 进度条收尾（2026-09-27）：断线重挂成功后必须把进度推到 100%
              //    并回填终态文案，否则进度条永远停在中断刻度（对比在线分支
              //    :5420-5423 的 setProgress(1) + setProgressMsg(doneMsg)）。
              const _doneMsg = _fin?.message || fin.message || "全局事实提取已在后台完成";
              lastProgress = 1;
              batcher.schedule("progress", () => setProgress(1));
              batcher.schedule("progressMsg", () => setProgressMsg(_doneMsg));
              pushLog(1, `${_isDegraded ? "⚠️" : "✅"} ${_doneMsg}`);
              if (_fin) {
                const _d = applyFactsCompletedEvent(_fin);
                setFactsSegmentStats(_d.segmentStats);
                setFactsCrossConflicts(_d.crossConflicts);
                // segment_stats 为生成的宽松契约（Record<string, unknown>），failed 实际为 number
                if (_isDegraded || ((_fin.segment_stats?.failed as number | undefined) || 0) > 0) {
                  msg.warning(_doneMsg);
                } else {
                  msg.success(_doneMsg);
                }
              } else {
                // ✅ 状态必须**显式清空**（2026-09-27）：在线路径的
                // applyFactsCompletedEvent 语义是「缺字段即清空」，而重挂接
                // 拿不到 checkpoint 时若什么都不做，上一次提取的失败告警 /
                // 跨段矛盾横幅会残留在界面上（甚至跨方案）。此处复用**同一个**
                // 解析器（传空对象）拿到规范化的「空」值，与在线路径逐字一致 ——
                // 不手写 null/[]，避免两处口径再次分叉。
                const _empty = applyFactsCompletedEvent({});
                setFactsSegmentStats(_empty.segmentStats);
                setFactsCrossConflicts(_empty.crossConflicts);
                if (_isDegraded) {
                  msg.warning(_doneMsg);
                } else {
                  msg.success(_doneMsg);
                }
              }
              loadFacts();
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

  /** ✅ 优化：合并原「从资料文档提取」（REST）与 SSE 提取（两者功能重复），
      统一入口 + 已有事实时的覆盖确认 */
  const handleGenerateFacts = async () => {
    if (!id) return;
    // ✅ R174 F2：未知态下重新生成必须先查询一次任务状态（防并发/重复计费）
    if (hasUnknownTask()) {
      guardUnknownTaskGeneration("全局事实提取", () => void handleGenerateFacts());
      return;
    }
    // ✅ 分步工作流 ③ 前置校验：必须有已解析的文档
    const parsedCount = docList.filter((d) => isDocParsed(d)).length;
    if (parsedCount === 0) {
      const failed = docList.filter((d) => isDocFailed(d)).length;
      msg.warning(
        failed > 0
          ? `有 ${failed} 个文件解析失败，请先重试解析`
          : docList.length > 0
            ? `有 ${docList.length} 个文件尚未解析，请先点击「解析文档」`
            : "请先上传资料文件（上传保存 → 解析文档 → AI 提取）",
      );
      return;
    }
    if ((factsSummary?.total || 0) > 0) {
      modal.confirm({
        title: "重新提取全局事实",
        content:
          "将基于已上传的项目资料（设计文件/地勘报告/合同等）重新提取全局事实变量。已确认和手动新增的事实会保留，仅未确认的 AI 提取事实会被刷新覆盖。",
        okText: "开始提取",
        cancelText: "取消",
        onOk: () => runGenerateFactsSse(),
      });
      return;
    }
    await runGenerateFactsSse();
  };

  /**
   * ✅ T-05（2026-11-06）：失败段的「立即重试」入口。
   *
   * 语义：先请求后端释放失败段的退避窗口（仅清 `next_retry_at` 并记录诊断，
   * 不改动任何事实数据），然后原样重跑事实提取。失败段若再次失败，后端按
   * 既有指数退避继续生效，不会变成无限重试。
   */
  const handleRetryFailedSegments = async () => {
    if (!id) return;
    if (hasUnknownTask()) {
      guardUnknownTaskGeneration("立即重试失败段", () => void handleRetryFailedSegments());
      return;
    }
    if (docList.filter((d) => isDocParsed(d)).length === 0) {
      msg.warning("没有已解析的资料文档，无法重试失败段");
      return;
    }
    try {
      const res = await factsApi.retryFailed(id);
      // ✅ R193（D-08）：重试请求返回时方案可能已切换——旧实现以旧 id 重跑
      //    SSE（跨方案污染）。切走即取消，与 runGenerateFactsSse 入口守卫同域。
      const _currentScheme = currentSchemeIdRef?.current;
      if (_currentScheme && _currentScheme !== id) {
        msg.info("方案已切换，已取消本次重试");
        return;
      }
      const released = res.data?.released ?? 0;
      if (released <= 0) {
        msg.info(
          res.data?.skipped_source === "manual_retry_no_failed_segments"
            ? "当前没有处于失败或退避状态的段，无需重试"
            : "未释放任何失败段（可能已恢复或已被清理）",
        );
        return;
      }
      const bypassed = res.data?.diagnostics?.bypassed_backoff ?? 0;
      msg.success(`已释放 ${released} 个失败段（其中 ${bypassed} 段绕过退避），开始重新提取…`);
      await runGenerateFactsSse();
    } catch (err) {
      msg.error(err instanceof Error && err.message ? err.message : "重试失败段请求失败");
    }
  };

  return { runGenerateFactsSse, handleGenerateFacts, handleRetryFailedSegments };
}
