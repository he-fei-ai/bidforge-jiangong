import type { PromptVariable, PromptVariableDiff } from "../../types/prompt";
import { diffPromptVariables, extractPromptVariables } from "../promptVariables";
import type { PromptVariablesRequest } from "./promptVariables.worker";

export interface PromptVariablesResult {
  variables: PromptVariable[];
  diff: PromptVariableDiff;
}

/** 主线程 fallback：jsdom / 不支持 module Worker 的环境走同一份纯函数 */
export function computePromptVariablesFallback(req: PromptVariablesRequest): PromptVariablesResult {
  return {
    variables: extractPromptVariables(req.current),
    diff: diffPromptVariables(req.current, req.baseline),
  };
}

type Pending = {
  resolve: (value: PromptVariablesResult) => void;
  reject: (reason: Error) => void;
  timer: number | null;
};

let workerInstance: Worker | null = null;
let nextId = 1;
const inflight = new Map<number, Pending>();

const WORKER_TIMEOUT_MS = 3000;

function rejectAll(reason: string): void {
  for (const pending of inflight.values()) {
    if (pending.timer !== null) window.clearTimeout(pending.timer);
    pending.reject(new Error(reason));
  }
  inflight.clear();
}

function getWorker(): Worker | null {
  if (typeof window === "undefined" || typeof Worker === "undefined") return null;
  if (workerInstance) return workerInstance;
  try {
    const w = new Worker(new URL("./promptVariables.worker.ts", import.meta.url), {
      type: "module",
    });
    w.onmessage = (event: MessageEvent<{ id: number; result: PromptVariablesResult }>) => {
      const pending = inflight.get(event.data?.id);
      if (!pending) return;
      inflight.delete(event.data.id);
      if (pending.timer !== null) window.clearTimeout(pending.timer);
      pending.resolve(event.data.result);
    };
    w.onerror = () => {
      rejectAll("compute worker failed");
      w.terminate();
      if (workerInstance === w) workerInstance = null;
    };
    workerInstance = w;
    return w;
  } catch {
    // 构建产物不支持 module Worker / 测试环境：静默降级到主线程
    return null;
  }
}

/**
 * 把纯计算送入 Web Worker；失败/超时/不支持时自动降级到主线程同步计算，
 * 保证功能不因运行环境缺失而回归。调用方必须以 signal 或 seq 丢弃过期结果
 * （输入防抖后仍可能出现乱序回包）。
 */
export function computePromptVariables(req: PromptVariablesRequest): Promise<PromptVariablesResult> {
  const w = getWorker();
  if (!w) return Promise.resolve(computePromptVariablesFallback(req));

  const id = nextId++;
  return new Promise<PromptVariablesResult>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      inflight.delete(id);
      resolve(computePromptVariablesFallback(req));
    }, WORKER_TIMEOUT_MS);
    inflight.set(id, { resolve, reject, timer });
    try {
      w.postMessage({ id, payload: req });
    } catch {
      inflight.delete(id);
      window.clearTimeout(timer);
      resolve(computePromptVariablesFallback(req));
    }
  });
}
