// @vitest-environment jsdom
/**
 * ✅ R174 F7（2026-11-03）：目录闸门列表 key 稳定性与勾选契约回归。
 *
 * 旧缺陷：outline.map 用纯 index 作 key，outline 引用变化（AI 重新生成 /
 * 父组件重渲）引起列表重排时，React 复用邻行 DOM 会出现「勾选框与章节错位」。
 *
 * 修复：key 改为「title+idx」组合键；勾选状态仍按 index 记录 —— selectedRef
 * 消费方（modal.confirm onOk 读取 number[] 索引）契约保持不变，本文件锁定之。
 */
import { describe, it, expect } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { useRef } from "react";
import { OutlineGateList } from "../pages/schemeWorkbench/OutlineGateList";

// 源码锚点：沿用仓内范式（import.meta.glob 读源码文本，不引 node:fs）
const RAW: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).map(([k, v]) => [k.replace(/^\.\.\//, ""), v] as const),
);

// jsdom 缺 matchMedia，antd Checkbox 会用到
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

const OUTLINE = [
  { title: "工程概况", description: "项目基本信息" },
  { title: "施工工艺", description: "工艺流程" },
  { title: "应急预案" }, // 无 description
];

function Harness({ onReady }: { onReady: (ref: { current: number[] }) => void }) {
  const ref = useRef<number[]>([]);
  onReady(ref);
  return <OutlineGateList outline={OUTLINE} selectedRef={ref} />;
}

describe("OutlineGateList · key 稳定与勾选契约（R174 F7）", () => {
  it("初始全选：selectedRef 为全部索引（number[] 契约不变），章节名完整渲染", () => {
    let ref!: { current: number[] };
    render(
      <Harness
        onReady={(r) => {
          ref = r;
        }}
      />,
    );
    expect(ref.current).toEqual([0, 1, 2]);
    expect(document.body.textContent).toContain("工程概况");
    expect(document.body.textContent).toContain("施工工艺");
    expect(document.body.textContent).toContain("应急预案");
  });

  it("取消勾选第 2 章 → selectedRef 排除索引 1（消费方按索引过滤的口径不变）", () => {
    let ref!: { current: number[] };
    const { container } = render(
      <Harness
        onReady={(r) => {
          ref = r;
        }}
      />,
    );
    const checkboxes = container.querySelectorAll<HTMLInputElement>("input.ant-checkbox-input");
    expect(checkboxes.length).toBe(3);
    fireEvent.click(checkboxes[1]);
    expect(ref.current).toEqual([0, 2]);
    // 重新勾选恢复（toggle 追加到尾部，按集合语义排序断言；消费方只做 Set 过滤）
    fireEvent.click(checkboxes[1]);
    expect([...ref.current].sort((a, b) => a - b)).toEqual([0, 1, 2]);
  });

  it("行 key 为 title+idx 组合键（源码级锁定，防止回退纯 index）", () => {
    const src = RAW["pages/schemeWorkbench/OutlineGateList.tsx"];
    expect(src, "OutlineGateList.tsx 应在 glob 覆盖内").toBeTruthy();
    expect(src).toContain("key={`${i}-${n.title");
    // 不存在裸 index key
    expect(src).not.toMatch(/<div key=\{i\}/);
  });
});

// =========================================================================
// ✅ R174 F6：矛盾候选值列表 key 稳定性（SchemeWorkbenchPage 内两处，
//    JSX 过深，按仓内锚点测试风格以源码断言锁定）
// =========================================================================
describe("矛盾候选值列表 · 内容组合键（R174 F6）", () => {
  it("两处 conflict_values 映射均改为「值::来源」组合键，不再裸 index", () => {
    // ✅ 2026-11-04 拆分（G2）：两处候选值渲染随 FactsGroupList 迁到
    //    pages/schemeWorkbench/FactsGroupList.tsx。判据语义不变
    //    （「两处映射都用内容组合键、不出现裸 index key」），故指向新位置。
    const src = RAW["pages/schemeWorkbench/FactsGroupList.tsx"];
    expect(src, "FactsGroupList.tsx 应在 glob 覆盖内").toBeTruthy();
    // 内容组合键（值+来源，末位 idx 仅作重复项消歧）
    const hits = src.match(
      /key=\{`\$\{String\(cv\.value\)\}::\$\{String\(cv\.source\)\}::\$\{ci\}`\}/g,
    );
    expect(hits?.length).toBe(2);
    // 不再存在裸 index key 的候选值行
    expect(src).not.toContain("<div key={ci}>");
  });
});
