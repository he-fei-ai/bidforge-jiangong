import { type GenStats, type SectionLogItem } from "../../utils/contentEvents";
import { MAX_OUTLINE_DEPTH } from "../../utils/outlineConstants";
import { statsEqual } from "./statsFormat.ts";
import type { OutlineNodeInput } from "./workbenchTypes.ts";
import { Alert, Tag, Tree } from "antd";
import { memo } from "react";

export type TreeNode = {
  key: string;
  title: string;
  level: number;
  status: string;
  word_count: number;
  word_budget: number;
  word_status?: string;
  content?: string;
  description?: string;
  /** 后端权威编号（点分路径，如 "1" / "1.1" / "1.1.1"） */
  outlineId?: string;
  /**
   * ✅ BUG 修复（2026-09-26）：后端 AI 调整目录（/adjust-outline）回传的真实
   * DB 主键。后端 renumber 会把节点 id 覆写成展示编号（"1"/"1.1"），
   * 真实主键只存在于该字段；treeToOutline 需原样透传，否则保存时
   * is_new 全为 True → 整表重建、已生成正文全部丢失。
   */
  __original_id?: string;
  /** F-CONTENT-STANDARD(2026-09-26)：章节级生成标准覆盖（""=沿用方案级） */
  generation_standard?: string;
  /** F-CONTENT-STANDARD(2026-09-26)：最近一次生成实际使用的标准（只读，供复查） */
  last_generation_standard?: string;
  /**
   * ✅ 2026-09-29（事实变更失效标记）：本章节的正文写于「全局事实变更」之前，
   * 即正文里引用的参数可能已过期。后端按方案级 `schemes.facts_updated_at`
   * 与章节 `updated_at` 读侧派生（非布尔列），重生成/手工编辑后自动消失。
   * 仅提示，不阻断任何操作 —— 由用户显式决定要不要重新生成。
   */
  facts_stale?: boolean;
  children?: TreeNode[];
};

/**
 * ✅ 2026-11-04 拆分：页面内联 `buildTree` 原位搬到本模块（函数体逐字搬运）。
 * REST/outline_json 形状 → 渲染树 TreeNode 的唯一映射入口。
 */
export function buildTree(nodes: OutlineNodeInput[]): TreeNode[] {
  return nodes.map((n) => {
    // ✅ 从 outline_json 里提取后端权威编号（点分路径，如 "1"/"1.1"/"1.1.1"）
    // 这是目录树编号正确显示的关键 —— UUID 无点号无法解析
    let outlineId: string | undefined;
    try {
      const oj = typeof n.outline_json === "string" ? JSON.parse(n.outline_json) : n.outline_json;
      if (oj?.id && /^\d+(\.\d+)*$/.test(String(oj.id))) {
        outlineId = String(oj.id);
      }
    } catch {
      /* ignore */
    }
    return {
      key: n.id as string,
      title: n.title!,
      level: n.level!,
      status: n.status!,
      word_count: n.word_count || 0,
      word_budget: n.word_budget || 0,
      content: n.content,
      description: n.description || "",
      outlineId,
      // ✅ 2026-09-29：事实变更失效标记（后端 _build_tree 读侧派生）
      facts_stale: !!n.facts_stale,
      children: n.children ? buildTree(n.children) : [],
    };
  });
}

// SectionLogItem / GenStats 类型已抽离至 utils/contentEvents.ts（与纯函数同源，
// 供组件级测试独立引用），页面统一从该处导入，避免类型定义双份漂移。

/** 阶段徽标配色（与后端 _STAGE_MODEL 的阶段标识对应） */
export const STAGE_COLOR: Record<string, string> = {
  context: "default",
  draft: "processing",
  continue: "warning",
  persist: "cyan",
};

/** ✅ 进度增强：毫秒 → 人类可读时长（与后端 _fmt_duration 同口径） */

export function findFirstContentDescendant(node: TreeNode): TreeNode | null {
  // ✅ BUG修复：同时检查 word_count>0，避免有字数但content为空的数据不一致节点被跳过
  if (node.content || (node.word_count || 0) > 0) return node;
  for (const child of node.children || []) {
    const found = findFirstContentDescendant(child);
    if (found) return found;
  }
  return null;
}

export function collectAllKeys(nodes: TreeNode[]): string[] {
  const keys: string[] = [];
  function walk(n: TreeNode) {
    keys.push(n.key);
    n.children?.forEach(walk);
  }
  nodes.forEach(walk);
  return keys;
}

