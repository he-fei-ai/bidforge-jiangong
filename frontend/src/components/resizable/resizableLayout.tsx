/**
 * 可拖拽面板布局 · 持久化 + 全局重置上下文（2026-10-30 R145）
 *
 * 职责：
 *  - localStorage 读写（键前缀 bp-rp:，存 0~1 的比例值，适应窗口尺寸变化）；
 *  - ResizableLayoutProvider：全局重置信号（重置布局按钮 → 所有 SplitPane 回默认值）；
 *  - clearAllResizableLayouts：清空本应用全部布局持久化键。
 *
 * 设计说明：
 *  - 持久化「比例」而非 px：窗口宽度变化后按比例还原，再由 SplitPane 用
 *    px 级 min/max 夹紧，兼顾「刷新后保持」与「窄屏不坍塌」；
 *  - 所有读写 try/catch fail-soft：隐私模式 / 存储被禁用时布局功能静默降级。
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export const RESIZE_STORAGE_PREFIX = "bp-rp:";

/** 窄屏阈值：<1024px 禁用拖拽，自动降级为单栏/折叠布局（用户需求三-3） */
export const RESIZE_NARROW_THRESHOLD = 1024;

/** 读取持久化比例；无值 / 非法值（非有限数、<=0、>=1）返回 null */
export function readStoredFraction(key: string): number | null {
  try {
    const raw = window.localStorage.getItem(RESIZE_STORAGE_PREFIX + key);
    if (raw == null) return null;
    const v = Number(raw);
    if (!Number.isFinite(v) || v <= 0 || v >= 1) return null;
    return v;
  } catch {
    return null;
  }
}

export function writeStoredFraction(key: string, fraction: number): void {
  if (!Number.isFinite(fraction) || fraction <= 0 || fraction >= 1) return;
  try {
    window.localStorage.setItem(RESIZE_STORAGE_PREFIX + key, String(fraction));
  } catch {
    // fail-soft：存储不可用时不影响拖拽本身
  }
}

export function clearStoredFraction(key: string): void {
  try {
    window.localStorage.removeItem(RESIZE_STORAGE_PREFIX + key);
  } catch {
    // ignore
  }
}

/** 清空本应用全部布局持久化键（重置布局按钮） */
export function clearAllResizableLayouts(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(RESIZE_STORAGE_PREFIX)) doomed.push(k);
    }
    for (const k of doomed) window.localStorage.removeItem(k);
  } catch {
    // ignore
  }
}

type ResizableLayoutCtx = {
  /** 每次「重置布局」自增；SplitPane 监听变化后回默认比例 */
  resetSignal: number;
  bump: () => void;
};

const Ctx = createContext<ResizableLayoutCtx>({ resetSignal: 0, bump: () => {} });

export function ResizableLayoutProvider({ children }: { children: ReactNode }) {
  const [resetSignal, setResetSignal] = useState(0);
  const bump = useCallback(() => setResetSignal((s) => s + 1), []);
  const value = useMemo(() => ({ resetSignal, bump }), [resetSignal, bump]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useResizableResetSignal(): number {
  return useContext(Ctx).resetSignal;
}

/** 「重置布局」动作：清空全部持久化键 + 广播重置信号 */
export function useResizableLayoutReset(): () => void {
  const { bump } = useContext(Ctx);
  return useCallback(() => {
    clearAllResizableLayouts();
    bump();
  }, [bump]);
}
