"""R6 回归测试：提示词模板变量误报过滤。

背景：logs/backend.log 中曾出现 40 次同一签名告警
    "Prompt 'content_generation_system' has missing variables: ['max']"
这是 _VARIABLE_PATTERN 把 JSON 示例（如 {max: 200}）里的字段名误当作变量占位符。
误报会淹没真实错误（如 scheme_name 真遗漏），且每次正文生成都触发一次 → 40 次噪声。

修复：validate_prompt_variables() 增加误报过滤：
- 白名单里的短字段名（max / min / id / type 等常见 JSON 键）
- **同时**在模板中出现在 JSON 上下文（含冒号或方括号）
- 才视为误报，不返回；否则保留。

业务语义变量（如 scheme_name / section_number / standards_text）不在白名单，
即使看起来在 JSON 附近也会正常返回缺失。
"""
from __future__ import annotations

import pytest


def _reg_with_value(key: str, value: str):
    """注册测试用提示词。"""
    from app.services.ai.prompts._registry import _reg
    return _reg(key, "test", key, value)


def test_max_in_json_example_is_filtered():
    """{max: 200} 类 JSON 示例里的 max 应该被过滤掉，不算缺失变量。"""
    from app.services.ai.prompts._registry import validate_prompt_variables

    _reg_with_value(
        "r6_test_json_max",
        "参数配置：{\"max\": 200, \"min\": 10}\n\n请按配置生成：{scheme_name}",
    )
    # 只传 scheme_name，不传 max/min
    missing = validate_prompt_variables("r6_test_json_max", scheme_name="测试方案")
    assert "max" not in missing, f"JSON 示例里的 max 应被过滤，但返回了 {missing}"
    assert "min" not in missing, f"JSON 示例里的 min 应被过滤，但返回了 {missing}"


def test_real_business_variable_is_not_filtered():
    """业务语义变量（scheme_name / section_number 等）即使出现在类似 JSON 上下文也应保留。"""
    from app.services.ai.prompts._registry import validate_prompt_variables

    _reg_with_value(
        "r6_test_real_var",
        "为 {scheme_name} 章节 {section_number} 生成正文",
    )
    missing = validate_prompt_variables("r6_test_real_var")
    assert "scheme_name" in missing
    assert "section_number" in missing


def test_short_var_without_json_context_is_not_filtered():
    """短字段名在**没有** JSON 上下文时不应被误过滤。"""
    from app.services.ai.prompts._registry import validate_prompt_variables

    _reg_with_value(
        "r6_test_short_no_json",
        "请使用 max 数量的样本进行训练。",  # 注意：这里 max 不在花括号里
    )
    # 上面文本不含 {max} 占位符，所以 required 应该是空的
    missing = validate_prompt_variables("r6_test_short_no_json")
    assert "max" not in missing  # 本来就不在 required


def test_plain_placeholder_still_reported():
    """普通 {placeholder} 不带 JSON 上下文时仍然报缺失（不回归）。"""
    from app.services.ai.prompts._registry import validate_prompt_variables

    _reg_with_value(
        "r6_test_plain",
        "这是给 {scheme_name} 的正文模板。",
    )
    missing = validate_prompt_variables("r6_test_plain")
    assert "scheme_name" in missing


def test_max_in_plain_text_not_json_is_not_filtered():
    """{max} 单独出现（无 JSON 上下文，无 LaTeX 下标）应该保留告警。

    中文冒号不算 JSON 特征 —— 这个用例锁定"过度抑制"边界：
    只有真正的 JSON/LaTeX 上下文才过滤，普通自然语言文本仍应告警。
    """
    from app.services.ai.prompts._registry import validate_prompt_variables

    _reg_with_value(
        "r6_test_max_plain",
        "最大数量：{max}\n请按该数量生成。",
    )
    missing = validate_prompt_variables("r6_test_max_plain")
    # 无 ASCII 冒号/方括号，也无 LaTeX 前缀 { —— _is_false_positive 应返回 False
    # 因此 max 保留在 missing 里
    assert "max" in missing, f"无 JSON/LaTeX 上下文的 max 应保留告警，但 missing={missing}"


def test_max_in_latex_subscript_is_filtered():
    """{max} 出现在 LaTeX 下标 $p_{max}$ 中应被识别为无需替换（不告警）。"""
    from app.services.ai.prompts._registry import validate_prompt_variables

    # 真实场景：content.py 里 "正确写法 $p_{max}$【待补充：基底最大压力】kPa"
    _reg_with_value(
        "r6_test_latex_max",
        "正确写法 `$p_{max}$【待补充：基底最大压力】kPa`",
    )
    missing = validate_prompt_variables("r6_test_latex_max")
    assert "max" not in missing, f"LaTeX 下标里的 max 应被过滤，但返回 {missing}"
