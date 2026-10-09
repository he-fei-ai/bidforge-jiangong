"""提示词预算常量单一事实源 + 字符预算分配器下沉的护栏（R47 债-1 / 债-2）。

债-1：``services/ai/prompts/_limits.py`` 集中 8 个 Token/长度预算常量；
      各消费方改 import 但保留原模块内同名绑定。
债-2：``_allocate_char_budgets`` 从 ``routers/sse_handlers`` 下沉到
      ``services/prompt_governance.allocate_char_budgets``；
      路由器不得反向 import ``sse_handlers`` 的私有符号。

护栏用 AST 遍历，不做「源码是否包含字符串 X」的假判据（R46 教训④）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_APP_DIR = _BACKEND_DIR / "app"


# ==========================================================================
# 债-1：_limits.py 单一事实源
# ==========================================================================
class TestLimitsSingleSource:
    """``_limits.py`` 是 Token/长度预算常量的唯一事实源；数值逐字沿用旧值。"""

    def test_limits_module_exists_with_expected_constants(self):
        from app.services.ai.prompts import _limits
        expected = {
            "FACTS_PROMPT_CAP": 3000,
            "FACT_PROMPT_VALUE_CAP": 120,
            "PROJECT_FACTS_LIMIT_OUTLINE": 1500,
            "PROJECT_FACTS_LIMIT_SUBLEVEL": 1000,
            "SECTION_CHUNK_LIMIT": 12000,
            "PER_SECTION_LIMIT": 6000,
            "REPAIR_REWRITE_MAX_CHARS": 12000,
            "WORD_OVER_RATIO": 1.3,
        }
        for name, want in expected.items():
            got = getattr(_limits, name, None)
            assert got == want, (
                f"_limits.{name} = {got!r}，预期 {want!r}。"
                "数值是出厂默认档，改动需同步更新护栏与 AGENTS.md。"
            )

    def test_scanner_and_repair_limits_aligned(self):
        """R38-D D2 本意：重写硬上限对齐扫描侧分片上限（都是 12000）。"""
        from app.services.ai.prompts import _limits
        assert _limits.REPAIR_REWRITE_MAX_CHARS == _limits.SECTION_CHUNK_LIMIT

    def test_consumers_rebind_original_names(self):
        """消费方仍以原模块名可读，下游业务代码零改动。"""
        from app.routers import compliance, sse_handlers
        from app.services import consistency_scanner, content_utils, repair_agent
        assert compliance.FACTS_PROMPT_CAP == 3000
        assert compliance._FACT_PROMPT_VALUE_CAP == 120
        assert sse_handlers.PROJECT_FACTS_LIMIT_OUTLINE == 1500
        assert sse_handlers.PROJECT_FACTS_LIMIT_SUBLEVEL == 1000
        assert consistency_scanner.SECTION_CHUNK_LIMIT == 12000
        assert consistency_scanner.PER_SECTION_LIMIT == 6000
        assert repair_agent.REPAIR_REWRITE_MAX_CHARS == 12000
        assert content_utils.WORD_OVER_RATIO == 1.3

    def test_consumers_import_from_limits(self):
        """AST 断言各消费方真的从 _limits import，而不是又写回字面量。

        「源码里没有 = 3000 这种字面量」是假判据（注释里会写）；这里改为：
        各消费方模块的 AST ImportFrom 必须真指向 ``app.services.ai.prompts._limits``。
        """
        targets = {
            "routers/compliance.py": {
                "FACTS_PROMPT_CAP", "FACT_PROMPT_VALUE_CAP",
            },
            "routers/sse_handlers.py": {
                "PROJECT_FACTS_LIMIT_OUTLINE", "PROJECT_FACTS_LIMIT_SUBLEVEL",
            },
            "services/consistency_scanner.py": {
                "SECTION_CHUNK_LIMIT", "PER_SECTION_LIMIT",
            },
            "services/repair_agent.py": {"REPAIR_REWRITE_MAX_CHARS"},
            "services/content_utils.py": {"WORD_OVER_RATIO"},
        }
        for rel, want_names in targets.items():
            path = _APP_DIR / rel
            tree = ast.parse(path.read_text(encoding="utf-8"))
            got: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == \
                        "app.services.ai.prompts._limits":
                    for alias in node.names:
                        got.add(alias.name)
            missing = want_names - got
            assert not missing, (
                f"{rel} 未从 _limits import {sorted(missing)}。"
                "数值已搬到 _limits，消费方必须 import，不得再写字面量。"
            )


# ==========================================================================
# 债-2：allocate_char_budgets 下沉 + 路由器不得反向 import sse_handlers 私有符号
# ==========================================================================
class TestAllocateBudgetsSink:
    """字符预算分配器的唯一实现归 ``prompt_governance``；路由器不得反向 import。"""

    def test_prompt_governance_exposes_public_function(self):
        from app.services import prompt_governance
        fn = getattr(prompt_governance, "allocate_char_budgets", None)
        assert callable(fn), "prompt_governance.allocate_char_budgets 必须存在且可调用"
        # 行为自检（与历史用例同口径）
        assert fn([], 4000) == []
        assert fn([100], 0) == [0]
        assert fn([1000, 1000, 1000], 3000) == [1000, 1000, 1000]  # 未超预算恒等

    def test_sse_handlers_retains_private_alias(self):
        """sse_handlers 内部仍以 ``_allocate_char_budgets`` 调用，行为逐字不变。"""
        from app.routers import sse_handlers
        from app.services import prompt_governance
        assert sse_handlers._allocate_char_budgets is prompt_governance.allocate_char_budgets

    def test_no_router_reverse_imports_sse_handlers_private_symbols(self):
        """AST 扫描 routers/*.py：不得出现 ``from app.routers.sse_handlers import _xxx``。

        路由器反向 import 另一个超大路由器的私有符号是分层债（R47 债-2）；
        ``sse_handlers`` 重构改名即静默退化。本护栏锁死：从此任何路由器都不得
        再 import sse_handlers 的下划线私有成员。

        R47 债-5（2026-10-06）：原 allowlist 里的 ``_build_facts_text`` 整组
        （``_load_facts_rows`` / ``_rank_facts_by_basis`` / ``_render_facts_text`` /
        ``_filter_facts_rows`` / ``_row_chapter`` / ``_chapter_inject_enabled`` /
        ``_facts_keywords`` / ``LOW_CONFIDENCE_THRESHOLD``）已下沉到
        ``services/facts_builder.py``；``compliance.py`` / ``consistency_scanner.py``
        改从 ``facts_builder`` import，allowlist 清空。
        """
        known_preexisting: set[str] = set()
        routers_dir = _APP_DIR / "routers"
        offenders: list[str] = []
        for p in routers_dir.glob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == \
                        "app.routers.sse_handlers":
                    for alias in node.names:
                        if alias.name.startswith("_") and \
                                alias.name not in known_preexisting:
                            offenders.append(
                                f"{p.name}:{node.lineno} -> sse_handlers.{alias.name}")
        assert not offenders, (
            "路由器反向 import sse_handlers 私有符号（分层反向耦合）：\n  "
            + "\n  ".join(offenders)
            + "\n\n请把该函数下沉到 services/ 下的归属模块（参考 allocate_char_budgets），"
            "再让路由器从 services import。"
        )

    def test_prompt_governance_allocate_char_budgets_called_from_at_least_two_sites(self):
        """``allocate_char_budgets`` 真被 ≥2 处 ``ast.Call`` 消费（防只定义不接线）。

        历史消费方：① ``sse_handlers._budgeted_truncate_sections`` / 事实分配
                    （以 ``as _allocate_char_budgets`` 别名调用）；
                    ② ``compliance._join_with_budget``（直调新公开名）。
        下沉后两者都应调新路径 —— 这里同时统计公开名 ``allocate_char_budgets``
        与 sse_handlers 内保留的别名 ``_allocate_char_budgets``。
        """
        call_sites: list[str] = []
        for p in _APP_DIR.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # 形式一: allocate_char_budgets(...) / _allocate_char_budgets(...)
                #         （from ... import 后直调；sse_handlers 走别名 _allocate_char_budgets）
                if isinstance(fn, ast.Name) and fn.id in (
                        "allocate_char_budgets", "_allocate_char_budgets"):
                    call_sites.append(f"{p.relative_to(_APP_DIR)}:{node.lineno}")
                # 形式二: <pkg>.allocate_char_budgets(...)（模块前缀）
                elif (isinstance(fn, ast.Attribute) and fn.attr == "allocate_char_budgets"
                      and isinstance(fn.value, ast.Name)):
                    call_sites.append(f"{p.relative_to(_APP_DIR)}:{node.lineno}")
        assert len(call_sites) >= 2, (
            f"allocate_char_budgets 仅 {len(call_sites)} 处调用：{call_sites}。"
            "下沉后 sse_handlers 与 compliance 两个调用方都应切到新路径。"
        )
# ==========================================================================
# 债-5：_build_facts_text 整组下沉到 services/facts_builder（R47 债-5）
# ==========================================================================
class TestFactsBuilderSink:
    """``services/facts_builder.py`` 是项目关键事实构建的唯一事实源。

    历史上 ``_build_facts_text`` 连同 7 个 helper + 1 个常量住在超大路由器
    ``sse_handlers`` 里，被 ``routers/compliance.py`` 与
    ``services/consistency_scanner.py`` 反向 import（路由→路由、service→路由）。
    现整组下沉为 service，依赖方向单向化（service 不依赖 router）。
    """

    def test_facts_builder_exposes_public_function(self):
        from app.services import facts_builder
        fn = getattr(facts_builder, "build_facts_text", None)
        assert callable(fn), "facts_builder.build_facts_text 必须存在且可调用"
        # LOW_CONFIDENCE_THRESHOLD 也应一并下沉
        assert facts_builder.LOW_CONFIDENCE_THRESHOLD == 0.5

    def test_sse_handlers_retains_legacy_alias(self):
        """sse_handlers 内部旧调用点（4121/5234/5921/6002/1636）零改动：
        旧私有名 ``_build_facts_text`` 必须是新公开名的同一对象。"""
        from app.routers import sse_handlers
        from app.services import facts_builder
        assert sse_handlers.build_facts_text is facts_builder.build_facts_text
        assert sse_handlers._build_facts_text is facts_builder.build_facts_text

    def test_no_service_reverse_imports_sse_handlers_private_symbols(self):
        """AST 扫描 services/*.py：不得出现 ``from app.routers.sse_handlers import _xxx``。

        service → router 反向 import 是分层债；与 routers/*.py 同型护栏互补。
        R47 债-5 已把 ``consistency_scanner.py`` 的反向 import 切到
        ``services/facts_builder``，allowlist 为空。
        """
        services_dir = _APP_DIR / "services"
        offenders: list[str] = []
        for p in services_dir.glob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == \
                        "app.routers.sse_handlers":
                    for alias in node.names:
                        if alias.name.startswith("_"):
                            offenders.append(
                                f"{p.name}:{node.lineno} -> sse_handlers.{alias.name}")
        assert not offenders, (
            "service 反向 import sse_handlers 私有符号（分层反向耦合）：\n  "
            + "\n  ".join(offenders)
            + "\n\n请把该函数下沉到 services/ 下的归属模块（参考 facts_builder），"
            "再让 service 从 facts_builder import。"
        )

    def test_build_facts_text_called_from_at_least_two_sites(self):
        """``build_facts_text`` 真被 ≥2 处 ``ast.Call`` 消费（防只定义不接线）。

        历史消费方：① ``sse_handlers`` 目录生成主链路（经 ``_build_facts_text`` 别名）；
                    ② ``routers/compliance.run_consistency_audit``（直调新公开名）；
                    ③ ``services/consistency_scanner.build_global_facts_text``（直调新公开名）。
        """
        call_sites: list[str] = []
        for p in _APP_DIR.rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in (
                        "build_facts_text", "_build_facts_text"):
                    call_sites.append(f"{p.relative_to(_APP_DIR)}:{node.lineno}")
                elif (isinstance(fn, ast.Attribute) and fn.attr == "build_facts_text"
                      and isinstance(fn.value, ast.Name)):
                    call_sites.append(f"{p.relative_to(_APP_DIR)}:{node.lineno}")
        assert len(call_sites) >= 2, (
            f"build_facts_text 仅 {len(call_sites)} 处调用：{call_sites}。"
            "下沉后 sse_handlers / compliance / consistency_scanner 三个调用方都应切到新路径。"
        )

    def test_known_consumers_import_from_facts_builder(self):
        """AST 断言两个原反向 import 消费方真改从 facts_builder import。"""
        targets = {
            "routers/compliance.py": {"build_facts_text"},
            "services/consistency_scanner.py": {"build_facts_text"},
        }
        for rel, want_names in targets.items():
            path = _APP_DIR / rel
            tree = ast.parse(path.read_text(encoding="utf-8"))
            got: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == \
                        "app.services.facts_builder":
                    for alias in node.names:
                        got.add(alias.name)
            missing = want_names - got
            assert not missing, (
                f"{rel} 未从 facts_builder import {sorted(missing)}。"
                "债-5 下沉后消费方必须从新路径 import，不得再回 sse_handlers。"
            )