/**
 * ✅ 性能优化：目录树的轻量指纹，用于「内容未变则跳过 setState」的相等性守卫。
 * 背景：正文生成期间每 3s 轮询一次轻量接口刷新目录树，即使数据完全没变也会产生
 * 全新树对象 → setTree 拿到新引用 → treeData / outlineEditTreeData /
 * countGeneratedLeaves / collectAllKeys 全部重算 + 整页重渲（每 3s 一次无效渲染）。
 * 对齐既有 statsEqual 的守卫思路：指纹相同就返回旧引用，React 直接跳过重渲。
 *
 * 只取影响渲染与用户可感知的字段（遍历顺序 / 字数 / 状态），刻意不含 content
 * 大字段（轻量轮询本就不返回它）；用短字符串拼接而非 JSON.stringify，
 * 避免每 3s 序列化整棵树。
 */
export function treeFingerprint(nodes: TreeNode[]): string {
  let out = "";
  function walk(ns: TreeNode[]) {
    for (const n of ns) {
      // ✅ 2026-09-29：facts_stale 参与指纹 —— 它是用户可感知的状态（目录树上
      // 的「事实已变更」徽标）。若不含它，3s 轻量轮询会因指纹相同而保留旧树，
      // 事实刚变更后的徽标要等到下一次完整 load() 才出现（表现为「标了却没显示」）。
      out += `${n.key}:${n.word_count}:${n.status}:${n.facts_stale ? 1 : 0};`;
      if (n.children?.length) walk(n.children);
    }
  }
  walk(nodes || []);
  return out;
}

/**
 * ✅ 编辑增强：把指定 key 的树节点滚动到可视区域中央（平滑滚动）。
 * 兼容"落库后 load 重建 DOM"的异步场景——若首次查询未命中节点元素，
 * 通过 requestAnimationFrame 重试若干次，直到节点出现在树中再滚动。
 * 仅做视图定位，不改动任何状态。
 */
