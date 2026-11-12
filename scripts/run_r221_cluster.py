# -*- coding: utf-8 -*-
"""R221：图表 / 导出 / 预检簇按文件名分块同步跑（规避 -k 全量收集的资源故障）。

AGENTS 既有教训：全量 pytest 不能后台跑，`-k` 收集 266 文件会触发宿主机
"The device is not ready" 与日志截断；按文件名分块（每块 <= 25 文件）同步跑
最稳。输出 UTF-8 落盘 ``_r221_cluster.txt``，控制台打印逐块摘要与总退出码。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TESTS = BACKEND / "tests"
OUT = ROOT / "_r221_cluster.txt"
KEYWORDS = ("chart", "export", "preflight", "readiness")
BATCH = 25


def main() -> int:
    files = sorted(
        p.name for p in TESTS.glob("test_*.py") if any(k in p.name.lower() for k in KEYWORDS)
    )
    logs: list[str] = [f"FILES {len(files)}"]
    failed_batches = 0
    for i in range(0, len(files), BATCH):
        batch = files[i : i + BATCH]
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *[f"tests/{name}" for name in batch], "-q", "--tb=short"],
            cwd=str(BACKEND),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        logs.append(f"\n===== BATCH {i // BATCH + 1} ({len(batch)} files) EXIT {proc.returncode} =====")
        logs.append(text)
        summary = [ln for ln in text.splitlines() if "passed" in ln or "failed" in ln or "error" in ln.lower()]
        print(f"BATCH {i // BATCH + 1} files={len(batch)} EXIT={proc.returncode}")
        for ln in summary[-3:]:
            print(ln.encode("ascii", "replace").decode())
        if proc.returncode != 0:
            failed_batches += 1
    OUT.write_text("\n".join(logs), encoding="utf-8")
    print("TOTAL_FILES", len(files), "FAILED_BATCHES", failed_batches)
    return 1 if failed_batches else 0


if __name__ == "__main__":
    raise SystemExit(main())
