/**
 * 通用 TTL 缓存（纯内存级，供前端轮询/统计请求降频复用）。
 *
 * 背景（2026-09-24 · 性能优化遗留项 #2）：GET /ai/stats 后端一次返回要
 * 聚合 9 条 SQL（含两条 SELECT DISTINCT 全表扫）；前端 AIConfigPage 每次
 * 挂载/刷新/清理后都会触发。本工具提供带过期时间的模块级缓存，在 TTL
 * 窗口内复用上一次响应，显著降低后端聚合压力（数据是 30 秒级统计快照，
 * 短暂延迟可接受）。
 *
 * 设计约束：
 * - 纯内存 Map，不落 LocalStorage / Cookie：刷新页面自然失效，不跨会话
 *   残留「服务端已变化、前端显示旧数据」的脏缓存；
 * - 时钟可注入（clock 参数）：单测用假时钟验证过期，无需真实等待；
 * - key 由调用方自行组合（如 `stats:{days}`），不同参数天然隔离；
 * - TTL 传 0/负数 = 永不过期（供仅按 key 失效的场景使用）。
 */
export interface TtlHit<T> {
  hit: true;
  value: T;
}
export interface TtlMiss {
  hit: false;
}
export type TtlGetResult<T> = TtlHit<T> | TtlMiss;

export class TtlCache<T = unknown> {
  private store = new Map<string, { value: T; expiresAt: number }>();

  constructor(
    /** 过期时长（毫秒）；传 0/负数表示永不过期 */
    private ttlMs: number,
    /** 时钟注入（默认 Date.now），单测传入假时钟验证过期 */
    private clock: () => number = Date.now
  ) {}

  get(key: string): TtlGetResult<T> {
    const entry = this.store.get(key);
    if (!entry) return { hit: false };
    if (this.ttlMs > 0 && this.clock() >= entry.expiresAt) {
      return { hit: false };
    }
    return { hit: true, value: entry.value };
  }

  set(key: string, value: T): void {
    this.store.set(key, {
      value,
      expiresAt: this.clock() + Math.max(this.ttlMs, 0),
    });
  }

  /** 删除指定 key（用于「后端数据已变化」场景，如清理审计日志后必须重拉） */
  delete(key: string): void {
    this.store.delete(key);
  }

  /** 清空全部条目（同 delete，但适合「一条变更影响多个统计维度」的场景） */
  clear(): void {
    this.store.clear();
  }
}