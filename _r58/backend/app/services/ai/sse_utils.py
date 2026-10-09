"""SSE 公共工具：心跳包装器等"""
import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

# ✅ P0 断连修复（2026-09-23）· 三个收尾时序常量：
# 控制事件（error/done）向队列推送的最长等待秒数。消费端已死（客户端断开）
# 时队列恒满 —— 旧实现裸 await queue.put 会永久悬挂（详见 _gen_to_queue 注释）。
# 消费端另有「队列空 + 生产者已结束」超时兜底，因此控制事件被丢弃也不会
# 造成悬挂或错误丢失（error 经 _err_box 上抛）。
_PUSH_TIMEOUT = 0.5
# 关闭内层生成器（aclose）的最长等待：其 finally 可能含 finish_task 等
# 落库收尾（含 retry_db_op 重试），给足时间但不允许无限阻塞。
_ACLOSE_TIMEOUT = 15.0
# 消费端等待下一条事件的轮询间隔：仅事件断流时生效，有事件时立即返回。
_GET_POLL_INTERVAL = 1.0


async def with_heartbeat(gen, interval: float = 10, stats_provider=None):
    """用心跳包装原始 SSE 生成器，防止长时间 AI 调用导致浏览器/代理超时断开

    核心设计：两路 Task 并行运行，通过 Queue 协调输出：
    - gen_to_queue：从原始生成器取业务数据
    - hb_to_queue：每隔 interval 秒推心跳 SSE comment（: heartbeat ...）

    这样即使 AI 调用卡顿 30 秒，心跳也能独立触发，保持连接活跃。

    ✅ 增强（2026-09-15）：新增 stats_provider 回调。原始生成器在 await
    长时间 AI 调用（单章 1~5 分钟）时整体挂起，无法 yield 任何业务事件，
    用户看到进度条长时间静止、误以为卡死。心跳 Task 是此时唯一仍在运行的
    通道，因此由它周期性读取业务侧维护的运行统计（已耗时 / 进行中章节 /
    累计字数 / ETA），以 `ping` 事件推送给前端。
    stats_provider 为同步无参函数，返回 dict（可为空）；抛异常时静默跳过，
    绝不影响心跳本身与业务流。

    ✅ P0 修复（2026-09-23 · 断连事务泄漏 + 僵尸任务不落终态）：
    1) _gen_to_queue 的 finally 此前裸 await queue.put(("done", None)) ——
       消费者已停止且队列满时**永久悬挂**，外层 finally 的 gather 永远等不到
       两个 Task，内层 gen 永远不被 aclose → event_stream 的 finally
       （finish_task 兜底 / 后台管线任务取消 / 事务收敛）永不执行 →
       僵尸 running 任务 + 全局共享连接挂着未提交写事务（池写全 500
       database is locked）。现改为：先显式 aclose 内层 gen（确保其
       finally 执行），再有界推送控制事件；
    2) 消费端主循环加「队列空 + 生产者已结束」超时兜底 —— 即使 done/error
       信号因队满被丢弃也能收尾（error 经 _err_box 兜底上抛，错误不丢）。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    stop = asyncio.Event()
    # 内层 gen 的异常暂存：error 信号若因队满被丢弃，消费端超时兜底仍能上抛
    _err_box: dict = {"error": None}

    async def _push(kind, item) -> bool:
        """有界推送控制事件（error/done）：队满重试至超时，绝不永久悬挂。"""
        deadline = time.monotonic() + _PUSH_TIMEOUT
        while True:
            try:
                queue.put_nowait((kind, item))
                return True
            except asyncio.QueueFull:
                pass
            if time.monotonic() >= deadline:
                return False
            # 消费端仍在消费时队列会变浅，给它一个排空窗口再重试
            await asyncio.sleep(0.05)

    async def _gen_to_queue():
        try:
            async for item in gen:
                await queue.put(("data", item))
        except Exception as e:
            _err_box["error"] = e
            try:
                await _push("error", e)
            except asyncio.CancelledError:
                pass  # 消费端超时兜底 + _err_box 仍能上抛，错误不丢
        finally:
            # ✅ P0 修复核心：必须显式关闭内层生成器 —— 本 Task 被取消时
            #    （消费者断开），内层 gen 正挂起在 yield 处，唯有 aclose 会让
            #    它的 finally（finish_task 兜底 / pipe_task.cancel / 事务收敛）
            #    执行。第一次 await 若因「取消标志尚未消费」立即抛
            #    CancelledError（asyncio Task._must_cancel），重试一次消化后
            #    真正执行；done 推送同样有界（消费端有超时兜底可收尾）。
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                for attempt in range(2):
                    aclose_coro = aclose()
                    try:
                        await asyncio.wait_for(aclose_coro, timeout=_ACLOSE_TIMEOUT)
                        break
                    except asyncio.CancelledError:
                        try:
                            aclose_coro.close()
                        except Exception:
                            pass
                        if attempt == 0:
                            continue
                        logger.info("with_heartbeat: 关闭内层生成器时被取消（已尽力收尾）")
                        break
                    except Exception as e:
                        logger.warning("with_heartbeat: 关闭内层生成器失败（忽略）: %s", e)
                        break
            try:
                await _push("done", None)
            except asyncio.CancelledError:
                pass  # 消费端超时兜底可收尾，done 丢弃不致悬挂

    async def _hb_to_queue():
        try:
            while not stop.is_set():
                await asyncio.sleep(interval)
                # ✅ 先推运行统计（业务侧实时状态），再推心跳 comment（连接活性）。
                #    统计仅用于前端展示，任何异常都不得影响心跳与业务流。
                if stats_provider is not None:
                    try:
                        payload = stats_provider()
                    except Exception:
                        payload = None
                    if payload:
                        await queue.put(("data", "data: " + json.dumps(
                            {"event": "ping", **payload},
                            ensure_ascii=False) + "\n\n"))
                await queue.put(("heartbeat", f": heartbeat {time.time()}\n\n"))
        except asyncio.CancelledError:
            pass

    gen_task = asyncio.create_task(_gen_to_queue())
    hb_task = asyncio.create_task(_hb_to_queue())

    try:
        while True:
            try:
                kind, item = await asyncio.wait_for(
                    queue.get(), timeout=_GET_POLL_INTERVAL)
            except TimeoutError:
                # 队列空闲超时：仅当生产者已结束（done/error 可能因队满被
                # 丢弃）才收尾；否则视为事件间隙（长 AI 调用 + 心跳空档），
                # 继续等待。error 经 _err_box 兜底上抛，语义不丢。
                if gen_task.done() and queue.empty():
                    err = _err_box.get("error")
                    if err is not None:
                        raise RuntimeError(str(err))
                    break
                continue
            if kind == "done":
                break
            if kind == "error":
                raise RuntimeError(str(item))
            yield item
    finally:
        stop.set()
        gen_task.cancel()
        hb_task.cancel()
        try:
            await asyncio.gather(gen_task, hb_task, return_exceptions=True)
        except Exception:
            pass