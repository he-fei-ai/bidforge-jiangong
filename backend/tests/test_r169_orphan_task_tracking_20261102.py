# -*- coding: utf-8 -*-
"""R169（2026-11-02）：游离 create_task 的保活登记 + 异常留痕 + 完成清理。

背景（P2-6）：
  ``export_flow`` 与 ``middleware`` 直接 ``asyncio.create_task`` 后不保存引用。
  事件循环对任务仅持弱引用，任务可能被 GC 提前取消；未捕获异常只会落
  ``Task exception was never retrieved`` 噪音，缺少 flow id / 任务名等定位信息。
本文件锁定：
  A. 假 flow 抛异常 → ERROR 日志含 task_id / scheme_id 与异常文本，无未处理告警；
  B. 任务正常完成后模块级强引用被清理（不长期累积）；
  C. middleware 的 fire-and-forget 观测任务同口径。
"""
import asyncio
import logging

import pytest
from app.middleware import _OBS_TASKS, _track_obs_task
from app.routers import export_flow


@pytest.fixture(autouse=True)
def _isolate_tracking():
    """用例间清空模块级任务集合，避免串扰。"""
    export_flow._FLOW_TASKS.clear()
    _OBS_TASKS.clear()
    yield
    export_flow._FLOW_TASKS.clear()
    _OBS_TASKS.clear()


def _no_never_retrieved(caplog) -> bool:
    """确认没有 asyncio 的「Task exception was never retrieved」噪音。"""
    return not [
        r for r in caplog.records
        if "never retrieved" in r.getMessage().lower()
    ]


@pytest.mark.asyncio
async def test_failed_flow_logged_with_locator_and_reference_cleaned(caplog):
    """A：假 flow 抛异常 → 可定位 ERROR 日志，且任务引用被清理。"""

    async def _boom():
        raise RuntimeError("R169 假 flow 故障")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(_boom())
        export_flow._track_flow_task(task, "task-r169", "sch-r169-abc")
        assert task in export_flow._FLOW_TASKS
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert task not in export_flow._FLOW_TASKS
    hits = [
        r for r in caplog.records
        if r.name == export_flow.logger.name and "R169 假 flow 故障" in r.getMessage()
    ]
    assert hits, "异常未被登记为 ERROR 日志"
    assert "task-r169" in hits[0].getMessage()
    assert "sch-r169" in hits[0].getMessage()  # scheme_id[:8] 定位片段
    assert _no_never_retrieved(caplog)


@pytest.mark.asyncio
async def test_completed_flow_reference_cleaned():
    """B：正常完成的任务同样从模块级集合移除。"""

    async def _ok():
        return "done"

    task = asyncio.create_task(_ok())
    export_flow._track_flow_task(task, "task-ok", "sch-ok-abc")
    assert task in export_flow._FLOW_TASKS

    await asyncio.gather(task)
    await asyncio.sleep(0)

    assert task not in export_flow._FLOW_TASKS
    assert task.exception() is None


@pytest.mark.asyncio
async def test_middleware_obs_task_failure_logged_and_cleaned(caplog):
    """C：middleware 观测任务异常留痕 + 完成后清理。"""

    async def _boom():
        raise RuntimeError("R169 假观测故障")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(_boom())
        _track_obs_task(task, "http_span:trace-r169")
        assert task in _OBS_TASKS
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert task not in _OBS_TASKS
    hits = [
        r for r in caplog.records
        if r.name == "http" and "R169 假观测故障" in r.getMessage()
    ]
    assert hits, "观测任务异常未被登记为 ERROR 日志"
    assert "http_span:trace-r169" in hits[0].getMessage()
    assert _no_never_retrieved(caplog)
