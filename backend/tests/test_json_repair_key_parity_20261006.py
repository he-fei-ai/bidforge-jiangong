"""JSON repair_key 路由契约护栏（2026-10-06 · R47-后 · 任务 3；R148 扫描器盲区收口）

背景
----
R38-P1-c 把 ``collect_json_response`` 的默认 ``repair_key`` 从
``outline_json_fix_system`` 改成 ``json_schema_fix_system``（GENERIC），
目录族 9 处显式传 ``OUTLINE_REPAIR_KEY``。本轮收尾 facts 族那处
裸字符串字面量 ``"facts_json_fix_system"``（facts_extractor.py:1122）
收成具名常量 ``FACTS_REPAIR_KEY``，与 OUTLINE_REPAIR_KEY 同型。

R148 盲区收口（2026-11-02，目录生成模块深度探索轮）
--------------------------------------------------
原扫描器只认 ``ast.Name`` 直接调用，对真实生产调用面存在三类结构性
盲区 —— 分步生成编排重构后目录族真实调用点已迁出/新增，旧锚
（≥9 / ≥20）假红：

1. **属性形态** ``_sh.collect_json_response(...)``（``routers/_sse_outline_review.py``
   6 处，全部显式传 ``_sh.OUTLINE_REPAIR_KEY``）—— ``_sh`` 是该模块
   延迟绑定 sse_handlers 的 facade，调用点是 ``ast.Attribute`` 形态。
2. **统一通道** ``collect_json_response_with_chat(chat_fn, ...)``（R57/R58，
   4 处：bid_analysis / consistency_edits / content_fact_autofix /
   content_plan）—— repair_key 在包装器内部解析（缺省 GENERIC），
   调用方不传 repair_key 属合法第四形态。
3. **关键字注入派发** ``_collect=_sh.collect_json_response``（调用方注入
   chat 函数，``services/outline_title_fix.py:371`` 内部经 ``_collect(...)``
   派发并显式传 ``repair_key=OUTLINE_REPAIR_KEY``）—— 非直接调用，
   扫描器不计数，由专项 AST 断言覆盖（见
   ``test_outline_title_fix_collect_is_outline_key``）。

阈值重锚：全量真实直接调用点 15（Name）+ 6（属性）+ 4（统一通道）
= **25**（护栏 ≥20 → ≥25）；显式 OUTLINE_REPAIR_KEY = 4（Name 形态
目录族）+ 6（``_sh`` 属性形态）= **10**（护栏 ≥9 → ≥10）。

本护栏锁死四件事：

1. **默认值不回退**：``json_response.py`` 里两个公开函数
   （``collect_json_response`` / ``collect_json_response_with_provider``）
   的 ``repair_key`` 默认参数必须是 ``GENERIC_REPAIR_KEY`` 这个 Name，
   且 ``GENERIC_REPAIR_KEY`` 常量值逐字等于 ``"json_schema_fix_system"``，
   **不得**回退到 ``outline_json_fix_system``。

2. **调用点 repair_key 形态合法**：全仓 ``backend/app/`` 下所有
   ``collect_json_response(`` / ``collect_json_response_with_provider(`` /
   ``collect_json_response_with_chat(`` 直接调用点（Name 或 Attribute
   形态均计），要么不传 ``repair_key=``（合法 = 走默认 GENERIC /
   统一通道内解析），要么传的是 ``OUTLINE_REPAIR_KEY`` /
   ``FACTS_REPAIR_KEY`` / ``GENERIC_REPAIR_KEY`` 三个具名常量之一
   （Name 按 import 别名解析；Attribute 取 ``.attr`` 判定，如
   ``_sh.OUTLINE_REPAIR_KEY``）。**非法形态**：传了 ``repair_key=``
   但值是字符串字面量 / 拼接 / 其他变量 → 红。

3. **扫描自检（非空转自证）**：至少扫到 25 个真实直接调用点
   （防扫描器被改空后假绿）；``_sse_outline_review.py`` 的 6 处
   ``_sh`` 属性形态、统一通道 4 文件、``outline_title_fix`` 派发
   各有专项断言 —— 扫描器漏掉任一类即红，不再依赖单一总数。

4. **派发点显式键**：``outline_title_fix.py`` 的 ``_try_outline_title_fix``
   函数体内必须存在 ``repair_key=OUTLINE_REPAIR_KEY`` 的真实调用
   （AST 断言，非「源码含字符串」假判据）。

排除
----
- ``bid_analysis.py`` 自维护的简化版 ``_repair_json``（1 轮 + 正则提取），
  不是 ``collect_json_response`` 调用，本护栏不扫（刻意移植设计，见
  ``round2_inventory.md`` P2-B 节）。
- ``bid_section_extraction.py:283`` 的 ``collector = ai_collect or
  collect_json_response`` 是赋值不是调用；下游 ``collector(...)`` 是
  间接派发，本护栏只锁直接调用点（间接派发默认走 GENERIC，合法）。
- ``json_response.py`` 自身的函数定义节点。
"""

