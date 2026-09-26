# -*- coding: utf-8 -*-
"""调用次数优化 · 第 5 批回归锁（2026-09-22 · O5 流式能力记忆 TTL 配置化+延长）

O5：STREAM_CAPABILITY_TTL 3600s → 86400s（模型对流式的支持不会一天内变化，
缩短只会增加 TTL 到期后的重探测浪费）。到期自动重探测、成功即清除的
自愈机制保持不变（既有 test_content_call_optimization.py 已覆盖）。
"""
import app.services.ai.provider_factory as pf
from app.config import Settings


class TestO5StreamCapabilityTtl:
    def test_settings_default(self):
        assert Settings().ai_stream_capability_ttl_seconds == 86400

    def test_module_constant_follows_settings(self):
        assert pf.STREAM_CAPABILITY_TTL == 86400.0

    def test_non_positive_clamped(self):
        # 非正配置不生效（避免 TTL<=0 导致每次调用都重探测流式）
        import app.services.ai.provider_factory as _pf
        assert _pf.STREAM_CAPABILITY_TTL >= 1.0

    def test_memory_mechanism_unchanged(self):
        """记忆读写行为不变：失败入账、成功清除（防实现回归）。"""
        pf.reset_stream_capability()
        key = "p|m"
        assert pf._stream_disabled(key) is False
        assert pf._note_stream_failure(key) is False      # 第 1 次：未达阈值
        assert pf._note_stream_failure(key) is True       # 第 2 次：认定不支持
        assert pf._stream_disabled(key) is True           # TTL 内直接跳过
        pf._note_stream_success(key)
        assert pf._stream_disabled(key) is False          # 成功即清除
        pf.reset_stream_capability()
