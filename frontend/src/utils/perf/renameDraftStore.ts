import { useSyncExternalStore } from "react";

/**
 * 目录树「行内重命名」的草稿 store。
 *
 * 旧实现把每敲一键的 renamingValue 放在 9000 行工作台页面的顶层 state：
 *   setRenamingValue → 顶层整页重渲 → convertToEditTree（deps 含 renamingValue）
 *   身份变化 → 300 个节点 / ~1200 个 JSX 元素全量重建 + 每节点 O(失败章数)
 *   子串匹配。这是「输入卡顿」最短、最确定的一条链路。
 *
 * 改为：草稿只存在这个模块级 store 里，只有真正使用它的 Input 订阅；
 * 提交（blur / Enter）时才写回页面 state 并调用 renameNode。
 * 左右两棵树共享同一草稿，避免各自本地 state 造成分叉。
 */
let draft = "";
const listeners = new Set<() => void>();

export function getRenameDraft(): string {
  return draft;
}

export function setRenameDraft(next: string): void {
  if (draft === next) return;
  draft = next;
  for (const cb of listeners) cb();
}

export function subscribeRenameDraft(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useRenameDraft(): string {
  return useSyncExternalStore(subscribeRenameDraft, getRenameDraft, () => "");
}
