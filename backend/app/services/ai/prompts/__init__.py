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
    _apply_variable_contracts,
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
# ✅ 2026-10-03（R38 · 提示词模块 P1-a）：正文侧红线简档此前在 content.py
#    **导入期**被 .replace() 拷贝进模板（`<<SCOPE_RULES>>`），既没注册也不走
#    _cache._resolve_shared_keys() 的运行时 DB 覆盖 → 用户在提示词编辑器改
#    「SHARED_SCOPE_RULES」时，目录侧 6 个模板全部生效、**正文侧 2 个模板
#    （每章下发一次、38 章即 38 次）完全不变**，且无任何告警。
#    这正是 _cache.py 2026-09-23 修过的老问题（outline 侧的导入期值拷贝）在
#    2026-10-01 重构正文红线时被重新引入。
#    修法：注册为可编辑提示词 + 模板改用 {SHARED_SCOPE_RULES_BRIEF} 占位符，
#    由运行时统一解析。出厂默认内容**逐字不变**（仍取同一常量），
#    故未定制用户的行为零变化。
_reg("SHARED_SCOPE_RULES_BRIEF", "共享规则", "文件性质红线·正文简版（禁投标内容）",
     _shared.SHARED_SCOPE_RULES_BRIEF)

# ✅ R38 D6（2026-10-03）：SHARED_* 在本包 __init__ 里注册，晚于 _registry 尾部
#    的首次 _apply_variable_contracts() —— 旧时序下契约表里的 SHARED_* 条目
#    （含显式空声明）对 meta 永远不生效。注册完成后幂等补套一次（同名重套
#    只写同样的值，零副作用）。
_apply_variable_contracts()


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