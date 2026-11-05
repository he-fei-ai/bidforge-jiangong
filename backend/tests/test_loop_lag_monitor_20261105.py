"""事件循环延迟埋点护栏（2026-11-05 · R2-P0-1）。

背景：生产日志里 `/sections` 8.6~10.2s 与 `/sse/tasks` 同期 8.6s，
但访问日志只有请求总耗时，无法判断「请求自身慢」还是「事件循环被
同步 CPU 任务整体阻塞」。中间件用「应唤醒时刻 vs 实际唤醒时刻」的
偏差做判据，是应用侧唯一能直接观测到阻塞的证据。

本文件锁定：
1. 循环被同步阻塞时确实产出 WARNING（证据可得）；
2. 冷却窗口内只打一条（默认不刷屏）；
3. 非法配置回落默认值，不抛错。
"""

import asyncio
import logging
import time

from app.middleware import _loop_lag_monitor, _positive


async def _block_loop(seconds: float) -> None:
    """在协程里同步 sleep —— 精确复现「同步 CPU 任务占住事件循环」。"""
    time.sleep(seconds)


class TestLoopLagMonitor:
    async def test_warns_when_loop_is_blocked(self, caplog):
        with caplog.at_level(logging.WARNING, logger="loop"):
            task = asyncio.create_task(_loop_lag_monitor(interval=0.05, threshold=0.05, cooldown=0))
            try:
                await asyncio.sleep(0.02)
                await _block_loop(0.25)
                await asyncio.sleep(0.15)
            finally:
                task.cancel()
        msgs = [r.getMessage() for r in caplog.records if r.name == "loop" and r.levelno == logging.WARNING]
        assert any("事件循环延迟" in m for m in msgs), f"未产出延迟告警：{msgs}"

    async def test_cooldown_suppresses_repeated_warnings(self, caplog):
        with caplog.at_level(logging.WARNING, logger="loop"):
            task = asyncio.create_task(_loop_lag_monitor(interval=0.05, threshold=0.05, cooldown=10))
            try:
                for _ in range(2):
                    await asyncio.sleep(0.02)
                    await _block_loop(0.2)
                    await asyncio.sleep(0.1)
            finally:
                task.cancel()
        msgs = [r.getMessage() for r in caplog.records if r.name == "loop" and r.levelno == logging.WARNING]
        assert len(msgs) == 1, f"冷却未生效，打了 {len(msgs)} 条：{msgs}"

    async def test_no_false_positive_on_idle_loop(self, caplog):
        with caplog.at_level(logging.WARNING, logger="loop"):
            task = asyncio.create_task(_loop_lag_monitor(interval=0.02, threshold=0.5, cooldown=0))
            try:
                await asyncio.sleep(0.3)
            finally:
                task.cancel()
        assert not [r for r in caplog.records if r.name == "loop" and r.levelno == logging.WARNING]

    def test_positive_falls_back_to_default(self):
        assert _positive(0, 7) == 7
        assert _positive(-3, 7) == 7
        assert _positive("abc", 7) == 7
        assert _positive(None, 7) == 7
        assert _positive(2, 7) == 2