export function scrollToNodeInContainer(container: HTMLElement | null, key: React.Key, tries = 10) {
  if (!container || tries <= 0) return;
  const sel = `[data-key="${CSS.escape(String(key))}"]`;
  const el = (container.querySelector(`.ant-tree-node-content-wrapper${sel}`) ||
    container.querySelector(sel)) as HTMLElement | null;
  if (el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  requestAnimationFrame(() => scrollToNodeInContainer(container, key, tries - 1));
}

/** 目录树中是否已有任意章节正文（用于"重新生成目录"的覆盖风险确认） */
export function hasAnyContent(nodes: TreeNode[]): boolean {
  for (const n of nodes) {
    if (n.content || (n.word_count || 0) > 0) return true;
    if (n.children?.length && hasAnyContent(n.children)) return true;
  }
  return false;
}

/** 目录树中已有正文的叶子章节数（用于上传替换目录时提示影响范围） */
export function treeNodesWithContent(nodes: TreeNode[]): number {
  let count = 0;
  const walk = (ns: TreeNode[]) => {
    for (const n of ns) {
      if (n.content || (n.word_count || 0) > 0) count++;
      if (n.children?.length) walk(n.children);
    }
  };
  walk(nodes || []);
  return count;
}

/**
 * ✅ 计算目录树的最大层级（迭代实现 + 节点数保护）。
 * 用于拖拽/新增时判断是否会产出超过三级上限的节点——旧实现对此无校验，
 * 深层节点只能在点「保存目录」时被后端静默裁剪，用户看到节点"消失"。
 */
export function outlineTreeDepth(nodes: TreeNode[]): number {
  let max = 0;
  let guard = 0;
  const stack: { node: TreeNode; level: number }[] = (nodes || []).map((n) => ({
    node: n,
    level: 1,
  }));
  while (stack.length && guard++ < 50000) {
    const { node, level } = stack.pop()!;
    if (level > max) max = level;
    for (const c of node.children || []) stack.push({ node: c, level: level + 1 });
  }
  return max;
}

/** 就地更新树中指定节点的字段（压缩完成后刷新字数/正文用） */
export function updateNodeFields(
  nodes: TreeNode[],
  key: string,
  fields: Partial<TreeNode>,
): TreeNode[] {
  return nodes.map((n) => {
    if (n.key === key) return { ...n, ...fields };
    if (n.children?.length) {
      return { ...n, children: updateNodeFields(n.children, key, fields) };
    }
    return n;
  });
}

// ✅ any 清理（2026-11-02）：目录行契约 —— 后端 outline / save-outline 载荷的
//    前端消费投影（只声明本模块读写的键，其余字段由调用方各自持有）。
//    OutlineRow（children 可选）用于入参与测试夹具；OutlineRowWithChildren
//    （children 必有）用于 treeToOutline / renumberOutline 的返回形态。
export interface OutlineRow {
  id?: string | number;
  title: string;
  level?: number;
  sort_order?: number;
  word_budget?: number;
  description?: string;
  __original_id?: string;
  outlineId?: string;
  children?: OutlineRow[];
}

export interface OutlineRowWithChildren extends OutlineRow {
  children: OutlineRow[];
}

/** outlineToTreeNode 入参投影：children 只做「是否为数组」探测，元素形状递归自证 */
export interface OutlineDirtyRow {
  id?: string | number;
  title: string;
  level?: number;
  word_budget?: number;
  description?: string;
  __original_id?: string;
  children?: unknown;
}

/** 统计目录树节点总数（含所有层级；纯函数，供导入识别与导出复用） */
export function countOutline(outline: unknown, depth = 0): number {
  if (depth > 50) return 0;
  const rows = outline as unknown[] | null | undefined;
  if (!rows?.length) return 0;
  return rows.reduce<number>(
    (acc, n) =>
      acc + 1 + countOutline((n as OutlineDirtyRow | null | undefined)?.children || [], depth + 1),
    0,
  );
}

/** 把后端「整理为标准结构」报告折算成人话（匹配/补全/补充章节统计） */
export function describeReorganizeReport(report: unknown): string {
  if (!report || typeof report !== "object") return "";
  const r = report as {
    matched_chapters?: unknown;
    standard_chapters?: unknown;
    kept_standard_skeleton?: unknown;
    appended_extras?: unknown;
    template?: unknown;
  };
  const matched = Number(r.matched_chapters) || 0;
  const standard = Number(r.standard_chapters) || 0;
  const skeleton = Number(r.kept_standard_skeleton) || 0;
  const extras = Number(r.appended_extras) || 0;
  const tpl = r.template ? `（模板 ${r.template}）` : "";
  const bits = [`匹配 ${matched}/${standard} 章`];
  if (skeleton > 0) bits.push(`补全空骨架 ${skeleton} 章`);
  if (extras > 0) bits.push(`${extras} 章归入「补充章节」`);
  return `已按标准章节骨架整理${tpl}：${bits.join("，")}`;
}

export type UploadOutlineSummary = {
  /** 是否得到可应用的目录（outline 非空）——空结果时绝不替换用户当前目录树 */
  ok: boolean;
  level: "success" | "warning";
  /** 主提示文案（成功=识别完成统计，失败=后端给出的原因） */
  message: string;
  /** 附加告警（内容不完整/截断/归位统计），逐条如实告知用户 */
  notices: string[];
  outline: OutlineRow[];
  nodeCount: number;
  reorganized: boolean;
  /** ✅ R179（D-40）：后端 AI 兜底识别链路「截断字数」（>0 表示仅部分文本参与识别） */
  truncatedChars: number;
  /** ✅ R179（D-40）：规则识别不足、走了 AI 兜底识别 */
  usedAiFallback: boolean;
};

/**
 * 「导入目录（智能识别）」结果诊断（纯函数，可单测）。
 *
 * ✅ 补齐数据传递链路（2026-09-20）：后端 parse_outline 早已回传
 *    empty_text / warning / parse_warnings / parse_truncated /
 *    reorganized / reorganize_report，但旧前端只读 outline / file_name，
 *    导致：① 扫描件无 OCR 返回空目录时仍误报「识别完成：共 0 个章节」
 *    并清空用户当前目录树；② 「整理为标准结构」归位统计对用户不可见。
 *    这里把「响应 → 用户可读提示」的折算收口成纯函数，供交互链路复用。
 */
export function summarizeUploadOutlineResult(data: unknown): UploadOutlineSummary {
  const d = (data || {}) as {
    outline?: unknown;
    reorganized?: unknown;
    parse_warnings?: unknown;
    reorganize_report?: unknown;
    warning?: unknown;
    file_name?: unknown;
    // ✅ R179（D-40）：AI 兜底识别的两个降级信号（upload_outline.py B2-2 起回传）
    truncated_chars?: unknown;
    used_ai_fallback?: unknown;
  };
  const outline: OutlineRow[] = Array.isArray(d.outline) ? (d.outline as OutlineRow[]) : [];
  const nodeCount = countOutline(outline);
  const reorganized = Boolean(d.reorganized);
  const notices: string[] = [];

  if (Array.isArray(d.parse_warnings)) {
    for (const w of d.parse_warnings) {
      if (typeof w === "string" && w.trim()) notices.push(w.trim());
      else if (w && typeof w === "object" && w.message) notices.push(String(w.message));
    }
  }
  if (d.reorganize_report) {
    const desc = describeReorganizeReport(d.reorganize_report);
    if (desc) notices.push(desc);
  }
  // ✅ R179（D-40）：消费后端 AI 兜底识别的两个降级信号。此前只进响应体、
  //    前端零展示 —— 用户误以为整份文件都参与了 AI 识别（承诺落空）。
  //    两者都走 notices（不阻断应用目录），与 parse_warnings 同层如实告知。
  const truncatedChars = Number(d.truncated_chars ?? 0);
  if (Number.isFinite(truncatedChars) && truncatedChars > 0) {
    notices.push(
      `文件较长，仅部分内容参与 AI 识别（截断 ${truncatedChars} 字），识别结果可能遗漏后文章节`,
    );
  }
  if (d.used_ai_fallback === true) {
    notices.push("规则识别结果不足，已用 AI 兜底识别，建议人工核对章节层级");
  }

  // 空结果：以「不替换目录树」为前提，如实回传后端给出的失败原因
  if (nodeCount === 0) {
    const warn =
      typeof d.warning === "string" && d.warning.trim()
        ? d.warning.trim()
        : "未识别到目录结构，请检查文件是否为扫描件（需 OCR）或格式是否受支持";
    return {
      ok: false,
      level: "warning",
      message: warn,
      notices,
      outline: [],
      nodeCount: 0,
      reorganized,
      truncatedChars,
      usedAiFallback: d.used_ai_fallback === true,
    };
  }

  // 非空但原文被截断（raw_text_truncated / parse_truncated）：仍可用，补充告警
  if (typeof d.warning === "string" && d.warning.trim()) notices.unshift(d.warning.trim());
  const name = d.file_name || "文件";
  const suffix = reorganized ? "，已按标准章节骨架整理" : "";
  return {
    ok: true,
    level: "success",
    message: `识别完成：${name}，共 ${nodeCount} 个章节，可在下方调整后保存${suffix}`,
    notices,
    outline,
    nodeCount,
    reorganized,
    truncatedChars,
    usedAiFallback: d.used_ai_fallback === true,
  };
}

// ============================================================
// 全局事实 Tab · 模块级可测单元（2026-09-21 拆出，行为由 factsTab.test 钉住）
// ============================================================
/** 筛选命中判定：当前 filter 在 groups 里是否存在可展示条目（悬空回退唯一事实源） */

export function flattenTreeKeys(nodes: TreeNode[]): string[] {
  const keys: string[] = [];
  const walk = (ns: TreeNode[]) => {
    ns.forEach((n) => {
      keys.push(n.key);
      if (n.children?.length) walk(n.children);
    });
  };
  walk(nodes);
  return keys;
}

/**
 * ✅ 目录编辑增强：同级上移/下移。
 * 在当前层（含递归子层）定位 key 所在兄弟数组，与相邻 sibling 交换位置。
 * - dir=-1 上移；dir=1 下移。
 * - 已在同层首/尾（无相邻 sibling）时返回 null（调用方据此禁用按钮，不触发移动）。
 * 仅交换兄弟顺序、不改层级与父子关系，符合"上下移动章节"语义。
 */
export function moveSiblingInTree(nodes: TreeNode[], key: string, dir: -1 | 1): TreeNode[] | null {
  const idx = nodes.findIndex((n) => n.key === key);
  if (idx !== -1) {
    if (idx + dir < 0 || idx + dir >= nodes.length) return null;
    const copy = [...nodes];
    [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]];
    return copy;
  }
  for (const n of nodes) {
    if (n.children && n.children.length) {
      const res = moveSiblingInTree(n.children, key, dir);
      if (res) {
        return nodes.map((x) => (x.key === n.key ? { ...x, children: res } : x));
      }
    }
  }
  return null;
}

