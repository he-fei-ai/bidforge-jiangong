/**
 * 侧栏项目树 · 跨页刷新事件总线（2026-10-17 · R91 引入）。
 *
 * 背景（浏览器实测复现的缺陷）：Layout 的 ProjectTreeMenu 只在挂载时拉取一次
 * 项目与方案，且 `loadedProjectIdsRef` 对已加载项目**永久去重**；Layout 包裹
 * 整个 Routes，应用内跳转不会重挂载该树 —— 于是新建/删除/改名项目或方案
 * （ProjectListPage / ProjectDetailPage）对侧栏完全不可见，用户必须 F5 整页
 * 刷新才能看到。两个写入入口与侧栏之间没有任何共享状态，故引入这个最小
 * pub/sub：所有写操作成功后调用 `notifySidebarTreeRefresh()`，侧栏订阅后
 * 绕过自身的去重缓存重取。
 */

export type SidebarTreeRefreshListener = (projectId?: string) => void;

const listeners = new Set<SidebarTreeRefreshListener>();

/** 订阅侧栏刷新；返回取消订阅函数（供 useEffect 清理）。 */
export function subscribeSidebarTreeRefresh(
  fn: SidebarTreeRefreshListener,
): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/**
 * 通知侧栏重取项目列表（可选带上「哪个项目的方案变了」以强制纳入重取集合）。
 * 单个订阅者异常不得影响其它订阅者 —— fail-soft，绝不向上抛。
 */
export function notifySidebarTreeRefresh(projectId?: string): void {
  for (const l of Array.from(listeners)) {
    try {
      l(projectId);
    } catch {
      // 订阅者之间互相隔离，吞掉异常
    }
  }
}

/**
 * 计算一次事件需要重取方案的 projectId 集合（纯函数，侧栏实现解耦可测）。
 * 规则：已加载过的项目 ∪ 当前展开键对应的项目 ∪ 事件目标项目。
 * 调用方据此先清空 loadedProjectIdsRef 去重缓存、再逐个 loadSchemes，
 * 否则「新建的第一个方案」永远进不了侧栏（去重缓存把它挡在门外）。
 */
export function computeRefreshTargets(
  loadedIds: Iterable<string>,
  expandedKeys: Iterable<string>,
  projectId?: string,
): string[] {
  const ids = new Set<string>(loadedIds);
  for (const k of expandedKeys) {
    const m = k.match(/^\/project\/([^/?]+)/);
    if (m) ids.add(m[1]);
  }
  if (projectId) ids.add(projectId);
  return Array.from(ids);
}

/**
 * 项目树节点点击的路由判定（纯函数）。
 * - 「进入项目新建方案 / 暂无方案」占位项曾是 disabled:true 的死占位 ——
 *   看着能点、点了没任何反应（用户报「点击新建方案时未能进入编辑状态」）。
 *   现改为进入项目详情并带 `?create=1` 直接打开新建方案弹窗（进入编辑态）。
 * - 「加载中...」占位项保持无反应。
 * - 其余 key（项目展开键 / 方案路径 / 系统菜单）原样返回交导航。
 */
export function resolveTreeClick(key: unknown): string | null {
  if (typeof key !== "string") return null;
  if (key.endsWith("/loading")) return null;
  const m = key.match(/^\/project\/([^/?]+)\/(?:create-scheme|empty)$/);
  if (m) return `/project/${m[1]}?create=1`;
  return key;
}
