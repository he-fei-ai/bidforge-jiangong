"""AI 配图导出期自动生成回归测试（v17，2026-09-22）。

产品约束：图表全部自动生成，不设置人工配图/生图功能和按钮。
```ai_image 占位块在导出准备阶段由 `_auto_generate_ai_image_blocks` 自动生成
真实图片并就地改写为 image 块；生成失败/缺 prompt/开关关闭时保持占位
（write_section 整块跳过、不占图号）。
"""
import json

import pytest

from app.routers.export import _auto_generate_ai_image_blocks


def _ai_block(code: str, title: str = "") -> dict:
    return {"type": "ai_image", "code": code, "title": title}


def _ai_code(prompt: str, title: str = "支护剖面图", style: str = "engineering_diagram") -> str:
    return json.dumps({"prompt": prompt, "style": style, "title": title},
                      ensure_ascii=False)


@pytest.fixture()
def fake_gen(monkeypatch):
    """替换图像生成引擎，记录调用参数并返回固定 URL。"""
    calls: list[tuple] = []

    async def _fake(provider, prompt, style, size, provider_name, image_config):
        calls.append((prompt, style))
        return f"http://img.example/{len(calls)}.png"

    monkeypatch.setattr(
        "app.services.ai.image_engine.generate_illustration_image", _fake)
    return calls


@pytest.mark.asyncio
async def test_ai_image_block_converted_to_image_on_export(fake_gen):
    """生成成功 → 占位块就地改写为 image 块（url/alt 齐备）。"""
    code = _ai_code("深基坑支护结构剖面")
    sections = [{"id": "s1", "content": "x"}]
    blocks_cache = {"s1": [_ai_block(code)]}
    n = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})
    assert n == 1
    block = blocks_cache["s1"][0]
    assert block["type"] == "image"
    assert block["url"].startswith("http://img.example/")
    assert block["alt"] == "支护剖面图"
    assert fake_gen and fake_gen[0][0] == "深基坑支护结构剖面"


@pytest.mark.asyncio
async def test_same_code_dedup_single_generation(fake_gen):
    """两个章节含同一占位码 → 只调用一次生成（避免重复计费）。"""
    code = _ai_code("同一张配图")
    blocks_cache = {
        "s1": [_ai_block(code)],
        "s2": [_ai_block(code)],
    }
    sections = [{"id": "s1"}, {"id": "s2"}]
    n = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})
    assert n == 2, "两个占位块都改写"
    assert len(fake_gen) == 1, "但只生成一次（缓存/去重）"


@pytest.mark.asyncio
async def test_generation_failure_keeps_placeholder(fake_gen, monkeypatch):
    """生成失败（返回 None）→ 块保持 ai_image 占位，导出时整块跳过不占图号。"""

    async def _fail(provider, prompt, style, size, provider_name, image_config):
        return None

    monkeypatch.setattr(
        "app.services.ai.image_engine.generate_illustration_image", _fail)
    code = _ai_code("会失败的配图")
    blocks_cache = {"s1": [_ai_block(code)]}
    sections = [{"id": "s1"}]
    n = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})
    assert n == 0
    assert blocks_cache["s1"][0]["type"] == "ai_image"


@pytest.mark.asyncio
async def test_switch_off_restores_legacy_behavior(fake_gen):
    """ai_image_auto_generate=False → 恢复旧行为：不生成、不改写（向后兼容开关）。"""
    code = _ai_code("不会触发的配图")
    blocks_cache = {"s1": [_ai_block(code)]}
    sections = [{"id": "s1"}]
    n = await _auto_generate_ai_image_blocks(
        sections, blocks_cache, {"ai_image_auto_generate": False})
    assert n == 0 and not fake_gen
    assert blocks_cache["s1"][0]["type"] == "ai_image"


@pytest.mark.asyncio
async def test_missing_prompt_skipped_without_call(fake_gen):
    """占位块缺 prompt → 不调用生成引擎、不改写（与登记侧删除语义一致）。"""
    code = json.dumps({"style": "engineering_diagram"}, ensure_ascii=False)
    blocks_cache = {"s1": [_ai_block(code)]}
    sections = [{"id": "s1"}]
    n = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})
    assert n == 0 and not fake_gen


@pytest.mark.asyncio
async def test_generation_concurrency_uses_configured_limit(monkeypatch):
    """并发上限必须读取 settings.image_max_concurrency，不能回退为硬编码 4。"""
    import asyncio

    from app.config import settings

    monkeypatch.setattr(settings, "image_max_concurrency", 1)
    active = 0
    peak = 0

    async def _tracked_gen(provider, prompt, style, size, provider_name, image_config):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return f"http://img.example/{prompt}.png"
        finally:
            active -= 1

    monkeypatch.setattr(
        "app.services.ai.image_engine.generate_illustration_image", _tracked_gen)
    blocks_cache = {
        f"s{i}": [_ai_block(_ai_code(f"配图 {i}"))] for i in range(4)
    }
    sections = [{"id": f"s{i}"} for i in range(4)]

    converted = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})

    assert converted == 4
    assert peak == 1, f"配置并发为 1，实际峰值为 {peak}"


@pytest.mark.asyncio
async def test_no_ai_blocks_is_noop(fake_gen):
    """无 ai_image 块（纯图表/纯文本方案）→ 零调用零开销。"""
    blocks_cache = {"s1": [{"type": "paragraph", "text": "正文"}]}
    sections = [{"id": "s1"}]
    n = await _auto_generate_ai_image_blocks(sections, blocks_cache, {})
    assert n == 0 and not fake_gen
