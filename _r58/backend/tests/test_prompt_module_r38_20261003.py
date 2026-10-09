"""R38 提示词模块护栏（2026-10-03）。

锁五类「已实证缺陷」的不变量，判据一律锚定**行为**而非实现字面量：

1. 共享红线「简版」必须走运行时 DB 覆盖链（导入期值拷贝断链修复）
2. compliance 三个 AI 端点的**校验字段集 ⊇ 消费字段集**（校验/消费错位修复）
3. 非目录任务不得再拿到目录修复提示词（repair_key 路由修复）
4. 修复轮韧性：修复目标恒为模型最初输出；修复轮异常不丢弃首轮结果；
   provider 变体真正透传 max_tokens / extra_body
5. 上下文注入不得头部切片丢条（事实预算按比例分配）+ 子层目录模板
   必须消费调用方已传的 standards_text

运行：python -m pytest tests/test_prompt_module_r38_20261003.py -v
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


# =====================================================================
# 1) 共享红线简档：运行时覆盖链
# =====================================================================
class TestScopeRulesBriefRuntimeOverride:
    """用户改共享规则 → 正文侧必须跟着变（旧实现正文侧永远不变）。"""

    BRIEF = "SHARED_SCOPE_RULES_BRIEF"
    FULL = "SHARED_SCOPE_RULES"
    MARK = "ZZ_R38_PROBE_MARKER_ZZ"

    @pytest.fixture(autouse=True)
    def _cache_guard(self):
        from app.services.ai.prompts import _cache
        old = (_cache._prompt_cache, _cache._loaded_db_path, _cache._loaded_db_mtime)
        yield
        (_cache._prompt_cache, _cache._loaded_db_path,
         _cache._loaded_db_mtime) = old

    def _set_override(self, key, text):
        from app.services.ai.prompts import _cache
        _cache._prompt_cache = {key: text}
        _cache._loaded_db_path = _cache._current_db_path()
        _cache._loaded_db_mtime = None

    @pytest.mark.parametrize("key", ["content_generation_system",
                                     "content_continue_system"])
    def test_brief_override_reaches_content_prompts(self, key):
        """改简档 → 两个正文模板都必须命中（修复前恒为 False）。"""
        from app.services.ai.prompts import _cache
        self._set_override(self.BRIEF, "### probe\n" + self.MARK)
        assert self.MARK in (_cache.get_prompt(key) or ""), (
            f"{key} 未消费用户对 {self.BRIEF} 的修改 —— 正文红线不可编辑")

    def test_outline_is_not_polluted_by_brief_override(self):
        """简档是独立可编辑条目：改它不得污染目录侧（目录侧用完整版）。"""
        from app.services.ai.prompts import _cache
        self._set_override(self.BRIEF, "### probe\n" + self.MARK)
        assert self.MARK not in (_cache.get_prompt("outline_short_system") or "")

    def test_full_override_still_reaches_outline(self):
        """完整版仍必须对目录侧生效（既有能力不得被本次修复破坏）。"""
        from app.services.ai.prompts import _cache
        self._set_override(self.FULL, "### probe\n" + self.MARK)
        assert self.MARK in (_cache.get_prompt("outline_short_system") or "")

    @pytest.mark.parametrize("key", ["content_generation_system",
                                     "content_continue_system"])
    def test_factory_default_render_is_byte_identical(self, key):
        """出厂默认渲染结果必须逐字带上出厂简档、无残留占位符。

        这是**向后兼容红线**：未定制提示词的用户拿到的提示词必须与修复前一致。
        """
        from app.services.ai.prompts import _cache
        from app.services.ai.prompts._shared import SHARED_SCOPE_RULES_BRIEF
        text = _cache.get_prompt(key) or ""
        assert SHARED_SCOPE_RULES_BRIEF in text
        assert "{" + self.BRIEF + "}" not in text

    def test_brief_registered_as_editable_prompt(self):
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert self.BRIEF in _ALL_PROMPTS, "简档未注册 → 编辑器里改不到"
        assert _ALL_PROMPTS[self.BRIEF]["category"] == "共享规则"

    def test_content_module_has_no_import_time_value_copy(self):
        """AST 判据：content.py 不得再 import/替换红线常量。"""
        tree = ast.parse((APP / "services/ai/prompts/content.py").read_text("utf-8"))
        imported = [
            a.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("_shared")
            for a in n.names
        ]
        assert "SHARED_SCOPE_RULES_BRIEF" not in imported, (
            "正文侧重新 import 红线常量 = 导入期值拷贝 = 用户改不动")
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "replace"):
                names = {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
                assert "SHARED_SCOPE_RULES_BRIEF" not in names, (
                    "line %d 出现把出厂红线值拷进模板的 replace" % n.lineno)


# =====================================================================
# 2) compliance：校验字段集 ⊇ 消费字段集
# =====================================================================
class TestComplianceValidatorCoversConsumedFields:
    def test_consistency_audit_requires_issues(self):
        """漏 issues 必须判不合格（旧实现只查 score → 空审计落库）。"""
        from app.routers.compliance import _validate_consistency_audit
        assert _validate_consistency_audit({"score": 100}) != [], \
            "缺 issues 竟然通过校验 → 会落库 issues='[]' 的假满分审计"
        assert _validate_consistency_audit({"issues": []}) != [], \
            "缺 score 未被拦下"

    def test_consistency_audit_accepts_empty_issues(self):
        """真的没有不一致时 issues=[] 是合法输出，不得触发无谓修复轮。"""
        from app.routers.compliance import _validate_consistency_audit
        assert _validate_consistency_audit({"score": 100, "issues": []}) == []
        assert _validate_consistency_audit({"score": 0, "issues": [1]}) == []

    def test_check_results_rejects_empty_shell_rows(self):
        """``{"results":[{}]}`` 必须判不合格（旧实现 truthy 即通过 → 空行落库）。"""
        from app.routers.compliance import _validate_check_results
        issues = _validate_check_results({"results": [{}]})
        assert any("item" in i for i in issues), "空壳行未被拦下"
        assert any("hit" in i for i in issues), "hit 类型未校验"

    def test_check_results_accepts_wellformed(self):
        from app.routers.compliance import _validate_check_results
        assert _validate_check_results(
            {"results": [{"item": "工程概况", "hit": True, "severity": "low"}]}) == []
        assert _validate_check_results({"results": []}) == ["缺少 results"]

    def test_expert_review_normalizes_declared_fields(self):
        """模型漏字段时归一为 []（前端拿到 [] 而非 undefined）并保持其余字段。

        ✅ 断言口径更新（2026-10-04）：非数组字段**不再**一律降级为空数组。
        后续轮次把「字符串单值」改为转成单元素数组**保留内容**并记 WARNING
        —— 旧写法会把 ``missing="工程概况"`` 直接丢弃，是信息损失。首版断言
        仍按「降级为 []」写，被单跑当场拦下（missing 实为 ``['x']``），
        现按更优口径分三段锁：漏字段补 [] / 字符串保留 / 其余类型降级 []。
        """
        from app.routers.compliance import _normalize_expert_review_result as norm
        out = norm({"score": 75})
        for f in ("ready", "missing", "suggestions"):
            assert out[f] == []
        assert out["score"] == 75
        # 字符串单值必须**保留**成单元素数组（丢弃即信息损失）
        out2 = norm({"score": 75, "ready": ["a"], "missing": "x"})
        assert out2["ready"] == ["a"] and out2["missing"] == ["x"], (
            "字符串单值未保留成单元素数组")
        # 空串 / 非字符串类型仍降级为空数组
        out3 = norm({"score": 75, "missing": "   ", "ready": 3})
        assert out3["missing"] == [] and out3["ready"] == [], (
            "空串/非字符串未降级为空数组")
        # 数组原样透传（不重排、不去重）
        out4 = norm({"score": 75, "ready": ["a", "a", "b"]})
        assert out4["ready"] == ["a", "a", "b"], "已有数组被改写"


# =====================================================================
# 3) repair_key 路由
# =====================================================================
#: scene 前缀 → 是否目录族（需要 outline_json_fix_system）
_OUTLINE_SCENES = (
    "outline_adjust", "outline_sublevel", "outline_fix",
    "outline_review", "outline_draft", "outline_level1", "outline_recognition",
)


def _collect_json_call_sites() -> list[tuple[str, ast.Call]]:
    out = []
    for path in APP.rglob("*.py"):
        if path.name == "json_response.py":
            continue
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "collect_json_response":
                continue
            out.append((path.relative_to(APP).as_posix(), node))
    return out


class TestRepairKeyRouting:
    def test_generic_default_is_not_outline_prompt(self):
        """默认修复提示词必须与目录无关（否则新增调用点默认就是错的）。"""
        from app.services.ai.json_response import GENERIC_REPAIR_KEY
        assert GENERIC_REPAIR_KEY != "outline_json_fix_system"

    def test_generic_prompt_never_instructs_outline_structure(self):
        """通用提示词不得**指示**产出目录结构。

        ⚠️ 只查「肯定式」指令词，不查裸术语：通用提示词里有一句
        「不得凭空引入 children / depth / description」是**反向**约束
        （明确禁止模型加这些字段），裸词匹配会把它误判成缺陷。
        """
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        body = _ALL_PROMPTS["json_schema_fix_system"]["default_content"]
        for bad in ("为每个一级目录补", "depth=1", "children 结构不规范",
                    "字段名 child → children", "完整目录"):
            assert bad not in body, (
                f"通用修复提示词含目录专属指令「{bad}」→ 会诱导非目录任务产出目录形状 JSON")

    def test_generic_prompt_differs_from_outline_prompt(self):
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert (_ALL_PROMPTS["json_schema_fix_system"]["default_content"]
                != _ALL_PROMPTS["outline_json_fix_system"]["default_content"])

    def test_generic_prompt_is_in_variable_contract(self):
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS,
            PROMPT_VARIABLE_CONTRACTS,
        )
        want = {"issues", "target_description", "invalid_content"}
        assert set(PROMPT_VARIABLE_CONTRACTS["json_schema_fix_system"]) == want
        assert set(_ALL_PROMPTS["json_schema_fix_system"].get("requires")) == want

    @pytest.mark.parametrize("rel,node", _collect_json_call_sites())
    def test_outline_family_calls_pass_repair_key_explicitly(self, rel, node):
        """目录族调用点必须显式传 repair_key（默认已改为通用提示词）。"""
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        scene = kwargs.get("scene")
        scene_val = getattr(scene, "value", None)
        if not isinstance(scene_val, str):
            pytest.skip("scene 非字面量，无法静态判定")
        if not scene_val.startswith(_OUTLINE_SCENES):
            pytest.skip("非目录族场景")
        assert "repair_key" in kwargs, (
            f"{rel}:{node.lineno} 目录族调用点未显式传 repair_key "
            f"→ 修复轮会用通用提示词而非目录提示词")

    def test_every_repair_key_used_is_registered(self):
        """调用点传的 repair_key 必须在注册表里真实存在。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        for rel, node in _collect_json_call_sites():
            for k in node.keywords:
                if k.arg != "repair_key":
                    continue
                key = getattr(k.value, "value", None)
                if isinstance(key, str):
                    assert key in _ALL_PROMPTS, (
                        f"{rel}:{node.lineno} repair_key={key} 未注册")


