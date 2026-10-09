"""SSE 路由装饰器错位护栏（2026-10-01）。

✅ P0 事故回归锁：``@router.post("/generate-facts/{scheme_id}")`` 曾错位挂在
内部辅助函数 ``_project_doc_diag_cols`` 上（其 ``db`` 参数无 Depends →
FastAPI 把它当**必需查询参数** → 恒 422），而真正的 ``generate_facts``
没有路由 —— 事实生成 HTTP 入口从错位起**完全不可用**
（生产 ``logs/backend_err.log`` 2026-10-01 14:07 两次 422 实证；
同期 ``/generate-facts`` 之前的 09-30 15:09 任务仍 completed，
说明错位发生在 09-30 之后的某轮编辑）。

根因同构（AGENTS.md §4.26）：编辑 sse_handlers.py 这类超大文件时，
「在函数前插入辅助函数」会把原本属于下一函数的装饰器留在旧函数头上，
工具不报错、结构合法，只有 HTTP 调用才暴露。

本文件锁定三层防线：
1. AST：``/generate-facts/{scheme_id}`` 路由装饰器必须挂在 ``generate_facts``；
2. 行级：sse_handlers.py 全部 ``@router.*`` 装饰器的紧邻下一行必须是
   ``async def``（装饰器错位的通用防线）；
3. 端到端：FastAPI 应用路由表里该路径已注册且 endpoint 名为 ``generate_facts``
   （静态扫描全部失效时的最后一道防线）。
"""
import ast
import io
import os

import pytest

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
_SSE_PATH = os.path.join(_APP_DIR, "routers", "sse_handlers.py")
_FACTS_ROUTE = "/generate-facts/{scheme_id}"


def _read_source() -> str:
    with io.open(_SSE_PATH, "rb") as f:
        return f.read().decode("utf-8")


def _decorated_function_names(source: str, route_path: str) -> list[str]:
    """AST 解析：带指定路径路由装饰器的函数名清单（多个即错位/重复注册）"""
    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            if dec.args[0].value == route_path:
                names.append(node.name)
    return names


class TestGenerateFactsRouteDecorator:
    def test_route_decorates_generate_facts(self):
        """装饰器必须挂在 generate_facts 上（P0 事故回归锁）"""
        names = _decorated_function_names(_read_source(), _FACTS_ROUTE)
        assert names == ["generate_facts"], (
            f"路由 {_FACTS_ROUTE} 应且仅应挂在 generate_facts 上，当前：{names}"
        )

    def test_route_registered_in_app(self):
        """端到端：路由已注册且 endpoint 为 generate_facts

        这是「接线类护栏要选必须依赖真实注册才有结论」的教训（§4.26.6）——
        静态扫描若被绕过（如动态注册），此处仍能发现。
        ⚠️ 遍历口径：应用层用 _IncludedRouter 包装（无 path 属性），需递归展开；
        直接平铺过滤会因取不到 path 而**恒空**（首版即踩此坑）。
        """
        from app.main import app

        def walk(routes):
            for r in routes:
                path = getattr(r, "path", None)
                if path is None:
                    # _IncludedRouter 包装：子路由经 original_router 暴露
                    sub = getattr(r, "routes", None) or getattr(
                        r, "original_router", None)
                    yield from walk(getattr(sub, "routes", []) or [])
                else:
                    yield r

        endpoints = [
            r for r in walk(app.routes)
            if getattr(r, "path", "") == f"/api/v1/sse{_FACTS_ROUTE}"
            and "POST" in getattr(r, "methods", set())
        ]
        assert len(endpoints) == 1, f"路由 {_FACTS_ROUTE} 应恰好注册 1 次"
        assert endpoints[0].endpoint.__name__ == "generate_facts"

    def test_422_root_cause_absent(self):
        """根因形态检测：带路由装饰器的函数若有 db 形参，则必须带默认值

        错位事故的 422 根因是「装饰器挂在了内部辅助函数上」——辅助函数的
        ``db`` 无默认值，FastAPI 把 db 当**必需查询参数** → 恒 422。
        ⚠️ 判据收敛（§5.14）：只锁「db 无默认值」这一形态；不要求所有端点
        都有 db/request 形参（task_control 等 task_id+body 端点本就无需 db，
        首版判据过宽把正确实现判成缺陷）。
        """
        tree = ast.parse(_read_source())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            has_route = any(
                isinstance(d, ast.Call) and d.args
                and isinstance(d.args[0], ast.Constant)
                and isinstance(d.args[0].value, str)
                and d.args[0].value.startswith("/")
                and getattr(d.func, "attr", "") in ("post", "get", "put", "delete", "patch")
                for d in node.decorator_list
            )
            if not has_route:
                continue
            arg_names = [a.arg for a in node.args.args]
            defaults = list(node.args.defaults)
            # 形参默认值对齐：defaults 从右往左对应 args 尾部
            defaults_padded = [None] * (len(arg_names) - len(defaults)) + defaults
            for name, d in zip(arg_names, defaults_padded):
                if name == "db" and d is None:
                    pytest.fail(
                        f"路由端点 {node.name} 的 db 形参缺少默认值（Depends）——"
                        f"FastAPI 会把 db 当必需查询参数导致恒 422（疑似装饰器错位）"
                    )



class TestAllRouteDecoratorsFollowedByDef:
    def test_decorator_next_line_is_def(self):
        """sse_handlers.py 全部 @router.* 装饰器的紧邻下一行必须是函数定义

        装饰器错位的通用防线：任何「装饰器留在旧函数头上 / 与函数之间插入
        了其它内容」的形态都会被本用例拦下。
        """
        source = _read_source()
        lines = source.splitlines()
        problems = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("@router."):
                continue
            # 找装饰器的最后一行（跨行装饰器 @router.post(...) 可能折行）
            j = i
            while lines[j].strip().startswith("@") or (
                lines[j].strip() and not lines[j].rstrip().endswith(")")
            ):
                if lines[j].rstrip().endswith("):") or " def " in lines[j + 1]:
                    break
                j += 1
                if j - i > 6:
                    break
            # 向下找第一个非空行
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            nxt = lines[k].strip() if k < len(lines) else ""
            if not nxt.startswith(("async def", "def")):
                problems.append(f"L{i + 1}: {stripped[:60]} -> 下一行 {nxt[:40]!r}")
        assert not problems, (
            "存在装饰器与函数定义分离（错位）的路由：\n" + "\n".join(problems)
        )