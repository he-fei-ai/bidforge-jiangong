"""标题编号规范（兼容层 — 已拆分为三个模块）

本文件保留为向后兼容的 re-export 层，所有实现已拆分至：
- heading_templates.py：共享常量 + 模板系统 + 工具函数
- heading_v1.py：HeadingNumberingGenerator（V3.1 静态方法版）
- heading_v2.py：HeadingNumberingGeneratorV2（V6.0 实例化版，父级ID追踪）

原有 import 路径 `from app.services.ai.heading_standard import ...` 继续有效。
"""
from app.services.ai.heading_templates import (
    ALPHABET,
    CHINESE_NUMBERS,
    CIRCLED_NUMBERS,
    DEFAULT_NUMBERING_TEMPLATES,
    HEADING_MANAGED_LEVELS,
    HEADING_REGEX_PATTERNS,
    HEADING_STYLE_CONFIG,
    ROMAN_NUMBERS,
    ROMAN_NUMBERS_UPPER,
    build_heading_spec_prompt,
    format_heading_by_id,
    format_outline_number_by_template,
    format_outline_title,
    get_heading_template,
    outline_number_parts,
    should_insert_space_after_number,
)
from app.services.ai.heading_v1 import HeadingNumberingGenerator
from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2

__all__ = [
    "HEADING_MANAGED_LEVELS",
    "CHINESE_NUMBERS",
    "ALPHABET",
    "HEADING_STYLE_CONFIG",
    "HEADING_REGEX_PATTERNS",
    "CIRCLED_NUMBERS",
    "ROMAN_NUMBERS",
    "ROMAN_NUMBERS_UPPER",
    "DEFAULT_NUMBERING_TEMPLATES",
    "outline_number_parts",
    "format_outline_number_by_template",
    "get_heading_template",
    "format_heading_by_id",
    "should_insert_space_after_number",
    "format_outline_title",
    "build_heading_spec_prompt",
    "HeadingNumberingGenerator",
    "HeadingNumberingGeneratorV2",
]