# =====================================================================
# 4) 修复轮韧性
# =====================================================================
class TestRepairRoundResilience:
    @staticmethod
    def _fake_chat(script):
        calls = []

        async def _chat(messages, **kw):
            calls.append(messages)
            item = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        return _chat, calls

    @pytest.mark.asyncio
    async def test_repair_target_is_first_raw(self, monkeypatch):
        """第 2 轮的修复目标必须是**模型最初**的输出，不能是上轮修复结果。"""
        from app.services.ai import json_response as J
        first = json.dumps({"outline": "BAD"})
        second = json.dumps({"outline": "ALSO_BAD"})
        third = json.dumps({"outline": "OK"})
        chat, calls = self._fake_chat([first, second, third])
        monkeypatch.setattr(J, "chat_with_fallback", chat)

        def vf(o):
            return [] if o.get("outline") == "OK" else ["bad"]

        obj, _ = await J.collect_json_response(
            [{"role": "system", "content": "S"}], vf, max_retries=2,
            repair_key=J.OUTLINE_REPAIR_KEY)
        assert obj == {"outline": "OK"}
        assert len(calls) == 3
        for repair_call in calls[1:]:
            last_user = repair_call[-1]["content"]
            assert first in last_user, "修复提示词未携带模型最初输出"
            assert second not in last_user, "修复目标被上一轮修复结果污染"

    @pytest.mark.asyncio
    async def test_repair_round_exception_does_not_lose_first_round(
            self, monkeypatch):
        """修复轮瞬时故障应消耗修复预算继续，而不是把整次调用炸掉。"""
        from app.services.ai import json_response as J
        first = json.dumps({"outline": "BAD"})
        boom = RuntimeError("429 rate limited")
        chat, calls = self._fake_chat([first, boom, first, json.dumps({"outline": "OK"})])
        monkeypatch.setattr(J, "chat_with_fallback", chat)

        def vf(o):
            return [] if o.get("outline") == "OK" else ["bad"]

        obj, _ = await J.collect_json_response(
            [{"role": "system", "content": "S"}], vf, max_retries=3,
            repair_key=J.OUTLINE_REPAIR_KEY)
        assert obj == {"outline": "OK"}
        assert len(calls) == 4

    @pytest.mark.asyncio
    async def test_all_repair_rounds_failing_raises_with_context(self, monkeypatch):
        from app.services.ai import json_response as J
        first = json.dumps({"outline": "BAD"})
        chat, _ = self._fake_chat([first, RuntimeError("net down"),
                                   RuntimeError("net down")])
        monkeypatch.setattr(J, "chat_with_fallback", chat)
        with pytest.raises(ValueError) as ei:
            await J.collect_json_response(
                [{"role": "system", "content": "S"}],
                lambda o: ["bad"], max_retries=2,
                repair_key=J.OUTLINE_REPAIR_KEY)
        assert "修复轮异常" in str(ei.value)

    @pytest.mark.asyncio
    async def test_first_round_exception_still_propagates(self, monkeypatch):
        """首轮异常必须照旧向上抛（调用方据此决定是否整体重来）。"""
        from app.services.ai import json_response as J
        chat, _ = self._fake_chat([RuntimeError("provider down")])
        monkeypatch.setattr(J, "chat_with_fallback", chat)
        with pytest.raises(RuntimeError):
            await J.collect_json_response([{"role": "system", "content": "S"}])

    @pytest.mark.asyncio
    async def test_provider_variant_passes_max_tokens_and_extra_body(self):
        """provider 变体必须真正透传 max_tokens / extra_body（旧实现声明即失效）。"""
        from app.services.ai import json_response as J
        seen = {}

        class _P:
            async def chat(self, messages, temperature=None, max_tokens=None,
                           extra_body=None):
                seen.update(t=max_tokens, b=extra_body)
                return json.dumps({"ok": True})

        got = await J.collect_json_response_with_provider(
            _P(), [{"role": "system", "content": "S"}],
            max_tokens=1234, extra_body={"x": 1})
        assert got == {"ok": True}
        assert seen == {"t": 1234, "b": {"x": 1}}

    @pytest.mark.asyncio
    async def test_provider_variant_backs_off_for_legacy_signature(self):
        """只认 temperature 的旧 provider 仍可用（逐个降级透传）。"""
        from app.services.ai import json_response as J

        class _Legacy:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, temperature=None):
                self.calls += 1
                return json.dumps({"ok": True})

        p = _Legacy()
        got = await J.collect_json_response_with_provider(
            p, [{"role": "system", "content": "S"}], max_tokens=99)
        assert got == {"ok": True}
        # 第一次带 max_tokens 的调用在进入函数体前就 TypeError（参数绑定阶段），
        # 故只有「去掉多余 kwarg」那次真正执行到 provider 内部并计数。
        assert p.calls == 1, "旧签名 provider 未走降级重试"

    @pytest.mark.asyncio
    async def test_provider_variant_accepts_repair_key(self):
        from app.services.ai import json_response as J
        seen_user = []

        class _P:
            def __init__(self):
                self.n = 0

            async def chat(self, messages, temperature=None):
                self.n += 1
                if self.n == 1:
                    return "not json"
                seen_user.append(messages[-1]["content"])
                return json.dumps({"ok": True})

        await J.collect_json_response_with_provider(
            _P(), [{"role": "system", "content": "S"}], max_retries=1,
            repair_key=J.OUTLINE_REPAIR_KEY)
        assert seen_user and "children" in seen_user[0], (
            "repair_key 未被 provider 变体采用")


