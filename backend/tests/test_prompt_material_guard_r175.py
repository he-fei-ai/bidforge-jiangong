"""R175（2026-11-03）提示词模块修复验证。

覆盖本轮四项核心修复：
1. **BUG-1 敏感信息外发**：脱敏接线（统一入口 ``prompt_material_guard``）
   —— 开关关闭时逐字节不变；开启时凭据被替换、正常文本（标准编号/数值/单位）不受损；
   覆盖事实提取素材、投标分析资料、招标分段、出站消息四条链路形态。
2. **BUG-2 治理开关重启失效**：``PUT /system/governance`` 落库 → 模拟重启
   （清空内存态）→ ``GET /system/governance`` 仍读到库内值。
3. **BUG-3 硬编码提示词逃逸治理**：4 条新注册模板可经 ``get_prompt`` 取到。
4. **P3-7 变量契约**：新模板契约无漂移；缺变量模板能被 ``check_prompt_variables`` 检出。
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.services import prompt_material_guard as pmg
from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg, check_prompt_variables

NEW_PROMPT_KEYS = (
    "bid_segment_merge_system",
    "bid_section_extract_system",
    "bid_section_merge_system",
    "ocr_extract_system",
)

_CRED_SAMPLE = (
    "项目联系人：张三；系统账号 API_KEY=sk-abcdef1234567890abcdef；\n"
    "数据库连接：postgres://admin:P@ssw0rd@10.0.0.5:5432/db；\n"
    "执行标准 JGJ 120-2012；基坑深度 10.5m；预算金额 320 万元；工期 180 日历天。"
)


@pytest.fixture(autouse=True)
def _reset_governance_flags():
    """每个用例前后复位三个治理开关，避免污染同进程其他文件。"""
    old = (
        getattr(settings, "prompt_redact_sensitive", False),
        getattr(settings, "prompt_injection_defense", False),
        getattr(settings, "prompt_context_budget", 0),
    )
    yield
    settings.prompt_redact_sensitive = old[0]
    settings.prompt_injection_defense = old[1]
    settings.prompt_context_budget = old[2]


# ---------------------------------------------------------------------------
# 1. 脱敏：关闭 / 开启
# ---------------------------------------------------------------------------


def test_redaction_disabled_keeps_text_bytewise():
    """默认关闭时必须**逐字节**原样返回（既有行为零变化）。"""
    settings.prompt_redact_sensitive = False
    out = pmg.govern_blob(_CRED_SAMPLE, budget=False)
    assert out == _CRED_SAMPLE
    out2 = pmg.redact_only(_CRED_SAMPLE)
    assert out2 == _CRED_SAMPLE


def test_redaction_enabled_masks_credentials_but_keeps_numbers_and_units():
    """开启后：凭据消失；标准编号 / 数值 / 单位 / 金额 / 工期必须完整保留。"""
    settings.prompt_redact_sensitive = True
    out = pmg.govern_blob(_CRED_SAMPLE, budget=False)
    assert "sk-abcdef1234567890abcdef" not in out
    assert "P@ssw0rd" not in out
    for keep in ("JGJ 120-2012", "10.5m", "320 万元", "180 日历天", "张三"):
        assert keep in out, f"正常文本被误脱敏: {keep}"


def test_redact_outbound_messages_covers_user_role_only():
    """出站收口：user 消息脱敏，system/assistant 模板不受影响。"""
    settings.prompt_redact_sensitive = True
    msgs = [
        {"role": "system", "content": "你是投标分析专家（模板，不含资料）"},
        {"role": "user", "content": _CRED_SAMPLE},
    ]
    out = pmg.redact_outbound_messages(msgs)
    assert out[0]["content"] == msgs[0]["content"]
    assert "sk-abcdef1234567890abcdef" not in out[1]["content"]

    settings.prompt_redact_sensitive = False
    same = pmg.redact_outbound_messages(msgs)
    assert same is msgs  # 关闭时返回原对象（零开销零行为变化）


def test_build_item_chain_redacts_project_material():
    """链路①：投标分析 build_item 注入的项目资料。"""
    settings.prompt_redact_sensitive = True
    from app.services.bid_analysis_service import build_item

    text = build_item(_CRED_SAMPLE, {"item_id": "project_basic_info", "label": "项目基本信息"})
    assert "sk-abcdef1234567890abcdef" not in text


def test_bid_section_extraction_chain_redacts_tender_text():
    """链路②：招标文件分段提取 + 合并。"""
    settings.prompt_redact_sensitive = True
    from app.services.bid_section_extraction import (
        build_extract_messages,
        build_merge_messages,
    )

    msgs = build_extract_messages(_CRED_SAMPLE, 1, 2)
    joined = "\n".join(m["content"] for m in msgs)
    assert "sk-abcdef1234567890abcdef" not in joined

    merged = build_merge_messages([{"title": "一标段", "evidence": [_CRED_SAMPLE]}])
    assert "sk-abcdef1234567890abcdef" not in merged[1]["content"]


def test_facts_material_chain_redacts():
    """链路③：事实提取素材（govern_blob 与 facts_extractor 同一入口）。"""
    settings.prompt_redact_sensitive = True
    out = pmg.govern_blob(_CRED_SAMPLE, "全局事实提取资料", budget=False)
    assert "sk-abcdef1234567890abcdef" not in out


def test_govern_blob_disabled_is_identity():
    """所有开关关闭时，govern_blob 必须是恒等函数。"""
    settings.prompt_redact_sensitive = False
    settings.prompt_injection_defense = False
    settings.prompt_context_budget = 0
    assert pmg.govern_blob(_CRED_SAMPLE) == _CRED_SAMPLE


# ---------------------------------------------------------------------------
# 2. 治理开关持久化（模拟重启）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_governance_persists_across_restart(tmp_path, monkeypatch):
    import app.db as _appdb

    monkeypatch.setattr(_appdb, "DB_PATH", tmp_path / "governance_r175.db")
    await _appdb.init_db()

    from app.routers.system import (
        GovernanceSettings,
        RedactionSettings,
        governance_get,
        governance_posture_get,
        governance_put,
        governance_redaction_put,
    )

    saved = await governance_put(
        GovernanceSettings(
            prompt_context_budget=24000,
            prompt_injection_defense=True,
        )
    )
    assert saved["prompt_context_budget"] == 24000
    assert saved["prompt_injection_defense"] is True
    red = await governance_redaction_put(RedactionSettings(enabled=True))
    assert red["redaction_enabled"] is True

    # 模拟重启：进程内内存态全部丢失
    settings.prompt_context_budget = 0
    settings.prompt_injection_defense = False
    settings.prompt_redact_sensitive = False

    reloaded = await governance_get()
    assert reloaded["prompt_context_budget"] == 24000
    assert reloaded["prompt_injection_defense"] is True

    posture = await governance_posture_get()
    assert posture["redaction_enabled"] is True
    assert posture["persisted"] is True
    assert posture["redaction_notice"] == ""


@pytest.mark.asyncio
async def test_governance_partial_put_keeps_other_flags(tmp_path, monkeypatch):
    """只提交部分字段时不得把其他开关静默重置（旧实现默认值的坑）。"""
    import app.db as _appdb

    monkeypatch.setattr(_appdb, "DB_PATH", tmp_path / "governance_partial.db")
    await _appdb.init_db()
    from app.routers.system import GovernanceSettings, governance_get, governance_put

    await governance_put(GovernanceSettings(prompt_injection_defense=True))
    payload = await governance_put(GovernanceSettings(prompt_context_budget=8000))
    assert payload["prompt_injection_defense"] is True
    assert (await governance_get())["prompt_context_budget"] == 8000


def test_governance_notice_when_redaction_off():
    settings.prompt_redact_sensitive = False
    posture = pmg.governance_posture()
    assert posture["redaction_enabled"] is False
    assert "未开启" in posture["redaction_notice"]


# ---------------------------------------------------------------------------
# 3. 硬编码提示词收编
# ---------------------------------------------------------------------------


def test_hardcoded_prompts_registered():
    from app.services.ai.prompts import get_prompt

    for key in NEW_PROMPT_KEYS:
        content = get_prompt(key)
        assert content, f"{key} 未注册或为空"
        assert key in _ALL_PROMPTS


def test_new_prompt_contracts_have_no_drift():
    issues = {i["key"]: i for i in check_prompt_variables(verbose=False)}
    for key in NEW_PROMPT_KEYS:
        assert key not in issues, f"{key} 契约漂移: {issues.get(key)}"


def test_missing_variable_template_is_detected():
    """P3-7：模板用了未声明的变量 → 启动期/测试期即可检出（而非运行期空串）。"""
    key = "r175_contract_drift_probe"
    _reg(key, "analysis", "R175 契约漂移探针", "静态前缀 {undeclared_var} 静态后缀", requires=[])
    try:
        issues = {i["key"]: i for i in check_prompt_variables(verbose=False)}
        assert key in issues
        assert "undeclared_var" in issues[key]["used_not_declared"]
    finally:
        _ALL_PROMPTS.pop(key, None)


# ---------------------------------------------------------------------------
# 4. 遗留项 V1：<<FUZZY_FILL>> 对 DB 定制版生效
# ---------------------------------------------------------------------------


def test_db_customized_marker_is_expanded_on_read(monkeypatch):
    """DB 定制内容里残留 <<FUZZY_FILL>> 时必须读取时展开（否则标记直达模型）。"""
    from app.services.ai.prompts import _cache

    monkeypatch.setattr(
        _cache, "_prompt_cache", {"content_generation_system": "定制前缀\n<<FUZZY_FILL>>\n定制后缀"}, raising=False
    )
    out = _cache.get_prompt("content_generation_system")
    assert "<<" not in out
    assert "定制前缀" in out and "定制后缀" in out
    # 展开出的规则段必须真的有内容（不是静默删标记）
    assert len(out) > len("定制前缀\n\n定制后缀") + 100


# ---------------------------------------------------------------------------
# 5. R179（2026-11-05）：截断可见 + 出站脱敏真正接线
# ---------------------------------------------------------------------------


def test_clip_text_truncation_is_visible():
    """超限截断必须与旧切片逐字一致，同时写日志并计入截断指标（可运维可查）。"""
    from app.services.ai.prompts import _metrics

    _metrics.reset()
    long_text = "甲" * 2000
    out = pmg.clip_text(long_text, 1500, "单元测试-截断")
    assert out == long_text[:1500], "截断结果必须与旧 [:1500] 切片逐字一致"
    assert _metrics.token_budget_truncated.get("单元测试-截断") == 1
    # 未超限：原样返回且不计数（不得制造假截断信号）
    assert pmg.clip_text("短文本", 1500, "单元测试-截断") == "短文本"
    assert _metrics.token_budget_truncated.get("单元测试-截断") == 1
    # limit <= 0 = 不截断
    assert pmg.clip_text(long_text, 0, "单元测试-截断") == long_text
    _metrics.reset()


def test_clip_text_logged_normal_ledger():
    """正常路径：裁剪结果逐字节等于旧切片，截断计数按标签累加。"""
    from app.services.ai.prompts import _metrics

    _metrics.reset()
    long_text = "丙" * 2000
    out = pmg.clip_text_logged(long_text, 1500, "单元测试-仅日志")
    assert out == long_text[:1500]
    assert _metrics.token_budget_truncated.get("单元测试-仅日志") == 1
    assert _metrics.clip_ledger_failures.get("truncation_counter") is None
    _metrics.reset()


def test_clip_text_logged_ledger_failure_is_observable(monkeypatch):
    """第 5 条：留痕机制自身故障不得静默 —— 裁剪结果不变，降级计数器 +1。

    变异点：若把 `_record_ledger_failure(...)` 删掉（回到 `pass`），
    `clip_ledger_failures` 将恒为 0，本用例转红。
    """
    from app.services.ai.prompts import _metrics

    _metrics.reset()

    def boom(label):
        raise RuntimeError("metrics store broken")

    monkeypatch.setattr(_metrics, "record_token_budget_truncated", boom)
    long_text = "丁" * 2000
    out = pmg.clip_text_logged(long_text, 1500, "单元测试-留痕故障")
    assert out == long_text[:1500], "留痕故障不得改变裁剪结果（fail-soft 边界）"
    assert _metrics.clip_ledger_failures.get("truncation_counter") == 1, (
        "留痕失败必须落到降级计数器，不能是纯 pass"
    )
    _metrics.reset()


async def test_provider_call_applies_outbound_redaction():
    """出站单点兜底必须真正接线：provider 收到的 user 消息已被脱敏。"""
    settings.prompt_redact_sensitive = True
    from app.services.ai.provider_factory import _call_provider

    seen: dict = {}

    class _FakeProvider:
        name = "fake-test"

        async def chat(self, messages, **kwargs):
            seen["messages"] = messages
            return "ok"

    await _call_provider(
        _FakeProvider(),
        [
            {"role": "system", "content": "系统模板"},
            {"role": "user", "content": _CRED_SAMPLE},
        ],
        request_mode="normal",
        temperature=None,
        json_mode=False,
        max_tokens=None,
    )
    assert seen["messages"][0]["content"] == "系统模板"
    assert "sk-abcdef1234567890abcdef" not in seen["messages"][1]["content"]
