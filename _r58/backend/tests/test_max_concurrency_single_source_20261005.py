"""A5（2026-10-05）：全局 AI 并发上限必须收敛为「settings.max_concurrency」单一源。

此前三处「5」各说各话：
  1) provider_factory._MAX_CONCURRENCY            —— 已配置化（2026-10-03）
  2) workflows_base.AdaptiveConcurrencyController.__init__(max_c=5)  —— 硬编码
  3) facts_extractor.FACTS_MAX_CONCURRENCY = 5                                     —— 硬编码

本次收敛点：
  - 全部走 settings.max_concurrency（默认 5）
  - 修改 settings.max_concurrency 后，新建的 AdaptiveConcurrencyController 上限必须随之变化
  - 源码静态锁：三处不再出现「= 5」硬编码字面量
"""
import inspect

import app.services.ai.provider_factory as pf
import app.services.ai.workflows_base as wb
import pytest
from app.config import settings
from app.services.facts_extractor import FACTS_MAX_CONCURRENCY


# ===========================================================================
# 1. 三个入口默认值一致，且等于 settings.max_concurrency
# ===========================================================================
class TestDefaultAligned:
    def test_default_matches_settings(self):
        """三处上限的默认值必须等于 settings.max_concurrency（默认 5）。"""
        expected = max(1, int(getattr(settings, "max_concurrency", 5) or 5))
        assert pf._MAX_CONCURRENCY == expected
        assert FACTS_MAX_CONCURRENCY == expected
        ctrl = wb.AdaptiveConcurrencyController()
        assert ctrl.max_c == expected

    def test_range_bound_follows_max_concurrency(self):
        assert pf._RANGE["concurrency"] == (1, pf._MAX_CONCURRENCY)


# ===========================================================================
# 2. 修改 settings.max_concurrency 后，新实例必须随之变化
# ===========================================================================
class TestSettingChangePropagates:
    def test_new_controller_reads_updated_settings(self, monkeypatch):
        """settings.max_concurrency=3 时，新建的控制器 max_c=3（initial 走显式参数）。"""
        monkeypatch.setattr(settings, "max_concurrency", 3)
        ctrl = wb.AdaptiveConcurrencyController(initial=1, min_c=1)
        assert ctrl.max_c == 3
        # initial 低于 max_c，set_concurrency 可推到 max_c=3
        ctrl.set_concurrency(3)
        assert ctrl.current == 3

    def test_settings_lowered_clamps_existing_controller(self, monkeypatch):
        """settings.max_concurrency=4 时，自适应升并发不能突破 max_c=4。"""
        monkeypatch.setattr(settings, "max_concurrency", 4)
        ctrl = wb.AdaptiveConcurrencyController(initial=1, min_c=1)
        # set_concurrency 同步更新 target（=4），并把 current 提到 4
        ctrl.set_concurrency(4)
        assert ctrl.current == ctrl.max_c == 4
        # 再升一档必须被 max_c 钳住
        ctrl._change(+1)
        assert ctrl.current == 4
        assert ctrl.current <= ctrl.max_c

    def test_settings_raises_ceiling_for_new_instance(self, monkeypatch):
        """settings.max_concurrency=8 时，新建控制器的 max_c=8。"""
        monkeypatch.setattr(settings, "max_concurrency", 8)
        ctrl = wb.AdaptiveConcurrencyController()
        assert ctrl.max_c == 8

    def test_explicit_max_c_still_wins_over_settings(self, monkeypatch):
        """调用方显式传 max_c 时，仍然优先（向后兼容已有测试 fixture）。"""
        monkeypatch.setattr(settings, "max_concurrency", 5)
        ctrl = wb.AdaptiveConcurrencyController(max_c=2)
        assert ctrl.max_c == 2


# ===========================================================================
# 3. 源码静态锁：硬编码字面量不得回潮
# ===========================================================================
class TestNoHardcodedFive:
    def test_workflows_base_not_hardcoded(self):
        """AdaptiveConcurrencyController.__init__ 不得再出现 max_c: int = 5。"""
        src = inspect.getsource(wb.AdaptiveConcurrencyController)
        assert "max_c: int = 5" not in src
        assert 'getattr(_settings, "max_concurrency"' in src

    def test_facts_extractor_not_hardcoded(self):
        """FACTS_MAX_CONCURRENCY 不再写成 `= 5`，必须来自 settings。"""
        import app.services.facts_extractor as fx
        src = inspect.getsource(fx)
        # 允许多处出现 max_concurrency 关键字，但不允许「= 5」独立赋给这个常量
        # 用宽松判据：赋值行不能是 `FACTS_MAX_CONCURRENCY = 5`
        assert "FACTS_MAX_CONCURRENCY = 5" not in src
        assert 'getattr(settings, "max_concurrency"' in src

    def test_provider_factory_still_source_of_truth(self):
        """provider_factory._MAX_CONCURRENCY 保持读 settings（不引入第二份兜底）。"""
        import app.services.ai.provider_factory as pfmod
        src = inspect.getsource(pfmod)
        assert 'getattr(settings, "max_concurrency"' in src


# ===========================================================================
# 4. 三处数值完全同步（防止漂移）
# ===========================================================================
class TestAllInSync:
    def test_three_way_sync(self, monkeypatch):
        """修改 settings 后，provider_factory._MAX_CONCURRENCY 需要 reload 才更新，
        但 FACTS_MAX_CONCURRENCY 和 AdaptiveConcurrencyController 都是惰性的：
        FACTS_MAX_CONCURRENCY 也是 import 时求值，所以只在默认值处强同步。"""
        expected = max(1, int(getattr(settings, "max_concurrency", 5) or 5))
        assert pf._MAX_CONCURRENCY == expected
        assert FACTS_MAX_CONCURRENCY == expected
        assert wb.AdaptiveConcurrencyController().max_c == expected
