"""提示词 SHARED_* 共享片段引用不应进入「变量列表」回归（2026-09-24）。

背景：{SHARED_FORBIDDEN_WORDS} / {SHARED_OUTPUT_SPEC} / {SHARED_SCOPE_RULES}
是目录生成提示词里的共享片段引用，由 ``_cache._resolve_shared_keys`` 在
运行时解析为对应共享提示词的当前内容（DB 优先），并非调用方需传参的变量。

旧实现 ``extract_variables`` 把它们一并抓进 variables 列表，导致：
  · 前端提示词编辑器把 SHARED_* 当「必需变量」展示，用户无从满足；
  · PATCH/RESET 审计 diff 把启用/停用一段共享规则误记为「变量增删」。

修复：新增 ``extract_user_variables``（排除 SHARED_*），在注册 / 列表 /
更新 / 重置 / 路由 diff 各处替代裸 ``extract_variables``。
``extract_variables``（含 SHARED_*）保留供残留检测与既有用例。
"""
from __future__ import annotations

import pytest
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    extract_user_variables,
    extract_variables,
    get_prompt_variables,
    validate_prompt_variables,
)
from app.services.ai.prompts._registry import (
    list_prompts as _list_prompts,
)

# 含 SHARED_* 的真实注册模板（outline_short_system 引用了 3 个共享片段）
SHARED_KEYS = {
    "SHARED_FORBIDDEN_WORDS",
    "SHARED_OUTPUT_SPEC",
    "SHARED_SCOPE_RULES",
}
OUTLINE_KEYS_WITH_SHARED = [
    "outline_short_system",
    "outline_level1_system",
    "outline_sublevel_system",
    "outline_patch_system",
    "outline_sublevel_batch_system",
]


class TestExtractUserVariables:
    def test_excludes_shared_references(self):
        tmpl = "{SHARED_FORBIDDEN_WORDS} 正文 {scheme_name}，类型 {scheme_type}"
        assert "SHARED_FORBIDDEN_WORDS" in extract_variables(tmpl)
        assert "SHARED_FORBIDDEN_WORDS" not in extract_user_variables(tmpl)
        assert extract_user_variables(tmpl) == ["scheme_name", "scheme_type"]

    def test_preserves_plain_and_dunder_variables(self):
        tmpl = "{scheme_name} {section_number} __RULE__ {SHARED_OUTPUT_SPEC}"
        out = extract_user_variables(tmpl)
        assert "scheme_name" in out
        assert "section_number" in out
        assert "RULE" in out  # __VAR__ 格式保留
        assert not any(v.startswith("SHARED_") for v in out)

    def test_no_shared_returns_same_as_raw(self):
        tmpl = "普通模板 {a} {b}"
        assert extract_user_variables(tmpl) == extract_variables(tmpl)


class TestRegistryVariablesExcludesShared:
    """注册表 variables 字段不得含 SHARED_*（前端列表直接消费此字段）。"""

    @pytest.mark.parametrize("key", OUTLINE_KEYS_WITH_SHARED)
    def test_outline_prompt_variables_no_shared(self, key):
        meta = _ALL_PROMPTS.get(key)
        assert meta is not None, f"{key} 未注册"
        variables = meta["variables"]
        leaked = [v for v in variables if v.startswith("SHARED_")]
        assert leaked == [], f"{key} 的 variables 不应含 SHARED_*：{leaked}"
        # 真实业务变量仍在
        assert "scheme_name" in variables
        assert "scheme_type" in variables

    def test_raw_extract_still_contains_shared(self):
        """extract_variables（原始）仍含 SHARED_*，供残留检测使用，未被破坏。"""
        tmpl = _ALL_PROMPTS["outline_short_system"]["content"]
        raw = extract_variables(tmpl)
        assert SHARED_KEYS.issubset(set(raw)), "原始提取应仍包含 SHARED_*"


class TestValidateExcludesShared:
    def test_validate_never_reports_shared(self):
        # outline_short_system 声明 4 个业务变量 + 3 个 SHARED_*（原始）
        # validate 不传任何 kwargs 时，缺失列表不得出现 SHARED_*
        missing = validate_prompt_variables("outline_short_system")
        assert not any(v.startswith("SHARED_") for v in missing), \
            f"validate 不得把 SHARED_* 当缺失变量：{missing}"
        # 真实业务变量仍如实报缺
        assert set(missing) >= {"scheme_name", "scheme_type",
                                "construction_scope", "project_facts"}

    def test_get_prompt_variables_excludes_shared(self):
        out = get_prompt_variables("outline_short_system")
        assert not any(v.startswith("SHARED_") for v in out)


class TestListPromptsExcludesShared:
    def test_list_prompts_items_no_shared(self):
        items = {p["key"]: p for p in _list_prompts("outline")}
        for key in OUTLINE_KEYS_WITH_SHARED:
            it = items[key]
            leaked = [v for v in it["variables"] if v.startswith("SHARED_")]
            assert leaked == [], f"{key} 列表 variables 不应含 SHARED_*：{leaked}"


class TestAuditDiffExcludesShared:
    """PATCH/RESET 审计 diff 不得把 SHARED_* 启停记为变量增删。"""

    async def test_toggle_shared_not_counted_as_variable_change(self, db_conn):
        from app import main as app_main
        from app.services.ai.prompts._registry import get_default_prompt

        key = "outline_short_system"
        default = get_default_prompt(key)
        # 仅去掉一段 {SHARED_OUTPUT_SPEC}（不增删任何业务变量）
        changed = default.replace("\n{SHARED_OUTPUT_SPEC}", "\n")
        try:
            res = await app_main.update_prompt(key, {"content": changed}, db=db_conn)
            assert res["ok"] is True
            # 启停共享片段不应被记为变量增删
            assert res["added_variables"] == []
            assert res["removed_variables"] == []
        finally:
            from app.services.ai.prompts._cache import reload_prompt_cache
            from app.services.ai.prompts._registry import reset_prompt
            reset_prompt(key)
            reload_prompt_cache()