from __future__ import annotations

import ast
import os

import pytest

# 允许的 repair_key 常量名（在 json_response.py 定义）
_ALLOWED_REPAIR_KEYS = {"OUTLINE_REPAIR_KEY", "FACTS_REPAIR_KEY", "GENERIC_REPAIR_KEY"}

# 计数目标：R148 起纳入属性形态与统一通道
_COLLECT_FAMILY = ("collect_json_response", "collect_json_response_with_provider")
_TARGET_NAMES = _COLLECT_FAMILY + ("collect_json_response_with_chat",)

# 被扫描的根目录（backend/app/）
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
_APP_ROOT = os.path.join(_HERE, "app")

# 定义文件（不扫它内部的调用点，但要校验它的签名/默认值）
_JSON_RESPONSE_REL = os.path.join("services", "ai", "json_response.py")

# R57/R58 统一通道的既有调用文件（非空转自证②）
_UNIFIED_CHAT_FILES = {
    "routers/bid_analysis.py",
    "services/consistency_edits.py",
    "services/content_fact_autofix.py",
    "services/content_plan.py",
}


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


def _classify_repair_key_value(value_node: ast.expr, aliases: dict[str, str]) -> tuple[str, str]:
    """repair_key= 右值的合法性判定。

    Name → 按 import 别名解析到三个具名常量之一（allowed_name）；
    Attribute → 取 ``.attr`` 判定（覆盖 ``_sh.OUTLINE_REPAIR_KEY`` 这类
    facade 属性形态）；字符串字面量 / 其它形态一律非法。
    返回 (kind, 展示值)。
    """
    if isinstance(value_node, ast.Name):
        orig = aliases.get(value_node.id, value_node.id)
        if orig in _ALLOWED_REPAIR_KEYS:
            return "allowed_name", orig
        return "illegal", f"Name({value_node.id} -> {orig})"
    if isinstance(value_node, ast.Attribute):
        if value_node.attr in _ALLOWED_REPAIR_KEYS:
            return "allowed_name", value_node.attr
        return "illegal", f"Attr({value_node.attr})"
    if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
        # 字符串字面量 → 非法（正是 R38-P1-c 要消除的形态）
        return "illegal", f"str({value_node.value!r})"
    return "illegal", f"<{type(value_node).__name__}>"


class _CallSite:
    __slots__ = ("file", "lineno", "func_name", "call_form", "repair_key_kind", "repair_key_value")

    def __init__(
        self, file: str, lineno: int, func_name: str, call_form: str, repair_key_kind: str, repair_key_value: str
    ):
        self.file = file
        self.lineno = lineno
        self.func_name = func_name
        self.call_form = call_form  # "name" | "attr"
        # "absent" | "allowed_name" | "illegal"
        self.repair_key_kind = repair_key_kind
        self.repair_key_value = repair_key_value

    def __repr__(self) -> str:  # pragma: no cover - 仅断言失败时看
        return (
            f"<CallSite {self.file}:{self.lineno} {self.func_name}"
            f"[{self.call_form}] repair_key={self.repair_key_kind}"
            f"({self.repair_key_value!r})>"
        )


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
            target = None
            call_form = None
            if isinstance(fn, ast.Name):
                if fn.id in _TARGET_NAMES:
                    target = fn.id
                    call_form = "name"
            elif isinstance(fn, ast.Attribute):
                # 属性形态：_sh.collect_json_response(...) 等 facade 派发
                if fn.attr in _TARGET_NAMES:
                    target = fn.attr
                    call_form = "attr"
            if target is None:
                continue
            # 找 repair_key= kwarg
            rk_value = None
            rk_kind = "absent"
            for kw in node.keywords:
                if kw.arg == "repair_key":
                    rk_kind, rk_value = _classify_repair_key_value(kw.value, aliases)
                    break
            sites.append(_CallSite(rel, node.lineno, target, call_form, rk_kind, rk_value or ""))
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
                f"{node.name} 的 repair_key 默认值不是 GENERIC_REPAIR_KEY；" f"实际={ast.dump(dflt)}"
            )
    assert seen == targets, f"未找到全部目标函数：seen={seen}"


