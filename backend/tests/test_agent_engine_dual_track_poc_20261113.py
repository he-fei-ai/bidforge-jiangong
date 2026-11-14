"""路径 B（注入执行器）POC · 三业务链路集成测试（R240 · 2026-11-13）。

对应《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》与本次任务书：

* **三、路径 B POC**：``AgentRouter`` 配置指向 :class:`MockOmpExecutor`，
  离线跑通【目录生成】→【正文生成】→【审核与修复】，验证数据契约、
  状态流转、上下文交接是否顺畅；
* **四、回退与容错**：模拟 omp 未安装 / 未授权 / 超时 / 崩溃 / 异常结构，
  验证默认回退到 Pi 轨、WARN 日志、异常结构不污染业务数据模型；
* **五、集成测试**：验证业务层 Prompt 与流程结果与改造前一致（业务无感知）；
* **配置契约 / 运维可达**：``.env.example`` 不得回潮死配置；
  ``GET /api/v1/omp/diagnostics`` 的 ``dual_track`` 字段必须真实可读。

⚠️ 全程离线：零真实 omp 二进制、零子进程、零网络、零业务库访问、零业务层改动。
⚠️ 本文件为**加法式新增**，不修改任何既有护栏与业务代码。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from app.services.ai.agent_engine.dual_track import (
    ENGINE_MOCK,
    ENGINE_PI,
    IMPL_MOCK,
    MODE_HYBRID,
    MODE_OMP,
    MODE_PI,
    AgentRouter,
    AgentTask,
    MockOmpExecutor,
    PiAgentExecutor,
    make_config,
)
from app.services.ai.agent_engine.dual_track.config import ENV_PREFIX

# 业务层 Prompt（模拟真实业务传入；引擎层不得改写任何一个字）。
PROMPT_OUTLINE = "请依据工程概况生成专项施工方案标准化目录树（L1~L4）。"
PROMPT_CONTENT = "请依据下列目录章节生成对应正文。"
PROMPT_AUDIT = "请审核下列正文并执行一致性修复。"

#: Pi 轨基线产物：用于断言「降级后业务结果与改造前一致」。
BASELINE_PI_PAYLOAD = "PI-轨基线产物（改造前后逐字节一致）"

#: 运维配置契约键集合（与 config.py 的 Settings 字段一一对应）。
_EXPECTED_ENV_KEYS = {
    "MODE",
    "ENABLED",
    "FALLBACK_ENABLED",
    "OMP_BINARY_PATH",
    "CONTEXT_THRESHOLD_RATIO",
    "COMPACTION_THRESHOLD",
    "MAX_RETRIES",
    "RETRY_BACKOFF_BASE",
    "TASK_TIMEOUT",
    "PROVIDER_AUTH",
    "GRAY_PERCENT",
    "GRAY_KEY",
    "OMP_EXECUTOR_IMPL",
    "MOCK_RESPONSE_PATH",
}


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch):
    """隔离外部 AGENT_ENGINE_* 环境变量，避免用例互相污染。"""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


class _PiChat:
    """Pi 轨离线替身：记录收到的 messages，返回固定基线产物。"""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    async def __call__(self, messages, **kwargs):
        self.seen.append(messages)
        return BASELINE_PI_PAYLOAD


def _task(**kwargs) -> AgentTask:
    base = dict(
        task_id="t_poc_1",
        scheme_id="SCHEME-POC-1",
        task_type="outline",
        prompt=PROMPT_OUTLINE,
    )
    base.update(kwargs)
    return AgentTask(**base)


def _poc_router(*, mock=None, pi=None, **cfg_overrides) -> AgentRouter:
    """构造路径 B 路由：mode=omp + 注入 Mock（不改业务代码即可整体换轨）。"""
    cfg = make_config(mode=MODE_OMP, omp_executor_impl=IMPL_MOCK, **cfg_overrides)
    return AgentRouter(config=cfg, mock_executor=mock, pi_executor=pi)


# ===================== A. 路径 B：三条业务链路 POC =====================
@pytest.mark.asyncio
async def test_three_stage_chain_with_context_handoff(tmp_path):
    """【目录 → 正文 → 审核修复】三链路：数据契约 / 状态流转 / 上下文交接。"""
    preset = {
        "responses": {
            "outline": json.dumps(
                {
                    "chapters": [
                        {"id": "1", "title": "工程概况"},
                        {"id": "2", "title": "施工部署"},
                    ],
                    "version": "v1",
                },
                ensure_ascii=False,
            ),
            "content": json.dumps(
                {"sections": [{"id": "1", "text": "工程概况正文"}], "version": "v1"},
                ensure_ascii=False,
            ),
            "audit": json.dumps(
                {"passed": True, "issues": [], "fixed": []}, ensure_ascii=False
            ),
        },
        "frames": [
            {"type": "session_start"},
            {"type": "assistant_delta", "delta": "…"},
            {"type": "tool_call", "name": "write"},
            {"type": "tool_result", "name": "write"},
            {"type": "prompt_result"},
        ],
    }
    response_file = tmp_path / "omp_poc_responses.json"
    response_file.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")

    mock = MockOmpExecutor(response_file=str(response_file))
    pi_chat = _PiChat()
    router = _poc_router(mock=mock, pi=PiAgentExecutor(chat_fn=pi_chat))

    # ① 目录生成
    r_outline = await router.executeTask(
        _task(task_id="poc-outline", task_type="outline")
    )
    # ② 正文生成：把目录叶子作为上下文交接注入业务 Prompt（模拟 OUTLINE.json 展平）
    outline = json.loads(r_outline.payload)
    leaves = [{"id": c["id"], "title": c["title"]} for c in outline["chapters"]]
    content_prompt = (
        PROMPT_CONTENT + "\n\n# OUTLINE.json 展平交接\n" + json.dumps(leaves, ensure_ascii=False)
    )
    r_content = await router.executeTask(
        _task(task_id="poc-content", task_type="content", prompt=content_prompt)
    )
    # ③ 审核与修复：把正文产物原样交接
    audit_prompt = PROMPT_AUDIT + "\n\n# 正文产物交接\n" + r_content.payload
    r_audit = await router.executeTask(
        _task(task_id="poc-audit", task_type="audit", prompt=audit_prompt)
    )

    results = (r_outline, r_content, r_audit)
    # 状态流转：三阶段均 completed，未发生降级（Mock 全部成功）
    assert [r.status for r in results] == ["completed"] * 3
    assert all(r.ok and not r.meta.get("degraded") for r in results)
    assert [r.engine for r in results] == [ENGINE_MOCK] * 3
    assert [r.task_id for r in results] == ["poc-outline", "poc-content", "poc-audit"]
    # 数据契约：产物可解析为 JSON，审核结论通过
    assert json.loads(r_content.payload)["sections"]
    assert json.loads(r_audit.payload)["passed"] is True
    # 上下文交接无损：目录叶子进入正文 Prompt，正文产物原样进入审核 Prompt
    assert all(leaf["id"] in mock.calls[1].prompt for leaf in leaves)
    assert r_content.payload in mock.calls[2].prompt
    # 业务层 Prompt 原样透传：引擎层不改写任何一个字
    assert mock.calls[0].prompt == PROMPT_OUTLINE
    assert mock.calls[1].prompt.startswith(PROMPT_CONTENT)
    assert mock.calls[2].prompt.startswith(PROMPT_AUDIT)
    # 模拟流式输出：帧序列完整、帧类型可观测
    assert all(r.meta["streamed"] is True for r in results)
    assert {"session_start", "assistant_delta", "tool_call", "tool_result", "prompt_result"} <= set(
        r_outline.meta["frame_types"]
    )
    assert len(mock.stream_frames) == 3 * len(preset["frames"])
    # 路由决策留痕（可观测）
    assert len(router.decisions) == 3
    assert all(d["requested_engine"] == ENGINE_MOCK for d in router.decisions)
    # Pi 轨完全未被触碰（路径 B 注入生效，且成功时不回退）
    assert pi_chat.seen == []


# ===================== B. 集成：模拟 omp 失败 ⇒ 默认回退 =====================
@pytest.mark.parametrize("fail_kind", ["unavailable", "auth", "timeout", "crash", "execution"])
@pytest.mark.asyncio
async def test_omp_failure_falls_back_to_pi(fail_kind, caplog):
    """五类 omp 故障均自动回退 Pi 轨 + WARN 日志 + degraded 标注。"""
    pi_chat = _PiChat()
    failing = MockOmpExecutor(fail_times=99, fail_kind=fail_kind)
    router = _poc_router(mock=failing, pi=PiAgentExecutor(chat_fn=pi_chat))
    task = _task(task_id=f"poc-fail-{fail_kind}")

    with caplog.at_level("WARNING", logger="ai_provider.agent_engine.dual_track"):
        result = await router.executeTask(task)

    assert result.ok is True and result.status == "completed"
    assert result.payload == BASELINE_PI_PAYLOAD
    assert result.meta["degraded"] is True
    assert result.meta["engine_used"] == ENGINE_PI
    assert result.meta["requested_engine"] == ENGINE_MOCK
    assert result.meta["primary_errors"]
    assert "自动回退" in caplog.text
    # 业务无感知：Pi 轨收到的 user Prompt 与业务原始 Prompt 逐字节一致
    assert pi_chat.seen[0][-1]["content"] == task.prompt


@pytest.mark.asyncio
async def test_fallback_payload_matches_pi_baseline():
    """业务无感知强断言：降级后产物与「改造前 Pi 轨基线」逐字段一致。"""
    pi_chat = _PiChat()
    pi = PiAgentExecutor(chat_fn=pi_chat)
    task = _task()

    baseline = await AgentRouter(
        config=make_config(mode=MODE_PI), pi_executor=pi
    ).executeTask(task)
    degraded = await _poc_router(
        mock=MockOmpExecutor(fail_times=99, fail_kind="unavailable"), pi=pi
    ).executeTask(task)

    assert degraded.payload == baseline.payload
    assert (degraded.task_id, degraded.task_type, degraded.status) == (
        baseline.task_id,
        baseline.task_type,
        baseline.status,
    )
    assert degraded.meta["degraded"] is True
    assert pi_chat.seen[0] == pi_chat.seen[1]  # 两次收到的 messages 完全相同


@pytest.mark.asyncio
async def test_feature_flag_off_never_invokes_omp():
    """全局 Feature Flag 关闭 ⇒ 强制旧轨（一键回退）。"""
    mock = MockOmpExecutor()
    pi_chat = _PiChat()
    router = _poc_router(mock=mock, pi=PiAgentExecutor(chat_fn=pi_chat), enabled=False)

    result = await router.executeTask(_task())
    assert result.ok and result.engine == ENGINE_PI
    assert mock.calls == [] and len(pi_chat.seen) == 1


@pytest.mark.asyncio
async def test_fallback_disabled_returns_failed_without_exception():
    """显式关闭回退 ⇒ 失败收敛（不抛异常、不动 Pi 轨）。"""
    pi_chat = _PiChat()
    router = _poc_router(
        mock=MockOmpExecutor(fail_times=99, fail_kind="auth"),
        pi=PiAgentExecutor(chat_fn=pi_chat),
        fallback_enabled=False,
    )
    result = await router.executeTask(_task())
    assert result.ok is False and result.status == "failed"
    assert pi_chat.seen == []


@pytest.mark.asyncio
async def test_malformed_omp_response_never_pollutes_business_model():
    """omp 返回畸形结构 ⇒ 白名单归一化，未知键的键名留痕、值一律丢弃。"""
    router = _poc_router(mock=MockOmpExecutor(malformed_times=1))
    result = await router.executeTask(_task())

    assert isinstance(result.payload, str)
    assert result.status in ("completed", "failed", "paused")
    assert result.task_id == "t_poc_1"  # 业务身份不被回包覆写
    assert "omp_internal_trace" in result.meta["omp_unknown_fields"]
    assert not hasattr(result, "weird_nested")
    assert not hasattr(result, "omp_internal_trace")
    assert "secret" not in str(result.to_dict())


@pytest.mark.asyncio
async def test_executor_exception_is_contained_then_falls_back():
    """执行器抛未收敛异常 ⇒ Router 兜住并回退，绝不向业务层冒泡。"""

    class Exploding:
        name = ENGINE_MOCK

        async def executeTask(self, task):
            raise RuntimeError("omp 进程崩溃（模拟）")

    pi_chat = _PiChat()
    router = _poc_router(mock=Exploding(), pi=PiAgentExecutor(chat_fn=pi_chat))
    result = await router.executeTask(_task())

    assert result.ok is True and result.meta["degraded"] is True
    assert any("RuntimeError" in e for e in result.meta["primary_errors"])


@pytest.mark.asyncio
async def test_hybrid_gray_keeps_business_result_shape():
    """灰度分流：无论落新轨还是旧轨，业务层拿到的都是归一化 AgentResult。"""
    for percent in (0, 100):
        router = AgentRouter(
            config=make_config(
                mode=MODE_HYBRID, gray_percent=percent, omp_executor_impl=IMPL_MOCK
            ),
            mock_executor=MockOmpExecutor(),
            pi_executor=PiAgentExecutor(chat_fn=_PiChat()),
        )
        result = await router.executeTask(_task(scheme_id="SCHEME-GRAY"))
        assert result.ok and result.status == "completed"
        assert isinstance(result.to_dict(), dict)


# ===================== C. 配置契约与运维可达 =====================
def test_env_example_contract_defaults_to_pi_and_has_no_dead_key():
    """配置契约：默认 pi、无死配置、路径 B 注入开关在运维面可见。"""
    from app.config import settings

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "AGENT_ENGINE_MODE=pi" in text
    assert "AGENT_ENGINE_HYBRID_OMP_PERCENT" not in text  # R237 遗留死键已清除
    assert "AGENT_ENGINE_OMP_EXECUTOR_IMPL=real" in text
    assert "AGENT_ENGINE_GRAY_PERCENT=0" in text

    keys = set(re.findall(r"^AGENT_ENGINE_([A-Z_]+)=", text, re.M))
    assert keys == _EXPECTED_ENV_KEYS, f"配置键集合漂移：{keys ^ _EXPECTED_ENV_KEYS}"

    # 每个键都必须有真实读点（Settings 字段或既有回落键），杜绝「代码不读的死配置」。
    for suffix in keys:
        field = "agent_engine_" + suffix.lower()
        if suffix == "TASK_TIMEOUT":
            assert hasattr(settings, "omp_task_timeout")  # 留空回落既有键
        else:
            assert hasattr(settings, field), f"死配置：{suffix} 无对应 Settings 字段"


def test_diagnostics_endpoint_exposes_dual_track_readonly():
    """运维可达：dual_track 配置与三链路路由预演挂进只读诊断端点。"""
    from app.routers import omp_ops

    diag = omp_ops._dual_track_diagnostic()
    assert diag["available"] is True and diag["preview_only"] is True
    cfg = diag["config"]
    assert cfg["mode"] == MODE_PI
    # 脱敏：只回报「是否配置」，不回传凭据路径 / 内容
    assert "provider_auth_configured" in cfg and "provider_auth" not in cfg
    assert [p["task_type"] for p in diag["route_preview"]] == [
        "outline",
        "content",
        "audit",
    ]
    assert all(0 <= p["bucket"] < 100 for p in diag["route_preview"])

    # 端点注册 + 字段挂载（源码级护栏，避免拉起整条诊断链）
    src = Path(omp_ops.__file__).read_text(encoding="utf-8")
    assert '"dual_track": _dual_track_diagnostic()' in src
    assert any(
        getattr(route, "path", "") == "/api/v1/omp/diagnostics"
        for route in omp_ops.router.routes
    )
