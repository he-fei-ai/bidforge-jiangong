"""R249 · hifi 模板轨渲染缓存 + 单飞去重护栏。

背景：render_hifi_png 此前绕过 ChartCache——同一载荷重复导出会反复 launch
Playwright 重渲染，且 export._render_one 在事件循环上被 Semaphore(4) 并发
gather，同图并发无共享去重。本轮给 hifi 轨补磁盘缓存 + asyncio 单飞门。

护栏不 launch 真浏览器：monkeypatch ChartExporter._render_html 为计数桩，
只验证「缓存/单飞」这层编排逻辑（真实出图由 R242 冒烟覆盖）。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from app.services.ai.hifi_charts import exporter
from app.services.ai.hifi_charts.exporter import (
    ChartExporter,
    _hifi_cache_key,
    get_hifi_render_stats,
    render_hifi_png,
)
from app.services.ai.mermaid_renderer import ChartCache, _cached_bytes_ok

_PAYLOAD = {
    "start_date": "2026-01-01",
    "tasks": [{"name": "勘察设计", "start": "2026-01-01", "end": "2026-01-10", "progress": 0.3}],
}
_PAYLOAD_B = {
    "start_date": "2026-02-01",
    "tasks": [{"name": "主体施工", "start": "2026-02-01", "end": "2026-02-20", "progress": 0.6}],
}


def _png(tag: int) -> bytes:
    """构造合法 PNG bytes（长度 >100，过 _cached_bytes_ok 的 PIL verify）。"""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60 + tag, 40), (10 * tag % 255, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """缓存重定向到 tmp（绕开环境只读 ACL）+ 计数桩替换真渲染 + 状态复位。"""
    from app.services.ai.mermaid_renderer import ChartCache as _CC

    exporter._hifi_gate._cache = _CC(str(tmp_path / "hifi"))
    exporter._hifi_gate._inflight.clear()
    exporter._hifi_gate.stats = {
        "cache_hits": 0,
        "cache_misses": 0,
        "inflight_shared": 0,
        "cache_errors": 0,
    }
    monkeypatch.setattr(exporter, "_HIFI_UNAVAILABLE_UNTIL", 0.0)

    calls = {"n": 0}

    async def _fake_render_html(self, html, selector, full=False):
        calls["n"] += 1
        return _png(calls["n"])

    monkeypatch.setattr(ChartExporter, "_render_html", _fake_render_html)
    return calls


@pytest.fixture()
def no_cache(tmp_path, monkeypatch):
    """缓存目录不可用（生产只读 ACL 时的降级形态）：无磁盘缓存，渲染照常。"""
    exporter._hifi_gate._cache = None
    exporter._hifi_gate._inflight.clear()
    exporter._hifi_gate.stats = {
        "cache_hits": 0,
        "cache_misses": 0,
        "inflight_shared": 0,
        "cache_errors": 0,
    }
    monkeypatch.setattr(exporter, "_HIFI_UNAVAILABLE_UNTIL", 0.0)
    calls = {"n": 0}

    async def _fake_render_html(self, html, selector, full=False):
        calls["n"] += 1
        return _png(calls["n"])

    monkeypatch.setattr(ChartExporter, "_render_html", _fake_render_html)
    return calls


@pytest.mark.asyncio
async def test_a1_disk_cache_roundtrip(gate):
    """同载荷第二次渲染命中磁盘缓存，不再触发热渲染。"""
    p1 = await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 1
    p2 = await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 1, "第二次同载荷必须命中缓存、不再 launch 渲染"
    assert p2 == p1
    stats = get_hifi_render_stats()
    assert stats["cache_hits"] == 1 and stats["cache_misses"] == 1


@pytest.mark.asyncio
async def test_a2_scale_preset_isolates_cache_key(gate):
    """同载荷不同 scale_preset 各存一份（截图倍率不同产物不同）。"""
    await render_hifi_png("gantt", _PAYLOAD, scale_preset="print")
    await render_hifi_png("gantt", _PAYLOAD, scale_preset="print")
    await render_hifi_png("gantt", _PAYLOAD, scale_preset="screen")
    assert gate["n"] == 2, "print 命中缓存，screen 首次独立渲染"
    k_print = _hifi_cache_key("gantt", _PAYLOAD, "print")
    k_screen = _hifi_cache_key("gantt", _PAYLOAD, "screen")
    assert k_print != k_screen


@pytest.mark.asyncio
async def test_a3_route_and_payload_isolate_cache_key(gate):
    """不同路由 / 不同载荷互不串用缓存槽。"""
    await render_hifi_png("gantt", _PAYLOAD)
    await render_hifi_png("gantt", _PAYLOAD_B)
    await render_hifi_png("org_matrix", {"nodes": {"level1": [{"label": "公司"}]}})
    assert gate["n"] == 3


@pytest.mark.asyncio
async def test_b1_single_flight_shares_concurrent_render(gate):
    """同 key 并发只起一个渲染 task，其余共享 leader 结果。"""
    results = await asyncio.gather(*[render_hifi_png("gantt", _PAYLOAD) for _ in range(5)])
    assert gate["n"] == 1, f"5 并发同载荷只应渲染 1 次，实际 {gate['n']}"
    assert len(set(results)) == 1
    stats = get_hifi_render_stats()
    assert stats["inflight_shared"] == 4 and stats["cache_misses"] == 1


@pytest.mark.asyncio
async def test_b2_single_flight_propagates_failure(gate, monkeypatch):
    """leader 渲染抛错时 follower 收到同族异常（调用方降级 PIL 轨），不静默假成功。"""
    calls = {"n": 0}

    async def _boom(self, html, selector, full=False):
        calls["n"] += 1
        raise RuntimeError("hifi 渲染失败（stub）")

    monkeypatch.setattr(ChartExporter, "_render_html", _boom)
    results = await asyncio.gather(
        *[render_hifi_png("gantt", _PAYLOAD) for _ in range(3)],
        return_exceptions=True,
    )
    assert calls["n"] == 1, "leader 渲染一次，follower 共享其失败"
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_c1_corrupt_cache_self_heals(gate, tmp_path):
    """磁盘命中但内容损坏（截断/半文件）时删除坏文件并重渲，不永久命中坏产物。"""
    good = await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 1
    # 落盘文件名是 hash（"hifi:" 前缀只拼进 _cache_key 的 material，不在文件名）。
    # 直接以缓存目录实际落盘文件为单一事实源，避免复刻 key 算法漂移。
    cache_dir = tmp_path / "hifi"
    pngs = list(cache_dir.glob("*.png"))
    assert len(pngs) == 1, f"首次渲染应落 1 个缓存文件，实际 {len(pngs)}"
    cache_file = pngs[0]
    assert cache_file.exists()
    # 写确定性垃圾字节（小图截断仍可能是完整合法 PNG，故直接破坏 magic/结构）。
    cache_file.write_bytes(b"\x00" * 40 + b"corrupted-not-a-png" * 20)
    assert not _cached_bytes_ok(cache_file.read_bytes())
    healed = await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 2, "坏缓存必须触发重渲（自愈）"
    assert _cached_bytes_ok(healed)
    assert healed != good, "自愈走的是真实重渲（桩按计数产出不同图），非命中坏缓存"


@pytest.mark.asyncio
async def test_c2_env_unavailable_ttl_short_circuits_before_cache(gate, monkeypatch):
    """环境级不可用 TTL 短路优先于缓存：置位期间连缓存命中也不读。"""
    await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 1
    monkeypatch.setattr(exporter, "_HIFI_UNAVAILABLE_UNTIL", time.monotonic() + 300.0)
    with pytest.raises(RuntimeError, match="TTL"):
        await render_hifi_png("gantt", _PAYLOAD)
    assert gate["n"] == 1


@pytest.mark.asyncio
async def test_d1_missing_cache_dir_degrades_but_renders(no_cache):
    """缓存目录不可用降级为无磁盘缓存：渲染照常出图、单飞去重仍生效。"""
    p1 = await render_hifi_png("gantt", _PAYLOAD)
    p2 = await render_hifi_png("gantt", _PAYLOAD)
    assert no_cache["n"] == 2, "无磁盘缓存时每次都渲染（不假命中）"
    assert isinstance(p1, bytes) and isinstance(p2, bytes)
    stats = get_hifi_render_stats()
    assert stats["cache_hits"] == 0 and stats["cache_errors"] == 0


@pytest.mark.asyncio
async def test_d2_no_cache_still_single_flights(no_cache):
    """无磁盘缓存时并发同载荷仍共享一次渲染（单飞独立于磁盘缓存层）。"""
    results = await asyncio.gather(*[render_hifi_png("gantt", _PAYLOAD) for _ in range(4)])
    assert no_cache["n"] == 1
    assert len(set(results)) == 1
    assert get_hifi_render_stats()["inflight_shared"] == 3


def test_e1_public_surface_and_key_namespacing():
    """公开接口在场；hifi 缓存键带独立命名空间，不与 mermaid 轨 chart_type 互踩。"""
    assert "get_hifi_render_stats" in exporter.__all__
    key = _hifi_cache_key("gantt", _PAYLOAD, "print")
    plain = ChartCache._cache_key("gantt", "x")
    assert isinstance(key, str) and len(key) == len(plain)
    assert key != plain


def test_e2_stats_snapshot_is_isolated_copy():
    """get_hifi_render_stats 返回快照副本，外部改写不污染内部计数。"""
    snap = get_hifi_render_stats()
    snap["cache_hits"] = 10 ** 9
    assert get_hifi_render_stats()["cache_hits"] != 10 ** 9
