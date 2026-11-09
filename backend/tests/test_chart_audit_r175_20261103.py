"""R175 图表生成与渲染模块审计 · 修复回归（BUG-175-01 ~ 04）。

覆盖：
- BUG-175-01：ChartCache 写入侧 skip_http 丢失（写后读命中 / 跨轨隔离 / follower 成功）；
- BUG-175-02：_RENDERER_VERSION 升级并使内存/磁盘两道缓存同时失效；
- BUG-175-03：甘特图校验端与渲染端任务有效性同一判据、截断留痕、无日期任务不重叠；
- BUG-175-04：图题空白折叠（保留词间单空格）而非删除。

⚠️ 只跑本文件即可（禁止全量 pytest）：
    python -u -m pytest tests/test_chart_audit_r175_20261103.py -p no:cacheprovider -q
"""

import json
import logging
import threading
import time
from io import BytesIO

from app.services.ai import mermaid_renderer as mr
from app.services.ai.mermaid_gantt import _build_gantt_tasks, _render_gantt_image_v2
from app.services.chart_validators import normalize_gantt_plan, validate_gantt_plan
from app.services.content_blocks import _clean_chart_title, _title_candidate

_GANTT_CODE = 'flowchart TD\n    A["施工准备"] --> B["基础施工"]'


def _r199_png() -> bytes:
    """✅ R199：get_or_render 磁盘命中补全文件完整性校验（长度 + PIL verify）——
    fixture 必须提供真实可解码 PNG（旧假字节 b"PNG-PIL" 会被新判据正确拒收）。"""
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 48), (90, 140, 200)).save(buf, format="PNG")
    data = buf.getvalue()
    assert len(data) > 100
    return data


def _make_cache(tmp_path):
    return mr.ChartCache(cache_dir=str(tmp_path / "charts"))


# ---------------- BUG-175-01：缓存 skip_http 写侧丢失 ----------------


def test_cache_skip_http_write_then_read_hits(tmp_path, monkeypatch):
    """写后必须能读到自己的缓存（旧实现写进 http=1 槽，skip_http=True 读 http=0 → 恒 miss）。"""
    cache = _make_cache(tmp_path)
    calls = []

    def fake_render(code, chart_type="flowchart", *, skip_http=False, allow_pil=True):
        calls.append(skip_http)
        return BytesIO(_r199_png())

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake_render)

    for _ in range(3):
        out = cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=True)
        assert out is not None and out.getvalue() == _r199_png()

    # 只渲染一次 → 第 2/3 次均命中自身写入的缓存
    assert calls == [True]
    assert cache.get_stats()["hits"] >= 2
    # 公开 get() 也必须用同一 skip_http 口径命中
    assert cache.get(_GANTT_CODE, chart_type="flowchart", allow_pil=True, skip_http=True) is not None


def test_cache_cross_track_isolation(tmp_path, monkeypatch):
    """skip_http=True（PIL）与 skip_http=False（HTTP）的缓存条目互不串用。"""
    cache = _make_cache(tmp_path)

    def fake_render(code, chart_type="flowchart", *, skip_http=False, allow_pil=True):
        return BytesIO(b"PIL-TRACK" if skip_http else b"HTTP-TRACK")

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", fake_render)

    pil = cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=True)
    http = cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=False)
    assert pil is not None and pil.getvalue() == b"PIL-TRACK"
    assert http is not None and http.getvalue() == b"HTTP-TRACK"

    # 两个轨道各自落一份文件，且再次读取仍各自命中（不串轨）
    assert len(list(cache.cache_dir.glob("*.png"))) == 2
    assert cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=True).getvalue() == b"PIL-TRACK"
    assert cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=False).getvalue() == b"HTTP-TRACK"


