/**
 * 章节编号合法性判据 · 前端侧（R87 FIX-1）
 *
 * 被测：`utils/outlineNumbering.storedIdParts` + `SchemeWorkbenchPage.formatOutlineTitle`
 *
 * 判据纪律：
 *  · 本文件测**纯函数**（不渲染整页）；
 *  · 接线面另由 wiring 组锁住（纯函数被摘 / 页面改回自持松过滤 ⇒ 红）；
 *  · 每条都配**反向用例**，防「判据放宽成恒真 / 恒假」。
 */
import { beforeAll, describe, expect, it } from "vitest";
import { hasRenderableStoredId, storedIdParts } from "../utils/outlineNumbering";

describe("storedIdParts · canonical 形态必须原样接受", () => {
  it.each([
    ["1", [1]],
    ["3.2", [3, 2]],
    ["3.2.4", [3, 2, 4]],
    ["3.2.4.5", [3, 2, 4, 5]],
    ["1.2.3.4.5", [1, 2, 3, 4, 5]],
  ])("%s -> %j", (id, want) => {
    expect(storedIdParts(id)).toEqual(want);
  });

  it("整数型输入被接受；float 必须被拒（后端 D-8a 只接受 int）", () => {
    expect(storedIdParts(32)).toEqual([32]);
    // 后端 `isinstance(stored_id, (str, int))` 显式拒 float：
    // float 3.5 会被拆成 ["3","5"] 造出假编号，故 D-8a 把它列为非法。
    expect(storedIdParts(3.2)).toEqual([]);
    expect(storedIdParts(3.5)).toEqual([]);
  });
});

describe("storedIdParts · 非法编号必须 fail-closed（不抢救）", () => {
  // 这批形态在 R87 之前会被「逐段 parseInt 挑数字」抢救成看似合法的序列，
  // 而后端 numbering.stored_id_parts 对同一输入返回 []。
  it.each([
    "abc1.2",   // -> 曾抢救为 [2]
    "0.3",      // -> 曾抢救为 [3]
    "1..2",     // -> 曾抢救为 [1,2]
    "1.2.x",    // -> 曾抢救为 [1,2]
    "1.0.2",    // -> 曾抢救为 [1,2]
    " 3 . 2 ",  // 段内含空格
    "",
    "第一章",
    "1-2",
    "1,2",
  ])("%j -> []（绝不编造编号）", (id) => {
    expect(storedIdParts(id)).toEqual([]);
    expect(hasRenderableStoredId(id)).toBe(false);
  });

  it.each([[null], [undefined], [{}], [[]], [true], [NaN], [Infinity]])(
    "非字符串/数字输入 %j -> []",
    (id) => {
      expect(storedIdParts(id as unknown)).toEqual([]);
    },
  );
});

describe("storedIdParts · 全角数字（后端反斜杠 d 匹配 Nd，JS 反斜杠 d 不匹配）", () => {
  // 若不覆盖全角：后端接受、前端拒绝 ⇒ **反向**分叉。
  it.each([["１２"], ["１２.３"], ["１.２.３"]])("%j -> 折成 ASCII 数字", (id) => {
    const parts = storedIdParts(id);
    expect(parts.length).toBeGreaterThan(0);
    expect(parts.every((n) => Number.isFinite(n) && n > 0)).toBe(true);
  });

  it("全角 0 号段同样拒绝", () => {
    expect(storedIdParts("０.３")).toEqual([]);
  });
});

describe("formatOutlineTitle · 接线面行为", () => {
  // 与后端编号/展示口径一致：canonical 仍渲染编号，脏编号退回裸标题。
  //
  // ⚠️ R88 加固：本块原先有 **5 处** `await import("../pages/SchemeWorkbenchPage")`
  //   —— 该页面组件 12k+ 行，每个用例各自付一次模块图 transform 成本，
  //   撞上 vitest **默认 60s** 用例超时就在并行负载下假红（R88 实测：与另一文件
  //   并跑时本文件 93s、该用例 timed out；隔离跑 8.7s 全绿）。
  //   ⇒ 改为 **一次 beforeAll 预热 + 显式超时预算**（loadMs 自证预热确实做了事）。
  let formatOutlineTitle: (id: string, level: number, title: string) => string;
  let loadMs = -1;

  beforeAll(async () => {
    const t0 = Date.now();
    ({ formatOutlineTitle } = await import("../pages/SchemeWorkbenchPage"));
    loadMs = Date.now() - t0;
  }, 120_000);

  it("模块预热确实完成（否则下面的断言是在测 undefined）", () => {
    expect(typeof formatOutlineTitle).toBe("function");
    expect(loadMs).toBeGreaterThanOrEqual(0);
  });

  it("canonical 仍渲染编号（修复不得改变正常数据）", () => {
    expect(formatOutlineTitle("1", 1, "工程概况")).toBe("第一章 工程概况");
    expect(formatOutlineTitle("1.2", 2, "降水")).toBe("2 降水");
    expect(formatOutlineTitle("1.2.3", 3, "分界")).toBe("2.3 分界");
  });

  it.each([["abc1.2"], ["0.3"], ["1.2.x"], ["1.0.2"], ["1..2"]])(
    "脏编号 %j -> 退回裸标题（不渲染编造的章号）",
    (id) => {
      expect(formatOutlineTitle(id, 2, "现场布置")).toBe("现场布置");
    },
  );

  it("空标题 / 空 id 行为不变", () => {
    expect(formatOutlineTitle("1", 1, "")).toBe("");
    expect(formatOutlineTitle("", 1, "工程概况")).toBe("工程概况");
  });
});

describe("wiring · 页面不得再自持松过滤", () => {
  const pages = import.meta.glob("../pages/*.tsx", {
    query: "?raw",
    import: "default",
  });

  it("页面消费 utils 的合法性判据", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    expect(src).toContain('from "../utils/outlineNumbering"');
    expect(src).toContain("storedIdParts(");
  });

  it("旧的「逐段 parseInt 挑数字」松过滤不得回流", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    // 该形态即 R86 从后端 heading_templates 删掉的那份；页面侧同步清除
    expect(src).not.toMatch(/\.map\(\s*\(p\)\s*=>\s*parseInt\(p,\s*10\)\s*\)/);
    expect(src).not.toMatch(/\.filter\(\s*\(n\)\s*=>\s*!Number\.isNaN\(n\)\s*\)/);
  });
});
