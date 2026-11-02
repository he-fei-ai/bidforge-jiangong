/**
 * 四档字数控制 · 前端唯一镜像（R159 · 2026-11-01）
 *
 * 真源：backend/app/services/content_utils.py 的 WORD_BUDGET_TIERS（R66 引入，
 * 档位语义见其注释）。此前数值散落前端三处字面量副本且无 parity 锁：
 *   - ContentGenerationTab 的 WORD_BUDGET_OPTIONS（label 内嵌数值）；
 *   - SchemeWorkbenchPage 的 WORD_BUDGET_TIER_VALUES 档位值表；
 *   - workflowDerived 的缺省预算 1200。
 * 本模块收敛为前端唯一镜像，数值与行为双向锁死：
 *   - 数值 parity：backend/tests/test_r159_word_tiers_ssot_20261101.py 源码锚；
 *   - 行为锁：src/tests/wordBudgetTiersR159.test.ts。
 * 改档位数值 = 改后端真源 + 本镜像两处（任一漏改即护栏红）。
 */

/** 四档档位值（key 稳定序与后端 dict 序一致：brief/standard/detailed/ultra） */
export const WORD_BUDGET_TIERS = {
  brief: 800,
  standard: 1200,
  detailed: 2000,
  ultra: 2500,
} as const;

export type WordBudgetTierKey = keyof typeof WORD_BUDGET_TIERS;

/** 档位 key 稳定序（遍历/合法值判定共用） */
export const WORD_BUDGET_TIER_KEYS = Object.keys(WORD_BUDGET_TIERS) as WordBudgetTierKey[];

/** 档位中文名（与后端 WORD_BUDGET_TIER_LABELS_ZH 同名同序） */
export const WORD_BUDGET_TIER_LABELS: Record<WordBudgetTierKey, string> = {
  brief: "简短",
  standard: "标准",
  detailed: "详细",
  ultra: "超长",
};

export const WORD_BUDGET_DEFAULT_TIER: WordBudgetTierKey = "standard";

/** 未设置预算时的默认目标字数（= 标准档；与后端 DEFAULT_WORD_BUDGET 同值） */
export const DEFAULT_WORD_BUDGET: number = WORD_BUDGET_TIERS[WORD_BUDGET_DEFAULT_TIER];

/** 宽索引读取面：请求体/旧数据里的档位名是任意 string，取值走这里 */
export const WORD_BUDGET_TIER_VALUE_OF: Record<string, number> = WORD_BUDGET_TIERS;

/** 正文生成面板的四档下拉（label 内嵌数值，档位变更自动跟随，禁止手写字面量 label） */
export const WORD_BUDGET_OPTIONS = [
  { label: "使用目录默认值", value: "default" },
  ...WORD_BUDGET_TIER_KEYS.map((k) => ({
    label: `${WORD_BUDGET_TIER_LABELS[k]} ${WORD_BUDGET_TIERS[k]} 字`,
    value: k,
  })),
  { label: "自定义", value: "custom" },
];

// --------------------------------------------------------------------------
// 字数预算输入区间（前端 UX 约定；⚠️ 后端 SectionUpdate.word_budget 为裸 int、
// 无范围校验，本区间只约束前端输入，与「服务端拒绝超限」无关）
// --------------------------------------------------------------------------

/** 单节字数预算编辑的合法下限 */
export const WORD_BUDGET_MIN = 50;
/** 单节字数预算编辑的合法上限 */
export const WORD_BUDGET_MAX = 50000;

// 「自定义」档是**二级单元总量**口径（word_budget_override 旧语义），与四档
// 「单叶目标字数」不同族；三个输入常数一并收敛至此，避免散落组件内联。
/** 自定义档输入下限 */
export const CUSTOM_TOTAL_BUDGET_MIN = 100;
/** 自定义档输入上限 */
export const CUSTOM_TOTAL_BUDGET_MAX = 20000;
/** 自定义档输入缺省值 */
export const CUSTOM_TOTAL_BUDGET_DEFAULT = 2000;
