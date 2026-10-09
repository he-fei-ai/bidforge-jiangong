"""JSON repair_key 路由契约护栏（2026-10-06 · R47-后 · 任务 3）

背景
----
R38-P1-c 把 ``collect_json_response`` 的默认 ``repair_key`` 从
``outline_json_fix_system`` 改成 ``json_schema_fix_system``（GENERIC），
目录族 9 处显式传 ``OUTLINE_REPAIR_KEY``。本轮收尾 facts 族那处
裸字符串字面量 ``"facts_json_fix_system"``（facts_extractor.py:1122）
收成具名常量 ``FACTS_REPAIR_KEY``，与 OUTLINE_REPAIR_KEY 同型。

本护栏锁死三件事：

1. **默认值不回退**：``json_response.py`` 里两个公开函数
   （``collect_json_response`` / ``collect_json_response_with_provider``）
   的 ``repair_key`` 默认参数必须是 ``GENERIC_REPAIR_KEY`` 这个 Name，
   且 ``GENERIC_REPAIR_KEY`` 常量值逐字等于 ``"json_schema_fix_system"``，
   **不得**回退到 ``outline_json_fix_system``。

2. **调用点 repair_key 形态合法**：全仓 ``backend/app/`` 下所有
   ``collect_json_response(`` / ``collect_json_response_with_provider(``
   直接调用点，要么不传 ``repair_key=``（合法 = 走默认 GENERIC），
   要么传的是 ``OUTLINE_REPAIR_KEY`` / ``FACTS_REPAIR_KEY`` /
   ``GENERIC_REPAIR_KEY`` 三个具名常量之一（按 import 别名解析）。
   **非法形态**：传了 ``repair_key=`` 但值是字符串字面量 / 拼接 /
   其他变量 / 其他 key → 红。

3. **扫描自检**：至少扫到 20 个真实直接调用点（防扫描器被改空后假绿）。

排除
----
- ``bid_analysis.py`` 自维护的简化版 ``_repair_json``（1 轮 + 正则提取），
  不是 ``collect_json_response`` 调用，本护栏不扫（刻意移植设计，见
  ``round2_inventory.md`` P2-B 节）。
- ``bid_section_extraction.py:283`` 的 ``collector = ai_collect or
  collect_json_response`` 是赋值不是调用；下游 ``collector(...)`` 是
  间接派发，本护栏只锁直接 ``collect_json_response(`` 调用点（间接
  派发默认走 GENERIC，合法）。
- ``json_response.py`` 自身的函数定义节点。
"""
from __future__ import annotations

import ast
import os
import pytest

# 允许的 repair_key 常量名（在 json_response.py 定义）
_ALLOWED_REPAIR_KEYS = {"OUTLINE_REPAIR_KEY", "FACTS_REPAIR_KEY", "GENERIC_REPAIR_KEY"}

# 被扫描的根目录（backend/app/）
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
_APP_ROOT = os.path.join(_HERE, "app")

# 定义文件（不扫它内部的调用点，但要校验它的签名/默认值）
_JSON_RESPONSE_REL = os.path.join("services", "ai", "json_response.py")


def _iter_py_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _read(abs_path: str) -> str:
    with open(abs_path, encoding="utf-8") as f:
        return f.read()


