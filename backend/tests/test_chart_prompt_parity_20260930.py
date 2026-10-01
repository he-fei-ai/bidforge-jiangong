"""图表三侧口径 + 提示词共享片段保存期校验 · 护栏（2026-09-30 第八轮）。

本轮修复两类「同一判据在多处各自实现」造成的真实分叉：

1. **Mermaid 侧 `default` 实参三侧不一致 → 幽灵图 + 绕过配图上限**
   登记侧（`_chart_pipeline._scan_chart_fences_full:210`）与
   `charts.py`（:261/:388）都用 `default=""`（首关键字不在映射表内 → 跳过），
   而导出侧（`content_blocks._parse_content_blocks`）此前写死
   `default="flowchart"`。于是「首关键字不在映射表内」的 mermaid 块在导出侧
   被当成 flowchart 产出 chart 块并**占用图号**，在登记侧却整块跳过 ——
   即「成稿有图、chart_predictions 查无此图、绕过每章 ≤1 / 同类型 ≤3 上限」，
   与紧邻的 chart-json 分支（`_cj_type not in PIL_RENDERABLE_CHART_TYPES →
   continue`）自相矛盾。

2. **SHARED 片段自引用「首次保存」漏判**
   PATCH 路由顺序是「先 `validate_prompt_content` → 再 `update_prompt` 落库」
   （`routers/prompts.py:187` / `:214`），而 `validate_prompt_content` 的
   ② 分支只读注册表里 SHARED 片段的**旧内容**，故**首次**保存自引用内容时
   读到的是不含自引用的出厂默认 → 判定通过 → 坏内容被写入 DB。

两组用例均满足「修复前必失败、修复后必通过」。
"""
from __future__ import annotations

import inspect

import pytest

from app.services.chart_validators import (
    detect_mermaid_chart_type,
)
from app.services.content_blocks import _parse_content_blocks
from app.routers._chart_pipeline import (
    _scan_chart_fences_full,
    build_inline_chart_plan,
)


def _export_chart_types(content: str) -> list[str]:
    return [b["chart_type"] for b in _parse_content_blocks(content)
            if b.get("type") == "chart"]


def _registered_chart_types(content: str) -> list[str]:
    return [ct for ct, _code, _ordinal in _scan_chart_fences_full(content)]


class TestMermaidDefaultParamParity:
    """`default` 实参分叉是本轮 P1 的根因，必须锁死为同一取值。"""

    def test_export_side_uses_empty_default_not_flowchart(self):
        """导出侧不得再把「认不出的 mermaid」兜底成 flowchart。"""
        from app.services import content_blocks

        src = inspect.getsource(content_blocks._parse_content_blocks)
        calls = [ln.strip() for ln in src.splitlines()
                 if "detect_mermaid_chart_type(" in ln]
        assert calls, "未找到 detect_mermaid_chart_type 调用点"
        for line in calls:
            assert 'default=""' in line, f"导出侧 default 实参已漂移: {line}"
            assert 'default="flowchart"' not in line, (
                f"导出侧仍用 default='flowchart'，与登记侧 default='' 分叉: {line}")

    @pytest.mark.parametrize("keyword", [
        "someRandomKeyword", "unknownDiagram", "nonsenseDiagram",
        "requirementDiagram", "packetDiagram",
    ])
    def test_unknown_keyword_not_exported_as_flowchart(self, keyword):
        """认不出的关键字：导出侧与登记侧都必须「整块跳过」，不得产出 flowchart。"""
        content = (f"施工流程如下图所示：\n\n```mermaid\n{keyword}\n"
                   "  A --> B\n```\n\n后续。\n")
        assert _export_chart_types(content) == []
        assert _registered_chart_types(content) == []

    @pytest.mark.parametrize("code,expected", [
        ("flowchart TD\n  A --> B", "flowchart"),
        ("graph LR\n  A --> B", "flowchart"),
        ("mindmap\n  root((x))\n    A", "mindmap"),
        ("journey\n  title t\n  section s\n    a: 1: b", "journey"),
        ("sequenceDiagram\n  A->>B: hi", "sequence"),
        ("pie title X\n  \"a\" : 10", "comparison"),
        ("xychart-beta\n  x-axis [1,2]\n  bar [3,4]", "comparison"),
        ("timeline\n  title T\n  2024 : x", "timeline"),
        ("%% 注释\nflowchart TD\n  A --> B", "flowchart"),
    ])
    def test_export_and_registration_agree_on_known_types(self, code, expected):
        """已识别类型：两侧必须给出**同一个**类型（逐条锁定，防再次分叉）。"""
        assert detect_mermaid_chart_type(code, default="") == expected
        content = f"引言。\n\n```mermaid\n{code}\n```\n\n小结。\n"
        assert _export_chart_types(content) == [expected]
        assert _registered_chart_types(content) == [expected]

    def test_full_keyword_map_parity(self):
        """对唯一映射表的**全部**关键字做三侧一致性断言。"""
        from app.services.chart_validators import MERMAID_KEYWORD_TO_CHART_TYPE

        for keyword, expected in MERMAID_KEYWORD_TO_CHART_TYPE.items():
            content = f"```mermaid\n{keyword}\n```"
            assert _registered_chart_types(content) == [expected], keyword
            assert _export_chart_types(content) == (
                [expected] if expected else []), keyword

    def test_unknown_block_does_not_bypass_registration(self):
        """回归：认不出的块不进管线 → 不进清单/预检，也无从绕过配图上限。"""
        content = "```mermaid\nunknownDiagram\n  A --> B\n```"
        new_content, rows = build_inline_chart_plan(
            "S", "SEC", content, enforce_limits=False)
        assert rows == []
        assert new_content == content  # 未进管线 → 正文不被改写