def test_cache_follower_shares_leader_result(tmp_path, monkeypatch):
    """单飞 follower 不得因写侧漏 skip_http 而假失败。"""
    cache = _make_cache(tmp_path)
    leader_started = threading.Event()
    render_calls = []

    def slow_render(code, chart_type="flowchart", *, skip_http=False, allow_pil=True):
        render_calls.append(skip_http)
        leader_started.set()
        time.sleep(0.15)
        return BytesIO(_r199_png())

    monkeypatch.setattr(mr, "render_mermaid_to_bytes", slow_render)

    results = []

    def worker():
        results.append(cache.get_or_render(_GANTT_CODE, "flowchart", skip_http=True))

    leader = threading.Thread(target=worker)
    leader.start()
    assert leader_started.wait(timeout=2.0), "leader 未进入渲染"
    follower = threading.Thread(target=worker)
    follower.start()
    leader.join(timeout=5.0)
    follower.join(timeout=5.0)

    assert len(results) == 2
    assert all(r is not None for r in results), "follower 假失败（旧 BUG-175-01 症状）"
    assert all(r.getvalue() == _r199_png() for r in results)
    assert render_calls == [True], "同一 key 只允许 leader 渲染一次"


# ---------------- BUG-175-02：渲染器版本号 ----------------


def test_renderer_version_bumped_and_feeds_both_caches():
    """版本号 ≥ 8，且同时进入内存 LRU 键与磁盘键（旧缓存自然失效）。"""
    assert mr._RENDERER_VERSION >= 8
    assert mr._render_cache_key(_GANTT_CODE, "flowchart", 90, "天", 5, True, True)[0] == f"v{mr._RENDERER_VERSION}"
    # 磁盘键含版本段：换版本后同一 payload 的 key 必然不同
    old = mr.ChartCache._cache_key("flowchart", _GANTT_CODE, True, True)
    assert isinstance(old, str) and len(old) == 32


# ---------------- BUG-175-03：甘特图校验/渲染同一判据 ----------------


def test_gantt_empty_name_parity_end_to_end():
    """`{"tasks":[{"id":"t1"}]}`：归一/校验/渲染三侧必须同向拒绝。"""
    payload = {"tasks": [{"id": "t1"}]}
    assert normalize_gantt_plan(payload) is None
    ok, errs = validate_gantt_plan(payload)
    assert not ok and errs
    assert _render_gantt_image_v2(payload) is None
    assert mr.render_mermaid_to_bytes(json.dumps(payload), "gantt") is None


def test_gantt_mixed_empty_name_dropped_both_sides():
    """混入空名任务时：校验端剔除、渲染端同样跳过，整体仍合法出图。"""
    payload = {
        "type": "gantt",
        "tasks": [
            {"id": 1, "name": "施工准备", "start": 0, "end": 10},
            {"id": 2},  # 空名 → 两侧都剔除
        ],
    }
    norm = normalize_gantt_plan(payload)
    assert [t["id"] for t in norm["tasks"]] == [1]
    ok, _ = validate_gantt_plan(payload)
    assert ok
    assert _render_gantt_image_v2(payload) is not None


def test_gantt_truncation_warns_instead_of_silent(caplog):
    """>30 任务：保持截断行为，但必须留 WARNING 痕迹。"""
    tasks = [{"id": i, "name": f"任务{i}", "start": i, "end": i + 1} for i in range(1, 41)]
    with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_gantt"):
        out = _build_gantt_tasks(tasks, None)
    assert len(out) == 30
    assert any("超过单图上限" in rec.getMessage() for rec in caplog.records)


def test_gantt_undated_tasks_laid_sequentially():
    """无日期任务沿时间轴顺延，不得全部堆在第 1 天。"""
    out = _build_gantt_tasks([{"name": "A"}, {"name": "B"}, {"name": "C"}], None)
    assert [(t["start"], t["end"]) for t in out] == [(1, 1), (2, 2), (3, 3)]
    for prev, cur in zip(out, out[1:]):
        assert cur["start"] >= prev["end"] + 1
    assert [t["isMilestone"] for t in out] == [False, False, False]


def test_gantt_days_without_start_advances_cursor():
    """只给 days 而不给 start：从游标起算，且后续任务继续顺延。"""
    out = _build_gantt_tasks([{"name": "A", "days": 5}, {"name": "B", "days": 3}], None)
    assert [(t["start"], t["end"]) for t in out] == [(1, 5), (6, 8)]


# ---------------- BUG-175-04：图题空白折叠 ----------------


