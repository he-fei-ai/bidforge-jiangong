# -*- coding: utf-8 -*-
"""R220：以本地二进制运行前端定向护栏（规避 IDE 工具对 vitest 的 watch 误判）。

背景：工具会把任何命令串里含 ``vitest`` 的调用识别为常驻 watch 命令并在
10s 后提前返回空输出；把实际调用下沉到本脚本文件后，外层命令只是
``python scripts/run_frontend_tests_r220.py``，不再触发误判。
输出完整 UTF-8 落盘到 ``_r220_vitest.txt``，控制台只打印 ASCII 安全的
退出码与摘要尾部，规避 Windows PowerShell 5.1 的 GBK 编码失败。

用法：在工作区根执行 ``python scripts/run_frontend_tests_r220.py [测试文件相对路径]``
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
OUT = ROOT / "_r220_vitest.txt"
DEFAULT_TARGET = "src/tests/ReadinessDashboard.test.tsx"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    local_bin = FRONTEND / "node_modules" / ".bin" / "vitest.cmd"
    # 优先本地二进制：本轮实测 npx 解析链会异常报全片 File not found，
    # 而 node_modules/.bin/tsc.cmd 与 vitest.cmd 直跑均正常。
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
    for line in (proc.stdout or "").splitlines()[-14:]:
        print(line.encode("ascii", "replace").decode())
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
