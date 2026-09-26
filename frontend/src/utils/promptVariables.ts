
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
