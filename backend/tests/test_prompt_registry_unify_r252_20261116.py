# -*- coding: utf-8 -*-
"""提示词注册表收口 · R252（2026-11-16）护栏。

收口对象：此前绕过统一注册表、无法后台编辑 / 无法 DB 定制 / 不进审计与指标的
「模板散落」三处 —— 正文生成标准 8 条片段（``content_standard.py``）与文种识别
system（``genre_assist.py``）。

四组断言（对齐 R39 · T1 的注册语义）：
  A 注册完备    —— 9 个新 key 已进 ``_ALL_PROMPTS``，出厂 ``default_content`` 与
                   源模块字面量**逐字节相等**（默认行为零变化的基石）。
  B 默认零漂移  —— 各 ``build_*`` builder 在默认配置下输出与源模块字面量逐字节
                   相等，且非法 standard 仍回落 precise（旧行为不变）。
  C DB 定制可达 —— monkeypatch ``_prompt_cache`` 后 builder 必须返回定制内容
                   （行为实证「服务层就绪 = 功能可达」，而非仅注册）。
  D 契约零告警  —— 新 key 声明 ``requires=[]``，``check_prompt_variables`` 不因
                   本批模板产生任何 issue。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# A · 注册完备 + 出厂默认逐字节相等
# ---------------------------------------------------------------------------
EXPECTED_KEYS = {
    "content_standard_system_precise": ("_SYSTEM_BLOCKS", "precise"),
    "content_standard_system_fuzzy": ("_SYSTEM_BLOCKS", "fuzzy"),
    "content_standard_user_precise": ("_USER_BLOCKS", "precise"),
    "content_standard_user_fuzzy": ("_USER_BLOCKS", "fuzzy"),
    "content_standard_facts_header_precise": ("_FACTS_HEADERS", "precise"),
    "content_standard_facts_header_fuzzy": ("_FACTS_HEADERS", "fuzzy"),
    "content_standard_continue_hint_precise": ("_CONTINUE_HINTS", "precise"),
    "content_standard_continue_hint_fuzzy": ("_CONTINUE_HINTS", "fuzzy"),
}

# 事实补全规则骨架（参数化，单独断言：出厂 default 是含 {label}/{missing_note} 的骨架）。
FACTS_KEY = "facts_completeness_rules"


def _registry():
    from app.services.ai.prompts._registry import _ALL_PROMPTS

    return _ALL_PROMPTS


def test_all_ten_new_keys_registered():
    reg = _registry()
    for key in EXPECTED_KEYS:
        assert key in reg, f"生成标准提示词未注册: {key}"
    assert "genre_assist_system" in reg
    assert FACTS_KEY in reg


def test_factory_defaults_are_byte_identical_to_source_literals():
    """出厂 default_content 必须与源模块字面量逐字节相等（默认行为零变化的基石）。"""
    from app.services import content_standard as cs
    from app.services import genre_assist as ga

    reg = _registry()
    for key, (dict_name, mode) in EXPECTED_KEYS.items():
        literal = getattr(cs, dict_name)[mode]
        assert reg[key]["default_content"] == literal, key
        assert reg[key]["content"] == literal, key
    assert reg["genre_assist_system"]["default_content"] == ga._SYSTEM


def test_facts_completeness_default_is_parameterized_skeleton():
    """事实补全骨架的出厂 default 含 {label}/{missing_note}，variables 正确提取。"""
    from app.services import facts_patches as fp

    reg = _registry()
    assert reg[FACTS_KEY]["default_content"] == fp._COMPLETENESS_TEMPLATE
    assert reg[FACTS_KEY]["requires"] == ["label", "missing_note"]
    assert set(reg[FACTS_KEY]["variables"]) == {"label", "missing_note"}


def test_new_keys_declare_empty_requires_whitelist():
    """无 ``{var}`` 调用方变量，声明空白名单让契约校验监督「编辑误引入占位符」。"""
    reg = _registry()
    for key in (*EXPECTED_KEYS, "genre_assist_system"):
        assert reg[key].get("requires") == [], key
        assert reg[key]["variables"] == [], key


# ---------------------------------------------------------------------------
# B · 默认零漂移
#
# 说明：不能在 import 期把 builder 返回值快照下来（那会先于本护栏固化），
# 故这里直接与源模块字面量逐字节比对 —— 等价于「改造前后逐字节一致」，
# 因为改造前 builder 就是 ``dict.get(...)`` 直接返回该字面量。
# ---------------------------------------------------------------------------
def test_builders_default_output_matches_literals():
    from app.services import content_standard as cs

    for mode in ("precise", "fuzzy"):
        assert cs.build_system_block(mode) == cs._SYSTEM_BLOCKS[mode]
        assert cs.build_user_block(mode) == cs._USER_BLOCKS[mode]
        assert cs.build_facts_header(mode) == cs._FACTS_HEADERS[mode]
        assert cs.build_continue_hint(mode) == cs._CONTINUE_HINTS[mode]


def test_invalid_standard_still_falls_back_to_precise():
    """非法 standard 仍回落 precise（旧行为不变），且不抛异常。"""
    from app.services import content_standard as cs

    assert cs.build_system_block("bogus") == cs._SYSTEM_BLOCKS["precise"]
    assert cs.build_user_block(None) == cs._USER_BLOCKS["precise"]
    assert cs.build_facts_header("") == cs._FACTS_HEADERS["precise"]
    assert cs.build_continue_hint("nonsense") == cs._CONTINUE_HINTS["precise"]


# ---------------------------------------------------------------------------
# C · DB 定制可达（行为实证）
# ---------------------------------------------------------------------------
def test_db_override_reaches_content_standard_builder(monkeypatch):
    """DB 定制 content_standard 片段必须真正生效（不能只注册不接线）。"""
    from app.services import content_standard as cs
    from app.services.ai.prompts import _cache

    monkeypatch.setattr(
        _cache,
        "_prompt_cache",
        {"content_standard_system_precise": "【定制】精准标准已改写"},
    )
    monkeypatch.setattr(_cache, "_loaded_db_path", None)
    assert cs.build_system_block("precise") == "【定制】精准标准已改写"
    # 未定制 mode 仍回退出厂字面量
    assert cs.build_system_block("fuzzy") == cs._SYSTEM_BLOCKS["fuzzy"]


def test_db_override_reaches_genre_assist_system(monkeypatch):
    from app.services import genre_assist as ga
    from app.services.ai.prompts import _cache

    monkeypatch.setattr(_cache, "_prompt_cache", {"genre_assist_system": "【定制】文种 system"})
    monkeypatch.setattr(_cache, "_loaded_db_path", None)
    assert ga._system_prompt() == "【定制】文种 system"


def test_blank_db_content_falls_back_to_factory_default(monkeypatch):
    """用户清空某片段（content=''）时回退出厂默认，不下发空 system（空值污染防护）。"""
    from app.services import content_standard as cs
    from app.services.ai.prompts import _cache

    monkeypatch.setattr(_cache, "_prompt_cache", {"content_standard_user_precise": "   "})
    monkeypatch.setattr(_cache, "_loaded_db_path", None)
    assert cs.build_user_block("precise") == cs._USER_BLOCKS["precise"]


def test_genre_suggest_category_fail_soft(monkeypatch):
    """文种识别辅助：chat 抛错时 fail-soft 返回 None（保持 undetermined，不阻断）。"""
    import asyncio

    from app.services import genre_assist as ga

    async def _boom(*a, **kw):
        raise RuntimeError("provider down")

    out = asyncio.run(ga.suggest_category("深基坑支护专项施工方案", chat_fn=_boom))
    assert out is None


def test_genre_suggest_category_injects_system_prompt(monkeypatch):
    """messages 的 system 角色确实取自 ``_system_prompt()``（注册表接线，非旧字面量）。"""
    import asyncio

    from app.services import genre_assist as ga

    captured = {}

    async def _chat(messages, **kw):
        captured["system"] = messages[0]["content"]
        return '{"category_ids": [], "sub_ids": [], "reason": ""}'

    monkeypatch.setattr(ga, "_SYSTEM", "出厂字面量（不应被直接使用）")
    from app.services.ai.prompts import _cache

    monkeypatch.setattr(_cache, "_prompt_cache", {"genre_assist_system": "【定制】文种 system"})
    monkeypatch.setattr(_cache, "_loaded_db_path", None)
    asyncio.run(ga.suggest_category("塔吊安拆专项施工方案", chat_fn=_chat))
    assert captured["system"] == "【定制】文种 system"


# ---------------------------------------------------------------------------
# D · 契约零告警
# ---------------------------------------------------------------------------
def test_contract_check_reports_no_issue_for_new_keys():
    from app.services.ai.prompts._registry import check_prompt_variables

    assert check_prompt_variables() == []


# ---------------------------------------------------------------------------
# E · 事实补全骨架 · 参数化 key 的行为
# ---------------------------------------------------------------------------
def test_facts_completeness_default_output_unchanged():
    """默认配置下 build_completeness_rules 输出与出厂骨架 .format 结果逐字节相等。"""
    from app.services import facts_patches as fp

    for mode in ("omit", "placeholder"):
        label = fp._COMPLETENESS_LABELS[mode]
        note = fp._COMPLETENESS_MISSING_NOTES[mode]
        expected = fp._COMPLETENESS_TEMPLATE.format(label=label, missing_note=note)
        assert fp.build_completeness_rules(mode) == expected
    assert fp.build_completeness_rules("fabricate") == ""
    assert fp.build_completeness_rules("bogus") == ""


def test_facts_completeness_db_override_and_broken_placeholder_failsoft(monkeypatch):
    """DB 定制生效；定制误删占位符时 fail-soft 回退出厂骨架（事实提取不崩）。"""
    from app.services import facts_patches as fp
    from app.services.ai.prompts import _cache

    label = fp._COMPLETENESS_LABELS["omit"]
    note = fp._COMPLETENESS_MISSING_NOTES["omit"]
    factory = fp._COMPLETENESS_TEMPLATE.format(label=label, missing_note=note)

    # ① 定制有效（占位符完整）→ 生效
    monkeypatch.setattr(
        _cache,
        "_prompt_cache",
        {FACTS_KEY: "定制骨架（{label}）缺值写法：{missing_note}"},
    )
    monkeypatch.setattr(_cache, "_loaded_db_path", None)
    assert fp.build_completeness_rules("omit") == f"定制骨架（{label}）缺值写法：{note}"

    # ② 定制写坏占位符（缺右括号）→ format 抛 ValueError → 回退出厂骨架
    monkeypatch.setattr(_cache, "_prompt_cache", {FACTS_KEY: "定制骨架（{label} 缺值写法：{missing_note"})
    assert fp.build_completeness_rules("omit") == factory
