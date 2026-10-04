# -*- coding: utf-8 -*-
"""目录生成 / 正文生成 · AI 调用次数与并发基线对比工具

设计原则（保证「优化前后」可比）：
  · 固定语料（N_LEAF=12，与 tests/test_consistency_scan_optimization.py 的
    fixture 完全一致 —— 即「同一批测试用例」）；
  · 真实代码路径 + fake AI：只替换最底层 AI 入口
    （sse_handlers.collect_json_response / consistency_scanner.ai_scan_*
     / repair_agent.repair_section），并发信号量、切批、增量缓存、失败回退、
    重试全部走生产代码；
  · 所有变体（含「基线=旧行为」）都跑**同一份代码**，差异只来自配置参数，
    因此结果就是纯配置效果，不含实现回归；
  · 失败注入是确定性的（calls % FAILURE_EVERY == 0 抛异常），变体间可复现。

产出：backend/_diagnostics/baseline_result.json + stdout 报表。

用法：
    cd backend
    python -m pytest _baseline_calls.py -q -s --no-header
"""
import asyncio
import json
import os
import random
import time
from collections import Counter

import app.routers.sse_handlers as sh
import app.services.consistency_scanner as cs
import app.services.repair_agent as ra
import pytest

# ---------- 固定语料（同一批测试用例） ----------
N_LEAF = 12          # 正文叶子章节数 / 目录一级章数
LEAF_CHARS = 200     # 每章正文字符数
FAILURE_EVERY = 3    # 每 3 次 AI 调用注入 1 次失败（33% 失败率）
CALL_MS = 0.4        # 每次 AI 调用模拟墙钟（毫秒）
LATENCY = CALL_MS / 1000.0

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "_diagnostics")
RESULT_PATH = os.path.join(OUT_DIR, "baseline_result.json")


class Tracker:
    """AI 调用计数器：次数 / 并发峰值 / 失败次数 / token 估算。"""

    def __init__(self, fail_every=0, fail_batches=False):
        self.calls = 0
        self.failures = 0
        self.rejected = 0      # AI 返回但被程序校验拒绝（白烧的调用）
        self.active = 0
        self.peak = 0
        self.in_chars = 0
        self.out_chars = 0
        self.fail_every = fail_every
        self.fail_batches = fail_batches

    async def hit(self, in_chars, out_chars, is_batch=False):
        """记录一次 AI 调用；按注入规则可能抛异常。"""
        self.active += 1
        if self.active > self.peak:
            self.peak = self.active
        self.calls += 1
        self.in_chars += int(in_chars)
        self.out_chars += int(out_chars)
        try:
            if self.fail_batches and is_batch:
                self.failures += 1
                raise RuntimeError("injected-batch-fail")
            if self.fail_every and (self.calls % self.fail_every) == 0:
                self.failures += 1
                raise RuntimeError("injected-fail")
            await asyncio.sleep(LATENCY)
            await asyncio.sleep(LATENCY)
            return "OK"
        finally:
            self.active -= 1

    def tokens(self):
        """中文约 1 字 ≈ 0.6 token（含标点的经验折算）。"""
        return int((self.in_chars + self.out_chars) * 0.6)

    def snap(self):
        return {"calls": self.calls, "failures": self.failures,
                "rejected": self.rejected, "peak_conc": self.peak,
                "tokens_est": self.tokens()}


