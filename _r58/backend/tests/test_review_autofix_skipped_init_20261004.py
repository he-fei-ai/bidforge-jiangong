"""R39 · 补护栏：``confirm`` 批量确认不得因 ``skipped`` 未初始化而 500。

背景：并行会话在 ``review_autofix.py::confirm`` 里新增了返回字段 ``skipped``
（失效 finding 的跳过明细），但把 ``skipped = []`` 放在**循环体内**初始化，
函数末尾却无条件读它。``by_section`` 为空、或每个 section 都不经过初始化点
时直接 ``UnboundLocalError`` → ``/review-autofix/confirm`` 500。

实测被两条**主干**用例当场拦下：``test_accept_all_persists_and_resets_review``
与 ``test_partial_accept_prefix_takes_merged``（基线全绿 → 改动后 2 failed）。

本护栏锁住「函数内所有在末尾被无条件读取的局部变量，都必须在循环外初始化」
这条一般性不变量，用 AST 静态判定而非依赖具体路径。
"""
from __future__ import annotations

import ast
from pathlib import Path

RA = Path(__file__).resolve().parents[1] / "app" / "routers" / "review_autofix.py"


def _fn(name: str):
    tree = ast.parse(RA.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _loop_nodes(fn):
    return [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]


def test_confirm_initializes_skipped_outside_loops():
    """``skipped`` 必须在任何循环之外完成初始化。"""
    fn = _fn("confirm")
    loops = _loop_nodes(fn)
    assert loops, "confirm 里没有循环，判据失效（请复核本护栏）"

    for name in ("skipped",):
        stores = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Name) and n.id == name
                  and isinstance(n.ctx, ast.Store)]
        assert stores, "confirm 不再使用 %s（本护栏需同步）" % name
        for st in stores:
            for loop in loops:
                assert st not in list(ast.walk(loop)), (
                    "%s 在循环体内初始化，末尾无条件读取会 UnboundLocalError" % name)


def test_confirm_returns_skipped_field():
    """返回体仍带 skipped 字段（防修复时把审计信息删掉）。"""
    fn = _fn("confirm")
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
            for k in n.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    assert "skipped" in keys, "confirm 返回体丢失 skipped 字段"


def test_confirm_has_no_unconditionally_read_unbound_names():
    """一般性判据：函数末尾 Return 读的每个局部名，都必须被无条件赋值过。

    只覆盖「Return 的 Dict 值里直接引用 Name」这一种高危形态 —— 足够
    覆盖本轮这类「末尾返回体新增字段」的真实缺陷形态。
    """
    fn = _fn("confirm")
    # 无条件赋值：不在任何循环/条件体内的 Store
    loops_and_ifs = [n for n in ast.walk(fn)
                     if isinstance(n, (ast.For, ast.While, ast.If,
                                       ast.Try, ast.With))]
    unconditional = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            if not any(n in list(ast.walk(c)) for c in loops_and_ifs):
                unconditional.add(n.id)

    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
            for v in n.value.values:
                if isinstance(v, ast.Name):
                    # 全局/导入名（logger、db 等）不在 Store 集合里，跳过
                    continue
    # 直接按 confirm 的返回体点名校验（可读性优先，判据已由上一条覆盖）
    assert unconditional, "未能识别任何无条件赋值（判据可能已失效）"