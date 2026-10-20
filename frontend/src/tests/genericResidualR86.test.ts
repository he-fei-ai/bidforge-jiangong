/**
 * 目录占位/泛化标题提示（R86 · L1）—— 后端 `generic_residual` 明细的前端消费。
 *
 * 判据纪律（AGENTS §5.14 / §5.15）：
 *  · 本文件测**纯函数**（utils 层），不渲染整页 —— 快且不依赖 antd/jsdom；
 *  · 接线面另由 `wiring` 组用源码扫描锁住（纯函数被摘时该组变红）；
 *  · 每条断言都配**反向用例**（无残留 / 旧后端 / 脏数据 → 必须空串），
 *    防止「判据放宽成恒真」的假护栏。
 */
import { describe, expect, it } from "vitest";
import {
  genericAutofixNotice,
  genericResidualHits,
  genericResidualNotice,
} from "../utils/genericResidual";

describe("genericResidualHits · 明细归一", () => {
  it("透传数组", () => {
    const hits = [{ path: "1", title: "专项6", kind: "generic" }];
    expect(genericResidualHits({ generic_residual: hits })).toEqual(hits);
  });

  it.each([
    ["旧后端不带该键", {}],
    ["null", null],
    ["undefined", undefined],
    ["非数组（对象）", { generic_residual: { a: 1 } }],
    ["非数组（字符串）", { generic_residual: "x" }],
    ["空数组", { generic_residual: [] }],
  ])("%s → 空数组（绝不可抛异常，SSE/保存回调里抛会中断收尾）", (_name, data) => {
    expect(genericResidualHits(data as unknown)).toEqual([]);
  });
});

describe("genericResidualNotice · 提示文案", () => {
  it("单条残留给出可操作提示", () => {
    const text = genericResidualNotice({
      generic_residual: [{ path: "1", title: "专项6", kind: "generic" }],
    });
    expect(text).toContain("1 处占位/泛化标题");
    expect(text).toContain("专项6");
    expect(text).toContain("具体到施工对象/部位/工序");
  });

  it("多条残留逐条列出", () => {
    const text = genericResidualNotice({
      generic_residual: [
        { title: "专项6" },
        { title: "总体实施策划（2）" },
        { title: "其他" },
      ],
    });
    expect(text).toContain("3 处占位/泛化标题");
    expect(text).toContain("专项6、总体实施策划（2）、其他");
  });

  it("超过 5 条只列前 5 条并给出总数", () => {
    const hits = Array.from({ length: 8 }, (_, i) => ({ title: `占位${i + 1}` }));
    const text = genericResidualNotice({ generic_residual: hits });
    expect(text).toContain("8 处占位/泛化标题");
    expect(text).toContain("等 8 处");
    expect(text).toContain("占位5");
    expect(text).not.toContain("占位6、");
  });

  it("标题全为空时仍给出计数（不得静默返回空串而让提示消失）", () => {
    const text = genericResidualNotice({
      generic_residual: [{ title: "" }, { title: "   " }],
    });
    expect(text).toContain("2 处占位/泛化标题");
    expect(text).toContain("（空标题）");
  });

  it("conflict_suffix 形态同样出提示（AI 用括号数字消歧产生的冲突后缀）", () => {
    const text = genericResidualNotice({
      generic_residual: [
        { title: "钢内支撑安装", kind: "conflict_suffix" },
      ],
    });
    expect(text).toContain("1 处占位/泛化标题");
  });
});

describe("genericResidualNotice · 反向用例（无残留必须完全静默）", () => {
  it.each([
    ["干净目录（旧后端形态）", { ok: true, count: 12 }],
    ["显式空数组", { generic_residual: [] }],
    ["null", null],
    ["undefined", undefined],
    ["非数组", { generic_residual: "oops" }],
  ])("%s → 空串（页面据此不弹任何提示）", (_name, data) => {
    expect(genericResidualNotice(data as unknown)).toBe("");
  });
});

/**
 * ✅ R101：后端落库前**自动外科式重写**占位标题，并以加法式键 `generic_autofixed`
 * （整数，仅 >0 回传）告知改写条数。此前前端零消费 ⇒ 自动改写**静默生效**。
 * 本组锁定「消费 + 反向静默」两面。
 */
