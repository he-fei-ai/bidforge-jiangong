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
    # ✅ R204（2026-11-09 · R202 遗留漂移收口）：R202 把「静默截断」改成
    #    「模型可见截断说明」，输出长度 = 保留正文 + 说明文本，故不能再断言
    #    整体长度 <= limit（旧断言自 R202 起恒红，本轮一并修正）。
    #    真正的护栏是：**保留的正文**仍受 limit 约束，且**必须**带截断说明
    #    （模型据此知道资料被裁过，不会断言「资料未提及」）。
    assert len(out) > 15  # 说明文本追加在保留正文之后
    assert "已截断" in out
    assert out.startswith("第一句结束。")
    kept_body = out.split("\n\n", 1)[0]
    assert len(kept_body) <= 15
    assert kept_body.endswith("。")
