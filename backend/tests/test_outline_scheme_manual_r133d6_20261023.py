# -*- coding: utf-8 -*-
"""R133-D6 · 提纲方案手动指定开关护栏（2026-10-23）

引入依据：《标书智能体（2）》§二「选择依据：方案预估字数、模型最大输出
token、目录层级深度要求。程序自动判定，**也支持用户手动指定**」——
本软件以全局配置项形态落地（用户确认：不改前端、不加请求级参数）。

契约：
  - config.outline_scheme ∈ {"auto", "oneshot", "stepwise"}，默认 "auto"
    （默认行为与引入前逐字一致）；
  - 判定点唯一收口到 _use_stepwise_for_budget（sse_handlers 唯一调用点
    是生成主流程的 use_stepwise 赋值）；
  - 手动 "oneshot" 不摘除 outline_oneshot_fallback_stepwise 失败降级兜底
    （fail-soft 红线优先，配置注释已声明）；
  - 非法取值 fail-soft：import 期归一 + helper 未知值 fall-through 阈值判定。

运行：pytest tests/test_outline_scheme_manual_r133d6_20261023.py -q
"""
from __future__ import annotations

import pathlib

from app.config import settings
from app.routers import sse_handlers as sh

SRC = (pathlib.Path(sh.__file__).resolve())


class TestConfigDefault:
    def test_default_is_auto(self):
        assert settings.outline_scheme == "auto", "默认必须 auto（旧行为不变）"

    def test_oneshot_fallback_flag_not_affected(self):
        """手动开关不摘除失败降级兜底（fail-soft 红线）。"""
        assert hasattr(settings, "outline_oneshot_fallback_stepwise")


class TestSchemeHelper:
    def test_auto_follows_threshold(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "auto")
        th = sh.OUTLINE_STEPWISE_MIN_WORDS
        assert sh._use_stepwise_for_budget(th) is False, "等于阈值走一次性（>判定）"
        assert sh._use_stepwise_for_budget(th + 1) is True
        assert sh._use_stepwise_for_budget(th - 1) is False

    def test_forced_oneshot_beats_huge_budget(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "oneshot")
        assert sh._use_stepwise_for_budget(10_000_000) is False, \
            "手动 oneshot 必须覆盖长方案阈值判定"

    def test_forced_stepwise_beats_tiny_budget(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "stepwise")
        assert sh._use_stepwise_for_budget(0) is True, \
            "手动 stepwise 必须覆盖短方案阈值判定"

    def test_unknown_value_falls_back_to_threshold(self, monkeypatch):
        """helper 层 fail-soft：未知取值与 auto 等价（不炸、不恒定）。"""
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "bogus")
        th = sh.OUTLINE_STEPWISE_MIN_WORDS
        assert sh._use_stepwise_for_budget(th - 1) is False
        assert sh._use_stepwise_for_budget(th + 1) is True

    def test_import_time_normalization_is_dual_layer(self):
        """import 期常量必须是合法三值之一（非法已在加载期归一为 auto）。"""
        assert sh.OUTLINE_SCHEME in ("auto", "oneshot", "stepwise")


class TestWiring:
    def test_single_decision_point(self, monkeypatch):
        """判定点唯一收口：主流程必须经 _use_stepwise_for_budget，且全仓
        不得残留 `word_budget > OUTLINE_STEPWISE_MIN_WORDS` 的裸比较。"""
        src = SRC.read_text(encoding="utf-8")
        assert "use_stepwise = _use_stepwise_for_budget(word_budget)" in src, \
            "生成主流程必须改走唯一判定 helper"
        # 裸比较只允许出现在 helper 自身实现里
        bare = src.count("word_budget > OUTLINE_STEPWISE_MIN_WORDS")
        assert bare == 1, f"裸阈值比较应只剩 helper 内 1 处，实际 {bare} 处"

    def test_manual_override_is_logged(self, monkeypatch):
        """手动生效必须留痕（可观测性，区分自动判定结果）。"""
        src = SRC.read_text(encoding="utf-8")
        assert 'logger.info("提纲方案手动指定为 %s' in src
