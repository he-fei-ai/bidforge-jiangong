"""R240 遗留项加固护栏 · 配置契约 / 真 omp 就绪预检 / 边界与口径（2026-11-13）。

对应上一轮报告 §7「遗留登记」与 §5.2「未执行项」的最优收口：

* **A 配置契约**：``AGENT_ENGINE_*`` 拼错或遗留死键不再静默失效；
  合法键集合由「配置快照字段 + Settings 字段」单一事实源派生，配置演进自动同步。
* **B 就绪预检**：真 omp 的 compaction / 工具调用验收前置条件做成可执行只读自检；
  ``ready=false ⇒ acceptance_allowed=false``，杜绝把「回退成功」包装成「真 omp 跑通」；
  即便全部闸门通过，也明确声明「预检不代替验收」。
* **C 边界与口径**：业务层 routers 零接线（既不偷偷把新轨接进生产，也不越权改业务）；
  R239 单轨与 R240 双轨两份配置快照均脱敏且同时可达。

⚠️ 全程离线：零真实 omp 进程、零子进程、零网络、零业务库访问。
⚠️ 加法式新增文件，不修改任何业务代码与既有护栏。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from app.services.ai.agent_engine import public_config as single_public_config
from app.services.ai.agent_engine.dual_track import (
    MODE_PI,
    AgentRouter,
    AgentTask,
    known_env_keys,
    load_dual_track_config,
    make_config,
    readiness,
    unknown_env_keys,
)
from app.services.ai.agent_engine.dual_track import (
    public_config as dual_public_config,
)
from app.services.ai.agent_engine.dual_track.config import ENV_PREFIX

_BACKEND = Path(__file__).resolve().parents[1]

#: 业务层端点清单（业务层 Prompt / 流程 / 写路径的守门范围）。
_BUSINESS_ROUTERS = (
    "_sse_outline_build.py",
    "_sse_outline_review.py",
    "sse_handlers.py",
    "sections.py",
    "review.py",
    "review_autofix.py",
    "consistency_repair.py",
    "export.py",
    "upload_outline.py",
    "system.py",
)

_EXPECTED_GATES = {"binary", "auth", "mode", "executor_impl", "tools", "config_keys"}


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch):
    """隔离外部 AGENT_ENGINE_* 环境变量，避免用例互相污染。"""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _auth_file(tmp_path: Path) -> Path:
    """构造一个合法但含敏感值的授权凭据文件（只用于脱敏断言）。"""
    path = tmp_path / "omp_auth.json"
    path.write_text(
        json.dumps({"SECRET_KEY": "SUPER_SECRET_VALUE"}), encoding="utf-8"
    )
    return path


# ===================== A. 配置契约：未知键不再静默失效 =====================
def test_known_env_keys_covers_config_snapshot_and_settings():
    """合法键集合覆盖 14 项配置 + R239 单轨字段，新增字段自动纳入。"""
    known = known_env_keys()
    for suffix in (
        "MODE", "ENABLED", "FALLBACK_ENABLED", "OMP_BINARY_PATH",
        "CONTEXT_THRESHOLD_RATIO", "COMPACTION_THRESHOLD", "MAX_RETRIES",
        "RETRY_BACKOFF_BASE", "TASK_TIMEOUT", "PROVIDER_AUTH",
        "GRAY_PERCENT", "GRAY_KEY", "OMP_EXECUTOR_IMPL", "MOCK_RESPONSE_PATH",
    ):
        assert ENV_PREFIX + suffix in known
    # 同一配置中心的 R239 单轨字段同样是合法键，不得误报。
    assert ENV_PREFIX + "PRIMARY" in known
    assert ENV_PREFIX + "FALLBACK_ORDER" in known


def test_unknown_env_keys_with_explicit_mapping():
    """纯函数式检测：合法键不报，拼错键与遗留死键都被点名。"""
    env = {
        "AGENT_ENGINE_MODE": "pi",
        "AGENT_ENGINE_GRAY_PERCENT": "20",
        "AGENT_ENGINE_MDOE": "omp",              # 拼写错误
        "AGENT_ENGINE_HYBRID_OMP_PERCENT": "50",  # R237 遗留死键
        "UNRELATED_VAR": "x",
    }
    assert unknown_env_keys(env) == [
        "AGENT_ENGINE_HYBRID_OMP_PERCENT",
        "AGENT_ENGINE_MDOE",
    ]


def test_unknown_env_keys_reads_live_environment(monkeypatch):
    """实时环境变量同样被检测（运维改错键立刻可见，无需重启）。"""
    monkeypatch.setenv("AGENT_ENGINE_MODE", "pi")
    monkeypatch.setenv("AGENT_ENGINE_TYPO_KEY", "1")
    assert unknown_env_keys() == ["AGENT_ENGINE_TYPO_KEY"]


def test_env_example_keys_are_all_known():
    """``.env.example`` 的每个键都必须有真实读点（单一事实源驱动，防死配置回潮）。"""
    text = (_BACKEND / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^(AGENT_ENGINE_[A-Z_]+)=", text, re.M))
    assert keys
    assert keys <= known_env_keys(), f"示例文件含无法读取的键：{keys - known_env_keys()}"


# ===================== B. 真 omp 验收前置就绪预检 =====================
def test_readiness_blocks_when_real_omp_unavailable(monkeypatch):
    """当前环境（无 omp 二进制 / mode=pi / 未注入凭据）严禁声称验收通过。"""
    from app.services.ai import omp_rpc

    monkeypatch.setattr(omp_rpc, "binary_exists", lambda b: False)
    out = readiness()

    assert out["ready"] is False
    assert out["acceptance_allowed"] is False
    assert set(out["gates"]) == _EXPECTED_GATES
    assert {"binary", "auth", "mode"} <= set(out["blocking_gates"])
    assert "回退成功 ≠ 真 omp 跑通" in out["reason"]
    assert "不拉起 omp" in out["note"]


def test_readiness_blocks_mock_executor_and_unknown_key(monkeypatch):
    """路径 B 的 mock 档 + 未知配置键均属阻塞项，不能进入真 omp 验收。"""
    monkeypatch.setenv("AGENT_ENGINE_OMP_EXECUTOR_IMPL", "mock")
    monkeypatch.setenv("AGENT_ENGINE_HYBRID_OMP_PERCENT", "50")
    out = readiness()

    assert out["gates"]["executor_impl"]["ok"] is False
    assert "AGENT_ENGINE_HYBRID_OMP_PERCENT" in out["gates"]["config_keys"]["unknown"]
    assert out["ready"] is False and out["acceptance_allowed"] is False


def test_readiness_never_leaks_auth_secret(monkeypatch, tmp_path):
    """凭据只回报可读性 / 合法性，路径与内容绝不进入诊断面。"""
    path = _auth_file(tmp_path)
    monkeypatch.setenv("AGENT_ENGINE_PROVIDER_AUTH", str(path))
    out = readiness()

    assert out["gates"]["auth"]["readable"] is True
    assert out["gates"]["auth"]["valid_json"] is True
    dumped = json.dumps(out, ensure_ascii=False)
    assert "SUPER_SECRET_VALUE" not in dumped
    assert path.name not in dumped
    assert out["gates"]["auth"].get("status") in ("ok", "failed", "unknown")


def test_readiness_passing_all_gates_still_does_not_claim_acceptance(monkeypatch, tmp_path):
    """全闸门齐备只表示「具备验收条件」，绝不表示验收已完成。"""
    from app.services.ai import omp_rpc

    monkeypatch.setattr(omp_rpc, "binary_exists", lambda b: True)
    monkeypatch.setenv("AGENT_ENGINE_MODE", "omp")
    monkeypatch.setenv("AGENT_ENGINE_OMP_EXECUTOR_IMPL", "real")
    monkeypatch.setenv("AGENT_ENGINE_PROVIDER_AUTH", str(_auth_file(tmp_path)))

    out = readiness()
    assert out["ready"] is True
    assert out["acceptance_allowed"] is True
    assert out["blocking_gates"] == []
    assert "前置条件齐备" in out["reason"]
    # 关键语义：预检通过 ≠ 真 omp 的 compaction / 工具调用已实测通过。
    assert "不代替验收" in out["reason"]
    assert "验收" in out["note"]


def test_readiness_gate_shape_is_stable():
    """每个闸门都有 ok，字段名稳定（诊断面可长期消费）。"""
    out = readiness()
    assert set(out["gates"]) == _EXPECTED_GATES
    assert all(isinstance(g["ok"], bool) for g in out["gates"].values())
    assert all(isinstance(g.get("hint", ""), str) for g in out["gates"].values())


# ===================== C. 边界护栏与双份快照口径 =====================
def test_business_routers_never_wire_dual_track_executor():
    """业务层零改动铁律：业务端点不得接线 AgentRouter，诊断端点只做只读预演。"""
    routers = _BACKEND / "app" / "routers"
    for name in _BUSINESS_ROUTERS:
        source = (routers / name).read_text(encoding="utf-8")
        assert "dual_track" not in source, f"{name} 接线了双轨层（业务层零改动铁律）"
        assert "AgentRouter" not in source, f"{name} 出现了 AgentRouter"

    # omp_ops.py 只读诊断面允许构造 Router 做路由预演，但**绝不执行任务**。
    ops = (routers / "omp_ops.py").read_text(encoding="utf-8")
    assert "route_plan(" in ops
    assert "executeTask(" not in ops


def test_default_mode_pi_keeps_new_track_unreachable():
    """默认 pi：任何任务都落旧轨，新轨零调用（默认零影响）。"""
    assert load_dual_track_config().mode == MODE_PI
    router = AgentRouter(config=make_config(mode=MODE_PI))
    task = AgentTask(task_id="t-boundary", task_type="outline", scheme_id="S-1")
    assert router.select(task) == "pi"
    assert router.route_plan(task)["requested_engine"] == "pi"


def test_both_public_configs_are_redacted():
    """R239 单轨 / R240 双轨两份快照分层不同，但脱敏口径必须一致。"""
    for snapshot in (single_public_config(), dual_public_config()):
        assert "provider_auth_configured" in snapshot
        assert "provider_auth" not in snapshot
        assert "SUPER_SECRET_VALUE" not in json.dumps(snapshot, ensure_ascii=False)


def test_diagnostics_exposes_readiness_and_both_snapshots():
    """运维可达：诊断端点同时下发单轨配置、双轨配置与就绪预检。"""
    from app.routers import omp_ops

    diag = omp_ops._dual_track_diagnostic()
    assert diag["available"] is True
    assert {"ready", "acceptance_allowed", "gates"} <= set(diag["readiness"])
    assert diag["readiness"]["acceptance_allowed"] is diag["readiness"]["ready"]

    src = Path(omp_ops.__file__).read_text(encoding="utf-8")
    assert '"engine_config": public_config()' in src
    assert '"dual_track": _dual_track_diagnostic()' in src
