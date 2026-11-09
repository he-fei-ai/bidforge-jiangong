# -*- coding: utf-8 -*-
"""提示词模块深度审计收口 · R204（2026-11-09）护栏。

覆盖本轮 12 项落地的**行为级**断言 + 若干**源码锁**（防回归漂移）：

1. FIX-1  SHARED 片段互引不再打爆调用栈（RecursionError → 500）
2. FIX-2  保存期互引检测（``shared_cycle`` → error → 400）
3. FIX-3  提示词写接口迁至管理面（``/admin/prompts/*``），旧路径只留 403 指引
4. FIX-4  PATCH 缺 ``content`` 字段 → 400（不再被当成「恢复默认」误删定制）
5. FIX-5  ``updated_at`` 毫秒精度（CAS 同秒冲突可检出）
6. FIX-6  ``sync-from-code`` 原子性（保存点 + 内存改写延后到 commit 之后）
7. FIX-7  指标 reset / 审计查询纳入管理面闸门
8. FIX-8  出站脱敏覆盖 **system** 角色（凭据不再原样外发）
9. FIX-9  ``context_length_limit`` 接线（此前全仓零生产调用的死旋钮）
10. FIX-10 上下文预算按**整份请求**分配（此前 N 条消息 = N×预算）
11. FIX-11 段优先级改为最长匹配（``目录库参考`` 不再被 ``目录`` 抢命中）
12. FIX-12 事实整理拒绝「截断值回写」（事实库静默丢数据）

外加：审计 diff 等长改字可见、指标 key 上界、repair_key 兜底、围栏 JSON
可解析性、修复轮上下文一致性、provider 日志脱敏、审计动作中文标签、
结构化摘要模型可见截断说明、评分项模板无双花括号、token 上界。
"""

from __future__ import annotations

import json
import re

import pytest


