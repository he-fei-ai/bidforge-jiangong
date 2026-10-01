import { describe, expect, it } from "vitest";
import {
  diffPromptVariables, extractOptionalBlockVars, extractPromptVariables,
  isOptionalBlockLine,
} from "../utils/promptVariables";

describe("提示词变量治理", () => {
  it("与后端一致地排除 JSON 对象键，同时保留普通占位符", () => {
    const vars = extractPromptVariables('示例 {tasks: []}，正文 {scheme_name}，__RULE__');
    expect(vars).toEqual([
      { name: "RULE", dunder: true },
      { name: "scheme_name", dunder: false },
    ]);
  });

  it("计算相对默认模板新增和删除的变量", () => {
    expect(diffPromptVariables(
      "默认 {scheme_name}，新增 {section_number}",
      "默认 {scheme_name}，旧 {standards_text}",
    )).toEqual({
      added: ["section_number"],
      removed: ["standards_text"],
    });
  });

  it("无变量变化时返回空差异", () => {
    expect(diffPromptVariables("内容 {name}", "默认 {name}")).toEqual({
      added: [], removed: [],
    });
  });

  it("排除 {SHARED_*} 共享片段引用（运行时解析，非调用方变量）", () => {
    // 与后端 extract_user_variables 对齐：SHARED_* 不进入变量展示 / diff
    const vars = extractPromptVariables(
      "{SHARED_FORBIDDEN_WORDS} 正文 {scheme_name} {SHARED_OUTPUT_SPEC}",
    );
    expect(vars).toEqual([{ name: "scheme_name", dunder: false }]);
  });

  it("启用/停用一段 {SHARED_*} 不计为变量增删", () => {
    expect(diffPromptVariables(
      "规则 {SHARED_OUTPUT_SPEC} 正文 {scheme_name}",
      "正文 {scheme_name}",
    )).toEqual({ added: [], removed: [] });
  });
});

// ======================================================================
// 可选区块判据 parity（与后端 _OPTIONAL_BLOCK_RE 逐项一致）
// ======================================================================
describe("可选区块（整行独占占位符）判据", () => {
  it("整行独占的占位符 = 可选区块（未传值时运行时整行丢弃）", () => {
    expect(isOptionalBlockLine("{scheme_basis}")).toBe(true);
    expect(isOptionalBlockLine("  {scheme_basis}  ")).toBe(true);
    expect(isOptionalBlockLine("\t{scheme_basis}  ")).toBe(true);
  });

  it("行内混排不是可选区块（空串降级为「标签：」是可接受行为）", () => {
    expect(isOptionalBlockLine("【方案名称】：{scheme_name}")).toBe(false);
    expect(isOptionalBlockLine("{a} → {b}")).toBe(false);
    expect(isOptionalBlockLine("正文 {a}")).toBe(false);
  });

  it("JSON 对象键不是占位符", () => {
    expect(isOptionalBlockLine('{"max": 60000}')).toBe(false);
  });

  // 回归：后端历史上渲染侧用 ≥1 字符、校验侧用 ≥2 字符，导致单字符
  // 变量 {x} 「被删行却仍报缺失」。统一为 ≥1 字符后两侧必须一致。
  it("单字符变量也算可选区块（后端两个方向已收敛为同一判据）", () => {
    expect(isOptionalBlockLine("{x}")).toBe(true);
    expect(isOptionalBlockLine("{ab}")).toBe(true);
  });

  it("空行 / 纯文本不是可选区块", () => {
    expect(isOptionalBlockLine("")).toBe(false);
    expect(isOptionalBlockLine("   ")).toBe(false);
    expect(isOptionalBlockLine("普通文本")).toBe(false);
  });

  it("extractOptionalBlockVars 收集整行独占变量（去重排序）", () => {
    expect(extractOptionalBlockVars(
      "头部\n{scheme_basis}\n{standards_text}\n尾部\n【事实】：{project_facts}\n{scheme_basis}\n",
    )).toEqual(["scheme_basis", "standards_text"]);
  });

  it("extractOptionalBlockVars 不含行内混排与 SHARED 引用", () => {
    const v = extractOptionalBlockVars(
      "【方案名称】：{scheme_name}\n{SHARED_SCOPE_RULES}\n{scheme_basis}\n");
    expect(v).toEqual(["SHARED_SCOPE_RULES", "scheme_basis"]);
  });

  it("空模板返回空数组", () => {
    expect(extractOptionalBlockVars("")).toEqual([]);
  });
});
