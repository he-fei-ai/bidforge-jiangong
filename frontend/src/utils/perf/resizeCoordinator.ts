import { useSyncExternalStore } from "react";

/**
 * 全局「尺寸变化阶段」协调器。
 *
 * 真实的连续尺寸变化有三个来源：
 *   ① 拖动浏览器窗口边缘（resize）；② antd Tree 拖拽排序（drop 时一次性重排）；
 *   ③ **可拖拽分隔面板的拖拽**（components/resizable/ResizablePanels —— 拖拽
 *      过程中面板尺寸连续变化，与拖窗口同性质）。
 * 旧实现里 5 个 useBreakpoint 调用方各挂一个 resize 监听，同帧 5 次 setState /
 * 5 次整树 diff；图表渲染也没有「拖拽期间暂停」的信号。
 *
 * 这里用**一个** window resize 监听把阶段广播给所有订阅者：
 *   active（拖拽中）→ idle（停手 120ms 后），并对外提供 useSyncExternalStore
 *   订阅，让 MarkdownRenderer 在 active 期间暂停解析/重绘、回到 idle 后统一重绘一次。
 *
 * ✅ 2026-11-16：面板拖拽不产生 window resize 事件，故新增 notifyResize() 作为
 *    **同一个**协调器的第二个入口（不新建第二份实现）：分隔面板拖拽时调用它，
 *    图表即可复用「拖拽中暂停 / 停手后重绘一次」的既有机制。
 */
export type ResizePhase = "idle" | "active";

const RESIZE_SETTLE_MS = 120;
const listeners = new Set<() => void>();
let phase: ResizePhase = "idle";
let rafId = 0;
let settleTimer = 0;
let listenerAttached = false;
let onResizeRef: (() => void) | null = null;

function emit(): void {
  for (const cb of listeners) cb();
}

function setPhase(next: ResizePhase): void {
  if (phase === next) return;
  phase = next;
  emit();
}

function detach(): void {
  if (!listenerAttached || typeof window === "undefined") return;
  if (onResizeRef) window.removeEventListener("resize", onResizeRef);
  if (rafId) window.cancelAnimationFrame(rafId);
  if (settleTimer) window.clearTimeout(settleTimer);
  rafId = 0;
  settleTimer = 0;
  onResizeRef = null;
  listenerAttached = false;
  setPhase("idle");
}

/**
 * 尺寸变化信号（window resize 与面板拖拽共用同一条通道）。
 * active 立即生效（rAF 合帧），idle 在最后一次信号后 120ms 静默期到达。
 */
function signalResize(): void {
  if (typeof window === "undefined") return;
  if (!rafId) {
    rafId = window.requestAnimationFrame(() => {
      rafId = 0;
      setPhase("active");
    });
  }
  if (settleTimer) window.clearTimeout(settleTimer);
  settleTimer = window.setTimeout(() => {
    settleTimer = 0;
    setPhase("idle");
  }, RESIZE_SETTLE_MS);
}

function attach(): void {
  if (listenerAttached || typeof window === "undefined") return;
  listenerAttached = true;
  const onResize = () => signalResize();
  onResizeRef = onResize;
  window.addEventListener("resize", onResize, { passive: true });
}

/**
 * 主动播报一次「尺寸变化」（面板拖拽等不产生 window resize 的尺寸变化场景）。
 *
 * 与 window resize 走同一条通道 => 订阅者（MarkdownRenderer 等）无需区分来源，
 * 不会产生第二套「重绘时机」口径。
 */
export function notifyResize(): void {
  signalResize();
}

export function subscribeResizePhase(cb: () => void): () => void {
  attach();
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
    // 最后一个订阅者离开时释放监听/定时器，避免常驻内存占用
    if (listeners.size === 0) detach();
  };
}

export function getResizePhase(): ResizePhase {
  return phase;
}

export function isLayoutResizing(): boolean {
  return phase === "active";
}

export function useResizePhase(): ResizePhase {
  return useSyncExternalStore(subscribeResizePhase, getResizePhase, () => "idle" as ResizePhase);
}
