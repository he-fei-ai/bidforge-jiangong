"""提示词模板包。

公共 API：
  - get_prompt(key, **kwargs) -> str  读取并渲染提示词（DB 优先，硬编码回退）
  - render_prompt(template, **kwargs) -> str  统一变量渲染函数
  - reload_prompt_cache()    重新加载缓存
  - _ALL_PROMPTS             全部提示词元信息注册表
  - render(key, **kwargs)    兼容旧接口的渲染函数

✅ R168（2026-11-01 · G5 收口）：``get_prompt_with_validation`` 已下线 ——
全仓零生产调用方（仅本文件导出 + 测试引用），与 ``get_prompt`` 内部已有
的缺变量告警路径完全重复。需要「读取即校验」时直接用
``get_prompt(key)`` + ``validate_prompt_variables(key, **kwargs)`` 组合。
"""

from __future__ import annotations

from . import _shared
from ._cache import (
    get_prompt,
    reload_prompt_cache,
)
from ._registry import (
    _ALL_PROMPTS,
    PROMPT_VARIABLE_CONTRACTS,
    PromptContractError,
    _apply_variable_contracts,
    _reg,
    check_prompt_variables,
    clean_prompt_text,
    extract_user_variables,
    extract_variables,
    get_default_prompt,
    get_prompt_variables,
    has_residual_placeholders,
    is_prompt_modified,
    list_prompts,
    render,
    render_prompt,
    reset_prompt,
    update_prompt,
    validate_prompt_variables,
)

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
# ✅ 2026-10-23（五段式提取方法 · 通用第 3/5 段）：处理规则与验证步骤两条
#    通用纪律，供全部结构化提取任务（bid_item_* 五任务、全局事实提取等）复用。
#    任务定制的「目标定位 / 提取要点 / 输出示例」仍由各任务模板内联。
_reg("SHARED_EXTRACT_RULES", "共享规则", "五段式提取 · 处理规则",
     _shared.SHARED_EXTRACT_RULES)
_reg("SHARED_EXTRACT_VERIFY", "共享规则", "五段式提取 · 验证步骤（输出前自检）",
     _shared.SHARED_EXTRACT_VERIFY)

# ✅ R38 D6（2026-10-03）：SHARED_* 在本包 __init__ 里注册，契约表里的
#    SHARED_* 条目（含显式空声明）必须在本调用之后才能对 meta 生效。
# ✅ R169（2026-11-01 · G6 收口）：本调用同时是惰性钩子（register_lazy_prompts）
#    的**唯一导入期触发点** —— _registry 尾部已不再触发。彼时 _cache/_registry
#    均已完成加载，bid_analysis_service 顶部 `from ._cache import get_prompt`
#    首次即成功，消除了每进程一条的 "partially initialized" WARNING
#    （旧时序：_registry 尾部在自身加载中触发钩子 → bid 撞半初始化的
#    _cache → ImportError 被吞 → 本处重试才成功）。
_apply_variable_contracts()


__all__ = [
    "get_prompt",
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