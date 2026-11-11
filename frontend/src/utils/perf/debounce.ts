import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 高频输入的统一防抖口径（300ms）。
 *
 * 用法：把「昂贵结果区」挂在 debounced 值上，输入区仍用即时值 ——
 * 这样每敲一键只重渲输入框本身，正则/过滤/排序/网络请求都落在静默 300ms 之后。
 *
 * 生命周期：value 变化会清掉上一个 timer；组件卸载时 clearTimeout，
 * 杜绝「卸载后 setState」。delay <= 0 时退化为同步透传（测试/SSR 友好）。
 */
export const DEFAULT_INPUT_DEBOUNCE_MS = 300;

export function useDebouncedValue<T>(value: T, delay = DEFAULT_INPUT_DEBOUNCE_MS): T {
  const [debounced, setDebounced] = useState<T>(value);

  useEffect(() => {
    if (delay <= 0) {
      setDebounced(value);
      return;
    }
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}

export type DebouncedCallback<A extends unknown[]> = {
  run: (...args: A) => void;
  flush: () => void;
  cancel: () => void;
};

/**
 * 防抖回调：保留最后一次参数，卸载时丢弃未执行的调用（不产生延迟 setState）。
 * 与 useDebouncedValue 共用同一 300ms 口径，避免各组件各写一个魔法数字。
 */
export function useDebouncedCallback<A extends unknown[]>(
  fn: (...args: A) => void,
  delay = DEFAULT_INPUT_DEBOUNCE_MS,
): DebouncedCallback<A> {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const timerRef = useRef<number | null>(null);
  const pendingRef = useRef<{ has: boolean; args: A }>({ has: false, args: [] as unknown as A });

  const cancel = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    pendingRef.current.has = false;
  }, []);

  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (pendingRef.current.has) {
      const args = pendingRef.current.args;
      pendingRef.current.has = false;
      fnRef.current(...args);
    }
  }, []);

  const run = useCallback(
    (...args: A) => {
      pendingRef.current = { has: true, args };
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        flush();
      }, delay);
    },
    [delay, flush],
  );

  useEffect(() => cancel, [cancel]);

  return { run, flush, cancel };
}