/** 检测目录树中是否存在超过 3 级的深层节点（后端落库时会被裁剪合并） */
export function hasDeepOutlineNodes(nodes: TreeNode[], depth = 1): boolean {
  if (depth > MAX_OUTLINE_DEPTH) return true;
  return nodes.some(
    (n) => (n.children?.length || 0) > 0 && hasDeepOutlineNodes(n.children!, depth + 1),
  );
}

/**
 * ✅ 单一口径：未落库「本地临时节点」的 key 前缀全集。
 *
 * 目录树里存在两类尚未写入 sections 表的临时 key：
 *   · `local_`  —— 目录树中手工「新增一级章节 / 新增子章节 / 新增同级」产生的节点；
 *   · `upload_` —— 「导入目录（智能识别）」用识别结果**替换本地树**时构造的节点
 *                   （outlineToTreeNode，见下方模块级实现）。
 *
 * ✅ BUG 根因修复（2026-09-21）：旧实现只认 `local_` 一种前缀，于是
 * 「导入目录」替换后的整棵临时树被各处误判为「已落库章节」，四处口径同时漂移：
 *   1. hasUnsavedLocalNodes 漏检 → 顶部橙色 Tag 与左侧 Alert 都不显示，
 *      用户不知道整棵树都还没保存；
 *   2. deleteNode 误调 `DELETE /sections/{upload_...}` → 后端 404，
 *      异常中断在 `setTree(removeNode(...))` 之前 → **本地节点根本删不掉**；
 *   3. renameNode 误调 `PATCH /sections/{upload_...}` → 404，
 *      被 `.catch(() => {})` 吞掉 → 本地改名看似成功、服务端无任何记录；
 *   4. moveNode 因 `hasUnsavedLocal=false` 走「立即 /reorder」分支，
 *      `executemany UPDATE ... WHERE id=?` 对不存在的 upload_ id 静默 0 行，
 *      却弹出「编号已实时更新（已同步到服务端）」——**假成功**，刷新后回到旧顺序。
 * 统一收口到 isUnsavedLocalKey，四处调用点共用同一判定，禁止再各写一份前缀判断。
 */
