# -*- coding: utf-8 -*-
"""全局事实「部分失败」终态降级（R180 · 2026-11-05）。

现场证据（运行库 task_registry / facts_chunk_failures）：
    23 段中 16 段成功、7 段模型声明「未提取到事实」，任务仍以
    ``completed`` + 「全局事实提取完成」落库 —— 用户无法区分「全部成功」
    与「部分降级」，也不会去重跑失败段。

不变量（本文件锁定）：
  1. 全部段成功 → ``completed``；任一未成功段（真实失败或模型声明未提取）
     → ``degraded``，且 message 必须带段数与涉及章节（可读原因）；
  2. 增量 ``skipped`` 属正常行为，不得触发 degraded；
  3. degraded 是终态：不会被 ``WHERE status IN ('running','paused')`` 的
     僵尸/中断清理误伤，也不会被晚到进度覆盖；
  4. SSE 事件名与任务状态同源（completed / degraded），checkpoint 同步。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.routers import sse_handlers as sh
from app.services.ai import task_registry as tr

REPO_ROOT = Path(__file__).resolve().parents[2]
SSE_HANDLERS = REPO_ROOT / "backend" / "app" / "routers" / "sse_handlers.py"


def _result(**seg_kw):
    base = {"total": 23, "ok": 16, "skipped": 0, "failed": 0, "declared_failed": 0}
    base.update(seg_kw)
    return SimpleNamespace(segment_stats=base)


# ===========================================================================
# A 组 · 判定与文案（纯函数）
# ===========================================================================


def test_all_segments_ok_stays_completed():
    status, event, msg = sh._facts_partial_terminal_payload(_result())
    assert (status, event) == ("completed", "completed")
    assert msg == "全局事实提取完成"


def test_declared_failed_segments_degrade_with_readable_reason():
    """现场形态：failed=0 但 declared_failed=7 —— 旧实现会误报 completed。"""
    r = _result(
        ok=16,
        declared_failed=7,
        declared_failed_details=[
            {"index": 3, "heading": "施工条件与环境特征"},
            {"index": 5, "heading": "施工部署"},
            {"index": 9, "heading": "技术措施"},
            {"index": 11, "heading": "质量保证"},
        ],
    )
    status, event, msg = sh._facts_partial_terminal_payload(r)
    assert status == "degraded" and event == "degraded"
    assert "7 段未提取到事实" in msg
    assert "7/23" in msg
    assert "结果可能不完整" in msg
    # 涉及项：最多 3 个章节，可读定位
    assert "施工条件与环境特征" in msg and "施工部署" in msg and "技术措施" in msg
    assert "质量保证" not in msg, "涉及章节只取前 3 个，避免 message 无限膨胀"


def test_real_failures_are_counted_separately():
    r = _result(
        ok=15,
        failed=2,
        declared_failed=1,
        failed_details=[{"index": 1, "heading": "工程概况", "reason": "HTTP 429"}],
        declared_failed_details=[{"index": 2, "heading": "编制依据"}],
    )
    status, event, msg = sh._facts_partial_terminal_payload(r)
    assert status == "degraded" and event == "degraded"
    assert "2 段提取失败" in msg and "1 段未提取到事实" in msg
    assert "3/23" in msg
    assert "工程概况" in msg and "编制依据" in msg


def test_incremental_skipped_is_not_degraded():
    r = _result(ok=0, skipped=23, total=23)
    status, event, msg = sh._facts_partial_terminal_payload(r)
    assert (status, event) == ("completed", "completed")
    assert "跳过 23 段已提取" in msg


# ===========================================================================
# B 组 · 终态兼容性（DB / 清理路径不得误伤 degraded）
# ===========================================================================


@pytest.mark.asyncio
async def test_degraded_persists_and_is_not_treated_as_interrupted(db_conn):
    tid = await tr.register_task("facts_generation", scheme_id="s1")
    msg = "全局事实提取降级完成（7 段未提取到事实，共 7/23 段未成功；结果可能不完整）"
    await tr.finish_task(tid, status="degraded", message=msg)

    cur = await db_conn.execute("SELECT status, message FROM task_registry WHERE id=?", (tid,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert row["status"] == "degraded"
    assert row["message"] == msg

    # degraded 是终态：僵尸任务回收（仅 running/paused）不得把它改写成 stopped
    interrupted = await tr.get_interrupted_tasks()
    assert [r["id"] for r in interrupted] == []
    assert await tr.reap_orphan_tasks(max_stale_seconds=0.0) == 0


@pytest.mark.asyncio
async def test_late_progress_cannot_overwrite_degraded_terminal(db_conn):
    tid = await tr.register_task("facts_generation", scheme_id="s1")
    await tr.update_progress(tid, 0.9, "收尾中")
    await tr.finish_task(tid, status="degraded", message="降级完成：7/23 段未成功")
    await tr.update_progress(tid, 1.0, "全局事实提取完成", force=True)

    cur = await db_conn.execute("SELECT status, message FROM task_registry WHERE id=?", (tid,))
    row = await cur.fetchone()
    await cur.close()
    assert row["status"] == "degraded", "晚到进度不得把 degraded 改回 completed/running"
    assert row["message"] == "降级完成：7/23 段未成功"


def test_degraded_is_registered_as_terminal_status():
    assert "degraded" in sh._TERMINAL_STATUSES


# ===========================================================================
# C 组 · 结构护栏：收尾点必须使用统一判定（防再次硬编码 completed）
# ===========================================================================


def test_facts_closeout_uses_shared_terminal_decision():
    src = SSE_HANDLERS.read_text(encoding="utf-8")
    closeout = src[src.index("extraction_result = await pipe_task") :]
    assert "_facts_partial_terminal_payload(" in closeout
    assert (
        "await finish_task(task_id, _terminal_status, done_msg)" in closeout
    ), "事实收尾必须按判定结果落终态（硬编码 completed 会让部分失败再次伪装成功）"
    assert 'yield _sse({"event": _terminal_event' in closeout, "SSE 事件名必须与任务状态同源"
    assert '"event": _terminal_event,' in closeout, "checkpoint 的 event 必须与终态同源"
