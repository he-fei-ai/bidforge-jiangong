# -*- coding: utf-8 -*-
"""R109 closeout guards: P0 on_end wiring + P1 HTTP 401 non-retryable."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


# ---------- P1: HTTP 401 is non-retryable ----------


def test_http_401_is_non_retryable():
    """HTTP 401 (令牌失效) must be treated as permanent, not transient."""
    from app.services.ai.provider_factory import is_non_retryable_error

    assert is_non_retryable_error('HTTP 401: {"error":{"message":"该令牌状态不可用"}}')
    assert is_non_retryable_error("HTTP 401 Unauthorized")
    # Existing codes still covered
    assert is_non_retryable_error("HTTP 402 quota_exceeded")
    assert is_non_retryable_error("HTTP 403 forbidden")
    assert is_non_retryable_error("HTTP 404 InvalidEndpointOrModel.NotFound")
    # Chinese token-unavailable text
    assert is_non_retryable_error("该令牌状态不可用 (request id: xxx)")
    # 429 still retryable
    assert not is_non_retryable_error("HTTP 429 rate limit")
    # generic network error still retryable
    assert not is_non_retryable_error("Connection reset by peer")


def test_401_triggers_quota_cooldown():
    """401 must flow into quota cooldown (via _is_quota_error)."""
    from app.services.ai.provider_factory import _is_quota_error

    assert _is_quota_error("HTTP 401: token expired")


# ---------- P0: on_end wired on content + facts chains ----------


def _find_with_heartbeat_calls(tree):
    """Return list of (lineno, has_on_end, stats_provider_arg)."""
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(getattr(func, "attr", None), "id", None)
        # with_heartbeat(...)
        if isinstance(func, ast.Name) and func.id == "with_heartbeat":
            has_on_end = any(kw.arg == "on_end" for kw in node.keywords)
            results.append((node.lineno, has_on_end))
    return results


def test_content_chain_has_on_end():
    src = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = _find_with_heartbeat_calls(tree)
    # We expect 3 with_heartbeat calls: outline, content, facts
    # All three must have on_end
    assert len(calls) >= 3, f"expected >=3 with_heartbeat calls, got {len(calls)}: {calls}"
    no_on_end = [ln for ln, has in calls if not has]
    assert not no_on_end, f"with_heartbeat at lines {no_on_end} missing on_end"


def test_content_holder_task_id_set():
    """_content_holder['task_id'] must be assigned after register_task_exclusive."""
    src = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
    assert '_content_holder["task_id"] = task_id' in src
    assert '_facts_holder["task_id"] = task_id' in src


def test_stream_end_guard_exists():
    """_stream_end_guard must still exist and return a callable."""
    from app.routers.sse_handlers import _stream_end_guard

    guard = _stream_end_guard({"task_id": "test-x"})
    assert callable(guard)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
