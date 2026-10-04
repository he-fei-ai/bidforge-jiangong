# -*- coding: utf-8 -*-
"""后台任务活动快照 P0 修复回归（2026-09-30，第九轮）

BUG（P0 ·「AI 明明在跑却说后台空闲」）：`routers/system.py::_build_activity_snapshot`
把 `ORDER BY t.created_at DESC LIMIT ?`（**先截断**）与
`running = [t for t in tasks if status in ('running','paused')]`（**后过滤**）
写成了两个独立步骤。

  system.py:76   ... ORDER BY t.created_at DESC LIMIT ?
  system.py:109  running = [t for t in tasks if t.get("status") in ("running","paused")]

`created_at DESC` 把**最新创建**的任务排在最前，而**正在运行的长任务恰恰是
最早创建的那批**（正文生成 12 章常跑十几分钟）。用户在此期间每完成一章、
切一次 Tab、重跑一次其它任务，都会插入更"新"的 task_registry 行 → 长任务
被挤出这 8 条 → `running` 变成空列表。

后果是**自相矛盾**的用户可见故障：同一份快照里 `ai.in_flight` 来自
`get_ai_live_stats()`（全局内存态，不受 LIMIT 影响）> 0，前端
`TaskStatusBar.tsx` 却按 `running.length === 0` 渲染「**后台空闲**」。
即：AI 调用确实在进行中，界面却告诉用户没有任何后台任务。

与 AGENTS.md 记录的根因模式一致：**「先截断后过滤」把两个本该有依赖关系的
判据拆开了**。正确的不变量是「只要存在运行中任务，running 必非空」——
它与 limit 无关，limit 只应约束"历史终态任务"展示多少条。

修复：把 running 独立成一条**不受 limit 约束**（上限给足）的查询，
终态历史仍按 created_at DESC LIMIT。行为对既有场景逐字节一致
（running 数量本就 < limit），仅在"长任务被挤出"时修正。

护栏：
  A. 行为：造 1 个 running 老任务 + N(>limit) 个更新的 completed 任务，
     断言 running 非空且含该任务；
  B. 反向：limit 仍然生效（completed 历史条数不因修复而膨胀）；
  C. 无 running 时 running 仍为空（不误报）。
"""
import time
import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.routers.system import _build_activity_snapshot


@pytest.fixture
async def db(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "activity-p0.sqlite"
    await init_db()
    conn = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "p"))
    await conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "s"))
    await conn.commit()
    yield conn, pid, sid
    await conn.close()


def _ts(base: str, offset_sec: int) -> str:
    """base（'YYYY-MM-DD HH:MM:SS'）偏移 offset_sec 秒后的同格式时间戳。"""
    import datetime as _dt
    t = _dt.datetime.strptime(base, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(seconds=offset_sec)
    return t.strftime("%Y-%m-%d %H:%M:%S")


async def _mk_task(conn, sid, *, status, created_at, task_type="content_draft"):
    tid = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO task_registry (id, task_type, status, progress, message, "
        "scheme_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (tid, task_type, status, 0.5, "生成中", sid,
         created_at, created_at))
    return tid


class TestRunningNotEvictedByLimit:
    """A. 核心回归：长任务不得被更新的终态任务挤出 running"""

    @pytest.mark.asyncio
    async def test_long_running_task_survives_newer_completed(self, db):
        conn, pid, sid = db
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        # 最早创建、仍在 running 的长任务
        long_id = await _mk_task(conn, sid, status="running", created_at=base)
        # 之后完成的 12 个终态任务（正文生成逐章完成会持续插入）
        for i in range(12):
            await _mk_task(conn, sid, status="completed",
                           created_at=_ts(base, i + 1))
        await conn.commit()

        snap = await _build_activity_snapshot(limit=8)
        running_ids = {t["id"] for t in snap["tasks"]["running"]}
        assert long_id in running_ids, (
            "运行中的长任务被 updated 任务挤出 LIMIT —— 前端会显示「后台空闲」，"
            "而 AI 实际仍在调用中")

    @pytest.mark.asyncio
    async def test_paused_also_survives(self, db):
        """paused 与 running 同属「进行中」，不得被挤出"""
        conn, pid, sid = db
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        pid_task = await _mk_task(conn, sid, status="paused", created_at=base)
        for i in range(12):
            await _mk_task(conn, sid, status="completed",
                           created_at=_ts(base, i + 1))
        await conn.commit()

        snap = await _build_activity_snapshot(limit=8)
        assert pid_task in {t["id"] for t in snap["tasks"]["running"]}


class TestLimitStillApplies:
    """B. 反向：修复不得让历史终态任务条数膨胀"""

    @pytest.mark.asyncio
    async def test_completed_history_respects_limit(self, db):
        conn, pid, sid = db
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        for i in range(20):
            await _mk_task(conn, sid, status="completed",
                           created_at=_ts(base, i + 1))
        await conn.commit()

        snap = await _build_activity_snapshot(limit=8)
        assert len(snap["tasks"]["recent"]) == 8, "历史任务条数必须仍受 limit 约束"

    @pytest.mark.asyncio
    async def test_no_running_yields_empty_running(self, db):
        """C. 反向：无进行中任务时不得误报 running"""
        conn, pid, sid = db
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        await _mk_task(conn, sid, status="completed", created_at=base)
        await conn.commit()

        snap = await _build_activity_snapshot(limit=8)
        assert snap["tasks"]["running"] == []


class TestSnapshotShapeUnchanged:
    """向后兼容：响应结构与既有字段不变"""

    @pytest.mark.asyncio
    async def test_snapshot_keys(self, db):
        conn, pid, sid = db
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        await _mk_task(conn, sid, status="running", created_at=base)
        await conn.commit()

        snap = await _build_activity_snapshot(limit=8)
        for k in ("server", "ai", "tasks"):
            assert k in snap, f"响应缺少既有字段: {k}"
        for k in ("running", "recent"):
            assert k in snap["tasks"], f"tasks 缺少既有子字段: {k}"
        t = snap["tasks"]["recent"][0]
        for k in ("id", "task_type", "status", "progress", "message", "live"):
            assert k in t, f"任务条目缺少既有字段: {k}"
