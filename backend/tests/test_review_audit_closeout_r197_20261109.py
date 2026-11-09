"""R197（2026-11-09）· 审核与预检模块深度审计收口护栏。

覆盖本轮落地的 12 项（行为实证 + 源码棘轮锁）：
- B1  ``_normalize_ai_results`` 字符串布尔 ``hit`` 归一（"true"/"false" → 真布尔）；
- B2  rule_id 顺序回退仅在「返回条数 == 清单条数」时启用（防漏判/乱序时下标强配错位）；
- B3  严重度档位词归一（中文「高/中/低/阻断」+ "block"）——
      ``normalize_severity_text`` 单一实现，评分与合并口径统一；
- B4  ``_context`` 首行命中不再 rfind(-1) 跳到全文末行；
- B5  ``_match_colloquial`` 统一在原文坐标系定位（剥离围栏不再使 pos 前移）；
- B6  confirm 非前缀路径零差异章节不进 pending（源码棘轮）；
- B7  ``_persist_fixed`` 落库前复查生成守卫（源码棘轮）；
- B8  ``reset_review_on_content_change`` 退回 UPDATE 改 compare-and-set（行为实证 0 行 → False）；
- B9  collect/stage 非法 scope 显式 422（行为实证 + 接线源码锁）；
- B10 断线兜底 checkpoint 带 ``partial: True``（行为实证）;
- B11 ``_CHECKPOINT_KINDS`` 由 checkpoint_schema 单一事实源派生（运行时恒等）；
- B12 ``/rollback`` 后失效一致性扫描缓存（源码棘轮）。
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# 源码读取 helper（与既有 closeout 测试同款）
# ---------------------------------------------------------------------------
_ROOT = "app"


def _src(relpath: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / relpath).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B1 / B2：_normalize_ai_results
# ---------------------------------------------------------------------------


class TestNormalizeAiResults:
    def _rule_ids(self) -> list[str]:
        from app.services.audit_rules import ALL_RULES

        return [ALL_RULES[0].rule_id, ALL_RULES[1].rule_id]

    def test_string_boolean_hit_normalized(self):
        """B1：字符串 "true"/"false" 必须归一为真布尔（评分侧只认 hit is True）。"""
        from app.routers.compliance import _normalize_ai_results

        rid = self._rule_ids()[0]
        out = _normalize_ai_results(
            [
                {"rule_id": rid, "item": "项A", "hit": "true"},
                {"rule_id": rid, "item": "项B", "hit": "false"},
            ],
            [],
            [rid],
        )
        assert out[0]["hit"] is True, out
        assert out[1]["hit"] is False, out

    def test_real_boolean_untouched(self):
        from app.routers.compliance import _normalize_ai_results

        rid = self._rule_ids()[0]
        out = _normalize_ai_results([{"rule_id": rid, "item": "项A", "hit": True}], [], [rid])
        assert out[0]["hit"] is True

    def test_index_fallback_only_when_counts_match(self):
        """B2：条数不等时不得按列表下标强配（会把 A 规则的结论安到 B 规则头上）。"""
        from app.routers.compliance import _normalize_ai_results

        rid_a, rid_b = self._rule_ids()
        # 模型漏判一项：清单 2 条、返回 1 条（rule_id 不可识别）
        out = _normalize_ai_results(
            [{"rule_id": "R9", "item": "未知清单项"}],
            [],
            [rid_a, rid_b],
        )
        assert out[0]["rule_id"] != rid_a, "条数不等时按下标回退 = 把清单第 1 项的结论强配给返回项"

    def test_index_fallback_still_works_when_counts_match(self):
        """B2 反向：条数相等时顺序回退保持可用（向后兼容，不因修复而丢失能力）。"""
        from app.routers.compliance import _normalize_ai_results

        rid_a, rid_b = self._rule_ids()
        out = _normalize_ai_results(
            [
                {"rule_id": "R9", "item": "未知项一"},
                {"rule_id": "R9", "item": "未知项二"},
            ],
            [],
            [rid_a, rid_b],
        )
        assert [o["rule_id"] for o in out] == [rid_a, rid_b]


# ---------------------------------------------------------------------------
# B3：严重度档位归一
# ---------------------------------------------------------------------------


class TestSeverityNormalization:
    def test_normalize_severity_text_mapping(self):
        from app.services.audit_scoring import normalize_severity_text

        assert normalize_severity_text("高") == "high"
        assert normalize_severity_text("中") == "medium"
        assert normalize_severity_text("低") == "low"
        assert normalize_severity_text("阻断") == "block"
        assert normalize_severity_text("严重") == "high"
        assert normalize_severity_text("HIGH") == "high"
        assert normalize_severity_text("block") == "block"
        assert normalize_severity_text("") == ""
        assert normalize_severity_text(None) == ""

    def test_chinese_severity_not_downgraded(self):
        """B3：AI 返回中文「高」不得被降为 medium（锚定失真）。"""
        from app.services.audit_scoring import ai_results_to_findings

        findings = ai_results_to_findings([{"rule_id": "", "item": "x", "hit": False, "severity": "高"}])
        assert findings, "未命中必须产出 finding"
        assert findings[0]["severity"] != "medium", findings

    def test_chinese_low_with_uncertain_hit_not_penalized(self):
        """B3：「疑似低风险不扣分」策略对中文「低」同样生效。"""
        from app.services.audit_scoring import ai_results_to_findings

        findings = ai_results_to_findings([{"rule_id": "", "item": "x", "hit": None, "severity": "低"}])
        assert findings == [], "hit=None + 低风险 = 不扣分仅提示"

    def test_merge_check_results_chinese_severity_comparison(self):
        """B3：合并口径的「取更高一档」对中文档位与 block 同样成立。"""
        from app.routers.compliance import _merge_check_results

        merged: dict = {}
        _merge_check_results(
            merged,
            [{"rule_id": "r1", "item": "项", "hit": False, "severity": "中", "suggestion": "s1"}],
        )
        _merge_check_results(
            merged,
            [{"rule_id": "r1", "item": "项", "hit": False, "severity": "高", "suggestion": "s2"}],
        )
        assert merged["r1"]["severity"] == "高", "中文「高」必须被识别为更高档位"

    def test_merge_check_results_block_rank(self):
        from app.routers.compliance import _merge_check_results

        merged: dict = {}
        _merge_check_results(
            merged,
            [{"rule_id": "r1", "item": "项", "hit": False, "severity": "high", "suggestion": "s1"}],
        )
        _merge_check_results(
            merged,
            [{"rule_id": "r1", "item": "项", "hit": False, "severity": "block", "suggestion": "s2"}],
        )
        assert merged["r1"]["severity"] == "block", "block 档位不得在合并时被当作 0 分丢弃"


# ---------------------------------------------------------------------------
# B4：_context 首行命中
# ---------------------------------------------------------------------------


class TestContextFirstLine:
    def test_first_line_hit_does_not_jump_to_eof(self):
        from app.services.review_autofix import _context

        content = "第一行命中目标\n第二行\n第三行\n第四行\n第五行"
        got = _context(content, 0, 4)
        assert got.startswith("第一行"), got
        assert "第五行" not in got, "首行命中时向前扩展不得 rfind(-1) 跳到全文末行"

    def test_mid_document_hit_unchanged(self):
        from app.services.review_autofix import _context

        content = "第一行\n第二行命中目标\n第三行\n第四行"
        got = _context(content, content.index("命中"), 2)
        assert "第二行" in got, got
        assert "第一行" in got and "第三行" in got, got
        assert "第四行" not in got, got


# ---------------------------------------------------------------------------
# B5：_match_colloquial 原文坐标系
# ---------------------------------------------------------------------------


class TestMatchColloquialCoordinates:
    def test_pos_maps_to_original_content_after_fence(self, monkeypatch):
        """命中点之前有代码围栏时 pos 不得前移（旧实现在剥离文本上 find）。"""
        from app.services import review_autofix as ra

        hit = "总体来说"
        content = "```\nprint('fence block')\n```\n正文开头。" + hit + "质量可以。"
        monkeypatch.setattr(ra, "find_colloquial_hits", lambda text: [hit])
        results = list(ra._match_colloquial(content))
        assert results, "命中项必须被定位"
        pos, matched = results[0]
        assert content[pos : pos + len(hit)] == hit, f"pos={pos} 不在原文坐标系上（matched={matched}）"
        assert "```" not in content[pos - 5 : pos + 10]


# ---------------------------------------------------------------------------
# B8：reset_review_on_content_change CAS
# ---------------------------------------------------------------------------


class TestResetReviewCAS:
    async def test_zero_rows_returns_false(self, monkeypatch):
        """CAS 行为实证：UPDATE 影响 0 行（状态已被并发方改写）→ 如实返回 False。"""
        from app.services import review_db

        async def _fake_fetch_one(db, sql, params=(), *, what="查询"):
            return {"review_status": "approved", "title": "章节"}

        async def _fake_exec_write(db, sql, params=(), *, what="写操作", require_rows=True):
            assert "AND review_status=?" in sql, "退回 UPDATE 必须带期望状态条件（compare-and-set）"
            assert require_rows is False, "0 行是「被并发改写」的合法结果，不得抛 503"
            return 0

        monkeypatch.setattr(review_db, "fetch_one", _fake_fetch_one)
        monkeypatch.setattr(review_db, "exec_write", _fake_exec_write)

        from app.routers import review as review_router

        ok = await review_router.reset_review_on_content_change(None, "sch-x", "sec-x")
        assert ok is False, "0 行 = 未退回，不得汇报成功"

    def test_source_lock_cas_predicate(self):
        src = _src("app/routers/review.py")
        assert 'WHERE id=? AND scheme_id=? AND review_status=?' in src


# ---------------------------------------------------------------------------
# B9：scope 枚举校验
# ---------------------------------------------------------------------------


class TestScopeValidation:
    async def test_valid_scopes_pass(self):
        from app.routers.review_autofix import _validate_scope

        for s in ("all_blocking", "auto_fixable", "all"):
            await _validate_scope(s)  # 不抛即通过

    async def test_invalid_scope_422(self):
        from app.routers.review_autofix import _validate_scope

        with pytest.raises(HTTPException) as ei:
            await _validate_scope("everything")
        assert ei.value.status_code == 422
        assert "scope" in str(ei.value.detail)

    def test_wiring_in_collect_and_stage(self):
        """源码棘轮：/collect 与 /stage 都必须在解析 scope 后立即校验。"""
        src = _src("app/routers/review_autofix.py")
        wired = len(re.findall(r"await _validate_scope\(scope\)", src))
        assert wired >= 2, f"/collect 与 /stage 必须各接线一次 _validate_scope（当前 {wired} 处）"


# ---------------------------------------------------------------------------
# B6 / B7 / B12：confirm 零差异守卫 + 落库生成守卫 + rollback 缓存失效（源码棘轮）
# ---------------------------------------------------------------------------


class TestAutofixRouterSourceLocks:
    @pytest.fixture()
    def src(self) -> str:
        return _src("app/routers/review_autofix.py")

    def test_confirm_zero_diff_not_persisted(self, src):
        """B6：链式重算返回原文（全 failed）时不得进 pending。"""
        assert 'if _content != (section.get("content") or ""):' in src

    def test_persist_fixed_guard_before_snapshot(self, src):
        """B7：_persist_fixed 体内必须在快照前复查生成守卫。"""
        i = src.index("async def _persist_fixed")
        j = src.index("create_snapshot", i)
        body = src[i:j]
        assert '_guard_not_generating(scheme_id, "自动修复落库")' in body

    def test_rollback_invalidates_consistency_cache(self, src):
        """B12：rollback 恢复正文后必须失效一致性扫描缓存（与 _persist_fixed 同口径）。"""
        i = src.index("@router.post(\"/rollback\")")
        j = src.index('@router.get("/repairs")', i)
        body = src[i:j]
        assert "invalidate_consistency_scan_cache" in body


# ---------------------------------------------------------------------------
# B10：断线兜底 checkpoint 带 partial
# ---------------------------------------------------------------------------


class TestPartialCheckpoint:
    async def test_partial_true_always_present(self, monkeypatch):
        from app.routers import sse_checkpoint as sc

        captured: list[dict] = []

        async def _fake_save(task_id: str, payload: dict):
            captured.append(dict(payload))

        monkeypatch.setattr(sc, "_save_outline_checkpoint", _fake_save)
        ok = await sc._checkpoint_partial_outline("task-1", [{"title": "第一章"}], ["第一章"])
        assert ok is True
        assert captured, "payload 必须落库"
        assert captured[0]["partial"] is True, "部分成果出口必须带 partial=True（前端 buildPartialOutlineHint 依赖）"

    async def test_partial_true_with_failed_chapters(self, monkeypatch):
        from app.routers import sse_checkpoint as sc

        captured: list[dict] = []

        async def _fake_save(task_id: str, payload: dict):
            captured.append(dict(payload))

        monkeypatch.setattr(sc, "_save_outline_checkpoint", _fake_save)
        await sc._checkpoint_partial_outline("task-2", [], ["章A", "章B"])
        assert captured[0]["partial"] is True
        assert captured[0]["failed_count"] == 2


# ---------------------------------------------------------------------------
# B11：_CHECKPOINT_KINDS 派生
# ---------------------------------------------------------------------------


def test_checkpoint_kinds_derived_from_schema():
    import app.routers.sse_handlers as sh
    from app.services import checkpoint_schema as cs

    assert sh._CHECKPOINT_KINDS == cs.CHECKPOINT_KINDS, "白名单必须从单一事实源派生（手抄副本已废除）"
    # 运行时 parity：三族 kind 与字段集逐项一致
    assert set(sh._CHECKPOINT_KINDS) == {"outline_generation", "content_generation", "facts_generation"}
