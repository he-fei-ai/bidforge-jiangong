"""task_registry.py 单元测试

覆盖 register_task / update_progress / finish_task

测试策略：
- 直接调用函数，验证 DB 持久化 + 内存状态 + SSE 广播
- monkeypatch ``broadcast`` 捕获实际推送载荷（2026-10-04 起 broadcast 已降级为
  no-op，生产代码不再依赖 ``_subscribers`` 队列；旧测试里的
  ``_subscribers[tid] = [q]`` 直接改队列的写法已一并去除）
- 验证 finish_task 清理内存态 ``_tasks``
"""
import asyncio
import json

import app.services.ai.task_registry as tr
import pytest
from app.services.ai.task_registry import (
    _tasks,
    broadcast,
    finish_task,
    is_stopped,
    register_task,
    request_control,
    set_task_status,
    update_progress,
    wait_resume,
)


# ============================================================
# register_task 测试
# ============================================================
class TestRegisterTask:
    """register_task：创建任务，持久化 + 内存状态"""

    async def test_returns_uuid(self, db_conn):
        tid = await register_task("generate_outline", scheme_id="s1")
        assert isinstance(tid, str)
        assert len(tid) == 36  # UUID4 格式

    async def test_persists_to_db(self, db_conn):
        """任务写入 task_registry 表"""
        tid = await register_task("generate_content",
                                  project_id="p1", scheme_id="s1")

        cur = await db_conn.execute("SELECT * FROM task_registry WHERE id=?", (tid,))
        row = await cur.fetchone()
        assert row is not None
        d = dict(row)
        assert d["task_type"] == "generate_content"
        assert d["project_id"] == "p1"
        assert d["scheme_id"] == "s1"
        assert d["status"] == "running"
        assert d["progress"] == 0.0

    async def test_checkpoint_json_stored(self, db_conn):
        """checkpoint 序列化为 JSON 存储"""
        cp = {"last_section": "sec1", "index": 5}
        tid = await register_task("generate_content", checkpoint=cp)

        cur = await db_conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?",
                                    (tid,))
        row = await cur.fetchone()
        stored = json.loads(row[0])
        assert stored == cp

    async def test_empty_checkpoint_default(self, db_conn):
        """无 checkpoint → 存储 {}"""
        tid = await register_task("generate_content")

        cur = await db_conn.execute("SELECT checkpoint_json FROM task_registry WHERE id=?",
                                    (tid,))
        row = await cur.fetchone()
        assert json.loads(row[0]) == {}

    async def test_memory_state_created(self, db_conn):
        """内存 _tasks 字典创建运行时状态"""
        tid = await register_task("generate_outline", scheme_id="s1")

        assert tid in _tasks
        state = _tasks[tid]
        assert state["type"] == "generate_outline"
        assert state["status"] == "running"
        assert state["progress"] == 0.0
        assert state["scheme_id"] == "s1"
        assert "pause_event" in state
        assert "stop_event" in state
        assert "child_tasks" in state

    async def test_pause_event_default_set(self, db_conn):
        """pause_event 默认 set（运行中，未暂停）"""
        tid = await register_task("generate_outline")
        assert _tasks[tid]["pause_event"].is_set() is True

    async def test_stop_event_default_not_set(self, db_conn):
        """stop_event 默认未 set"""
        tid = await register_task("generate_outline")
        assert _tasks[tid]["stop_event"].is_set() is False


# ============================================================
# update_progress 测试
# ============================================================
class TestUpdateProgress:
    """update_progress：更新进度 + 广播事件"""

    async def test_updates_db_progress(self, db_conn):
        tid = await register_task("generate_content", scheme_id="s1")

        await update_progress(tid, 0.5, message="处理中")

        cur = await db_conn.execute("SELECT progress, message FROM task_registry WHERE id=?",
                                    (tid,))
        row = await cur.fetchone()
        assert row[0] == 0.5
        assert row[1] == "处理中"

    async def test_updates_memory_progress(self, db_conn):
        tid = await register_task("generate_content")
        await update_progress(tid, 0.75)
        assert _tasks[tid]["progress"] == 0.75

    async def test_broadcasts_progress_event(self, db_conn, monkeypatch):
        """进度更新时 broadcast 收到 progress 事件载荷"""
        captured = []

        async def _fake_broadcast(tid, payload):
            captured.append((tid, payload))

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        tid = await register_task("generate_content")

        await update_progress(tid, 0.3, message="进度更新")

        assert len(captured) == 1
        event_tid, event = captured[0]
        assert event_tid == tid
        assert event["event"] == "progress"
        assert event["task_id"] == tid
        assert event["progress"] == 0.3
        assert event["message"] == "进度更新"

    async def test_custom_event_name(self, db_conn, monkeypatch):
        """自定义 event 名称"""
        captured = []

        async def _fake_broadcast(tid, payload):
            captured.append(payload)

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        tid = await register_task("generate_content")

        await update_progress(tid, 0.5, event="section_done")

        assert len(captured) == 1
        assert captured[0]["event"] == "section_done"

    async def test_no_subscriber_no_error(self, db_conn):
        """无订阅者时不报错（broadcast 现为 no-op）"""
        tid = await register_task("generate_content")
        # 生产代码调用 broadcast 时不再依赖任何订阅队列
        await update_progress(tid, 0.5)
        # 无异常即通过


