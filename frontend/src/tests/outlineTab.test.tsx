// @vitest-environment jsdom
/**
 * 「目录生成」Tab · 组件级交互测试（2026-09-20 补齐）。
 *
 * 此前 outline Tab 全部内联在 8000 行工作台页面里、零组件级测试覆盖。
 * 本文件锁定两个已拆出组件的行为：
 *
 * OutlineGateList（一级目录确认闸门，对齐 OpenBidKit outline-selection）：
 *   1. 默认全选所有一级章节，勾选结果同步到 selectedRef（modal.confirm onOk 读取，
 *      避免闭包陈旧值）；
 *   2. 取消勾选 / 重新勾选实时同步 ref；
 *   3. 空标题回退「（未命名第 n 章）」。
 *
 * GenerationProgressCard（生成进度卡）：
 *   4. 目录分步链路（stats.stepwise）：「已完成 x/y 章」、节点数、失败数取后端权威值；
 *   5. 目录实时日志：分步才有章节维度（total>0 才渲染 x/y 章，防历史日志「0/0 章」）；
 *   6. 正文模式：运行中章节标签、已生成字数、并发档位。
 *
 * 目录树纯逻辑（模块级导出，2026-09-20 补齐）：
 *   7. hasUnsavedLocalNodes：嵌套 local_ 新增节点识别（根层检测 BUG 回归锁定）；
 *   8. moveSiblingInTree / flattenTreeKeys / hasDeepOutlineNodes / renumberTreeLocally。
 *
 * OutlineTreeActions（目录树操作区，2026-09-20 拆出）：
 *   9. 未选中禁用口径 / 选中可移动上抛 dir / 同层首尾禁用 / 下一步显隐。
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import {
  OutlineGateList,
  GenerationProgressCard,
  OutlineTreeActions,
  OutlineNodeBudgetPanel,
  hasUnsavedLocalNodes,
  isUnsavedLocalKey,
  UNSAVED_KEY_PREFIXES,
  moveSiblingInTree,
  flattenTreeKeys,
  hasDeepOutlineNodes,
  renumberTreeLocally,
  treeToOutline,
  renumberOutline,
  outlineToTreeNode,
  buildPartialOutlineHint,
  formatOutlineTitle,
  WORD_BUDGET_MIN,
  WORD_BUDGET_MAX,
} from "../pages/SchemeWorkbenchPage";
import type { GenerationProgressProps, TreeNode } from "../pages/SchemeWorkbenchPage";

// vitest 未开 globals，RTL 自动清理不生效 —— 显式清理，避免跨用例 DOM 累积
afterEach(cleanup);

// jsdom 缺 matchMedia / ResizeObserver，antd（Tooltip / Progress）会用到
if (!(window as any).matchMedia) {
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
if (!(globalThis as any).ResizeObserver) {
  (globalThis as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ============================================================
// OutlineGateList · 一级目录确认闸门
// ============================================================
const GATE_OUTLINE = [
  { title: "第一章 工程概况", description: "工程总体说明" },
  { title: "第二章 施工部署", description: "" },
  { title: "", description: "AI 未给标题" },
];

function setupGate() {
  const selectedRef: { current: number[] } = { current: [0, 1, 2] };
  const utils = render(
    <OutlineGateList outline={GATE_OUTLINE} selectedRef={selectedRef} />,
  );
  const checkboxes = () =>
    Array.from(utils.container.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[];
  return { ...utils, selectedRef, checkboxes };
}

describe("OutlineGateList（一级目录确认闸门）", () => {
  it("渲染全部一级章节且默认全选，ref 初始化为全量下标", () => {
    const { checkboxes, selectedRef, getByText } = setupGate();
    expect(checkboxes().length).toBe(3);
    expect(checkboxes().every((c) => c.checked)).toBe(true);
    expect(selectedRef.current).toEqual([0, 1, 2]);
    expect(getByText("第一章 工程概况")).toBeTruthy();
  });

  it("取消勾选实时同步 selectedRef（onOk 读取的权威来源）", () => {
    const { checkboxes, selectedRef } = setupGate();
    fireEvent.click(checkboxes()[1]); // 取消第二章
    expect(checkboxes()[1].checked).toBe(false);
    expect(selectedRef.current).toEqual([0, 2]);
    fireEvent.click(checkboxes()[2]); // 再取消空标题章
    expect(selectedRef.current).toEqual([0]);
  });

  it("重新勾选恢复对应下标（追加语义，顺序无关：onOk 按 outline 顺序 filter）", () => {
    const { checkboxes, selectedRef } = setupGate();
    fireEvent.click(checkboxes()[0]);
    expect(selectedRef.current).toEqual([1, 2]);
    fireEvent.click(checkboxes()[0]);
    expect([...selectedRef.current].sort()).toEqual([0, 1, 2]);
  });

  it("空标题回退「（未命名第 n 章）」", () => {
    const { getByText } = setupGate();
    expect(getByText("（未命名第 3 章）")).toBeTruthy();
  });

  it("描述非空时展示（截断 60 字符内原样）", () => {
    const { getByText, queryByText } = render(
      <OutlineGateList
        outline={[{ title: "A", description: "desc-A" }, { title: "B", description: "" }]}
        selectedRef={{ current: [0, 1] }}
      />,
    );
    expect(getByText("desc-A")).toBeTruthy();
    expect(queryByText("desc-B")).toBeNull();
  });
});

// ============================================================
// GenerationProgressCard · 生成进度卡
// ============================================================
function baseProgressProps(over: Partial<GenerationProgressProps> = {}): GenerationProgressProps {
  return {
    genType: "outline",
    progress: 0.42,
    progressMsg: "第3章子目录完成",
    sectionLogs: [],
    outlinePhase: "生成二三级目录",
    outlineDoneTotal: null,
    outlineLogs: [],
    stats: {},
    ...over,
  } as GenerationProgressProps;
}

describe("GenerationProgressCard（生成进度卡）", () => {
  it("目录模式：标题与阶段标签", () => {
    const { getByText } = render(<GenerationProgressCard {...baseProgressProps()} />);
    expect(getByText("📂 正在生成目录")).toBeTruthy();
    expect(getByText("生成二三级目录")).toBeTruthy();
  });

  it("目录分步链路：「已完成 x/y 章」+ 节点数 + 失败数取后端权威值", () => {
    const { getByText } = render(
      <GenerationProgressCard
        {...baseProgressProps({
          stats: { stepwise: true, done: 3, total: 10, nodes: 42, failed: 2 },
        })}
      />,
    );
    expect(getByText("已完成 3/10 章")).toBeTruthy();
    expect(getByText("目录 42 个节点")).toBeTruthy();
    expect(getByText("失败 2")).toBeTruthy();
  });

  it("目录失败数取「后端 stats / 本地日志」较大值", () => {
    const { getByText } = render(
      <GenerationProgressCard
        {...baseProgressProps({ stats: { failed: 3 } })}
      />,
    );
    expect(getByText("失败 3")).toBeTruthy();
  });

  it("目录实时日志：分步日志带 x/y 章，历史日志（无 total）不带", () => {
    const { getByText, queryByText } = render(
      <GenerationProgressCard
        {...baseProgressProps({
          outlineLogs: [
            { progress: 0.3, message: "第1章子目录完成", done: 1, total: 3, time: 1 },
            { progress: 0.1, message: "AI 生成一级目录", time: 2 },
          ],
        })}
      />,
    );
    expect(getByText("第1章子目录完成")).toBeTruthy();
    expect(getByText("1/3 章")).toBeTruthy();
    expect(queryByText("0/0 章")).toBeNull();
  });

  it("性能优化：日志按时间倒序渲染（最新一条在最上方）", () => {
    const { container } = render(
      <GenerationProgressCard
        {...baseProgressProps({
          outlineLogs: [
            { progress: 0.1, message: "第一条日志", time: 1 },
            { progress: 0.5, message: "第二条日志", time: 2 },
            { progress: 0.9, message: "第三条日志", time: 3 },
          ],
        })}
      />,
    );
    const rendered = Array.from(container.querySelectorAll(".ant-list-item"))
      .map((el) => el.textContent || "");
    // 断言是「顺序」而非仅存在：倒序改造不能把最新一条压到滚动区底部
    expect(rendered[0]).toContain("第三条日志");
    expect(rendered[1]).toContain("第二条日志");
    expect(rendered[2]).toContain("第一条日志");
  });

  it("性能优化：日志渲染行数封顶，超量历史不再全部渲染为 DOM", () => {
    const many = Array.from({ length: 260 }, (_unused, i) => ({
      progress: i / 260,
      message: `日志-${i}`,
      time: i,
    }));
    const { container, queryByText } = render(
      <GenerationProgressCard {...baseProgressProps({ outlineLogs: many })} />,
    );
    const rows = container.querySelectorAll(".ant-list-item");
    // 渲染行数封顶 200（长方案分步生成时避免上千行 DOM 拖垮滚动）
    expect(rows.length).toBe(200);
    // 保留的是「最近」的 200 条：最早的第 0 条已被截断 …
    expect(queryByText("日志-0")).toBeNull();
    // … 而最新一条必须在
    expect(queryByText("日志-259")).toBeTruthy();
  });

  it("正文模式：运行中章节、已生成字数、并发档位", () => {
    const { getByText } = render(
      <GenerationProgressCard
        {...baseProgressProps({
          genType: "content",
          outlinePhase: "",
          stats: { words: 1234, concurrency: 3 },
          sectionLogs: [
            {
              section_id: "s1", title: "第一章 工程概况", status: "running",
              stage: "draft", stage_label: "起草中", time: 1,
            },
          ],
        })}
      />,
    );
    expect(getByText("📝 正在生成正文")).toBeTruthy();
    expect(getByText("第一章 工程概况 · 起草中")).toBeTruthy();
    expect(getByText("已生成 1,234 字")).toBeTruthy();
    expect(getByText("并发 3")).toBeTruthy();
  });

  it("进度异常态：失败且 progress>=1 时进度条标红（status=exception）", () => {
    const { container } = render(
      <GenerationProgressCard
        {...baseProgressProps({ progress: 1, stats: { failed: 5 } })}
      />,
    );
    expect(container.querySelector(".ant-progress-status-exception")).toBeTruthy();
  });
});

// ============================================================
// 目录树纯逻辑（模块级导出，2026-09-20 补齐）
// ============================================================
function mkNode(key: string, title: string, children: TreeNode[] = []): TreeNode {
  return {
    key, title, level: 1, status: "empty", word_count: 0,
    word_budget: 1500, children,
  };
}

describe("目录树纯逻辑（模块级导出）", () => {
  it("hasUnsavedLocalNodes：嵌套在二/三级下的 local_ 新增节点必须被识别（根层检测回归）", () => {
    // ✅ BUG 回归锁定：旧实现 tree.some((n) => n.key.startsWith("local_")) 只查根层，
    //    嵌套新增会被 moveNode 的 /reorder 立即持久化动作弄丢
    const nested = [
      mkNode("1", "工程概况"),
      mkNode("2", "施工部署", [mkNode("2.1", "部署", [mkNode("local_x", "新章节")])]),
    ];
    expect(hasUnsavedLocalNodes(nested)).toBe(true);
    expect(hasUnsavedLocalNodes([mkNode("local_x", "新章节")])).toBe(true);
    expect(hasUnsavedLocalNodes([mkNode("1", "A"), mkNode("2", "B")])).toBe(false);
    expect(hasUnsavedLocalNodes([])).toBe(false);
  });

  it("moveSiblingInTree：同级交换、嵌套层交换、首/尾返回 null、找不到返回 null", () => {
    const tree = [
      mkNode("1", "A"),
      mkNode("2", "B", [mkNode("2.1", "B1"), mkNode("2.2", "B2")]),
      mkNode("3", "C"),
    ];
    // 根层上移 B → [B, A, C]
    const up = moveSiblingInTree(tree, "2", -1)!;
    expect(up.map((n) => n.key)).toEqual(["2", "1", "3"]);
    // 嵌套层交换 B1/B2，不动父层
    const nested = moveSiblingInTree(tree, "2.2", -1)!;
    expect(nested[1].children!.map((n) => n.key)).toEqual(["2.2", "2.1"]);
    expect(nested.map((n) => n.key)).toEqual(["1", "2", "3"]); // 根层不变
    // 边界：同层首/尾
    expect(moveSiblingInTree(tree, "1", -1)).toBeNull();
    expect(moveSiblingInTree(tree, "3", 1)).toBeNull();
    expect(moveSiblingInTree(tree, "nope", 1)).toBeNull();
  });

  it("flattenTreeKeys：深度优先收集全部 key（/reorder 全量重排的 order 来源）", () => {
    const tree = [
      mkNode("1", "A", [mkNode("1.1", "A1")]),
      mkNode("2", "B"),
    ];
    expect(flattenTreeKeys(tree)).toEqual(["1", "1.1", "2"]);
  });

  it("hasDeepOutlineNodes：三级为上限，四级及以上命中", () => {
    const three = [mkNode("1", "A", [mkNode("1.1", "B", [mkNode("1.1.1", "C")])])];
    const four = [mkNode("1", "A", [mkNode("1.1", "B", [mkNode("1.1.1", "C", [mkNode("1.1.1.1", "D")])])])];
    expect(hasDeepOutlineNodes(three)).toBe(false);
    expect(hasDeepOutlineNodes(four)).toBe(true);
  });

  it("renumberTreeLocally：按位置重算 level 与点分编号，不动 key", () => {
    const tree = [mkNode("x", "A", [mkNode("y", "B")]), mkNode("z", "C")];
    const out = renumberTreeLocally(tree);
    expect(out[0].outlineId).toBe("1");
    expect(out[0].level).toBe(1);
    expect(out[0].children![0].outlineId).toBe("1.1");
    expect(out[0].children![0].level).toBe(2);
    expect(out[1].outlineId).toBe("2");
    expect(out.map((n) => n.key)).toEqual(["x", "z"]);
  });
});

// ============================================================
// OutlineTreeActions · 目录树卡片右上操作区（2026-09-20 拆出）
// ============================================================
function setupActions(over: Partial<Parameters<typeof OutlineTreeActions>[0]> = {}) {
  const props: Parameters<typeof OutlineTreeActions>[0] = {
    selected: null,
    moveFlags: {},
    showNext: true,
    onAddChild: vi.fn(),
    onMove: vi.fn(),
    onDelete: vi.fn(),
    onNextStep: vi.fn(),
    ...over,
  };
  const utils = render(<OutlineTreeActions {...props} />);
  const btnByText = (text: string) => {
    // 中文 Button 文本含空格，须空白归一
    const norm = (s: string) => (s || "").replace(/\s+/g, "");
    return Array.from(utils.container.querySelectorAll("button")).find(
      (b) => norm(b.textContent || "").includes(norm(text)),
    ) as HTMLButtonElement;
  };
  return { ...utils, props, btnByText };
}

describe("OutlineTreeActions（目录树操作区）", () => {
  it("未选中章节：新增子章节/上移/下移/删除全部禁用，点击不上抛", () => {
    const { btnByText, props } = setupActions();
    for (const label of ["新增子章节", "上移", "下移", "删除"]) {
      const btn = btnByText(label);
      expect(btn.disabled).toBe(true);
      fireEvent.click(btn);
    }
    expect(props.onAddChild).not.toHaveBeenCalled();
    expect(props.onMove).not.toHaveBeenCalled();
    expect(props.onDelete).not.toHaveBeenCalled();
  });

  it("选中章节且可移动：四个按钮启用，上移/下移分别上抛 dir=-1/+1", () => {
    const sel = mkNode("2", "B");
    const { btnByText, props } = setupActions({
      selected: sel,
      moveFlags: { "2": { up: true, down: true } },
    });
    for (const label of ["新增子章节", "上移", "下移", "删除"]) {
      expect(btnByText(label).disabled).toBe(false);
    }
    fireEvent.click(btnByText("上移"));
    expect(props.onMove).toHaveBeenCalledWith(-1);
    fireEvent.click(btnByText("下移"));
    expect(props.onMove).toHaveBeenCalledWith(1);
    fireEvent.click(btnByText("新增子章节"));
    expect(props.onAddChild).toHaveBeenCalledTimes(1);
    fireEvent.click(btnByText("删除"));
    expect(props.onDelete).toHaveBeenCalledTimes(1);
  });

  it("同层首/尾：moveFlags 禁用对应方向的移动按钮", () => {
    const sel = mkNode("1", "A");
    const { btnByText } = setupActions({
      selected: sel,
      moveFlags: { "1": { up: false, down: true } },
    });
    expect(btnByText("上移").disabled).toBe(true);
    expect(btnByText("下移").disabled).toBe(false);
  });

  it("下一步按钮：showNext 控制显隐，点击上抛 onNextStep", () => {
    const shown = setupActions();
    expect(shown.btnByText("下一步")).toBeTruthy();
    fireEvent.click(shown.btnByText("下一步"));
    expect(shown.props.onNextStep).toHaveBeenCalledTimes(1);
    const hidden = setupActions({ showNext: false });
    expect(hidden.btnByText("下一步")).toBeUndefined();
  });
});

// ============================================================
// 模块导出完整性 + B11 接线回归锁（防 swap-revert 回退）
// ============================================================
// ✅ 背景（2026-09-20 第七轮）：第六轮的「可测性重构 + B11 修复」曾被
//    .bak-preswap 文件恢复整体回退——flattenTreeKeys/moveSiblingInTree/
//    hasDeepOutlineNodes/renumberTreeLocally 退回组件内闭包（未导出）、
//    hasUnsavedLocalNodes/OutlineTreeActions 完全丢失，导致本测试文件 import
//    崩溃、20 项测试静默不运行（前端从 289 通过掉到 283 通过 | 9 失败）。
//    以下断言钉住「导出存在」，防同类事故（swap-revert 回退的信号即导出变 undefined）。
//    B11 接线（全树遍历）由上方 hasUnsavedLocalNodes 行为测试覆盖，无需源码级检查。
describe("模块导出完整性回归锁（防 swap-revert 回退）", () => {
  it("模块级导出均已定义（import 不再静默失败）", () => {
    // memo 包裹的组件 typeof 为 'object'，纯函数为 'function'——关注点是
    // 「导出存在（非 undefined）」而非具体类型，这恰是 swap-revert 回退的信号。
    expect(OutlineGateList).toBeDefined();
    expect(GenerationProgressCard).toBeDefined();
    expect(OutlineTreeActions).toBeDefined();
    expect(OutlineNodeBudgetPanel).toBeDefined();
    expect(hasUnsavedLocalNodes).toBeDefined();
    expect(isUnsavedLocalKey).toBeDefined();
    expect(UNSAVED_KEY_PREFIXES).toBeDefined();
    expect(moveSiblingInTree).toBeDefined();
    expect(flattenTreeKeys).toBeDefined();
    expect(hasDeepOutlineNodes).toBeDefined();
    expect(renumberTreeLocally).toBeDefined();
    // 2026-09-21 新增导出：可测性重构（组件内闭包 → 模块级）+ 功能缺口补齐
    expect(treeToOutline).toBeDefined();
    expect(renumberOutline).toBeDefined();
    expect(outlineToTreeNode).toBeDefined();
    expect(buildPartialOutlineHint).toBeDefined();
    expect(WORD_BUDGET_MIN).toBeGreaterThan(0);
    expect(WORD_BUDGET_MAX).toBeGreaterThanOrEqual(WORD_BUDGET_MIN);
  });
});


// ============================================================
// 未落库本地节点判定（isUnsavedLocalKey / hasUnsavedLocalNodes）
// ============================================================
// ✅ BUG 根因回归锁（2026-09-21）：旧实现只认 `local_` 一种前缀，
// 「导入目录（智能识别）」替换本地树产生的 `upload_` 节点被误判为「已落库章节」，
// 导致 deleteNode 404（本地节点删不掉）、renameNode 404 被 .catch 吞掉、
// moveNode 误走「已同步到服务端」假成功分支。以下用例钉住两类前缀同口径。
describe("isUnsavedLocalKey（未落库临时节点判定，单一口径）", () => {
  it("local_（手工新增）与 upload_（导入识别替换）都识别为未落库", () => {
    expect(isUnsavedLocalKey("local_1712345678901_ab12c")).toBe(true);
    expect(isUnsavedLocalKey("upload_1712345678902_3")).toBe(true);
  });

  it("后端章节主键（UUID / 任意字符串）都不是未落库节点", () => {
    expect(isUnsavedLocalKey("3f2c9a1e-7b4d-4a6f-9c0e-1d2b3a4c5d6e")).toBe(false);
    expect(isUnsavedLocalKey("第一章 工程概况")).toBe(false);
  });

  it("空值与脏数据防御（不抛异常）", () => {
    expect(isUnsavedLocalKey("")).toBe(false);
    expect(isUnsavedLocalKey(undefined as unknown as string)).toBe(false);
    expect(isUnsavedLocalKey(123 as unknown as string)).toBe(false);
  });

  it("前缀全集是模块级常量，避免各调用点各写一份字面量", () => {
    expect([...UNSAVED_KEY_PREFIXES].sort()).toEqual(["local_", "upload_"]);
  });
});

describe("hasUnsavedLocalNodes（全树递归，覆盖 upload_ 前缀回归）", () => {
  const mk = (key: string, children: TreeNode[] = []): TreeNode => ({
    key,
    title: key,
    level: 1,
    status: "empty",
    word_count: 0,
    word_budget: 1500,
    children,
  });

  it("upload_ 节点在任意层级都命中（导入识别替换后的整棵树）", () => {
    const tree = [mk("upload_1_0", [mk("upload_1_1", [mk("upload_1_2")])])];
    expect(hasUnsavedLocalNodes(tree)).toBe(true);
  });

  it("仅嵌套在三级的 upload_ 节点也命中（根层检测漏检的回归点）", () => {
    const nestedOnly = [mk("uuid-root", [mk("uuid-mid", [mk("upload_2_0")])])];
    expect(hasUnsavedLocalNodes(nestedOnly)).toBe(true);
  });

  it("纯后端章节（UUID key）不命中；空树不命中", () => {
    expect(hasUnsavedLocalNodes([mk("uuid-1", [mk("uuid-2")])])).toBe(false);
    expect(hasUnsavedLocalNodes([])).toBe(false);
    expect(hasUnsavedLocalNodes(undefined as unknown as TreeNode[])).toBe(false);
  });
});



// ============================================================
// 目录树 ↔ outline 结构互转（可测性重构：组件内闭包 → 模块级导出）
// ============================================================
describe("treeToOutline（本地树 → 提交结构）", () => {
  it("id 直接取节点 key（DB 主键 / 临时前缀 key 都原样保留），按位置写 sort_order", () => {
    const tree: TreeNode[] = [
      {
        key: "local_1_x",
        title: "一",
        level: 1,
        status: "empty",
        word_count: 0,
        word_budget: 800,
        description: "描述一",
        children: [
          {
            key: "uuid-child",
            title: "一.1",
            level: 2,
            status: "empty",
            word_count: 120,
            word_budget: 600,
            children: [],
          },
        ],
      },
      {
        key: "upload_2_y",
        title: "二",
        level: 1,
        status: "empty",
        word_count: 0,
        word_budget: 1500,
        children: [],
      },
    ];
    const out = treeToOutline(tree);
    expect(out.map((n) => n.id)).toEqual(["local_1_x", "upload_2_y"]);
    expect(out.map((n) => n.sort_order)).toEqual([0, 1]);
    expect(out[0]).toMatchObject({
      title: "一",
      level: 1,
      word_budget: 800,
      description: "描述一",
    });
    expect(out[0].children).toHaveLength(1);
    expect(out[0].children[0]).toMatchObject({
      id: "uuid-child",
      title: "一.1",
      level: 2,
      sort_order: 0,
      word_budget: 600,
      description: "",
    });
    // 空 children 一律序列化为 []（后端 normalize_outline 依赖该约定）
    expect(out[1].children).toEqual([]);
  });

  it("round-trip：outlineToTreeNode(treeToOutline(tree)) 保留 key/level/budget", () => {
    const tree: TreeNode[] = [
      {
        key: "uuid-1",
        title: "工程概况",
        level: 1,
        status: "generated",
        word_count: 880,
        word_budget: 1200,
        description: "总体说明",
        children: [
          {
            key: "uuid-2",
            title: "建设规模",
            level: 2,
            status: "empty",
            word_count: 0,
            word_budget: 500,
            children: [],
          },
        ],
      },
    ];
    const back = outlineToTreeNode(treeToOutline(tree));
    expect(back.map((n) => n.key)).toEqual(["uuid-1"]);
    expect(back[0].children![0].key).toBe("uuid-2");
    expect(back[0].word_budget).toBe(1200);
    expect(back[0].children![0].word_budget).toBe(500);
    // status/word_count 在 outline 结构中不携带，回退初始态（与导入识别链路一致）
    expect(back[0].status).toBe("empty");
    expect(back[0].word_count).toBe(0);
    // 编号由位置重新推导
    expect(back[0].outlineId).toBe("1");
    expect(back[0].children![0].outlineId).toBe("1.1");
  });
});

describe("renumberOutline（按位置重排展示编号）", () => {
  it("生成点分路径 outlineId，且**不改 id**（id 是 DB 主键载体，供后端智能保留正文）", () => {
    const outline = [
      {
        id: "uuid-a",
        title: "A",
        level: 1,
        children: [
          { id: "uuid-a1", title: "A1", level: 2, children: [] },
          { id: "uuid-a2", title: "A2", level: 2, children: [] },
        ],
      },
      { id: "uuid-b", title: "B", level: 1, children: [] },
    ];
    const out = renumberOutline(outline);
    expect(out.map((n) => n.outlineId)).toEqual(["1", "2"]);
    expect(out.map((n) => n.sort_order)).toEqual([0, 1]);
    expect(out[0].children!.map((n: any) => n.outlineId)).toEqual(["1.1", "1.2"]);
    expect(out[0].children![1].sort_order).toBe(1);
    // ✅ id 必须原样保留：被改写会让后端 __original_id 判 is_new 全为 True
    //    → 已有章节整表重建、已生成正文全部丢失
    expect(out.map((n) => n.id)).toEqual(["uuid-a", "uuid-b"]);
    expect(out[0].children!.map((n: any) => n.id)).toEqual(["uuid-a1", "uuid-a2"]);
  });

  it("空数组安全返回空数组；子节点缺失时补 []", () => {
    expect(renumberOutline([])).toEqual([]);
    const out = renumberOutline([{ id: "x", title: "X" }]);
    expect(out[0].children).toEqual([]);
    expect(out[0].outlineId).toBe("1");
  });
});

describe("outlineToTreeNode（导入识别结果 → 本地临时树）", () => {
  it("编号由位置推导（不直接采信识别结果的 id），key 用 upload_ 前缀标记未落库", () => {
    const outline = [
      {
        id: "n1",
        title: "工程概况",
        level: 1,
        description: "总体说明",
        children: [{ id: "n1-1", title: "建设规模" }],
      },
      { title: "施工部署" },
    ];
    const tree = outlineToTreeNode(outline);
    expect(tree).toHaveLength(2);
    // outlineId 必须来自位置（"1"/"1.1"/"2"），而不是 "n1"/"n1-1"
    expect(tree[0].outlineId).toBe("1");
    expect(tree[0].children![0].outlineId).toBe("1.1");
    expect(tree[1].outlineId).toBe("2");
    // level 缺省按父级 +1
    expect(tree[0].level).toBe(1);
    expect(tree[0].children![0].level).toBe(2);
    // 无 id 的节点回退到 upload_ 前缀（必须被 isUnsavedLocalKey 识别）
    expect(tree[1].key.startsWith("upload_")).toBe(true);
    expect(isUnsavedLocalKey(tree[1].key)).toBe(true);
    // 有 id 的节点保留 id 作为 key（保存后由后端按 __original_id 匹配）
    expect(tree[0].key).toBe("n1");
    // 默认预算 1500、初始态；description 不丢
    expect(tree[1].word_budget).toBe(1500);
    expect(tree[1].status).toBe("empty");
    expect(tree[1].word_count).toBe(0);
    expect(tree[0].description).toBe("总体说明");
    expect(tree[1].description).toBe("");
  });

  it("识别结果携带 word_budget 时透传", () => {
    const tree = outlineToTreeNode([{ title: "A", word_budget: 3200 }]);
    expect(tree[0].word_budget).toBe(3200);
  });

  it("children 非数组（畸形识别结果）时回退空数组；空输入安全", () => {
    const tree = outlineToTreeNode([{ title: "A", children: "bad" }]);
    expect(tree[0].children).toEqual([]);
    expect(outlineToTreeNode([])).toEqual([]);
    expect(outlineToTreeNode(undefined as unknown as any)).toEqual([]);
  });
});



// ============================================================
// 断线重挂「部分成果」弹窗文案（buildPartialOutlineHint）
// ============================================================
// ✅ 口径补齐回归锁（2026-09-21）：旧实现 failed / stopped 两个消费分支
// 各自手拼文案，且都不告诉用户**为什么中断**（checkpoint 的 event/partial
// 字段此前被后端白名单过滤掉，前端无从消费）。现收口为纯函数。
describe("buildPartialOutlineHint（部分成果弹窗文案）", () => {
  it("event=stopped：标题/基调为「已停止 · info」，文案说明可继续使用", () => {
    const h = buildPartialOutlineHint({ event: "stopped", nodeCount: 12 });
    expect(h.title).toBe("目录生成已停止，但有部分成果");
    expect(h.tone).toBe("info");
    expect(h.content).toContain("12 个章节节点");
    expect(h.content).toContain("用户主动停止或连接中断");
    expect(h.okText).toBe("保存已生成部分");
    expect(h.cancelText).toBe("不保存");
  });

  it("event=error：标题/基调为「中断 · warning」，文案提示成果可能不完整", () => {
    const h = buildPartialOutlineHint({ event: "error", nodeCount: 5 });
    expect(h.title).toBe("目录生成中断，但有部分成果");
    expect(h.tone).toBe("warning");
    expect(h.content).toContain("AI 服务异常中断");
    expect(h.content).toContain("可能不完整");
  });

  it("未带 event 的旧 checkpoint 也安全回退为「已停止」口径（不崩、不误导）", () => {
    expect(buildPartialOutlineHint({ nodeCount: 3 }).tone).toBe("info");
    expect(buildPartialOutlineHint({ event: "", nodeCount: 3 }).tone).toBe("info");
    expect(buildPartialOutlineHint({ event: "completed", nodeCount: 3 }).tone).toBe("info");
  });

  it("nodeCount 脏数据收敛到 0（不出现负数或 NaN）", () => {
    expect(buildPartialOutlineHint({ nodeCount: -4 }).content).toContain("0 个章节节点");
    expect(buildPartialOutlineHint({ nodeCount: NaN }).content).toContain("0 个章节节点");
    expect(buildPartialOutlineHint({ nodeCount: undefined }).content).toContain("0 个章节节点");
  });
});



// ============================================================
// OutlineNodeBudgetPanel · 选中章节字数预算编辑（2026-09-21 功能缺口补齐）
// ============================================================
// 旧实现字数预算全链路**只读**（目录树行内 `(L1 · 1500字)`、正文页 `x字/y字`），
// 用户无法在正文生成前按需规划每章篇幅；现提供编辑入口并锁定其校验口径。
describe("OutlineNodeBudgetPanel（字数预算编辑面板）", () => {
  const mkNode = (over: Partial<TreeNode> = {}): TreeNode => ({
    key: "sec-1",
    title: "施工准备",
    level: 1,
    status: "empty",
    word_count: 0,
    word_budget: 1500,
    outlineId: "1",
    children: [],
    ...over,
  });

  function setupBudget(node: TreeNode = mkNode(), onBudgetChange = vi.fn()) {
    const utils = render(
      <OutlineNodeBudgetPanel node={node} onBudgetChange={onBudgetChange} />,
    );
    const input = () =>
      utils.container.querySelector('input[aria-label="字数预算"]') as HTMLInputElement;
    return { ...utils, onBudgetChange, input };
  }

  it("渲染章节名（带编号）、层级、当前字数与初始预算值", () => {
    const { getByText, input } = setupBudget(mkNode());
    // 编号由 formatOutlineTitle 生成：L1 → 「第一章 施工准备」
    expect(getByText(formatOutlineTitle("1", 1, "施工准备"))).toBeTruthy();
    expect(getByText("L1")).toBeTruthy();
    expect(input().value).toBe("1500");
  });

  it("修改并失焦 → 上抛 onBudgetChange(key, budget)", () => {
    const { input, onBudgetChange } = setupBudget();
    fireEvent.change(input(), { target: { value: "2200" } });
    fireEvent.blur(input());
    expect(onBudgetChange).toHaveBeenCalledTimes(1);
    expect(onBudgetChange).toHaveBeenCalledWith("sec-1", 2200);
    expect(input().value).toBe("2200");
  });

  it("回车提交（不等失焦）", () => {
    const { input, onBudgetChange } = setupBudget();
    fireEvent.change(input(), { target: { value: "900" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onBudgetChange).toHaveBeenCalledWith("sec-1", 900);
  });

  it("低于下限 → 收敛到 WORD_BUDGET_MIN；高于上限 → 收敛到 WORD_BUDGET_MAX", () => {
    const low = setupBudget();
    fireEvent.change(low.input(), { target: { value: "10" } });
    fireEvent.blur(low.input());
    expect(low.onBudgetChange).toHaveBeenCalledTimes(1);
    expect(low.onBudgetChange).toHaveBeenCalledWith("sec-1", WORD_BUDGET_MIN);
    expect(low.input().value).toBe(String(WORD_BUDGET_MIN));

    const high = setupBudget();
    fireEvent.change(high.input(), { target: { value: "999999" } });
    fireEvent.blur(high.input());
    expect(high.onBudgetChange).toHaveBeenCalledTimes(1);
    expect(high.onBudgetChange).toHaveBeenCalledWith("sec-1", WORD_BUDGET_MAX);
    expect(high.input().value).toBe(String(WORD_BUDGET_MAX));
  });

  it("空值 / 0 / 负数 → 回退原值，**不**触发回调（防脏数据写库）", () => {
    for (const bad of ["", "0", "-200"]) {
      const { input, onBudgetChange } = setupBudget();
      fireEvent.change(input(), { target: { value: bad } });
      fireEvent.blur(input());
      expect(onBudgetChange, `value=${bad} 不应触发回调`).not.toHaveBeenCalled();
      expect(input().value).toBe("1500");
    }
  });

  it("值未变化 → 不触发回调（避免无谓写库）", () => {
    const { input, onBudgetChange } = setupBudget();
    fireEvent.change(input(), { target: { value: "1500" } });
    fireEvent.blur(input());
    expect(onBudgetChange).not.toHaveBeenCalled();
  });

  it("超出预算时给出「超预算」提示；未超预算给出目标篇幅提示", () => {
    // ⚠️ RTL 的 getByText/queryByText 默认搜索整个 document.body（baseElement），
    // 同一测试里先后挂载的 over / under 面板会互相命中 —— 改为按 container 断言。
    const over = setupBudget(mkNode({ word_count: 3000, word_budget: 1500 }));
    expect(over.container.textContent).toContain("超预算，可在正文页压缩");

    const under = setupBudget(mkNode({ word_count: 200, word_budget: 1500 }));
    expect(under.container.textContent).toContain("生成正文时按此目标控制篇幅");
    expect(under.container.textContent).not.toContain("超预算");
  });

  it("切换选中章节 → 草稿同步为新章节的预算（不残留上一个章节的值）", () => {
    const utils = render(
      <OutlineNodeBudgetPanel node={mkNode({ word_budget: 1500 })} onBudgetChange={vi.fn()} />,
    );
    const inputEl = () =>
      utils.container.querySelector('input[aria-label="字数预算"]') as HTMLInputElement;
    expect(inputEl().value).toBe("1500");
    utils.rerender(
      <OutlineNodeBudgetPanel
        node={mkNode({ key: "sec-2", title: "施工方案", word_budget: 2600 })}
        onBudgetChange={vi.fn()}
      />,
    );
    expect(inputEl().value).toBe("2600");
  });

  it("word_budget 为 0 / 缺省 → 回退 1500 参与计算", () => {
    const { input } = setupBudget(mkNode({ word_budget: 0 }));
    expect(input().value).toBe("1500");
  });
});