# ===========================================================================
# 2. 共享片段自引用：首次保存必须即被拒绝
# ===========================================================================


class TestSharedSelfReferenceFirstSave:
    """`validate_prompt_content` 必须检查**待保存正文**，而非注册表旧内容。"""

    @pytest.mark.parametrize("key", [
        "SHARED_OUTPUT_SPEC", "SHARED_SCOPE_RULES", "SHARED_FORBIDDEN_WORDS",
    ])
    def test_first_save_self_reference_is_error(self, key):
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, validate_prompt_content,
        )
        assert key in _ALL_PROMPTS
        issues = validate_prompt_content(key, f"见 {{{key}}} 的要求")
        assert "shared_self_reference" in {i["code"] for i in issues}, (
            f"{key} 首次保存自引用未被拒绝（路由先校验后落库，"
            f"旧实现读的是注册表旧内容）")

    def test_route_validates_before_writing(self):
        """锁死根因：PATCH 路由确实「先校验、后落库」，故校验必须基于入参 text。

        判据取真正改变注册表的写入点 ``_up(key, content)``（:214），
        而非函数头部的 import（``from ... import reload_prompt_cache``）——
        后者在源码中位置更靠前，用它比较会得到错误的顺序。
        """
        from app.routers import prompts as prompts_router

        src = inspect.getsource(prompts_router.update_prompt)
        validate_at = src.index("issues = validate_prompt_content(key, content)")
        write_at = src.index("_up(key, content)")
        assert validate_at < write_at, (
            "PATCH 已改为先落库后校验；此时 validate_prompt_content 若仍只读"
            "注册表旧内容，自引用将再次漏判——需同步复核本护栏的必要性")

    @pytest.mark.parametrize("key", [
        "SHARED_OUTPUT_SPEC", "SHARED_SCOPE_RULES", "SHARED_FORBIDDEN_WORDS",
    ])
    def test_normal_shared_save_stays_clean(self, key):
        """反向：不含自引用的正常保存不得被误拒（向后兼容）。"""
        from app.services.ai.prompts._registry import validate_prompt_content

        assert validate_prompt_content(key, "只输出 JSON，不要多余解释。") == []

    def test_other_key_referencing_shared_is_not_flagged(self):
        """反向：普通模板引用合法 SHARED 片段不得被当成自引用。"""
        from app.services.ai.prompts._registry import validate_prompt_content

        issues = validate_prompt_content(
            "content_generation_system",
            "生成正文，遵守 {SHARED_OUTPUT_SPEC} 的约定。")
        assert "shared_self_reference" not in {i["code"] for i in issues}

    def test_factory_defaults_have_no_self_reference_error(self):
        """出厂默认不得含自引用 error（否则每次保存都弹无意义告警）。"""
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, validate_prompt_content,
        )

        for key, meta in _ALL_PROMPTS.items():
            default = meta.get("default_content") or meta.get("content") or ""
            issues = validate_prompt_content(key, default)
            assert "shared_self_reference" not in {i["code"] for i in issues}, key



# ===========================================================================
# 3. 契约体检必须复用运行期误报判据（否则每次保存出厂模板都弹假告警）
# ===========================================================================


class TestContractCheckFalsePositiveParity:
    """③ 分支与 `validate_prompt_variables` / `check_prompt_variables` 同口径。"""

    def test_factory_defaults_have_no_issues(self):
        """出厂默认逐个模板断言「零问题」——修复前 `content_generation_system`
        会因 `$p_{max}$`（LaTeX 下标示例）报出 `contract_var_added`。"""
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, validate_prompt_content,
        )

        offenders = {}
        for key, meta in _ALL_PROMPTS.items():
            default = meta.get("default_content") or meta.get("content") or ""
            issues = validate_prompt_content(key, default)
            if issues:
                offenders[key] = [i["code"] for i in issues]
        assert offenders == {}, f"出厂默认不应有任何问题: {offenders}"

    def test_latex_and_json_examples_are_not_contract_drift(self):
        """`$p_{max}$` / `{"min": 1}` 属示例，不得报成契约漂移。"""
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, validate_prompt_content,
        )

        default = _ALL_PROMPTS["content_generation_system"]["default_content"]
        noisy = default + '\n\n示例：$p_{max}$ 与 {"min": 1}、{"id": 2}。\n'
        codes = {i["code"] for i in
                 validate_prompt_content("content_generation_system", noisy)}
        assert "contract_var_added" not in codes

    @pytest.mark.parametrize("mutate,expect", [
        ("{scheme_name}", "contract_var_removed"),
        ("{custom_business_field}", "contract_var_added"),
    ])
    def test_real_contract_drift_still_reported(self, mutate, expect):
        """反向：真实契约漂移必须仍然报出（过滤不得掩盖真问题）。"""
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, validate_prompt_content,
        )

        default = _ALL_PROMPTS["content_generation_system"]["default_content"]
        if mutate == "{scheme_name}":
            text = default.replace(mutate, "方案名称")
        else:
            text = default + f"\n\n补充：{mutate} 必须填写。\n"
        codes = {i["code"] for i in
                 validate_prompt_content("content_generation_system", text)}
        assert expect in codes, f"{expect} 未报出（误报过滤过头）: {codes}"
