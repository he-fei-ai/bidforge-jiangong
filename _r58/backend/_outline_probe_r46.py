# -*- coding: utf-8 -*-
"""R46 结构勘查探针：输出两个模块的函数/类清单（只读）。"""
import ast
import sys

FILES = [
    "app/routers/review.py",
    "app/routers/review_autofix.py",
    "app/routers/compliance.py",
    "app/services/audit_rules.py",
    "app/services/preflight_engine.py",
    "app/services/audit_scoring.py",
    "app/services/audit_service.py",
    "app/services/consistency_scanner.py",
    "app/services/conflict_arbiter.py",
    "app/services/review_autofix.py",
    "app/services/content_blocks.py",
    "app/routers/export.py",
    "app/routers/_chart_pipeline.py",
    "app/services/chart_payload.py",
]


def main() -> int:
    only = sys.argv[1:] if len(sys.argv) > 1 else FILES
    for f in only:
        p = "backend/" + f if not f.startswith("backend/") else f
        src = open(p, encoding="utf-8").read()
        try:
            t = ast.parse(src)
        except SyntaxError as e:
            print(f"[SYNTAX ERR] {f}: {e}")
            continue
        print("=" * 24, f, len(src.splitlines()), "lines")
        for n in t.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                print(f"  def {n.name} L{n.lineno}")
            elif isinstance(n, ast.ClassDef):
                print(f"  class {n.name} L{n.lineno}")
                for b in n.body:
                    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        print(f"      - {b.name} L{b.lineno}")
            elif isinstance(n, ast.Assign):
                for tg in n.targets:
                    if isinstance(tg, ast.Name):
                        print(f"  CONST {tg.id} L{n.lineno}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