# =====================================================================
# 5) 上下文注入不得头部切片丢条 + 子层模板消费 standards_text
# =====================================================================
class TestContextInjectionIntegrity:
    def test_facts_budget_does_not_drop_trailing_facts(self):
        """事实注入超预算时必须**按比例**保留每条，不得尾部整段消失。"""
        from app.routers.compliance import _facts_prompt_text
        facts = [{"name": "事实%03d" % i, "value": "值" * 40} for i in range(1, 121)]
        out = _facts_prompt_text(facts)
        assert "事实120" in out, "尾部事实整条消失（头部切片回归）"
        assert out.count("\n") >= 100, f"仅保留 {out.count(chr(10)) + 1}/120 条"

    def test_facts_budget_passthrough_when_under_cap(self):
        from app.routers.compliance import _facts_prompt_text
        facts = [{"name": "基坑开挖深度", "value": "5.6m",
                  "is_safety_critical": True}]
        out = _facts_prompt_text(facts)
        assert out == "- 基坑开挖深度：5.6m（安全关键）"

    def test_facts_empty_degrades_to_placeholder(self):
        from app.routers.compliance import _facts_prompt_text
        assert _facts_prompt_text(None) == "（无）"
        assert _facts_prompt_text([]) == "（无）"
        assert _facts_prompt_text([{"value": "无名字"}]) == "（无）"

    def test_safety_marker_survives_budget_pressure(self):
        """按比例分配不得把「（安全关键）」切成半截。

        端到端验证时实测：120 条事实下每条配额只有 ``budget // n * 2``，
        标记被切成 ``（安`` —— 模型读不到「安全关键」三个字，还读到半个括号。
        安全关键标记恰恰是预算压力下最不该丢的信息。
        """
        from app.routers.compliance import _facts_prompt_text
        facts = [{"name": f"关键参数{i}", "value": "v" * 40,
                  "is_safety_critical": i == 0} for i in range(120)]
        text = _facts_prompt_text(facts)
        assert "（安全关键）" in text, "安全关键标记被预算截断吃掉了"
        assert "（安\n" not in text and "（安全关\n" not in text, (
            "残留被切碎的后缀残段：" + repr(text[:80]))
        assert "关键参数119" in text, "尾部事实仍然被丢弃"

    def test_facts_under_cap_is_byte_identical(self):
        """未超预算时输出必须与引入 suffixes 参数前逐字一致。"""
        from app.routers.compliance import _facts_prompt_text
        out = _facts_prompt_text([{"name": "基坑开挖深度", "value": "5.6m",
                                   "is_safety_critical": True}])
        assert out == "- 基坑开挖深度：5.6m（安全关键）"

    def test_suffixes_none_is_passthrough(self):
        """``suffixes`` 是纯增量参数：缺省 / None / 空列表三者逐字一致。

        ⚠️ 期望值不能写成「超预算输入也等于原拼接」——
        ``total > budget`` 时本函数**本就该**按比例截断（这正是 P1-e 的目的），
        拿未截断结果当期望值是**期望值写错**而非被测代码错：首版就这么写错，
        被全量回归当场拦下（``test_suffixes_none_is_passthrough`` failed）。
        这里把不变量拆成两段，各自可被 A/B 定向失败。
        """
        from app.routers.compliance import _join_with_budget
        over = ["-" + "x" * 80 for _ in range(40)]   # 总长 3279 > 3000 → 走截断分支
        assert _join_with_budget(over, 3000) == _join_with_budget(over, 3000, None) \
            == _join_with_budget(over, 3000, []), "suffixes 参数改变了截断结果"
        under = ["-" + "x" * 10 for _ in range(5)]  # 总长 59 <= 3000 → 逐字早退
        assert _join_with_budget(under, 3000) == "\n".join(under), "未超预算时未逐字早退"
        assert _join_with_budget(under, 3000, ["（安全关键）"] * 5) \
            == "\n".join(under), "未超预算时后缀参数污染了输出"

    def test_drop_partial_suffix_removes_残段(self):
        from app.routers.compliance import _drop_partial_suffix
        assert _drop_partial_suffix("- x：（安", "（安全关键）") == "- x："
        assert _drop_partial_suffix("- x：完整（安全关键）", "（安全关键）") \
            == "- x：完整（安全关键）"
        assert _drop_partial_suffix("- x：无", "（安全关键）") == "- x：无"

    @pytest.mark.parametrize("key", ["outline_sublevel_system",
                                     "outline_sublevel_batch_system"])
    def test_sublevel_templates_consume_standards_text(self, key):
        """调用点一直在传 standards_text；模板不消费 = 编制依据在子层丢失。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert "standards_text" in _ALL_PROMPTS[key]["default_variables"], (
            f"{key} 不消费 standards_text（调用点已传 → 编制依据规范在二三级目录阶段丢失）")

    def test_standards_text_wording_is_identical_across_outline_templates(self):
        """四个目录模板的规范注入行必须逐字一致（避免同义不同措辞的分叉）。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        line = "【编制依据规范（按本方案类别匹配，仅可引用，禁杜撰编号与已废止版本）】："
        for key in ("outline_short_system", "outline_level1_system",
                    "outline_sublevel_system", "outline_sublevel_batch_system"):
            assert line in _ALL_PROMPTS[key]["default_content"], (
                f"{key} 的编制依据注入行措辞与其它目录模板不一致")

    def test_no_contract_drift_after_all_changes(self):
        """新增契约项后，启动期契约校验必须仍然干净。"""
        from app.services.ai.prompts import check_prompt_variables
        assert check_prompt_variables() == []

    def test_no_prompt_key_references_unknown_shared(self):
        """出厂模板里不得引用未注册的 SHARED_* 片段。"""
        import re

        from app.services.ai.prompts._registry import _ALL_PROMPTS
        rx = re.compile(r"\{(SHARED_[A-Z0-9_]+)\}")
        missing = set()
        for key, meta in _ALL_PROMPTS.items():
            for ref in rx.findall(meta.get("default_content") or ""):
                if ref not in _ALL_PROMPTS:
                    missing.add((key, ref))
        assert not missing, f"未注册的共享片段引用：{sorted(missing)}"


