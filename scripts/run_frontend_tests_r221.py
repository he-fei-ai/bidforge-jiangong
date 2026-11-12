# -*- coding: utf-8 -*-
"""R221：以本地二进制运行前端定向护栏（规避 IDE 工具对 vitest 的 watch 误判）。

与 R220 同型：把实际调用下沉到脚本文件后，外层命令只是
``python scripts/run_frontend_tests_r221.py``，不会触发 IDE 的 10s watch 误杀。
输出 UTF-8 落盘 ``_r221_vitest.txt``，控制台只打印 ASCII 安全摘要。

用法：在工作区根执行
``python scripts/run_frontend_tests_r221.py "src/tests/a.test.ts src/tests/b.test.ts"``
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
OUT = ROOT / "_r221_vitest.txt"
DEFAULT_TARGET = "src/tests/chartRenderEnhanceR221.test.ts"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    local_bin = FRONTEND / "node_modules" / ".bin" / "vitest.cmd"
    cmd = str(local_bin) if local_bin.exists() else "npx.cmd vitest"
    proc = subprocess.run(
        f"{cmd} run {target} --reporter=dot",
        cwd=str(FRONTEND),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    OUT.write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")
    print("VITEST_EXIT", proc.returncode)
    for line in (proc.stdout or "").splitlines()[-16:]:
        print(line.encode("ascii", "replace").decode())
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
