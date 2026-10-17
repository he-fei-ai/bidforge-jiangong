/** 章节编号的**合法性判据**（前端侧）—— 与后端 `numbering.stored_id_parts` 同口径。
 *
 * ⚠️ R87 FIX-1（P1）：此前本文件（`SchemeWorkbenchPage.formatOutlineTitle`）自持一份
 * **松过滤**：
 *
 * ```ts
 * String(id || "").split(".").map((p) => parseInt(p, 10)).filter((n) => !Number.isNaN(n))
 * ```
 *
 * 它会把**非法**存储编号「抢救」成看似合法的数字序列，而后端
 * `numbering._stored_id_parts` 对同一输入 **fail-closed**（返回 `[]` ⇒ 落库正文
 * 与导出成稿都不套号）。实测分叉：
 *
 * | stored_id | 导出成稿 | 改前前端目录树 |
 * |---|---|---|
 * | `abc1.2` | 无编号（fail-closed） | `2 现场布置` ← **凭空造出章号** |
 * | `0.3` | 无编号 | `3 现场布置` |
 * | `1.2.x` | 无编号 | `1.2 现场布置` |
 * | `1.0.2` | 无编号 | `0.2 现场布置` |
 *
 * ⇒ 用户看到的目录树编号与交付的 DOCX **不一致**（AGENTS §4.13 D4
 * 「落库正文 ≠ 导出成稿」根因家族）。R86 只封了后端一半，本函数补齐另一半。
 *
 * ## 与后端逐条对齐
 *
 * 后端（`numbering.py`）：
 *   `_DOT_PATH_RE = re.compile(r"\d+(\.\d+)*")` + `not any(p == "0")`
 *
 * 本实现的两处**刻意**差异（均朝「更严」方向，绝不朝「编造」方向）：
 *  1. **全角数字**：Python 的 `\d` 匹配 Unicode Nd（含全角），JS 的 `\d` **不会**。
 *     若不覆盖全角，`１２.３` 会被前端拒、后端接受 ⇒ **反向**分叉。故字符类
 *     同时覆盖 ASCII 与全角，并在取值前把全角折成 ASCII（`Number("１２")`
 *     在 JS 里是 `NaN`，不折必然崩）。
 *  2. **float**：后端 D-8a 的判据是 `isinstance(stored_id, (str, int))` ——
 *     **float 被显式拒绝**（`3.5` 会被拆成 `["3","5"]` 而造成假编号）。
 *     JS 的 `typeof x === "number"` 包含小数，故必须补 `Number.isInteger`，
 *     否则 `storedIdParts(3.2)` 前端给 `[3,2]`、后端给 `[]` ⇒ 分叉。
 *  3. **0 号段**：后端只拒字面量 `"0"`；本实现拒一切数值 ≤ 0 的段
 *     （即连 `"00"` 也拒）。0 号章节在任何合法目录里都不存在，展示层更严是安全的。
 *
 * 跨语言 parity 由后端护栏 `tests/test_outline_closeout_r87_20261016.py`
 * 逐条比对（canonical 与垃圾形态两侧输出必须逐项一致）。
 */

/** 全角数字 → ASCII 数字（`Number("１２")` 在 JS 里是 NaN，必须先折）。 */
function toAsciiDigits(s: string): string {
  return s.replace(/[０-９]/g, (c) =>
    String.fromCharCode(c.charCodeAt(0) - 0xfee0),
  );
}

/**
 * 存储态章节编号 → 合法数字段列表。
 *
 * 与后端 `numbering.stored_id_parts` **同口径**：仅接受「点分纯数字路径」，
 * 非法（UUID / 含字母 / 空段 / 全角以外的杂字符 / 前导后随空白以外的杂质）
 * 一律返回 `[]` ⇒ 调用方必须**不渲染编号**（而不是渲染一个猜出来的号）。
 */
export function storedIdParts(id: unknown): number[] {
  if (typeof id !== "string" && typeof id !== "number") return [];
  // 后端只接受 int（float 被显式拒绝，见文件头第 2 点）；JS 的 number 含小数
  if (typeof id === "number" && !Number.isInteger(id)) return [];
  const raw = String(id).trim();
  // 字符类同时覆盖 ASCII 与全角数字（见文件头「与后端逐条对齐」第 1 点）
  if (!/^[0-9０-９]+(\.[0-9０-９]+)*$/.test(raw)) return [];
  const out: number[] = [];
  for (const seg of raw.split(".")) {
    const n = Number(toAsciiDigits(seg));
    if (!Number.isFinite(n)) return [];
    // 编号从 1 起，0 号段不存在（见文件头第 2 点）
    if (n <= 0) return [];
    out.push(n);
  }
  return out;
}

/** 存储态编号是否可安全渲染（`true` ⇒ 可套编号；`false` ⇒ 必须退回裸标题）。 */
export function hasRenderableStoredId(id: unknown): boolean {
  return storedIdParts(id).length > 0;
}
