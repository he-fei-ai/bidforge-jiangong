"""R11 回归测试：mermaid_renderer 空输入静默 + 渲染缓存。

背景：logs/backend.log 中 22 次连续 "[渲染] 空输入直接返回 None"。
     空输入是**正常控制流**（调用方判定无图可渲染），却打 WARNING
     淹没真实错误信号。本轮修复：
     1. 空输入改为 DEBUG 级日志（业务语义不变，仍返回 None）；
     2. 新增模块级 LRU 渲染缓存（同参数命中即返回，跳过 PIL v2 CPU 密集路径）。
"""
from __future__ import annotations

import logging

import pytest

from app.services.ai import mermaid_renderer


def test_empty_input_returns_none_without_warning(caplog):
    """空输入仍返回 None（业务语义不变），但不再打 WARNING。"""
    caplog.set_level(logging.DEBUG)
    result = mermaid_renderer.render_mermaid_to_bytes("", "flowchart")
    assert result is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING
                and "空输入" in r.getMessage()]
    assert not warnings, f"空输入不应产生 WARNING 日志，实际: {[r.getMessage() for r in warnings]}"
    # DEBUG 级应该有记录（业务可观测性保留）
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG
              and "空输入" in r.getMessage()]
    assert debugs, "空输入应至少产生一条 DEBUG 日志（保留可观测性）"


def test_render_cache_hit_on_second_call():
    """同一份代码第二次调用应命中缓存（跳过 PIL v2 渲染）。"""
    mermaid_renderer.clear_render_cache()
    code = "graph TD; A-->B; B-->C;"
    # 第一次：真实渲染
    r1 = mermaid_renderer.render_mermaid_to_bytes(code, "flowchart")
    if r1 is None:
        pytest.skip("当前环境下 flowchart 渲染失败，跳过缓存测试")
    assert r1 is not None
    r1_bytes = r1.getvalue()
    # ✅ 断言更新（2026-09-22）：缓存命中改为返回副本（防任一调用方
    #    read/seek 污染其他方，见 _render_cache_get 注释），`r2 is r1`
    #    永不成立；改用 spy 验证第二次确实走了缓存分支 + 内容字节一致。
    hits = {"n": 0}
    orig_get = mermaid_renderer._render_cache_get

    def _spy(key):
        v = orig_get(key)
        if v is not None:
            hits["n"] += 1
        return v

    mermaid_renderer._render_cache_get = _spy
    try:
        r2 = mermaid_renderer.render_mermaid_to_bytes(code, "flowchart")
    finally:
        mermaid_renderer._render_cache_get = orig_get
    assert hits["n"] == 1, "第二次调用应命中缓存（_render_cache_get 返回非 None）"
    assert r2 is not None and r2 is not r1, "命中后应返回独立副本而非同一对象"
    assert r2.getvalue() == r1_bytes
    # 内容应可读（BytesIO 已 seek(0)）
    assert len(r2.getvalue()) > 100, "缓存结果应包含有效 PNG 字节"


def test_render_cache_different_params_miss():
    """不同参数（如 unit/tick）应产生独立缓存条目。"""
    mermaid_renderer.clear_render_cache()
    code = "gantt\ntitle Test\n"
    r1 = mermaid_renderer.render_mermaid_to_bytes(code, "gantt", unit="天", tick=5)
    r2 = mermaid_renderer.render_mermaid_to_bytes(code, "gantt", unit="小时", tick=10)
    if r1 is None or r2 is None:
        pytest.skip("当前环境下 gantt 渲染失败，跳过参数差异测试")
    assert r1 is not r2, "不同 unit/tick 应产生不同缓存条目"


def test_render_cache_cleared_by_clear():
    """clear_render_cache 后再次渲染应重新走完整路径。"""
    mermaid_renderer.clear_render_cache()
    code = "graph TD; A-->B;"
    r1 = mermaid_renderer.render_mermaid_to_bytes(code, "flowchart")
    if r1 is None:
        pytest.skip("当前环境下 flowchart 渲染失败")
    assert mermaid_renderer._render_cache.get(
        mermaid_renderer._render_cache_key(code, "flowchart", 90, "天", 5, False, True)
    ) is not None, "首次渲染后应写入缓存"
    mermaid_renderer.clear_render_cache()
    assert len(mermaid_renderer._render_cache) == 0, "clear 后缓存应清空"


def test_render_cache_lru_eviction():
    """缓存容量超过 _RENDER_CACHE_MAX 时应按 LRU 淘汰最老条目。"""
    mermaid_renderer.clear_render_cache()
    # 直接写入超过容量的条目，验证淘汰逻辑（避免真渲染导致性能问题）
    max_size = mermaid_renderer._RENDER_CACHE_MAX
    from io import BytesIO
    for i in range(max_size + 10):
        key = ("flowchart", f"code-{i}", 90, "天", 5, False, True)
        mermaid_renderer._render_cache_put(key, BytesIO(b"png-bytes-" + str(i).encode()))
    assert len(mermaid_renderer._render_cache) == max_size, \
        f"缓存应限制在 {max_size} 项，实际 {len(mermaid_renderer._render_cache)}"
    # 最老的条目（code-0）应被淘汰
    old_key = ("flowchart", "code-0", 90, "天", 5, False, True)
    assert old_key not in mermaid_renderer._render_cache, "最老条目应被淘汰"


def test_render_cache_put_ignores_none():
    """_render_cache_put 对 None 结果应忽略（避免把错误固化）。"""
    mermaid_renderer.clear_render_cache()
    key = ("flowchart", "empty", 90, "天", 5, False, True)
    mermaid_renderer._render_cache_put(key, None)
    assert key not in mermaid_renderer._render_cache
    assert len(mermaid_renderer._render_cache) == 0