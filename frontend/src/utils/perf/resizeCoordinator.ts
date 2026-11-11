import { useSyncExternalStore } from "react";

/**
 * 全局「尺寸变化阶段」协调器。
 *
 * 本仓库没有自定义 Splitter / 面板拖拽，真实的连续尺寸变化来自：
 *   ① 拖动浏览器窗口边缘（resize）；② antd Tree 拖拽排序（drop 时一次性重排）。
 * 旧实现里 5 个 useBreakpoint 调用方各挂一个 resize 监听，同帧 5 次 setState /
 * 5 次整树 diff；图表渲染也没有「拖拽期间暂停」的信号。
 *
 * 这里用**一个** window resize 监听把阶段广播给所有订阅者：
 *   active（拖拽中）→ idle（停手 120ms 后），并对外提供 useSyncExternalStore
 *   订阅，让 MarkdownRenderer 在 active 期间暂停解析/重绘、回到 idle 后统一重绘一次。
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

function attach(): void {
  if (listenerAttached || typeof window === "undefined") return;
  listenerAttached = true;
  const onResize = () => {
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
  };
  onResizeRef = onResize;
  window.addEventListener("resize", onResize, { passive: true });
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
