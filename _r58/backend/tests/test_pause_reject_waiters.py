# -*- coding: utf-8 -*-
"""暂停路径接入 reject_waiters 的回归测试（2026-09-23 · Fix C 接入）。

背景：
  之前 request_control(action="pause") 只 clear 了 pause_event，正在
  concurrency_controller.semaphore 上排队的 AI 请求协程不会被立即打断——它们
  会继续排队，直到已有在飞请求释放许可才被放行，然后才在生成循环顶部的
  wait_resume 挂起。表现为暂停响应"秒级"。

  Fix C 把 ResizableSemaphore 的 reject_waiters 能力接入 pause 路径：pause 时
  同步调用它把排队协程全部唤醒并抛 CancelledError（"acquire rejected"），
  暂停响应降到"微秒级"。本测试锁定该不变量。

不变量：
  1. pause 有排队等待者时：等待者立即收到 CancelledError（不等许可释放）；
  2. pause 无排队等待者时：无副作用（不破坏任何状态）；
  3. pause 对已经在飞的协程无影响（它们本轮结束后自然进入 wait_resume 挂起）；
  4. 被拒绝的协程不"消耗"许可（sem._value 不减少），记账一致；
  5. resume / stop 不触发 reject_waiters（该行为只属于 pause）。
"""
import asyncio
from types import SimpleNamespace

from app.services.ai import task_registry as tr
from app.services.ai.task_registry import register_task, request_control
from app.services.ai.workflows_base import ResizableSemaphore


# ============================================================
# 1. ResizableSemaphore.reject_waiters 自身语义
# ============================================================
class TestResizableSemaphoreRejectWaiters:
    async def test_reject_wakes_waiters_with_cancelled_error(self):
        """核心回归：有排队的等待者立即被唤醒并抛 CancelledError。"""
        sem = ResizableSemaphore(1)
        states: list = []

        async def worker():
            try:
                async with sem:
                    await asyncio.sleep(0.2)
                    states.append("acquired")
            except asyncio.CancelledError as e:
                states.append(f"cancelled: {e}")

        workers = [asyncio.create_task(worker()) for _ in range(4)]
        await asyncio.sleep(0.05)
        assert sem._held == 1, f"预期 1 个持锁，实际 {sem._held}"
        assert len(sem._waiters) == 3, f"预期 3 个排队，实际 {len(sem._waiters)}"

        rejected = sem.reject_waiters()
        assert rejected == 3, f"应拒绝 3 个等待者，实测 {rejected}"

        await asyncio.gather(*workers, return_exceptions=True)
        assert states.count("cancelled: Semaphore acquire rejected (scope paused)") == 3
        assert states.count("acquired") == 1
        # 关键：拒绝未"消耗"许可，_value 应恢复
        assert sem._value == 1

    async def test_reject_empty_queue_is_noop(self):
        """无排队时 reject_waiters 返回 0，不抛异常。"""
        sem = ResizableSemaphore(2)
        assert sem.reject_waiters() == 0

    async def test_reject_does_not_cancel_held(self):
        """正在持有许可的协程不受影响。"""
        sem = ResizableSemaphore(1)
        inside = asyncio.Event()

        async def holder():
            async with sem:
                inside.set()
                await asyncio.sleep(0.1)

        t = asyncio.create_task(holder())
        await inside.wait()
        sem.reject_waiters()  # 应无副作用
        await t
        assert t.exception() is None

    async def test_reject_waiters_only_rejects_requested_owner(self):
        """暂停任务 A 不得拒绝正在等待全局许可的任务 B。"""
        sem = ResizableSemaphore(1)
        entered: list[str] = []
        release = asyncio.Event()

        async def holder():
            async with sem:
                entered.append("holder")
                await release.wait()

        async def waiter(name: str):
            ok = await sem.acquire(owner_id=name)
            if ok:
                sem.release()
                entered.append(name)
            else:
                entered.append(f"rejected-{name}")

        holder_task = asyncio.create_task(holder())
        await asyncio.sleep(0)
        task_a = asyncio.create_task(waiter("A"))
        task_b = asyncio.create_task(waiter("B"))
        await asyncio.sleep(0)

        assert sem.reject_waiters(owner_id="A") == 1
        await asyncio.sleep(0)
        assert "rejected-A" in entered
        assert "rejected-B" not in entered

        release.set()
        await asyncio.gather(holder_task, task_a, task_b, return_exceptions=True)
        assert "B" in entered


