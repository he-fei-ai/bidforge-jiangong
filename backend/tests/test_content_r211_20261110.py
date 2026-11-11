"""R211（2026-11-10）正文生成模块 · 深度审计增强护栏（后端）

轮次背景
--------
用户指令「聚焦【正文生成模块】，进行深度审计、增强优化，并修复过程中发现的所有
BUG……先建立任务计划并探查模块结构，再深度审计、跑测试建基线、增强优化、修 BUG、
回归验证」。

本文件锁住 R211 后端落地的 3 项修复 + 1 条防回流棘轮，每条护栏以**行为实证**为
主、源码棘轮为辅：

  FIX-1 P1  落库字数门控必须用「正文是否真的变了」判据（``numbering.
            content_actually_changed``），而不是规范化函数返回的 ``changed``
            布尔 —— 后者**只统计子标题改写**，而返回值恒是**净化后**文本
            （字面 \\n 还原 / 分页页脚整行删除 / 表题粘连拆分）⇒ 仅净化时
            ``changed=False`` 而正文已变 ⇒ 落库旧字数（DB word_count ≠ 实际
            字数），字数档位 / 续写触发 / 自动压缩触发随之判错。
            本轮把**四处**调用点（手工保存 / 改名残留清理 / 批量重规范化 /
            压缩路径 / 正文生成路径）一次收口到唯一判据出口。
  FIX-2 P2  编排数据两个入口（``ContentPlanData.from_json`` / ``validate``）的
            布尔归一与图表类型白名单必须**同结论**（此前 ``"false"`` 字符串在
            两个入口一个判假一个判真）。
  FIX-3 P3  ``ALPHABET``（52 项）不得被用于生成 L7 字母编号 —— 防 R209 已修的
            「第 53 项起塌缩为 az」缺陷回流（唯一实现是 ``numbering.alpha_label``）。

禁改原则：这些断言锁的是**意图**（净化结果必须落库 / 两入口同口径 / L7 字母
不得塌缩），不是实现细节；重构实现时请同步更新断言，但不得删断言。
"""

from __future__ import annotations

import ast
import pathlib

import app.services.content_plan as cp
import pytest
from app.services import numbering
from app.services.content_sanitize import normalize_raw_content

_BACKEND = pathlib.Path(__file__).resolve().parent.parent

#: 含分页页脚残留的正文（净化型改写的真实触发形态之一）
_FOOTER_SRC = "第一段正文。\n第 1 页 共 10 页\n第二段正文。"


# ===========================================================================
# FIX-1 · 「正文是否真的变了」唯一判据
# ===========================================================================


class TestContentActuallyChanged:
    def test_pure_text_change_is_detected(self):
        assert numbering.content_actually_changed("abc", "abd", False) is True

    def test_no_change_is_false(self):
        assert numbering.content_actually_changed("abc", "abc", False) is False

    def test_changed_flag_still_counts(self):
        assert numbering.content_actually_changed("abc", "abc", True) is True

    def test_empty_vs_none_treated_equal(self):
        assert numbering.content_actually_changed("", None, False) is False

    def test_sanitize_only_change_is_detected(self):
        """核心场景：净化型改写（正文变了但子标题没改）必须判「变了」。"""
        out, _stats = normalize_raw_content(_FOOTER_SRC)
        assert out != _FOOTER_SRC, "样例前提失效：净化未改变正文"
        assert numbering.content_actually_changed(_FOOTER_SRC, out, False) is True

    def test_normalize_returns_changed_content_even_when_flag_false(self):
        """反证：``normalize_section_content_subheadings`` 的返回值恒是净化后文本，
        而 ``changed`` 只统计子标题改写 —— 这正是「只看 changed 会漏」的根因。"""
        index = {
            "meta": {"sec1": {"number": "1", "level": 1, "title": "章"}},
            "has_children": {"sec1": False},
        }
        import asyncio

        new_content, changed = asyncio.run(
            numbering.normalize_section_content_subheadings(
                None, "s", "sec1", _FOOTER_SRC, index=index
            )
        )
        assert new_content != _FOOTER_SRC, "规范化未清除分页残留（前提失效）"
        assert changed is False, "子标题未改写时 changed 应为 False（前提失效）"
        assert numbering.content_actually_changed(_FOOTER_SRC, new_content, changed) is True


# ===========================================================================
# FIX-1 · 五处调用点必须统一走唯一判据（防同族再漂移）
# ===========================================================================


