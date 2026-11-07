"""R176 纯函数微基准（不碰 DB / 网络 / 服务，单次测量 ≤ --budget 秒）。

覆盖上一轮报告 §6 实测过的同一批函数：
  · ``audit_content``            —— 导出正文审核（_export_issue_scan）
  · ``_content_fingerprint``     —— 导出缓存指纹（_export_cache）
  · ``sanitize_ai_content``      —— 正文清洗（content_polish）
  · ``_render_facts_text``       —— 全局事实 -> 提示词文本（facts_builder）
  · ``_dedup_continuation``      —— 续写去重（sse_utils）
  · ``_parse_content_blocks``    —— 正文块解析（content_blocks）

统计口径：每个样本是「一次 op」的墙钟耗时（内部自动批量摊薄计时噪声），
输出 best（最小值）/ median / P95，以及每节毫秒（ms/section）与每千字毫秒。

用法
----
    # 默认 S/M/L 三档，每档 7 次重复，单函数单档最多测 6 秒
    python backend/tools/perf/bench_micro.py

    # 指定章节规模（数值）与重复次数
    python backend/tools/perf/bench_micro.py --sections 30,120,300 --repeat 9

    # 只跑某几项，输出 JSON 便于对比
    python backend/tools/perf/bench_micro.py --only audit_content,_content_fingerprint --json

注意：进程级首次 import 会触发 FastAPI 模块加载（一次性，通常 <1s）；
若某模块正被其他会话改写导致 import 失败，该项会记为 SKIP 而不是中断整轮。
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import statistics
import sys
import time
import traceback
from pathlib import Path

_THIS = Path(__file__).resolve()
BACKEND_ROOT = _THIS.parents[2]          # backend/
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(_THIS.parent))    # perf/，用于 import dataset

from dataset.generate_dataset import TIERS, build_corpus  # noqa: E402


# ---------------------------------------------------------------------------
# 懒加载：任一模块 import 失败只影响该项
# ---------------------------------------------------------------------------
def _load_audit_content():
    # 先导入 facade，规避 prompts 包的部分初始化循环（上一轮已实测过的坑）
    import app.routers.export  # noqa: F401
    from app.routers._export_issue_scan import audit_content
    return audit_content


def _load_fingerprint():
    from app.routers._export_cache import _content_fingerprint
    return _content_fingerprint


def _load_sanitize():
    from app.services.content_polish import sanitize_ai_content
    return sanitize_ai_content


def _load_facts_render():
    from app.services.facts_builder import _render_facts_text
    return _render_facts_text


def _load_dedup():
    from app.routers.sse_utils import _dedup_continuation
    return _dedup_continuation


def _load_parse_blocks():
    from app.services.content_blocks import _parse_content_blocks
    return _parse_content_blocks


LOADERS = {
    "audit_content": _load_audit_content,
    "content_fingerprint": _load_fingerprint,
    "sanitize_ai_content": _load_sanitize,
    "render_facts_text": _load_facts_render,
    "dedup_continuation": _load_dedup,
    "parse_content_blocks": _load_parse_blocks,
}


# ---------------------------------------------------------------------------
# 语料与调用参数
# ---------------------------------------------------------------------------
def corpus_for(label: str) -> dict:
    """label 为 S/M/L 或正整数（按 L 档前 N 节裁剪）。"""
    if label in TIERS:
        return build_corpus(label)
    n = int(label)
    base = build_corpus("L")
    sections = base["sections"][:n]
    ratio = min(1.0, n / max(1, len(base["sections"])))
    facts = base["facts"][:max(1, int(len(base["facts"]) * ratio))]
    return {"tier": f"N{n}", "seed": base["seed"], "spec": base["spec"],
            "sections": sections, "facts": facts, "docs": base["docs"]}


def build_prep(corpus: dict) -> dict:
    """最小但真实的导出 prep（与 _content_fingerprint 的读取键一一对应）。"""
    sections = corpus["sections"]
    chart_count = int(corpus["spec"].get("charts", 4))
    chart_fp = [(sections[i % len(sections)]["id"], "flowchart", f"h{i:04d}")
                for i in range(chart_count)]
    return {
        "config": {},
        "scheme": {"name": "基准方案", "project_id": "proj-bench"},
        "fe_codes": ["cm-0001", "cm-0002", "cm-0003"],
        "fe_images": {},
        "chart_fp": chart_fp,
        "sections": sections,
        "global_facts": [{"gt": f["gt"], "title": f["title"],
                          "content": f["content"]} for f in corpus["facts"]],
        "appendix_sources": [],
        "global_facts_status": {"ok": True},
        "image_config": {},
    }


def facts_rows(corpus: dict) -> list[tuple]:
    return [(f["gt"], f["title"], f["content"], f["confidence"],
             f["chapter"], f["value_kind"]) for f in corpus["facts"]]


def make_call(name: str, corpus: dict, fns: dict):
    """返回 (fn, unit_divisor, unit_label, chars)。"""
    if name == "audit_content":
        return (lambda: fns[name](corpus["sections"]),
                len(corpus["sections"]), "ms/section",
                sum(len(s["content"]) for s in corpus["sections"]))
    if name == "content_fingerprint":
        prep = build_prep(corpus)
        return (lambda: fns[name](prep),
                len(corpus["sections"]), "ms/section",
                sum(len(s["content"]) for s in corpus["sections"]))
    if name == "render_facts_text":
        rows = facts_rows(corpus)
        chapter = corpus["sections"][0]["plain_title"]
        return (lambda: fns[name](rows, chapter=chapter),
                len(rows), "ms/fact",
                sum(len(r[2]) for r in rows))
    if name == "parse_content_blocks":
        content = corpus["sections"][0]["content"]
        return (lambda: fns[name](content), 1, "ms/section", len(content))
    if name == "sanitize_ai_content":
        content = corpus["sections"][0]["content"]
        return (lambda: fns[name](content), 1, "ms/section", len(content))
    if name == "dedup_continuation":
        content = corpus["sections"][0]["content"]
        prev = content[-600:]
        cont = content[-200:] + "\n\n此外，施工过程中应结合现场实际情况动态调整。"
        return (lambda: fns[name](prev, cont), 1, "ms/op", len(prev) + len(cont))
    raise KeyError(name)


# ---------------------------------------------------------------------------
# 计时
# ---------------------------------------------------------------------------
def _p95(samples: list[float]) -> float:
    if len(samples) == 1:
        return samples[0]
    idx = min(len(samples) - 1, max(0, math.ceil(0.95 * len(samples)) - 1))
    return sorted(samples)[idx]


def measure(fn, repeat: int, budget_s: float) -> dict:
    """自适应批量 + 预算保护；返回 best/median/p95（毫秒）与样本数。"""
    fn()  # 预热（也用于估算批量大小）
    t0 = time.perf_counter()
    fn()
    est = max(1e-9, time.perf_counter() - t0)

    inner = 1 if est > 0.02 else min(50, max(1, int(0.01 / est)))
    samples: list[float] = []
    spent = 0.0
    gc.disable()
    try:
        for _ in range(max(1, repeat)):
            start = time.perf_counter()
            for _ in range(inner):
                fn()
            elapsed = (time.perf_counter() - start) / inner
            samples.append(elapsed * 1000.0)
            spent += elapsed * inner
            if spent >= budget_s:
                break
    finally:
        gc.enable()

    return {
        "best_ms": min(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": _p95(samples),
        "samples": len(samples),
        "inner": inner,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="R176 纯函数微基准")
    ap.add_argument("--tiers", default="S,M,L",
                    help="规模档位，逗号分隔：S/M/L 或正整数（按 L 档裁剪）")
    ap.add_argument("--sections", default="",
                    help="数值章节规模覆盖（逗号分隔，优先于 --tiers）")
    ap.add_argument("--repeat", type=int, default=7, help="每项重复次数")
    ap.add_argument("--budget", type=float, default=6.0,
                    help="单函数单档累计测量预算（秒，硬上限 30）")
    ap.add_argument("--only", default="", help="只跑指定项（逗号分隔）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    budget = max(1.0, min(30.0, args.budget))
    labels = ([x.strip() for x in args.sections.split(",") if x.strip()]
              or [x.strip() for x in args.tiers.split(",") if x.strip()])
    names = ([x.strip() for x in args.only.split(",") if x.strip()]
             or list(LOADERS))

    # 一次性加载（模块级缓存）
    fns: dict = {}
    load_errors: dict[str, str] = {}
    for name in names:
        try:
            fns[name] = LOADERS[name]()
        except Exception as exc:
            load_errors[name] = f"{type(exc).__name__}: {exc}"

    rows: list[dict] = []
    for name in names:
        if name in load_errors:
            rows.append({"bench": name, "scale": "-", "status": "SKIP",
                         "reason": load_errors[name]})
            continue
        for label in labels:
            corpus = corpus_for(label)
            try:
                fn, divisor, unit, chars = make_call(name, corpus, fns)
                stats = measure(fn, args.repeat, budget)
            except Exception:
                rows.append({"bench": name, "scale": label, "status": "ERROR",
                             "reason": traceback.format_exc(limit=2)})
                continue
            rows.append({
                "bench": name, "scale": label, "status": "OK",
                "sections": len(corpus["sections"]),
                "chars": chars,
                "repeat": stats["samples"],
                "inner": stats["inner"],
                "best_ms": round(stats["best_ms"], 4),
                "median_ms": round(stats["median_ms"], 4),
                "p95_ms": round(stats["p95_ms"], 4),
                "ms_per_unit": round(stats["median_ms"] / max(1, divisor), 4),
                "unit": unit,
            })

    if args.json:
        print(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    header = (f"{'bench':<24}{'scale':<8}{'secs':>5}{'chars':>8}"
              f"{'reps':>5}{'best_ms':>10}{'median':>10}{'p95':>10}{'ms/unit':>12}")
    print(header)
    print("-" * len(header))
    for r in rows:
        if r["status"] != "OK":
            print(f"{r['bench']:<24}{r['scale']:<8}  {r['status']}: "
                  f"{str(r.get('reason', '')).splitlines()[-1][:120]}")
            continue
        print(f"{r['bench']:<24}{r['scale']:<8}{r['sections']:>5}{r['chars']:>8}"
              f"{r['repeat']:>5}{r['best_ms']:>10.3f}{r['median_ms']:>10.3f}"
              f"{r['p95_ms']:>10.3f}{r['ms_per_unit']:>12.4f} {r['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
