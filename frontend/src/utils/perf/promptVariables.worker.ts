import { diffPromptVariables, extractPromptVariables } from "../promptVariables";

export interface PromptVariablesRequest {
  current: string;
  baseline: string;
}

/**
 * 模板变量扫描在 Worker 内执行：长模板（可达 20 万字符）每键两次全量正则
 * 扫描会占满主线程，输入掉帧。计算逻辑直接复用 utils/promptVariables，
 * 保证 Worker 与主线程 fallback 是同一份实现（单一事实源）。
 *
 * 刻意不写 `/// <reference lib="webworker" />`：该三斜杠引用会把全局 self/window
 * 事件映射改成 DedicatedWorkerGlobalScopeEventMap，污染 DOM 侧 window.addEventListener
 * 的类型（本仓 tsc 已实测报 TS2367）。改用局部窄类型，零全局副作用。
 */
type WorkerScope = {
  onmessage: ((event: MessageEvent<{ id: number; payload: PromptVariablesRequest }>) => void) | null;
  postMessage: (message: unknown) => void;
};

const ctx = self as unknown as WorkerScope;

ctx.onmessage = (event) => {
  const { id, payload } = event.data || ({} as { id: number; payload: PromptVariablesRequest });
  const current = payload?.current ?? "";
  const baseline = payload?.baseline ?? "";
  ctx.postMessage({
    id,
    result: {
      variables: extractPromptVariables(current),
      diff: diffPromptVariables(current, baseline),
    },
  });
};
