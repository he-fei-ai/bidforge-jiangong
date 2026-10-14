"""R66（2026-10-11）正文生成字数偏差收口 · 护栏。

覆盖（与修复方案一一对应，判据全部来自实测/契约，不设第二套口径）：

A 档位与默认值：四档 800/1200/2000/2500、DEFAULT=1200（=标准档）、
  叶子下限 300、resolve_word_budget_tier 宽容解析与非法拒绝、
  models/schema_sql/各路由 1500 字面量清零。
B 容差带与提示文案：settings.word_budget_tolerance=0.1 时
  _word_budget_hint 与旧硬编码**逐字节**一致；tol 可配置且只影响带与文案。
C 汇总纯函数：word_deviation_of / word_audit_summary（含 tolerance **透传**
  ——首版曾漏传，ratio 1.15 在 tol=0.2 下必须算 in_band）。
D 续写机制：should_continue_round 默认逐字等价旧实现；300 下限在
  大预算下不改变结论、在小预算下由 sse 侧 min(300, budget) 钉住不越预算。
E SSE 接线（源码静态锁）：档位分支、legacy 分配下限、auto_shrink_over
  条件默认、字数校验结构化日志、section_done 偏差键、checkpoint/
  completed 的 word_audit 键与白名单。
F 审核预检 DLV-16：聚合 1 条、无预算沉默、容差带随 settings、
  注册表/引擎产出集/版本 bump/autofix 文案四处同步。
G 提示词契约与模块级重名体检（R65 教训：往老模块追加名字先 grep）。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app import models
from app.config import settings
from app.routers import sse_handlers as sse_module
from app.routers.sse_handlers import _CHECKPOINT_KINDS, _word_budget_hint
from app.services import checkpoint_schema as cs
from app.services import content_runtime as cr
from app.services import content_utils as cu
from app.services.audit_rules import (
    _PROGRAM_EMITTED_RULE_IDS,
    RULE_VERSION,
    get_rule,
    validate_rule_registry,
)
from app.services.preflight_engine import PreflightContext, check_deliverability
from app.services.review_autofix import FIX_MODE_MANUAL, capability_of

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SRC = {
    name: (_BACKEND / "app" / rel).read_text(encoding="utf-8")
    for name, rel in {
        "sse": "routers/sse_handlers.py",
        "compliance": "routers/compliance.py",
        "sections": "routers/sections.py",
        "upload_outline": "routers/upload_outline.py",
        "charts": "routers/charts.py",
        "schemes": "routers/schemes.py",
        "review_autofix": "routers/review_autofix.py",
        "schema_sql": "schema_sql.py",
        "models": "models.py",
        "prompts_content": "services/ai/prompts/content.py",
        "preflight": "services/preflight_engine.py",
    }.items()
}


# ===========================================================================
# A 档位与默认值（四档 + 300 下限 + 1500 清零）
# ===========================================================================
class TestTierConstants:
    def test_four_tiers_exact(self):
        assert cu.WORD_BUDGET_TIERS == {
            "brief": 800, "standard": 1200, "detailed": 2000, "ultra": 2500}
        assert cu.WORD_BUDGET_DEFAULT_TIER == "standard"

    def test_default_budget_equals_standard_tier(self):
        assert cu.DEFAULT_WORD_BUDGET == 1200
        assert cu.DEFAULT_WORD_BUDGET == cu.WORD_BUDGET_TIERS[cu.WORD_BUDGET_DEFAULT_TIER]

    def test_leaf_floor_is_300(self):
        assert cu.LEAF_MIN_WORD_BUDGET == 300

    @pytest.mark.parametrize("raw,expect", [
        ("brief", 800), ("STANDARD", 1200), (" detailed ", 2000), ("ultra", 2500),
        (800, 800), ("2500", 2500), (1200.0, 1200),
        (None, None), ("", None), ("   ", None), (True, None), (False, None),
        (750, None), ("999", None), (0, None), (-800, None),
        ("cheap", None), ({}, None), (["brief"], None),
    ])
    def test_resolve_tier_tolerant_and_strict(self, raw, expect):
        assert cu.resolve_word_budget_tier(raw) == expect

    def test_models_default_locks_to_constant(self):
        # models 刻意不 import services（循环导入），同值由此锁死
        assert models.SectionCreate(title="x").word_budget == cu.DEFAULT_WORD_BUDGET
        assert "word_budget: int = 1200" in _SRC["models"]

    def test_ddl_default_is_1200(self):
        assert "word_budget INTEGER DEFAULT 1200" in _SRC["schema_sql"]
        assert "word_budget INTEGER DEFAULT 1500" not in _SRC["schema_sql"]

    @pytest.mark.parametrize("key", [
        "sections", "upload_outline", "charts", "schemes", "review_autofix"])
    def test_no_1500_literal_near_word_budget(self, key):
        assert not re.search(r"word_budget[^\n]{0,60}\b1500\b", _SRC[key]), (
            f"{key}.py 仍有 1500 字面量预算，应改用 DEFAULT_WORD_BUDGET")

    def test_routers_use_single_source_constant(self):
        for key in ("sections", "upload_outline", "charts", "schemes", "review_autofix"):
            assert "DEFAULT_WORD_BUDGET" in _SRC[key], key


# ===========================================================================
# B 容差带与提示文案（默认逐字节回退）
# ===========================================================================
class TestToleranceAndHint:
    def test_settings_default_tolerance(self):
        assert settings.word_budget_tolerance == 0.1

    def test_hint_byte_identical_at_default(self):
        got = _word_budget_hint(1200)
        assert got == "1200字（可接受 0.9~1.1 倍即 1080~1320 字；硬上限 1440 字，超出即视为不合格）"

    @pytest.mark.parametrize("b", [800, 1000, 1200, 1500, 2000, 2500, 3000])
    def test_hint_equals_old_hardcoded_formula(self, b):
        # 旧实现：int(b*0.9) / int(b*1.1) / int(b*1.2) 硬编码 —— 新公式必须逐字相等
        expected = (f"{b}字（可接受 0.9~1.1 倍即 {int(b * 0.9)}~{int(b * 1.1)} 字；"
                    f"硬上限 {int(b * 1.2)} 字，超出即视为不合格）")
        assert _word_budget_hint(b) == expected

    def test_hint_configurable_tolerance(self, monkeypatch):
        monkeypatch.setattr(settings, "word_budget_tolerance", 0.2)
        got = _word_budget_hint(1200)
        assert "0.8~1.2 倍即 960~1440 字" in got
        assert "硬上限 1680 字" in got

    @pytest.mark.parametrize("b", [0, None, -10, ""])
    def test_hint_empty_for_no_budget(self, b):
        assert _word_budget_hint(b) == ""

    @pytest.mark.parametrize("wc,wb,in_band", [
        (900, 1000, True), (1100, 1000, True),        # 边界恰在 0.9/1.1
        (890, 1000, False), (1111, 1000, False),      # 带外
        (1080, 1200, True), (1320, 1200, True),       # 档位标准边界
    ])
    def test_deviation_band_edges(self, wc, wb, in_band):
        dev = cu.word_deviation_of(wc, wb)
        assert dev["within_band"] is in_band
        assert dev["deviation"] == wc - wb
        assert dev["ratio"] == round(wc / wb, 4)

    def test_dirty_budget_falls_back_to_default(self):
        dev = cu.word_deviation_of(600, 0)
        assert dev["deviation"] == 600 - cu.DEFAULT_WORD_BUDGET

    def test_tolerance_kwarg_moves_the_band(self):
        assert cu.word_deviation_of(1150, 1000)["within_band"] is False
        assert cu.word_deviation_of(1150, 1000, tolerance=0.2)["within_band"] is True


# ===========================================================================
# C word_audit_summary（可追溯汇总，tolerance 必须透传）
# ===========================================================================
class TestWordAuditSummary:
    def test_counts_and_items(self):
        rows = [
            {"section_id": "a", "title": "A", "level": 3, "word_budget": 1200, "word_count": 1200},
            {"section_id": "b", "title": "B", "level": 3, "word_budget": 1200, "word_count": 2100},
            {"section_id": "c", "title": "C", "level": 3, "word_budget": 1200, "word_count": 250},
        ]
        out = cu.word_audit_summary(rows)
        assert out["total"] == 3
        assert out["in_band"] == 1 and out["under_band"] == 2
        assert out["over"] == 1 and out["under"] == 1
        assert out["below_leaf_floor"] == 1
        assert out["items"][1]["deviation"] == 900
        assert out["items"][1]["within_band"] is False
        assert out["items"][1]["word_status"] == "over"

    def test_tolerance_propagates_into_band(self):
        # 首版缺陷回归锁：summary 收了 tolerance 却没传给 word_deviation_of
        rows = [{"title": "X", "word_budget": 1200, "word_count": 1380}]
        assert cu.word_audit_summary(rows)["in_band"] == 0
        assert cu.word_audit_summary(rows, tolerance=0.2)["in_band"] == 1

    def test_median_mean_and_empty(self):
        empty = cu.word_audit_summary([])
        assert empty["total"] == 0 and empty["median_ratio"] == 0.0
        rows = [
            {"word_budget": 1000, "word_count": 500},
            {"word_budget": 1000, "word_count": 1000},
            {"word_budget": 1000, "word_count": 2000},
        ]
        out = cu.word_audit_summary(rows)
        assert out["median_ratio"] == 1.0
        assert out["mean_ratio"] == round((0.5 + 1.0 + 2.0) / 3, 4)

    def test_dirty_rows_tolerated(self):
        out = cu.word_audit_summary([{}, {"word_count": None, "word_budget": "x"}])
        assert out["total"] == 2


# ===========================================================================
# D 续写机制（默认逐字等价 + 300 下限 + 不越显式预算）
# ===========================================================================
class TestContinuationRuntime:
    def _call(self, wc, budget, floor=0, count=1, passes=0, rounds=2):
        return cr.should_continue_round(
            wc=wc, word_budget=budget, continue_count=count,
            min_passes=passes, max_rounds=rounds, min_floor_chars=floor)

    def test_default_equals_legacy_80pct(self):
        assert self._call(1199, 1500) is True    # 旧阈值 0.8×1500=1200
        assert self._call(1200, 1500) is False

    def test_floor_invisible_when_80pct_exceeds_it(self):
        # 预算 1200：0.8×1200=960 > 300 ⇒ 加下限不改变任何结论
        for wc in (959, 960, 961):
            assert self._call(wc, 1200) == self._call(wc, 1200, floor=300)

    def test_floor_bites_for_small_budget(self):
        # 预算 320：旧阈值 256，wc=280 旧判定 False；下限 300 后 True
        assert self._call(280, 320) is False
        assert self._call(280, 320, floor=300) is True

    def test_sse_clamp_floor_never_exceeds_explicit_budget(self):
        # sse 侧传 min(300, budget)：预算 200 ⇒ 下限 200，与续写上限 1.1×200=220 兼容
        floor = min(cu.LEAF_MIN_WORD_BUDGET, 200)
        assert self._call(190, 200, floor=floor) is True
        assert self._call(200, 200, floor=floor) is False

    def test_cap_factor_default_byte_compat(self):
        msgs = cr.build_continuation_messages(
            user_ctx="ctx", cont_tail="tail", wc=500, word_budget=1200,
            cont_prompt="p", eff_standard="precise")
        assert "总字数上限为 1320 字" in msgs[3]["content"]

    def test_cap_factor_follows_tolerance(self):
        msgs = cr.build_continuation_messages(
            user_ctx="ctx", cont_tail="tail", wc=500, word_budget=1200,
            cont_prompt="p", eff_standard="precise", total_cap_factor=1.2)
        assert "总字数上限为 1440 字" in msgs[3]["content"]


# ===========================================================================
# E SSE 接线（源码静态锁 —— 逻辑埋在 7 千行闭包里，值级单测够不着）
# ===========================================================================
class TestSseWiringSource:
    def test_tier_parsed_from_body(self):
        assert 'resolve_word_budget_tier(body.get("word_budget_tier"))' in _SRC["sse"]

    def test_auto_shrink_conditional_default(self):
        assert ("_shrink_default = word_budget_tier is not None "
                "or word_budget_override is not None") in _SRC["sse"]
        assert 'body.get("auto_shrink_over"), default=_shrink_default' in _SRC["sse"]

    def test_tier_floor_and_persistence(self):
        assert "max(int(word_budget_tier), LEAF_MIN_WORD_BUDGET)" in _SRC["sse"]
        assert "UPDATE sections SET word_budget=? WHERE id=?" in _SRC["sse"]

    def test_legacy_allocation_floor_at_apply(self):
        # 真实源码是跨行调用：去尽一切空白后匹配，防格式漂移造成假失配
        flat = re.sub(r"\s+", "", _SRC["sse"])
        assert ('max(u["alloc"].get(lf["id"],word_budget_override),'
                'LEAF_MIN_WORD_BUDGET)') in flat

    def test_apply_fn_itself_untouched_by_floor(self):
        # 守恒契约由既有性能测试锁死（total_budget=100），下限绝不能进这个函数
        src = inspect.getsource(sse_module._apply_word_budget_allocations)
        assert "LEAF_MIN_WORD_BUDGET" not in src

    def test_continuation_wiring(self):
        assert "min_floor_chars=min(LEAF_MIN_WORD_BUDGET, int(word_budget or 0))" in _SRC["sse"]
        assert "total_cap_factor=1.0 + _cont_tol" in _SRC["sse"]

    def test_traceability_log_and_audit_rows(self):
        assert "字数校验|节=%s" in _SRC["sse"]
        assert '"audit_rows"' in _SRC["sse"]

    def test_section_done_deviation_keys(self):
        for k in ('"word_deviation"', '"word_ratio"', '"word_within_band"'):
            assert k in _SRC["sse"], k

    def test_word_audit_in_checkpoint_and_completed(self):
        assert "completed_payload['word_audit'] = word_audit_summary(" in _SRC["sse"]
        assert _SRC["sse"].count("word_audit_summary(") >= 2
        # _CHECKPOINT_KINDS[kind] = (结果名, 字段白名单元组)
        assert "word_audit" in _CHECKPOINT_KINDS["content_generation"][1]

    def test_schema_parity_and_ts_regen(self):
        assert "word_audit" in cs.CHECKPOINT_KINDS["content_generation"][1]
        assert cs.CHECKPOINT_FIELD_TYPES["content_result"]["word_audit"] == "dict"
        ts = (_BACKEND.parent / "frontend" / "src" / "types" / "checkpointSchema.ts")
        assert ts.exists() and "word_audit" in ts.read_text(encoding="utf-8")


# ===========================================================================
# F 审核预检 DLV-16
# ===========================================================================
def _leaf(sec_id, title, wc, wb, content="x"):
    return {"id": sec_id, "parent_id": "p", "title": title, "content": content * wc,
            "word_count": wc, "level": 3, "word_budget": wb, "status": "done",
            "sort_order": int(sec_id) if sec_id.isdigit() else 1}


def _dlv16(findings):
    return [f for f in findings if f["rule_id"] == "DLV-16"]


class TestPreflightDlv16:
    def test_aggregates_to_single_finding(self):
        secs = [{"id": "p", "parent_id": "", "title": "父", "content": "",
                 "word_count": 0, "level": 1, "word_budget": 0, "sort_order": 0}]
        secs += [_leaf("1", "超纲A", 2100, 1200), _leaf("2", "低限B", 250, 1200),
                 _leaf("3", "达标C", 1150, 1200)]
        d = _dlv16(check_deliverability(PreflightContext(scheme_id="s", sections=secs)))
        assert len(d) == 1  # 问题数不随偏离章节数膨胀
        assert "偏离目标容差带" in d[0]["detail"]
        assert "达标 1/3" in d[0]["detail"]
        ev = "\n".join(d[0]["evidence"])
        assert "超纲A" in ev and "超目标 175%" in ev
        assert "低限B" in ev and "低于 300 字下限" in ev
        assert "达标C" not in ev

    def test_silent_without_budget(self):
        secs = [{"id": "1", "parent_id": "", "title": "T", "content": "x" * 3000,
                 "word_count": 3000, "level": 3, "sort_order": 1}]
        assert _dlv16(check_deliverability(
            PreflightContext(scheme_id="s", sections=secs))) == []

    def test_silent_when_all_in_band(self):
        secs = [_leaf("1", "A", 1200, 1200), _leaf("2", "B", 1080, 1200)]
        assert _dlv16(check_deliverability(
            PreflightContext(scheme_id="s", sections=secs))) == []

    def test_parents_never_judged(self):
        # p1 有 children 且字数严重超纲（3000 vs 1200）——**不得**被 DLV-16 判定；
        # 叶子 c1 恰在带内 ⇒ 整场景无 DLV-16。若判据错把父章节拉进来说明
        # has_children 过滤失效。
        secs = [{"id": "1", "parent_id": "", "title": "父", "content": "x" * 3000,
                 "word_count": 3000, "level": 1, "word_budget": 1200, "sort_order": 0},
                {"id": "2", "parent_id": "1", "title": "子", "content": "y" * 1200,
                 "word_count": 1200, "level": 2, "word_budget": 1200, "sort_order": 1}]
        assert _dlv16(check_deliverability(
            PreflightContext(scheme_id="s", sections=secs))) == []

    def test_evidence_capped_at_10(self):
        secs = [_leaf(str(i), f"S{i}", 2100, 1200) for i in range(1, 13)]
        d = _dlv16(check_deliverability(PreflightContext(scheme_id="s", sections=secs)))
        assert len(d) == 1
        assert "12 个三级目录" in d[0]["detail"]
        assert len(d[0]["evidence"]) == 10

    def test_tolerance_driven_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "word_budget_tolerance", 0.2)
        secs = [_leaf("1", "1.15倍", 1380, 1200)]
        assert _dlv16(check_deliverability(
            PreflightContext(scheme_id="s", sections=secs))) == []
        monkeypatch.setattr(settings, "word_budget_tolerance", 0.1)
        assert len(_dlv16(check_deliverability(
            PreflightContext(scheme_id="s", sections=secs)))) == 1

    def test_registry_and_emitted_set_and_autofix(self):
        rule = get_rule("DLV-16")
        assert rule.mode == "program" and rule.dimension == "deliverability"
        assert rule.severity == "medium"
        assert "DLV-16" in _PROGRAM_EMITTED_RULE_IDS
        assert validate_rule_registry(emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS) == []
        cap = capability_of("DLV-16")
        assert cap.mode == FIX_MODE_MANUAL and "正文生成" in cap.reason

    def test_rule_version_bumped(self):
        assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 10)

    def test_compliance_select_carries_word_budget(self):
        assert "word_count, word_budget, level" in _SRC["compliance"]


# ===========================================================================
# G 提示词契约 + 模块级重名体检（R65 教训）
# ===========================================================================
class TestPromptContractsAndShadowing:
    def test_prompt_clauses_present(self):
        src = _SRC["prompts_content"]
        assert "记作 X；若【目标字数】给出了可接受区间与硬上限" in src
        assert "不得为省字数而省略必含要素" in src
        assert "写完后自检一遍字数" in src
        assert "不少于 300 字（三级目录正文下限）" in src

    @pytest.mark.parametrize("fname", [
        "services/content_utils.py", "services/content_runtime.py",
        "services/preflight_engine.py"])
    def test_no_duplicate_module_level_names(self, fname):
        path = _BACKEND / "app" / fname
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
        dup = {n for n in names if names.count(n) > 1}
        assert not dup, f"{fname} 模块级重名（静默覆盖风险）: {sorted(dup)}"
