# -*- coding: utf-8 -*-
"""后台任务「暂停 / 恢复 / 停止」控制链路的不变量回归测试（2026-09-17）。

背景：左侧菜单栏「后台任务」窗口的三个按钮此前存在以下"看着能点、实际不算可用"的问题，
本次逐条锁定，防止回归：

  1. **暂停期间「已耗时」继续增长** —— 协作式暂停不打断已在飞的 AI 调用，用户侧唯一的
     "暂停已生效"证据就是耗时冻结。`get_task_elapsed_ms` 必须扣除暂停时段
     （活动快照 `tasks[].elapsed` 由它产出）。
  2. **暂停时死占全局并发许可** —— `guarded_gen` 的暂停闸门原先排在
     `async with concurrency_controller.semaphore` **之后**，暂停期间正在等待的章节会一直
     持有全局信号量，同进程内其它方案的生成任务被饿死。闸门必须排在信号量之前。
  3. **僵尸任务无法停止** —— 控制路由只认本进程内存 `_tasks`；DB 仍 running/paused 但内存
     态已丢失（多 worker / 异常退出残留）的任务，点停止恒返回"任务不存在"，
     任务栏留下清不掉的运行中行。
  4. **重复 pause/resume 打乱暂停账本** —— 幂等 + 累加语义。

设计约束（改这里之前先读）：
  - 暂停是**协作式**的，不在飞行中中断 HTTP 调用（已有章节内容会被丢弃）；
    只有 stop 才取消子任务。
  - `set_task_status` 只对 running/paused 生效（终态不可回写），必须返回是否真的迁移。
"""
import asyncio

import pytest

import app.routers.sse_handlers as sh
from app.services.ai import task_registry as tr
from app.services.ai.task_registry import (
    get_task_elapsed_ms, get_task_paused_ms, is_stopped, register_child_task,
    register_task, request_control, set_task_status, wait_resume,
)

_AI_SLEEP = 0.15


# ============================================================
# 1. 暂停时长的账本语义
# ============================================================
class TestPauseLedger:
    """暂停 / 恢复必须维护可累加、幂等的暂停时长账本。"""

    async def test_elapsed_excludes_pause_window(self, db_conn):
        """核心回归：暂停 0.4s 后，elapsed 仍在 0.15s 量级（而非 0.55s）。"""
        tid = await register_task("content_generation", scheme_id="s1")
        await asyncio.sleep(0.3)

        request_control(tid, "pause")
        await asyncio.sleep(0.4)
        elapsed_ms = get_task_elapsed_ms(tid)

        assert elapsed_ms is not None
        assert elapsed_ms < 400, f"暂停期间 elapsed 不得继续增长，实测 {elapsed_ms:.0f}ms"

    async def test_elapsed_resumes_counting_after_resume(self, db_conn):
        """恢复后 elapsed 继续增长（暂停时长被扣除，但不是"永久冻结"）。"""
        tid = await register_task("content_generation", scheme_id="s1")
        request_control(tid, "pause")
        await asyncio.sleep(0.3)
        request_control(tid, "resume")

        before = get_task_elapsed_ms(tid) or 0.0
        await asyncio.sleep(0.3)
        after = get_task_elapsed_ms(tid) or 0.0
        assert after - before > 200, f"恢复后 elapsed 应继续增长：{before:.0f} → {after:.0f}"

    async def test_repeated_pause_resume_accumulates(self, db_conn):
        """两段暂停**累加**计入账本；重复 pause / resume 幂等不清零。"""
        tid = await register_task("content_generation", scheme_id="s1")

        request_control(tid, "pause")
        await asyncio.sleep(0.2)
        request_control(tid, "resume")
        await asyncio.sleep(0.05)
        request_control(tid, "pause")
        await asyncio.sleep(0.2)

        paused_ms = get_task_paused_ms(tid)
        assert paused_ms >= 400, f"两段暂停应累加（≥400ms），实测 {paused_ms:.0f}ms"

        # 重复 pause 不得重置账本
        request_control(tid, "pause")
        request_control(tid, "pause")
        assert get_task_paused_ms(tid) >= paused_ms, "重复 pause 不得清零已累计时长"

    async def test_repeated_resume_is_idempotent(self, db_conn):
        """未在暂停中重复 resume：不抛异常、也不产生负的暂停时长。"""
        tid = await register_task("content_generation", scheme_id="s1")
        request_control(tid, "resume")
        request_control(tid, "resume")
        assert get_task_paused_ms(tid) == pytest.approx(0.0)
        request_control(tid, "pause")
        await asyncio.sleep(0.1)
        request_control(tid, "resume")
        request_control(tid, "resume")
        assert get_task_paused_ms(tid) >= 100

    async def test_unknown_task_returns_none_elapsed(self, db_conn):
        assert get_task_elapsed_ms("nonexistent") is None
        assert get_task_paused_ms("nonexistent") == 0.0


