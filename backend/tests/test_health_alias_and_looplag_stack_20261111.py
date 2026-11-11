"""第三轮日志驱动分析修复护栏（2026-11-11）。

背景（backend.log 2026-11-07~11-10 · 2563 行日志驱动）：
1. ``GET /health -> 404`` 累计 34 条（外部探针每 30s 一发）—— middleware
   注释本就声明「/health 供探活使用」，但后端只挂了 ``/api/v1/health``。
   本文件锁定顶层别名与正式端点返回体逐字一致。
2. 事件循环严重阻塞（29150/16771/15638/6978ms）旧日志只有数值无法定位
   罪魁 —— 严重阻塞时新增「在途任务栈采样」WARNING。本文件锁定：
   采集分支必产出栈采样行、无在途任务时走兜底文案、且任何输入都不抛异常
   （纯观测增强，绝不影响主流程）。
"""

import asyncio
import logging

import pytest
from app.middleware import _log_blocking_task_stacks
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


class TestHealthAlias:
    def test_top_level_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") in ("ok", "degraded"), body
        assert "version" in body

    def test_alias_body_matches_canonical(self, client):
        """别名与 /api/v1/health 必须同一 handler：返回体逐字一致。"""
        assert client.get("/health").json() == client.get("/api/v1/health").json()


class TestLoopLagStackCapture:
    async def test_captures_pending_task_stacks(self, caplog):
        """有在途任务时产出「在途任务栈采样」WARNING，含可定位的 file:lineno。"""
        with caplog.at_level(logging.WARNING, logger="loop"):
            probe = asyncio.create_task(asyncio.sleep(10), name="probe-task")
            try:
                await asyncio.sleep(0)  # 让 probe 真正挂起
                monitor = asyncio.current_task()
                _log_blocking_task_stacks(monitor, 6.0)
            finally:
                probe.cancel()
                try:
                    await probe
                except asyncio.CancelledError:
                    pass
        msgs = [r.getMessage() for r in caplog.records if r.name == "loop"]
        sampled = [m for m in msgs if "在途任务栈采样" in m]
        assert sampled, f"未产出栈采样告警：{msgs}"
        assert "probe-task" in sampled[0], sampled[0]
        assert "asyncio" in sampled[0] or ".py:" in sampled[0], sampled[0]

    async def test_falls_back_when_no_pending_tasks(self, caplog):
        with caplog.at_level(logging.WARNING, logger="loop"):
            _log_blocking_task_stacks(asyncio.current_task(), 6.0)
        msgs = [r.getMessage() for r in caplog.records if r.name == "loop"]
        assert any("未采集到在途任务栈" in m for m in msgs), msgs

    def test_never_raises_on_degenerate_input(self, caplog):
        """monitor_task=None / lag 极值都不得抛异常（纯诊断，失败只 debug）。"""
        _log_blocking_task_stacks(None, 999.0)
        _log_blocking_task_stacks(None, 0.0)
