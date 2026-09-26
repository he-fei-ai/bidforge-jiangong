"""提示词模板包。

公共 API：
  - get_prompt(key, **kwargs) -> str  读取并渲染提示词（DB 优先，硬编码回退）
  - get_prompt_with_validation(key, **kwargs) -> tuple  读取并校验变量
  - render_prompt(template, **kwargs) -> str  统一变量渲染函数
  - reload_prompt_cache()    重新加载缓存
  - _ALL_PROMPTS             全部提示词元信息注册表
  - render(key, **kwargs)    兼容旧接口的渲染函数
"""

from __future__ import annotations

from ._registry import (
    _ALL_PROMPTS,
    _reg,
    check_prompt_variables,
    clean_prompt_text,
    extract_variables,
    extract_user_variables,
    get_default_prompt,
    get_prompt as _registry_get_prompt,
    get_prompt_variables,
    has_residual_placeholders,
    is_prompt_modified,
    list_prompts,
    PROMPT_VARIABLE_CONTRACTS,
    PromptContractError,
    render,
    render_prompt,
    reset_prompt,
    update_prompt,
    validate_prompt_variables,
)
from ._cache import (
    get_prompt,
    get_prompt_with_validation,
    reload_prompt_cache,
)
from . import _shared

_reg("SHARED_FORBIDDEN_WORDS", "共享规则", "标题禁用词列表", _shared.SHARED_FORBIDDEN_WORDS)
_reg("SHARED_OUTPUT_SPEC", "共享规则", "输出格式规范", _shared.SHARED_OUTPUT_SPEC)
_reg("SHARED_SCOPE_RULES", "共享规则", "文件性质红线（禁投标内容）", _shared.SHARED_SCOPE_RULES)


__all__ = [
    "get_prompt",
    "get_prompt_with_validation",
    "reload_prompt_cache",
    "render_prompt",
    "render",
    "has_residual_placeholders",
    "_ALL_PROMPTS",
    "_reg",
    "get_default_prompt",
    "get_prompt_variables",
    "is_prompt_modified",
    "reset_prompt",
    "validate_prompt_variables",
    "clean_prompt_text",
    "extract_variables",
    "extract_user_variables",
    "check_prompt_variables",
    "PromptContractError",
    "PROMPT_VARIABLE_CONTRACTS",
    "list_prompts",
    "update_prompt",
]