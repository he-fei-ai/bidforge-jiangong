"""R213（2026-11-10）· 目录生成模块深度探索增强 —— 新增护栏。

本轮不改变编号算法（子承父系 / 等级正传 / 唯一性已由 R148/R187/R209 收口），
修复的是「闸门异常分支、复制写入口、checkpoint 三出口、配额确定性失败、
配置脏值、日志口径、提示词自相矛盾、前后端加法式信号未接线」这些接缝。

14 项落地对应测试分组：
- A FIX-1  目录库规范化异常分支 fail-closed
- B FIX-2  复制目录库统一编号规范化
- C FIX-3  AI 调整回传伪造 __original_id 被剔除
- D FIX-4  异常终止 checkpoint 补内部指纹
- E FIX-5  停止 / 断线兜底 checkpoint 补复用与截断信号
- F FIX-6  checkpoint 白名单 + TS 生成器类型
- G FIX-7  输入指纹覆盖全部影响目录的输入
- H FIX-8  一级章上限负数配置回退默认
- I FIX-9  续跑日志分母 / 文案
- J FIX-10 正文提示词内两处编号示例互斥
- K FIX-11 配额/认证确定性失败熔断
- L 端到端 编号不变式 + 信号链贯通 + 前后端接线
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO / "backend" / "app"
FRONTEND_SRC = REPO / "frontend" / "src"


def _src(rel: str) -> str:
    return (BACKEND_APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A · FIX-1 目录库入库闸门异常分支
# ---------------------------------------------------------------------------


class TestLibraryGateFailClosed:
    def test_for_library_exception_rejects_instead_of_passthrough(self, monkeypatch):
        """规范化抛异常时不得把原始脏编号原样入库且返回空 problems。"""
        from app.services import outline_utils as ou

        def boom(_tree):
            raise RuntimeError("boom")

        monkeypatch.setattr(ou, "normalize_outline", boom)
        text, problems = ou.normalize_outline_for_library([{"id": "9.9.9.9", "title": "x"}])
        assert text == "[]"
        assert problems and "boom" in problems[0]
        assert "9.9.9.9" not in text

    def test_normalize_json_exception_returns_default(self, monkeypatch):
        """同族异常分支：返回 default，不落「保留原始结构」。"""
        from app.services import outline_utils as ou

        def boom(_tree):
            raise RuntimeError("boom")

        monkeypatch.setattr(ou, "normalize_outline", boom)
        dirty = [{"id": "9.9.9.9", "title": "x"}]
        assert ou.normalize_outline_json(dirty, default="[]") == "[]"
        assert ou.normalize_outline_json(dirty, default="FALLBACK") == "FALLBACK"

    def test_normal_path_still_renumbers_and_validates(self):
        from app.services import outline_utils as ou

        text, problems = ou.normalize_outline_for_library(
            [{"id": "7", "title": "A", "children": [{"id": "9.9", "title": "B"}]}]
        )
        tree = json.loads(text)
        assert tree[0]["id"] == "1"
        assert tree[0]["children"][0]["id"] == "1.1"
        assert not problems


# ---------------------------------------------------------------------------
# B · FIX-2 复制目录库写入口
# ---------------------------------------------------------------------------


class TestDuplicateLibraryGate:
    def test_duplicate_uses_library_normalizer(self):
        from app.routers import outline_library as ol

        # ✅ R214 重锚：复制改走统一出口 `_normalize_for_write`（其内部才是
        #    normalize_outline_for_library 单一实现），「复制必须规范化」意图不变。
        #    改用源码切片而非 inspect.getsource（旧 pyc 行号错位会切错函数）。
        src = _src("routers/outline_library.py")
        dup = src[src.index("async def duplicate_library(") :]
        assert "_normalize_for_write(" in dup
        assert "normalize_outline_for_library" in src
        # INSERT 的 outline_json 参数必须是规范化结果，而非源行原样复制
        assert "_copy_json," in dup

    def test_dirty_snapshot_is_renumbered_on_copy(self):
        from app.services.outline_utils import normalize_outline_for_library

        dirty = [
            {
                "id": "7",
                "title": "A",
                "children": [
                    {"id": "9.9", "title": "B", "children": [{"id": "9.9.9", "title": "C"}]}
                ],
            }
        ]
        tree = json.loads(normalize_outline_for_library(dirty)[0])
        assert tree[0]["id"] == "1"
        assert tree[0]["children"][0]["id"] == "1.1"
        assert tree[0]["children"][0]["children"][0]["id"] == "1.1.1"


# ---------------------------------------------------------------------------
# C · FIX-3 AI 调整回传的主键注入面
# ---------------------------------------------------------------------------


class TestAdjustOriginalIdRelink:
    def test_forged_original_id_is_stripped(self):
        from app.routers.sections import _relink_adjusted_original_ids

        nodes = [
            {"id": "fake", "__original_id": "real-db-id", "title": "x"},
            {
                "id": "real-db-id",
                "title": "y",
                "children": [{"id": "fake2", "__original_id": "real-db-id"}],
            },
        ]
        _relink_adjusted_original_ids(nodes, {"real-db-id"})
        assert "__original_id" not in nodes[0]
        assert "id" not in nodes[0]
        assert nodes[1]["__original_id"] == "real-db-id"
        assert "__original_id" not in nodes[1]["children"][0]
        assert "id" not in nodes[1]["children"][0]

    def test_legitimate_id_still_relinks_children(self):
        from app.routers.sections import _relink_adjusted_original_ids

        nodes = [{"id": "db-1", "children": [{"id": "db-2"}]}]
        _relink_adjusted_original_ids(nodes, {"db-1", "db-2"})
        assert nodes[0]["__original_id"] == "db-1"
        assert nodes[0]["children"][0]["__original_id"] == "db-2"


# ---------------------------------------------------------------------------
# D · FIX-4 异常终止 checkpoint
# ---------------------------------------------------------------------------


class TestErrorExitCheckpoint:
    def test_error_exit_writes_fingerprint_and_signals(self):
        src = _src("routers/sse_handlers.py")
        start = src.index('_err_payload = {"event": "error"')
        seg = src[start : start + 2600]
        assert "_err_ckpt" in seg
        assert 'input_fingerprint' in seg
        assert "resumed_chapters" in seg
        assert "level1_truncated" in seg
        assert "failed_count" in seg

    def test_failed_status_is_resumable(self):
        """异常终止任务属于可续跑集合 ⇒ 指纹缺失会直接绕过陈旧防护。"""
        from app.routers.sse_checkpoint import _RESUMABLE_TASK_STATUSES

        assert "failed" in _RESUMABLE_TASK_STATUSES


# ---------------------------------------------------------------------------
# E · FIX-5 停止 / 断线兜底 checkpoint
# ---------------------------------------------------------------------------


class TestPartialCheckpointSignals:
    @pytest.mark.asyncio
    async def test_checkpoint_partial_outline_carries_all_signals(self, monkeypatch):
        from app.routers import sse_checkpoint as cp

        captured = {}

        async def fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            captured.update(payload)

        monkeypatch.setattr(cp, "_save_outline_checkpoint", fake_save)
        ok = await cp._checkpoint_partial_outline(
            "t1",
            [{"id": "1", "title": "x"}],
            ["failed-ch"],
            input_fingerprint="fp",
            resumed_chapters=3,
            level1_truncated=10,
            level1_total=50,
        )
        assert ok is True
        assert captured["input_fingerprint"] == "fp"
        assert captured["resumed_chapters"] == 3
        assert captured["level1_truncated"] == 10
        assert captured["level1_total"] == 50
        assert captured["partial"] is True

    @pytest.mark.asyncio
    async def test_checkpoint_partial_outline_omits_keys_when_absent(self, monkeypatch):
        from app.routers import sse_checkpoint as cp

        captured = {}

        async def fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            captured.update(payload)

        monkeypatch.setattr(cp, "_save_outline_checkpoint", fake_save)
        await cp._checkpoint_partial_outline("t1", [{"id": "1", "title": "x"}], [])
        assert "resumed_chapters" not in captured
        assert "level1_truncated" not in captured
        assert "input_fingerprint" not in captured

    @pytest.mark.asyncio
    async def test_stopped_event_and_checkpoint_both_carry_truncation(self, monkeypatch):
        from app.routers import _sse_outline_review as rv
        from app.routers import sse_handlers as sh

        captured = {}

        async def fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            captured["ckpt"] = dict(payload)

        async def fake_finish(task_id, status, message=""):
            captured["finish"] = status

        monkeypatch.setattr(sh, "_save_outline_checkpoint", fake_save)
        monkeypatch.setattr(sh, "finish_task", fake_finish)
        holder = {
            "outline": [{"id": "1", "title": "x"}],
            "failed_chapters": [],
            "resumed_chapters": 2,
            "level1_truncated": 10,
            "level1_total": 50,
            "input_fingerprint": "fp",
        }
        raw = await rv._finish_stopped_with_partial("t-stop", holder)
        payload = json.loads(raw.removeprefix("data: ").strip())
        assert payload["level1_truncated"] == 10
        assert payload["level1_total"] == 50
        assert payload["resumed_chapters"] == 2
        assert captured["ckpt"]["level1_truncated"] == 10
        assert captured["ckpt"]["input_fingerprint"] == "fp"
        assert captured["finish"] == "stopped"


# ---------------------------------------------------------------------------
# F · FIX-6 checkpoint 白名单 / TS 生成器
# ---------------------------------------------------------------------------


class TestCheckpointSchemaR213:
    def test_whitelist_and_types(self):
        from app.services import checkpoint_schema as cs

        fields = cs.CHECKPOINT_KINDS["outline_generation"][1]
        assert "level1_truncated" in fields
        assert "level1_total" in fields
        # 内部判据不得外泄到浏览器（R210 既有护栏重锚后仍生效）
        assert "input_fingerprint" not in fields
        assert cs.CHECKPOINT_FIELD_TYPES["outline_result"]["level1_truncated"] == "int"
        assert cs.CHECKPOINT_FIELD_TYPES["outline_result"]["level1_total"] == "int"

    def test_generated_ts_has_number_fields(self):
        from app.services import checkpoint_schema as cs

        ts = cs.generate_typescript_schema()
        assert "level1_truncated?: number;" in ts
        assert "level1_total?: number;" in ts
        generated = (FRONTEND_SRC / "types" / "checkpointSchema.ts").read_text(encoding="utf-8")
        assert "level1_truncated?: number;" in generated


# ---------------------------------------------------------------------------
# G · FIX-7 输入指纹覆盖面
# ---------------------------------------------------------------------------


class TestOutlineInputFingerprintCoverage:
    def test_all_prompt_inputs_are_fingerprinted(self):
        src = _src("routers/sse_handlers.py")
        start = src.index("_outline_fp = _outline_input_fingerprint(")
        seg = src[start : start + 1600]
        for name in (
            "project_brief",
            "standards_text",
            "project_facts_outline or project_facts",
            "construction_scope",
            "reference_outline",
            "requirements_text",
            "is_dangerous",
            "scheme_basis",
        ):
            assert name in seg, name
        # 新增 part 追加在末尾，不重排 R210 的 6 个旧 part
        assert seg.index("construction_scope,") < seg.index("reference_outline,")

    def test_changed_requirements_changes_fingerprint(self):
        from app.routers.sse_handlers import _outline_input_fingerprint

        base = _outline_input_fingerprint("n", "t", "brief", "std", "facts", "scope", "", "req-A", "", "")
        changed = _outline_input_fingerprint("n", "t", "brief", "std", "facts", "scope", "", "req-B", "", "")
        assert base != changed
        assert _outline_input_fingerprint("ab", "c") != _outline_input_fingerprint("a", "bc")


# ---------------------------------------------------------------------------
# H · FIX-8 一级章上限脏配置
# ---------------------------------------------------------------------------


class TestMaxLevel1Config:
    def test_negative_falls_back_to_default(self):
        from app.routers.sse_handlers import resolve_outline_max_level1

        value, warn = resolve_outline_max_level1(-5)
        assert value == 40
        assert "负数" in warn
        assert resolve_outline_max_level1(0) == (0, "")
        assert resolve_outline_max_level1(12) == (12, "")
        assert resolve_outline_max_level1("oops")[0] == 40

    def test_zero_still_means_unlimited(self):
        """0 = 显式不限（R210 契约），不得被当成非法值回退。"""
        src = _src("routers/sse_handlers.py")
        assert "if OUTLINE_MAX_LEVEL1_CHAPTERS and _level1_total > OUTLINE_MAX_LEVEL1_CHAPTERS" in src


# ---------------------------------------------------------------------------
# I · FIX-9 续跑日志
# ---------------------------------------------------------------------------


class TestResumeLogAccuracy:
    def test_denominator_is_original_level1_total(self):
        src = _src("routers/sse_handlers.py")
        start = src.index("目录断点续跑：")
        seg = src[start : start + 400]
        assert "_level1_total" in seg
        assert "len(level1)" not in seg
        assert "分母为模型原始一级章数" in seg


# ---------------------------------------------------------------------------
# J · FIX-10 提示词内部编号示例
# ---------------------------------------------------------------------------


class TestPromptNumberingConsistency:
    def test_no_hardcoded_111_example(self):
        src = _src("services/ai/prompts/content.py")
        assert "> 1.1.1 立杆稳定性验算" not in src
        assert "〔本节三级小标题，按上方规范编号〕" in src
        assert "{subheading_rule}" in src

    def test_subheading_rule_still_prefix_aware(self):
        from app.services.ai.heading_templates import build_subheading_rule

        rule = build_subheading_rule(section_prefix="3.2")
        assert "3.2.1" in rule
        assert "3.2.1.1" in rule


# ---------------------------------------------------------------------------
# K · FIX-11 配额/认证确定性失败熔断
# ---------------------------------------------------------------------------


class TestQuotaCircuitBreaker:
    def test_helpers_are_task_scoped_and_idempotent(self):
        from app.routers import _sse_outline_review as rv

        rv._clear_quota_blocked("t-helper")
        assert not rv._is_quota_blocked("t-helper")
        rv._mark_quota_blocked("t-helper")
        assert rv._is_quota_blocked("t-helper")
        assert not rv._is_quota_blocked("other-task")
        rv._clear_quota_blocked("t-helper")
        rv._clear_quota_blocked("t-helper")
        assert not rv._is_quota_blocked("t-helper")

    @pytest.mark.asyncio
    async def test_blocked_chapter_skips_ai_call(self, monkeypatch):
        from app.routers import _sse_outline_review as rv
        from app.routers import sse_handlers as sh

        calls = []

        def fake_collect(*args, **kwargs):
            calls.append(1)
            raise AssertionError("熔断后不应发起 AI 调用")

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        rv._mark_quota_blocked("t-blocked")
        try:
            st, children = await rv._fetch_chapter_children(
                0,
                {"title": "x"},
                sub_prompt=[],
                task_id="t-blocked",
                sem=None,
                validate_fn=None,
                timeout=1,
                push_stats=None,
            )
            assert (st, children) == ("failed", [])
            assert calls == []
        finally:
            rv._clear_quota_blocked("t-blocked")

    @pytest.mark.asyncio
    async def test_blocked_task_skips_directed_retry(self):
        from app.routers import _sse_outline_review as rv

        rv._mark_quota_blocked("t-retry")
        try:
            per = [("failed", [])]
            out = await rv._retry_empty_chapters(
                [(0, {"title": "x"})], per, [[]], "t-retry", None, 1, None, {}
            )
            assert out == per
        finally:
            rv._clear_quota_blocked("t-retry")

    @pytest.mark.asyncio
    async def test_batch_quota_failure_does_not_fallback_per_chapter(self, monkeypatch):
        from app.routers import _sse_outline_review as rv
        from app.routers import sse_handlers as sh

        calls = {"n": 0}

        class QuotaError(Exception):
            pass

        async def raise_quota():
            calls["n"] += 1
            raise QuotaError("402 insufficient balance")

        def fake_collect(*args, **kwargs):
            return raise_quota()

        async def fake_await(coro, push_stats, task_id=""):
            return await coro

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        monkeypatch.setattr(sh, "_await_with_stats", fake_await)
        monkeypatch.setattr(sh, "is_stopped", lambda task_id: False)
        monkeypatch.setattr(sh, "AI_RETRY_ON_QUOTA_ERROR", False)
        monkeypatch.setattr(sh, "_is_quota_error", lambda exc: True)
        rv._clear_quota_blocked("t-batch")
        try:
            unit = [(0, {"title": "a"}), (1, {"title": "b"}), (2, {"title": "c"})]
            status, per = await rv._fetch_unit_children(
                unit,
                batch_prompt=[],
                single_prompts=[[], [], []],
                task_id="t-batch",
                sem=None,
                timeout=1,
                push_stats=None,
            )
            assert status == "failed"
            assert per == [("failed", [])] * 3
            # 旧行为 = 1（批量）+ 3（逐章回退）+ 3（定向重试）
            assert calls["n"] == 1
            assert rv._is_quota_blocked("t-batch")
        finally:
            rv._clear_quota_blocked("t-batch")

    def test_entry_and_finally_clear_the_marker(self):
        from app.routers.sse_handlers import _clear_quota_blocked

        src = _src("routers/sse_handlers.py")
        assert src.count("_clear_quota_blocked(task_id)") == 2
        assert callable(_clear_quota_blocked)


# ---------------------------------------------------------------------------
# L · 端到端：编号不变式 + 信号链 + 前后端接线
# ---------------------------------------------------------------------------


class TestEndToEndChain:
    def test_child_inherits_parent_number_and_level_propagates(self):
        from app.services.outline_utils import normalize_outline, validate_outline_numbering

        tree = [
            {"title": "A", "children": [{"title": "A1", "children": [{"title": "A1a"}]}]},
            {"title": "B"},
        ]
        out = normalize_outline(copy.deepcopy(tree))
        assert (out[0]["id"], out[0]["level"]) == ("1", 1)
        assert (out[0]["children"][0]["id"], out[0]["children"][0]["level"]) == ("1.1", 2)
        assert (out[0]["children"][0]["children"][0]["id"], out[0]["children"][0]["children"][0]["level"]) == (
            "1.1.1",
            3,
        )
        assert (out[1]["id"], out[1]["level"]) == ("2", 1)
        assert validate_outline_numbering(out) == []
        # 标题改名后编号由位置重算，不应因标题变化而漂移
        out[0]["title"] = "A（改名）"
        out2 = normalize_outline(copy.deepcopy(out))
        assert out2[0]["id"] == "1"
        assert out2[0]["children"][0]["id"] == "1.1"

    def test_checkpoint_filter_keeps_user_signals_and_hides_internal_fingerprint(self):
        """holder → checkpoint 落库 → task_status 白名单过滤 → 前端可读。"""
        from app.services import checkpoint_schema as cs

        fields = set(cs.CHECKPOINT_KINDS["outline_generation"][1])
        ckpt = {
            "kind": "outline_result",
            "event": "error",
            "outline": [{"id": "1", "title": "x"}],
            "input_fingerprint": "fp",
            "resumed_chapters": 3,
            "level1_truncated": 10,
            "level1_total": 50,
        }
        filtered = {k: v for k, v in ckpt.items() if k in fields}
        assert filtered["level1_truncated"] == 10
        assert filtered["level1_total"] == 50
        assert filtered["resumed_chapters"] == 3
        assert "input_fingerprint" not in filtered

    def test_frontend_consumes_all_three_backend_signals(self):
        generation = (FRONTEND_SRC / "pages/schemeWorkbench/useOutlineGeneration.tsx").read_text(encoding="utf-8")
        actions = (FRONTEND_SRC / "pages/schemeWorkbench/useOutlineActions.tsx").read_text(encoding="utf-8")
        page = (FRONTEND_SRC / "pages/SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
        assert "outlineTruncationNotice(fin?.outline_result)" in generation
        assert "outlineClampNotice(data)" in actions
        assert "outlineUnmountedNotice(data)" in page
