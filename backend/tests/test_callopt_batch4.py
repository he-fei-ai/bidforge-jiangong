# -*- coding: utf-8 -*-
"""调用次数优化 · 第 4 批回归锁（2026-09-22）

覆盖：
  O7  正文链路并发默认 3 → 2（config 默认值 + 模块常量绑定一致）
  O6  批处理按 provider 成功率防御性降批（默认关闭 = 零行为变化）
"""
import pytest

import app.services.ai.provider_factory as pf
import app.services.consistency_scanner as cs
import app.routers.sse_handlers as sh
import app.services.repair_agent as ra
from app.config import Settings, settings
from app.services.crypto import encrypt_api_key


class TestO7ConcurrencyDefaults:
    def test_settings_defaults(self):
        s = Settings()
        assert s.outline_chapter_concurrency == 2
        assert s.consistency_scan_concurrency == 2
        assert s.consistency_repair_concurrency == 2

    def test_module_constants_follow_settings(self):
        assert sh.OUTLINE_CHAPTER_CONCURRENCY == 2
        assert cs.CONSISTENCY_SCAN_CONCURRENCY == 2
        assert ra.REPAIR_CONCURRENCY == 2


class TestO6BatchBySuccessRate:
    def _seed_rate(self, provider, ok, fail):
        pf._provider_reliability[provider] = {"ok": ok, "fail": fail}

    def test_off_is_noop(self):
        assert pf.get_effective_batch_size(4, "agnes") == 4

    def test_on_without_samples_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        assert pf.get_effective_batch_size(4, "unknown-p") == 4

    def test_on_no_provider_name_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        assert pf.get_effective_batch_size(4, "") == 4

    def test_on_high_rate_keeps_base(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        self._seed_rate("agnes", 17, 3)   # 85%
        assert pf.get_effective_batch_size(4, "agnes") == 4

    def test_on_low_rate_downgrades_to_single(self, monkeypatch):
        """低成功率 provider 强制逐章（防御批量失败回退放大）。"""
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        self._seed_rate("bad", 2, 18)     # 10%
        assert pf.get_effective_batch_size(12, "bad") == 1

    def test_threshold_zero_never_downgrades(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        monkeypatch.setattr(settings, "ai_batch_min_success_rate", 0.0)
        self._seed_rate("bad", 0, 20)
        assert pf.get_effective_batch_size(6, "bad") == 6

    async def test_async_entry_off_is_noop(self):
        assert await pf.effective_batch_size(4) == 4

    async def test_async_entry_with_low_rate(self, db_conn, monkeypatch):
        """端到端：低成功率主配置 → 批大小降为 1。"""
        async def _cfg():
            return {"provider_name": "bad"}

        monkeypatch.setattr(pf, "_load_active_config", _cfg)
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        pf._provider_reliability["bad"] = {"ok": 2, "fail": 18}
        assert await pf.effective_batch_size(4) == 1

    async def test_async_entry_swallows_config_errors(self, monkeypatch):
        """主配置读取失败 → 不干预（返回配置值），绝不因分级逻辑阻断生成。"""
        async def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(pf, "_load_active_config", _boom)
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)
        assert await pf.effective_batch_size(4) == 4


class TestO6ScanIntegration:
    async def test_low_rate_provider_uses_single_section_path(self, db_conn,
                                                              monkeypatch):
        """开关开启 + 低成功率 → 扫描走逐章路径（12 章 = 12 次调用，不批量）。"""
        for i in range(12):
            await db_conn.execute(
                "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
                " VALUES (?,?,?,?,1,?)",
                (f"s{i}", "sch1", f"第{i+1}章",
                 "基坑深度 18.5m，工期 120 日历天。" * 10, i))
        await db_conn.commit()
        batch_calls, single_calls = [], []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            batch_calls.append(len(sections))
            return {s["id"]: [] for s in sections}

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            single_calls.append(section["id"])
            return []

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", True)

        async def _cfg():
            return {"provider_name": "bad"}

        monkeypatch.setattr(pf, "_load_active_config", _cfg)
        pf._provider_reliability["bad"] = {"ok": 1, "fail": 9}   # 10%

        res = await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                                scheme_name="n", scheme_type="t",
                                use_cache=False)
        assert not batch_calls and len(single_calls) == 12, (
            "低成功率 provider 必须逐章扫描（防御批量回退放大）")
        assert res["sections"] == 12

    async def test_switch_off_keeps_batch_path(self, db_conn, monkeypatch):
        """开关关闭（默认）→ 批处理路径与现状逐字一致。"""
        for i in range(8):
            await db_conn.execute(
                "INSERT INTO sections (id, scheme_id, title, content, level, sort_order)"
                " VALUES (?,?,?,?,1,?)",
                (f"s{i}", "sch1", f"第{i+1}章",
                 "基坑深度 18.5m，工期 120 日历天。" * 10, i))
        await db_conn.commit()
        batch_calls = []

        async def fake_batch(*, sections, facts, project_docs, design_docs, standards):
            batch_calls.append(len(sections))
            return {s["id"]: [] for s in sections}

        async def fake_single(*, section, facts, project_docs, design_docs, standards):
            raise AssertionError("开关关闭时不得走单章路径")

        monkeypatch.setattr(cs, "ai_scan_batch", fake_batch)
        monkeypatch.setattr(cs, "ai_scan_section", fake_single)
        monkeypatch.setattr(cs, "CONSISTENCY_SCAN_BATCH_SIZE", 4)
        monkeypatch.setattr(settings, "ai_batch_by_success_rate", False)
        await cs.run_scan(db_conn, scheme_id="sch1", project_id="p1",
                          scheme_name="n", scheme_type="t", use_cache=False)
        assert batch_calls == [4, 4], "默认配置下批处理行为必须与现状一致"
