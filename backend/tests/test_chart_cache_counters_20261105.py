"""图表缓存命中计数出口护栏（2026-11-05 · R2-P1 图表缓存可观测）。

现状核对：``ChartCache`` 内部**已有** hits/misses 累加（``get_or_render``
命中/未命中分支），但 ``get_stats()`` 只被自身使用、没有任何 API/响应头
出口 → 生产上无法回答「本次导出图表缓存命中了吗」。本文件锁定新增的
轻量计数出口 ``get_counters()`` 语义，以及它与命中路径的一致性。
"""

import io

from app.services.ai.mermaid_renderer import _chart_cache


def _put_cached_chart(code: str, chart_type: str = "flowchart") -> None:
    """按缓存 key 规则预置一份磁盘缓存。

    ✅ R199（2026-11-09）：get_or_render 磁盘命中补全文件完整性校验（长度 +
    PIL verify）—— 预置内容必须是真实可解码 PNG（旧 b"fake-png-bytes" 会被
    新判据正确拒收并删除重渲，hits 恒不增）。
    """
    from PIL import Image

    key = _chart_cache._cache_key(chart_type, code, True, False)
    path = _chart_cache.cache_dir / f"{key}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 30)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


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