# ---------------------------------------------------------------------------
# 护栏 2：调用点 repair_key 形态合法
# ---------------------------------------------------------------------------


def test_all_call_sites_repair_key_form_is_legal():
    sites = _scan_call_sites()
    illegal = [s for s in sites if s.repair_key_kind == "illegal"]
    assert not illegal, (
        "以下 collect_json_response / collect_json_response_with_provider / "
        "collect_json_response_with_chat 调用点传了 repair_key= 但值不是 "
        "OUTLINE_REPAIR_KEY/FACTS_REPAIR_KEY/GENERIC_REPAIR_KEY 三个具名常量之一"
        "（拼字符串/硬编码字面量/其他变量都是漂移信号）：\n  " + "\n  ".join(repr(s) for s in illegal)
    )


# ---------------------------------------------------------------------------
# 护栏 3：扫描自检（≥25 个真实直接调用点 + 三类形态各配非空转自证）
# ---------------------------------------------------------------------------


def test_scan_finds_at_least_25_real_call_sites():
    sites = _scan_call_sites()
    assert len(sites) >= 25, (
        f"扫描只找到 {len(sites)} 个直接调用点（预期 ≥25 = 15 Name 形态 "
        "+ 6 _sh 属性形态 + 4 统一通道；R148 重锚）。"
        "若扫描器被改空/误过滤，本护栏会假绿 —— 先核对 _iter_py_files 路径与 "
        "ast.Call.func 判定。实际调用点：\n  " + "\n  ".join(repr(s) for s in sites)
    )


def test_sse_outline_review_facade_sites_are_covered():
    """非空转自证①：``_sse_outline_review.py`` 的 6 处 ``_sh.collect_json_response``
    属性形态必须被扫描器真实扫到，且全部显式传 ``_sh.OUTLINE_REPAIR_KEY``。

    单靠总数断言，扫描器漏掉属性形态后总数仍可能 ≥20（历史假红根因），
    故按文件 + 形态 + 键值三重收窄。
    """
    sites = _scan_call_sites()
    facade = [s for s in sites if s.file == "routers/_sse_outline_review.py"]
    assert len(facade) >= 6, (
        f"_sse_outline_review.py 只扫到 {len(facade)} 处调用点（预期 ≥6）。"
        "若扫描器退回只认 ast.Name（R148 前形态），属性形态会被整体漏掉。"
    )
    for s in facade:
        assert s.call_form == "attr", (
            f"{s.file}:{s.lineno} 形态 {s.call_form!r} 非属性形态（分步编排 "
            "重构不应把 _sh facade 调用改回裸 import）。"
        )
        assert s.repair_key_kind == "allowed_name", f"{s.file}:{s.lineno} repair_key 形态非法：{s.repair_key_value!r}"
        assert s.repair_key_value == "OUTLINE_REPAIR_KEY", (
            f"{s.file}:{s.lineno} 是目录族调用点，应使用 OUTLINE_REPAIR_KEY，" f"实际={s.repair_key_value!r}"
        )


