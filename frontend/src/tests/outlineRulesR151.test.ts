/**
 * R162（2026-11-01）目录结构硬性标准 · 前端行为护栏
 *
 * 锁 outlineRules.ts 的判据行为：三级最小叶子（L1 下 3~5 个二级、L2 下 3~5
 * 个三级；L3 下是否细分 L4 不作数量校验，L4 挂子级即违规）、违规归属父节点
 * key（红点渲染的定位键）、顶层一级数量不受约束。
 *
 * ⚠️ 与后端单一事实源的数值 parity、页面接线（红点/渲染截断/修复入口）的
 * 源码锚统一放在后端 tests/test_r162_outline_leaf3_standard_20261101.py 的 H 组
 * （前端 tsc 环境无 node:fs/@types/node，本文件只做纯行为测试）。
 */
import { describe, expect, it } from "vitest";
import {
  CHILDREN_MAX,
  CHILDREN_MIN,
  OUTLINE_MAX_LEVEL,
  countOutlineViolations,
  formatOutlineViolationTooltip,
  outlineViolationsByKey,
} from "../utils/outlineRules";

function node(key: string, level: number, title: string,
              children?: any[]) {
  return { key, level, title, ...(children ? { children } : {}) };
}

/** 合规最小树：1 个一级 → 3 个二级 → 每个 3 个三级（三级即最小叶子） */
function compliantTree(): any[] {
  return [node("1", 1, "第一章", Array.from({ length: 3 }, (_, i) =>
    node(`1.${i + 1}`, 2, `二级${i + 1}`, Array.from({ length: 3 }, (_, j) =>
      node(`1.${i + 1}.${j + 1}`, 3, `三级${j + 1}`)))))];
}

describe("outlineRules · 三级叶子标准行为", () => {
  it("常量与后端单一事实源一致（数值面，源码面在后端 H 组锚定）", () => {
    expect(OUTLINE_MAX_LEVEL).toBe(4);
    expect(CHILDREN_MIN).toEqual({ 1: 3, 2: 3 });
    expect(CHILDREN_MAX).toEqual({ 1: 5, 2: 5 });
  });

  it("合规三级树零违规；顶层一级数量不受约束", () => {
    expect(outlineViolationsByKey(compliantTree()).size).toBe(0);
    const ten = Array.from({ length: 10 }, (_, i) =>
      node(`c${i}`, 1, `第${i + 1}章`, []));
    ten.forEach((n) => {
      n.children = Array.from({ length: 3 }, (_, j) =>
        node(`${n.key}.${j}`, 2, "二", Array.from({ length: 3 }, (_, k) =>
          node(`${n.key}.${j}.${k}`, 3, "三"))));
    });
    expect(outlineViolationsByKey(ten).size).toBe(0);
  });

  it("L3 下细分 L4 不作数量校验（R162 核心翻转：3 个或 0 个都合规）", () => {
    const tree = compliantTree();
    const l3 = (tree[0] as any).children[0].children[0];
    l3.children = Array.from({ length: 3 }, (_, k) =>
      node(`1.1.1.${k + 1}`, 4, `四级${k + 1}`));
    expect(outlineViolationsByKey(tree).size).toBe(0);

    const plain = compliantTree();
    expect(outlineViolationsByKey(plain).size).toBe(0);
  });

  it("二级下只有 2 个三级 → too_few_children，归属到该二级节点 key", () => {
    const tree = compliantTree();
    (tree[0] as any).children[0].children =
      (tree[0] as any).children[0].children.slice(0, 2);
    const map = outlineViolationsByKey(tree);
    expect(map.get("1.1")?.length).toBe(1);
    expect(map.get("1.1")![0].code).toBe("too_few_children");
    expect(map.get("1.1")![0].actual).toBe("2");
    expect(countOutlineViolations(map)).toBe(1);
  });

  it("一级下 6 个二级 → too_many_children；一级为空叶子 → too_few（叶子层级必须达到三级）", () => {
    const six = [node("1", 1, "第一章", Array.from({ length: 6 }, (_, i) =>
      node(`1.${i + 1}`, 2, "二")))];
    const map = outlineViolationsByKey(six);
    expect(map.get("1")![0].code).toBe("too_many_children");

    const leafL1 = [node("1", 1, "第一章")];
    const map2 = outlineViolationsByKey(leafL1);
    expect(map2.get("1")![0].code).toBe("too_few_children");
    expect(map2.get("1")![0].detail).toContain("叶子层级必须达到三级");
  });

  it("四级挂子级 → depth_exceeded（严禁 L5）", () => {
    const tree = compliantTree();
    const l3 = (tree[0] as any).children[0].children[0];
    l3.children = [node("1.1.1.1", 4, "四级", [node("x", 5, "五级")])];
    const map = outlineViolationsByKey(tree);
    const vs = map.get("1.1.1.1")!;
    expect(vs.some((v: any) => v.code === "depth_exceeded")).toBe(true);
  });

  it("Tooltip 文本拼接逐条违规", () => {
    const text = formatOutlineViolationTooltip([
      { code: "too_few_children", level: 2, title: "x", detail: "违规A", expected: "3~5", actual: "2" },
      { code: "depth_exceeded", level: 4, title: "y", detail: "违规B", expected: "0", actual: "2" },
    ]);
    expect(text).toBe("违规A；违规B");
  });
});