# ============================================================
# finish_task 测试
# ============================================================
class TestFinishTask:
    """finish_task：完成 + 清理"""

    async def test_updates_db_status(self, db_conn):
        tid = await register_task("generate_content")

        await finish_task(tid, status="completed", message="全部完成")

        cur = await db_conn.execute("SELECT status, message FROM task_registry WHERE id=?",
                                    (tid,))
        row = await cur.fetchone()
        assert row[0] == "completed"
        assert row[1] == "全部完成"

    async def test_default_status_completed(self, db_conn):
        tid = await register_task("generate_content")
        await finish_task(tid)
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())[0] == "completed"

    async def test_broadcasts_completed_event(self, db_conn, monkeypatch):
        """完成时广播 completed 事件"""
        captured = []

        async def _fake_broadcast(tid, payload):
            captured.append((tid, payload))

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        tid = await register_task("generate_content")

        await finish_task(tid, status="completed", message="done")

        assert len(captured) == 1
        event_tid, event = captured[0]
        assert event_tid == tid
        assert event["event"] == "completed"
        assert event["task_id"] == tid
        assert event["message"] == "done"

    async def test_failed_status_broadcasts_failed_event(self, db_conn, monkeypatch):
        """status=failed → 广播 failed 事件"""
        captured = []

        async def _fake_broadcast(tid, payload):
            captured.append(payload)

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        tid = await register_task("generate_content")

        await finish_task(tid, status="failed", message="出错了")

        assert len(captured) == 1
        assert captured[0]["event"] == "failed"

    async def test_stopped_status_broadcasts_stopped_event(self, db_conn, monkeypatch):
        """status=stopped → 广播 stopped 事件"""
        captured = []

        async def _fake_broadcast(tid, payload):
            captured.append(payload)

        monkeypatch.setattr(tr, "broadcast", _fake_broadcast)
        tid = await register_task("generate_content")

        await finish_task(tid, status="stopped")

        assert len(captured) == 1
        assert captured[0]["event"] == "stopped"

    async def test_cleans_up_memory_state(self, db_conn):
        """完成后清理 _tasks 内存态"""
        tid = await register_task("generate_content")

        await finish_task(tid)

        assert tid not in _tasks

    async def test_memory_status_updated_before_cleanup(self, db_conn):
        """finish 前内存 status 被更新（在清理之前）"""
        tid = await register_task("generate_content")
        # finish_task 内部先更新 _tasks[tid]["status"]，再 pop
        # 验证：finish 后 _tasks 已被清理，但 DB 中 status 正确
        await finish_task(tid, status="completed")
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())[0] == "completed"


# ============================================================
# set_task_status 测试
# ============================================================
class TestSetTaskStatus:
    """set_task_status：仅更新状态，不清理"""

    async def test_updates_db(self, db_conn):
        tid = await register_task("generate_content")
        await set_task_status(tid, "paused")

        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())[0] == "paused"

    async def test_updates_memory(self, db_conn):
        tid = await register_task("generate_content")
        await set_task_status(tid, "paused")
        assert _tasks[tid]["status"] == "paused"

    async def test_does_not_clean_up(self, db_conn):
        """set_task_status 不清理内存（区别于 finish_task）"""
        tid = await register_task("generate_content")
        await set_task_status(tid, "paused")
        assert tid in _tasks


