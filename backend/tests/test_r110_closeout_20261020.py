# -*- coding: utf-8 -*-
"""R110 closeout guards.

P1-1: 跨章全熔断快速失败（provider_factory._gate_candidates 连续 N 次所有候选不可用后短时快速失败）
P1-2: sensetime 思考吞噬翻倍失败后 provider 短时冷却（避免降级链重复"吞噬→翻倍→失败"）

承接 R109 §五登记的 2 项 P1。
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
PF_PATH = BACKEND / "app" / "services" / "ai" / "provider_factory.py"


# ---------- fixtures: 每个测试前重置模块级状态 ----------

@pytest.fixture(autouse=True)
def _reset_r110_state():
    """每个测试前清空 thinking-dead 与 all-circuit-fast-fail 状态。"""
    from app.services.ai import provider_factory as pf
    pf.reset_thinking_dead()
    pf.reset_all_circuit_fast_fail()
    yield
    pf.reset_thinking_dead()
    pf.reset_all_circuit_fast_fail()


# =====================================================================
# P1-2: thinking-dead cooldown（思考吞噬翻倍失败后 provider 短时冷却）
# =====================================================================

class TestThinkingDeadHelpers:
    """_note_thinking_dead / _is_thinking_dead / clear_thinking_dead 单元契约。"""

    def test_note_marks_provider(self):
        from app.services.ai.provider_factory import _is_thinking_dead, _note_thinking_dead
        assert not _is_thinking_dead("sensetime")
        _note_thinking_dead("sensetime")
        assert _is_thinking_dead("sensetime")
        # 其他 provider 不受影响
        assert not _is_thinking_dead("agnes")

    def test_clear_specific_provider(self):
        from app.services.ai.provider_factory import _is_thinking_dead, _note_thinking_dead, clear_thinking_dead
        _note_thinking_dead("sensetime")
        _note_thinking_dead("deepseek")
        clear_thinking_dead("sensetime")
        assert not _is_thinking_dead("sensetime")
        assert _is_thinking_dead("deepseek")

    def test_clear_all(self):
        from app.services.ai.provider_factory import _is_thinking_dead, _note_thinking_dead, clear_thinking_dead
        _note_thinking_dead("sensetime")
        _note_thinking_dead("deepseek")
        clear_thinking_dead()
        assert not _is_thinking_dead("sensetime")
        assert not _is_thinking_dead("deepseek")

    def test_ttl_expiry(self, monkeypatch):
        """冷却到期后 _is_thinking_dead 返回 False 并清理过期项。"""
        from app.services.ai import provider_factory as pf
        # 用极短 TTL 标记
        pf._thinking_dead_until["sensetime"] = time.time() - 0.001  # 已过期
        assert not pf._is_thinking_dead("sensetime")
        # 过期项应被清理（dict 不残留）
        assert "sensetime" not in pf._thinking_dead_until

    def test_disabled_when_cooldown_zero(self, monkeypatch):
        """THINKING_DEAD_COOLDOWN=0 时 _note_thinking_dead 空操作。"""
        from app.services.ai import provider_factory as pf
        monkeypatch.setattr(pf, "THINKING_DEAD_COOLDOWN", 0.0)
        pf._note_thinking_dead("sensetime")
        assert not pf._is_thinking_dead("sensetime")
        assert "sensetime" not in pf._thinking_dead_until

    def test_empty_pname_is_noop(self):
        from app.services.ai.provider_factory import _note_thinking_dead
        _note_thinking_dead("")  # 不应抛
        _note_thinking_dead(None)  # type: ignore[arg-type]


class TestGateCandidatesSkipsThinkingDead:
    """_gate_candidates 必须跳过 thinking-dead 的 provider。"""

    def _candidate(self, pname="sensetime", with_key=True):
        return {
            "provider_name": pname,
            "api_key": "sk-test" if with_key else "",
            "base_url": "https://example.com",
            "model": "test-model",
            "config_id": "c1",
        }

    @pytest.fixture(autouse=True)
    def _mock_gate_deps(self, monkeypatch):
        """桩掉 _gate_candidates 的 DB/异步依赖避免挂起。"""
        from app.services.ai import provider_factory as pf
        async def _no_disabled():
            return set()
        monkeypatch.setattr(pf, "resolve_disabled_providers", _no_disabled)
        async def _no_audit(*a, **k):
            pass
        monkeypatch.setattr(pf, "_log_audit", _no_audit)

    @pytest.mark.asyncio
    async def test_thinking_dead_provider_skipped(self):
        from app.services.ai.provider_factory import _gate_candidates, _note_thinking_dead
        _note_thinking_dead("sensetime")
        # 全部候选处于思考吞噬冷却 → gate 抛「所有 AI 提供商调用失败」，
        # 末条错误信息点名「思考吞噬冷却」。
        import pytest as _pt
        with _pt.raises(RuntimeError) as ei:
            await _gate_candidates([self._candidate("sensetime")], "test")
        assert "思考吞噬" in str(ei.value)

    @pytest.mark.asyncio
    async def test_non_dead_provider_passes(self):
        from app.services.ai.provider_factory import _gate_candidates
        usable, err = await _gate_candidates([self._candidate("agnes")], "test")
        assert len(usable) == 1
        assert usable[0]["provider_name"] == "agnes"

    @pytest.mark.asyncio
    async def test_mix_dead_and_alive(self):
        from app.services.ai.provider_factory import _gate_candidates, _note_thinking_dead
        _note_thinking_dead("sensetime")
        cands = [self._candidate("sensetime"), self._candidate("agnes")]
        usable, _ = await _gate_candidates(cands, "test")
        # 死候选被跳过，存活候选放行
        assert len(usable) == 1
        assert usable[0]["provider_name"] == "agnes"


class TestCallOneCandidateNotesThinkingDead:
    """_call_one_candidate 翻倍失败后必须 _note_thinking_dead。"""

    @pytest.mark.asyncio
    async def test_notes_after_doubled_retry_fails(self, monkeypatch):
        """翻倍重试后仍思考吞噬 → 标记 thinking-dead。"""
        import threading

        from app.services.ai import provider_factory as pf

        c = {
            "provider_name": "sensetime",
            "api_key": "sk-test",
            "base_url": "https://example.com",
            "model": "test-model",
            "config_id": "c1",
            "max_tokens": 1536,
            "temperature": 0.7,
        }
        # 桩 _build_provider 返回简单对象（只需 last_finish_reason 属性）
        class _FakeProv:
            last_finish_reason = "length"
        monkeypatch.setattr(pf, "_build_provider",
                            lambda *a, **k: _FakeProv())

        # 桩 _call_provider 恒抛思考吞噬异常
        async def _always_thinking_exhausted(*a, **k):
            raise RuntimeError("finish_reason=length，正文为空")
        monkeypatch.setattr(pf, "_call_provider", _always_thinking_exhausted)

        # 桩 audit 日志、熔断器、并发控制器、实时状态避免副作用与死锁
        async def _no_audit(*a, **k):
            pass
        monkeypatch.setattr(pf, "_log_audit", _no_audit)
        monkeypatch.setattr(pf.circuit_breaker, "record_failure", lambda *a, **k: None)

        class _NoOpSem:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class _NoOpCtrl:
            """桩并发控制器：信号量直通 + 采样/调整空操作。"""
            semaphore = _NoOpSem()

            def record(self, *a, **k):
                pass

            def adjust_concurrency(self, *a, **k):
                pass
        monkeypatch.setattr(pf, "concurrency_controller", _NoOpCtrl())
        monkeypatch.setattr(pf, "_ai_live_lock", threading.Lock())
        monkeypatch.setattr(pf, "_ai_live", {
            "in_flight": 0, "total": 0, "failed": 0,
            "last_provider": "", "last_model": "", "last_ok": True,
            "last_duration": 0.0, "last_at": 0.0,
        })
        monkeypatch.setattr(pf, "_ab", type("X", (), {"notify": lambda *a: None})())

        result, err = await pf._attempt_candidate(
            c, [], temperature=0.7, json_mode=False, max_tokens=1536,
            req_timeout=30, scene="test", extra_body=None)
        # 翻倍重试一次后仍失败 → 返回空 + 异常
        assert result == ""
        assert err is not None
        # ✅ 关键断言：该 provider 已被标记 thinking-dead
        assert pf._is_thinking_dead("sensetime")

    @pytest.mark.asyncio
    async def test_clears_on_success(self, monkeypatch):
        """成功调用 → clear_thinking_dead 清除标记。"""
        import threading

        from app.services.ai import provider_factory as pf

        # 预先标记
        pf._note_thinking_dead("sensetime")
        assert pf._is_thinking_dead("sensetime")

        c = {
            "provider_name": "sensetime",
            "api_key": "sk-test",
            "base_url": "https://example.com",
            "model": "test-model",
            "config_id": "c1",
            "max_tokens": 1536,
            "temperature": 0.7,
        }
        class _FakeProv:
            last_finish_reason = "stop"
        monkeypatch.setattr(pf, "_build_provider", lambda *a, **k: _FakeProv())

        async def _success(*a, **k):
            return "正文内容"
        monkeypatch.setattr(pf, "_call_provider", _success)
        async def _no_audit(*a, **k):
            pass
        monkeypatch.setattr(pf, "_log_audit", _no_audit)
        monkeypatch.setattr(pf.circuit_breaker, "record_success", lambda *a, **k: None)
        monkeypatch.setattr(pf, "_note_quota_success", lambda *a, **k: None)
        monkeypatch.setattr(pf, "extract_usage", lambda *a, **k: {
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})

        class _NoOpSem:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class _NoOpCtrl:
            """桩并发控制器：信号量直通 + 采样/调整空操作。"""
            semaphore = _NoOpSem()

            def record(self, *a, **k):
                pass

            def adjust_concurrency(self, *a, **k):
                pass
        monkeypatch.setattr(pf, "concurrency_controller", _NoOpCtrl())
        monkeypatch.setattr(pf, "_ai_live_lock", threading.Lock())
        monkeypatch.setattr(pf, "_ai_live", {
            "in_flight": 0, "total": 0, "failed": 0,
            "last_provider": "", "last_model": "", "last_ok": True,
            "last_duration": 0.0, "last_at": 0.0,
        })
        monkeypatch.setattr(pf, "_ab", type("X", (), {"notify": lambda *a: None})())

        result, err = await pf._attempt_candidate(
            c, [], temperature=0.7, json_mode=False, max_tokens=1536,
            req_timeout=30, scene="test", extra_body=None)
        assert result == "正文内容"
        assert err is None
        # ✅ 成功后 thinking-dead 已清除
        assert not pf._is_thinking_dead("sensetime")


# =====================================================================
# P1-1: all-circuit-fast-fail（跨章全熔断快速失败）
# =====================================================================

class TestAllCircuitFastFail:
    """_record_all_circuit_outcome / _is_all_circuit_fast_fail 契约。"""

    def test_below_threshold_not_active(self):
        from app.services.ai.provider_factory import _is_all_circuit_fast_fail, _record_all_circuit_outcome
        _record_all_circuit_outcome(success=False)
        assert not _is_all_circuit_fast_fail()  # 仅 1 次，未达阈值 2

    def test_activates_at_threshold(self):
        from app.services.ai.provider_factory import _is_all_circuit_fast_fail, _record_all_circuit_outcome
        _record_all_circuit_outcome(success=False)
        _record_all_circuit_outcome(success=False)
        assert _is_all_circuit_fast_fail()

    def test_success_resets(self):
        from app.services.ai.provider_factory import _is_all_circuit_fast_fail, _record_all_circuit_outcome
        _record_all_circuit_outcome(success=False)
        _record_all_circuit_outcome(success=False)
        assert _is_all_circuit_fast_fail()
        _record_all_circuit_outcome(success=True)
        assert not _is_all_circuit_fast_fail()

    def test_reset_clears_state(self):
        from app.services.ai.provider_factory import (
            _is_all_circuit_fast_fail,
            _record_all_circuit_outcome,
            reset_all_circuit_fast_fail,
        )
        _record_all_circuit_outcome(success=False)
        _record_all_circuit_outcome(success=False)
        reset_all_circuit_fast_fail()
        assert not _is_all_circuit_fast_fail()

    def test_disabled_when_threshold_zero(self, monkeypatch):
        from app.services.ai import provider_factory as pf
        monkeypatch.setattr(pf, "ALL_CIRCUIT_FAST_FAIL_THRESHOLD", 0)
        # threshold=0 时 _record_all_circuit_outcome 空操作
        pf._record_all_circuit_outcome(success=False)
        pf._record_all_circuit_outcome(success=False)
        pf._record_all_circuit_outcome(success=False)
        assert not pf._is_all_circuit_fast_fail()


class TestGateCandidatesFastFail:
    """_gate_candidates 在全熔断快速失败窗口内直接 raise。"""

    def _candidate(self, pname="sensetime"):
        return {
            "provider_name": pname,
            "api_key": "sk-test",
            "base_url": "https://example.com",
            "model": "test-model",
            "config_id": "c1",
        }

    @pytest.fixture(autouse=True)
    def _mock_gate_deps(self, monkeypatch):
        """桩掉 _gate_candidates 的 DB/异步依赖避免挂起。"""
        from app.services.ai import provider_factory as pf
        async def _no_disabled():
            return set()
        monkeypatch.setattr(pf, "resolve_disabled_providers", _no_disabled)
        async def _no_audit(*a, **k):
            pass
        monkeypatch.setattr(pf, "_log_audit", _no_audit)

    @pytest.mark.asyncio
    async def test_raises_when_fast_fail_active(self):
        from app.services.ai.provider_factory import _gate_candidates, _record_all_circuit_outcome
        # 激活快速失败
        _record_all_circuit_outcome(success=False)
        _record_all_circuit_outcome(success=False)
        with pytest.raises(RuntimeError, match="全熔断快速失败"):
            await _gate_candidates([self._candidate()], "test")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_inactive(self):
        from app.services.ai.provider_factory import _gate_candidates
        # 未激活 → 正常 gate（候选有 key → 放行）
        usable, _ = await _gate_candidates([self._candidate()], "test")
        assert len(usable) == 1


# =====================================================================
# AST 静态锁：接线必须在位（防后人误删）
# =====================================================================

def _src():
    return PF_PATH.read_text(encoding="utf-8")


class TestStaticWiringP1_2:
    """P1-2 接线 AST 锁。"""

    def test_note_thinking_dead_called_in_attempt_candidate(self):
        """_attempt_candidate 内翻倍失败后必须调 _note_thinking_dead。"""
        tree = ast.parse(_src())
        # 找 _attempt_candidate 函数
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "_attempt_candidate":
                target = node
                break
        assert target is not None, "_attempt_candidate 函数不存在"
        # 在函数体内找 _note_thinking_dead 调用
        found = False
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "_note_thinking_dead":
                    found = True
                    break
        assert found, "_call_one_candidate 内未调 _note_thinking_dead（P1-2 接线缺失）"

    def test_is_thinking_dead_check_in_gate_candidates(self):
        """_gate_candidates 内必须检查 _is_thinking_dead。"""
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "_gate_candidates":
                target = node
                break
        assert target is not None
        found = False
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "_is_thinking_dead":
                    found = True
                    break
        assert found, "_gate_candidates 内未检查 _is_thinking_dead"

    def test_clear_thinking_dead_in_success_path(self):
        """成功路径必须调 clear_thinking_dead。"""
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "_attempt_candidate":
                target = node
                break
        assert target is not None
        found = False
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "clear_thinking_dead":
                    found = True
                    break
        assert found, "成功路径未调 clear_thinking_dead"


class TestStaticWiringP1_1:
    """P1-1 接线 AST 锁。"""

    def test_fast_fail_check_in_gate_candidates(self):
        """_gate_candidates 顶部必须检查 _is_all_circuit_fast_fail。"""
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "_gate_candidates":
                target = node
                break
        assert target is not None
        found = False
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "_is_all_circuit_fast_fail":
                    found = True
                    break
        assert found, "_gate_candidates 顶部未检查 _is_all_circuit_fast_fail"

    def test_record_outcome_in_gate_candidates(self):
        """_gate_candidates 内必须调 _record_all_circuit_outcome（success=True 与 False 两处）。"""
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "_gate_candidates":
                target = node
                break
        assert target is not None
        calls = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "_record_all_circuit_outcome":
                    calls.append(node)
        # 必须同时存在 success=True 与 success=False 两处调用
        # （只有 True 没有 False = 空 usable 不计入全熔断计数，快速失败永不激活）
        have_true = have_false = False
        for c in calls:
            for kw in c.keywords:
                if kw.arg == "success" and isinstance(kw.value, ast.Constant):
                    if kw.value.value is True:
                        have_true = True
                    elif kw.value.value is False:
                        have_false = True
        assert have_true and have_false, (
            f"_gate_candidates 内 _record_all_circuit_outcome 调用不完整"
            f"（total={len(calls)} true={have_true} false={have_false}，"
            "须含 success=True 与 success=False 两处）")

    def test_record_success_in_chat_with_fallback(self):
        """chat_with_fallback 成功路径必须调 _record_all_circuit_outcome(success=True)。"""
        tree = ast.parse(_src())
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name == "chat_with_fallback":
                target = node
                break
        assert target is not None
        found = False
        for node in ast.walk(target):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "_record_all_circuit_outcome":
                    found = True
                    break
        assert found, "chat_with_fallback 成功路径未调 _record_all_circuit_outcome"


# =====================================================================
# 配置项登记锁
# =====================================================================

class TestConfigDeclaration:
    """新增配置项必须在 config.py 声明（环境变量可配）。"""

    def test_thinking_dead_cooldown_declared(self):
        src = (BACKEND / "app" / "config.py").read_text(encoding="utf-8")
        assert "ai_thinking_dead_cooldown" in src
        assert "ai_all_circuit_fast_fail_threshold" in src
        assert "ai_all_circuit_fast_fail_cooldown" in src

    def test_defaults_match_design(self):
        from app.config import settings
        assert getattr(settings, "ai_thinking_dead_cooldown", None) == 60.0
        assert getattr(settings, "ai_all_circuit_fast_fail_threshold", None) == 2
        assert getattr(settings, "ai_all_circuit_fast_fail_cooldown", None) == 60.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
