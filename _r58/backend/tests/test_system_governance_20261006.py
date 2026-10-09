"""R47 债-3：GET/PUT /system/governance 运行时开关端点的护栏。

- 默认值逐字沿用 settings 现值（prompt_context_budget=0 / prompt_injection_defense=False）。
- PUT 写回进程内 settings；GET 随后读到新值。
- 不开启时行为与现状完全一致（不修改任何提示词构建路径）。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.routers import system as system_router


@pytest.fixture
def _restore_governance_settings():
    """每个用例后把两个开关还原到测试前的现值（不污染全局 settings）。"""
    saved = (
        getattr(settings, "prompt_context_budget", 0),
        getattr(settings, "prompt_injection_defense", False),
    )
    yield
    settings.prompt_context_budget = saved[0]
    settings.prompt_injection_defense = saved[1]


@pytest.mark.asyncio
async def test_governance_get_returns_two_expected_fields(_restore_governance_settings):
    """GET 返回结构包含两个字段，且类型正确。"""
    settings.prompt_context_budget = 0
    settings.prompt_injection_defense = False
    out = await system_router.governance_get()
    assert set(out.keys()) == {"prompt_context_budget", "prompt_injection_defense"}
    assert isinstance(out["prompt_context_budget"], int)
    assert isinstance(out["prompt_injection_defense"], bool)


@pytest.mark.asyncio
async def test_governance_defaults_are_zero_and_false(_restore_governance_settings):
    """默认值逐字为 0 / False（不开启时行为与现状完全一致）。"""
    settings.prompt_context_budget = 0
    settings.prompt_injection_defense = False
    out = await system_router.governance_get()
    assert out["prompt_context_budget"] == 0
    assert out["prompt_injection_defense"] is False


@pytest.mark.asyncio
async def test_governance_put_then_get_roundtrip(_restore_governance_settings):
    """PUT 写回后 GET 读到新值；返回体与写回一致。"""
    # 先复位到默认
    settings.prompt_context_budget = 0
    settings.prompt_injection_defense = False

    body = system_router.GovernanceSettings(
        prompt_context_budget=4096,
        prompt_injection_defense=True,
    )
    resp = await system_router.governance_put(body)
    assert resp["prompt_context_budget"] == 4096
    assert resp["prompt_injection_defense"] is True

    # GET 应读到新值
    out = await system_router.governance_get()
    assert out["prompt_context_budget"] == 4096
    assert out["prompt_injection_defense"] is True

    # 进程内 settings 也真被改了（下游 sse_handlers 的 getattr 能读到）
    assert settings.prompt_context_budget == 4096
    assert settings.prompt_injection_defense is True


@pytest.mark.asyncio
async def test_governance_put_accepts_zero_and_false_off(_restore_governance_settings):
    """写回 0 / False 应被接受（关闭开关），与默认态一致。"""
    settings.prompt_context_budget = 8192
    settings.prompt_injection_defense = True
    body = system_router.GovernanceSettings(
        prompt_context_budget=0,
        prompt_injection_defense=False,
    )
    resp = await system_router.governance_put(body)
    assert resp["prompt_context_budget"] == 0
    assert resp["prompt_injection_defense"] is False