def _build_import_alias_map(tree: ast.Module) -> dict[str, str]:
    """收集本文件里从 app.services.ai.json_response 导入的名字 → 原名映射。

    支持 ``from app.services.ai.json_response import X`` 与
    ``import ... as Y``（本仓实际只用前者，防御性两种都收）。
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.endswith("services.ai.json_response"):
                for a in node.names:
                    orig = a.name
                    local = a.asname or a.name
                    aliases[local] = orig
    return aliases


class _CallSite:
    __slots__ = ("file", "lineno", "func_name", "repair_key_kind", "repair_key_value")

    def __init__(self, file: str, lineno: int, func_name: str,
                 repair_key_kind: str, repair_key_value: str):
        self.file = file
        self.lineno = lineno
        self.func_name = func_name
        # "absent" | "allowed_name" | "illegal"
        self.repair_key_kind = repair_key_kind
        self.repair_key_value = repair_key_value

    def __repr__(self) -> str:  # pragma: no cover - 仅断言失败时看
        return (f"<CallSite {self.file}:{self.lineno} {self.func_name} "
                f"repair_key={self.repair_key_kind}({self.repair_key_value!r})>")


def _scan_call_sites():
    sites: list[_CallSite] = []
    for abs_path in _iter_py_files(_APP_ROOT):
        rel = os.path.relpath(abs_path, _APP_ROOT).replace(os.sep, "/")
        if rel == _JSON_RESPONSE_REL.replace(os.sep, "/"):
            continue  # 定义文件不扫调用点
        src = _read(abs_path)
        try:
            tree = ast.parse(src, filename=abs_path)
        except SyntaxError:
            continue
        aliases = _build_import_alias_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Name):
                continue
            if fn.id not in ("collect_json_response", "collect_json_response_with_provider"):
                continue
            # 找 repair_key= kwarg
            rk_value = None
            rk_kind = "absent"
            for kw in node.keywords:
                if kw.arg == "repair_key":
                    rk_value_node = kw.value
                    if isinstance(rk_value_node, ast.Name):
                        orig = aliases.get(rk_value_node.id, rk_value_node.id)
                        if orig in _ALLOWED_REPAIR_KEYS:
                            rk_kind = "allowed_name"
                            rk_value = orig
                        else:
                            rk_kind = "illegal"
                            rk_value = f"Name({rk_value_node.id} -> {orig})"
                    elif isinstance(rk_value_node, ast.Constant) and isinstance(rk_value_node.value, str):
                        # 字符串字面量 → 非法（正是本轮要消除的形态）
                        rk_kind = "illegal"
                        rk_value = f"str({rk_value_node.value!r})"
                    else:
                        rk_kind = "illegal"
                        rk_value = f"<{type(rk_value_node).__name__}>"
                    break
            sites.append(_CallSite(rel, node.lineno, fn.id, rk_kind,
                                   rk_value or ""))
    return sites


# ---------------------------------------------------------------------------
# 护栏 1：默认值不回退（GENERIC_REPAIR_KEY 必须是 json_schema_fix_system）
# ---------------------------------------------------------------------------

def test_generic_repair_key_value_is_not_outline():
    """GENERIC_REPAIR_KEY 常量值必须是 json_schema_fix_system，不得回退 outline。"""
    abs_path = os.path.join(_APP_ROOT, _JSON_RESPONSE_REL)
    src = _read(abs_path)
    tree = ast.parse(src, filename=abs_path)
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "GENERIC_REPAIR_KEY":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        assert node.value.value == "json_schema_fix_system", (
                            f"GENERIC_REPAIR_KEY 被改回 {node.value.value!r} —— "
                            "R38-P1-c 已收口：非目录任务不得再用目录修复提示词。"
                        )
                        found = True
    assert found, "未在 json_response.py 顶层找到 GENERIC_REPAIR_KEY 赋值"


def test_public_functions_default_repair_key_is_generic_name():
    """两个公开函数的 repair_key 默认参数必须是 Name(GENERIC_REPAIR_KEY)，
    不得是字符串字面量 outline_json_fix_system / 其他 key。

    注意：``collect_json_response`` 的 repair_key 在 ``*`` 之后（kwonly），
    而 ``collect_json_response_with_provider`` 的 repair_key 是 positional-or-keyword
    （签名里没有 ``*``），所以两处默认值分别落在 ``kw_defaults`` 与 ``defaults``。
    """
    abs_path = os.path.join(_APP_ROOT, _JSON_RESPONSE_REL)
    tree = ast.parse(_read(abs_path), filename=abs_path)
    targets = {"collect_json_response", "collect_json_response_with_provider"}
    seen = set()
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in targets:
            seen.add(node.name)
            dflt = None
            # kwonlyargs（有 * 分隔的那批）
            for kw, d in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if kw.arg == "repair_key":
                    dflt = d
                    break
            # positional-or-keyword（无 * 分隔，defaults 尾部对齐 args 尾部）
            if dflt is None:
                n_args = len(node.args.args)
                n_defaults = len(node.args.defaults)
                for i, a in enumerate(node.args.args):
                    if a.arg == "repair_key":
                        # defaults 与 args 尾部对齐
                        idx_in_defaults = i - (n_args - n_defaults)
                        if 0 <= idx_in_defaults < n_defaults:
                            dflt = node.args.defaults[idx_in_defaults]
                        break
            assert dflt is not None, f"{node.name} 未声明 repair_key 参数"
            assert isinstance(dflt, ast.Name) and dflt.id == "GENERIC_REPAIR_KEY", (
                f"{node.name} 的 repair_key 默认值不是 GENERIC_REPAIR_KEY；"
                f"实际={ast.dump(dflt)}"
            )
    assert seen == targets, f"未找到全部目标函数：seen={seen}"


# ---------------------------------------------------------------------------
# 护栏 2：调用点 repair_key 形态合法
# ---------------------------------------------------------------------------

def test_all_call_sites_repair_key_form_is_legal():
    sites = _scan_call_sites()
    illegal = [s for s in sites if s.repair_key_kind == "illegal"]
    assert not illegal, (
        "以下 collect_json_response / collect_json_response_with_provider 调用点"
        "传了 repair_key= 但值不是 OUTLINE_REPAIR_KEY/FACTS_REPAIR_KEY/GENERIC_REPAIR_KEY"
        " 三个具名常量之一（拼字符串/硬编码字面量/其他变量都是漂移信号）：\n  "
        + "\n  ".join(repr(s) for s in illegal)
    )


# ---------------------------------------------------------------------------
# 护栏 3：扫描自检（≥20 个真实直接调用点）
# ---------------------------------------------------------------------------

def test_scan_finds_at_least_20_real_call_sites():
    sites = _scan_call_sites()
    assert len(sites) >= 20, (
        f"扫描只找到 {len(sites)} 个直接调用点（预期 ≥20）。"
        "若扫描器被改空/误过滤，本护栏会假绿 —— 先核对 _iter_py_files 路径与 "
        "ast.Call.func 判定。"
    )


def test_facts_extractor_uses_named_constant_not_bare_string():
    """回归锁：facts_extractor.py 那处必须传 FACTS_REPAIR_KEY（而非裸字符串）。"""
    sites = _scan_call_sites()
    facts = [s for s in sites
             if s.file == os.path.join("services", "facts_extractor.py").replace(os.sep, "/")]
    assert facts, "未在 facts_extractor.py 找到 collect_json_response 调用点"
    for s in facts:
        assert s.repair_key_kind == "allowed_name", (
            f"facts_extractor.py:{s.lineno} repair_key 形态非法：{s.repair_key_value!r}"
        )
        assert s.repair_key_value == "FACTS_REPAIR_KEY", (
            f"facts_extractor.py:{s.lineno} 应使用 FACTS_REPAIR_KEY，"
            f"实际={s.repair_key_value!r}"
        )


def test_outline_family_still_uses_outline_repair_key():
    """目录族调用点仍显式传 OUTLINE_REPAIR_KEY（R38-P1-c 不得回退）。

    不变量：全仓至少 9 处 collect_json_response 调用点显式传
    ``OUTLINE_REPAIR_KEY``（与 round2_inventory P2-A 表 9 处目录族对应）。
    注意：sse_handlers.py 里 word_budget_alloc（:5151）等非目录调用点
    合法走默认 GENERIC，不在本计数内。
    """
    sites = _scan_call_sites()
    outline_sites = [s for s in sites
                     if s.repair_key_kind == "allowed_name"
                     and s.repair_key_value == "OUTLINE_REPAIR_KEY"]
    assert len(outline_sites) >= 9, (
        f"显式传 OUTLINE_REPAIR_KEY 的调用点只有 {len(outline_sites)} 处"
        f"（预期 ≥9）：\n  " + "\n  ".join(repr(s) for s in outline_sites)
    )
