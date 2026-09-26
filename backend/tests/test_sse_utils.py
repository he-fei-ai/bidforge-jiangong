"""sse_utils.py 单元测试

覆盖 PB-5 性能优化：with_heartbeat 中 Queue maxsize=100

测试策略：
- 构造异步生成器验证数据透传、心跳注入、错误传播
- 验证超过 maxsize 的数据量仍能完整传递（背压暂停生产者，不丢数据）
- 验证心跳在数据间隙时独立触发（不依赖生成器产出）
"""
import asyncio

import pytest

from app.services.ai.sse_utils import with_heartbeat


# ============================================================
# 辅助函数
# ============================================================
async def _collect(gen, interval=10):
    """收集 with_heartbeat 的所有输出"""
    results = []
    async for item in with_heartbeat(gen, interval=interval):
        results.append(item)
    return results


# ============================================================
# 数据透传测试
# ============================================================
class TestWithHeartbeatDataPassing:
    """with_heartbeat 基本数据透传"""

    async def test_single_item(self):
        """单项数据透传"""
        async def gen():
            yield "data: hello\n\n"

        results = await _collect(gen(), interval=10)
        assert results == ["data: hello\n\n"]

    async def test_multiple_items(self):
        """多项数据透传，顺序保持"""
        async def gen():
            yield "data: 1\n\n"
            yield "data: 2\n\n"
            yield "data: 3\n\n"

        results = await _collect(gen(), interval=10)
        assert results == ["data: 1\n\n", "data: 2\n\n", "data: 3\n\n"]

    async def test_empty_generator(self):
        """空生成器：无输出"""
        async def gen():
            return
            yield  # 使其成为 async generator

        results = await _collect(gen(), interval=10)
        assert results == []


# ============================================================
# 心跳测试
# ============================================================
class TestWithHeartbeatHeartbeat:
    """with_heartbeat 心跳注入"""

    async def test_heartbeat_during_long_wait(self):
        """长时间无数据时发送心跳

        生成器 sleep 0.25s，心跳间隔 0.1s → 期间应触发至少 1 次心跳。
        """
        async def gen():
            await asyncio.sleep(0.25)
            yield "data: done\n\n"

        results = await _collect(gen(), interval=0.1)

        # 应包含心跳和数据
        heartbeats = [r for r in results if "heartbeat" in r]
        assert len(heartbeats) >= 1
        assert "data: done\n\n" in results

    async def test_heartbeat_format(self):
        """心跳格式为 SSE comment：: heartbeat <timestamp>\n\n"""
        async def gen():
            await asyncio.sleep(0.15)
            yield "data: x\n\n"

        results = await _collect(gen(), interval=0.05)

        # ✅ 修复：原实现 if heartbeats: —— 一次心跳都没产生时测试静默通过
        #（零失败能力）。数据间隔 0.15s > 心跳间隔 0.05s，必然产生心跳。
        heartbeats = [r for r in results if "heartbeat" in r]
        assert heartbeats, "数据间隔大于心跳间隔时必须产生心跳"
        hb = heartbeats[0]
        assert hb.startswith(": heartbeat ")
        assert hb.endswith("\n\n")

    async def test_no_heartbeat_when_data_fast(self):
        """数据快速产出时无心跳（间隔远大于产出间隔）"""
        async def gen():
            for i in range(5):
                yield f"data: {i}\n\n"

        results = await _collect(gen(), interval=10)  # 10s 间隔，不会触发
        assert len(results) == 5
        assert all("heartbeat" not in r for r in results)


# ============================================================
# 错误传播测试
# ============================================================
class TestWithHeartbeatError:
    """with_heartbeat 错误传播"""

    async def test_generator_exception_raises_runtime_error(self):
        """生成器抛异常 → with_heartbeat 抛 RuntimeError"""
        async def gen():
            yield "data: 1\n\n"
            raise ValueError("AI 调用失败")

        with pytest.raises(RuntimeError, match="AI 调用失败"):
            async for _ in with_heartbeat(gen(), interval=10):
                pass

    async def test_error_after_partial_data(self):
        """部分数据后异常：已 yield 的数据到达，随后抛错"""
        async def gen():
            yield "data: 1\n\n"
            yield "data: 2\n\n"
            raise RuntimeError("连接断开")

        received = []
        with pytest.raises(RuntimeError, match="连接断开"):
            async for item in with_heartbeat(gen(), interval=10):
                received.append(item)

        assert received == ["data: 1\n\n", "data: 2\n\n"]


