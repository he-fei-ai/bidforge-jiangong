import { useRef, useState } from "react";
import type { MutableRefObject } from "react";
import type { ModalFuncProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { tasksApi } from "../../api";
import { resolveUnknownTaskNext, findRecentOutlineTaskImpl, type TaskTerminalInfo, type UnknownTaskInfo } from "./taskPolling";

/**
 * ✅ 2026-11-04 拆分（G10）：重挂放弃后的「任务态未知」状态机自
 * SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：`markUnknownTask` / `clearUnknownTask` /
 * `handleReattachUnknownTask` / `guardUnknownTaskGeneration` 函数体逐字搬运，
 * 状态与 ref 一并收敛到本 hook（ref 仍是守卫的唯一事实源，state 只驱动横幅）。
 * 页面保留 `UNKNOWN_TASK_TYPE_LABEL`（横幅文案）与 `pollTaskUntilTerminal`
 * （三条生成链共用），经 deps 注入。
 */
export interface UnknownTaskRecoveryDeps {
  id: string | undefined;
  msg: MessageInstance;
  modal: { confirm: (cfg: ModalFuncProps) => void };
  generating: boolean;
  genSeqRef: MutableRefObject<number>;
  // ✅ R194（FIX-13）：手动接管期间回填，恢复停止/暂停按钮的控制可达性
  activeTaskIdRef: MutableRefObject<string>;
  setGenerating: (v: boolean) => void;
  setGenType: (v: string) => void;
  setTaskPaused: (v: boolean) => void;
  setProgress: (v: number) => void;
  setProgressMsg: (v: string) => void;
  load: () => Promise<void>;
  pollTaskUntilTerminal: (taskId: string) => Promise<TaskTerminalInfo | null>;
  // ✅ R194（FIX-16）：content 任务终态消费 content_result（失败明细合并进日志区
  //    属页面作用域，经回调注入；缺省不消费，行为与引入前一致）
  onContentResult?: (fin: TaskTerminalInfo) => void;
  // ✅ R216（2026-11-11 · P2 · D-1）：outline 任务终态消费 outline_result —— 目录
  //    生成不写 sections（必须经一级闸门确认才入库），completed 只 load() 刷不出
  //    任何东西 ⇒ checkpoint 里的目录成果凭空丢失。镜像 onContentResult 注入。
  onOutlineResult?: (fin: TaskTerminalInfo) => void;
  // ✅ R224（F17）：facts 任务手动接管终态消费 facts_result —— segment_stats /
  //    cross_conflicts 此前只在断线重挂恢复（useFactsGeneration reattach 分支），
  //    手动接管与刷新接管两路缺口（content=R194 FIX-16 / outline=R216 D-1 同族）。
  //    completed/degraded 的统计恢复与 loadFacts 在回调内完成；failed/stopped
  //    仍走通用分支。
  onFactsResult?: (fin: TaskTerminalInfo) => void;
  unknownTaskTypeLabel: Record<string, string>;
}

export function useUnknownTaskRecovery(deps: UnknownTaskRecoveryDeps) {
  const {
    id,
    msg,
    modal,
    generating,
    genSeqRef,
    activeTaskIdRef,
    setGenerating,
    setGenType,
    setTaskPaused,
    setProgress,
    setProgressMsg,
    load,
    pollTaskUntilTerminal,
    onContentResult,
    onOutlineResult,
    onFactsResult,
    unknownTaskTypeLabel,
  } = deps;

  // ✅ R174 F2（2026-11-03）：重挂轮询耗尽（200×3s）或连续网络失败后的
  //    「任务态未知」可恢复状态。ref 是守卫的唯一事实源（用户点「暂时隐藏」
  //    只隐藏横幅、不清 ref —— 在「可能仍在跑」的未知态下，重新生成必须
  //    经 guardUnknownTaskGeneration 二次确认，不允许无提示重复发起）。
  const [unknownTask, setUnknownTask] = useState<UnknownTaskInfo | null>(null);
  const unknownTaskRef = useRef<UnknownTaskInfo | null>(null);

  /** 进入未知态：ref（守卫事实源）+ state（横幅展示）+ 明确 warning */
  // 注：G10 迁移后不再手工 memo —— 该函数无「身份稳定」依赖方（调用点均为
  //   普通闭包调用），保持与页面内原位实现等价，同时不引入 eslint 抑制注释。
  const markUnknownTask = (ut: UnknownTaskInfo) => {
    unknownTaskRef.current = ut;
    setUnknownTask(ut);
    msg.warning(
      `「${unknownTaskTypeLabel[ut.genType] || ut.genType}」任务与前端失去联系，` +
        `后台任务可能仍在运行（AI 仍在计费）。请通过页面顶部横幅「重新接管任务」恢复监听；` +
        `重新生成前请先按指引查询任务状态，避免并发 / 重复计费`,
      8,
    );
  };

  /** 清除未知态（仅在确认任务已终态 / 不存在 / 接管到终态后调用） */
  const clearUnknownTask = () => {
    unknownTaskRef.current = null;
    setUnknownTask(null);
  };

  /** 手动恢复入口：重新接管未知态任务的轮询（用户触发，非自动无限轮询） */
  const handleReattachUnknownTask = async () => {
    const ut = unknownTaskRef.current;
    // ✅ R194（FIX-13）：本协程是否回填过 activeTaskIdRef（退出时只清自己写的值）
    let didBackfill = false;
    if (!ut || !id) return;
    // ✅ R223（2026-11-12 · P2 · FE-3）：任务归属校验 —— 恢复回调（onOutlineResult →
    //    outlineRestoreFnRef）闭包捕获**当前页面**的方案 id，接管异方案任务会把
    //    旧方案的 checkpoint 目录 setTree 到本页并在闸门确认后写进当前方案
    //    （跨方案整表重建）。标记点已带 schemeId（R223 同批），有归属且不匹配
    //    时拒绝接管并清除横幅；无 schemeId 的旧内存态放行（同页面生命周期，
    //    不存在跨升级持久化）。
    if (ut.schemeId && ut.schemeId !== id) {
      msg.warning("该未知任务属于其他方案，已清除本页标记；请回到对应方案页面接管");
      clearUnknownTask();
      return;
    }
    if (generating) {
      msg.info("当前已有任务在前台监听，请等待其结束");
      return;
    }
    // 世代快照：接管期间用户若发起新生成（bump genSeqRef），本循环到终态时
    // 不得越位复位新任务的状态（与刷新接管同口径）。
    // ✅ 2026-11-07：未知态可能来自「SSE 断线时尚未收到 task_id」，此时 taskId
    //    为空串。先按方案检索最近同类型在跑任务，检索不到说明后台确无在跑任务，
    //    清除未知态并刷新；避免对空 id 轮询 200×3s。
    let reattachTaskId = ut.taskId;
    if (!reattachTaskId) {
      reattachTaskId =
        (await findRecentOutlineTaskImpl(async () => (await tasksApi.list(id)).data, {
          typeFilter: `${ut.genType}_generation`,
        })) || "";
      if (!reattachTaskId) {
        clearUnknownTask();
        msg.info("未发现仍在运行的后台任务，已恢复空闲");
        load();
        return;
      }
    }
    const mySeq = ++genSeqRef.current;
    setUnknownTask(null); // 横幅先收起；ref 由终态/再次失败分支裁决
    setGenerating(true);
    setGenType(ut.genType);
    setTaskPaused(false);
    setProgress(0);
    setProgressMsg("正在重新接管后台任务...");
    // ✅ R194（FIX-13 · C-4）：手动接管同样回填 activeTaskIdRef —— 接管轮询
    //    窗口内停止/暂停按钮可见但 handleControl 读空 id 永远提示「尚未取得任务
    //    标识」。接管拿到的是**同一任务**的 id，回填语义与刷新接管（:1828）对齐；
    //    退出时只清本路径写入的值。
    if (!activeTaskIdRef.current) {
      activeTaskIdRef.current = reattachTaskId;
      didBackfill = true;
    }
    try {
      const fin = await pollTaskUntilTerminal(reattachTaskId);
      if (genSeqRef.current !== mySeq) return;
      if (!fin) {
        // 再次耗尽：回到未知态（ref 一直在，只是恢复横幅与提示）
        markUnknownTask({ ...ut, reason: "再次接管仍未获得任务终态" });
        return;
      }
      clearUnknownTask();
      // ✅ R194（FIX-16 · C-9）：content 任务终态消费 content_result（失败明细 /
      //    进度摘要），与断线重挂路径同口径；mergeFailedSections 属页面作用域，
      //    经 onContentResult 回调注入（hook 不持有日志区状态）。
      if (ut.genType === "content") onContentResult?.(fin);
      // ✅ R216（2026-11-11 · P2 · D-1）：outline 任务终态消费 outline_result
      //    （树恢复 / 截断与复用信号 / 闸门）—— 此前 completed 走
      //    「已完成，正在刷新」+ load()，目录生成不写 sections ⇒ 成果丢失。
      if (ut.genType === "outline") onOutlineResult?.(fin);
      // ✅ R224（F17）：facts 手动接管 completed/degraded 走专用消费（统计恢复 +
      //    loadFacts 都在回调内），不走下方通用分支防双提示；failed/stopped 仍走
      //    通用分支。
      if (ut.genType === "facts" && (fin.status === "completed" || fin.status === "degraded")) {
        onFactsResult?.(fin);
      } else if (ut.genType !== "outline") {
        // ✅ R223（2026-11-12 · P2 · FE-1）：outline 终态**不得再走通用 load() 分支** ——
        //    onOutlineResult 已恢复树/闸门（或无成果时自行 load），此处的无条件
        //    load() 会拉回保存前旧树，把刚 setTree 的成果预览冲掉（目录生成不写
        //    sections，刷新拿不到生成成果）；对照刷新接管（有 checkpoint 成果时
        //    不 load）与断线重挂同纪律，手动接管是漏网的第三条。content/facts
        //    写各自的数据表，通用 load() 刷新语义保留。
        if (fin.status === "completed" || fin.status === "degraded") {
          msg.success("后台任务已完成，正在刷新");
          load();
        } else if (fin.status === "failed") {
          msg.error(fin.message || "后台任务失败");
        } else {
          msg.info("后台任务已停止");
          load();
        }
      }
    } finally {
      if (genSeqRef.current === mySeq) {
        setGenerating(false);
        setGenType("");
        setTaskPaused(false);
        if (didBackfill && activeTaskIdRef.current === reattachTaskId) {
          activeTaskIdRef.current = "";
        }
      }
    }
  };

  /**
   * 未知态下的新生成守卫：三条生成链（目录/正文/事实）入口统一调用。
   * 无未知态直接放行；有未知态必须先一次性查询任务状态：
   *   running/paused → 自动转入重新接管，不放行新生成；
   *   终态 / 404     → 清除未知态后放行；
   *   其它 / 查询失败 → 保持未知态，本次不放行（用户可重试）。
   */
  const guardUnknownTaskGeneration = (label: string, proceed: () => void) => {
    const ut = unknownTaskRef.current;
    if (!ut) {
      proceed();
      return;
    }
    modal.confirm({
      title: "⚠️ 上一次后台任务状态未知，可能仍在运行",
      width: 540,
      content: (
        <div>
          <p>
            上一次「{label}」任务与前端失去联系超过 10 分钟（重挂轮询耗尽）， 任务
            <b>可能仍在后台运行（AI 仍在计费）</b>。
          </p>
          <p>直接重新生成可能导致并发任务 / 重复计费。建议先查询该任务状态：</p>
          <ul style={{ paddingLeft: 20, margin: "4px 0" }}>
            <li>仍在运行 → 自动重新接管监听，本次不发起新生成</li>
            <li>已结束 / 不存在 → 清除标记并继续发起新生成</li>
          </ul>
        </div>
      ),
      okText: "查询任务状态",
      cancelText: "取消",
      onOk: async () => {
        try {
          // ✅ R224（F18）：空 taskId（断线时未收到 task_id 的未知态）直接
          //    GET /sse/task/"" → 路由不匹配必 404 → 被 catch 按「任务已不存在，
          //    可安全重新生成」放行 —— 守卫对该形态恒 fail-open。与
          //    handleReattachUnknownTask（:116 空 id 检索兜底）对称：先按类型
          //    检索最近同类型任务，检索不到才判「后台确无在跑任务」放行。
          let checkTaskId = ut.taskId;
          if (!checkTaskId) {
            checkTaskId =
              (await findRecentOutlineTaskImpl(async () => (await tasksApi.list(id)).data, {
                typeFilter: `${ut.genType}_generation`,
              })) || "";
            if (!checkTaskId) {
              clearUnknownTask();
              msg.info("未发现仍在运行的后台任务，可安全重新生成");
              proceed();
              return;
            }
          }
          const { data } = await tasksApi.status(checkTaskId);
          const next = resolveUnknownTaskNext(data?.status);
          if (next === "reattach") {
            msg.warning("后台任务仍在运行，已转入重新接管监听，本次未发起新生成");
            void handleReattachUnknownTask();
            return;
          }
          if (next === "proceed") {
            clearUnknownTask();
            msg.info(`后台任务已结束（${data?.status || "终态"}），可安全重新生成`);
            proceed();
            return;
          }
          msg.warning(
            `任务状态异常（${data?.status || "空"}），为避免重复计费本次未发起新生成，请稍后重试`,
          );
        } catch (e: unknown) {
          const status = (e as { response?: { status?: number } })?.response?.status;
          if (status === 404) {
            clearUnknownTask();
            msg.info("后台任务已不存在（可能已被清理），可安全重新生成");
            proceed();
            return;
          }
          // ✅ R208：响应拦截器已把后端 detail 写进 error.message，旧实现
          //    硬编码文案把 503/超时等真实病因吞掉，用户无从判断可否重试。
          const err = e as { message?: string; response?: { data?: { detail?: string } } };
          msg.error(
            err?.response?.data?.detail ||
              err?.message ||
              "查询任务状态失败，请检查网络后重试（本次未发起新生成）",
          );
        }
      },
    });
  };

  /** 三条生成链入口的只读判定（替代页面直接读 ref） */
  const hasUnknownTask = () => unknownTaskRef.current !== null;

  return {
    unknownTask,
    setUnknownTask,
    markUnknownTask,
    clearUnknownTask,
    handleReattachUnknownTask,
    guardUnknownTaskGeneration,
    hasUnknownTask,
  };
}
