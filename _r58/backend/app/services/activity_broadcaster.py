"""系统活动广播器

供「后台任务运行状态栏」SSE 实时推送使用。
任务注册、进度更新、AI 调用起止等事件发生后，向所有订阅者发送刷新信号；
SSE 端点收到信号后重新聚合任务 / AI / 服务态并推送给浏览器。

设计要点：
- 纯内存、无持久化；
- 订阅者用 asyncio.Queue，队列满时丢弃最旧事件（背压保护）；
- notify() 可在同步 / 异步上下文中调用，不会阻塞事件循环；
- 不聚合具体业务数据，仅作为“请刷新”信号，降低事件体积。
"""
import asyncio
import logging
import threading

logger = logging.getLogger("activity_broadcaster")

_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()


def _put_nowait(q: asyncio.Queue, item: object) -> None:
    """非阻塞放入队列；队列满时丢弃最旧事件，保证订阅者不会积压。"""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


def notify() -> None:
    """通知所有订阅者：系统活动状态可能已变更。

    同步 / 异步上下文均可调用。若当前无运行事件循环（如单元测试），
    则静默跳过，不影响业务主流程。
    """
    with _lock:
        subs = list(_subscribers)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    for q in subs:
        try:
            loop.call_soon_threadsafe(_put_nowait, q, True)
        except Exception:
            # 通知失败不影响主流程
            pass


async def subscribe() -> asyncio.Queue:
    """订阅系统活动变更事件。返回队列，调用方通过 await queue.get() 等待信号。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    """取消订阅并清理队列。"""
    with _lock:
        _subscribers.discard(q)
    try:
        while not q.empty():
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass
