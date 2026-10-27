"""Agent 任务全局 FIFO 队列（R135）。

对齐易标的并发边界：``agentService.cjs`` 中「同一父 Agent 同时运行的子代理
不超过文本并发上限的倍数，超出部分在创建 Runtime 前排队」与
``executionMode: 'sequential'`` 工具语义 —— Agent 级任务**串行执行**，
防止多个 Agent 并发改文件导致冲突。

本仓实现（进程内单例，asyncio 原语）：

- 所有 Agent 任务（正文修复、后续可扩审计/配图规划）经 :func:`submit`
  进入同一队列，**同一时间只运行一个**；
- 队列按 ``(priority, 入队序)`` 出队 —— 正文修复优先级高于配图规划，
  同优先级严格 FIFO；
- 支持暂停（不再启动新任务，运行中的任务不受影响——其自身的暂停由
  task_registry 控制）/ 恢复 / 取消待执行任务 / 单任务超时；
- 每个任务的结果经 ``asyncio.Future`` 回传；任务函数异常原样回传，
  **绝不静默吞掉**。

⚠️ 进程内语义：与 task_registry 相同，多 worker 部署下不跨进程（当前
部署为单进程，与既有 ``_tasks`` 内存态同一边界）。
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: 待执行队列上限（防止无人消费的堆积；运行中任务不占额度）。
DEFAULT_MAX_PENDING = 16


class AgentQueueFull(RuntimeError):
    """待执行队列已满（调用方应择机重试，而不是无限堆积）。"""


@dataclass
class QueueJob:
    """一次入队的 Agent 任务。"""

    fn: Callable[[], Awaitable[Any]]
    priority: int = 0
    task_id: str = ""
    description: str = ""
    seq: int = 0
    submitted_at: float = field(default_factory=time.monotonic)
    future: asyncio.Future | None = None


@dataclass
class QueueStats:
    """队列快照（供 /status 端点与测试断言）。"""

    running: dict
    pending: list
    paused: bool
    max_pending: int
    completed: int
    failed: int


class GlobalAgentQueue:
    """全局单 worker FIFO 队列。"""

    def __init__(self, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._max_pending = max(1, int(max_pending))
        self._heap: list = []
        self._counter = itertools.count()
        self._wake = asyncio.Event()
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._worker: asyncio.Task | None = None
        self._current: QueueJob | None = None
        self._paused = False
        self._completed = 0
        self._failed = 0

    async def submit(
        self,
        fn: Callable[[], Awaitable[Any]],
        *,
        priority: int = 0,
        task_id: str = "",
        description: str = "",
        timeout: float | None = None,
    ) -> Any:
        """入队并等待执行完成，返回任务函数的返回值。

        Args:
            fn: 零参异步可调用（执行期间的取消/暂停由其内部与
                task_registry 协作；队列只负责串行化）。
            priority: 数值越大越先出队（正文修复可用 10，后台任务 0）。
            timeout: 单任务墙钟超时（秒）；超时取消该任务并抛
                ``asyncio.TimeoutError``，**不中断 worker**。
        Raises:
            AgentQueueFull: 待执行数已达上限。
        """
        pending = len(self._heap) + (1 if self._current is not None else 0)
        if pending >= self._max_pending:
            raise AgentQueueFull(
                f"Agent 任务队列已满（{pending}/{self._max_pending}），请稍后重试")
        job = QueueJob(fn=fn, priority=int(priority), task_id=str(task_id),
                       description=str(description), seq=next(self._counter))
        loop = asyncio.get_running_loop()
        job.future = loop.create_future()
        heapq.heappush(self._heap, (-job.priority, job.seq, job))
        self._ensure_worker()
        self._wake.set()
        try:
            if timeout is not None:
                return await asyncio.wait_for(job.future, timeout)
            return await job.future
        except asyncio.TimeoutError:
            # 超时：任务可能仍在跑；把 future 置取消，worker 侧写回前会
            # 检查 done()，避免 InvalidStateError。
            if not job.future.done():
                job.future.cancel()
            raise

    def pause(self) -> None:
        """暂停出队：不再启动新任务；运行中的任务不受影响。"""
        self._paused = True
        self._pause_gate.clear()

    def resume(self) -> None:
        """恢复出队。"""
        self._paused = False
        self._pause_gate.set()

    def cancel_pending(self, task_id: str) -> int:
        """取消指定 task_id 的**待执行**任务（运行中的不取消）。返回取消数。"""
        removed = 0
        kept: list = []
        while self._heap:
            entry = heapq.heappop(self._heap)
            job = entry[2]
            if job.task_id and job.task_id == str(task_id) and job.future \
                    and not job.future.done():
                job.future.cancel()
                removed += 1
                continue
            kept.append(entry)
        for entry in kept:
            heapq.heappush(self._heap, entry)
        return removed

    def stats(self) -> QueueStats:
        current = self._current
        return QueueStats(
            running=({
                "task_id": current.task_id,
                "description": current.description,
                "priority": current.priority,
                "waited_ms": round((time.monotonic() - current.submitted_at) * 1000),
            } if current else {}),
            pending=[{
                "task_id": j.task_id, "description": j.description,
                "priority": j.priority,
            } for _p, _s, j in sorted(self._heap, key=lambda e: (-e[0], e[1]))],
            paused=self._paused,
            max_pending=self._max_pending,
            completed=self._completed,
            failed=self._failed,
        )

    async def wait_idle(self, timeout: float = 30.0) -> bool:
        """等待队列清空（测试辅助）；超时返回 False。"""
        deadline = time.monotonic() + timeout
        while (self._current is not None or self._heap) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return not (self._current is not None or self._heap)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                await self._pause_gate.wait()  # 暂停闸门：出队前等待恢复
                entry = None
                if self._heap:
                    entry = heapq.heappop(self._heap)
                if entry is None:
                    break
                job = entry[2]
                if job.future is not None and job.future.done():
                    continue  # 已被取消/超时置取消的任务直接丢弃
                self._current = job
                try:
                    result = await job.fn()
                    if job.future is not None and not job.future.done():
                        job.future.set_result(result)
                    self._completed += 1
                except asyncio.CancelledError:
                    if job.future is not None and not job.future.done():
                        job.future.cancel()
                    self._failed += 1
                except BaseException as error:  # noqa: BLE001 —— 原样回传给 submit 方
                    if job.future is not None and not job.future.done():
                        job.future.set_exception(error)
                    self._failed += 1
                finally:
                    self._current = None


#: 进程内单例。
_QUEUE: GlobalAgentQueue | None = None


def global_agent_queue() -> GlobalAgentQueue:
    """返回进程内全局 Agent 队列（懒创建）。"""
    global _QUEUE
    if _QUEUE is None:
        from app.config import settings
        _QUEUE = GlobalAgentQueue(
            max_pending=int(getattr(settings, "content_agent_queue_max_pending",
                                    DEFAULT_MAX_PENDING) or DEFAULT_MAX_PENDING))
    return _QUEUE


def reset_global_agent_queue(max_pending: int = DEFAULT_MAX_PENDING) -> GlobalAgentQueue:
    """重建全局队列（测试隔离用；生产代码不要调用）。"""
    global _QUEUE
    _QUEUE = GlobalAgentQueue(max_pending=max_pending)
    return _QUEUE

