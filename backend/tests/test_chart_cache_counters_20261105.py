"""图表缓存命中计数出口护栏（2026-11-05 · R2-P1 图表缓存可观测）。

现状核对：``ChartCache`` 内部**已有** hits/misses 累加（``get_or_render``
命中/未命中分支），但 ``get_stats()`` 只被自身使用、没有任何 API/响应头
出口 → 生产上无法回答「本次导出图表缓存命中了吗」。本文件锁定新增的
轻量计数出口 ``get_counters()`` 语义，以及它与命中路径的一致性。
"""

from app.services.ai.mermaid_renderer import _chart_cache


def _put_cached_chart(code: str, chart_type: str = "flowchart") -> None:
    """按缓存 key 规则预置一份磁盘缓存（不依赖 mermaid 二进制）。"""
    key = _chart_cache._cache_key(chart_type, code, True, False)
    path = _chart_cache.cache_dir / f"{key}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-png-bytes")


class TestChartCacheCounters:
    def test_counters_shape(self):
        c = _chart_cache.get_counters()
        assert set(c) == {"hits", "misses"}
        assert all(isinstance(v, int) for v in c.values())

    def test_hit_increments_counter(self):
        before = _chart_cache.get_counters()
        code = "graph TD; A-->B; %% counters-hit"
        _put_cached_chart(code)
        out = _chart_cache.get_or_render(code, "flowchart", fmt="png", allow_pil=True)
        assert out is not None
        after = _chart_cache.get_counters()
        assert after["hits"] == before["hits"] + 1
        assert after["misses"] == before["misses"]

    def test_counters_are_process_global(self):
        """计数是进程级累计（导出侧用「前后差值」得到本次导出口径）。"""
        a = _chart_cache.get_counters()
        b = _chart_cache.get_counters()
        assert a == b
