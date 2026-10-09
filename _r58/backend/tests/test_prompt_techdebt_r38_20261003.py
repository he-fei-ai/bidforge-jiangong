# -*- coding: utf-8 -*-
"""R38 遗留技术债收口护栏（2026-10-03 · D2/D4/D6 + 契约表补全登记）。

覆盖三块收口 + 一组登记：
  D2  修复侧「不截断正文」纪律 + 整章重写超长硬上限（省必败调用，
      用户可见结果与旧行为一致：仍判 failed）。
  D4  project_facts 提示词预算单一事实源（数值逐字不变，消灭魔法数）。
  D6  零占位模板「显式空声明」语义接入契约校验 + ILLUSTRATION_* 死代码删除。
  登记  consistency_repair_edits_user / facts_finalize_system /
      facts_knowledge_patch_system 接入 PROMPT_VARIABLE_CONTRACTS。
"""
import asyncio
import re

import pytest
from app.services import repair_agent
from app.services.ai.prompts import _registry as R
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    PROMPT_VARIABLE_CONTRACTS,
    check_prompt_variables,
)
from app.services.ai.prompts.illustration import ILLUSTRATION_PROMPT_OPTIMIZE  # noqa: F401


# =========================================================================
# D2 · 整章重写超长硬上限
# =========================================================================
class TestRepairRewriteHardCap:

    def test_constant_value(self):
        """阈值对齐扫描侧分片上限 SECTION_CHUNK_LIMIT=12000（数值即口径）。"""
        assert repair_agent.REPAIR_REWRITE_MAX_CHARS == 12000

    @pytest.mark.asyncio
    async def test_oversized_section_skips_rewrite_call(self, monkeypatch):
        """超长章：定点编辑照跑，整章重写**不得**再发起第二次 AI 调用。"""
        calls: list = []

        async def fake_chat(messages, **kwargs):
            calls.append(kwargs.get("scene", ""))
            return "不是 JSON"  # 定点编辑解析失败 → 回落整章重写路径

        monkeypatch.setattr(repair_agent, "chat_with_fallback", fake_chat)
        content = "正文" * 7000  # 14000 字 > 12000
        out = await repair_agent.repair_section(
            section_id="s1", section_title="测试章", section_content=content,
            conflicts_in_section=[{"topic": "t", "current_value": "1",
                                   "authoritative_value": "2"}],
            facts="", sources="")
        assert out == content, "超长章应原样返回（本轮未修复）"
        assert len(calls) == 1, (
            f"超长章只允许定点编辑一次调用，重写兜底必须被门槛拦下：{calls}")

    @pytest.mark.asyncio
    async def test_normal_section_still_falls_back_to_rewrite(self, monkeypatch):
        """正常长度章：行为逐字不变（编辑失败后重写兜底照发第二次调用）。"""
        calls: list = []

        async def fake_chat(messages, **kwargs):
            calls.append(kwargs.get("scene", ""))
            return "不是 JSON"

        monkeypatch.setattr(repair_agent, "chat_with_fallback", fake_chat)
        content = "正文" * 100
        out = await repair_agent.repair_section(
            section_id="s2", section_title="普通章", section_content=content,
            conflicts_in_section=[{"topic": "t", "current_value": "1",
                                   "authoritative_value": "2"}],
            facts="", sources="")
        assert len(calls) == 2, "重写兜底仍应发起（编辑 + 重写各一次）"
        assert isinstance(out, str)

    def test_edits_side_no_truncation_discipline(self):
        """定点编辑侧「不截断正文」纪律（勿顺手加 [:N]）——行为级锚点：
        超长原文必须原样进入提示词尾部（截断会让 old_text 永远无法命中）。"""
        from app.services import consistency_edits as ce
        long_content = "X" * 30000
        captured: dict = {}

        async def fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return "不是 JSON"

        res = asyncio.run(ce.collect_repair_edits(
            section_id="s3", section_title="t", section_content=long_content,
            conflicts_in_section=[], facts="", sources="",
            chat_fn=fake_chat))
        assert long_content in captured["user"], "定点编辑提示词不得截断章节原文"
        assert res.content == long_content