# ============================================================
# 控制功能测试
# ============================================================
class TestTaskControl:
    """request_control / is_stopped / wait_resume"""

    async def test_is_stopped_false_when_running(self, db_conn):
        tid = await register_task("generate_content")
        assert is_stopped(tid) is False

    async def test_is_stopped_true_after_stop(self, db_conn):
        tid = await register_task("generate_content")
        request_control(tid, "stop")
        assert is_stopped(tid) is True

    async def test_is_stopped_true_for_unknown_task(self, db_conn):
        """不存在的任务视为已停止"""
        assert is_stopped("nonexistent") is True

    async def test_pause_then_resume(self, db_conn):
        """暂停后 wait_resume 挂起，恢复后返回"""
        tid = await register_task("generate_content")
        request_control(tid, "pause")
        assert _tasks[tid]["pause_event"].is_set() is False

        # 在后台恢复
        async def _resume_after_delay():
            await asyncio.sleep(0.05)
            request_control(tid, "resume")

        asyncio.create_task(_resume_after_delay())
        await wait_resume(tid)  # 应挂起直到 resume
        assert _tasks[tid]["pause_event"].is_set() is True

    async def test_request_control_unknown_task(self, db_conn):
        """不存在的任务返回 False"""
        assert request_control("nonexistent", "pause") is False

    async def test_request_control_records_last_action(self, db_conn):
        """last_control 被记录"""
        tid = await register_task("generate_content")
        request_control(tid, "pause")
        assert _tasks[tid]["last_control"] == "pause"
        request_control(tid, "resume")
        assert _tasks[tid]["last_control"] == "resume"


# ============================================================
# broadcast 测试（2026-10-04 起：broadcast 已降级为 no-op）
# ============================================================
class TestBroadcast:
    """broadcast 现为空操作；仅验证存在性、可调用性与向后兼容的签名。

    旧测试里 `_subscribers[tid] = [q]` + `q.get_nowait()` 的语义已随
    死链路清理一并去除（生产代码从未注册过 Queue，行为等价于空转）。
    """

    async def test_broadcast_is_noop(self):
        tid = "test-task-id"
        # 无异常即通过；no-op 不产生副作用
        result = await broadcast(tid, {"event": "test", "data": 42})
        assert result is None

    async def test_broadcast_no_subscribers(self):
        """无订阅者时不报错（历史行为保留）"""
        await broadcast("no-subs", {"event": "test"})
        # 无异常即通过


# ============================================================
# 辅助功能测试（提高覆盖率）
# ============================================================
class TestTaskRegistryAuxiliary:
    """register_child_task / wait_resume 边缘 / get_interrupted_tasks"""

    async def test_wait_resume_unknown_task_returns_none(self, db_conn):
        """wait_resume 对不存在的任务直接返回（不阻塞）"""
        await wait_resume("nonexistent")  # 不报错、不阻塞

    async def test_register_child_task(self, db_conn):
        """register_child_task 将子任务加入 child_tasks 集合"""
        tid = await register_task("generate_content")

        async def _child():
            await asyncio.sleep(0.01)

        child = asyncio.create_task(_child())
        tr.register_child_task(tid, child)

        assert child in _tasks[tid]["child_tasks"]
        await child
        # 完成后自动移除
        assert child not in _tasks[tid]["child_tasks"]

    async def test_stop_cancels_child_tasks(self, db_conn):
        """stop 时取消所有未完成的子任务"""
        tid = await register_task("generate_content")

        async def _long_child():
            await asyncio.sleep(10)

        child = asyncio.create_task(_long_child())
        tr.register_child_task(tid, child)

        request_control(tid, "stop")
        await asyncio.sleep(0.05)  # 让取消生效

        assert child.cancelled() or child.done()

    async def test_get_interrupted_tasks(self, db_conn):
        """get_interrupted_tasks 返回 running/paused 状态的任务"""
        tid1 = await register_task("generate_content")
        tid2 = await register_task("generate_outline")
        await set_task_status(tid2, "paused")
        # completed 的不应出现
        tid3 = await register_task("generate_chart")
        await finish_task(tid3, status="completed")

        interrupted = await tr.get_interrupted_tasks()
        ids = {t["id"] for t in interrupted}
        assert tid1 in ids
        assert tid2 in ids
        assert tid3 not in ids

    async def test_get_interrupted_tasks_empty(self, db_conn):
        """无中断任务时返回空列表"""
        result = await tr.get_interrupted_tasks()
        assert result == []