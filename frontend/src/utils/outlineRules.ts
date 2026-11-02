/**
 * 目录结构硬性标准（R162 · 2026-11-01）—— 前端镜像。
 *
 * 单一事实源 = `backend/app/services/outline_rules.py`：
 *   - 层级深度：目录树最多 L4 级，三级为最小叶子（L4 仅作兼容层）；严禁 L5；
 *   - 数量控制：L1 下 3~5 个 L2；L2 下 3~5 个 L3。三级为最小叶子，
 *     三级下是否细分 L4 不作数量校验（「三级下 4~6 个四级」转入 L3 叶子节
 *     正文生成提示词，成稿以 Heading 4 呈现）；
 *   - 编号：第一章 -> 1 -> 1.1 -> 1.1.1 子承父系（后端 numbering 推导）。
 *
 * ⚠️ 数值必须与后端保持 parity：backend/tests/test_r162_outline_leaf3_standard_20261101.py
 * 有源码级双向锚（改动任一侧必须同步另一侧，否则护栏红）。
 *
 * 消费方（SchemeWorkbenchPage）：目录树红点警告（数量不达标 / 深度越界的
 * 节点标红）+ 渲染层只画到 L4（更深节点数据保留、不渲染，后端落库前已裁剪）。
 */

/** 目录允许的最大层级（L4 为兼容层上限，与后端 OUTLINE_MAX_LEVEL 一致） */
export const OUTLINE_MAX_LEVEL = 4;

/** 最小叶子层级（三级为最小叶子，与后端 LEAF_LEVEL 一致） */
export const LEAF_LEVEL = 3;

/** 各父级层级的子节点数量区间（闭区间，与后端 CHILDREN_MIN/MAX 一致） */
export const CHILDREN_MIN: Record<number, number> = { 1: 3, 2: 3 };
export const CHILDREN_MAX: Record<number, number> = { 1: 5, 2: 5 };

export type OutlineViolationCode =
  | "depth_exceeded"
  | "too_few_children"
  | "too_many_children"
  | "numbering_mismatch";

export interface OutlineRuleViolation {
  code: OutlineViolationCode;
  /** 违规归属节点的层级（父级层级） */
  level: number;
  title: string;
  detail: string;
  expected: string;
  actual: string;
}

/** 最小结构约束：与页面 TreeNode 的 key/level/title/children 兼容 */
interface RuleNodeLike {
  key: string;
  level: number;
  title: string;
  children?: unknown[];
}

const LEVEL_NAMES: Record<number, string> = {
  1: "一级", 2: "二级", 3: "三级", 4: "四级",
};

/**
 * 逐节点校验目录树，返回 **key → 违规清单** 映射（红点渲染用）。
 *
 * 判定与后端 validate_section_rows 同口径：
 *   - L1/L2 节点（作父级）：子节点数必须落在 CHILDREN_MIN/MAX（含 0 =
 *     叶子层级不足）；L3 为最小叶子，其下是否细分 L4 不作数量校验；
 *   - L4 节点：不得再有 children（出现 L5 即深度违规）。
 * 违规归属到**父节点本身**（在它身上补齐/精简子节点）。
 */
export function outlineViolationsByKey<T extends RuleNodeLike>(
  roots: readonly T[],
): Map<string, OutlineRuleViolation[]> {
  const map = new Map<string, OutlineRuleViolation[]>();
  const push = (key: string, v: OutlineRuleViolation) => {
    const list = map.get(key);
    if (list) list.push(v);
    else map.set(key, [v]);
  };

  const walk = (nodes: readonly RuleNodeLike[]) => {
    for (const node of nodes) {
      const level = Number(node.level) || 1;
      const kids = Array.isArray(node.children) ? node.children : [];
      if (level >= 1 && level <= 2) {
        const lo = CHILDREN_MIN[level];
        const hi = CHILDREN_MAX[level];
        if (kids.length < lo) {
          push(node.key, {
            code: "too_few_children",
            level,
            title: node.title,
            detail: `${LEVEL_NAMES[level]}节点「${node.title}」下仅 ${kids.length} 个子节点，要求 ${lo}~${hi} 个${kids.length === 0 ? "（叶子层级必须达到三级）" : ""}`,
            expected: `${lo}~${hi}`,
            actual: String(kids.length),
          });
        } else if (kids.length > hi) {
          push(node.key, {
            code: "too_many_children",
            level,
            title: node.title,
            detail: `${LEVEL_NAMES[level]}节点「${node.title}」下多达 ${kids.length} 个子节点，要求 ${lo}~${hi} 个`,
            expected: `${lo}~${hi}`,
            actual: String(kids.length),
          });
        }
      } else if (level >= OUTLINE_MAX_LEVEL && kids.length > 0) {
        push(node.key, {
          code: "depth_exceeded",
          level,
          title: node.title,
          detail: `「${node.title}」是四级节点，不得再挂 ${kids.length} 个子节点（出现第五级）`,
          expected: "0",
          actual: String(kids.length),
        });
      }
      if (kids.length > 0) walk(kids as RuleNodeLike[]);
    }
  };

  walk(roots);
  return map;
}

/** 违规总数（0 = 合规） */
export function countOutlineViolations(
  violations: Map<string, OutlineRuleViolation[]>,
): number {
  let total = 0;
  for (const list of violations.values()) total += list.length;
  return total;
}

/** 单节点违规的悬浮提示文本（红点 Tooltip 用） */
export function formatOutlineViolationTooltip(
  violations: readonly OutlineRuleViolation[],
): string {
  return violations.map((v) => v.detail).join("；");
}
