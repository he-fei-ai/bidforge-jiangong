# -*- coding: utf-8 -*-
"""R222：后台日志结构化采集（图表生成 / 渲染 / 导出降级方向）。

用法：``python scripts/log_audit_r222.py``
输出：控制台摘要 + ``_r222_log_audit.txt``（UTF-8）。

聚类口径沿用 R219：数字 / trace / pid / 章节短 id 归一化后按模板计数，
并显式标注测试噪声特征（MagicMock / monkeypatch / fixture / 模拟崩溃 等）。
"""
from __future__ import annotations

import collections
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "backend.log"
OUT = ROOT / "_r222_log_audit.txt"

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(\w+)\] ([^:]+): (.*)$")
CHART_RE = re.compile(
    r"chart|图表|mermaid|渲染|PIL|gantt|甘特|配图|render_stats|Render-Stats|降级|degraded|借图",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"MagicMock|monkeypatch|fixture|pytest|NO_SUCH_KEY|simulate|模拟|disk full|999|test_",
    re.IGNORECASE,
)


def normalize(msg: str) -> str:
    msg = re.sub(r"trace=[0-9a-f]+", "trace=<T>", msg)
    msg = re.sub(r"pid=\d+", "pid=<N>", msg)
    msg = re.sub(r"\b[0-9a-f]{8,}\b", "<HEX>", msg)
    msg = re.sub(r"\d+(?:\.\d+)?", "<N>", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:180]


def main() -> int:
    raw = LOG.read_bytes().decode("utf-8", errors="replace")
    lines = raw.splitlines()
    levels: collections.Counter[str] = collections.Counter()
    chart_lines: list[tuple[str, str, str, str]] = []
    warn_tpl: collections.Counter[str] = collections.Counter()
    err_tpl: collections.Counter[str] = collections.Counter()
    chart_tpl: collections.Counter[str] = collections.Counter()
    noise = 0
    parsed = 0
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        parsed += 1
        ts, level, module, msg = m.groups()
        levels[level] += 1
        if NOISE_RE.search(msg):
            noise += 1
        if CHART_RE.search(msg):
            chart_lines.append((ts, level, module, msg))
            chart_tpl[(level, normalize(msg))] += 1
        if level in ("WARNING", "ERROR"):
            (warn_tpl if level == "WARNING" else err_tpl)[normalize(msg)] += 1

    out: list[str] = [
        f"LOG {LOG} lines={len(lines)} parsed={parsed}",
        f"SPAN {lines[0][:19]} ~ {lines[-1][:19]}",
        f"LEVELS {dict(levels)}",
        f"CHART_LINES {len(chart_lines)}  NOISE_MARKERS {noise}",
        "",
        "=== CHART-RELATED WARNING/ERROR TEMPLATES (top 40) ===",
    ]
    for (level, tpl), n in chart_tpl.most_common(40):
        if level in ("WARNING", "ERROR"):
            tag = "NOISE?" if NOISE_RE.search(tpl) else ""
            out.append(f"{n:5d} [{level}] {tag} {tpl}")
    out += ["", "=== ALL WARNING TEMPLATES (top 30) ==="]
    for tpl, n in warn_tpl.most_common(30):
        out.append(f"{n:5d} {tpl}")
    out += ["", "=== ALL ERROR TEMPLATES (top 30) ==="]
    for tpl, n in err_tpl.most_common(30):
        out.append(f"{n:5d} {tpl}")
    out += ["", "=== CHART-RELATED RAW LINES (last 80) ==="]
    for ts, level, module, msg in chart_lines[-80:]:
        out.append(f"{ts} [{level}] {module}: {msg[:300]}")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print("LOG_LINES", len(lines), "PARSED", parsed, "CHART_LINES", len(chart_lines))
    print("LEVELS", dict(levels))
    print("\n".join(out[4:44]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