def _normalize_call_count(src: str) -> int:
    tree = ast.parse(src)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name == "normalize_section_content_subheadings":
                n += 1
    return n


class TestNormalizeCallSitesUnified:
    def test_sections_py_call_sites_use_shared_predicate(self):
        src = (_BACKEND / "app" / "routers" / "sections.py").read_text(encoding="utf-8")
        calls = _normalize_call_count(src)
        assert calls >= 4, f"调用点数量异常（{calls}），样例前提可能失效"
        assert src.count("content_actually_changed(") >= calls, (
            "sections.py 存在未走唯一判据的规范化调用点（同族漂移回流）"
        )

    def test_sse_handlers_call_sites_use_shared_predicate(self):
        src = (_BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        calls = _normalize_call_count(src)
        assert calls >= 1, "调用点数量异常，样例前提可能失效"
        assert src.count("content_actually_changed(") >= calls, (
            "sse_handlers.py 存在未走唯一判据的规范化调用点（同族漂移回流）"
        )

    def test_no_raw_changed_only_gate_remains(self):
        """不得再出现「只看 changed 就当正文没变」的旧门控形态。"""
        for rel in ("app/routers/sections.py", "app/routers/sse_handlers.py"):
            src = (_BACKEND / rel).read_text(encoding="utf-8")
            assert "if _num_changed:" not in src, f"{rel} 仍用 changed-only 门控"
            assert "if _renorm_changed:" not in src, f"{rel} 仍用 changed-only 门控"
            assert "if _ch2:" not in src, f"{rel} 仍用 changed-only 门控"


# ===========================================================================
# FIX-2 · 编排数据两入口同口径
# ===========================================================================


class TestContentPlanParity:
    @pytest.mark.parametrize(
        "raw",
        [
            True,
            False,
            "true",
            "false",
            "是",
            "否",
            "yes",
            "no",
            "1",
            "0",
            "",
            None,
        ],
    )
    def test_chart_needed_parity(self, raw):
        a = cp.ContentPlanData.from_json({"chart_needed": raw})
        b = cp.ContentPlanData.validate({"chart_needed": raw})
        assert a.chart_needed == b.chart_needed, raw

    @pytest.mark.parametrize(
        "raw",
        [True, False, "true", "false", "是", "1", "", None],
    )
    def test_table_needed_parity(self, raw):
        a = cp.ContentPlanData.from_json({"table_needed": raw})
        b = cp.ContentPlanData.validate({"table_needed": raw})
        assert a.table_needed == b.table_needed, raw

    def test_chart_types_whitelist_parity(self):
        raw = {"chart_needed": True, "chart_types": ["flowchart", "ai_image", "evil"]}
        a = cp.ContentPlanData.from_json(raw)
        b = cp.ContentPlanData.validate(raw)
        assert a.chart_types == b.chart_types == ["flowchart"], (a.chart_types, b.chart_types)

    def test_chart_types_cleared_when_not_needed(self):
        raw = {"chart_needed": False, "chart_types": ["flowchart"]}
        assert cp.ContentPlanData.from_json(raw).chart_types == []
        assert cp.ContentPlanData.validate(raw).chart_types == []

    def test_coerce_bool_flag_is_shared(self):
        """两个入口必须走同一归一出口（不得各写一份）。"""
        assert cp.coerce_bool_flag("false") is False
        assert cp.coerce_bool_flag("是") is True
        assert cp.coerce_bool_flag(1) is True
        assert cp.coerce_bool_flag(None) is False


# ===========================================================================
# FIX-3 · ALPHABET 不得被用于 L7 编号（防 R209 缺陷回流）
# ===========================================================================


def test_alphabet_is_never_indexed():
    """``ALPHABET``（52 项）一旦被索引生成 L7 标签，第 53 项起会塌缩为 az
    （R209 已修的缺陷）。全仓 ``app/`` 下不得出现 ``ALPHABET[...]`` 取下标。"""
    hits = []
    for path in (_BACKEND / "app").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                if node.value.id == "ALPHABET":
                    hits.append(f"{path.name}:{node.lineno}")
    assert hits == [], f"ALPHABET 被下标取用（L7 字母将塌缩）：{hits}"


def test_alpha_label_is_the_single_l7_source():
    """L7 标签必须走 ``alpha_label``（Excel 续编）。"""
    assert numbering.alpha_label(27) == "aa"
    assert numbering.alpha_label(52) == "az"
    assert numbering.alpha_label(53) == "ba"
