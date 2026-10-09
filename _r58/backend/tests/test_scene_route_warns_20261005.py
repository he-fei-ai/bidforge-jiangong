"""A4 · 场景路由静默告警护栏（2026-10-05）

背景：`resolve_scene_config` 遇到未登记场景时**仍按既有语义返回 None**（保持
向后兼容），但不再"静默失效"——首次命中时打 warning 便于归因。

本测试锁定 3 件事：
1. 未登记场景首次命中打 warning，且**仅打一次**（同进程内去重）；
2. 已登记场景绝不触发该 warning（避免噪音）；
3. 行为完全兼容：未登记场景返回 None，与告警引入前一致。
"""
from __future__ import annotations

import logging

import pytest
from app.services.ai.provider_factory import (
    KNOWN_SCENES,
    _scene_unregistered_warned_scenes,
    resolve_scene_config,
)


def _reset_warned() -> None:
    """清空告警去重集合（避免测试间串扰）。"""
    _scene_unregistered_warned_scenes.clear()


async def test_unregistered_scene_logs_warning_once_only(
    db_conn, caplog: pytest.LogCaptureFixture
) -> None:
    """未登记场景：首次调用打 warning，后续调用不再打（进程内去重）。"""
    _reset_warned()
    scene = "totally_unknown_scene_xyz"
    assert scene not in KNOWN_SCENES

    with caplog.at_level(logging.WARNING, logger="provider_factory"):
        # 第一次调用 → 返回 None
        r1 = await resolve_scene_config(scene)
        # 第二次调用 → 返回 None，且不再打 warning
        r2 = await resolve_scene_config(scene)

    assert r1 is None, "未登记场景应返回 None 走主配置"
    assert r2 is None, "同上"
    # 关键断言：只打一次 warning
    warn_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "provider_factory" and rec.levelno == logging.WARNING
        and "未在 KNOWN_SCENES 登记" in rec.getMessage()
    ]
    assert len(warn_lines) == 1, f"应仅告警一次，实际 {len(warn_lines)} 次：{warn_lines}"
    assert scene in warn_lines[0], f"告警应包含场景名：{warn_lines[0]}"


async def test_known_scene_does_not_warn(
    db_conn, caplog: pytest.LogCaptureFixture
) -> None:
    """已登记场景：不应触发未登记告警（避免噪音）。"""
    _reset_warned()
    scene = "content_draft"
    assert scene in KNOWN_SCENES, "前提：该场景应已登记"

    with caplog.at_level(logging.WARNING, logger="provider_factory"):
        await resolve_scene_config(scene)

    warn_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "provider_factory" and "未在 KNOWN_SCENES 登记" in rec.getMessage()
    ]
    assert warn_lines == [], f"已登记场景不应触发告警：{warn_lines}"


async def test_empty_and_whitespace_scene_no_warn(
    db_conn, caplog: pytest.LogCaptureFixture
) -> None:
    """空串 / 全空白场景：直接返回 None，不触发未登记告警。"""
    _reset_warned()
    with caplog.at_level(logging.WARNING, logger="provider_factory"):
        assert await resolve_scene_config("") is None
        assert await resolve_scene_config("   ") is None
    warn_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "provider_factory" and "未在 KNOWN_SCENES 登记" in rec.getMessage()
    ]
    assert warn_lines == [], f"空/空白场景不应触发告警：{warn_lines}"