export const UNSAVED_KEY_PREFIXES = ["local_", "upload_"] as const;

/** 判断某节点 key 是否属于「尚未落库的本地临时节点」（见 UNSAVED_KEY_PREFIXES 说明） */
export function isUnsavedLocalKey(key: string): boolean {
  const k = String(key || "");
  return UNSAVED_KEY_PREFIXES.some((p) => k.startsWith(p));
}

/**
 * ✅ B11 修复：全树递归检测未保存的本地临时节点（见 UNSAVED_KEY_PREFIXES）。
 * 旧实现只查根层（tree.some），嵌套在二/三级下的新增节点会被漏检 ——
 * moveNode/拖拽随即使 /reorder 立即持久化，未落库的新增章节被丢失。
 * 现同时覆盖 local_（手工新增）与 upload_（导入识别替换）两类临时 key。
 */
export function hasUnsavedLocalNodes(nodes: TreeNode[]): boolean {
  for (const n of nodes || []) {
    if (isUnsavedLocalKey(n.key)) return true;
    if (n.children?.length && hasUnsavedLocalNodes(n.children)) return true;
  }
  return false;
}

/**
 * 目录树 → 提交给 /save-outline 的 outline 结构（纯函数，可单测）。
 *
 * 关键约定：`id` 直接取节点 key —— 已落库章节是 DB 主键（UUID），
 * 临时节点是 local_/upload_ 前缀 key。后端 `_save_outline_to_db` 先按
 * `__original_id` 判定 is_new，再统一 renumber，故此处不做编号改写。
 */
export function treeToOutline(nodes: TreeNode[]): OutlineRowWithChildren[] {
  return nodes.map((n, i) => ({
    id: n.key,
    // ✅ BUG 修复（2026-09-26）：原样透传 __original_id（后端 AI 调整目录
    //    /adjust-outline 回传的真实 DB 主键）。缺失时后端 _save_outline_to_db
    //    会退化成"按 id 字段匹配"——而此时 id 已被 renumberOutline 之前的
    //    编号语义污染，is_new 全为 True → 整表重建、已生成正文全部丢失。
    //    有 __original_id 时后端优先按它匹配已有 section。
    ...(n.__original_id ? { __original_id: n.__original_id } : {}),
    title: n.title,
    level: n.level,
    sort_order: i,
    word_budget: n.word_budget,
    description: n.description || "",
    children: n.children && n.children.length ? treeToOutline(n.children) : [],
  }));
}

