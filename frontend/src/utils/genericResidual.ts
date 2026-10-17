/** 目录占位/泛化标题提示（R86 · L1）
 *
 * 背景：用户在 TOC 里看到的「专项6」「总体实施策划（2）」有两批来源 ——
 * ① AI 生成（有占位闸门 + 外科式重写修复链）；
 * ② **目录写入路径**（手工新增/改名、上传目录识别、目录库套用）。
 * 后端已在 create / update / save-outline / upload-outline 四条入口统一
 * 加法式回传 `generic_residual` 明细（**绝不阻断保存、绝不静默改写标题**），
 * 但前端此前零消费 ⇒ 用户完全无感知。
 *
 * 契约（务必保持向后兼容）：
 * - 仅当确有残留才回传该键（旧后端 / 干净目录都不带）⇒ 无键时返回空串；
 * - 元素形如 `{ path, title, kind }`，`kind` 为 `generic` | `conflict_suffix`；
 * - **纯函数、零 IO**：只负责把响应片段拼成一句可操作的中文提示。
 *   符合本仓「派生纯函数层」约定（与 `contentEvents.ts` / `workflowDerived.ts` 同层），
 *   因此可被 vitest 直接覆盖，而不必渲染整个页面。
 */

export interface GenericResidualHit {
  path?: string;
  title?: string;
  kind?: "generic" | "conflict_suffix" | string;
}

const MAX_SHOWN = 5;

export function genericResidualHits(data: unknown): GenericResidualHit[] {
  const raw = (data as { generic_residual?: unknown } | null | undefined)
    ?.generic_residual;
  return Array.isArray(raw) ? (raw as GenericResidualHit[]) : [];
}

/** 有残留时返回可操作提示文案；无残留 / 旧后端 / 脏数据一律返回空串。 */
export function genericResidualNotice(data: unknown): string {
  const hits = genericResidualHits(data);
  if (!hits.length) return "";
  const titles = hits
    .map((h) => String(h?.title ?? "").trim())
    .filter((t) => t.length > 0);
  const shown = titles.slice(0, MAX_SHOWN).join("、") || "（空标题）";
  const more = titles.length > MAX_SHOWN ? ` 等 ${titles.length} 处` : "";
  return (
    `目录已保存，但检出 ${hits.length} 处占位/泛化标题（如「${shown}」${more}）——` +
    `这类标题不是合法目录名，建议逐条改为具体到施工对象/部位/工序的名称`
  );
}