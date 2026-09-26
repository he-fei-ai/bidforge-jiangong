import { describe, expect, it } from "vitest";
import {
  diffPromptVariables, extractPromptVariables,
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