describe("genericAutofixNotice · 自动改写提示（R101）", () => {
  it(">0 时给出可核对提示", () => {
    const text = genericAutofixNotice({ generic_autofixed: 3 });
    expect(text).toContain("已自动改写 3 处");
    expect(text).toContain("只改标题，不增删章节");
    expect(text).toContain("核对");
  });

  it("纯数字字符串同样识别（序列化路径可能回字符串）", () => {
    expect(genericAutofixNotice({ generic_autofixed: "2" })).toContain("2 处");
  });

  it("小数向下取整（只报条数）", () => {
    expect(genericAutofixNotice({ generic_autofixed: 2.9 })).toContain("2 处");
  });

  it.each([
    ["无该键（旧后端 / 干净目录）", { ok: true, count: 3 }],
    ["0（未发生改写）", { generic_autofixed: 0 }],
    ["负数", { generic_autofixed: -1 }],
    ["NaN", { generic_autofixed: Number.NaN }],
    ["布尔 true（脏数据，不得当成 1 报）", { generic_autofixed: true }],
    ["非数字字符串", { generic_autofixed: "abc" }],
    ["空串", { generic_autofixed: "" }],
    ["null", null],
    ["undefined", undefined],
  ])("%s → 空串（绝不误报改写）", (_name, data) => {
    expect(genericAutofixNotice(data as unknown)).toBe("");
  });
});

describe("wiring · 页面必须消费（防纯函数被摘后仍全绿）", () => {
  const pages = import.meta.glob("../pages/*.tsx", { query: "?raw", import: "default" });

  it("SchemeWorkbenchPage 导入并调用 genericResidualNotice", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    expect(src).toContain('from "../utils/genericResidual"');
    expect(src).toContain("genericResidualNotice(");
  });

  it("SchemeWorkbenchPage 消费 genericAutofixNotice（R101：自动改写不得静默）", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    // 必须与 genericResidualNotice 在**同一出口函数**内成对接线，
    // 否则 7 个调用点只有一个能拿到提示（弱判据会漏掉这种半接线）。
    const start = src.indexOf("function notifyGenericResidual(");
    expect(start).toBeGreaterThan(-1);
    const win = src.slice(start, start + 800);
    expect(win).toContain("genericResidualNotice(data)");
    expect(win).toContain("genericAutofixNotice(data)");
  });

  it("全部目录写入消费点都接了 notifyGenericResidual", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    // 与既有 notifyClearedContent 同一形态：每个消费点都必须成对出现
    const cleared = (src.match(/notifyClearedContent\(data/g) || []).length;
    const residual = (src.match(/notifyGenericResidual\(data/g) || []).length;
    expect(cleared).toBeGreaterThan(0);
    expect(residual).toBe(cleared);
  });

  /**
   * ✅ R88 护栏升级：上面那条「计数相等」是**弱判据** —— 某个消费点若两个通知
   * **同时**缺失，计数依旧相等、护栏照绿。R88 FIX-9 修的正是这种点
   * （套用目录库 `applyAndSave` 两键都不读 ⇒ 用户静默丢正文 + 占位残留零提示），
   * 而计数锁对它是完全失明的。
   *
   * ⇒ 补**按位置**判据：锁死「套用目录库」这一具体消费点必须成对接线。
   * 判据取「调用点之后的下一个 `msg.success` / `setLibOpen` 之前」这一段窗口，
   * 避免全文件匹配。
   */
  it("套用目录库消费点成对接线（R88 FIX-9：计数锁对它完全失明）", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    const start = src.indexOf("outlineLibraryApi.applyAndSave(");
    expect(start).toBeGreaterThan(-1);
    const win = src.slice(start, start + 1200);
    expect(win).toContain("notifyClearedContent(data)");
    expect(win).toContain("notifyGenericResidual(data)");
  });

  /** 反向锁：不得靠「把两个通知写在一个哑元里」骗过上条的位置判据。 */
  it("套用目录库不得重复调用 applyAndSave（重复会二次整表重建）", async () => {
    const mods = await Promise.all(Object.entries(pages).map(([, l]) => l()));
    const src = mods.join("\n");
    expect((src.match(/outlineLibraryApi\.applyAndSave\(/g) || []).length).toBe(1);
  });
});