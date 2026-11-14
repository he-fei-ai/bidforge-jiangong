"""双轨 Agent 引擎（R240）· 单元测试：配置加载 / 路由分发 / 执行器切换。

对应验收项：
* 配置抽离：全部阈值来自配置中心，环境变量热更新，非法值归一；
* 双轨路由：pi / omp / hybrid 三态分发、稳定哈希灰度、Feature Flag；
* 执行器切换：依赖注入可整体替换新轨（路径 B）；
* 数据契约：统一接口、畸形结构归一化不污染业务模型。

全部离线：零真实 omp 进程、零网络、零业务库访问。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.services.ai.agent_engine.dual_track import (
    ENGINE_MOCK,
    ENGINE_OMP,
    ENGINE_PI,
    IMPL_MOCK,
    MODE_HYBRID,
    MODE_OMP,
    MODE_PI,
    AgentRouter,
    AgentTask,
    IAgentExecutor,
    MockOmpExecutor,
    OmpAgentExecutor,
    PiAgentExecutor,
    failed_result,
    load_dual_track_config,
    make_config,
    normalize_result,
    public_config,
    stable_bucket,
)
from app.services.ai.agent_engine.dual_track.config import ENV_PREFIX

_DUAL_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "agent_engine" / "dual_track"


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch):
    """隔离外部 AGENT_ENGINE_* 环境变量，避免用例互相污染。"""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _task(**kwargs) -> AgentTask:
    base = dict(task_id="t_unit_1", scheme_id="SCHEME-A", task_type="outline", prompt="生成目录")
    base.update(kwargs)
    return AgentTask(**base)


# ===================== A. 配置加载与硬编码清理 =====================
def test_default_mode_is_pi_and_values_match_settings():
    """默认 pi（改造前行为）；所有阈值与配置中心字段逐项对齐。"""
    from app.config import settings

    cfg = load_dual_track_config()
    assert cfg.mode == MODE_PI
    assert cfg.mode == settings.agent_engine_mode
    assert cfg.context_threshold_ratio == settings.agent_engine_context_threshold_ratio
    assert cfg.max_retries == settings.agent_engine_max_retries
    assert cfg.retry_backoff_base == settings.agent_engine_retry_backoff_base
    assert cfg.compaction_threshold == settings.agent_engine_compaction_threshold
    assert cfg.provider_auth == settings.agent_engine_provider_auth
    assert cfg.task_timeout == settings.omp_task_timeout
    # 新键为空时回落既有 omp_binary_path（单一事实源）。
    assert cfg.omp_binary_path == (settings.agent_engine_omp_binary_path or settings.omp_binary_path)


def test_required_config_keys_exist_in_settings():
    """用户要求的 5 个配置项在配置中心均有定义，禁止散落在业务代码。"""
    from app.config import settings

    required = (
        "agent_engine_mode",
        "agent_engine_omp_binary_path",
        "agent_engine_context_threshold_ratio",
        "agent_engine_max_retries",
        "agent_engine_provider_auth",
        "agent_engine_enabled",
        "agent_engine_fallback_enabled",
        "agent_engine_gray_percent",
        "agent_engine_gray_key",
        "agent_engine_omp_executor_impl",
    )
    assert all(hasattr(settings, name) for name in required)


def test_env_hot_reload_without_restart(monkeypatch):
    """实时环境变量优先于 Settings：改完新任务立即生效（可热更新）。"""
    assert load_dual_track_config().mode == MODE_PI
    monkeypatch.setenv(ENV_PREFIX + "MODE", "omp")
    monkeypatch.setenv(ENV_PREFIX + "MAX_RETRIES", "4")
    monkeypatch.setenv(ENV_PREFIX + "CONTEXT_THRESHOLD_RATIO", "0.85")
    monkeypatch.setenv(ENV_PREFIX + "OMP_BINARY_PATH", "D:/tools/omp.exe")
    monkeypatch.setenv(ENV_PREFIX + "PROVIDER_AUTH", "D:/secrets/omp_auth.json")
    monkeypatch.setenv(ENV_PREFIX + "TASK_TIMEOUT", "66")
    monkeypatch.setenv(ENV_PREFIX + "GRAY_PERCENT", "30")

    cfg = load_dual_track_config()
    assert cfg.mode == MODE_OMP
    assert cfg.max_retries == 4
    assert cfg.context_threshold_ratio == 0.85
    assert cfg.omp_binary_path == "D:/tools/omp.exe"
    assert cfg.provider_auth == "D:/secrets/omp_auth.json"
    assert cfg.task_timeout == 66
    assert cfg.gray_percent == 30

    # 再改一次，仍即时生效（无进程级缓存）。
    monkeypatch.setenv(ENV_PREFIX + "MODE", "hybrid")
    assert load_dual_track_config().mode == MODE_HYBRID


def test_invalid_config_is_normalized_not_silently_accepted(monkeypatch, caplog):
    """非法配置归一 + WARN，绝不静默吞掉。"""
    monkeypatch.setenv(ENV_PREFIX + "MODE", "quantum")
    monkeypatch.setenv(ENV_PREFIX + "CONTEXT_THRESHOLD_RATIO", "1.5")
    monkeypatch.setenv(ENV_PREFIX + "MAX_RETRIES", "-3")
    monkeypatch.setenv(ENV_PREFIX + "GRAY_PERCENT", "200")
    monkeypatch.setenv(ENV_PREFIX + "GRAY_KEY", "random_uuid")
    monkeypatch.setenv(ENV_PREFIX + "OMP_EXECUTOR_IMPL", "chaos")
    with caplog.at_level("WARNING"):
        cfg = load_dual_track_config()
    assert cfg.mode == MODE_PI
    assert 0 < cfg.context_threshold_ratio <= 1
    assert cfg.max_retries >= 0
    assert cfg.gray_percent == 100
    assert cfg.gray_key == "scheme_id"
    # 非法注入开关必须回落 real，绝不把生产流量误打进 Mock。
    assert cfg.omp_executor_impl == "real"


def test_public_config_is_sanitized(monkeypatch):
    """脱敏快照只回报「是否配置」，不泄露凭据路径 / 内容。"""
    monkeypatch.setenv(ENV_PREFIX + "PROVIDER_AUTH", "D:/secrets/omp_auth.json")
    snap = public_config()
    assert snap["provider_auth_configured"] is True
    assert "D:/secrets" not in str(snap)
    assert snap["mode"] == MODE_PI


def test_no_business_thresholds_hardcoded_outside_config_module():
    """源码护栏：业务/路由/执行器模块不得出现具体阈值字面量。

    允许的唯一位置是 config.py 的「极端兜底」字典；这样阈值只能来自配置中心。
    """
    forbidden = ("0.7", "max_retries = ", "context_threshold_ratio = 0", "120000")
    for filename in ("router.py", "executors.py", "contracts.py"):
        source = (_DUAL_DIR / filename).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{filename} 出现硬编码阈值 {token}"


# ===================== B. 路由分发 =====================
def test_mode_pi_routes_to_pi():
    router = AgentRouter(config=make_config(mode=MODE_PI))
    assert router.select(_task()) == ENGINE_PI


def test_mode_omp_routes_to_omp():
    router = AgentRouter(config=make_config(mode=MODE_OMP))
    assert router.select(_task()) == ENGINE_OMP


def test_mode_omp_with_mock_impl_routes_to_mock():
    """路径 B：配置指向 MockOmpExecutor，无需改业务代码即可整体换轨。"""
    router = AgentRouter(config=make_config(mode=MODE_OMP, omp_executor_impl=IMPL_MOCK))
    assert router.select(_task()) == ENGINE_MOCK


def test_feature_flag_off_forces_pi():
    """全局 Feature Flag 关闭 ⇒ 强制旧轨（一键回退）。"""
    cfg = make_config(mode=MODE_OMP, enabled=False)
    assert AgentRouter(config=cfg).select(_task()) == ENGINE_PI


def test_hybrid_zero_and_full_percent():
    zero = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=0))
    full = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=100))
    tasks = [_task(scheme_id=f"S{i}", task_id=f"t{i}") for i in range(40)]
    assert all(zero.select(t) == ENGINE_PI for t in tasks)
    assert all(full.select(t) == ENGINE_OMP for t in tasks)


def test_hybrid_hash_is_stable_and_monotonic():
    """同一项目 ID 恒定落同一轨；放量比例单调不减（灰度不抖动）。"""
    tasks = [_task(scheme_id=f"SCHEME-{i}", task_id=f"t{i}") for i in range(60)]
    previous: set[str] = set()
    for percent in (0, 10, 25, 50, 75, 100):
        router = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=percent))
        chosen = {t.scheme_id for t in tasks if router.select(t) == ENGINE_OMP}
        assert previous.issubset(chosen), "放量比例提高后，已命中项目不得被踢回 Pi 轨"
        previous = chosen
        # 重复调用结果恒定。
        assert all(
            router.select(t) == router.select(t) for t in tasks
        )
    assert len(previous) == len(tasks)


def test_hybrid_gray_key_switch(monkeypatch):
    """灰度键可在 scheme_id / task_id 间切换（均来自配置）。"""
    task = _task(scheme_id="SAME", task_id="DIFFERENT")
    by_scheme = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=50, gray_key="scheme_id"))
    by_task = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=50, gray_key="task_id"))
    assert by_scheme.route_plan(task)["hash_key"] == "SAME"
    assert by_task.route_plan(task)["hash_key"] == "DIFFERENT"
    # 同 scheme_id、不同 task_id：scheme 轨相同，task 轨可不同。
    other = _task(scheme_id="SAME", task_id="ANOTHER")
    assert by_scheme.select(task) == by_scheme.select(other)


def test_stable_bucket_is_deterministic():
    """blake2b 稳定哈希：同键恒定、不同键分散。"""
    assert stable_bucket("A") == stable_bucket("A")
    values = {stable_bucket(f"SCHEME-{i}") for i in range(100)}
    assert len(values) > 50


def test_route_plan_diagnostic_shape():
    plan = AgentRouter(config=make_config(mode=MODE_HYBRID, gray_percent=20)).route_plan(_task())
    assert {"mode", "enabled", "requested_engine", "fallback_enabled", "gray_key",
            "gray_percent", "hash_key", "bucket", "omp_executor_impl"} <= set(plan)
    assert 0 <= plan["bucket"] < 100


# ===================== C. 执行器切换与契约 =====================
@pytest.mark.asyncio
async def test_all_executors_implement_unified_interface():
    """三个执行器 + Router 均满足 IAgentExecutor（executeTask 契约）。"""
    async def fake_chat(messages, **kwargs):
        return "ok"

    executors = (
        PiAgentExecutor(chat_fn=fake_chat),
        OmpAgentExecutor(inner=MockOmpExecutor()),
        MockOmpExecutor(),
        AgentRouter(config=make_config(mode=MODE_PI)),
    )
    for executor in executors:
        assert isinstance(executor, IAgentExecutor)


@pytest.mark.asyncio
async def test_executor_switch_by_injection():
    """注入替换执行器：同一 Router，换注入即换底层引擎，业务调用点不变。"""
    async def fake_chat(messages, **kwargs):
        return "PI-PAYLOAD"

    pi = PiAgentExecutor(chat_fn=fake_chat)
    mock_a = MockOmpExecutor(responses="A-PAYLOAD")
    mock_b = MockOmpExecutor(responses="B-PAYLOAD")
    task = _task()

    router_a = AgentRouter(config=make_config(mode=MODE_OMP, omp_executor_impl=IMPL_MOCK),
                           pi_executor=pi, mock_executor=mock_a)
    router_b = AgentRouter(config=make_config(mode=MODE_OMP, omp_executor_impl=IMPL_MOCK),
                           pi_executor=pi, mock_executor=mock_b)
    assert (await router_a.executeTask(task)).payload == "A-PAYLOAD"
    assert (await router_b.executeTask(task)).payload == "B-PAYLOAD"


def test_normalize_result_isolates_unknown_fields():
    """omp 畸形结构：未知键只留键名到 meta，值一律丢弃，业务模型不被污染。"""
    task = _task()
    raw = {
        "ok": True,
        "payload": ["不该是列表"],
        "status": 777,
        "attempts": "两次",
        "weird_nested": {"secret": object()},
        "omp_internal_trace": "内部字段",
    }
    result = normalize_result(raw, task=task, engine=ENGINE_OMP)
    assert isinstance(result.payload, str)
    assert result.status in ("completed", "failed", "paused")
    assert result.attempts == 0
    assert result.meta["omp_unknown_fields"] == ["omp_internal_trace", "weird_nested"]
    assert "secret" not in str(result)
    assert not hasattr(result, "weird_nested")
    assert not hasattr(result, "omp_internal_trace")


@pytest.mark.parametrize("bad", [None, 123, object(), [], {"ok": object()}])
def test_normalize_result_never_raises(bad):
    """任意异常结构都收敛为 AgentResult，绝不向业务层抛异常。"""
    result = normalize_result(bad, task=_task(), engine=ENGINE_OMP)
    assert hasattr(result, "to_dict")
    assert isinstance(result.to_dict(), dict)


def test_business_identity_cannot_be_overwritten_by_omp_response():
    """omp 回包篡改 task_id / task_type 会被路由层纠正。"""
    task = _task(task_id="REAL-ID", task_type="content")
    result = normalize_result(
        {"ok": True, "payload": "x", "task_id": "FAKE", "task_type": "evil"},
        task=task,
        engine=ENGINE_OMP,
    )
    assert result.task_id == "REAL-ID"
    assert result.task_type == "content"


def test_failed_result_status_vocabulary():
    result = failed_result(_task(), ENGINE_OMP, errors=["x"])
    assert result.ok is False
    assert result.status == "failed"
    assert result.engine == ENGINE_OMP