/**
 * 按位置重排 outline 的展示编号（纯函数，可单测）。
 *
 * 只写前端展示字段 `outlineId`（点分路径）与 `sort_order`，**不改 `id`**：
 * `id` 是「已落库章节的 DB 主键 / 临时节点 key」的载体，被后端用于
 * 智能保留正文；编号由后端 `renumber_outline` 落库时统一生成。
 */
export function renumberOutline(outline: OutlineRow[], parentKey = ""): OutlineRowWithChildren[] {
  return outline.map((node, idx) => {
    const newKey = parentKey ? `${parentKey}.${idx + 1}` : String(idx + 1);
    return {
      ...node,
      sort_order: idx,
      outlineId: newKey,
      children: node.children ? renumberOutline(node.children, newKey) : [],
    };
  });
}

/**
 * 「导入目录（智能识别）」/「AI 调整目录」结果 → 本地临时树（纯函数，可单测）。
 *
 * ✅ 编号由「位置」推导（与后端 renumber_outline 同口径），而不是直接用
 * n.id —— 识别结果里的 id 常为 "n1"/任意值，直接当编号用会让目录树完全不
 * 显示编号（保存前后观感不一致）。
 *
 * key 的三档口径（顺序即优先级，缺一即造成正文丢失）：
 *   1. `__original_id` —— 后端 AI 调整目录回传的**真实 DB 主键**。
 *      其 id 字段已被 renumber 覆写成展示编号（"1"/"1.1"），主键只在该字段。
 *   2. `upload_` 前缀 —— 尚未落库的本地临时节点（导入识别结果 / AI 新增章）。
 *      必须在「id 是纯数字展示编号」时优先于 id：编号 "3" 既不是 DB 主键、
 *      也不带 local_/upload_ 前缀 → isUnsavedLocalKey 判不出「未保存」，
 *      保存时后端又匹配不到任何 section → 整表重建、正文丢失。
 *   3. `n.id` —— 仅当它**不是**纯数字编号时才可用（如识别结果的 "n1"）。
 *
 * ✅ BUG 修复（2026-09-26 · 临时节点 key 冲突）：旧实现用
 * `upload_${Date.now()}_${i}`，`i` 只是**同父内序号** —— 整棵树在一次
 * `Date.now()` 毫秒内构造完（几十节点很常见，性能优化后更快），于是
 * 「第1章的第1个子节」与「第2章的第1个子节」得到**完全相同的 key**
 * （`upload_1767..._0`）。后果不是"仅 React 警告"：
 *   · renameInNode / updateNodeFields / removeNode 按 key 匹配**所有**节点
 *     → 改一个章节标题，另一章跟着一起改；
 *   · findSectionById 只返回第一个命中 → 改名/删除/字数预算作用在错误的章节上；
 *   · antd Tree 重复 key 渲染错乱，collectAllKeys(expandedKeys) 也出现重复项。
 * 现改用**位置路径**（outlineId 点分路径，树内天然唯一）生成 key：
 * 唯一、稳定（同一棵树重复构造得到同一批 key，便于 diff 与测试锁定）。
 */
export const _PURE_NUMERIC_ID_RE = /^\d+(?:\.\d+)*$/;

export function outlineToTreeNode(
  outline: OutlineDirtyRow[],
  parentPath = "",
  parentLevel = 0,
): TreeNode[] {
  return (outline || []).map((n, i) => {
    const level = n.level || parentLevel + 1;
    const outlineId = parentPath ? `${parentPath}.${i + 1}` : String(i + 1);
    const hasChildren = Array.isArray(n.children) && n.children.length > 0;
    // ✅ 判定「n.id 是否只是展示编号」：纯数字点分路径（"1"/"1.1"）一律视为
    //    编号而非主键 —— 后端 renumber 的产物必然落在此形态，必须走 upload_ 分支。
    const rawId = n.id == null ? "" : String(n.id);
    const isNumericId = _PURE_NUMERIC_ID_RE.test(rawId);
    const key = n.__original_id || (rawId && !isNumericId ? rawId : `upload_${outlineId}`);
    return {
      key,
      // 透传 __original_id，供 treeToOutline 原样回传给后端匹配正文
      ...(n.__original_id ? { __original_id: n.__original_id } : {}),
      title: n.title,
      level,
      status: "empty",
      word_count: 0,
      word_budget: n.word_budget || 1200,
      description: n.description || "",
      outlineId,
      children: hasChildren
        ? outlineToTreeNode(n.children as OutlineDirtyRow[], outlineId, level)
        : [],
    };
  });
}