# =========================================================================
# D4 · project_facts 预算单一事实源
# =========================================================================
class TestProjectFactsBudgetSingleSource:

    def test_constants_match_legacy_values(self):
        """数值逐字不变（1500/1000）——本轮收口是零行为变化的判据。"""
        from app.routers import sse_handlers as sh
        assert sh.PROJECT_FACTS_LIMIT_OUTLINE == 1500
        assert sh.PROJECT_FACTS_LIMIT_SUBLEVEL == 1000

    def test_no_magic_number_slice_on_project_facts(self):
        """project_facts 的切片必须走常量，仓内不得残留字面量切片。"""
        import pathlib

        import app as app_pkg
        src = (pathlib.Path(app_pkg.__file__).parent
               / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        offenders = [
            ln.strip() for ln in src.splitlines()
            if re.search(r"project_facts\b[^\n]*\[:\s*\d", ln)]
        assert not offenders, (
            "project_facts 截断残留魔法数（应引用 PROJECT_FACTS_LIMIT_*）：\n"
            + "\n".join(offenders))

    def test_constants_actually_consumed(self):
        """两个常量真的被消费（防空断言：只定义不使用等于没接线）。"""
        import pathlib

        import app as app_pkg
        src = (pathlib.Path(app_pkg.__file__).parent
               / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert src.count("PROJECT_FACTS_LIMIT_OUTLINE") >= 4, "OUTLINE 档应≥3处消费"
        assert src.count("PROJECT_FACTS_LIMIT_SUBLEVEL") >= 3, "SUBLEVEL 档应≥2处消费"


# =========================================================================
# D6 · 死代码删除 + 零占位显式空声明
# =========================================================================
class TestDeadIllustrationRemoved:

    def test_dead_functions_removed_from_image_engine(self):
        from app.services.ai import image_engine
        assert not hasattr(image_engine, "generate_illustration_prompt")
        assert not hasattr(image_engine, "generate_illustration_arrange")

    def test_dead_templates_unregistered(self):
        assert "ILLUSTRATION_PLAN_SYSTEM" not in _ALL_PROMPTS
        assert "ILLUSTRATION_ARRANGE_SYSTEM" not in _ALL_PROMPTS
        # 在役的优化提示词模板不受影响（配图流水线消费）
        assert "ILLUSTRATION_PROMPT_OPTIMIZE" in _ALL_PROMPTS

    def test_no_dangling_references(self):
        """删除后 app/ 内不得再有对被删符号的**代码**引用。

        判据走 AST（ImportFrom 别名 / Name 引用 / get_prompt 字面量实参），
        不用文本扫行 —— 注释与 docstring 里的「已删除」说明不得误报。
        """
        import ast
        import pathlib

        import app as app_pkg
        root = pathlib.Path(app_pkg.__file__).parent
        syms = {"ILLUSTRATION_PLAN_SYSTEM", "ILLUSTRATION_ARRANGE_SYSTEM",
                "generate_illustration_prompt", "generate_illustration_arrange"}
        bad = []
        for p in root.rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    hit = [a.name for a in node.names if a.name in syms]
                elif isinstance(node, ast.Name) and node.id in syms:
                    hit = [node.id]
                elif (isinstance(node, ast.Call) and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and node.args[0].value in syms):
                    hit = [node.args[0].value]
                else:
                    hit = []
                for h in hit:
                    bad.append(f"{p.name}:{node.lineno} {h}")
        assert not bad, "存在被删符号的悬空引用：" + "\n".join(bad)


ZERO_DECLARED_KEYS = [
    "SHARED_FORBIDDEN_WORDS", "SHARED_OUTPUT_SPEC",
    "SHARED_SCOPE_RULES", "SHARED_SCOPE_RULES_BRIEF",
    "consistency_scan_system", "consistency_scan_batch_system",
    "consistency_arbitrate_system", "consistency_repair_system",
    "consistency_repair_edits_system", "review_autofix_system",
]


class TestZeroDeclarationSemantics:

    @pytest.mark.parametrize("key", ZERO_DECLARED_KEYS)
    def test_registered_as_explicit_empty(self, key):
        """空列表 = 显式声明「零占位符」，与未声明（None）语义分离。"""
        assert key in PROMPT_VARIABLE_CONTRACTS
        assert PROMPT_VARIABLE_CONTRACTS[key] == []
        meta = _ALL_PROMPTS[key]
        assert meta.get("requires") == [], (
            f"{key} 的 requires 应被 apply 覆盖为 []（而非 None/缺失）")
        assert meta.get("default_variables") == [], f"{key} 出厂应零占位"

    def test_registry_health_zero_drift(self):
        """新登记（含空声明与三个真实契约）后启动期校验必须零漂移。"""
        assert check_prompt_variables() == []

    def test_zero_declaration_detects_added_placeholder(self):
        """承重用例：往零声明模板出厂基线加 {probe_var} → check 必须报
        used_not_declared 漂移（旧语义 `if not requires` 下空声明恒跳过，
        本用例即 A/B 定向失败点）。"""
        key = "review_autofix_system"
        meta = _ALL_PROMPTS[key]
        orig_default = meta["default_content"]
        orig_vars = meta["default_variables"]
        meta["default_content"] = orig_default + "\n{probe_var}"
        meta["default_variables"] = list(orig_vars) + ["probe_var"]
        try:
            issues = check_prompt_variables()
            hit = [i for i in issues if i.get("key") == key]
            assert hit, "零声明模板新增占位符必须被启动期校验发现"
            assert "probe_var" in str(hit[0].get("used_not_declared")), hit
        finally:
            meta["default_content"] = orig_default
            meta["default_variables"] = orig_vars
        assert check_prompt_variables() == [], "还原后必须回到零漂移健康态"

    def test_undeclared_template_still_skipped(self):
        """向后兼容红线：未声明（None）模板照旧完全跳过，不受空声明接入影响。

        本轮收口后仓内已无未声明模板（契约全覆盖），故用临时伪模板验证
        跳过语义本身（finally 删除，不留残）。
        """
        key = "__probe_undeclared_template__"
        assert key not in _ALL_PROMPTS
        _ALL_PROMPTS[key] = {
            "key": key, "category": "探针", "label": "探针",
            "content": "正文", "default_content": "正文",
            "variables": [], "default_variables": [], "requires": None,
        }
        try:
            meta = _ALL_PROMPTS[key]
            meta["default_content"] = meta["default_content"] + "\n{probe_var2}"
            meta["default_variables"] = ["probe_var2"]
            issues = [i for i in check_prompt_variables()
                      if i.get("key") == key]
            assert not issues, "未声明模板不参与契约校验（跳过语义不得扩大）"
        finally:
            _ALL_PROMPTS.pop(key, None)


# =========================================================================
# 契约表补全登记（R38 遗留「11 个模板」的现存残余 3 个）
# =========================================================================
class TestNewlyContractedTemplates:

    @pytest.mark.parametrize("key,vars_", [
        ("consistency_repair_edits_user",
         {"authoritative_sources", "conflicts_in_section", "global_facts",
          "section_id", "section_title"}),
        ("facts_finalize_system", {"current_facts"}),
        ("facts_knowledge_patch_system", {"current_facts", "knowledge_text"}),
    ])
    def test_contract_matches_template_placeholders(self, key, vars_):
        assert key in PROMPT_VARIABLE_CONTRACTS
        assert set(PROMPT_VARIABLE_CONTRACTS[key]) == vars_
        meta = _ALL_PROMPTS[key]
        assert set(meta.get("default_variables") or []) == vars_, (
            "契约必须与模板实际占位符双向一致（check 已锁，此处显式登记）")

    def test_all_registered_templates_covered(self):
        """收口完整性：注册表内有占位符的模板必须全部在契约表（含空声明组）。

        其他测试文件（如 test_prompt_governance）会用 _reg 造临时探针模板，
        全量套件顺序不可控 —— 排除探针键防串扰。
        """
        probe_prefixes = ("contract_test_", "__probe")
        uncovered = [
            k for k, meta in _ALL_PROMPTS.items()
            if k not in PROMPT_VARIABLE_CONTRACTS
            and not k.startswith(probe_prefixes)
            and [v for v in (meta.get("default_variables") or [])
                 if not R._is_false_positive(k, v,
                                             meta.get("default_content") or "")]
        ]
        assert not uncovered, f"仍有模板未纳入契约表：{uncovered}"
