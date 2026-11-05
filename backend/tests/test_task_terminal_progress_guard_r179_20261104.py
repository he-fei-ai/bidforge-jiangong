# -*- coding: utf-8 -*-
"""任务终态不可被「晚到的进度写」覆盖（R179 · 2026-11-04）。

事故证据（运行库 + logs/backend.log，task_registry 89981f59-c649-4b06-9fe9-000602f2d6cb）：
  - 15:40:50 客户端离开工作台 → SSE 生成器退出 → 兜底 finish_task 写 stopped 并 pop
    内存态（日志 15:40:54「SSE 生成器退出但任务仍在内存…兜底标记为 stopped」）；
  - 15:40:58 run_inner 在取消落地前跑完最后一项，调用 update_progress（此时
    `state is None`，命中 `need_write = force or state is None` 的免节流分支）；
  - 旧实现 `_update_task_progress_db` 的 UPDATE **没有状态守卫**，把已终态行的
    message 覆盖成「已完成 17/17」、progress 覆盖成 1.0；而
    `_write_task_terminal_db` 的 `WHERE status IN ('running','paused')` 又拒绝
    把 stopped 纠正为 completed；
  - DB 最终呈现 `status=stopped + message=已完成 17/17 + progress=1.0` 的自相矛盾终态
    （前端任务栏显示「已停止」，用户误判整轮提取要重跑）。

不变量（本文件锁定）：
  1. 终态行（completed/failed/stopped/degraded）是唯一事实源，任何晚到的
     `update_progress` 都不得改动其 status / progress / message；
  2. 守卫不得误伤进行中任务：running/paused 行仍必须正常接受进度落库。
"""

import pytest
from app.services.ai import task_registry as tr


async def _read_row(db_conn, tid):
    cur = await db_conn.execute("SELECT status, progress, message FROM task_registry WHERE id=?", (tid,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None, f"任务 {tid} 未落库"
    return row


async def test_late_progress_cannot_overwrite_stopped_terminal(db_conn):
    """断线兜底写入 stopped 后，晚到的「已完成 N/N」进度不得改写终态行。"""
    tid = await tr.register_task("bid_analysis", scheme_id="s1")
    await tr.update_progress(tid, 0.5, "进行中")

    await tr.finish_task(tid, status="stopped", message="客户端断开，任务已终止")
    assert tid not in tr._tasks, "finish_task 必须清理内存态（G12-5 不变量）"

    # 晚到收尾：state=None → force 分支（旧实现会无条件 UPDATE）
    await tr.update_progress(tid, 1.0, "已完成 17/17", force=True)

    row = await _read_row(db_conn, tid)
    assert row["status"] == "stopped", "终态 status 不得被进度写回滚/改写"
    assert row["message"] == "客户端断开，任务已终止", f"终态 message 被晚到进度覆盖成 {row['message']!r}"
    assert row["progress"] == pytest.approx(0.5), f"终态 progress 被晚到进度覆盖成 {row['progress']!r}"


async def test_late_progress_cannot_overwrite_completed_terminal(db_conn):
    """completed 行同理：晚到进度不得把 message 改成其它文案。"""
    tid = await tr.register_task("content_generation", scheme_id="s1")
    await tr.update_progress(tid, 0.9, "收尾中")
    await tr.finish_task(tid, status="completed", message="本次生成 15120 字")

    await tr.update_progress(tid, 0.5, "进度回退", force=True)

    row = await _read_row(db_conn, tid)
    assert row["status"] == "completed"
    assert row["message"] == "本次生成 15120 字"
    assert row["progress"] == pytest.approx(0.9)


async def test_running_task_still_accepts_progress_writes(db_conn):
    """守卫不得误伤：running 行必须正常接受进度落库。"""
    tid = await tr.register_task("facts_generation", scheme_id="s1")
    await tr.update_progress(tid, 0.25, "第 3/12 段")

    row = await _read_row(db_conn, tid)
    assert row["status"] == "running"
    assert row["message"] == "第 3/12 段"
    assert row["progress"] == pytest.approx(0.25)
