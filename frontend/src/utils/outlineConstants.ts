/**
 * 目录系统硬性上限：三级（与后端 `services/outline_utils.MAX_OUTLINE_DEPTH` 对齐）。
 *
 * ✅ 2026-10-05（D5 · 前端常量收敛）：此前 `SchemeWorkbenchPage.tsx` 与
 * `OutlineLibraryEditModal.tsx` **各自内联**一份 `MAX_OUTLINE_DEPTH = 3` ——
 * 两处可独立漂移（改一处忘另一处 → 三个入口对目录层级的拦截口径不一致）。
 * 现提取为唯一前端事实源，两个组件统一从此处导入。
 *
 * 注意：后端若调整该上限，需同步本常量与导出层级模板。
 */
export const MAX_OUTLINE_DEPTH = 3;