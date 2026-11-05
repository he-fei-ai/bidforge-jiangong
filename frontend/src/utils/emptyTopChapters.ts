/** 空壳一级章提示（R175 · G-3 逃生舱配套）
 *
 * 背景：后端 R175 起在目录写路径（save-outline / upload-outline）统一落
 * 「空壳一级章闸门」——「已有多级结构却仍有 children=[] 一级章」的混合形态树
 * （分步生成失败章 / 手工误留节点）默认 400 拒存，两条放行链路会**加法式**
 * 回传 `empty_top_chapters` 明细：
 *   ① source="ai"（分步部分成果 / 停止恢复保存）—— fail-soft 放行；
 *   ② 请求体显式 allow_empty_chapters=true（前端空章确认框「仍要保存」）。
 * 若前端零消费，放行落库就**静默生效**：用户不知道目录里留着无子章节的空章。
 *
 * 契约（与 genericResidual 同款，务必保持向后兼容）：
 * - 仅当确有空壳章才回传该键（旧后端 / 干净目录都不带）⇒ 无键时返回空串；
 * - 元素形如 `{ index, title }`（index 为 0 起的一级章序号）；
 * - **纯函数、零 IO**：只负责把响应片段拼成一句可操作的中文提示，
 *   符合本仓「派生纯函数层」约定（与 `genericResidual.ts` 同层），
 *   vitest 可直接覆盖，而不必渲染整个页面。
 */

export interface EmptyTopChapterHit {
  index?: number;
  title?: string;
}

const MAX_SHOWN = 5;

export function emptyTopChapterHits(data: unknown): EmptyTopChapterHit[] {
  const raw = (data as { empty_top_chapters?: unknown } | null | undefined)?.empty_top_chapters;
  return Array.isArray(raw) ? (raw as EmptyTopChapterHit[]) : [];
}

/** 有空壳章时返回可操作提示文案；无键 / 空数组 / 旧后端 / 脏数据一律空串。 */
export function emptyTopChaptersNotice(data: unknown): string {
  const hits = emptyTopChapterHits(data);
  if (!hits.length) return "";
  const names = hits
    .slice(0, MAX_SHOWN)
    .map((h) => {
      const i =
        typeof h?.index === "number" && Number.isFinite(h.index) && h.index >= 0
          ? `第${Math.floor(h.index) + 1}章`
          : "某一级章";
      const t = String(h?.title ?? "").trim();
      return t ? `${i}「${t}」` : i;
    })
    .join("、");
  const more = hits.length > MAX_SHOWN ? ` 等 ${hits.length} 章` : "";
  return (
    `目录已保存，但有 ${hits.length} 个一级章暂无子章节（${names}${more}）——` +
    `空章导出时将只保留章标题，建议继续为其补充二级章节或删除多余空章`
  );
}