# ============================================================
# 驱动 1：目录生成 · 逐章子目录
#   复刻 sse_handlers.py:2612-2620 的单元划分与并发驱动逻辑
# ============================================================
async def drive_outline(merge_k, conc, fail_every=0, fail_batches=False,
                        n=N_LEAF):
    tr = Tracker(fail_every=fail_every, fail_batches=fail_batches)
    chs = [{"title": f"第{i+1}章", "description": ""} for i in range(n)]
    single_prompts = [[{"role": "system", "content": f"SINGLE_PROMPT_{i}"}]
                      for i in range(n)]

    def batch_prompt_for(unit):
        """批提示词内嵌本批全局章号，便于断言「章未丢、号未错」。"""
        ids = ",".join(str(gi + 1) for gi, _ in unit)
        return [{"role": "system", "content": f"BATCH_PROMPT {ids}"}]

    async def fake_collect(messages, validate_fn=None, *a, **kw):
        """与真实 collect_json_response 同签名：返回 (obj, raw_text)。"""
        txt = " ".join(str(m.get("content", "")) for m in messages)
        is_batch = txt.startswith("BATCH_PROMPT")
        ids = ([int(x) for x in txt.split(" ", 1)[1].split(",")]
               if is_batch else [0])
        await tr.hit(len(txt), len(ids) * 800, is_batch=is_batch)
        obj = ({"chapters": [{"chapter_id": str(i), "outline": []} for i in ids]}
               if is_batch else {"outline": []})
        return obj, json.dumps(obj, ensure_ascii=False)

    async def fake_aws(coro, push_stats, *a, **k):
        return await coro

    async def noop(*a, **k):
        return None

    old = (sh.collect_json_response, sh._await_with_stats,
           sh.wait_resume, sh.is_stopped)
    sh.collect_json_response = fake_collect
    sh._await_with_stats = fake_aws
    sh.wait_resume = noop
    sh.is_stopped = lambda *a, **k: False
    try:
        batch_size = min(max(1, conc), max(1, n))
        sem = asyncio.Semaphore(batch_size) if batch_size > 1 else None
        units = [[(gi, chs[gi]) for gi in range(u, min(u + merge_k, n))]
                 for u in range(0, n, merge_k)]
        t0 = time.perf_counter()
        results = []
        # merge_k=1 时 _fetch_unit_children 会把 batch_prompt 当作单章提示词
        # （sse_handlers.py:2211 sub_prompt=batch_prompt），因此必须按单元区分
        def _bp(u):
            return batch_prompt_for(u) if merge_k > 1 else single_prompts[u[0][0]]

        if conc <= 1:
            for u in units:
                results.append(await sh._fetch_unit_children(
                    u, batch_prompt=_bp(u), single_prompts=single_prompts,
                    task_id="t", sem=sem, timeout=1.0, push_stats=None))
        else:
            results = list(await asyncio.gather(*[
                sh._fetch_unit_children(
                    u, batch_prompt=_bp(u),
                    single_prompts=single_prompts,
                    task_id="t", sem=sem, timeout=1.0, push_stats=None)
                for u in units]))
        # 质量护栏：统计每章状态，防止「为省调用而丢掉章节成果」的回归
        statuses = []
        for st, per_ch in results:
            if st == "stopped":
                statuses.append("stopped")
                continue
            statuses.extend([s for s, _ in per_ch] or [st])
        return (tr, (time.perf_counter() - t0) * 1000.0,
                {"units": len(units), "statuses": dict(Counter(statuses))})
    finally:
        (sh.collect_json_response, sh._await_with_stats,
         sh.wait_resume, sh.is_stopped) = old