# =====================================================================
# 6) 目录审核提示词：8.1「按清单核对」指令不得悬空
# =====================================================================
class TestReviewAuditHintTracksBlock:
    """指令与它引用的清单必须同生共死。

    旧实现把 8.1 写死在模板里，而清单来自 ``{outline_checkpoint_block}``：
    关闭 ``outline_checkpoint_check`` 后清单整行被丢弃、指令仍在 → 审核模型
    会去核对一份**根本不存在**的清单，臆造「缺失章节」写进 suggestions，
    进而触发外科式补齐加进并不存在的章节。
    """

    BASE = dict(scheme_name="深基坑支护专项施工方案", scheme_type="深基坑",
                construction_scope="基坑支护、土方开挖", scheme_basis="",
                is_dangerous="是", project_facts="", outline_json="[]")

    @pytest.fixture(autouse=True)
    def _restore_switch(self):
        from app.routers import sse_handlers as sh
        old = sh.settings.outline_checkpoint_check
        yield
        sh.settings.outline_checkpoint_check = old

    def _render(self, *, hazardous=True):
        from app.routers import sse_handlers as sh
        from app.services.ai.prompts import render
        return render("outline_review_system", **self.BASE,
                      **sh._outline_review_checkpoint_kwargs(
                          self.BASE["scheme_name"], self.BASE["scheme_type"],
                          hazardous))

    def test_on_state_carries_both_block_and_instruction(self):
        on = self._render()
        assert "审核检查点前置要求" in on, "开关开启时清单必须注入"
        assert "8.1 目录是否覆盖" in on, "开关开启时 8.1 指令必须注入"
        assert "{outline_checkpoint_audit_hint}" not in on, "占位符不得残留"

    def test_off_state_drops_both_no_dangling_reference(self, monkeypatch):
        from app.routers import sse_handlers as sh
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        off = self._render()
        assert "8.1 目录是否覆盖" not in off, (
            "关闭开关后 8.1 指令必须随清单一起消失（否则模型核对不存在的清单）")
        assert "审核检查点前置要求" not in off, "关闭开关后不得残留检查点段"
        assert "{outline_checkpoint_audit_hint}" not in off

    def test_generation_prompt_never_carries_audit_instruction(self):
        """生成链路不得携带审核语义（它说「判 passed=false」）。"""
        from app.routers import sse_handlers as sh
        from app.services.ai.prompts import render
        gen = render("outline_short_system", scheme_name="S", scheme_type="T",
                     construction_scope="", scheme_basis="", standards_text="",
                     project_facts="",
                     **sh._outline_checkpoint_kwargs("S", "T", True))
        assert "8.1 目录是否覆盖" not in gen, (
            "审核指令不得进入生成提示词（共享 kwargs 被误用）")
        assert "审核检查点前置要求" in gen, "生成链路仍必须拿到清单本体"

    def test_hint_placeholder_owns_its_own_line(self):
        """独占行才能在未传时整行丢弃，否则会留下半截指令碎片。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        hits = [ln for ln in _ALL_PROMPTS["outline_review_system"]["default_content"]
                .split("\n") if "outline_checkpoint_audit_hint" in ln]
        # ⚠️ 必须先断言「恰好一处」：若只对命中行做 strip 断言，占位符被改成
        #    固定文案时 for 循环一次都不进 → 用例**空转通过**（A/B 实测踩到）。
        assert len(hits) == 1, f"占位符必须恰好出现 1 行，实际 {len(hits)} 行"
        assert hits[0].strip() == "{outline_checkpoint_audit_hint}", (
            f"占位符未独占一行，关闭开关时会留下孤行：{hits[0]!r}")

    def test_hint_is_declared_in_variable_contract(self):
        from app.services.ai.prompts._registry import PROMPT_VARIABLE_CONTRACTS
        assert "outline_checkpoint_audit_hint" in \
            PROMPT_VARIABLE_CONTRACTS["outline_review_system"]

    @pytest.mark.asyncio
    async def test_review_call_site_uses_review_variant(self, monkeypatch):
        """接线锁：审核链路必须调 review 变体而非共享变体。

        判据锚定**行为**（实际下发的提示词），不锚定 ``inspect.getsource``
        字面量 —— 写成源码匹配会被注释里的同名函数名误伤（AGENTS.md §5.7）。

        ⚠️ 必须在**开关开启**态断言 8.1 **在场**：关闭态下两个变体输出完全相同
        （都不带清单也都不带指令），拿关闭态做判据等于没判别 —— A/B 反向验证
        实测「退回共享 kwargs 后用例照过」。
        """
        import json as _json

        from app.routers import sse_handlers as sh

        seen = {}

        async def fake_collect(messages, validate_fn=None, **kw):
            seen["prompt"] = messages[0]["content"]
            return {"passed": True, "suggestions": []}, _json.dumps(
                {"passed": True, "suggestions": []}, ensure_ascii=False)

        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", True)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        outline = [{"title": "工程概况", "description": "d", "children": []}]
        await sh._review_and_fix_outline(
            outline, "深基坑", True, "简述", scheme_name="基坑支护专项施工方案")
        prompt = seen["prompt"]
        assert "审核检查点前置要求" in prompt, "清单本体必须下发"
        assert "8.1 目录是否覆盖" in prompt, (
            "审核链路下发的是共享 kwargs —— 开关开启时 8.1 指令丢失")
        assert "{outline_checkpoint_audit_hint}" not in prompt

    @pytest.mark.asyncio
    async def test_review_prompt_has_no_dangling_reference_when_off(self, monkeypatch):
        """关闭态：清单与指令必须一起消失（悬空引用会让模型臆造缺失章节）。"""
        import json as _json

        from app.routers import sse_handlers as sh

        seen = {}

        async def fake_collect(messages, validate_fn=None, **kw):
            seen["prompt"] = messages[0]["content"]
            return {"passed": True, "suggestions": []}, _json.dumps(
                {"passed": True, "suggestions": []}, ensure_ascii=False)

        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        outline = [{"title": "工程概况", "description": "d", "children": []}]
        await sh._review_and_fix_outline(
            outline, "深基坑", True, "简述", scheme_name="基坑支护专项施工方案")
        assert "8.1 目录是否覆盖" not in seen["prompt"]
        assert "{outline_checkpoint_audit_hint}" not in seen["prompt"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))