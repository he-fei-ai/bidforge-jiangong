# -*- coding: utf-8 -*-
"""R222：按索引区间复跑图表/导出簇的某一批（规避 PowerShell 命令行超长）。

用法：``python scripts/run_r222_batch.py 50 75``
输出 UTF-8 落盘 ``_r222_batch.txt``，摘要逐行打印。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
KEYWORDS = ("chart", "export", "preflight", "readiness")
OUT = ROOT / "_r222_batch.txt"


def main() -> int:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    files = sorted(
        p.name for p in (BACKEND / "tests").glob("test_*.py")
        if any(k in p.name.lower() for k in KEYWORDS)
    )[start:end]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *[f"tests/{n}" for n in files], "-q", "--tb=short"],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    OUT.write_text(text, encoding="utf-8")
    print("FILES", len(files), "EXIT", proc.returncode)
    for line in text.splitlines():
        if " passed" in line or " failed" in line or line.startswith("FAILED ") or line.startswith("ERROR "):
            print(line.encode("ascii", "replace").decode())
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