/**
 * 本地重算展示用编号（outlineId/level）：拖拽/移动后即时刷新，
 * 仅改展示字段，不动 key 与正文数据。
 */
export function renumberTreeLocally(
  nodes: TreeNode[],
  parentPath = "",
  parentLevel = 0,
): TreeNode[] {
  return nodes.map((n, i) => {
    const level = parentLevel + 1;
    const outlineId = parentPath ? `${parentPath}.${i + 1}` : String(i + 1);
    return {
      ...n,
      level,
      outlineId,
      children: n.children?.length ? renumberTreeLocally(n.children, outlineId, level) : [],
    };
  });
}

/**
 * 目录树卡片右上操作区（2026-09-21 拆出，行为由 outlineTab.test 钉住）：
 * 「下一步 / 新增子章节 / 上移 / 下移 / 删除」全部回调化，禁用口径内聚：
 * 未选中 → 四个操作键禁用；同层首/尾（moveFlags）→ 对应方向禁用。
 */
/**
 * 断线重挂后「部分成果」弹窗文案（模块级导出，行为由 outlineTab.test 钉住）。
 *
 * ✅ 口径补齐（2026-09-21）：旧实现 failed / stopped 两个分支各自手拼文案，
 * 且只说「已生成 N 章的目录尚未保存」——**不告诉用户为什么中断**。
 * 现在 checkpoint 白名单已回传 `event` / `partial`（见后端
 * `_CHECKPOINT_KINDS.outline_generation`），可据此区分两种语义：
 *   · event=stopped —— 用户主动停止 / 客户端断线兜底：成果本身可信，
 *     建议按需补齐后保存；
 *   · event=error —— AI 调用异常后落库的兜底成果：内容可能不完整，
 *     建议先补齐再考虑重试整章。
 * 收口成纯函数，避免两个消费分支（takeover / SSE 中断重挂）文案再次漂移。
 */
export type PartialOutlineHint = {
  title: string;
  content: string;
  okText: string;
  cancelText: string;
  /** 弹窗视觉基调：warning = 异常中断，info = 用户主动停止 */
  tone: "warning" | "info";
};

export function buildPartialOutlineHint(opts: {
  event?: string;
  partial?: boolean;
  nodeCount?: number;
}): PartialOutlineHint {
  const isErr = opts.event === "error" || opts.event === "failed";
  const n = Math.max(0, Number(opts.nodeCount) || 0);
  const prefix = `检测到已生成 ${n} 个章节节点尚未保存。`;
  const cause = isErr
    ? "本次生成因 AI 服务异常中断，成果可能不完整：建议先按需补齐缺失章节，再考虑重试整章。"
    : "本次生成由用户主动停止或连接中断，已生成部分结果可直接使用：按需补齐后保存即可。";
  return {
    title: isErr ? "目录生成中断，但有部分成果" : "目录生成已停止，但有部分成果",
    content: `${prefix}${cause}`,
    okText: "保存已生成部分",
    cancelText: "不保存",
    tone: isErr ? "warning" : "info",
  };
}

export function findSectionById(nodes: TreeNode[], key: string): TreeNode | null {
  for (const n of nodes) {
    if (n.key === key) return n;
    const found = findSectionById(n.children || [], key);
    if (found) return found;
  }
  return null;
}

// ✅ P0-5 组件拆分：章节日志列表独立 memo 组件——sectionLogs 更新只重渲日志区，
//    不再带动整页其余部分重渲；行按 section_id 稳定 key，避免整表重建

export function countGeneratedLeaves(nodes: TreeNode[]): { done: number; total: number } {
  let done = 0;
  let total = 0;
  const walk = (n: TreeNode) => {
    if (!n.children || n.children.length === 0) {
      total += 1;
      if ((n.word_count || 0) > 0 || (n.content || "").trim()) done += 1;
    } else {
      n.children.forEach(walk);
    }
  };
  nodes.forEach(walk);
  return { done, total };
}

/** 规范符合性检查默认清单（每行一条，前端发送为 string[]，后端交给 AI 逐条判定） */
