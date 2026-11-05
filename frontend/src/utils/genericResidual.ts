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
 * - 另有加法式键 `generic_autofixed`（整数，仅 >0 才回传）：落库前自动改写了几条
 *   占位标题 —— 由 `genericAutofixNotice` 消费，使自动改写**不再静默生效**。
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
  const raw = (data as { generic_residual?: unknown } | null | undefined)?.generic_residual;
  return Array.isArray(raw) ? (raw as GenericResidualHit[]) : [];
}

/** 有残留时返回可操作提示文案；无残留 / 旧后端 / 脏数据一律返回空串。 */
export function genericResidualNotice(data: unknown): string {
  const hits = genericResidualHits(data);
  if (!hits.length) return "";
  const titles = hits.map((h) => String(h?.title ?? "").trim()).filter((t) => t.length > 0);
  const shown = titles.slice(0, MAX_SHOWN).join("、") || "（空标题）";
  const more = titles.length > MAX_SHOWN ? ` 等 ${titles.length} 处` : "";
  return (
    `目录已保存，但检出 ${hits.length} 处占位/泛化标题（如「${shown}」${more}）——` +
    `这类标题不是合法目录名，建议逐条改为具体到施工对象/部位/工序的名称`
  );
}

/** 自动改写条数：仅接受正整数（数字或纯数字字符串），其余一律 0。 */
export function genericAutofixedCount(data: unknown): number {
  const raw = (data as { generic_autofixed?: unknown } | null | undefined)?.generic_autofixed;
  if (typeof raw === "number") {
    return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0;
  }
  if (typeof raw === "string" && raw.trim() !== "") {
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  }
  return 0;
}

/** 后端在落库前**自动改写**了占位标题时返回提示文案；否则空串。
 *
 * 背景：R89 起后端在目录写路径（save-outline / upload-outline …）落库前会做一次
 * 「外科式标题重写」——只回填标题、绝不增删节点。若前端不提示，自动改写就**静默
 * 生效**：用户以为目录原样保存，实际章节名已变。故后端加法式回传 `generic_autofixed`
 * （仅 >0 时才有该键），本函数负责把它变成一句可核对的中文提示。
 *
 * 契约与 `generic_autofixed` 同：无键 / 0 / 脏数据 ⇒ 返回空串（旧后端向后兼容）。
 */
export function genericAutofixNotice(data: unknown): string {
  const n = genericAutofixedCount(data);
  if (!n) return "";
  return (
    `目录保存时已自动改写 ${n} 处占位/泛化标题（只改标题，不增删章节），` +
    `请核对改写后的名称是否符合预期`
  );
}

/** ✅ D-09（R176 · 2026-11-04）：后端落库前自动改写**未执行**（异常/超时）时
 *  返回提示文案；否则空串。
 *
 * 背景：后端 `sections._save_outline_to_db` / `upload_outline.save_as_outline`
 * 的自动重写是 fail-soft——失败时按原样保存且**不阻断**。此前失败只写后端
 * 日志，前端只看到「保存成功」，不知道占位标题仍留在目录里。后端加法式回传
 * `generic_autofix_failed=true`（仅失败时才有该键），本函数把它变成可操作提示。
 *
 * 契约：无键 / 非 true ⇒ 空串（旧后端向后兼容，不产生任何噪声）。
 */
export function genericAutofixFailedNotice(data: unknown): string {
  const raw = (data as { generic_autofix_failed?: unknown } | null | undefined)
    ?.generic_autofix_failed;
  if (raw !== true && raw !== "true") return "";
  return (
    "标题自动重写未执行（服务繁忙或超时），目录已按原样保存 —— " +
    "若目录中存在「专项6」「总体实施策划（2）」之类的占位标题，请手动改名后重新保存"
  );
}
