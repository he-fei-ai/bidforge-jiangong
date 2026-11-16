/**
 * 可拖拽面板「布局持久化」单一事实源（2026-11-16 新增）。
 *
 * 职责边界（刻意收窄，避免与组件实现分叉）：
 *   - 只负责「把某个分隔组的面板比例读出来 / 写回去 / 清掉」；
 *   - 不认识 React、不认识 DOM、不做尺寸计算（那是 ResizablePanels 的事）。
 *
 * 存储格式：`swb-layout:<storageKey>` → JSON number[]（百分比，和为 100）。
 * 选百分比而不是像素的原因：面板比例必须随窗口宽度自适应——用户把窗口拉宽时，
 * 像素尺寸会把比例算歪（40% 在 1200px 下是 480px，在 1800px 下仍应是 720px）。
 * 最小/最大尺寸（px）由组件在拖拽时按当前容器尺寸换算回百分比参与钳制。
 *
 * 命名空间前缀 `swb-layout:`：与既有 `swb-split-*` 键（上一版 SplitPane 的
 * 像素记忆）隔离，避免读到旧格式数据；旧键在新版上线后自然失效，无需清理。
 */

/** localStorage 键前缀（所有可拖拽布局键共用，便于「重置布局」一键清空）。 */
export const LAYOUT_STORAGE_PREFIX = "swb-layout:";

/** 「重置全部布局」广播事件名（window 事件）。 */
export const LAYOUT_RESET_EVENT = "swb:layout-reset";

/** 读某个分隔组的布局比例；无记录 / 解析失败 / 格式非法一律返回 null（走默认布局）。 */
export function readLayout(key: string): number[] | null {
  if (!key) return null;
  try {
    const raw = window.localStorage.getItem(LAYOUT_STORAGE_PREFIX + key);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const sizes = parsed.map((n) => Number(n));
    if (sizes.some((n) => !Number.isFinite(n) || n < 0)) return null;
    // 比例之和必须为正（否则面板全部塌陷）
    if (sizes.reduce((a, b) => a + b, 0) <= 0) return null;
    return sizes;
  } catch {
    return null;
  }
}

/** 写某个分隔组的布局比例（失败静默：隐私模式 / 配额满不应阻断拖拽本身）。 */
export function writeLayout(key: string, sizes: number[]): void {
  if (!key || !Array.isArray(sizes) || sizes.length === 0) return;
  try {
    window.localStorage.setItem(LAYOUT_STORAGE_PREFIX + key, JSON.stringify(sizes));
  } catch {
    /* ignore */
  }
}

/** 删除某个分隔组的布局记录。 */
export function clearLayout(key: string): void {
  if (!key) return;
  try {
    window.localStorage.removeItem(LAYOUT_STORAGE_PREFIX + key);
  } catch {
    /* ignore */
  }
}

/** 列出全部已记忆的布局键（不含前缀）。
 * ✅ 2026-11-16 收口：同时扫本前缀与既有 `swb-split-*` 像素记忆键（旧版 SplitPane
 *  写的是裸键，不带本前缀），避免「重置布局」后拖拽记忆仍残留。 */
export function listLayoutKeys(): string[] {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const k = window.localStorage.key(i);
      if (k && (k.startsWith(LAYOUT_STORAGE_PREFIX) || k.startsWith("swb-split-"))) {
        keys.push(k);
      }
    }
    return keys;
  } catch {
    return [];
  }
}

/** 清空全部已记忆的布局，返回被清除的条数（含 `swb-split-*` 像素记忆键）。 */
export function clearAllLayouts(): number {
  const keys = listLayoutKeys();
  try {
    for (const k of keys) window.localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
  return keys.length;
}

/**
 * 广播「重置全部布局」事件。
 *
 * 组件侧（ResizableGroup）监听该事件后把面板比例复位到默认值。
 * 用事件而不是逐组回调，是为了让「重置布局」按钮与各分隔组**解耦**：
 * 按钮不必知道仓里有多少个分组，新增分组也不必改按钮。
 */
export function emitLayoutReset(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(LAYOUT_RESET_EVENT));
}