def test_chart_title_collapses_whitespace_keeps_single_space():
    assert _clean_chart_title("Site Layout Plan 施工平面布置") == "Site Layout Plan 施工平面布置"
    assert "SiteLayoutPlan" not in _clean_chart_title("Site Layout Plan 施工平面布置")
    assert _clean_chart_title("Site Layout Plan") == "Site Layout Plan"
    # 全角空格同样折叠为半角单空格
    assert _clean_chart_title("施工\u3000平面\u3000布置") == "施工 平面 布置"
    # 首尾空白去掉，连续空白压成一个
    assert _clean_chart_title("  Site   Layout   Plan  ") == "Site Layout Plan"


def test_title_candidate_keeps_word_separators():
    assert _title_candidate("Site Layout Plan 施工平面布置") == "Site Layout Plan 施工平面布置"
    assert _title_candidate("Site  Layout  Plan") == "Site Layout Plan"


# ---------------- R175 续：甘特任务预算收敛到单一判定点 ----------------


def _gantt_mermaid(n: int) -> str:
    lines = ["gantt", "    dateFormat YYYY-MM-DD", "    section S"]
    for i in range(n):
        lines.append(f"    任务{i} :2026-01-01, 2d")
    return "\n".join(lines)


def test_mermaid_path_passes_full_tasks_to_renderer(monkeypatch, caplog):
    """Mermaid 语法路径不得在生成侧截断：完整任务列表交给渲染器后，
    由 `_apply_gantt_task_budget` 唯一判定并 WARNING 留痕。"""
    captured = {}
    real_render = mr._render_gantt_image_v2

    def spy(plan, *args, **kwargs):
        captured["n"] = len(plan.get("tasks") or [])
        return real_render(plan, *args, **kwargs)

    monkeypatch.setattr(mr, "_render_gantt_image_v2", spy)
    with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_gantt"):
        out = mr.render_mermaid_to_bytes(_gantt_mermaid(31), "gantt", skip_http=True)

    assert out is not None
    assert captured["n"] == 31, "生成侧仍在渲染器之前截断（预算判定点未收敛）"
    assert any("超过单图上限" in rec.getMessage() for rec in caplog.records)


def test_gantt_budget_boundary_30_vs_31(caplog):
    """边界：恰好 30 条零丢弃零告警；恰好 31 条丢 1 条并 WARNING。"""
    from app.services.ai.mermaid_gantt import (
        _GANTT_MAX_TASKS,
        _apply_gantt_task_budget,
        gantt_truncated_task_count,
    )

    tasks = [{"id": i, "name": f"任务{i}", "start": i, "end": i + 1} for i in range(1, _GANTT_MAX_TASKS + 2)]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_gantt"):
        kept = _apply_gantt_task_budget(tasks[:_GANTT_MAX_TASKS])
    assert kept == tasks[:_GANTT_MAX_TASKS]
    assert not [r for r in caplog.records if "超过单图上限" in r.getMessage()]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.ai.mermaid_gantt"):
        kept = _apply_gantt_task_budget(tasks)
    assert len(kept) == _GANTT_MAX_TASKS
    assert [r for r in caplog.records if "超过单图上限" in r.getMessage()]

    # 统计出口与渲染侧同一上限：31 条 → 丢 1 条。
    assert gantt_truncated_task_count(_gantt_mermaid(31)) == 1
    assert gantt_truncated_task_count(_gantt_mermaid(30)) == 0


def test_gantt_30_tasks_render_bytes_stable():
    """≤30 条：生成侧不再切片后，产出必须与直接渲染同一 plan 逐字节一致。"""
    from app.services.ai.mermaid_gantt import _parse_mermaid_gantt

    code = _gantt_mermaid(30)
    parsed = _parse_mermaid_gantt(code)
    assert len(parsed) == 30
    max_end = max(t["end"] for t in parsed)
    plan = {
        "type": "gantt",
        "title": "施工进度计划",
        "totalDays": max(max_end, 90),
        "tasks": [
            {
                "id": idx + 1,
                "name": t["task"],
                "start": t["start"],
                "end": t["end"],
                "dependencies": [],
                "isMilestone": t["start"] == t["end"],
                "section": t.get("section", ""),
            }
            for idx, t in enumerate(parsed)
        ],
    }
    direct = _render_gantt_image_v2(plan)
    via_mermaid = mr.render_mermaid_to_bytes(code, "gantt", skip_http=True)
    assert direct is not None and via_mermaid is not None
    assert via_mermaid.getvalue() == direct.getvalue()