# ============================================================
# 2. request_control("pause") 必须调用 reject_waiters
# ============================================================
class TestPauseTriggersRejectWaiters:
    """所有测试通过替换 ``tr._cc`` 模块级符号为 SimpleNamespace 探针来观测调用。

    选 SimpleNamespace 而非 Mock：Mock 的调用记录是隐式的、依赖内部实现细节，
    探针对象显式记录调用序列，断言更直接、语义更清晰。
    """

    async def test_pause_calls_reject_waiters(self, db_conn, monkeypatch):
        """pause 路径必须同步调用 concurrency_controller.semaphore.reject_waiters。"""
        tid = await register_task("content_generation")
        call_log = []

        class _ProbeSem:
            def reject_waiters(self, owner_id=None):
                call_log.append(("reject", owner_id))
                return 5

        fake_cc = SimpleNamespace(semaphore=_ProbeSem())
        monkeypatch.setattr(tr, "_cc", fake_cc)

        request_control(tid, "pause")
        assert call_log == [("reject", tid)], "pause 必须按 task_id 触发 reject_waiters"
        # 断言暂停账本仍然正确
        assert tr._tasks[tid]["paused_at"] is not None
        # 断言 pause_event 已 clear
        assert tr._tasks[tid]["pause_event"].is_set() is False

    async def test_pause_with_zero_waiters_is_clean(self, db_conn, monkeypatch):
        """无排队等待者时 pause 依然正常（reject 返回 0 也无副作用）。"""
        tid = await register_task("content_generation")
        call_log = []

        class _ProbeSem:
            def reject_waiters(self, owner_id=None):
                call_log.append(("reject", owner_id))
                return 0

        monkeypatch.setattr(tr, "_cc", SimpleNamespace(semaphore=_ProbeSem()))
        ok = request_control(tid, "pause")
        assert ok is True
        assert call_log == [("reject", tid)]

    async def test_pause_with_reject_exception_is_tolerated(self, db_conn, monkeypatch):
        """reject_waiters 抛异常时不得中断 pause 主流程（防御性容错）。"""
        tid = await register_task("content_generation")

        class _BrokenSem:
            def reject_waiters(self, owner_id=None):
                raise RuntimeError("boom")

        monkeypatch.setattr(tr, "_cc", SimpleNamespace(semaphore=_BrokenSem()))
        # 关键：不应抛异常
        ok = request_control(tid, "pause")
        assert ok is True
        # pause 主流程应仍然生效
        assert tr._tasks[tid]["pause_event"].is_set() is False

    async def test_resume_does_not_call_reject_waiters(self, db_conn, monkeypatch):
        """resume 不触发 reject_waiters（该行为只属于 pause 语义）。"""
        tid = await register_task("content_generation")
        call_log = []

        class _ProbeSem:
            def reject_waiters(self):
                call_log.append("reject")
                return 0

        monkeypatch.setattr(tr, "_cc", SimpleNamespace(semaphore=_ProbeSem()))
        request_control(tid, "resume")
        assert call_log == []

    async def test_stop_does_not_call_reject_waiters(self, db_conn, monkeypatch):
        """stop 走的是 child_task.cancel() 路径，不通过 reject_waiters。"""
        tid = await register_task("content_generation")
        call_log = []

        class _ProbeSem:
            def reject_waiters(self):
                call_log.append("reject")
                return 0

        monkeypatch.setattr(tr, "_cc", SimpleNamespace(semaphore=_ProbeSem()))
        request_control(tid, "stop")
        assert call_log == []


# ============================================================
# 3. 端到端：真实并发结构下 pause 立刻打断排队协程
# ============================================================
class TestE2EPauseInterruptsQueued:
    async def test_pause_interrupts_queued_ai_callees(self, db_conn, monkeypatch):
        """核心 e2e：模拟正文生成的并发结构，pause 后排队章节应立刻放弃。

        流程：
          - 用真 ResizableSemaphore(1) 替换 concurrency_controller.semaphore
          - 启动 4 个 worker 模拟章节生成（每个都在 sem 内做 sleep 模拟 AI 调用）
          - 让第 1 个拿到许可并 sleep 0.05s，其余 3 个排队
          - 触发 pause：期望被拒绝的 3 个立即抛 CancelledError
        """
        real_sem = ResizableSemaphore(1)
        monkeypatch.setattr(tr, "_cc", SimpleNamespace(semaphore=real_sem))

        tid = await register_task("content_generation")
        states = []

        async def chapter(idx):
            try:
                async with real_sem:
                    states.append(f"start-{idx}")
                    await asyncio.sleep(0.05)
                    states.append(f"done-{idx}")
            except asyncio.CancelledError:
                states.append(f"rejected-{idx}")

        workers = [asyncio.create_task(chapter(i)) for i in range(4)]
        await asyncio.sleep(0.02)
        assert len([s for s in states if s.startswith("start-")]) == 1
        assert len(real_sem._waiters) == 3

        request_control(tid, "pause")
        await asyncio.gather(*workers, return_exceptions=True)

        rejected = [s for s in states if s.startswith("rejected-")]
        assert len(rejected) == 3, f"预期 3 个排队被拒绝，实际 {rejected}（states={states}）"
