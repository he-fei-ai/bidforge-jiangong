"""测试分类工具（只读）：识别含“源码文本断言”的测试文件。

用途（仓库根目录执行）：
    python scripts/classify_tests.py
输出：
    scripts/source_lock_report.csv   —— 每个测试文件的命中数与首行位置
    stdout                           —— 测试总数 / strict / broad 占比
"""

from __future__ import annotations

import ast
import csv
import sys
from pathlib import Path

SOURCE_READ_NAMES = {"read_text", "read_bytes", "getsource", "getsourcelines"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return getattr(func, "attr", None) or getattr(func, "id", None)


def _read_hits(tree: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in SOURCE_READ_NAMES or name == "read":
                hits.append((node.lineno, name))
    return hits


def _looks_like_source_lock(tree: ast.AST) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """返回 (strict, broad)。

    strict：读取文件且出现 .py 字面量（强信号）；
    broad：任意读取调用（宽口径，用于跟踪历史存量）。
    """
    reads = _read_hits(tree)
    has_py_literal = any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".py")
        for node in ast.walk(tree)
    )
    strict = [hit for hit in reads if has_py_literal]
    return strict, reads


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tests_dir = root / "backend" / "tests"
    files = sorted(tests_dir.glob("test_*.py"))
    rows: list[tuple[str, int, int, int]] = []
    strict_count = 0
    broad_count = 0
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        strict, broad = _looks_like_source_lock(tree)
        if strict:
            strict_count += 1
        if broad:
            broad_count += 1
        if broad:
            rows.append((path.name, len(strict), len(broad), min(line for line, _ in broad)))

    report = Path(__file__).resolve().parent / "source_lock_report.csv"
    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("file", "strict_hits", "broad_hits", "first_line"))
        writer.writerows(rows)

    total = len(files)
    strict_ratio = strict_count / total if total else 0.0
    broad_ratio = broad_count / total if total else 0.0
    print(f"tests={total} strict={strict_count} ({strict_ratio:.1%}) broad={broad_count} ({broad_ratio:.1%})")
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
