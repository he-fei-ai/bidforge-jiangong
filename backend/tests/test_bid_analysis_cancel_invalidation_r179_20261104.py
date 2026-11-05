# -*- coding: utf-8 -*-
"""结构化提取「取消 / 异常」收尾必须失效下游缓存（R179 · 2026-11-04）。

运行库证据：
  - task 89981f59（bid_analysis，15:15:46 → 15:40:58）在客户端断开后被取消；
  - 日志 15:40:58「清理了 1 个中断遗留的 running 解析项」，**没有**任何
    「提取结果变化：已失效 N 个方案的导出缓存」行；
  - 而该轮 16/17 项已于 15:22~15:40 写进 `bid_analysis_items`（status=success）。
  - 收尾的 `_safe_invalidate_downstream` 写在
    `_run_bid_analysis_with_progress` 末尾（normal 路径），CancelledError 从
    `asyncio.gather` / `_get_missing_required` 处抛出后**永远走不到**；
    与函数注释「停止场景同样生效」自相矛盾。

不变量：`run_inner` 的 `except asyncio.CancelledError` 与 `except Exception`
两个分支都必须调用 `_safe_invalidate_downstream`，否则已落库的部分结果会
继续命中旧导出/一致性缓存。
"""

import ast
import inspect

from app.routers import bid_analysis as ba


def _iter_runcinner_handlers(tree):
    """返回 run_inner 内所有 ExceptHandler，及其分支体内的函数调用名集合。"""
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_inner":
            target = node
            break
    assert target is not None, "bid_analysis.py 中找不到 run_inner（结构已变，请同步本护栏）"

    out = []
    for node in ast.walk(target):
        if isinstance(node, ast.ExceptHandler):
            names = {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            type_name = ""
            if node.type is not None:
                type_name = (
                    getattr(node.type, "id", None) or getattr(getattr(node.type, "attr", None), "__str__", lambda: "")()
                )
            out.append((type_name, names))
    return out


def test_cancel_and_error_handlers_invalidate_downstream_cache():
    tree = ast.parse(inspect.getsource(ba))
    handlers = _iter_runcinner_handlers(tree)
    cancel = [n for t, n in handlers if t == "CancelledError"]
    error = [n for t, n in handlers if t == "Exception"]
    assert cancel, "run_inner 缺少 except asyncio.CancelledError 收口分支"
    assert (
        "_safe_invalidate_downstream" in cancel[0]
    ), "取消分支未失效下游缓存：已落库的部分提取结果会继续命中旧 export_cache"
    assert (
        "_safe_invalidate_downstream" in error[0]
    ), "异常分支未失效下游缓存：已落库的部分提取结果会继续命中旧 export_cache"
