"""R179（2026-11-06）启动期治理配置回放的回归护栏。

缺陷事实：``app/main.py`` lifespan 从 ``app.routers.system`` 导入
``load_governance_settings``，但该名字在 system.py 中从未实现；异常被外层
``except Exception`` 吞掉，于是「启动期把库内治理开关回放进运行时 settings」
一直静默失败 —— 用户在界面开启的注入防护 / 凭据脱敏在重启后实际不生效，
看起来却像「治理配置本来就是默认值」。

本文件用两类用例咬住这个缺陷：

1. 直接调用 ``load_governance_settings()``，断言库内非默认值真的写进
   ``app.config.settings``（下游 ``prompt_material_guard`` 的唯一读取源）；
2. 真实进入 ``app.main.lifespan`` 启动段，断言启动后 settings 已是库内值
   —— 任何「导入名写错 / 调用被静默吞掉」都会让 settings 停在默认值而变红。

另有一条反向用例：回放函数抛异常时启动日志必须留下 WARNING，
不允许「静默降级」。
"""

from __future__ import annotations

import logging

import pytest
from app.config import settings
from app.routers import system as system_router

#: 非默认治理配置（与 settings 默认 0 / False / False 明确可区分）
_SEEDED_KV = {
    "prompt_context_budget": "24000",
    "prompt_injection_defense": "true",
    "redaction_enabled": "true",
}


@pytest.fixture
def _restore_runtime_governance():
    """用例前后复位三个运行时开关，避免污染同进程其它测试文件。"""
    saved = (
        settings.prompt_context_budget,
        settings.prompt_injection_defense,
        settings.prompt_redact_sensitive,
    )
    yield
    (
        settings.prompt_context_budget,
        settings.prompt_injection_defense,
        settings.prompt_redact_sensitive,
    ) = saved


def _reset_runtime_to_defaults() -> None:
    """模拟进程刚启动、尚未回放库值时的内存态。"""
    settings.prompt_context_budget = 0
    settings.prompt_injection_defense = False
    settings.prompt_redact_sensitive = False


def _patch_lifespan_side_effects(monkeypatch):
    """隔离 lifespan 中与本用例无关的启停副作用（保留治理回放真实路径）。"""
    import app.main as main_mod
    import app.services.crypto as crypto_mod
    import app.utils.process_cleanup as cleanup_mod

    async def _noop_async(*_args, **_kwargs):
        return None

    # 数据库连接由 db_conn 夹具注入的内存库提供，这里不真建/真关文件库
    monkeypatch.setattr(main_mod, "init_db", _noop_async)
    monkeypatch.setattr(main_mod, "close_db", _noop_async)
    # 主密钥自检会写真实 data 目录；启动期孤儿 GC 与清理钩子与本用例无关
    monkeypatch.setattr(crypto_mod, "ensure_master_key_ready", lambda: None)
    monkeypatch.setattr(main_mod, "_run_startup_orphan_export_gc", _noop_async)
    monkeypatch.setattr(cleanup_mod, "register_shutdown_cleanup", lambda _fn: None)
    monkeypatch.setattr(cleanup_mod, "sweep_temp", lambda *_a, **_k: 0)


class TestLoadGovernanceSettings:
    """``load_governance_settings`` 必须存在，并把库值回填到运行时 settings。"""

    def test_symbol_is_exported_by_system_router(self):
        fn = getattr(system_router, "load_governance_settings", None)
        assert callable(fn), "app.routers.system 必须提供 load_governance_settings（main.py 启动期依赖它）"

    async def test_db_values_are_applied_to_runtime_settings(self, db_conn, _restore_runtime_governance):
        await system_router._governance_kv_save(dict(_SEEDED_KV))
        _reset_runtime_to_defaults()

        applied = await system_router.load_governance_settings()

        assert settings.prompt_context_budget == 24000
        assert settings.prompt_injection_defense is True
        assert settings.prompt_redact_sensitive is True
        assert applied == {
            "prompt_context_budget": 24000,
            "prompt_injection_defense": True,
            "redaction_enabled": True,
        }

        # 下游唯一读取源（prompt_material_guard）看到的是库值，而不是默认值
        from app.services import prompt_material_guard as pmg

        posture = pmg.governance_posture()
        assert posture["context_budget"] == 24000
        assert posture["injection_defense_enabled"] is True
        assert posture["redaction_enabled"] is True

    async def test_absent_keys_keep_env_defaults(self, db_conn, _restore_runtime_governance):
        """库内无记录时不得改写运行时默认值（治理配置是可选项）。"""
        settings.prompt_context_budget = 4096
        settings.prompt_injection_defense = True
        settings.prompt_redact_sensitive = True

        applied = await system_router.load_governance_settings()

        assert settings.prompt_context_budget == 4096
        assert settings.prompt_injection_defense is True
        assert settings.prompt_redact_sensitive is True
        assert applied == {
            "prompt_context_budget": 4096,
            "prompt_injection_defense": True,
            "redaction_enabled": True,
        }


class TestStartupLifespanAppliesGovernance:
    """真实走 lifespan 启动段：治理配置必须在启动时就生效。"""

    async def test_startup_applies_persisted_non_default_settings(
        self, db_conn, monkeypatch, _restore_runtime_governance
    ):
        import app.main as main_mod

        await system_router._governance_kv_save(dict(_SEEDED_KV))
        _reset_runtime_to_defaults()
        _patch_lifespan_side_effects(monkeypatch)

        async with main_mod.lifespan(main_mod.app):
            # 启动段已执行：若导入名写错/异常被吞，这里仍是 0 / False
            assert settings.prompt_context_budget == 24000
            assert settings.prompt_injection_defense is True
            assert settings.prompt_redact_sensitive is True

    async def test_startup_replay_failure_is_logged_not_silent(
        self, db_conn, monkeypatch, caplog, _restore_runtime_governance
    ):
        """回放失败属可降级路径，但必须留下 WARNING（不得无声）。"""
        import app.main as main_mod

        async def _boom():
            raise RuntimeError("governance kv 不可读")

        _patch_lifespan_side_effects(monkeypatch)
        monkeypatch.setattr(system_router, "load_governance_settings", _boom)
        _reset_runtime_to_defaults()

        with caplog.at_level(logging.WARNING):
            async with main_mod.lifespan(main_mod.app):
                pass

        hits = [r for r in caplog.records if r.levelno >= logging.WARNING and "治理开关回放失败" in r.getMessage()]
        assert hits, "治理配置回放失败必须留下 WARNING 日志（否则就是静默降级）"
        assert "governance kv 不可读" in hits[0].getMessage()
