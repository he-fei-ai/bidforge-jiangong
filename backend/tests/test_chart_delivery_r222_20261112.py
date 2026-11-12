"""R222（2026-11-12）· 日志驱动：导出渲染降级可见性护栏。

**日志取证**：`logs/backend.log` 中 2026-11-09 21:15:40~21:16:01 方案
`8d3594d4` 的一次真实 DOCX 导出，21 秒内只有「导出前内容体检 / 表题自动生成 /
DOCX 已落盘 / 导出缓存未命中（产物已写缓存）」四条 INFO，**没有任何图表渲染
WARNING**；`X-Cache-Status=miss` 表明所有 degraded 判据都为假。而该次成稿
实际缺了甘特图（见 `chart-render-fix-report/chart-render-fix-report.md`）。

**根因**：`render_stats.failed` 只统计**预渲染阶段**返回 None 的图，回答不了
「这张图最终有没有进 DOCX」。构建期的四种跳过（无登记代码 / 去重 / 探测失败 /
插入失败）全部不回写，于是成稿缺图却被判为未降级、写进缓存长期固化。

本轮把「交付侧实际落图数」收口为单一事实源，本文件锁定下列不变式：
  A 累加器与单一判据（线程隔离 / 容错）
  B fix_stats → render_stats 桥的映射
  C 端到端：缺图 → degraded + charts-dropped + 不写缓存（旧实现会 miss）
  D 端到端：正常出图 → 不误降级（加法式字段不破坏既有行为）
  E 旁车：历史统计写入与命中回读（旧旁车 → 空 dict）
  F 双链同口径 + 结构化日志模板
"""

from __future__ import annotations

import inspect
import json
from io import BytesIO
from pathlib import Path

import pytest
from app.routers import export as E
from fastapi.responses import FileResponse

_WS_ROOT = Path(__file__).resolve().parents[2]


def _real_png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (32, 24), "white").save(buf, format="PNG")
    return buf.getvalue()


# ===========================================================================
# A · 累加器与单一判据
# ===========================================================================


def test_delivery_reset_is_thread_local_and_starts_clean():
    a = E._chart_delivery_reset()
    assert a == {"expected": 0, "inserted": 0, "dropped": 0, "duplicate_skipped": 0, "details": []}
    E._chart_delivery()["expected"] = 3
    E._note_chart_delivery("no_code", "sec1", "gantt")
    assert E._chart_delivery()["dropped"] == 1
    b = E._chart_delivery_reset()
    assert b["dropped"] == 0 and b["expected"] == 0
    assert b is not a


def test_delivery_reasons_are_classified():
    E._chart_delivery_reset()
    E._note_chart_delivery("ok", "s", "gantt", dropped=False)
    E._note_chart_delivery("duplicate", "s", "gantt")
    E._note_chart_delivery("insert_failed", "s2", "flowchart")
    E._note_chart_delivery("no_code", "s3", "layout")
    d = E._chart_delivery()
    assert d["inserted"] == 1
    assert d["duplicate_skipped"] == 1
    assert d["dropped"] == 2, d
    assert {x["reason"] for x in d["details"]} == {"insert_failed", "no_code"}


def test_delivery_details_are_capped_at_20():
    E._chart_delivery_reset()
    for i in range(35):
        E._note_chart_delivery("no_code", f"s{i}", "gantt")
    d = E._chart_delivery()
    assert d["dropped"] == 35
    assert len(d["details"]) == 20


def test_dropped_judge_is_defensive_about_dirty_fix_stats():
    assert E._chart_delivery_dropped(None) == 0
    assert E._chart_delivery_dropped({}) == 0
    assert E._chart_delivery_dropped({"chart_delivery_dropped": "3"}) == 3
    assert E._chart_delivery_dropped({"chart_delivery_dropped": "oops"}) == 0
    assert E._chart_delivery_dropped({"chart_delivery_dropped": None}) == 0


# ===========================================================================
# B · 单一桥映射
# ===========================================================================


