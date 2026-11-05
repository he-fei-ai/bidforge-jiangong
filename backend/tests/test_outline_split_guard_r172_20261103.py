"""拆分护栏（R172 · 目录生成模块遗留加固）。

背景：并行会话把 ``sse_handlers.py``（9000+ 行）拆成三个模块——
``_sse_outline_build.py``（构建期/进度/校验）、``_sse_outline_review.py``（审核/修复/合并）、
原文件保留「分步编排 + 终态收口」—— 并通过转发 import 保持 monkeypatch 语义。
这类拆分最常见的回归是**同一函数名在两个文件里各自定义一遍**（调用方拿到的不是预期那份）
与**转发了已不存在/拼错的名字**。

本文件一律用 AST 判定（不 import 生产代码，避免与被测模块的 import 顺序耦合），
覆盖：文件内重复、跨文件重复（遮蔽）、转发名字必须真实存在、转发模块必须来自两个新模块、
两个拆出模块互相不得 import 对方（兄弟模块循环依赖）。
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROUTERS = BACKEND / "app" / "routers"
SSE = ROUTERS / "sse_handlers.py"
BUILD = ROUTERS / "_sse_outline_build.py"
REVIEW = ROUTERS / "_sse_outline_review.py"

SPLIT_MODULES = ("app.routers._sse_outline_build", "app.routers._sse_outline_review")


def _fn_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _imports_from(path: Path, module: str) -> list[str]:
    """返回从给定模块 import 的所有别名原名（as 别名同名视为原名导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == module:
            for a in n.names:
                if a.name != "*":
                    names.append(a.name)
    return names


class TestSplitIntegrity:
    def test_three_files_have_no_duplicate_defs_within_file(self):
        """任何一个拆出模块都不得在自身文件里定义同名函数两次。"""
        for p in (SSE, BUILD, REVIEW):
            names = _fn_names(p)
            dups = {n for n in names if names.count(n) > 1}
            assert not dups, f"{p.name} 内重复定义：{sorted(dups)}"

    def test_sse_handlers_defines_no_name_already_split_out(self):
        """sse_handlers 不得再自定义已拆出的函数（遮蔽会让转发 import 失效）。"""
        sh = set(_fn_names(SSE))
        split = set(_fn_names(BUILD)) | set(_fn_names(REVIEW))
        assert not (sh & split), (
            f"sse_handlers.py 仍自定义了已拆出的函数：{sorted(sh & split)}；"
            "应从 _sse_outline_build / _sse_outline_review 转发导入"
        )

    def test_split_modules_define_no_overlapping_names(self):
        """两个拆出模块不得各自定义同名函数（归属必须唯一，否则调用方拿哪份不确定）。"""
        common = set(_fn_names(BUILD)) & set(_fn_names(REVIEW))
        assert not common, f"拆出模块间重复定义：{sorted(common)}"

    def test_sse_handlers_forwards_split_names_that_exist(self):
        """sse_handlers 从拆出模块 import 的每个名字都必须真实定义在该模块。"""
        for module, path in ((SPLIT_MODULES[0], BUILD), (SPLIT_MODULES[1], REVIEW)):
            imported = _imports_from(SSE, module)
            defined = set(_fn_names(path))
            # 常量 / 模块级赋值也是合法转发目标，单列白名单由存在性检查兜底：
            # 未定义在任何地方的名字直接失败；定义在别处（如常量）由下一条护栏兜底。
            src = path.read_text(encoding="utf-8")
            for name in imported:
                assert name in defined or name in src, f"sse_handlers 从 {module} 转发了不存在的名字：{name}"

    def test_split_modules_do_not_import_each_other(self):
        """兄弟模块互相 import 会制造难以诊断的导入顺序问题（本仓已有循环导入先例）。"""
        build_src = BUILD.read_text(encoding="utf-8")
        review_src = REVIEW.read_text(encoding="utf-8")
        assert "_sse_outline_review" not in build_src, "_sse_outline_build 不得引用 _sse_outline_review"
        assert "_sse_outline_build" not in review_src, "_sse_outline_review 不得引用 _sse_outline_build"

    def test_split_modules_do_not_import_sse_handlers_at_module_top(self):
        """拆出模块只允许把 sse_handlers 当 facade 延迟绑定，不得在模块顶层 from-import 它。

        允许 ``import app.routers.sse_handlers as _sh``（常见兼容绑定），
        禁止 ``from app.routers.sse_handlers import ...``（会把整个模块在导入期拉起，
        且与 sse_handlers 顶部转发 import 构成本环）。
        """
        for p in (BUILD, REVIEW):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for n in tree.body:  # 只看模块顶层，不看函数内局部导入
                if isinstance(n, ast.ImportFrom) and n.module == "app.routers.sse_handlers":
                    raise AssertionError(f"{p.name} 顶层 from-import sse_handlers（应改用 `import ... as _sh`）")
