"""OmpProvider（阶段 1 骨架）单元测试。

只覆盖纯逻辑（prompt 渲染、RPC 帧文本提取）、能力声明与两道安全闸门
（未启用 / 二进制缺失均抛错且不拉起子进程）。不依赖真实 omp 或真实模型。
"""

import asyncio

import app.services.ai.provider_factory as pf
import pytest
from app.services.ai.providers.omp_provider import (
    OmpProvider,
    _extract_assistant_text,
    _render_prompt,
)


# ===================== 纯逻辑（同步） =====================
def test_render_prompt_basic():
    out = _render_prompt(
        [
            {"role": "system", "content": "你是投标专家"},
            {"role": "user", "content": "写一段引言"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续"},
        ]
    )
    assert "System instructions:" in out
    assert "你是投标专家" in out
    assert "User: 写一段引言" in out
    assert "Assistant: 好的" in out
    assert "User: 继续" in out
    # 非 json_mode 不应出现 JSON 指令
    assert "valid JSON object" not in out


def test_render_prompt_json_mode():
    out = _render_prompt([{"role": "user", "content": "返回配置"}], json_mode=True)
    assert "valid JSON object only" in out
    assert "markdown code fences" in out


def test_render_prompt_multimodal_content():
    # content 为 parts 列表时仅取 text 部分
    out = _render_prompt(
        [{"role": "user", "content": [{"type": "text", "text": "正文"}, {"type": "image_url", "image_url": {"url": "x"}}]}]
    )
    assert "正文" in out


def test_extract_assistant_text_stage0_shapes():
    frames = [
        {"type": "available_commands_update"},
        {"type": "message_start", "message": {"role": "assistant", "content": [{"type": "text", "text": "你好"}]}},
        {"type": "message_update", "content": [{"type": "text", "text": "，世界"}]},
        # 工具调用帧（无 text）必须被忽略
        {"type": "message_start", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "write", "arguments": {}}]}},
        {"type": "prompt_result", "id": "p1", "status": "completed", "message": {"role": "assistant", "content": [{"type": "text", "text": "！"}]}},
    ]
    assert _extract_assistant_text(frames) == "你好，世界！"


def test_extract_assistant_text_plain_string_content():
    frames = [{"type": "message_update", "content": "直接字符串"}]
    assert _extract_assistant_text(frames) == "直接字符串"


# ===================== 能力声明 =====================
def test_supports_vision_false():
    p = OmpProvider(api_key="", base_url="", model="openai/gpt-4o")
    assert p.supports_vision() is False


# ===================== 安全闸门 =====================
async def test_chat_disabled_raises(monkeypatch):
    monkeypatch.setattr(pf.settings, "omp_provider_enabled", False)
    # 即使二进制不存在，也应因「未启用」在二进制检查之前抛错
    monkeypatch.setattr(pf.settings, "omp_binary_path", "definitely_not_a_real_binary_xyz")
    p = OmpProvider(api_key="", base_url="", model="openai/gpt-4o")
    with pytest.raises(RuntimeError, match="未启用"):
        await p.chat([{"role": "user", "content": "hi"}])


async def test_chat_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(pf.settings, "omp_provider_enabled", True)
    monkeypatch.setattr(pf.settings, "omp_binary_path", "definitely_not_a_real_binary_xyz")
    p = OmpProvider(api_key="", base_url="", model="openai/gpt-4o")
    with pytest.raises(RuntimeError, match="可执行文件不存在"):
        await p.chat([{"role": "user", "content": "hi"}])


# ===================== provider_factory 接线 =====================
def test_omp_preset_registered():
    assert "omp" in pf.PROVIDER_PRESETS
    assert pf.PROVIDER_PRESETS["omp"]["type"] == "omp"


def test_build_provider_omp_returns_omp_provider():
    p = pf._build_provider("omp", "sk-x", "", "openai/gpt-4o")
    assert isinstance(p, OmpProvider)
    # omp 允许空 Key；非 omp 空 Key 仍应报错（回归保护）
    p2 = pf._build_provider("omp", "", "", "openai/gpt-4o")
    assert isinstance(p2, OmpProvider)
    with pytest.raises(ValueError, match="API Key 为空"):
        pf._build_provider("deepseek", "", "", "deepseek-chat")


def test_build_provider_omp_default_model():
    p = pf._build_provider("omp", "sk-x", "", "")
    assert p.model == pf.PROVIDER_PRESETS["omp"]["model"]
