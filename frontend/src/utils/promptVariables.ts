
import type { PromptVariable, PromptVariableDiff } from "../types/prompt";

// 与后端 _registry._VARIABLE_PATTERN 对齐：{var} 总长至少 2，且排除 JSON 对象键 `{key}:`。
const VARIABLE_REGEX = /\{([A-Za-z_]\w{1,})(?!\s*:)\}|__([A-Z][A-Z0-9_]{2,})__/g;

// 与后端 extract_user_variables 对齐：{SHARED_*} 是运行时由后端
// _resolve_shared_keys 动态解析的共享片段引用，并非调用方需提供的变量，
// 不应进入编辑器的「变量」展示 / diff。
function isSharedReference(name: string): boolean {
  return name.startsWith("SHARED_");
}

export function extractPromptVariables(template: string): PromptVariable[] {
  const map = new Map<string, boolean>();
  let match: RegExpExecArray | null;
  const re = new RegExp(VARIABLE_REGEX.source, "g");
  while ((match = re.exec(template || "")) !== null) {
    const name = match[1] || match[2];
    if (!name || isSharedReference(name)) continue;
    if (match[1]) map.set(match[1], false);
    else if (match[2]) map.set(match[2], true);
  }
  return Array.from(map.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, dunder]) => ({ name, dunder }));
}

export function diffPromptVariables(current: string, baseline: string): PromptVariableDiff {
  const before = new Set(extractPromptVariables(baseline).map((item) => item.name));
  const after = new Set(extractPromptVariables(current).map((item) => item.name));
  return {
    added: [...after].filter((name) => !before.has(name)).sort(),
    removed: [...before].filter((name) => !after.has(name)).sort(),
  };
}

/**
 * 「整行独占占位符」判据 —— 与后端 `_registry._OPTIONAL_BLOCK_RE` **逐项一致**。
 *
 * 语义：模板里单独占一整行的 `{xxx}` 是**可选区块**，调用方未传值时
 * `render_prompt` 会把整行丢弃（有值才注入这一段）；行内混排
 * （如 `【方案名称】：{scheme_name}`）不删行，空串降级为 `【方案名称】：`。
 *
 * ✅ 2026-09-27 新增：后端这条判据历史上在**两个方向**分叉过 ——
 * 渲染侧删行用 `[A-Za-z0-9_]*`（≥1 字符）、校验侧豁免用 `[A-Za-z_]\w{1,}`
 * （≥2 字符），导致单字符变量 `{x}`「被删行却仍报缺失变量」。后端已把两处
 * 收敛到同一常量，本前端函数与之保持一致，编辑器据此可提示用户
 * 「这个变量不传就会整段消失」。
 */
const OPTIONAL_BLOCK_LINE_RE = /^[ \t]*\{([A-Za-z_]\w*)\}[ \t]*$/;

export function isOptionalBlockLine(line: string): boolean {
  return OPTIONAL_BLOCK_LINE_RE.test(line ?? "");
}

/** 模板中所有「整行独占」的可选区块变量名（去重、排序）。 */
export function extractOptionalBlockVars(template: string): string[] {
  const out = new Set<string>();
  for (const line of (template || "").split("\n")) {
    const m = OPTIONAL_BLOCK_LINE_RE.exec(line);
    if (m) out.add(m[1]);
  }
  return [...out].sort();
}
