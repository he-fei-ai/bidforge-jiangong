import { useEffect, useRef } from "react";

/**
 * 延后触发 + 卸载清理的共享定时器（R157 · 遗留 L1 收口）。
 *
 * 页面「① 上传文件保存」入口与 `UploadParseTab` 的 beforeUpload 此前各自维护
 * 一份 `uploadTimerRef` 手工副本（登记 / 赋值前清旧 / 置空三步）。R144 教训①
 * 「同族修复漏平行入口」的根因即双副本 —— R136 FIX-7 修组件、R144 修页面，
 * 两次都是先漏一边。收敛为单一 hook 后该纪律不再依赖人的记忆：
 *
 * - `schedule(fn)`：赋值前先 clearTimeout 未触发的旧 timer（宏任务间隙内
 *   再次选文件时，旧 timer 若不清理会双批并发上传），再登记新 timer；
 * - `clear()`：幂等清理（卸载 effect 自动调用，调用方可在卸载钩子里显式
 *   再调一次与其它 AbortController 清理并列）；
 * - 回调在触发时以 schedule 时刻的闭包执行（与既有行为逐字等价）。
 */
export function useDelayedCallback(): {
  schedule: (fn: () => void) => void;
  clear: () => void;
} {
  const timerRef = useRef<number | null>(null);
  const clear = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  useEffect(() => clear, []);
  const schedule = (fn: () => void) => {
    clear();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      fn();
    }, 0);
  };
  return { schedule, clear };
}