# ---------------------------------------------------------------------------
# FIX-1 · SHARED 片段互引 → RecursionError（全局 500）
# ---------------------------------------------------------------------------
class TestSharedCycleRuntime:
    def test_mutual_reference_does_not_recurse(self, monkeypatch):
        """A→B→A 互引：解析必须收敛（保留字面量），不得抛 RecursionError。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(
            _cache,
            "_prompt_cache",
            {
                "SHARED_A": "甲 {SHARED_B}",
                "SHARED_B": "乙 {SHARED_A}",
                "HOST_TPL": "宿主 {SHARED_A}",
            },
        )
        monkeypatch.setattr(_cache, "_loaded_db_path", None)
        out = _cache.get_prompt("HOST_TPL")
        assert isinstance(out, str)
        # 环被检出并**保留字面量**（既不崩，也不静默丢内容）
        assert "{SHARED_A}" in out
        assert "甲" in out and "乙" in out

    def test_self_reference_still_literal(self, monkeypatch):
        """自引用（老 BUG-P0-2 场景）依旧保留字面量。"""
        from app.services.ai.prompts import _cache

        monkeypatch.setattr(
            _cache,
            "_prompt_cache",
            {"SHARED_A": "自引 {SHARED_A}", "HOST_TPL": "宿主 {SHARED_A}"},
        )
        monkeypatch.setattr(_cache, "_loaded_db_path", None)
        out = _cache.get_prompt("HOST_TPL")
        assert "{SHARED_A}" in out

    def test_resolver_does_not_call_get_prompt_recursively(self):
        """源码锁：``_resolve_shared_keys`` 不得再调用 ``get_prompt`` 取数。

        旧实现用 ``get_prompt(key)`` 取子片段内容，而 ``get_prompt`` 内部又
        会再调一次 ``_resolve_shared_keys``（默认 depth=0/chain=()）→ 解析链
        被重置 → 环检测永不命中。
        """
        from pathlib import Path

        src = Path("app/services/ai/prompts/_cache.py").read_text(encoding="utf-8")
        body = src.split("def _resolve_shared_keys", 1)[1].split("def _raw_prompt_text", 1)[0]
        assert "get_prompt(" not in body, "_resolve_shared_keys 又开始调用 get_prompt（解析链会被重置）"
        assert "_raw_prompt_text(" in body


# ---------------------------------------------------------------------------
# FIX-2 · 保存期互引检测
# ---------------------------------------------------------------------------
class TestSharedCycleSaveTime:
    def test_cycle_detected_as_error(self, monkeypatch):
        from app.services.ai.prompts import _registry

        monkeypatch.setitem(_registry._ALL_PROMPTS, "SHARED_X", {"content": "x {SHARED_Y}", "default_content": "x {SHARED_Y}"})
        monkeypatch.setitem(_registry._ALL_PROMPTS, "SHARED_Y", {"content": "y {SHARED_X}", "default_content": "y {SHARED_X}"})
        issues = _registry.validate_prompt_content("SHARED_Y", "y {SHARED_X}")
        codes = [i["code"] for i in issues]
        assert "shared_cycle" in codes, issues
        assert any(i["level"] == "error" for i in issues if i["code"] == "shared_cycle")

    def test_no_false_positive_for_acyclic(self, monkeypatch):
        from app.services.ai.prompts import _registry

        monkeypatch.setitem(_registry._ALL_PROMPTS, "SHARED_P", {"content": "p", "default_content": "p"})
        monkeypatch.setitem(_registry._ALL_PROMPTS, "SHARED_Q", {"content": "q {SHARED_P}", "default_content": "q {SHARED_P}"})
        issues = _registry.validate_prompt_content("SHARED_Q", "q {SHARED_P}")
        assert "shared_cycle" not in [i["code"] for i in issues], issues

    def test_labels_cover_new_codes(self):
        from app.services.ai.prompts._registry import PROMPT_ISSUE_LABELS

        assert "shared_cycle" in PROMPT_ISSUE_LABELS
        assert "shared_self_reference" in PROMPT_ISSUE_LABELS


# ---------------------------------------------------------------------------
# FIX-3 / FIX-4 / FIX-5 / FIX-6 / FIX-7 · 提示词治理面
# ---------------------------------------------------------------------------
class TestPromptsAdminSurface:
    def test_write_endpoints_moved_to_admin_router(self):
        from app.routers import prompts as P

        admin_paths = {r.path for r in P.admin_prompts_router.routes}
        assert "/admin/prompts/{key}" in admin_paths
        assert "/admin/prompts/{key}/reset" in admin_paths
        assert "/admin/prompts/{key}/rollback" in admin_paths
        assert "/admin/prompts/audit-logs" in admin_paths

    async def test_legacy_paths_reject_with_guidance(self):
        from app.routers import prompts as P
        from fastapi import HTTPException

        for fn in (
            P.update_prompt_legacy_denied,
            P.reset_prompt_legacy_denied,
            P.rollback_prompt_legacy_denied,
        ):
            with pytest.raises(HTTPException) as ei:
                await fn("k")
            assert ei.value.status_code == 403
            assert "admin/prompts" in str(ei.value.detail)

        with pytest.raises(HTTPException) as ei:
            await P.prompt_audit_logs_legacy_denied()
        assert ei.value.status_code == 403
        assert "admin/prompts" in str(ei.value.detail)

    def test_patch_requires_content_field(self):
        """缺 content 字段 → 400（不得被当成「恢复默认」而删掉用户定制）。"""
        from pathlib import Path

        src = Path("app/routers/prompts.py").read_text(encoding="utf-8")
        assert '"content" not in body' in src

    def test_updated_at_uses_millisecond_precision(self):
        """CAS 依赖 updated_at：秒级精度会让同秒并发写静默覆盖。

        ⚠️ 时间戳由**应用层**生成（``_now_ts()``）—— SQLite 不支持
        ``strftime('%f')``（实测返回 NULL → updated_at 被写成 NULL）。
        """
        from pathlib import Path

        from app.routers.prompts import _now_ts

        ts = _now_ts()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", ts), ts
        src = Path("app/routers/prompts.py").read_text(encoding="utf-8")
        assert "updated_at=excluded.updated_at" in src
        assert "updated_at=datetime('now','localtime')" not in src

    def test_sync_updates_memory_only_after_commit(self):
        """sync-from-code：内存注册表改写必须发生在 commit 之后。"""
        from pathlib import Path

        src = Path("app/routers/prompts.py").read_text(encoding="utf-8")
        assert "_pending_memory_updates" in src
        commit_at = src.index("await db.commit()")
        mem_at = src.index("for _k, _c in _pending_memory_updates:")
        assert mem_at > commit_at, "内存改写发生在 commit 之前（失败会造成内存/DB 分叉）"
        assert "SAVEPOINT" in src and "ROLLBACK TO" in src

    def test_metrics_reset_requires_admin(self):
        from pathlib import Path

        src = Path("app/routers/prompts.py").read_text(encoding="utf-8")
        idx = src.index("prompt-metrics/reset")
        window = src[idx : idx + 700]
        assert "require_admin_access" in window


# ---------------------------------------------------------------------------
# FIX-8 · 出站脱敏覆盖 system
# ---------------------------------------------------------------------------
class TestRedactAllRoles:
    def test_system_role_is_redacted(self, monkeypatch):
        from app.config import settings
        from app.services import prompt_material_guard as pmg

        monkeypatch.setattr(settings, "prompt_injection_defense", False)
        monkeypatch.setattr(settings, "prompt_redact_sensitive", True)
        msgs = [
            {"role": "system", "content": "章节正文含 sk-abcdef1234567890abcdef 与 http://u:P@ssw0rd@h/p"},
            {"role": "user", "content": "用户资料含 sk-abcdef1234567890abcdef"},
        ]
        out = pmg.redact_outbound_messages(msgs)
        for m in out:
            assert "sk-abcdef1234567890abcdef" not in m["content"]
            assert "P@ssw0rd" not in m["content"]

    def test_disabled_returns_same_object(self, monkeypatch):
        from app.config import settings
        from app.services import prompt_material_guard as pmg

        monkeypatch.setattr(settings, "prompt_injection_defense", False)
        monkeypatch.setattr(settings, "prompt_redact_sensitive", False)
        msgs = [{"role": "system", "content": "sk-abcdef1234567890abcdef"}]
        assert pmg.redact_outbound_messages(msgs) is msgs


# ---------------------------------------------------------------------------
# FIX-9 · context_length_limit 接线（死旋钮）
# ---------------------------------------------------------------------------
class TestContextLengthLimitWired:
    def test_messages_level_limit_applies(self, monkeypatch):
        from app.routers import _sse_outline_review as sor

        monkeypatch.setattr(sor._sh.settings, "context_length_limit", 100)
        msgs = [
            {"role": "system", "content": "S" * 50},
            {"role": "user", "content": "甲" * 300},
            {"role": "user", "content": "乙" * 300},
        ]
        out = sor._apply_context_length_limit_to_messages(msgs)
        total = sum(len(m["content"]) for m in out if m["role"] == "user")
        assert total <= 100 + 200  # 允许每条附带的模型可见说明
        assert "已截断" in "".join(m["content"] for m in out if m["role"] == "user")

    def test_disabled_is_identity(self, monkeypatch):
        from app.routers import _sse_outline_review as sor

        monkeypatch.setattr(sor._sh.settings, "context_length_limit", 0)
        msgs = [{"role": "user", "content": "x" * 5000}]
        assert sor._apply_context_length_limit_to_messages(msgs) is msgs

    def test_production_wiring_exists(self):
        """正文链路必须真的调用（此前只定义未接线 → 旋钮形同虚设）。"""
        from pathlib import Path

        src = Path("app/routers/sse_handlers.py").read_text(encoding="utf-8")
        assert "_apply_context_length_limit_to_messages(messages)" in src


# ---------------------------------------------------------------------------
# FIX-10 · 预算按整份请求分配
# ---------------------------------------------------------------------------
class TestBudgetTotal:
    def test_total_user_length_within_budget(self, monkeypatch):
        from app.routers import _sse_outline_review as sor

        monkeypatch.setattr(sor._sh.settings, "prompt_context_budget", 500)
        seg = "【全局事实】：\n" + "事实" * 200 + "\n"
        msgs = [
            {"role": "user", "content": seg},
            {"role": "user", "content": seg},
            {"role": "user", "content": seg},
        ]
        out = sor._apply_prompt_context_budget_to_messages(msgs)
        total = sum(len(m["content"]) for m in out if m["role"] == "user")
        assert total <= 500 + 60, f"预算被放大：{total} > 500"

    def test_single_message_matches_legacy_behavior(self, monkeypatch):
        from app.routers import _sse_outline_review as sor

        monkeypatch.setattr(sor._sh.settings, "prompt_context_budget", 200)
        text = "【全局事实】：\n" + "事实" * 300 + "\n"
        msgs = [{"role": "user", "content": text}]
        out = sor._apply_prompt_context_budget_to_messages(msgs)
        assert out[0]["content"] == sor._apply_prompt_context_budget(text)

    def test_disabled_is_identity(self, monkeypatch):
        from app.routers import _sse_outline_review as sor

        monkeypatch.setattr(sor._sh.settings, "prompt_context_budget", 0)
        msgs = [{"role": "user", "content": "x" * 5000}]
        assert sor._apply_prompt_context_budget_to_messages(msgs) is msgs


# ---------------------------------------------------------------------------
# FIX-11 · 段优先级最长匹配
# ---------------------------------------------------------------------------
def test_segment_priority_longest_match():
    from app.services.prompt_governance import segment_priority_of

    assert segment_priority_of("目录库参考") == 3
    assert segment_priority_of("目录") == 1
    assert segment_priority_of("调整要求") == -1
    assert segment_priority_of("项目资料摘要") == 3


# ---------------------------------------------------------------------------
# FIX-12 · 事实整理拒绝截断值回写
# ---------------------------------------------------------------------------
class TestFactsTruncatedWriteback:
    def test_truncated_echo_is_rejected(self):
        from app.services import facts_enrich as fe

        class _Item:
            key = "k1"
            name = "长工艺说明"
            value = "原值" * 400  # 800 字，远超 300 字截断线

        item = _Item()
        original = item.value
        truncated = fe._clip(original)  # 送 AI 的形态：300 字 + …
        out, changed = fe.apply_finalize_result([item], {"facts": [{"fact_key": "k1", "name": "长工艺说明", "value": truncated}]})
        assert changed == 0, "截断值被回写（事实库会静默丢数据）"
        assert out[0].value == original

    def test_legit_rewrite_is_applied(self):
        from app.services import facts_enrich as fe

        class _Item:
            key = "k1"
            name = "短事实"
            value = "原始描述"

        item = _Item()
        out, changed = fe.apply_finalize_result([item], {"facts": [{"fact_key": "k1", "name": "短事实", "value": "改写后的完整描述"}]})
        assert changed == 1
        assert out[0].value == "改写后的完整描述"


# ---------------------------------------------------------------------------
# 其他横切修复
# ---------------------------------------------------------------------------
def test_scoring_template_has_no_double_braces():
    """``{{`` 是 Python .format 风格转义，本仓渲染器是**正则替换**、不会还原
    → 双花括号会原样下发模型，弱模型模仿后 JSON 解析必失败。"""
    from app.services.ai.prompts import _registry

    _registry.register_lazy_prompts()
    meta = _registry._ALL_PROMPTS["scoring_items_extract"]
    text = meta.get("default_content") or meta.get("content") or ""
    assert "{{" not in text and "}}" not in text


def test_audit_diff_reveals_equal_length_change():
    from app.services.audit_service import diff_prompt_snapshot

    changes = diff_prompt_snapshot({"before": "必须采用锚桩法", "after": "严禁采用锚桩法"})
    fields = [c["field"] for c in changes]
    assert "content_changed" in fields, changes


def test_audit_actions_include_sync_kinds():
    from app.services.audit_service import PROMPT_AUDIT_ACTIONS

    assert "sync_insert" in PROMPT_AUDIT_ACTIONS
    assert "sync_overwrite" in PROMPT_AUDIT_ACTIONS


def test_metrics_counter_keys_are_bounded():
    from app.services.ai.prompts import _metrics

    _metrics.reset()
    for i in range(_metrics._MAX_COUNTER_KEYS + 50):
        _metrics.record_token_budget_truncated(f"动态标签-{i}")
    assert len(_metrics.token_budget_truncated) <= _metrics._MAX_COUNTER_KEYS + 1
    assert _metrics._OVERFLOW_KEY in _metrics.token_budget_truncated


def test_repair_fallback_respects_requested_key(monkeypatch):
    """渲染失败时兜底必须用**调用方请求的** repair_key（此前硬编码通用 key）。"""
    from app.services.ai import json_response as jr

    monkeypatch.setattr(
        jr,
        "render",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = jr._build_repair_user_prompt(["x"], "{}", "facts_json_fix_system")
    assert "facts" in out.lower()


def test_extract_json_skips_unparsable_fence():
    from app.services.ai.json_response import extract_json

    text = '这是示例：\n```json\n{不是合法 JSON}\n```\n真正答案：\n```json\n{"ok": 1}\n```'
    got = extract_json(text)
    assert got is not None
    assert json.loads(got) == {"ok": 1}


def test_repair_round_uses_first_raw_consistently():
    """修复轮 assistant 上下文必须与 invalid_content 同源（都是最初输出）。"""
    from pathlib import Path

    src = Path("app/services/ai/json_response.py").read_text(encoding="utf-8")
    assert '{"role": "assistant", "content": first_raw or ""}' in src


def test_provider_failure_log_is_sanitized():
    from pathlib import Path

    src = Path("app/services/ai/provider_factory.py").read_text(encoding="utf-8")
    assert 'logger.error("Provider %s 调用失败: %s", pname, e)' not in src
    assert 'logger.error("Provider %s 调用失败: %s", pname, _error_summary(e))' in src


def test_structured_brief_truncation_is_model_visible():
    """结构化摘要被截断时必须给模型看得到（否则模型断言「资料未提及」）。"""
    from pathlib import Path

    src = Path("app/routers/_sse_outline_review.py").read_text(encoding="utf-8")
    assert "项目资料摘要已截断" in src


def test_max_tokens_and_word_budget_are_bounded():
    from app.services import content_utils as cu

    assert cu.normalize_word_budget_override(10**9) == cu._max_word_budget()
    assert cu.max_tokens_for_budget(10**9) <= cu._max_output_tokens()
    # 正常值不受影响
    assert cu.max_tokens_for_budget(1200) == max(cu.MAX_TOKENS_FLOOR, int(1200 * cu.MAX_TOKENS_OVER_FACTOR * cu.WORD_TOKENS_PER_CHAR))


def test_ledger_failures_are_counted_not_swallowed():
    """三个裁剪原语的留痕失败必须落到 clip_ledger_failures（不再静默 pass）。"""
    from pathlib import Path

    src = Path("app/services/prompt_material_guard.py").read_text(encoding="utf-8")
    assert src.count("_record_ledger_failure(") >= 4


def test_truncation_ratchet_still_green():
    """裸切扫描棘轮：本轮不得引入新的模型可见裸切（工具 + 双层棘轮须在库）。"""
    from pathlib import Path

    tool = Path("../tools/prompt_bare_slice_scan.py")
    assert tool.exists()
    ratchet = Path("tests/test_prompt_bare_slice_scan_ratchet_20261109.py")
    assert ratchet.exists()
    assert "MODEL_FACING_REGISTRY" in ratchet.read_text(encoding="utf-8")
