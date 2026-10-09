# -*- coding: utf-8 -*-
"""Diagnostic probe (R38): reproduce the flaky failure WITHOUT fixture isolation.

Injects dead-level reliability stats for the primary candidate identity, then
calls pf._build_candidates directly (no monkeypatch) and prints the resulting
order/key_broken. NOT collected by default runs (file name doesn't match
python_files pattern); invoke explicitly:
    python -m pytest tests/_r38_probe_flaky_repro.py -q
"""
import asyncio


def test_probe_dead_stats_reorder():
    import app.services.ai.provider_factory as pf
    key = pf._reliability_key("deepseek", "m", "c1", "https://api.x.com/v1")
    legacy = "deepseek"
    with pf._provider_reliability_lock:
        pf._provider_reliability[key] = {"ok": 0, "fail": 50}
        pf._provider_reliability[legacy] = {"ok": 0, "fail": 50}
    try:
        cfg = {"id": "c1", "provider_name": "deepseek",
               "api_key_encrypted": "gAAAA-not-a-valid-token",
               "base_url": "https://api.x.com/v1", "model": "m"}
        cands = asyncio.run(pf._build_candidates(cfg))
        for i, c in enumerate(cands):
            print("CAND[%d] id=%r provider=%r api_key=%r key_broken=%r primary=%r"
                  % (i, c.get("config_id"), c.get("provider_name"),
                     (c.get("api_key") or "")[:6],
                     c.get("key_broken"), c.get("_is_primary")))
        # original fragile assertion under polluted stats:
        fragile_ok = bool(cands) and cands[0]["key_broken"] is True
        print("FRAGILE_ASSERT_PASSES_WITHOUT_ISOLATION:", fragile_ok)
    finally:
        with pf._provider_reliability_lock:
            pf._provider_reliability.pop(key, None)
            pf._provider_reliability.pop(legacy, None)