def test_merge_bridge_maps_delivery_stats_additively():
    rs = {"fe": 1, "backend_ok": 2, "failed": 0}
    out = E._merge_borrowed_stats(
        rs,
        {
            "chart_delivery_expected": 4,
            "chart_delivery_inserted": 2,
            "chart_delivery_dropped": 1,
            "chart_delivery_duplicate_skipped": 1,
            "chart_delivery_dropped_details": [{"section_id": "s", "chart_type": "gantt", "reason": "no_code"}],
        },
    )
    assert out["chart_expected"] == 4
    assert out["chart_inserted"] == 2
    assert out["chart_dropped"] == 1
    assert out["chart_duplicate_skipped"] == 1
    assert out["chart_dropped_details"][0]["reason"] == "no_code"
    assert out["fe"] == 1 and out["backend_ok"] == 2, "既有键不得被改写"


def test_merge_bridge_leaves_old_shape_untouched():
    out = E._merge_borrowed_stats({"fe": 1, "backend_ok": 0, "failed": 0}, None)
    assert "chart_expected" not in out
    assert "chart_dropped" not in out
    assert out["fallback_borrowed"] == 0


# ===========================================================================
# C/D · 导出端到端
# ===========================================================================


async def _run_export(db_conn, scheme_id: str, content: str, monkeypatch, render_result=None):
    """render_result 传 callable 可每次返回新的 BytesIO（避免流被读一次后耗尽）。"""
    from app.services.ai import mermaid_renderer as mr

    def _fake(code, ct, fmt, skip_http, allow_pil, strategy_out=None):
        if isinstance(strategy_out, dict):
            strategy_out["strategy"] = "pil_syntax_fallback"
        return render_result() if callable(render_result) else render_result

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _fake)
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (scheme_id, f"p-{scheme_id}", "R222 落图统计"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"c-{scheme_id}", scheme_id, "", 0, 1, "第一章 施工", "generated", content),
    )
    await db_conn.commit()
    resp = await E.export_docx(
        scheme_id,
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    assert isinstance(resp, FileResponse), resp
    return resp.headers, json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")


@pytest.mark.asyncio
async def test_missing_chart_in_final_doc_is_degraded_even_with_failed_zero(db_conn, monkeypatch):
    """复现日志实证：有图表围栏但无登记代码 → 预渲染 failed=0、成稿却缺图。

    旧实现：unique_codes 为空 → 渲染段静默 → degraded 判据全假 → miss + 写缓存。
    R222：交付侧落图统计记 no_code → degraded + charts-dropped + 不写缓存。
    """
    headers, stats = await _run_export(
        db_conn,
        "s-r222-nocode",
        "施工进度安排见下图。\n\n[CHART_TYPE: gantt]\n",
        monkeypatch=monkeypatch,
    )
    assert int(stats.get("failed") or 0) == 0, stats
    assert stats.get("chart_expected") == 1, stats
    assert stats.get("chart_inserted") == 0, stats
    assert stats.get("chart_dropped") == 1, stats
    assert stats.get("chart_dropped_details", [{}])[0].get("reason") == "no_code", stats
    assert headers.get("X-Cache-Status") == "degraded", headers
    assert "charts-dropped" in (headers.get("X-Export-Degraded") or ""), headers


@pytest.mark.asyncio
async def test_render_success_without_insertion_is_degraded(db_conn, monkeypatch):
    """最隐蔽的一类：渲染返回 PNG 但插入失败 → backend_ok=1/failed=0。

    用坏字节模拟 _chart_probe 判坏图（PIL verify 失败），走 :3860 跳过出口。
    """
    headers, stats = await _run_export(
        db_conn,
        "s-r222-badpng",
        "工艺流程如下。\n\n```mermaid\nflowchart TD\n A[准备] --> B[施工]\n```\n",
        monkeypatch=monkeypatch,
        render_result=lambda: BytesIO(b"not-a-png"),
    )
    assert stats.get("backend_ok") == 1, stats
    assert stats.get("failed") == 0, stats
    assert stats.get("chart_dropped") == 1, stats
    assert stats.get("chart_dropped_details", [{}])[0].get("reason") == "render_or_probe_failed", stats
    assert headers.get("X-Cache-Status") == "degraded", headers


@pytest.mark.asyncio
async def test_normal_chart_delivery_does_not_false_degrade(db_conn, monkeypatch):
    """正常出图必须仍是 miss（加法式字段不得把全部导出判成降级）。"""
    headers, stats = await _run_export(
        db_conn,
        "s-r222-ok",
        "进度计划如下。\n\n```mermaid\ngantt\n title 进度\n dateFormat YYYY-MM-DD\n"
        " 施工准备 :2026-01-01, 5d\n```\n",
        monkeypatch=monkeypatch,
        render_result=lambda: BytesIO(_real_png()),
    )
    assert stats.get("chart_expected") == 1, stats
    assert stats.get("chart_inserted") == 1, stats
    assert not stats.get("chart_dropped"), stats
    assert headers.get("X-Cache-Status") == "miss", headers
    assert "charts-dropped" not in (headers.get("X-Export-Degraded") or "")


# ===========================================================================
# E · 旁车历史统计
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_sidecar_persists_chart_delivery_and_reads_back(tmp_path):
    out = tmp_path / "sample.docx"
    out.write_bytes(b"fake-docx")
    prep = {
        "content_audit": {"items": [], "counts": {}},
        "fences_fixed": 0,
        "sections": [],
        "render_stats": {
            "failed": 0,
            "backend_ok": 2,
            "chart_expected": 3,
            "chart_inserted": 2,
            "chart_dropped": 1,
        },
    }
    await E._write_audit_sidecar(out, prep, "hash-222")
    got = await E._read_chart_delivery_sidecar(out, "hash-222")
    assert got["chart_dropped"] == 1, got
    assert got["chart_expected"] == 3, got
    assert got["backend_ok"] == 2, got


@pytest.mark.asyncio
async def test_sidecar_reader_degrades_silently_for_missing_or_old_or_wrong_hash(tmp_path):
    assert (await E._read_chart_delivery_sidecar(tmp_path / "nope.docx", "h")) == {}
    out = tmp_path / "old.docx"
    out.write_bytes(b"x")
    sidecar = E._audit_sidecar_path(out)
    sidecar.write_text(json.dumps({"content_hash": "h", "audit": {"items": []}}), encoding="utf-8")
    assert (await E._read_chart_delivery_sidecar(out, "h")) == {}
    assert (await E._read_chart_delivery_sidecar(out, "other-hash")) == {}
    sidecar.write_text("{broken", encoding="utf-8")
    assert (await E._read_chart_delivery_sidecar(out, "h")) == {}


# ===========================================================================
# F · 双链同口径 / 结构化日志
# ===========================================================================


def test_docx_and_pdf_share_the_same_delivery_judge():
    docx = inspect.getsource(E.export_docx)
    pdf = inspect.getsource(E.export_pdf)
    assert "_chart_delivery_dropped(fix_stats)" in docx
    assert "_chart_delivery_dropped(fix_stats)" in pdf
    assert "charts-dropped" in docx and "charts-dropped" in pdf
    # R177 既有护栏锁死「每链单一合流点」，本轮不得破坏
    whole = inspect.getsource(E)
    assert whole.count("_merge_borrowed_stats(render_stats, fix_stats)") == 3


def test_structured_degraded_log_and_zero_code_stat_log_exist():
    src = Path(E.__file__).read_text(encoding="utf-8")
    assert "export_chart_degraded scheme=" in src
    assert "export_chart_delivery scheme=" in src
    # 零图路径不再完全静默：统计日志必须位于 unique_codes 分支之外
    assert "本次无待渲染图表代码" in src
    # 命中路径不得再退回「只有 cached:true」的零信息响应
    assert "cached_degraded" in src
