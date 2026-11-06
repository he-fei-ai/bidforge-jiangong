/**
 * ✅ R181（2026-11-06）：后端已回传、前端此前未消费的诊断字段 → 用户可见文案。
 *
 * 单独放在纯函数模块，便于回归用例直接锁定文案口径，也避免测试为了拿一个
 * 字符串去加载整页 React/antd 组件。
 */

/** 导出配置：`scene_routes_ok=false` 时必须提示「本次导出不含场景路由」。 */
export function exportSceneRoutesNotice(sceneRoutesOk: boolean | undefined): string {
  return sceneRoutesOk === false
    ? "场景模型路由读取失败，本次导出文件不含场景路由；请稍后重新导出"
    : "";
}

/** 导入预演：提示「其中 N 条导入后仍为停用」（skip 行不计入）。 */
export function importDryRunDisabledNotice(
  planned: Array<{ action?: string; enabled?: number | null }>,
): string {
  const disabled = planned.filter((p) => p.action !== "skip" && p.enabled === 0).length;
  return disabled > 0
    ? `导入预演：其中 ${disabled} 条将保持「停用」状态，不参与真实调用；如需启用，请在配置列表中逐条开启`
    : "";
}

/** 正文保存：把围栏修复计数（含疑似吞行）转成可操作提示。 */
export function sectionSaveFenceNotice(data: {
  fence_fixed?: unknown;
  fence_swallowed_lines?: unknown;
}): string {
  const fixed = Number(data.fence_fixed || 0);
  const swallowed = Number(data.fence_swallowed_lines || 0);
  if (!Number.isFinite(fixed) || fixed <= 0) return "";
  if (swallowed > 0) {
    return `已自动补齐未闭合代码围栏 ${fixed} 处；其中约 ${swallowed} 行疑似被误困在围栏内，请核对正文是否完整`;
  }
  return `已自动补齐未闭合代码围栏 ${fixed} 处（仅闭合标记，未吞入正文）`;
}