# ============================================================
# 驱动 2：正文生成 · 全文一致性扫描（含增量缓存）
# ============================================================
async def seed_leaf_sections(db, n=N_LEAF):
    for i in range(n):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
            " VALUES (?,?,?,?,1,?)",
            (f"s{i}", "sch1", f"第{i+1}章",
             "基坑深度 18.5m，工期 120 日历天。" * (LEAF_CHARS // 20), i))
    await db.commit()


async def drive_scan(db, batch_size, conc, use_cache, fail_every=0,
                     fail_batches=False):
    tr = Tracker(fail_every=fail_every, fail_batches=fail_batches)
    ok_batches, ok_single = [], []

    async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
        await tr.hit(sum(len(s.get("content", "")) for s in sections) + 1200,
                     len(sections) * 600, is_batch=True)
        ok_batches.append(len(sections))
        return {s["id"]: [] for s in sections}

    async def fake_single(*, section, facts, project_docs, design_docs, standards):
        await tr.hit(len(section.get("content", "")) + 1200, 600, is_batch=False)
        ok_single.append(section["id"])
        return []

    old = (cs.ai_scan_batch, cs.ai_scan_section, cs.CONSISTENCY_SCAN_BATCH_SIZE,
           cs.CONSISTENCY_SCAN_CONCURRENCY)
    cs.ai_scan_batch, cs.ai_scan_section = fake_batch, fake_single
    cs.CONSISTENCY_SCAN_BATCH_SIZE = max(1, int(batch_size))
    cs.CONSISTENCY_SCAN_CONCURRENCY = max(1, int(conc))
    try:
        t0 = time.perf_counter()
        res = await cs.run_scan(db, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t",
                                use_cache=bool(use_cache))
        return (tr, (time.perf_counter() - t0) * 1000.0,
                {"sections": res.get("sections"), "cached": res.get("cached"),
                 "conflicts": res.get("total"),
                 "batch_sizes": ok_batches, "single_sections": ok_single})
    finally:
        (cs.ai_scan_batch, cs.ai_scan_section,
         cs.CONSISTENCY_SCAN_BATCH_SIZE, cs.CONSISTENCY_SCAN_CONCURRENCY) = old


# ============================================================
# 驱动 3：正文生成 · 一致性定向修复
# ============================================================
BAD_AFTER = "修复后正文：檐口高度 18.5m。"      # 仍含错误值 → 校验不过

def conflict(cid, sid, topic="檐口高度"):
    return {
        "id": cid, "conflict_type": "numeric", "severity": "high",
        "topic": topic, "authoritative_value": "42.5m",
        "authoritative_source": "全局事实", "status": "pending",
        "occurrences": [{"section_id": sid, "section_title": f"章节{sid}",
                         "value": "18.5m", "text": f"{topic} 18.5m"}],
    }


async def drive_repair(db, conc, max_sections, retry_invalid, bad_every=0):
    """bad_every: 每 N 个章节组返回「校验不过」的结果（确定性注入，跨变体可比）。"""
    tr = Tracker()
    groups_called = []

    async def fake_repair(*, section_id, section_title, section_content,
                          conflicts_in_section, facts, sources):
        groups_called.append(section_id)
        await tr.hit(len(section_content) + 1500, 3000)
        # 按章节号取模，结果与并发调度顺序无关（跨轮次可复现）
        bad = bool(bad_every) and (int(section_id[1:]) % bad_every) == 0
        return BAD_AFTER if bad else "修复后正文：檐口高度 42.5m。"

    old = (ra.repair_section, ra.REPAIR_CONCURRENCY, ra.REPAIR_RETRY_ON_INVALID,
           ra.REPAIR_MAX_SECTIONS, ra.validate_repair)
    ra.repair_section = fake_repair
    ra.REPAIR_CONCURRENCY = max(1, int(conc))
    ra.REPAIR_RETRY_ON_INVALID = bool(retry_invalid)
    ra.REPAIR_MAX_SECTIONS = int(max_sections)
    ra.validate_repair = (lambda **kw:
                          (False, ["仍含错误值"])
                          if "18.5m" in str(kw.get("after", "")) else (True, []))
    try:
        t0 = time.perf_counter()
        res = await ra.run_repair(db, scheme_id="sch1", scan_id="scan1",
                                  conflicts=conflicts, mode="auto",
                                  severity_threshold="low")
        return (tr, (time.perf_counter() - t0) * 1000.0,
                {"groups_called": len(groups_called),
                 "repaired": res.get("repaired"), "failed": res.get("failed"),
                 "skipped": res.get("skipped")})
    finally:
        (ra.repair_section, ra.REPAIR_CONCURRENCY, ra.REPAIR_RETRY_ON_INVALID,
         ra.REPAIR_MAX_SECTIONS, ra.validate_repair) = old


# ============================================================
# 变体矩阵（同一批用例 × 不同配置）  outline = (merge_k, concurrency)
# ============================================================
def variants():
    return [
        ("V0 基线(旧行为)", dict(outline=(1, 1), scan_batch=1, scan_conc=1,
                                use_cache=False, repair_conc=1, max_sections=0,
                                retry_invalid=True)),
        ("V1 当前默认", dict(outline=(1, 3), scan_batch=4, scan_conc=3,
                             use_cache=True, repair_conc=3, max_sections=40,
                             retry_invalid=False)),
        ("V2 推荐·降并发防限流", dict(outline=(1, 2), scan_batch=2, scan_conc=2,
                                      use_cache=True, repair_conc=2,
                                      max_sections=40, retry_invalid=False)),
        ("V3 推荐·强模型批量", dict(outline=(3, 2), scan_batch=6, scan_conc=2,
                                     use_cache=True, repair_conc=2,
                                     max_sections=40, retry_invalid=False)),
        ("V4 极限批量", dict(outline=(6, 3), scan_batch=12, scan_conc=3,
                              use_cache=True, repair_conc=3, max_sections=40,
                              retry_invalid=False)),
    ]


REPAIR_BAD = 4      # 每 4 个修复章节组返回一次「校验不过」
conflicts = []


async def _fresh(db, n=N_LEAF):
    """每个变体前重置语料与修复留痕，保证各变体语义等同「首次生成」。"""
    for t in ("sections", "consistency_repairs", "consistency_conflicts",
              "consistency_scan_cache", "scheme_snapshots"):
        try:
            await db.execute(f"DELETE FROM {t} WHERE scheme_id='sch1'")
        except Exception:
            await db.execute(f"DELETE FROM {t}")
    await db.commit()
    await seed_leaf_sections(db, n)
    global conflicts
    conflicts = [conflict(f"C{i:03d}", f"s{i}") for i in range(n)]


def static_costs():
    """非逐章、固定成本（静态代码核算，非本次 mock 实测）。"""
    return {"outline_draft": 1, "outline_review": 1, "outline_fix": "0~1",
            "content_draft": N_LEAF, "content_continue": f"0~{2 * N_LEAF}",
            "content_shrink": f"0~{3 * N_LEAF}(默认关闭)",
            "word_budget_alloc": "0 或 1", "consistency_arbitrate": 1}


@pytest.mark.asyncio
async def test_baseline_matrix(db_conn):
    os.makedirs(OUT_DIR, exist_ok=True)
    rows, bad_rows, worst_rows = [], [], []
    for name, cfg in variants():
        await _fresh(db_conn)
        rec = {"variant": name, "config": dict(cfg)}
        tr_o, wall_o, det_o = await drive_outline(cfg["outline"][0], cfg["outline"][1])
        tr_s, wall_s, det_s = await drive_scan(
            db_conn, cfg["scan_batch"], cfg["scan_conc"], cfg["use_cache"])
        tr_r, wall_r, det_r = await drive_repair(
            db_conn, cfg["repair_conc"], cfg["max_sections"], cfg["retry_invalid"])
        # ---- 质量护栏断言：优化不许以「丢章节/丢冲突」为代价 ----
        assert det_o["statuses"].get("ok") == N_LEAF, (
            f"{name}: 12 章未全部生成成功，实际={det_o['statuses']}")
        assert det_s["sections"] == N_LEAF, f"{name}: 扫描章节数异常 {det_s}"
        assert det_r["repaired"] == N_LEAF, f"{name}: 修复未覆盖全部冲突 {det_r}"
        rec["outline_chapter"] = dict(
            tr_o.snap(), wall_ms=round(wall_o, 2),
            chapter_status=det_o["statuses"], units=det_o["units"])
        rec["consistency_scan"] = dict(
            tr_s.snap(), wall_ms=round(wall_s, 2), sections=det_s["sections"],
            cached=det_s["cached"], batch_sizes=det_s["batch_sizes"])
        rec["consistency_repair"] = dict(
            tr_r.snap(), wall_ms=round(wall_r, 2),
            groups_called=det_r["groups_called"], repaired=det_r["repaired"],
            failed=det_r["failed"], skipped=det_r["skipped"])
        ks = ("outline_chapter", "consistency_scan", "consistency_repair")
        rec["TOTAL"] = {
            "calls": sum(rec[k]["calls"] for k in ks),
            "peak_conc": max(rec[k]["peak_conc"] for k in ks),
            "tokens_est": sum(rec[k]["tokens_est"] for k in ks),
            "wall_ms": round(sum(rec[k]["wall_ms"] for k in ks), 2)}
        rows.append(rec)

        await _fresh(db_conn)
        b = {"variant": name,
             "fail": f"每 {FAILURE_EVERY} 次调用失败 1 次",
             "worst": "批调用必失败（回退到逐章调用）"}
        tr_o2, _, _ = await drive_outline(cfg["outline"][0], cfg["outline"][1],
                                          FAILURE_EVERY)
        tr_s2, _, _ = await drive_scan(db_conn, cfg["scan_batch"], cfg["scan_conc"],
                                       cfg["use_cache"], FAILURE_EVERY)
        tr_r2, _, dr2 = await drive_repair(db_conn, cfg["repair_conc"],
                                           cfg["max_sections"], cfg["retry_invalid"],
                                           bad_every=REPAIR_BAD)
        b["outline_chapter"] = dict(tr_o2.snap())
        b["consistency_scan"] = dict(tr_s2.snap())
        b["consistency_repair"] = dict(tr_r2.snap(), repaired=dr2["repaired"],
                                       failed=dr2["failed"])
        b["TOTAL_calls"] = sum(x["calls"] for x in
                               (b["outline_chapter"], b["consistency_scan"],
                                b["consistency_repair"]))
        b["TOTAL_failures"] = sum(x["failures"] for x in
                                  (b["outline_chapter"], b["consistency_scan"],
                                   b["consistency_repair"]))

        # 最坏场景：批调用全部失败 → 观察「批处理回退」的调用放大
        await _fresh(db_conn)
        w = {"variant": name}
        tr_o3, _, _ = await drive_outline(cfg["outline"][0], cfg["outline"][1],
                                         fail_batches=True)
        tr_s3, _, det_s3 = await drive_scan(db_conn, cfg["scan_batch"],
                                            cfg["scan_conc"], cfg["use_cache"],
                                            fail_batches=True)
        tr_r3, _, _ = await drive_repair(db_conn, cfg["repair_conc"],
                                         cfg["max_sections"], cfg["retry_invalid"])
        w["outline_chapter"] = dict(tr_o3.snap())
        w["consistency_scan"] = dict(tr_s3.snap(),
                                     fallback_sections=det_s3["single_sections"])
        w["consistency_repair"] = dict(tr_r3.snap())
        w["TOTAL_calls"] = sum(x["calls"] for x in
                               (w["outline_chapter"], w["consistency_scan"],
                                w["consistency_repair"]))
        bad_rows.append(b)
        worst_rows.append(w)

    m = {}
    for cfg_v in ("V0", "V2"):
        cfg = next(c for n, c in variants() if n.startswith(cfg_v))
        await _fresh(db_conn)
        t1, _, d1 = await drive_repair(db_conn, cfg["repair_conc"],
                                       cfg["max_sections"], cfg["retry_invalid"])
        t2, _, d2 = await drive_repair(db_conn, cfg["repair_conc"],
                                       cfg["max_sections"], cfg["retry_invalid"])
        m[cfg_v] = {"run1_calls": t1.calls, "run2_calls": t2.calls,
                    "run1_repaired": d1["repaired"], "run2_skipped": d2["skipped"]}

    out = {"corpus": {"leaf_sections": N_LEAF, "chars_per_section": LEAF_CHARS,
                      "conflicts": N_LEAF, "call_latency_ms": CALL_MS,
                      "failure_every": FAILURE_EVERY, "repair_bad_every": REPAIR_BAD},
           "static_costs_per_run": static_costs(),
           "variants_ok": rows, "variants_fail_injected": bad_rows,
           "variants_worst_batch_fail": worst_rows,
           "repair_incremental_skip": m}
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(report(rows, bad_rows, worst_rows, m))


HEAD = (f"{'变体':<24}{'目录章':>7}{'扫描':>6}{'修复':>6}{'合计':>7}"
        f"{'并发峰值':>9}{'tokens':>10}{'墙钟ms':>9}")


def report(rows, bad_rows, worst_rows, m):
    L = []
    L.append("=" * 100)
    L.append(f"基线对比  语料：{N_LEAF} 叶子章节 / 每章 {LEAF_CHARS} 字 / "
             f"{N_LEAF} 条冲突  单次调用模拟 {CALL_MS}ms")
    L.append("=" * 100)
    L.append(HEAD)
    L.append("-" * 100)
    for r in rows:
        o, s, p = (r["outline_chapter"], r["consistency_scan"],
                   r["consistency_repair"])
        L.append(f"{r['variant']:<24}{o['calls']:>7}{s['calls']:>6}{p['calls']:>6}"
                 f"{r['TOTAL']['calls']:>7}{r['TOTAL']['peak_conc']:>9}"
                 f"{r['TOTAL']['tokens_est']:>10}{r['TOTAL']['wall_ms']:>9.1f}")
    L.append("-" * 100)
    L.append("扫描阶段批形状 / 增量缓存命中：")
    for r in rows:
        s = r["consistency_scan"]
        L.append(f"  {r['variant']:<24} batch_sizes={s['batch_sizes']}"
                 f"  cached={s['cached']}/{s['sections']}"
                 f"  章状态={r['outline_chapter']['chapter_status']}")
    L.append("")
    L.append(f"[场景A] 失败注入（每 {FAILURE_EVERY} 次调用失败 1 次）—— 重试放大：")
    L.append(HEAD)
    L.append("-" * 100)
    for b in bad_rows:
        o, s, p = (b["outline_chapter"], b["consistency_scan"],
                   b["consistency_repair"])
        L.append(f"{b['variant']:<24}{o['calls']:>7}{s['calls']:>6}{p['calls']:>6}"
                 f"{b['TOTAL_calls']:>7}{'-':>9}{'-':>10}{'-':>9}"
                 f"   失败={b['TOTAL_failures']}"
                 f"  修复:repaired={b['consistency_repair'].get('repaired')}"
                 f"/failed={b['consistency_repair'].get('failed')}")
    L.append("")
    L.append("[场景B] 批调用必失败 —— 批处理回退的调用放大（越大越差）：")
    L.append(HEAD)
    L.append("-" * 100)
    for w in worst_rows:
        o, s, p = (w["outline_chapter"], w["consistency_scan"],
                   w["consistency_repair"])
        L.append(f"{w['variant']:<24}{o['calls']:>7}{s['calls']:>6}{p['calls']:>6}"
                 f"{w['TOTAL_calls']:>7}{'-':>9}{'-':>10}{'-':>9}"
                 f"   回退逐章数={len(s.get('fallback_sections') or [])}")
    L.append("-" * 100)
    L.append("修复阶段增量跳过（连续两次全量生成，同一批语料）：")
    for k, v in m.items():
        L.append(f"  {k}: 第1次 {v['run1_calls']} 次调用"
                 f"(repaired={v['run1_repaired']}) -> 第2次 {v['run2_calls']} 次调用"
                 f"(skipped={v['run2_skipped']})")
    L.append("=" * 100)
    return "\n".join(L)


# ============================================================
# 场景C：增量扫描缓存（第二次生成，正文未变 → 应 0 次扫描调用）
# 场景D：规模放大（30 章长方案，看各变体调用数如何随 N 增长）
# ============================================================
@pytest.mark.asyncio
async def test_scan_cache_and_scale(db_conn):
    cache_rows, scale_rows = [], []
    for label, batch_size, conc in (("batch=1(旧行为)", 1, 1),
                                    ("batch=4(当前默认)", 4, 3),
                                    ("batch=12(极限)", 12, 3)):
        await _fresh(db_conn)
        t1, _, _ = await drive_scan(db_conn, batch_size, conc, True)
        t2, _, det2 = await drive_scan(db_conn, batch_size, conc, True)
        t3, _, det3 = await drive_scan(db_conn, batch_size, conc, False)
        cache_rows.append({"label": label, "run1_calls": t1.calls,
                           "run2_calls_cached": t2.calls,
                           "run2_hits": det2["cached"],
                           "run3_calls_forced_full": t3.calls})

    for label, n in (("N=12", 12), ("N=30", 30), ("N=80", 80)):
        row = {"label": label}
        for vname, cfg in variants():
            await _fresh(db_conn, n=n)
            tr_o, _, _ = await drive_outline(cfg["outline"][0], cfg["outline"][1],
                                            n=n)
            tr_s, _, _ = await drive_scan(db_conn, cfg["scan_batch"],
                                          cfg["scan_conc"], cfg["use_cache"])
            tr_r, _, _ = await drive_repair(db_conn, cfg["repair_conc"],
                                            cfg["max_sections"],
                                            cfg["retry_invalid"])
            row[vname.split()[0]] = tr_o.calls + tr_s.calls + tr_r.calls
        scale_rows.append(row)

    with open(os.path.join(OUT_DIR, "baseline_cache_scale.json"), "w",
              encoding="utf-8") as f:
        json.dump({"scan_incremental_cache": cache_rows,
                   "scale_by_chapter_count": scale_rows},
                  f, ensure_ascii=False, indent=2)
    L = ["", "=" * 100, "[场景C] 一致性扫描增量缓存（同一正文连续扫描 3 次）", "=" * 100]
    for r in cache_rows:
        L.append(f"  {r['label']:<20} 第1次={r['run1_calls']}  "
                 f"第2次(缓存)={r['run2_calls_cached']} (命中 {r['run2_hits']})  "
                 f"第3次(强制全量)={r['run3_calls_forced_full']}")
    L.append("")
    L.append("[场景D] 调用数随章节数增长（目录章+扫描+修复，无故障）")
    heads = [v[0].split()[0] for v in variants()]
    L.append(f"  {'规模':<8}" + "".join(f"{h:>8}" for h in heads))
    for r in scale_rows:
        L.append(f"  {r['label']:<8}" + "".join(f"{r[h]:>8}" for h in heads))
    L.append("=" * 100)
    print("\n".join(L))
