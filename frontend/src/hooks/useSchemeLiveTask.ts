/**
 * useSchemeLiveTask：监听指定方案的后台任务运行状态
 *
 * 数据源：GET /api/v1/sse/tasks?scheme_id=（后端 task_registry 表 + 内存实时态合并）
 *
 * 能力：
 * - 3s 轮询（单飞 + 页面隐藏时暂停），返回该方案当前 running/paused 的任务（无则 null）；
 * - 任务由运行态进入终态（completed/failed/stopped）时触发 onFinished
 *   （调用方用于刷新页面数据，兑现「完成后自动刷新」承诺）；
 * - 任务开始/终态事件写入全局消息中心（不弹 toast，统一由 Header 气泡展示）；
 *   suppressNotify() 返回 true 时跳过记录（页面自身生成流程已有自己的消息，避免重复）。
 */
import { useEffect, useRef, useState } from "react";
import { tasksApi } from "../api";
import { pushActivity, setLiveTask as setStoreLiveTask } from "../utils/activityCenter";

export type LiveTask = {
  id: string;
  task_type: string;
  status: string;
  progress: number;
  message?: string;
  scheme_id?: string;
  live?: boolean;
};

// ✅ 性能优化（2026-09-24 · 遗留项 #1）：自适应轮询间隔。
// 有运行/暂停任务时 3s 快频（及时感知完成并触发 onFinished 刷新数据）；
// 空闲（无运行任务）时 15s 慢频探查 —— 工作台开着但不跑任务时
// 轮询量直接 ↓80%，缓解「前端轮询风暴」对后端连接池/审计查询的压力。
export const ACTIVE_POLL_MS = 3000;
export const IDLE_POLL_MS = 15000;

/** 依据「当前是否有运行/暂停任务」决定下一次轮询间隔（纯函数，供单测）。 */
export function liveTaskPollIntervalMs(hasActiveTask: boolean): number {
  return hasActiveTask ? ACTIVE_POLL_MS : IDLE_POLL_MS;
}

// ✅ 修复（2026-09-18）：补齐 bid_analysis，避免任务栏显示原始英文类型名
const TYPE_LABELS: Record<string, string> = {
  outline_generation: "目录生成",
  content_generation: "正文生成",
  facts_generation: "事实提取",
  bid_analysis: "结构化提取",
};
export const liveTaskTypeLabel = (t: string) => TYPE_LABELS[t] || t || "后台任务";

export function useSchemeLiveTask(
  schemeId?: string,
  opts?: {
    onFinished?: () => void;
    suppressNotify?: () => boolean;
  }
) {
  const [liveTask, setLiveTask] = useState<LiveTask | null>(null);
  // 上一轮各任务状态（用于检测 running/paused → 终态 的跳变）
  const prevStatusRef = useRef<Map<string, string>>(new Map());
  // ✅ 性能优化：上一次写入的全局 active 任务（浅比较用，数据未变时跳过 setState）
  const lastActiveRef = useRef<LiveTask | null>(null);
  const inFlightRef = useRef(false);
  const optsRef = useRef(opts);
  optsRef.current = opts;

  useEffect(() => {
    if (!schemeId) {
      setLiveTask(null);
      setStoreLiveTask(null);
      lastActiveRef.current = null;
      prevStatusRef.current = new Map();
      return;
    }
    let stopped = false;
    let timer: number | null = null;

    // ✅ 性能优化（2026-09-24 · 遗留项 #1）：每轮轮询结束后按最新 active 状态
    //    重排下一次定时器（有任务 3s / 空闲 15s），替代原先固定 3s 的 setInterval。
    const scheduleNext = (intervalMs: number) => {
      if (timer !== null) window.clearInterval(timer);
      timer = window.setInterval(poll, intervalMs);
    };

    const poll = async () => {
      if (stopped || inFlightRef.current || document.hidden) return;
      inFlightRef.current = true;
      try {
        const { data } = await tasksApi.list(schemeId, 5);
        if (stopped) return;
        const items: LiveTask[] = data.tasks || [];
        const active =
          items.find((t) => t.status === "running" || t.status === "paused") || null;
        // ✅ 性能优化：轮询每 3s 都会产生新对象身份，若关键字段未变则复用旧引用、
        //    跳过 setState，避免空闲期每 3s 一次的整页重渲
        const prevActive = lastActiveRef.current;
        const same =
          (prevActive === null && active === null) ||
          (!!prevActive && !!active &&
            prevActive.id === active.id &&
            prevActive.status === active.status &&
            prevActive.progress === active.progress &&
            prevActive.message === active.message);
        if (!same) {
          lastActiveRef.current = active;
          setLiveTask(active);
          // ✅ 同步到全局消息中心存储：全局 Header 的「后台任务进行中」提示据此展示
          setStoreLiveTask(active);
        }

        // 任务状态跳变检测：新出现运行中任务 / 运行中 → 终态
        const prev = prevStatusRef.current;
        const suppress = optsRef.current?.suppressNotify?.() ?? false;
        for (const t of items) {
          const before = prev.get(t.id);
          const label = liveTaskTypeLabel(t.task_type);
          const pct = Math.round((t.progress || 0) * 100);
          if (before !== "running" && before !== "paused") {
            // 新检测到的运行中任务（含页面刷新后重新挂接）→ 记入消息中心
            if (!suppress && (t.status === "running" || t.status === "paused")) {
              pushActivity(
                "info",
                `检测到后台「${label}」任务${t.status === "paused" ? "已暂停" : "正在运行"}（${pct}%）`,
                "后台任务"
              );
            }
            continue;
          }
          if (t.status === before) continue;
          if (t.status === "completed" || t.status === "failed" || t.status === "stopped") {
            // ✅ 后台消息只进消息中心（全局 Header 气泡展示），不再弹重复 toast
            if (!suppress) {
              pushActivity(
                t.status === "completed" ? "success" : t.status === "failed" ? "warning" : "info",
                `后台「${label}」任务${
                  t.status === "completed" ? "已完成，数据已刷新" : t.status === "failed" ? "失败，请检查后重试" : "已停止"
                }${t.message && t.status !== "completed" ? `：${t.message}` : ""}`,
                "后台任务"
              );
            }
            optsRef.current?.onFinished?.();
          }
        }
        prevStatusRef.current = new Map(items.map((t) => [t.id, t.status]));
      } catch {
        // 离线/异常保持安静（侧边栏健康状态已负责提示）
      } finally {
        inFlightRef.current = false;
        if (!stopped) {
          // 自适应调度：本轮结束按最新 active 状态重排间隔
          scheduleNext(liveTaskPollIntervalMs(lastActiveRef.current !== null));
        }
      }
    };

    poll();
    if (timer === null) {
      // 初始调用可能因页面隐藏提前返回（finally 未执行），兜底保证定时器存在
      scheduleNext(liveTaskPollIntervalMs(false));
    }
    const onVisible = () => {
      if (!document.hidden) poll();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      if (timer !== null) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      // 离开本方案页面 → 清除全局 Header 的后台任务提示
      setStoreLiveTask(null);
    };
  }, [schemeId]);

  return liveTask;
}
