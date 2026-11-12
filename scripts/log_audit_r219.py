# -*- coding: utf-8 -*-
"""R219 后台日志只读聚类与功能轨迹分析工具。

用法: python scripts/log_audit_r219.py [日志路径]

输出: 级别/时间跨度 -> logger 分布 -> WARNING+ERROR 模板聚类(数字与ID归一化)
      -> trace/task/pid 串联 -> 耗时与 token 抽样 -> 测试注入噪声标记。
纯只读，不改动任何日志或业务数据。
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys

LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+"
    r"\[(INFO|WARNING|ERROR|DEBUG|CRITICAL)\]\s+([^:]+):\s*(.*)$"
)
HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;]+")
QUOTE_RE = re.compile(r"'[^']{1,80}'|\"[^\"]{1,80}\"")
TRACE_RES = {
    "trace": re.compile(r"(?:trace(?:_id)?|rid)[=:]\s*([0-9a-zA-Z_-]{6,32})"),
    "task": re.compile(r"\btask[=:]\s*([0-9a-zA-Z_-]{6,48})"),
    "pid": re.compile(r"\bpid[=:]\s*(\d{2,10})"),
    "project": re.compile(r"\bproject[=:]\s*(\d{2,12})"),
    "scheme": re.compile(r"\bscheme[=:]\s*([0-9a-zA-Z_-]{1,32})"),
}
NOISE_MARKERS = (
    "MagicMock", "monkeypatch", "inflight_pool_repro", "shutdown_fate",
    "pytest", "Temp\\", "AppData\\Local\\Temp", "simulated", "disk full",
    "NO_SUCH_KEY", "test_", "fake_", "dummy",
)
TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|秒|毫秒)(?![A-Za-z])")
TOKEN_RE = re.compile(r"(?:tokens?|max_tokens|prompt_tokens|completion_tokens)\s*[=:约]?\s*(\d+)")


def normalize(text: str) -> str:
    """把变量归一化成模板，用于同类日志聚合。"""
    s = PATH_RE.sub("<PATH>", text)
    s = HEX_RE.sub("<ID>", s)
    s = QUOTE_RE.sub("<Q>", s)
    s = NUM_RE.sub("<N>", s)
    return re.sub(r"\s+", " ", s).strip()[:220]


def is_noise(text: str) -> bool:
    return any(m in text for m in NOISE_MARKERS)


def is_stream_request(msg: str) -> bool:
    """判定访问日志是否属于 SSE 流式长连接（用于耗时基线分离）。

    只认三类：R219 之后的显式 ``kind=stream`` 标记、系统活动 SSE 常连接、
    目录/正文/事实生成 SSE 流。⚠️ 绝不能按 ``"/sse/"`` 粗判 ——
    ``/api/v1/sse/tasks`` 是每 3s 的普通轮询（实测 21~78ms），粗判会把
    数百条轮询误算成长连接；反之 ``review/autofix/stage``、``export/docx``
    等真实慢请求没被排除，必须留在 P95/P99 里。
    """
    return "kind=stream" in msg or "activity/stream" in msg or "/sse/generate-" in msg


def main() -> None:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "logs/backend.log")
    # 可选第二参数：报告由 Python 直接以 UTF-8 落盘，规避 Windows PowerShell 5.1
    # 管道按 cp936 解码 Python stdout 造成的中文乱码（本轮实测）。
    if len(sys.argv) > 2:
        sys.stdout = open(sys.argv[2], "w", encoding="utf-8", newline="\n")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    records = []
    unparsed = 0
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            unparsed += 1
            continue
        ts, level, logger, msg = m.groups()
        records.append((ts, level, logger.strip(), msg.strip(), line))

    print(f"=== 文件 {path} · 总行 {len(lines)} · 解析 {len(records)} · 续行/未解析 {unparsed}")
    print(f"=== 时间跨度 {records[0][0]} -> {records[-1][0]}")
    print("=== 级别", dict(collections.Counter(r[1] for r in records)))
    print("=== 按日", dict(sorted(collections.Counter(r[0][:10] for r in records).items())))

    print("\n=== logger TOP 25")
    for name, n in collections.Counter(r[2] for r in records).most_common(25):
        print(f"{n:6d}  {name}")

    for level in ("ERROR", "WARNING"):
        subset = [r for r in records if r[1] == level]
        groups: dict[str, list] = collections.defaultdict(list)
        for r in subset:
            groups[normalize(r[3])].append(r)
        print(f"\n=== {level} {len(subset)} 条 / 聚类 {len(groups)} 类")
        for tpl, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            first, last = items[0][0], items[-1][0]
            noise = sum(1 for r in items if is_noise(r[4]))
            print(f"[{len(items):3d}] {first} ~ {last} noise={noise} | {tpl}")
            for r in items[:2]:
                print(f"        · {r[2]}: {r[3][:260]}")

    print("\n=== Trace / task / pid 串联频次 TOP 20")
    trace_vals: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in records:
        for kind, regex in TRACE_RES.items():
            for v in regex.findall(r[3]):
                trace_vals[kind][v] += 1
    for kind, counter in trace_vals.items():
        print(f"-- {kind}: {len(counter)} 个不同值, 命中 {sum(counter.values())} 次")
        for v, n in counter.most_common(8):
            print(f"   {n:5d}  {v}")

    print("\n=== 耗时抽样（含单位的数字）")
    durations = []
    for r in records:
        for num, unit in TIME_RE.findall(r[3]):
            try:
                val = float(num)
            except ValueError:
                continue
            durations.append((val if unit in ("ms", "毫秒") else val * 1000, r[0], r[2], r[3][:120]))
    vals = sorted(d[0] for d in durations)
    if vals:
        def pct(p: float) -> float:
            return vals[min(len(vals) - 1, int(len(vals) * p))]
        print(f"样本 {len(vals)} · P50={pct(.5):.0f}ms P95={pct(.95):.0f}ms P99={pct(.99):.0f}ms max={vals[-1]:.0f}ms")
        for val, ts, logger, msg in sorted(durations, reverse=True)[:12]:
            print(f"   {val:10.0f}ms  {ts}  {logger}: {msg}")

    print("\n=== 请求耗时基线（流式长连接与普通请求分离）")
    http_costs: list[tuple[int, int, bool]] = []
    for r in records:
        if r[2] != "http" or "->" not in r[3]:
            continue
        m = re.search(r"->\s*(\d{3})\s*\((\d+)ms\)", r[3])
        if not m:
            continue
        is_sse = is_stream_request(r[3])
        http_costs.append((int(m.group(1)), int(m.group(2)), is_sse))
    ordinary = sorted(c for _, c, s in http_costs if not s)
    streams = [x for x in http_costs if x[2]]
    def pctl(vals: list[int], p: float) -> int:
        return vals[min(len(vals) - 1, int(len(vals) * p))] if vals else 0
    ok = sum(1 for st, _, _ in http_costs if 200 <= st < 400)
    bad = sum(1 for st, _, _ in http_costs if st >= 400)
    print(
        f"请求 {len(http_costs)} 条 · 普通 {len(ordinary)} · 流式长连接 {len(streams)}"
        f" · 成功率 {ok}/{len(http_costs)} ({ok / len(http_costs) * 100:.2f}%) · 4xx/5xx {bad}"
    )
    print(
        f"普通请求 P50={pctl(ordinary, .5)}ms P95={pctl(ordinary, .95)}ms"
        f" P99={pctl(ordinary, .99)}ms max={ordinary[-1] if ordinary else 0}ms"
    )
    if streams:
        sc = sorted(c for _, c, _ in streams)
        print(f"流式连接时长 P50={pctl(sc, .5)}ms P95={pctl(sc, .95)}ms max={sc[-1]}ms（不计入请求耗时基线）")
    retry_kw = ("重试", "放大至", "降级链收敛", "补偿重跑", "自动切换分步链路")
    retries = [r for r in records if any(k in r[3] for k in retry_kw)]
    print(f"重试/降级信号 {len(retries)} 条")

    print("\n=== Token 抽样")
    tok = [(int(v), r[0], r[2], r[3][:140]) for r in records for v in TOKEN_RE.findall(r[3])]
    print(f"样本 {len(tok)} · 合计 {sum(t[0] for t in tok)}")
    for v, ts, logger, msg in sorted(tok, reverse=True)[:10]:
        print(f"   {v:8d}  {ts}  {logger}: {msg}")

    print("\n=== ERROR/堆栈续行上下文")
    for i, r in enumerate(records):
        if r[1] in ("ERROR", "CRITICAL"):
            print(f"-- {r[0]} [{r[1]}] {r[2]}: {r[3][:400]}")


if __name__ == "__main__":
    main()