# ============================================================
# Queue maxsize=100 背压测试
# ============================================================
class TestWithHeartbeatQueueMaxsize:
    """Queue maxsize=100 背压测试

    PB-5 核心验证点：Queue(maxsize=100) 防止生成器无限产出
    导致内存溢出。当消费者跟不上时，生产者的 queue.put 会 await 挂起，
    形成背压。但所有数据最终都会到达（不丢不重）。
    """

    async def test_more_than_100_items_all_delivered(self):
        """150 项数据（超过 maxsize=100）全部到达

        消费者持续消费，生产者偶尔被背压暂停，但最终全部传递。
        """
        N = 150

        async def gen():
            for i in range(N):
                yield f"data: {i}\n\n"

        results = await _collect(gen(), interval=100)  # 长间隔避免心跳

        assert len(results) == N
        # 验证顺序和内容完整
        for i, item in enumerate(results):
            assert item == f"data: {i}\n\n"

    async def test_500_items_with_small_delay(self):
        """500 项数据 + 微小延迟：背压生效，数据完整"""
        N = 500

        async def gen():
            for i in range(N):
                await asyncio.sleep(0)  # 让出事件循环
                yield f"data: item-{i}\n\n"

        results = await _collect(gen(), interval=100)

        assert len(results) == N
        assert results[0] == "data: item-0\n\n"
        assert results[-1] == f"data: item-{N-1}\n\n"

    async def test_backpressure_does_not_block_forever(self):
        """背压不会死锁：生成器有 IO 等待时也能正常完成"""
        async def gen():
            for i in range(120):
                if i % 10 == 0:
                    await asyncio.sleep(0.001)  # 模拟 IO
                yield f"data: {i}\n\n"

        results = await _collect(gen(), interval=100)
        assert len(results) == 120


# ============================================================
# 资源清理测试
# ============================================================
class TestWithHeartbeatCleanup:
    """with_heartbeat 资源清理"""

    async def test_tasks_cancelled_on_normal_exit(self):
        """正常结束后内部 Task 被取消，无泄漏"""
        async def gen():
            yield "data: 1\n\n"

        results = await _collect(gen(), interval=0.01)

        # 等一拍让取消生效
        await asyncio.sleep(0.05)
        # 无悬挂的 Task（无法直接检查，但若泄漏会触发 RuntimeError 警告）
        assert results == ["data: 1\n\n"]

    async def test_tasks_cancelled_on_exception(self):
        """异常退出后内部 Task 被取消"""
        async def gen():
            yield "data: 1\n\n"
            raise ValueError("boom")

        with pytest.raises(RuntimeError):
            async for _ in with_heartbeat(gen(), interval=0.01):
                pass

        await asyncio.sleep(0.05)  # 让取消生效

    async def test_heartbeat_task_cancelled_when_gen_finishes(self):
        """生成器结束后心跳 Task 被取消（不继续发心跳）"""
        async def gen():
            yield "data: 1\n\n"

        results = await _collect(gen(), interval=0.001)  # 极短间隔
        # 正常完成，不应死等心跳
        assert results == ["data: 1\n\n"]

    async def test_gen_raises_immediately(self):
        """生成器立即抛异常：心跳还没开始就被取消"""
        async def gen():
            raise ValueError("instant fail")
            yield  # 使其成为 async generator

        with pytest.raises(RuntimeError, match="instant fail"):
            async for _ in with_heartbeat(gen(), interval=10):
                pass


# ============================================================
# P0 断连回归（2026-09-23）：消费者提前退出不得永久悬挂
# ============================================================
import asyncio  # noqa: E402  （追加在文件尾部，允许重复导入）


class TestWithHeartbeatDisconnectSafety:
    """消费者断开（提前退出/aclose）时 with_heartbeat 的收尾语义。

    事故根因：_gen_to_queue 的 finally 裸 await queue.put(("done", None))，
    消费者已死且队列满 → 生产者永久悬挂 → 外层 gather 永不返回 →
    内层 event_stream 永不被 aclose → 其 finally（finish_task 兜底 /
    事务收敛）永不执行 → 僵尸 running 任务 + 全局连接悬挂写事务
    （全站 500 database is locked）。
    """

    async def test_consumer_exit_closes_inner_generator(self):
        """消费一帧后断开 → 内层生成器的 finally 必须执行（旧实现挂死）"""
        closed = {"flag": False}

        async def gen():
            try:
                i = 0
                while True:
                    yield f"data: {i}\n\n"
                    i += 1
            finally:
                closed["flag"] = True

        stream = with_heartbeat(gen(), interval=10)
        async for _ in stream:
            break  # 模拟客户端只收到一帧就断开
        # 旧实现：gen_task 卡死在裸 queue.put(("done", None))（队列满），
        # aclose 内的 gather 永远等待 → 本行超时挂死。
        await asyncio.wait_for(stream.aclose(), timeout=10)
        assert closed["flag"], "内层生成器的 finally 必须执行（P0 断连修复）"

    async def test_close_completes_within_bound_with_backlog(self):
        """大队列积压 + 消费者即刻退出：收尾必须在有限时间内完成"""
        async def gen():
            i = 0
            while True:
                yield f"data: {i}\n\n"
                i += 1

        stream = with_heartbeat(gen(), interval=0.05)
        got = 0
        async for _ in stream:
            got += 1
            if got >= 3:
                break
        await asyncio.wait_for(stream.aclose(), timeout=10)  # 不得悬挂

    async def test_error_still_propagates(self):
        """错误语义不回退：内层 gen 抛错 → 消费端仍收到 RuntimeError"""
        async def gen():
            yield "data: a\n\n"
            raise ValueError("提取管线炸了")

        results = []
        with pytest.raises(RuntimeError, match="提取管线炸了"):
            async for item in with_heartbeat(gen(), interval=10):
                results.append(item)
        assert results == ["data: a\n\n"]
