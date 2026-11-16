/**
 * useElementSize —— 基于 ResizeObserver 的「容器真实尺寸」订阅 Hook（2026-11-16 · R250）。
 *
 * 【为什么需要它】可拖拽分隔面板（components/SplitPane）拖拽时改变的是**面板容器**的
 * 尺寸，而不是 window 尺寸——所以 window.resize 事件根本不会触发。既有
 * resizeCoordinator 走的是「拖拽中暂停重绘、停手 120ms 后统一重绘一次」的阶段机制，
 * 能防拖拽掉帧，但它**并不观测容器真实尺寸**：内部那些「按容器宽度自适应」的组件
 * （Mermaid 图、表格、代码编辑器）若只靠阶段信号，仍可能用旧宽度渲染而暂时变形。
 *
 * 本 Hook 用一个真正的 ResizeObserver 订阅容器 content-box 尺寸，把 width/height
 * 暴露为 React state，供宽度敏感组件按**实测宽度**重排/重绘（如 mermaid 的
 * useMaxWidth、表格列宽、编辑器 layout）。
 *
 * 【设计要点】
 *  - 单实例：每个 Hook 只建一个 ResizeObserver，随 ref 节点变化 re-observe；
 *  - rAF 合帧：拖拽时 contentRect 每帧都变，若每次都 setState 会掉帧，故用
 *    requestAnimationFrame 合帧，卸载时 cancelAnimationFrame 收尾；
 *  - SSR / 无 ResizeObserver 兜底：永不 throw，退化为 width/height = 0 的 no-op；
 *  - 只读 content-box：与 CSS box-sizing 解耦，符合「内容可用空间」语义。
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** 容器实测尺寸（content-box，px）。首次观测前为 0。 */
export interface ElementSize {
  width: number;
  height: number;
}

/**
 * 订阅某个元素的 content-box 尺寸。
 *
 * @returns `[size, ref]`
 *   - `size`：`{ width, height }`，实测 content-box 尺寸（首次观测前为 0）；
 *   - `ref`：挂到要观测的容器元素上（`<div ref={ref} />`）。
 */
export function useElementSize<T extends HTMLElement>(): [ElementSize, (node: T | null) => void] {
  // 观测到的尺寸（初始 0，等首次 ResizeObserver 回调 / ref 挂载后才有真实值）
  const [size, setSize] = useState<ElementSize>({ width: 0, height: 0 });

  // 当前观测的 DOM 节点（由 callback ref 写入，触发下方 effect 重新 observe）
  const [node, setNode] = useState<T | null>(null);
  // ResizeObserver 实例（跨 effect 复用，卸载时 disconnect）
  const observerRef = useRef<ResizeObserver | null>(null);
  // rAF 合帧句柄（拖拽期间每帧一次 setState 太频，用 rAF 合到下一帧）
  const rafRef = useRef<ReturnType<typeof requestAnimationFrame> | null>(null);

  /** callback ref：把节点交给 state，节点变化会触发 observe effect。 */
  const ref = useCallback((n: T | null) => {
    setNode(n);
  }, []);

  useEffect(() => {
    // SSR / 极旧环境无 ResizeObserver：直接 no-op（size 保持 0），绝不让调用方崩溃。
    if (typeof ResizeObserver === "undefined") return;
    if (!node) return;

    // 单实例：本 effect 生命周期内只建一次，节点变化时复用同一个 observer。
    if (!observerRef.current) {
      observerRef.current = new ResizeObserver((entries) => {
        const entry = entries[entries.length - 1];
        if (!entry) return;
        const { width, height } = entry.contentRect;
        // rAF 合帧：把同帧内的多次回调压成一次 setState，拖拽不掉帧。
        if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
        rafRef.current = requestAnimationFrame(() => {
          rafRef.current = null;
          setSize({ width, height });
        });
      });
    }

    const observer = observerRef.current;
    observer.observe(node);

    // 观测是异步的：节点刚挂载时先同步量一次，避免首帧用 0 宽渲染闪烁。
    const rect = node.getBoundingClientRect();
    setSize({ width: rect.width, height: rect.height });

    return () => {
      observer.unobserve(node);
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [node]);

  // 组件卸载：断开 observer，杜绝常驻内存泄漏。
  useEffect(() => {
    return () => {
      if (observerRef.current) {
        observerRef.current.disconnect();
        observerRef.current = null;
      }
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, []);

  return [size, ref];
}

export default useElementSize;
