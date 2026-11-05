import { useRef, useState } from "react";
import type { MutableRefObject } from "react";
import type { ModalFuncProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { tasksApi } from "../../api";
import { resolveUnknownTaskNext, type TaskTerminalInfo, type UnknownTaskInfo } from "./taskPolling";

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
  setGenerating: (v: boolean) => void;
  setGenType: (v: string) => void;
  setTaskPaused: (v: boolean) => void;
  setProgress: (v: number) => void;
  setProgressMsg: (v: string) => void;
  load: () => Promise<void>;
  pollTaskUntilTerminal: (taskId: string) => Promise<TaskTerminalInfo | null>;
  unknownTaskTypeLabel: Record<string, string>;
}

export function useUnknownTaskRecovery(deps: UnknownTaskRecoveryDeps) {
  const {
    id,
    msg,
    modal,
    generating,
    genSeqRef,
    setGenerating,
    setGenType,
    setTaskPaused,
    setProgress,
    setProgressMsg,
    load,
    pollTaskUntilTerminal,
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
    if (!ut || !id) return;
    if (generating) {
      msg.info("当前已有任务在前台监听，请等待其结束");
      return;
    }
    // 世代快照：接管期间用户若发起新生成（bump genSeqRef），本循环到终态时
    // 不得越位复位新任务的状态（与刷新接管同口径）。
    const mySeq = ++genSeqRef.current;
    setUnknownTask(null); // 横幅先收起；ref 由终态/再次失败分支裁决
    setGenerating(true);
    setGenType(ut.genType);
    setTaskPaused(false);
    setProgress(0);
    setProgressMsg("正在重新接管后台任务...");
    try {
      const fin = await pollTaskUntilTerminal(ut.taskId);
      if (genSeqRef.current !== mySeq) return;
      if (!fin) {
        // 再次耗尽：回到未知态（ref 一直在，只是恢复横幅与提示）
        markUnknownTask({ ...ut, reason: "再次接管仍未获得任务终态" });
        return;
      }
      clearUnknownTask();
      if (fin.status === "completed" || fin.status === "degraded") {
        msg.success("后台任务已完成，正在刷新");
        load();
      } else if (fin.status === "failed") {
        msg.error(fin.message || "后台任务失败");
      } else {
        msg.info("后台任务已停止");
        load();
      }
    } finally {
      if (genSeqRef.current === mySeq) {
        setGenerating(false);
        setGenType("");
        setTaskPaused(false);
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
          const { data } = await tasksApi.status(ut.taskId);
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
          msg.error("查询任务状态失败，请检查网络后重试（本次未发起新生成）");
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
