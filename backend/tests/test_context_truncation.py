# -*- coding: utf-8 -*-
"""正文生成上下文截断 helper 单测（默认关闭=向后兼容，2026-09-22）"""
import app.routers.sse_handlers as sh


def test_default_off_keeps_long_text(monkeypatch):
    """context_length_limit=0（默认）：超长文本原样返回，不影响既有行为。"""
    monkeypatch.setattr(sh.settings, "context_length_limit", 0)
    long = "x" * 5000
    assert sh._truncate_context(long) == long


def test_off_when_short(monkeypatch):
    monkeypatch.setattr(sh.settings, "context_length_limit", 100)
    assert sh._truncate_context("短文本") == "短文本"


def test_applies_boundary_truncation_when_over(monkeypatch):
    monkeypatch.setattr(sh.settings, "context_length_limit", 15)
    text = "第一句结束。第二句内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长。"
    out = sh._truncate_context(text)
    assert len(out) <= 15
    assert out.endswith("。")