# ============================================================
# 2. 暂停闸门的位置（不得占用全局并发许可）
# ============================================================
class TestPauseGateBeforeSemaphore:
    """复刻 sse_handlers.guarded_gen 的并发结构，锁定「闸门在信号量之前」的语义。"""

    async def _worker(self, tid, idx, sem, log, *, gate_before_sem=True):
        """gate_before_sem=True → 修复后顺序；False → 旧实现顺序。"""
        if gate_before_sem:
            await wait_resume(tid)
            if is_stopped(tid):
                raise asyncio.CancelledError()
        async with sem:
            if not gate_before_sem:
                await wait_resume(tid)
                if is_stopped(tid):
                    raise asyncio.CancelledError()
            log.setdefault("running", {})[idx] = 1
            try:
                await asyncio.sleep(_AI_SLEEP)
            except asyncio.CancelledError:
                log.setdefault("cancelled", []).append(idx)
                raise
            log.setdefault("done", []).append(idx)
            log["running"].pop(idx, None)

    async def test_pause_does_not_hold_all_permits(self, db_conn):
        """暂停期间全局信号量必须**全部归还**，否则会饿死其它任务。"""
        tid = await register_task("content_generation", scheme_id="s1")
        sem = asyncio.Semaphore(2)
        log: dict = {"done": [], "cancelled": [], "running": {}}

        children = [asyncio.create_task(self._worker(tid, i, sem, log)) for i in range(6)]
        for t in children:
            register_child_task(tid, t)

        await asyncio.sleep(_AI_SLEEP * 1.5)
        request_control(tid, "pause")
        await asyncio.sleep(_AI_SLEEP * 3)

        assert sem._value == 2, (
            f"暂停期间必须释放全部并发许可（期望 2，实测 {sem._value}）——"
            "占着许可会让同进程内其它方案的生成任务被饿死")

        request_control(tid, "resume")
        await asyncio.sleep(_AI_SLEEP * 5)
        assert len(log["done"]) == 6, f"恢复后应跑完全部章节，实测 {len(log['done'])}"

        for t in children:
            if not t.done():
                t.cancel()
        await asyncio.gather(*children, return_exceptions=True)

    async def test_legacy_order_would_starve_semaphore(self, db_conn):
        """判别实验：旧顺序（闸门在信号量之后）确实会占满许可 —— 证明上一条有效。"""
        tid = await register_task("content_generation", scheme_id="s1")
        sem = asyncio.Semaphore(2)
        log: dict = {"done": [], "cancelled": [], "running": {}}

        children = [
            asyncio.create_task(self._worker(tid, i, sem, log, gate_before_sem=False))
            for i in range(6)]
        for t in children:
            register_child_task(tid, t)

        await asyncio.sleep(_AI_SLEEP * 1.5)
        request_control(tid, "pause")
        await asyncio.sleep(_AI_SLEEP * 2)
        assert sem._value == 0, "旧顺序应占满许可（判别实验失效说明结构已变化）"

        request_control(tid, "resume")
        for t in children:
            if not t.done():
                t.cancel()
        await asyncio.gather(*children, return_exceptions=True)

    def test_guarded_gen_checks_gate_before_work(self):
        """源码级守卫：guarded_gen 必须以「暂停闸门 → 停止检查」开头。

        ✅ 2026-09-19 二次同步：正文生成并发控制已落地**窗口调度器**（显式队列
        + 固定窗口，窗口大小 = 用户档位 effective_concurrency），guarded_gen
        不再持有任何 Semaphore（旧守卫断言的每任务 _run_semaphore 方案已被
        窗口调度器取代，原断言与实现矛盾导致误报）。守卫目标更新为：
        1) wait_resume / is_stopped 检查点必须排在业务工作（消费 gen_one）之前；
        2) 函数内不得出现任何信号量（全局或每任务）。
        """
        import inspect

        src = inspect.getsource(sh)
        start = src.index("async def guarded_gen")
        body = src[start:start + 1200]
        i_gate = body.index("wait_resume")
        i_work = body.index("gen_one(")
        assert i_gate < i_work, "暂停闸门必须排在消费 gen_one 之前"
        # 结构性断言（词法 "Semaphore" 会被 docstring 说明文字误伤）：
        # 函数体内不得出现任何 async with（信号量/锁），也不得引用旧闸门符号
        assert "async with" not in body, (
            "guarded_gen 不得持有任何异步锁/信号量 —— 并发闸门已由窗口调度器接管")
        assert "_run_semaphore" not in body, (
            "guarded_gen 不得引用已移除的每任务信号量 _run_semaphore")
        assert "concurrency_controller.semaphore" not in body, (
            "guarded_gen 不得再使用全局自适应信号量（会脱离用户档位）")

    async def test_stop_skips_unstarted_sections(self, db_conn):
        """停止后尚未开始（未抢到许可）的章节直接跳过，且不算失败。"""
        tid = await register_task("content_generation", scheme_id="s1")
        sem = asyncio.Semaphore(1)
        log: dict = {"done": [], "cancelled": [], "running": {}}

        children = [asyncio.create_task(self._worker(tid, i, sem, log)) for i in range(4)]
        for t in children:
            register_child_task(tid, t)

        await asyncio.sleep(_AI_SLEEP * 0.5)
        request_control(tid, "stop")
        await asyncio.sleep(_AI_SLEEP * 2)

        skipped = 4 - len(log["done"])
        assert skipped > 0, "停止后应有未开始的章节被跳过"
        for t in children:
            if not t.done():
                t.cancel()
        await asyncio.gather(*children, return_exceptions=True)


