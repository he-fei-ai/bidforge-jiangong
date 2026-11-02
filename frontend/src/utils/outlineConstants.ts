/**
 * 目录系统硬性上限：四级（R162 · 2026-11-01，与后端
 * `services/outline_rules.OUTLINE_MAX_LEVEL` / `outline_utils.MAX_OUTLINE_DEPTH`
 * 对齐）。三级为最小叶子（正文写作单元），四级仅作兼容层，严禁第五级；
 * 子节点数量硬约束（L1 下 3~5 个 L2、L2 下 3~5 个 L3；L3 下是否细分 L4
 * 不作数量校验，其「4~6 个四级要点」转由 L3 叶子节正文提示词承载）的判据在
 * `utils/outlineRules.ts`（红点警告用）。
 *
 * ✅ 2026-10-05（D5 · 前端常量收敛）：此前 `SchemeWorkbenchPage.tsx` 与
 * `OutlineLibraryEditModal.tsx` **各自内联**一份常量 ——
 * 两处可独立漂移（改一处忘另一处 → 三个入口对目录层级的拦截口径不一致）。
 * 现提取为唯一前端事实源，两个组件统一从此处导入。
 *
 * 注意：后端若调整该上限，需同步本常量与导出层级模板。
 */
export const MAX_OUTLINE_DEPTH = 4;