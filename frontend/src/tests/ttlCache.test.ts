/**
 * TtlCache 通用 TTL 缓存单测（性能优化遗留项 #2 / /ai/stats 前端 30s 缓存）。
 *
 * 锁定行为：
 * 1. TTL 窗口内命中返回缓存值（不发后端请求）；过期后 miss 允许重拉；
 * 2. delete / clear 可强制失效（清理审计日志后必须重拉统计）；
 * 3. 过期为「窗口右闭」语义：expiresAt 时刻起 cache 命中失败；
 * 4. 永不过期（ttlMs=0/负）仅在显式删除时失效；
 * 5. 不同 key 互不干扰（按 days 分桶）。
 *
 * 用假时钟注入（clock），无需真实等待。
 */
import { describe, it, expect } from "vitest";
import { TtlCache } from "../utils/ttlCache";

/** 可手动拨动的假时钟 */
function fakeClock() {
  let now = 0;
  return {
    clock: () => now,
    advance: (ms: number) => {
      now += ms;
      return now;
    },
  };
}

describe("TtlCache · AI 统计前端缓存", () => {
  it("TTL 窗口内命中返回缓存值，不触发 fetch", () => {
    const fc = fakeClock();
    const cache = new TtlCache<number>(30_000, fc.clock);
    cache.set("stats:30", 42);

    // 未过期 → 命中
    fc.advance(29_999);
    const r = cache.get("stats:30");
    expect(r.hit).toBe(true);
    if (r.hit) expect(r.value).toBe(42);

    // 恰好到达 expiresAt 时刻 → 不再命中（右闭语义）
    fc.advance(1);
    expect(cache.get("stats:30").hit).toBe(false);
  });

  it("未命中返回 miss 且不阻塞 set 覆盖", () => {
    const cache = new TtlCache<number>(30_000);
    expect(cache.get("nope").hit).toBe(false);
    cache.set("nope", 7);
    expect(cache.get("nope").hit).toBe(true);
  });

  it("delete 强制失效（后端数据变化后必须重拉）", () => {
    const cache = new TtlCache<number>(30_000);
    cache.set("stats:30", 1);
    cache.delete("stats:30");
    expect(cache.get("stats:30").hit).toBe(false);
  });

  it("clear 清空全部（按项目维度失效）", () => {
    const cache = new TtlCache<number>(30_000);
    cache.set("a", 1);
    cache.set("b", 2);
    cache.clear();
    expect(cache.get("a").hit).toBe(false);
    expect(cache.get("b").hit).toBe(false);
  });

  it("ttlMs=0 永不过期，仅显式删除生效", () => {
    const fc = fakeClock();
    const cache = new TtlCache<number>(0, fc.clock);
    cache.set("k", 9);
    fc.advance(10_000_000);
    expect(cache.get("k").hit).toBe(true);
    cache.delete("k");
    expect(cache.get("k").hit).toBe(false);
  });

  it("不同 key 互不干扰（按 days 分桶）", () => {
    const cache = new TtlCache<number>(30_000);
    cache.set("stats:1", 10);
    cache.set("stats:365", 3650);
    const a = cache.get("stats:1");
    const b = cache.get("stats:365");
    expect(a.hit && a.value).toBe(10);
    expect(b.hit && b.value).toBe(3650);
  });
});