# ============================================================
# 3. set_task_status 返回值语义
# ============================================================
class TestSetTaskStatusReturn:
    """set_task_status 必须返回是否真的发生迁移，供控制路由区分"已停/本来就结束"。"""

    async def test_returns_true_on_transition(self, db_conn):
        tid = await register_task("content_generation")
        assert await set_task_status(tid, "paused") is True

    async def test_paused_to_stopped_transition(self, db_conn):
        """暂停态 → 停止态必须放行（否则暂停后无法停止）。"""
        tid = await register_task("content_generation")
        await set_task_status(tid, "paused")
        assert await set_task_status(tid, "stopped") is True

    async def test_returns_false_when_already_terminal(self, db_conn):
        tid = await register_task("content_generation")
        await tr.finish_task(tid, "completed")
        assert await set_task_status(tid, "stopped") is False


# ============================================================
# 4. 僵尸任务的可停止性（控制路由）
# ============================================================
class TestOrphanTaskControl:
    """DB 仍 running/paused 但本进程无内存态的任务：停止必须生效。"""

    async def test_stop_orphan_task_marks_stopped(self, db_conn, monkeypatch):
        """核心回归：僵尸任务点停止必须能清掉，不能恒返回"任务不存在"。"""
        tid = await register_task("content_generation", scheme_id="s1")
        # 模拟"另一个 worker / 异常退出"：抹掉本进程内存态，DB 仍是 running
        tr._tasks.pop(tid, None)

        monkeypatch.setattr(sh, "get_conn", lambda: _ConnFactory(db_conn))
        res = await sh.task_control(tid, {"action": "stop"})

        assert res.get("ok") is True, res
        assert res.get("orphaned") is True
        cur = await db_conn.execute("SELECT status FROM task_registry WHERE id=?", (tid,))
        assert (await cur.fetchone())[0] == "stopped"

    async def test_terminal_task_still_reports_failure(self, db_conn, monkeypatch):
        """真正已结束的任务不得误报"已停止"。"""
        tid = await register_task("content_generation", scheme_id="s1")
        await tr.finish_task(tid, "completed")
        tr._tasks.pop(tid, None)

        monkeypatch.setattr(sh, "get_conn", lambda: _ConnFactory(db_conn))
        res = await sh.task_control(tid, {"action": "stop"})
        assert res.get("ok") is False

    async def test_unknown_action_rejected(self, db_conn):
        res = await sh.task_control("whatever", {"action": "explode"})
        assert res == {"ok": False, "message": "未知操作"}

    async def test_in_memory_task_uses_normal_path(self, db_conn, monkeypatch):
        """内存态存在时不得走僵尸分支（必须同时作用到 pause_event）。"""
        tid = await register_task("content_generation", scheme_id="s1")
        monkeypatch.setattr(sh, "get_conn", lambda: _ConnFactory(db_conn))
        res = await sh.task_control(tid, {"action": "pause"})
        assert res == {"ok": True, "action": "pause"}
        assert tr._tasks[tid]["pause_event"].is_set() is False


class _ConnFactory:
    """让 `await get_conn()` 返回测试连接（sse_handlers 在 import 期绑定了 get_conn）。"""

    def __init__(self, conn):
        self._conn = conn

    def __call__(self):
        return self

    def __await__(self):
        async def _ret():
            return self._conn
        return _ret().__await__()
