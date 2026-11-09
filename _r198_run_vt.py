# -*- coding: utf-8 -*-
"""R198: run frontend vitest files synchronously (UTF-8 safe wrapper)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"j:\编程\专项方案工具箱\frontend")
cmd = [
    str(ROOT / "node_modules" / ".bin" / "vitest.CMD"),
    "run",
    *sys.argv[1:],
]
r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True)
out = (r.stdout or b"").decode("utf-8", errors="replace")
err = (r.stderr or b"").decode("utf-8", errors="replace")
report = Path(r"j:\编程\专项方案工具箱\_r198_vt_out.txt")
report.write_text(out + "\n--- STDERR ---\n" + err, encoding="utf-8")
tail = [ln for ln in out.splitlines() if ("Test Files" in ln or "Tests " in ln or "×" in ln or "FAIL" in ln)]
print("\n".join(tail[-8:]))
print("EXIT =", r.returncode)
