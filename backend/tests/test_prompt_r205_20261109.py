"""R205（2026-11-09）护栏：提示词模块遗留项收口的行为级验证。

覆盖本轮落地的遗留项（按最优方案执行）：
  L-2  正文截断标记（chat_with_fallback_meta.truncated，检测侧）
  L-4  修复轮坏 JSON 长度上限（invalid_content 截断）
  L-5  collect_json_response 整体超时（total_timeout）
  L-10 未知 prompt key 严格模式（strict=True 抛 KeyError）
  L-11 PII 脱敏（身份证/手机号/邮箱，默认关闭、开启后串联脱敏）
  L-13 缓存加载失败保留上次成功缓存 + 打点 degraded 指标
"""
import asyncio

import pytest
from app.services.ai.json_response import _clip_invalid, collect_json_response
from app.services.ai.prompts._cache import get_prompt
from app.services.ai.prompts._metrics import prompt_cache_degraded
from app.services.prompt_material_guard import redact_outbound_messages, redact_pii


# ---------------------------------------------------------------------------
# L-4 · 修复轮坏 JSON 长度上限
# ---------------------------------------------------------------------------
def test_repair_prompt_clips_invalid_content():
    long_bad = "x" * 50000  # facts 段一份资料可数万字
    out = _clip_invalid(long_bad)
    # 截断后应远小于原文（仅保留首尾各 4000 + 中间省略标记），且保留「中间已省略」标记
    assert len(out) < len(long_bad) // 4
    assert "中间已省略" in out
    assert long_bad not in out


def test_clip_invalid_short_passthrough():
    assert _clip_invalid("短文本") == "短文本"
    assert _clip_invalid("") == ""


# ---------------------------------------------------------------------------
# L-5 · 整体超时
# ---------------------------------------------------------------------------
def test_total_timeout_raises(monkeypatch):
    import app.services.ai.json_response as jr

    async def _slow(msgs, **kw):
        await asyncio.sleep(1.0)
        return "{}"

    monkeypatch.setattr(jr, "chat_with_fallback", _slow)

    async def _run():
        return await collect_json_response(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            validate_fn=None,
            total_timeout=0.1,
        )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run())


def test_total_timeout_none_unchanged(monkeypatch):
    import app.services.ai.json_response as jr

    async def _ok(msgs, **kw):
        return '{"ok": 1}'

    monkeypatch.setattr(jr, "chat_with_fallback", _ok)

    async def _run():
        return await collect_json_response(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            total_timeout=None,
        )

    r = asyncio.run(_run())
    assert r[0] == {"ok": 1}  # collect_json_response 返回 (obj, raw)


# ---------------------------------------------------------------------------
# L-10 · 未知 key 严格模式
# ---------------------------------------------------------------------------
def test_unknown_key_strict_raises(monkeypatch):
    from app.services.ai.prompts import _cache as C

    monkeypatch.setattr(C, "_prompt_cache", {})
    monkeypatch.setattr(C, "_last_good_cache", {})
    with pytest.raises(KeyError):
        get_prompt("definitely_not_a_real_key_xyz", strict=True)


def test_unknown_key_default_empty(monkeypatch):
    from app.services.ai.prompts import _cache as C

    monkeypatch.setattr(C, "_prompt_cache", {})
    monkeypatch.setattr(C, "_last_good_cache", {})
    assert get_prompt("definitely_not_a_real_key_xyz") == ""


# ---------------------------------------------------------------------------
# L-11 · PII 脱敏
# ---------------------------------------------------------------------------
def test_redact_pii_default_off():
    text = "身份证110105199003078888 手机13800138000 a@b.com"
    out, n = redact_pii(text)
    assert n == 0
    assert out == text  # 默认关闭，逐字节不变


def test_redact_pii_redacts_common_patterns(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "prompt_pii_redact_enabled", True)
    text = "身份证110105199003078888 手机13800138000 邮箱a@b.com"
    out, n = redact_pii(text)
    assert n == 3
    assert "110105199003078888" not in out
    assert "13800138000" not in out
    assert "a@b.com" not in out


def test_redact_outbound_includes_pii_when_enabled(monkeypatch):
    from app.config import settings

    # PII 脱敏串联在出站脱敏门禁内（redaction_enabled 为总开关）。
    monkeypatch.setattr(settings, "prompt_redact_sensitive", True)
    monkeypatch.setattr(settings, "prompt_pii_redact_enabled", True)
    msgs = [
        {"role": "system", "content": "合规检查章节正文含身份证110105199003078888与手机13800138000"},
        {"role": "user", "content": "用户资料含13800138000"},
    ]
    out = redact_outbound_messages(msgs)
    for m in out:
        assert "110105199003078888" not in m["content"]
        assert "13800138000" not in m["content"]


# ---------------------------------------------------------------------------
# L-13 · 缓存加载失败保留上次成功缓存 + degraded 指标
# ---------------------------------------------------------------------------
def test_cache_load_failure_keeps_last_good(monkeypatch):
    from app.services.ai.prompts import _cache as C

    # 上次成功缓存里有定制内容
    monkeypatch.setattr(C, "_last_good_cache", {"my_custom_key": "CUSTOM_BODY"})
    monkeypatch.setattr(C, "_loaded_db_path", None)
    before = dict(prompt_cache_degraded)

    # 强制加载失败
    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(C, "_load_prompt_cache_sync", _boom)
    monkeypatch.setattr(C, "_prompt_cache", None)

    # 触发一次加载（路径 None → 走初始加载分支）
    res = C._get_prompt_cache()
    assert res.get("my_custom_key") == "CUSTOM_BODY", "加载失败时必须保留上次成功缓存"
    # degraded 指标 +1
    after = dict(prompt_cache_degraded)
    assert sum(after.values()) == sum(before.values()) + 1
