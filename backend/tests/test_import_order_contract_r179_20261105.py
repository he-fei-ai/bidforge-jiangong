"""R179 · 导入顺序运行时契约守卫（2026-11-05）

背景：`tests/test_content_dedupe_progress_r175_20261103.py` 与
`tests/test_context_degraded_d39_r176_20261104.py` 的 import 顺序是**运行时契约**：
必须先导入 `app.routers.sse_handlers` 完成 facade 初始化，再导入
`app.routers._sse_outline_build` / `_sse_outline_review`；顺序反向会触发
「partially initialized module」循环导入失败。此前该契约只靠 `# noqa: I001`
注释锁着，没有任何测试。本文件把它变成可执行护栏：

1. 契约顺序在**全新子进程**中导入必须成功（避免受本进程已导入模块的影响）；
2. 打乱顺序（先导入 `_sse_outline_build`）必须失败，且失败原因必须是循环导入；
3. 两个源文件的顶层 import 顺序不得被格式化/重排工具改变。
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"

CONTRACT_FILES = (
    TESTS / "test_content_dedupe_progress_r175_20261103.py",
    TESTS / "test_context_degraded_d39_r176_20261104.py",
)


def _run_in_fresh_interpreter(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env.pop("PYTHONSTARTUP", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestImportOrderContract:
    def test_contract_order_imports_succeed(self):
        """契约顺序：sse_handlers → _sse_outline_build → _sse_outline_review。"""
        proc = _run_in_fresh_interpreter(
            "import app.routers.sse_handlers\n"
            "from app.routers import _sse_outline_build\n"
            "from app.routers import _sse_outline_review\n"
            "print('CONTRACT_OK')"
        )
        assert proc.returncode == 0, proc.stderr
        assert "CONTRACT_OK" in proc.stdout

    def test_build_first_order_fails(self):
        """顺序打乱（先导入 _sse_outline_build）必须失败，且是循环导入。"""
        proc = _run_in_fresh_interpreter("import app.routers._sse_outline_build")
        assert proc.returncode != 0, "先导入 _sse_outline_build 竟然成功，契约前提已变"
        err = proc.stderr + proc.stdout
        assert "partially initialized" in err or "circular import" in err, err

    @pytest.mark.parametrize("path", CONTRACT_FILES, ids=lambda p: p.name)
    def test_source_keeps_contract_import_order(self, path: Path):
        """源码顶层 import 顺序：sse_handlers 必须早于 _sse_outline_build/review。"""
        raw = path.read_text(encoding="utf-8")
        assert "noqa: I001" in raw, f"{path.name} 的 noqa: I001 契约注释被删除"

        tree = ast.parse(raw)
        modules: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "app.routers":
                    modules.extend(f"app.routers.{alias.name}" for alias in node.names)
                else:
                    modules.append(node.module)

        def _idx(target: str) -> int:
            return modules.index(target) if target in modules else -1

        handlers = _idx("app.routers.sse_handlers")
        build = _idx("app.routers._sse_outline_build")
        review = _idx("app.routers._sse_outline_review")
        assert handlers >= 0, f"{path.name} 未导入 sse_handlers"
        assert build >= 0, f"{path.name} 未导入 _sse_outline_build"
        assert handlers < build, f"{path.name} 中 _sse_outline_build 早于 sse_handlers"
        if review >= 0:
            assert handlers < review, f"{path.name} 中 _sse_outline_review 早于 sse_handlers"
