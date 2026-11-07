"""R176 端到端分段基准（默认拒绝与并发会话抢资源）。

设计原则
--------
1. **不启动服务、不建库、不写生产 DB**：脚本只对已运行的 ``--base-url`` 发请求；
   数据集只读 ``dataset/_generated/``。
2. **并发守卫（硬要求）**：检测到 pytest / uvicorn / vitest / managed 进程，
   或工作树有未提交改动时，默认拒绝启动（退出码 2），必须 ``--force`` 才可绕过。
   这是为了排除上一轮"并行写盘导致幻影失败"的干扰。
3. ``--dry-run`` 不连 DB、不起服务、不发请求，只打印将执行什么（仍会做只读守卫检查）。

覆盖报告 §9 的 10 个测量点（V1–V10）
------------------------------------
V1 导出 DOCX 冷缓存        V2 导出 DOCX 热缓存
V3 AI 并发退档（需 429 注入）V4 DB 池探活影响（需环境开关）
V5 上传期间事件循环尖刺      V6 正文生成吞吐（SSE）
V7 事实抽取吞吐（SSE）      V8 审核/一致性扫描吞吐
V9 前端首屏/操作延迟（浏览器 trace） V10 目录生成未保存 + checkpoint 恢复

能全自动跑的：V1/V2/V6/V7/V8/V10；V3/V4/V5/V9 需要人工前置条件，
脚本会打印 SKIP 原因与操作步骤，不伪造数字。

用法
----
    python backend/tools/perf/bench_e2e.py --dry-run
    python backend/tools/perf/bench_e2e.py --dry-run --tier M
    # 安静窗口（工作树干净、无并发 pytest/uvicorn）：
    python backend/tools/perf/bench_e2e.py --base-url http://127.0.0.1:8000 \
        --scheme-id <id> --tier M --cases V1,V2,V6 --repeat 5

指标口径（详见 README.md）
--------------------------
P50/P95/P99 为单向延迟（含服务端处理与传输）；吞吐 = 完成量 / 墙钟秒。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_THIS = Path(__file__).resolve()
PERF_ROOT = _THIS.parent
GEN_ROOT = PERF_ROOT / "dataset" / "_generated"

# 与并发会话冲突的进程命令行特征（小写匹配）
CONFLICT_PATTERNS = ("pytest", "uvicorn", "app.main:app", "vitest", "managed",
                    "run_perf", "bench_e2e.py --base-url")


# ---------------------------------------------------------------------------
# 只读守卫
# ---------------------------------------------------------------------------
def _ps_windows() -> list[dict]:
    ps = ("Get-CimInstance Win32_Process -Filter "
          "\"Name='python.exe' or Name='node.exe' or Name='java.exe'\" | "
          "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=25)
    raw = (out.stdout or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [{"pid": int(d.get("ProcessId") or 0),
             "name": d.get("Name") or "",
             "cmd": d.get("CommandLine") or ""} for d in data]


def _ps_posix() -> list[dict]:
    procs: list[dict] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return procs
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmd = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "ignore").strip()
        except OSError:
            continue
        if cmd:
            procs.append({"pid": int(pid_dir.name), "name": "", "cmd": cmd})
    return procs


def detect_conflicts() -> list[dict]:
    try:
        procs = _ps_windows() if os.name == "nt" else _ps_posix()
    except Exception as exc:  # 守卫自身失败时按"无法确认"处理
        return [{"pid": 0, "name": "guard-error", "cmd": str(exc)}]
    hits: list[dict] = []
    own = os.getpid()
    for p in procs:
        if p["pid"] == own:
            continue
        cmd = p["cmd"].lower()
        if any(pat in cmd for pat in CONFLICT_PATTERNS):
            hits.append({**p, "cmd": p["cmd"][:180]})
    return hits


def git_dirty(root: Path) -> tuple[bool, int]:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                             capture_output=True, text=True, timeout=20)
        lines = [x for x in (out.stdout or "").splitlines() if x.strip()]
        return bool(lines), len(lines)
    except Exception:
        return True, -1


# ---------------------------------------------------------------------------
# 用例定义
# ---------------------------------------------------------------------------
CASES = [
    {"id": "V1", "name": "导出 DOCX（冷缓存）", "metric": "ms/次（冷）",
     "auto": True, "needs": "scheme_id；建议先清缓存"},
    {"id": "V2", "name": "导出 DOCX（热缓存）", "metric": "ms/次（热）",
     "auto": True, "needs": "scheme_id；V1 之后立即执行"},
    {"id": "V3", "name": "AI 并发退档（429/超时注入）", "metric": "退档次数/恢复秒",
     "auto": False, "needs": "mock provider 或代理注入 429；观察 SSE 退档事件"},
    {"id": "V4", "name": "DB 池探活影响", "metric": "导出/读接口 P95 差值",
     "auto": False, "needs": "同一工作树切换 DB 探活开关后各跑一轮 V1/V2"},
    {"id": "V5", "name": "上传期间事件循环尖刺", "metric": "/health P99（并发上传时）",
     "auto": False, "needs": "上传接口 URL 与样本文档；默认用 global-facts/upload-documents"},
    {"id": "V6", "name": "正文生成吞吐（SSE）", "metric": "sections/s、chars/s",
     "auto": True, "needs": "已保存目录且有可生成章节的 scheme_id"},
    {"id": "V7", "name": "事实抽取吞吐（SSE）", "metric": "段/s",
     "auto": True, "needs": "已上传资料的 scheme_id"},
    {"id": "V8", "name": "审核/一致性扫描吞吐", "metric": "ms/次、规则/s（近似）",
     "auto": True, "needs": "scheme_id"},
    {"id": "V9", "name": "前端首屏/操作延迟", "metric": "LCP / INP",
     "auto": False, "needs": "Chrome DevTools Performance trace（人工）"},
    {"id": "V10", "name": "目录生成未保存 + checkpoint", "metric": "section 事件/s",
     "auto": True, "needs": "scheme_id；不调用 save-outline"},
]


# ---------------------------------------------------------------------------
# 统计与 HTTP 工具
# ---------------------------------------------------------------------------
def pctl(samples: list[float], q: float) -> float:
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(samples: list[float]) -> dict:
    if not samples:
        return {}
    return {
        "n": len(samples),
        "p50_ms": round(pctl(samples, 0.50), 2),
        "p95_ms": round(pctl(samples, 0.95), 2),
        "p99_ms": round(pctl(samples, 0.99), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "mean_ms": round(statistics.fmean(samples), 2),
    }


def _client(base_url: str, timeout: float):
    import httpx
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)


def run_export(client, scheme_id: str, repeat: int) -> list[float]:
    samples: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        r = client.post(f"/api/v1/schemes/{scheme_id}/export/docx", json={})
        r.raise_for_status()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def run_sse(path: str, payload: dict, client, timeout: float) -> dict:
    """消费一次 SSE 任务，返回墙钟、事件数、首事件延迟与各事件名计数。"""
    counts: dict[str, int] = {}
    first_event_s: float | None = None
    t0 = time.perf_counter()
    with client.stream("POST", path, json=payload) as resp:
        resp.raise_for_status()
        event = ""
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                key = event or "message"
                counts[key] = counts.get(key, 0) + 1
                if first_event_s is None:
                    first_event_s = time.perf_counter() - t0
    return {"wall_s": round(time.perf_counter() - t0, 3),
            "first_event_s": round(first_event_s or 0.0, 3),
            "events": sum(counts.values()),
            "by_event": counts}


def run_consistency(client, scheme_id: str) -> float:
    t0 = time.perf_counter()
    r = client.post(f"/api/v1/compliance/consistency-audit/{scheme_id}", json={})
    r.raise_for_status()
    return (time.perf_counter() - t0) * 1000.0


def run_compliance_check(client) -> float:
    t0 = time.perf_counter()
    r = client.post("/api/v1/compliance/check", json={})
    r.raise_for_status()
    return (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def print_plan(tier: str, cases: list[dict], repeat: int) -> None:
    print(f"数据集档位：{tier}  （S/M/L = 30/120/300 节，生成器见 dataset/）")
    print(f"重复：{repeat} 次；指标：P50/P95/P99 + 吞吐")
    header = f"{'ID':<5}{'用例':<32}{'指标':<28}{'自动':<6}"
    print(header)
    print("-" * len(header))
    for c in cases:
        print(f"{c['id']:<5}{c['name']:<32}{c['metric']:<28}"
              f"{'是' if c['auto'] else '否':<6}")
        if c["needs"]:
            print(f"      前置：{c['needs']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="R176 端到端分段基准")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印计划，不发请求、不连 DB")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--scheme-id", default="")
    ap.add_argument("--tier", default="M", choices=["S", "M", "L"])
    ap.add_argument("--cases", default="all",
                    help="逗号分隔用例 ID，默认 all")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--force", action="store_true",
                    help="绕过并发进程/脏工作树守卫（危险，仅限自觉隔离的窗口）")
    ap.add_argument("--upload-url", default="",
                    help="V5 用的上传接口（完整路径），留空则 V5 SKIP")
    ap.add_argument("--out", type=Path, default=None,
                    help="结果 JSON 路径（默认 dataset/_generated/results/）")
    args = ap.parse_args()

    cases = CASES if args.cases == "all" else [
        c for c in CASES if c["id"] in {x.strip() for x in args.cases.split(",")}]

    conflicts = detect_conflicts()
    dirty, dirty_n = git_dirty(PERF_ROOT.parents[2])

    print("== 并发守卫 ==")
    if conflicts:
        print(f"检测到 {len(conflicts)} 个冲突进程：")
        for c in conflicts[:10]:
            print(f"  pid={c['pid']} {c['name']} :: {c['cmd']}")
    else:
        print("未检测到 pytest/uvicorn/vitest/managed 进程")
    print(f"工作树：{'有改动 %d 项' % dirty_n if dirty else '干净'}")

    if args.dry_run:
        print("\n== DRY-RUN：不发送任何请求 ==")
        print_plan(args.tier, cases, args.repeat)
        if conflicts or dirty:
            print("\n[提示] 当前环境不满足安静窗口；真实运行会被拒绝（需 --force）。")
        return 0

    if (conflicts or dirty) and not args.force:
        print("\n[拒绝启动] 存在并发进程或工作树未冻结。"
              "请在安静窗口运行，或确认隔离后加 --force。", file=sys.stderr)
        return 2
    if not args.scheme_id and any(c["auto"] and c["id"] != "V8" for c in cases):
        print("[拒绝启动] 缺少 --scheme-id（V1/V2/V6/V7/V10 需要）。",
              file=sys.stderr)
        return 3

    if not GEN_ROOT.exists():
        print(f"[提示] 数据集尚未生成：{GEN_ROOT}\n"
              f"      先运行 python backend/tools/perf/dataset/generate_dataset.py "
              f"--tier {args.tier}", file=sys.stderr)
        return 3

    results: dict[str, dict] = {}
    started = datetime.now().isoformat(timespec="seconds")
    client = _client(args.base_url, args.timeout)

    try:
        for c in cases:
            cid = c["id"]
            if not c["auto"]:
                results[cid] = {"status": "SKIP", "reason": c["needs"]}
                continue
            if not args.scheme_id:
                results[cid] = {"status": "SKIP", "reason": "缺 --scheme-id"}
                continue
            total0 = time.perf_counter()
            if cid in ("V1", "V2"):
                samples = run_export(client, args.scheme_id, max(1, args.repeat))
                results[cid] = {"status": "OK", **summarize(samples)}
            elif cid == "V6":
                sse = run_sse(f"/api/v1/sse/generate-content/{args.scheme_id}",
                              {"concurrency": "balanced"}, client, args.timeout)
                done = sse["by_event"].get("section_done", 0)
                results[cid] = {"status": "OK", **sse,
                                "sections_per_s": round(done / sse["wall_s"], 3),
                                "chars_per_s": "需从 section_done 的 data 汇总"}
            elif cid == "V7":
                sse = run_sse(f"/api/v1/sse/generate-facts/{args.scheme_id}",
                              {}, client, args.timeout)
                results[cid] = {"status": "OK", **sse,
                                "fragments_per_s": sse["events"] / max(sse["wall_s"], 1e-6)}
            elif cid == "V8":
                results[cid] = {
                    "status": "OK",
                    "consistency_ms": round(run_consistency(client, args.scheme_id), 2),
                    "check_ms": round(run_compliance_check(client), 2),
                }
            elif cid == "V10":
                sse = run_sse(f"/api/v1/sse/generate-outline/{args.scheme_id}",
                              {}, client, args.timeout)
                results[cid] = {"status": "OK", **sse,
                                "sections_per_s": round(
                                    sse["by_event"].get("outline", 0) / max(sse["wall_s"], 1e-6), 3)}
            elif cid == "V5":
                results[cid] = {"status": "SKIP", "reason": "需 --upload-url 与样本文档"}
            results[cid]["elapsed_s"] = round(time.perf_counter() - total0, 2)
    finally:
        client.close()

    payload = {"started": started, "finished": datetime.now().isoformat(timespec="seconds"),
               "base_url": args.base_url, "scheme_id": args.scheme_id,
               "tier": args.tier, "results": results}
    out = args.out or (GEN_ROOT / "results" /
                       f"e2e_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8", newline="\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