def test_unified_chat_channel_sites_are_covered():
    """非空转自证②：R57/R58 统一通道 4 文件必须被扫到。

    ``collect_json_response_with_chat`` 的 repair_key 在包装器内部解析
    （缺省 GENERIC），调用方不传属合法第四形态；但若某调用方显式传了，
    仍受护栏 2 的形态合法性约束。
    """
    sites = _scan_call_sites()
    by_file: dict[str, int] = {}
    for s in sites:
        if s.func_name == "collect_json_response_with_chat":
            by_file[s.file] = by_file.get(s.file, 0) + 1
    missing = _UNIFIED_CHAT_FILES - set(by_file)
    assert not missing, (
        f"统一通道文件缺扫：{sorted(missing)}。若扫描器被改空/误过滤 "
        "（例如只认 collect_json_response 两个名字），本护栏会假绿。"
    )


def test_facts_extractor_uses_named_constant_not_bare_string():
    """回归锁：facts_extractor.py 那处必须传 FACTS_REPAIR_KEY（而非裸字符串）。"""
    sites = _scan_call_sites()
    facts = [s for s in sites if s.file == os.path.join("services", "facts_extractor.py").replace(os.sep, "/")]
    assert facts, "未在 facts_extractor.py 找到 collect_json_response 调用点"
    for s in facts:
        assert (
            s.repair_key_kind == "allowed_name"
        ), f"facts_extractor.py:{s.lineno} repair_key 形态非法：{s.repair_key_value!r}"
        assert s.repair_key_value == "FACTS_REPAIR_KEY", (
            f"facts_extractor.py:{s.lineno} 应使用 FACTS_REPAIR_KEY，" f"实际={s.repair_key_value!r}"
        )


def test_outline_family_still_uses_outline_repair_key():
    """目录族调用点仍显式传 OUTLINE_REPAIR_KEY（R38-P1-c 不得回退）。

    不变量：全仓至少 10 处直接调用点显式传 ``OUTLINE_REPAIR_KEY``
    （R148 重锚：Name 形态 4 处 + ``_sse_outline_review.py`` 的 ``_sh``
    属性形态 6 处）。注意：sse_handlers.py 里 word_budget_alloc（:2211）
    等非目录调用点合法走默认 GENERIC，不在本计数内。
    """
    sites = _scan_call_sites()
    outline_sites = [
        s for s in sites if s.repair_key_kind == "allowed_name" and s.repair_key_value == "OUTLINE_REPAIR_KEY"
    ]
    assert len(outline_sites) >= 10, (
        f"显式传 OUTLINE_REPAIR_KEY 的调用点只有 {len(outline_sites)} 处"
        f"（预期 ≥10）：\n  " + "\n  ".join(repr(s) for s in outline_sites)
    )


# ---------------------------------------------------------------------------
# 护栏 4：派发点显式键（outline_title_fix 的 _collect 注入派发）
# ---------------------------------------------------------------------------


def test_outline_title_fix_collect_is_outline_key():
    """非空转自证③：``outline_title_fix.py`` 经 ``_collect=_sh.collect_json_response``
    关键字注入派发（调用方注入，非直接调用，扫描器不计），其内部
    ``_collect(...)`` 调用必须显式传 ``repair_key=OUTLINE_REPAIR_KEY``。

    AST 真实调用节点断言，非「源码含字符串」假判据。
    """
    abs_path = os.path.join(_APP_ROOT, "services", "outline_title_fix.py")
    tree = ast.parse(_read(abs_path), filename=abs_path)
    aliases = _build_import_alias_map(tree)
    fn_node = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_try_outline_title_fix":
            fn_node = node
            break
    assert fn_node is not None, "未在 outline_title_fix.py 找到 _try_outline_title_fix"
    hits: list[str] = []
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "repair_key":
                v = kw.value
                if isinstance(v, ast.Name):
                    hits.append(aliases.get(v.id, v.id))
                elif isinstance(v, ast.Attribute):
                    hits.append(v.attr)
                else:
                    hits.append(f"<{type(v).__name__}>")
    assert hits, (
        "_try_outline_title_fix 内未找到任何 repair_key= 实参 —— "
        "外科式标题重写是目录族调用，缺显式键会静默走 GENERIC 修复提示词。"
    )
    assert hits == ["OUTLINE_REPAIR_KEY"], (
        f"_try_outline_title_fix 的 repair_key 应恰为 OUTLINE_REPAIR_KEY，" f"实际={hits!r}"
    )
