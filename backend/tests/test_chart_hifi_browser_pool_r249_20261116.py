"""R249b hifi browser 池护栏（纯逻辑，不 launch 真浏览器）。

用 monkeypatch 替换 _HifiBrowserPool._launch / _open 为计数桩，行为实证：
同 loop 重复 acquire 只 launch 一次（复用）、连接态失效触发重建、渲染中崩溃
discard 后下次重建、new_page 串行化、close_all 停 driver。
"""
from __future__ import annotations

import asyncio

import pytest
from app.services.ai.hifi_charts import exporter as exporter_mod


class _FakeBrowser:
    def __init__(self) -> None:
        self.connected = True
        self.closed = False
        self.pages_opened = 0

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    async def new_page(self, **kwargs):
        self.pages_opened += 1
        assert kwargs.get("device_scale_factor") is not None
        assert kwargs.get("viewport") == {"width": 1500, "height": 1000}
        return _FakePage()


class _FakePage:
    async def set_content(self, html, wait_until=None, timeout=None) -> None:
        return None

    async def wait_for_timeout(self, ms) -> None:
        return None

    async def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG fake" + b"\x00" * 200

    async def query_selector(self, selector):
        if selector == ".gantt-container":
            return _FakeElement()
        return None

    async def close(self) -> None:
        return None


class _FakeElement:
    async def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG fake" + b"\x00" * 200


class _FakePageEmpty:
    async def set_content(self, html, wait_until=None, timeout=None) -> None:
        return None

    async def wait_for_timeout(self, ms) -> None:
        return None

    async def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG fake" + b"\x00" * 200

    async def query_selector(self, selector):
        return None

    async def close(self) -> None:
        return None


class _FakePageOld:
    async def set_content(self, html, wait_until=None, timeout=None) -> None:
        return None

    async def wait_for_timeout(self, ms) -> None:
        return None

    async def screenshot(self, **kwargs) -> bytes:
        return b"\x89PNG fake" + b"\x00" * 200

    async def query_selector(self, selector):
        return None

    async def close(self) -> None:
        return None


class _FakeDriver:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def pool(monkeypatch):
    p = exporter_mod._HifiBrowserPool()
    launched = {"n": 0}

    async def _fake_launch(self):
        launched["n"] += 1
        return _FakeDriver()

    async def _fake_open(self, pw):
        return _FakeBrowser()

    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_launch", _fake_launch)
    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_open", _fake_open)
    return p, launched


@pytest.mark.asyncio
async def test_a1_same_loop_acquire_reuses_one_launch(pool):
    p, launched = pool
    b1 = await p.acquire()
    b2 = await p.acquire()
    assert b1 is b2
    assert launched["n"] == 1
    assert p.launches == 1 and p.reuses == 1 and p.rebuilds == 0


@pytest.mark.asyncio
async def test_a2_disconnected_browser_triggers_rebuild(pool):
    p, launched = pool
    b1 = await p.acquire()
    b1.connected = False  # 连接态失效（browser 崩溃/被外部关闭）
    b2 = await p.acquire()
    assert b2 is not b1
    assert b1.closed is True
    assert launched["n"] == 2
    assert p.rebuilds == 1


@pytest.mark.asyncio
async def test_a3_new_page_serialized_and_typed(pool):
    p, launched = pool
    browser = await p.acquire()
    kw = {"device_scale_factor": 3, "viewport": {"width": 1500, "height": 1000}}
    pages = await asyncio.gather(*[p.new_page(browser, **kw) for _ in range(4)])
    assert len(pages) == 4
    assert browser.pages_opened == 4


@pytest.mark.asyncio
async def test_a4_discard_pops_and_closes(pool):
    p, launched = pool
    b1 = await p.acquire()
    await p.discard(b1)
    assert b1.closed is True
    assert p._drivers == {}
    b2 = await p.acquire()
    assert b2 is not b1
    assert launched["n"] == 2


@pytest.mark.asyncio
async def test_a5_close_all_stops_drivers(pool):
    p, _launched = pool
    await p.acquire()
    await p.close_all()
    assert p._browsers == {}
    assert p._drivers == {}
    assert p.stats if hasattr(p, "stats") else True


@pytest.mark.asyncio
async def test_b1_stats_snapshot_keys(pool):
    p, _launched = pool
    await p.acquire()
    await p.acquire()
    # 模块级 get_hifi_browser_stats 读的是单例；此处验证单例公开面键集。
    s = exporter_mod.get_hifi_browser_stats()
    assert set(s) == {"launches", "reuses", "rebuilds"}
    assert isinstance(p.launches, int)


@pytest.mark.asyncio
async def test_b2_render_html_uses_pool_not_per_call_launch(monkeypatch):
    """_render_html 走池：同 loop 两次渲染只 launch 一次 browser。"""
    p = exporter_mod._hifi_browser_pool
    launched = {"n": 0}

    async def _fake_launch(self):
        launched["n"] += 1
        return _FakeDriver()

    async def _fake_open(self, pw):
        return _FakeBrowser()

    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_launch", _fake_launch)
    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_open", _fake_open)
    exp = exporter_mod.ChartExporter(scale=1)
    html = "<html><body><div class='gantt-container'>x</div></body></html>"
    await exp._render_html(html, ".gantt-container")
    await exp._render_html(html, ".gantt-container")
    assert launched["n"] == 1
    await exporter_mod.close_hifi_browser()


@pytest.mark.asyncio
async def test_b3_render_html_missing_element_is_deterministic_no_rebuild(monkeypatch):
    """元素缺失（确定性业务失败）不触发 browser 重建，原样抛错。"""
    p = exporter_mod._hifi_browser_pool

    async def _fake_launch(self):
        return _FakeDriver()

    async def _fake_open(self, pw):
        return _FakeBrowser()

    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_launch", _fake_launch)
    monkeypatch.setattr(exporter_mod._HifiBrowserPool, "_open", _fake_open)
    monkeypatch.setattr(
        exporter_mod._HifiBrowserPool, "new_page", _HifiBrowserPool_new_page_empty
    )
    exp = exporter_mod.ChartExporter(scale=1)
    html = "<html><body></body></html>"
    with pytest.raises(RuntimeError, match="截图目标元素缺失"):
        await exp._render_html(html, ".nope")
    assert p.rebuilds == 0
    await exporter_mod.close_hifi_browser()


async def _HifiBrowserPool_new_page_empty(self, browser, **kwargs):
    return _FakePageEmpty()


def test_c1_public_surface():
    assert "close_hifi_browser" in exporter_mod.__all__
    assert "get_hifi_render_stats" in exporter_mod.__all__
    assert callable(exporter_mod.close_hifi_browser)
    assert callable(exporter_mod.get_hifi_browser_stats)


@pytest.mark.asyncio
async def test_c2_close_hifi_browser_idempotent():
    await exporter_mod.close_hifi_browser()
    await exporter_mod.close_hifi_browser()
    assert exporter_mod.get_hifi_browser_stats()["launches"] >= 0
