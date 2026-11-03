"""审核与预检模块遗留收口护栏（R98 · 2026-10-17）

R95 §6 遗留清单（源自 R90 §7）中本轮落地的三项修复 + 两项取证结论，
逐项做成可执行断言：

  A 组 · 守卫原语行为契约 · sections.ensure_no_generation_write
         （正文在跑→409 文案A / 目录在跑→409 文案B / 两级顺序 / 日志参数）
  B 组 · charts._guard_chart_target 反查与 fail-open
         （section 反查 / prediction 反查 / 查无不拦 / 反查异常不拦 / 空参不查库）
  C 组 · 9 处正文写点接线静态源码锁
         （shrink / autofix apply·rollback·confirm / consistency repair·confirm·rollback
           / charts fix-mermaid·generate-ai-image —— 一律锚调用形态于具体端点函数源码）
  D 组 · DLV 口径唯一实现 content_coverage_stats
         （公式数值锁 / 空输入边界 / preflight_stats 共用锁 / export 共用与旧口径不回流锁）
  E 组 · 导出缓存指纹双维取证锁（R90 遗留⑧「charts 不接 invalidate」的判据）
         （改正文 miss / 改 data_json miss / 同输入稳定 / charts 写点零 invalidate 直调）
  F 组 · MONITOR_CATEGORIES ⊆ CATEGORY_STANDARDS 子集锁（R90 遗留④的不动手部分）
  G 组 · stage_label 回退口径两份副本一致锁（R90 遗留②最小加固）

判据纪律（AGENTS §5.14）：静态锁一律锚**调用形态**（函数名+实参）或 AST 节点，
不锚裸字符串是否存在；计数型断言配非空自证。

编号说明：原拟 R96 与并行会话（导出模块收口 test_export_closeout_r96_20261017）
撞号，按「后来者让号」惯例改号 R98（R93 先例）。
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

import app.routers.charts as charts_mod
import app.routers.consistency_repair as consistency_repair_mod
import app.routers.export as export_mod
import app.routers.review_autofix as review_autofix_mod
import app.routers.sections as sections_mod
import app.routers.sse_handlers as sse_handlers_mod
import app.services.content_progress as content_progress_mod
import app.services.preflight_engine as preflight_mod
import pytest
from app.services.preflight_engine import PreflightContext, content_coverage_stats
from app.services.standards_registry import CATEGORY_STANDARDS
from fastapi import HTTPException

APP_DIR = Path(sections_mod.__file__).resolve().parents[1]

SCHEME = "sch-r97"


def _src(fn) -> str:
    return inspect.getsource(fn)


# ---------------------------------------------------------------------------
# A 组 · 守卫原语行为契约
# ---------------------------------------------------------------------------

class TestGuardPrimitive:
    """ensure_no_generation_write 的两级判定与文案契约。"""

    def test_no_task_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(sections_mod, "_scheme_task_status_in_progress",
                            lambda *a, **kw: None)
        sections_mod.ensure_no_generation_write(SCHEME, "单元测试动作")

    def test_content_running_raises_409_with_action(self, monkeypatch):
        monkeypatch.setattr(
            sections_mod, "_scheme_task_status_in_progress",
            lambda sid, t: "running" if t == "content_generation" else None)
        with pytest.raises(HTTPException) as ei:
            sections_mod.ensure_no_generation_write(SCHEME, "自动修复落库")
        assert ei.value.status_code == 409
        assert "正文正在后台生成中" in str(ei.value.detail)
        # 动作名必须进文案（用户要知道是哪个操作被拦）
        assert "自动修复落库" in str(ei.value.detail)

    def test_content_paused_also_blocked(self, monkeypatch):
        # 口径与既有 update_section 守卫逐字一致：running 与 paused 都拦
        monkeypatch.setattr(
            sections_mod, "_scheme_task_status_in_progress",
            lambda sid, t: "paused" if t == "content_generation" else None)
        with pytest.raises(HTTPException) as ei:
            sections_mod.ensure_no_generation_write(SCHEME, "字数压缩")
        assert ei.value.status_code == 409

    def test_outline_running_raises_409_outline_wording(self, monkeypatch):
        monkeypatch.setattr(
            sections_mod, "_scheme_task_status_in_progress",
            lambda sid, t: "running" if t == "outline_generation" else None)
        with pytest.raises(HTTPException) as ei:
            sections_mod.ensure_no_generation_write(SCHEME, "全文一致性修复")
        assert ei.value.status_code == 409
        assert "目录正在后台生成中" in str(ei.value.detail)
        assert "整表重建" in str(ei.value.detail)

    def test_content_takes_priority_over_outline(self, monkeypatch):
        # 双任务同时在跑：文案必须是「正文」形态，且 outline 查询不发生
        seen: list[str] = []

        def _fake(sid, task_type):
            seen.append(task_type)
            return "running"
        monkeypatch.setattr(sections_mod, "_scheme_task_status_in_progress", _fake)
        with pytest.raises(HTTPException) as ei:
            sections_mod.ensure_no_generation_write(SCHEME, "批量修复确认")
        assert "正文正在后台生成中" in str(ei.value.detail)
        assert seen == ["content_generation"]

    def test_primitive_delegates_to_unique_entries(self):
        # 判据不另起炉灶：原语必须经 content/outline 两个「唯一入口」函数
        src = _src(sections_mod.ensure_no_generation_write)
        assert "content_generation_in_progress(scheme_id)" in src
        assert "outline_generation_in_progress(scheme_id)" in src
        assert "_scheme_task_status_in_progress" not in src  # 不直接碰底层

    def test_warning_log_carries_action_scheme_and_status(self, monkeypatch, caplog):
        monkeypatch.setattr(
            sections_mod, "_scheme_task_status_in_progress",
            lambda sid, t: "running" if t == "content_generation" else None)
        with caplog.at_level("WARNING", logger="sections"):
            with pytest.raises(HTTPException):
                sections_mod.ensure_no_generation_write(SCHEME, "图表代码修复")
        msgs = [r.getMessage() for r in caplog.records
                if r.name == "sections" and r.levelname == "WARNING"]
        assert any("图表代码修复" in m and SCHEME in m and "running" in m
                   for m in msgs), f"WARNING 日志要素不全: {msgs}"


# ---------------------------------------------------------------------------
# B 组 · charts._guard_chart_target 反查与 fail-open
# ---------------------------------------------------------------------------

class _FakeCur:
    def __init__(self, row, raises: bool = False):
        self._row, self._raises = row, raises

    async def fetchone(self):
        if self._raises:
            raise RuntimeError("模拟库连接炸了")
        return self._row


class _FakeDB:
    def __init__(self, row=None, raises: bool = False):
        self._row, self._raises = row, raises
        self.sqls: list[str] = []

    async def execute(self, sql, params=()):
        self.sqls.append(sql)
        return _FakeCur(self._row, self._raises)


async def _probe_guard(db, section_id="", action="图表代码修复", prediction_id=""):
    seen: list[tuple[str, str]] = []

    def _fake(sid, task_type):
        # 用底层唯一入口做探针：ensure_no_generation_write 依序经
        # content→outline 两级判定调用底层，记录 (scheme_id, task_type) 全景，
        # 既观测「是否进入守卫」也观测「outline 二级确实被走到（无任务时）」
        seen.append((sid, task_type))
        return None
    orig = sections_mod._scheme_task_status_in_progress
    sections_mod._scheme_task_status_in_progress = _fake
    try:
        await charts_mod._guard_chart_target(db, section_id, action,
                                             prediction_id=prediction_id)
    finally:
        sections_mod._scheme_task_status_in_progress = orig
    return [sid for sid, t in seen if t == "content_generation"], seen


class TestChartGuardResolution:
    async def test_section_id_resolves_scheme(self):
        db = _FakeDB(row={"scheme_id": "S1"})
        called, seen = await _probe_guard(db, section_id="sec1")
        assert called == ["S1"]
        # 无任务时两级判定都要走到（content → outline），不半途短路
        assert [t for _, t in seen] == ["content_generation", "outline_generation"]
        assert "FROM sections" in db.sqls[0]

    async def test_prediction_id_fallback(self):
        db = _FakeDB(row={"scheme_id": "S2"})
        called, _ = await _probe_guard(db, section_id="", prediction_id="pred1")
        assert called == ["S2"]
        assert "FROM chart_predictions" in db.sqls[0]

    async def test_missing_row_fail_open(self):
        # 查不到章节 → 绝不拦（守卫不比被保护路径更"知道"章节存在，404 归原链路）
        db = _FakeDB(row=None)
        called, _ = await _probe_guard(db, section_id="ghost")
        assert called == []

    async def test_db_error_fail_open(self):
        db = _FakeDB(raises=True)
        called, _ = await _probe_guard(db, section_id="sec1")
        assert called == []

    async def test_empty_ids_no_db_query(self):
        db = _FakeDB(row={"scheme_id": "S9"})
        called, _ = await _probe_guard(db, section_id="", prediction_id="")
        assert called == [] and db.sqls == []

    async def test_scheme_id_column_null_fail_open(self):
        db = _FakeDB(row={"scheme_id": None})
        called, _ = await _probe_guard(db, section_id="sec1")
        assert called == []

    async def test_guard_blocks_when_generating(self):
        # 反查到 scheme 且任务在跑 → 必须真实抛 409（不是只「委派了就完事」）
        orig = sections_mod._scheme_task_status_in_progress
        sections_mod._scheme_task_status_in_progress = (
            lambda sid, t: "running" if t == "content_generation" else None)
        try:
            with pytest.raises(HTTPException) as ei:
                await charts_mod._guard_chart_target(
                    _FakeDB(row={"scheme_id": "S1"}), "sec1", "AI 配图生成")
            assert ei.value.status_code == 409
            assert "AI 配图生成" in str(ei.value.detail)
        finally:
            sections_mod._scheme_task_status_in_progress = orig


# ---------------------------------------------------------------------------
# C 组 · 9 处正文写点接线静态源码锁（锚调用形态于端点函数源码）
# ---------------------------------------------------------------------------

WIRING = [
    (sections_mod.shrink_section, "ensure_no_generation_write(scheme_id, \"字数压缩\")"),
    (review_autofix_mod.apply, "_guard_not_generating(scheme_id, \"自动修复落库\")"),
    (review_autofix_mod.rollback, "_guard_not_generating(scheme_id, \"自动修复回滚\")"),
    (review_autofix_mod.confirm, "_guard_not_generating(scheme_id, \"批量修复确认\")"),
    (consistency_repair_mod.repair, "ensure_no_generation_write(scheme_id, \"全文一致性修复\")"),
    (consistency_repair_mod.confirm, "ensure_no_generation_write(scheme_id, \"一致性修复拒绝恢复\")"),
    (consistency_repair_mod.rollback, "ensure_no_generation_write(scheme_id, \"一致性修复回滚\")"),
    (charts_mod.fix_mermaid, "_guard_chart_target(db, section_id, \"图表代码修复\""),
    (charts_mod.generate_ai_image, "_guard_chart_target(db, section_id, \"AI 配图生成\")"),
]


class TestWiringSourceLocks:
    @pytest.mark.parametrize("fn, anchor", WIRING,
                             ids=[f.__name__ + "-" + str(i)
                                  for i, (f, _) in enumerate(WIRING)])
    def test_call_site_present(self, fn, anchor):
        assert anchor in _src(fn), f"{fn.__module__}.{fn.__name__} 缺守卫接线: {anchor}"

    def test_wiring_count_matches(self):
        # 非空自证：9 处写点一个都不能少（新增写点时此清单必须同步扩）
        assert len(WIRING) == 9

    def test_helper_delegates_to_primitive(self):
        # review_autofix / charts 的 helper 都只是委派，判据不许本地复制一份
        src_ra = _src(review_autofix_mod._guard_not_generating)
        src_ch = _src(charts_mod._guard_chart_target)
        for src, where in ((src_ra, "review_autofix._guard_not_generating"),
                           (src_ch, "charts._guard_chart_target")):
            assert "from app.routers.sections import ensure_no_generation_write" in src
            assert "content_generation_in_progress" not in src, (
                where + " 复制了判据，绕开单一事实源")

    def test_autofix_helper_delegation_is_functional(self, monkeypatch):
        # A/B M2 抓出的盲区：静态锁只锚 import 行与调用点实参串，
        # 摘掉 helper **体内的委派调用行**全部仍绿 —— 必须功能实证委派为活路径。
        orig = sections_mod._scheme_task_status_in_progress
        sections_mod._scheme_task_status_in_progress = (
            lambda sid, t: "running" if t == "content_generation" else None)
        try:
            with pytest.raises(HTTPException) as ei:
                review_autofix_mod._guard_not_generating(SCHEME, "自动修复落库")
            assert ei.value.status_code == 409
            assert "自动修复落库" in str(ei.value.detail)
        finally:
            sections_mod._scheme_task_status_in_progress = orig

    def test_shrink_guard_before_ai_and_query(self):
        # 守卫必须在任何 AI 调用 / 正文读取之前（省一次无意义的压缩成本）
        src = _src(sections_mod.shrink_section)
        g = src.index('ensure_no_generation_write(scheme_id, "字数压缩")')
        assert g < src.index("SELECT id, title, content")

    def test_consistency_confirm_only_blocks_when_rejected_exists(self):
        # 纯 accepted 只写冲突状态不触碰 content —— 拦截必须限定在 rejected 分支内
        src = _src(consistency_repair_mod.confirm)
        g = src.index('ensure_no_generation_write(scheme_id, "一致性修复拒绝恢复")')
        assert src.index("if rejected:") < g


# ---------------------------------------------------------------------------
# D 组 · DLV 口径唯一实现 content_coverage_stats
# ---------------------------------------------------------------------------

def _tree_sections() -> list[dict]:
    """父空子满 + 一个纯空白叶子 + 一个真空叶子的标准样本树。"""
    return [
        {"id": "p1", "parent_id": "", "content": "", "word_count": 0},
        {"id": "p1a", "parent_id": "p1", "content": "正文甲", "word_count": 10},
        {"id": "p1b", "parent_id": "p1",
         "content": "   \n\t ", "word_count": 0},          # 纯空白 = 空
        {"id": "p2", "parent_id": "", "content": "独立正文", "word_count": 5},
        {"id": "p2a", "parent_id": "p2", "content": "", "word_count": 0},  # 真空叶子
    ]


class TestContentCoverageStats:
    def test_exported_from_module(self):
        assert "content_coverage_stats" in preflight_mod.__all__

    def test_formula_numeric_lock(self):
        cov = content_coverage_stats(_tree_sections())
        assert cov == {
            "section_count": 5,
            "leaf_count": 3,          # p1a p1b p2a
            "generated_count": 3,     # p1(经子孙) p1a p2；p1b 纯空白、p2a 空 不计
            "empty_ratio": round(2 / 3 * 100, 1),
        }

    def test_empty_input_boundary(self):
        cov = content_coverage_stats([])
        assert cov["section_count"] == 0 and cov["leaf_count"] == 0
        assert cov["generated_count"] == 0
        assert cov["empty_ratio"] == 0.0  # 分母 max(...,1) 防 ZeroDivision

    def test_preflight_stats_uses_shared_impl(self):
        sections = _tree_sections()
        cov = content_coverage_stats(sections)
        st = preflight_mod.preflight_stats(
            PreflightContext(scheme_id="s", sections=sections))
        for k in ("section_count", "leaf_count", "generated_count", "empty_ratio"):
            assert st[k] == cov[k], f"总检看板 {k} 与共用口径分叉"
        src = _src(preflight_mod.preflight_stats)
        assert "_cov = content_coverage_stats(ctx.sections)" in src
        # AST 级锁：函数体内不得再出现 build_section_tree_index **调用**
        # （注释里提到该名字是历史说明，不构成第二份判据）
        tree = ast.parse(textwrap.dedent(src))
        self_calls = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)
                      and n.func.id == "build_section_tree_index"]
        assert not self_calls, (
            "preflight_stats 又自己算了一份覆盖口径 —— 判据再次分叉")


# ---------------------------------------------------------------------------
# E 组 · 导出缓存指纹双维取证锁 + charts 写点「刻意不接 invalidate」判据
# ---------------------------------------------------------------------------

def _fingerprint_prep(content: str, data_json: str) -> dict:
    return {
        "config": {},
        "fe_codes": [],
        "fe_images": {},
        "scheme": {"name": "n", "project_id": "p"},
        "sections": [{"id": "s1", "parent_id": "", "sort_order": 1,
                      "level": 1, "title": "t", "content": content}],
        "chart_fp": [("s1", "flowchart", data_json)],
        "global_facts": [],
    }


class TestFingerprintEvidence:
    """R90 遗留⑧取证结论的可执行形态：charts 写点无需接 invalidate。"""

    def test_same_input_stable(self):
        a = export_mod._content_fingerprint(_fingerprint_prep("甲", "{\"a\":1}"))
        b = export_mod._content_fingerprint(_fingerprint_prep("甲", "{\"a\":1}"))
        assert a == b

    def test_content_change_misses(self):
        base = export_mod._content_fingerprint(_fingerprint_prep("甲", "{\"a\":1}"))
        moved = export_mod._content_fingerprint(_fingerprint_prep("乙", "{\"a\":1}"))
        assert base[1] != moved[1]

    def test_data_json_change_misses(self):
        # fix-mermaid 改 data_json（前端重登记同路径）同样必须 miss
        base = export_mod._content_fingerprint(_fingerprint_prep("甲", "{\"a\":1}"))
        moved = export_mod._content_fingerprint(_fingerprint_prep("甲", "{\"a\":2}"))
        assert base[1] != moved[1]

    def test_fingerprint_covers_both_dims_in_source(self):
        src = _src(export_mod._content_fingerprint)
        assert '"sections"' in src and '"charts"' in src

    def test_charts_writes_do_not_call_invalidate(self):
        # 取证结论：charts.py 两个正文写点**刻意不接** invalidate_export_cache
        # （接了反而把全方案缓存单点清光 —— 行为放大；指纹已天然 miss）。
        # 此锁防「后人以为是漏接线又补一刀」。
        src = (APP_DIR / "routers" / "charts.py").read_bytes().decode("utf-8")
        assert "invalidate_export_cache" not in src
        assert "export_cache" not in src

    def test_invalidate_still_real_for_its_owners(self):
        # 非空自证：facts 侧既有失效链没被本轮误伤
        from app.services.facts_extractor import invalidate_export_cache
        assert callable(invalidate_export_cache)


# ---------------------------------------------------------------------------
# F 组 · export.collect_export_issues 共用口径 + 旧口径不回流
# ---------------------------------------------------------------------------

class TestExportSharedCoverage:
    def test_calls_shared_impl(self):
        src = _src(export_mod.collect_export_issues)
        assert "from app.services.preflight_engine import content_coverage_stats" in src
        assert "_cov = content_coverage_stats(sections)" in src

    def test_return_keys_from_shared_cov(self):
        src = _src(export_mod.collect_export_issues)
        assert '"generated_count": _cov["generated_count"]' in src
        assert re.search(r'empty_ratio\s*=\s*_cov\["empty_ratio"\]', src)

    def test_old_caliber_not_reintroduced(self):
        # 旧形态「if s.get(\"content\"): generated_count += 1」不得回流
        src = _src(export_mod.collect_export_issues)
        assert "generated_count += 1" not in src

    def test_empty_leaf_judge_has_strip(self):
        # empty_section 判据与共用口径对齐：纯空白 content 也算空
        src = _src(export_mod.collect_export_issues)
        assert 'if not (s.get("content") or "").strip() and not has_children' in src

    def test_status_and_wordcount_judges_untouched(self):
        # 刻意不动的两条判据（status_inconsistent 用 truthy、low_word_count 用 wc）
        # —— 它们不是覆盖口径，不许被顺手「统一」掉
        src = _src(export_mod.collect_export_issues)
        assert 'if s.get("status") == "empty" and (s.get("content") or wc > 0):' in src
        assert "if not has_children and 0 < wc < 100:" in src


# ---------------------------------------------------------------------------
# G 组 · MONITOR_CATEGORIES 子集锁 + stage_label 副本一致锁
# ---------------------------------------------------------------------------

class TestCategoryAndStageLabelLocks:
    def test_monitor_categories_is_subset_of_registry(self):
        # R90 遗留④：收敛 MONITOR_CATEGORIES 与 CATEGORY_STANDARDS 的键集是
        # 行为变更（会改变 SAF-06 门控命中面），本轮不动手 —— 但子集关系必须锁住：
        # 监测类别若出现注册表不认识的键，匹配环节会静默失灵。
        mon = preflight_mod.MONITOR_CATEGORIES
        assert mon, "MONITOR_CATEGORIES 为空 —— 判据静默失灵"
        assert set(mon) <= set(CATEGORY_STANDARDS), (
            f"越界键: {set(mon) - set(CATEGORY_STANDARDS)}")

    def test_monitor_categories_referenced_by_single_object(self):
        src = _src(preflight_mod.check_safety)
        assert "MONITOR_CATEGORIES" in src

    def test_stage_label_fallback_parity(self):
        # R90 遗留② → ✅ R168 重锚：content_progress 的休眠函数族（含
        # snapshot_stats）已删除（活路径唯一在 sse_handlers._snapshot_stats，
        # 旧「两副本 parity」前提消失）。本护栏改为两段：
        # (a) 活副本缺 stage_label 回退 "" 的口径不变（回退 stage 原值会静默
        #     改前端语义）；
        # (b) 休眠副本不得回流（死代码棘轮）—— content_progress 收敛为纯常量
        #     事实源，函数族一回来就说明有人按旧副本「恢复共用」，直接红。
        live = re.search(r'"stage_label": rec\.get\([^)]*\)',
                        _src(sse_handlers_mod._snapshot_stats)).group(0)
        assert live == '"stage_label": rec.get("stage_label", "")'
        assert not hasattr(content_progress_mod, "snapshot_stats"), (
            "content_progress.snapshot_stats 休眠副本回流 —— 活实现唯一在 "
            "sse_handlers/_sse_outline_build，恢复共用会静默回退 B19 行为")

    def test_old_stage_fallback_not_reintroduced(self):
        src = _src(sse_handlers_mod._snapshot_stats)
        assert 'rec.get("stage_label", rec.get("stage", ""))' not in src
