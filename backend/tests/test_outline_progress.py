"""目录生成进度增强 + 审核语义收紧 回归测试（2026-09-15）

覆盖本轮改动：
- sse_handlers._outline_progress
  → 阶段模型（base 累积 + span 渐近填充）单调不倒退、封顶 1.0、
    分步链路按「已完成章数 + 当前章按耗时填充」推进
- sse_handlers._snapshot_outline_stats / _outline_phase_label
  → 运行统计快照字段口径、ETA 语义、异常安全
- sse_handlers._validate_outline(max_nodes) / _outline_fix_validate_fn
  → 修复链路节点上限放宽（长方案完整目录 > 500 节点不再被整份丢弃）
- sse_handlers._sublevel_validate_fn
  → 单章子目录畸形数组拦截（原为事件流内闭包，无法单测）
- sse_handlers._review_and_fix_outline
  → passed 缺失语义（有建议即按不通过修复）、判不通过但无建议时注入兜底建议、
    phase_cb 阶段回调与异常隔离
- OUTLINE_STEPWISE_MIN_WORDS 常量（原为内联魔数 50000）
"""
import json
import time

import pytest
from app.routers import sse_handlers as sh


# ============================================================
# 阶段模型结构不变量
# ============================================================
class TestPhaseModelInvariants:
    def test_short_path_progress_never_goes_backwards(self):
        """短方案链路：prepare → draft → review → fix 的「进入值/完成值」单调不减。

        base 为累积基准，必须等于上一阶段的完成值（或略低但不超过其填充上限），
        否则阶段切换时进度条会倒退。
        """
        seq = []
        for ph in ("prepare", "draft", "review", "fix"):
            m = sh._OUTLINE_PHASE_MODEL[ph]
            seq.append(m["base"])
            seq.append(round(m["base"] + m["span"], 4))
        assert seq == sorted(seq), f"短方案阶段进度非单调: {seq}"

    def test_stepwise_path_progress_never_goes_backwards(self):
        """长方案链路：prepare → level1 → sublevels → review → fix 单调不减。"""
        seq = []
        for ph in ("prepare", "level1", "sublevels", "review", "fix"):
            m = sh._OUTLINE_PHASE_MODEL[ph]
            seq.append(m["base"])
            seq.append(round(m["base"] + m["span"], 4))
        assert seq == sorted(seq), f"长方案阶段进度非单调: {seq}"

    def test_final_phase_reaches_full_progress(self):
        """末阶段（fix）base + span 必须恰好到 1.0，否则进度条永远到不了 100%。"""
        m = sh._OUTLINE_PHASE_MODEL["fix"]
        assert round(m["base"] + m["span"], 4) == 1.0

    def test_all_phases_have_label(self):
        for ph, m in sh._OUTLINE_PHASE_MODEL.items():
            assert m["label"], f"阶段 {ph} 缺少中文标签"


# ============================================================
# _outline_progress
# ============================================================
class TestOutlineProgress:
    def _st(self, phase, *, elapsed=0.0, **kw):
        now = time.monotonic()
        st = {
            "phase": phase,
            "phase_started_at": now - elapsed,
            "chapter_started_at": now - elapsed,
            "started_at": now - elapsed,
        }
        st.update(kw)
        return st

    def test_phase_entry_equals_base(self):
        """刚进入某阶段（elapsed=0）时进度等于该阶段的 base。"""
        for ph in ("prepare", "draft", "level1", "review", "fix"):
            st = self._st(ph)
            assert sh._outline_progress(st) == pytest.approx(
                sh._OUTLINE_PHASE_MODEL[ph]["base"], abs=1e-6), ph

    def test_phase_fill_capped_at_85_percent(self):
        """阶段内渐近填充封顶 85%，给「阶段完成」留出可见跳变。"""
        for ph in ("draft", "level1", "review", "fix"):
            m = sh._OUTLINE_PHASE_MODEL[ph]
            st = self._st(ph, elapsed=m["expect"] * 10)
            expect = round(m["base"] + m["span"] * sh._STAGE_FILL_MAX, 4)
            assert sh._outline_progress(st) == pytest.approx(expect, abs=1e-4), ph
            # 且严格小于「阶段完成值」，进度条不会提前顶格
            assert sh._outline_progress(st) < m["base"] + m["span"]

    def test_fill_is_monotonic_within_phase(self):
        """阶段内进度随耗时单调不减（draft 是最耗时的阶段，必须持续前进）。"""
        vals = [sh._outline_progress(self._st("draft", elapsed=e))
                for e in (0, 10, 30, 60, 120, 300)]
        assert vals == sorted(vals), vals
        assert vals[0] < vals[-1]

    def test_sublevels_advances_by_chapter(self):
        """分步链路按「已完成章数 / 总章数」推进，与耗时无关（elapsed=0 时也成立）。"""
        m = sh._OUTLINE_PHASE_MODEL["sublevels"]
        for done in (0, 2, 5, 10):
            st = self._st("sublevels", elapsed=0.0, sub_total=10, sub_done=done)
            expect = round(m["base"] + m["span"] * (done / 10), 4)
            assert sh._outline_progress(st) == pytest.approx(expect, abs=1e-4), done

    def test_sublevels_current_chapter_fills_by_elapsed(self):
        """当前章 AI 调用期间（最长 180s）进度条仍缓慢前进，且不超过下一格。"""
        st0 = self._st("sublevels", elapsed=0.0, sub_total=10, sub_done=3)
        st1 = self._st("sublevels", elapsed=sh.OUTLINE_CHAPTER_EXPECT, sub_total=10, sub_done=3)
        st2 = self._st("sublevels", elapsed=sh.OUTLINE_CHAPTER_EXPECT * 100,
                       sub_total=10, sub_done=3)
        p0, p1, p2 = (sh._outline_progress(s) for s in (st0, st1, st2))
        assert p0 < p1
        # 封顶后不再增长，且不超过「本章完成」的刻度（done+1）
        assert p2 == pytest.approx(p1, abs=1e-6)
        m = sh._OUTLINE_PHASE_MODEL["sublevels"]
        assert p2 < m["base"] + m["span"] * (4 / 10) + 1e-6

    def test_completed_chapter_does_not_overcount(self):
        """章完成后进度必须恰为 (done/total)，不得因「当前章填充」多算近一章。

        踩坑记录：`_outline_progress` 在 done < total 时会按「当前章已耗时」叠加
        填充量。若章完成时**不重置** `chapter_started_at`，耗时长的一章完成后
        进度会比实际多做 ~0.85 章（本仓 2026-09-15 自查发现并修正）。
        """
        now = time.monotonic()
        m = sh._OUTLINE_PHASE_MODEL["sublevels"]
        # 第 3 章耗时 200s（远超 OUTLINE_CHAPTER_EXPECT=45）后完成：
        # 生产代码在 sub_done=i+1 的同一时刻把 chapter_started_at 重置为「现在」。
        st = {"phase": "sublevels", "phase_started_at": now - 200,
              "chapter_started_at": now, "started_at": now - 200,
              "sub_total": 10, "sub_done": 3}
        expect = round(m["base"] + m["span"] * (3 / 10), 4)
        assert sh._outline_progress(st) == pytest.approx(expect, abs=1e-4)

    def test_overcount_pitfall_is_real(self):
        """反向断言：不重置计时基准时会明显过冲 —— 守住这个坑，防止回归。"""
        now = time.monotonic()
        m = sh._OUTLINE_PHASE_MODEL["sublevels"]
        st = {"phase": "sublevels", "phase_started_at": now - 200,
              "chapter_started_at": now - 200, "started_at": now - 200,
              "sub_total": 10, "sub_done": 3}
        correct = m["base"] + m["span"] * (3 / 10)
        assert sh._outline_progress(st) > correct + 0.02

    def test_sublevels_zero_total_is_safe(self):
        """total=0（尚未拿到一级目录）时不得抛异常、不得返回负数。"""
        assert sh._outline_progress(self._st("sublevels", sub_total=0, sub_done=0)) >= 0.0

    def test_unknown_phase_falls_back_to_prepare(self):
        st = self._st("nonsense")
        assert sh._outline_progress(st) == pytest.approx(
            sh._OUTLINE_PHASE_MODEL["prepare"]["base"], abs=1e-6)

    def test_never_exceeds_one(self):
        st = self._st("fix", elapsed=10 ** 6, sub_total=1, sub_done=1)
        assert sh._outline_progress(st) <= 1.0

    def test_empty_state_is_safe(self):
        assert 0.0 <= sh._outline_progress({}) <= 1.0


# ============================================================
# _outline_phase_label / _snapshot_outline_stats
# ============================================================
class TestOutlinePhaseLabel:
    def test_known_and_unknown(self):
        assert sh._outline_phase_label({"phase": "review"}) == "目录审核中"
        assert sh._outline_phase_label({"phase": "nope"}) == ""
        assert sh._outline_phase_label({}) == ""


class TestSnapshotOutlineStats:
    def _st(self, phase="draft", **kw):
        now = time.monotonic()
        st = {"phase": phase, "phase_started_at": now, "chapter_started_at": now,
              "started_at": now - 12.0}
        st.update(kw)
        return st

    def test_field_shape_matches_content_stats(self):
        """字段与正文 _snapshot_stats 同构，前端可复用同一套展示逻辑。"""
        s = sh._snapshot_outline_stats(self._st())
        for key in ("elapsed_ms", "eta_ms", "done", "total", "failed", "words",
                    "concurrency", "running", "phase", "phase_label", "progress"):
            assert key in s, key
        assert s["elapsed_ms"] >= 12000
        assert s["phase"] == "draft"
        assert s["phase_label"] == "AI 生成目录"
        assert s["words"] == 0 and s["concurrency"] == 0 and s["running"] == []

    def test_failed_counts_failed_chapters(self):
        s = sh._snapshot_outline_stats(self._st(failed_chapters=["第一章", "第二章"]))
        assert s["failed"] == 2

    def test_stepwise_flag_and_nodes_exposed(self):
        s = sh._snapshot_outline_stats(self._st(stepwise=True, nodes=42))
        assert s["stepwise"] is True
        assert s["nodes"] == 42

    def test_eta_extrapolates_from_done_chapters(self):
        """分步链路：done>0 时按「已耗时 / 已完成章数」外推剩余章节。"""
        now = time.monotonic()
        st = {"phase": "sublevels", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now - 60.0, "sub_total": 10, "sub_done": 2}
        s = sh._snapshot_outline_stats(st)
        # 已耗时 60s 完成 2 章 → 剩余 8 章约 240s
        assert 200_000 <= s["eta_ms"] <= 300_000

    def test_eta_uses_phase_expect_before_first_chapter(self):
        """首章（done=0）时按当前阶段预期耗时外推本阶段剩余时间。"""
        now = time.monotonic()
        st = {"phase": "review", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 5, "sub_done": 0}
        s = sh._snapshot_outline_stats(st)
        assert s["eta_ms"] == pytest.approx(
            sh._OUTLINE_PHASE_MODEL["review"]["expect"] * 1000, rel=0.1)

    def test_eta_during_review_uses_stage_remaining(self):
        """✅ 语义变更（2026-09-16）：章节全部完成后进入审核/修复阶段，ETA 不再消失。

        旧实现要求 `total > done` 才给 ETA（且一次性直出链路 total=0 恒不满足），
        于是恰好在最长 180s 的审核+修复区间里「预计剩余」为空 —— 用户无法判断
        还要等多久。现改为：无章节样本时按当前阶段（校准后）预期耗时给阶段剩余。
        """
        now = time.monotonic()
        st = {"phase": "review", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 5, "sub_done": 5}
        assert sh._snapshot_outline_stats(st)["eta_ms"] == pytest.approx(
            sh._OUTLINE_PHASE_MODEL["review"]["expect"] * 1000, rel=0.1)

    def test_no_eta_when_stage_time_exhausted(self):
        """阶段耗时已超出预期（无处可估）→ 返回 None，不给出误导性的 0/负值。"""
        now = time.monotonic()
        st = {"phase": "review", "phase_started_at": now - 10 ** 4,
              "chapter_started_at": now, "started_at": now - 10 ** 4,
              "sub_total": 5, "sub_done": 5}
        assert sh._snapshot_outline_stats(st)["eta_ms"] is None

    def test_no_eta_after_progress_complete(self):
        """进度已到 100%（last_p=1.0）→ 无剩余，避免「100% 还要等很久」。"""
        now = time.monotonic()
        st = {"phase": "fix", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 0, "sub_done": 0, "last_p": 1.0}
        assert sh._snapshot_outline_stats(st)["eta_ms"] is None

    def test_never_raises_on_bad_input(self):
        assert sh._snapshot_outline_stats({"started_at": "bad"}) == {}


# ============================================================
# _validate_outline(max_nodes) / _outline_fix_validate_fn
# ============================================================
def _wide_outline(n_children: int, n_roots: int = 1):
    """构造节点总数 = n_roots × (1 + n_children) 的目录。"""
    return [
        {"title": f"章{r}", "description": "", "children": [
            {"title": f"节{r}-{c}", "description": "", "children": []}
            for c in range(n_children)
        ]}
        for r in range(n_roots)
    ]


class TestValidateOutlineMaxNodes:
    def test_default_limit_still_500(self):
        """生成链路默认上限保持 500（防止模型无限输出）。"""
        ol = _wide_outline(600)   # 601 节点
        assert any("500" in s for s in sh._validate_outline({"outline": ol}, strict_depth=False))

    def test_limit_is_configurable(self):
        ol = _wide_outline(600)
        assert sh._validate_outline({"outline": ol}, strict_depth=False, max_nodes=1200) == []

    def test_zero_means_unlimited(self):
        ol = _wide_outline(2000)
        assert sh._validate_outline({"outline": ol}, strict_depth=False, max_nodes=0) == []

    def test_fix_validate_fn_accepts_large_full_outline(self):
        """BUG 修复：长方案完整目录（30 章 × 21 节点 = 630）此前会被判非法，
        导致修复结果整份丢弃、"按审核建议修复" 空转。"""
        big = _wide_outline(20, n_roots=30)     # 630 节点
        assert sh._outline_validate_fn({"outline": big}) != []      # 生成链路：超限
        assert sh._outline_fix_validate_fn({"outline": big}) == []  # 修复链路：放行

    def test_fix_validate_fn_still_rejects_broken_structure(self):
        """放宽节点数不等于放弃结构校验：空目录/缺 title 仍须拦截。"""
        assert sh._outline_fix_validate_fn({"outline": []}) != []
        assert sh._outline_fix_validate_fn({"outline": [{"children": []}]}) != []

    def test_fix_validate_fn_still_caps_runaway_output(self):
        """仍保留 OUTLINE_FIX_MAX_NODES 上限，模型跑飞时不会无限接受。"""
        huge = _wide_outline(sh.OUTLINE_FIX_MAX_NODES, n_roots=2)
        assert sh._outline_fix_validate_fn({"outline": huge}) != []


# ============================================================
# _sublevel_validate_fn（原为事件流内闭包）
# ============================================================
class TestSublevelValidateFn:
    def test_accepts_normal_children(self):
        assert sh._sublevel_validate_fn({"outline": [{"title": "施工准备"}]}) == []

    def test_rejects_empty_or_non_list(self):
        assert sh._sublevel_validate_fn({"outline": []}) != []
        assert sh._sublevel_validate_fn({"outline": None}) != []
        assert sh._sublevel_validate_fn({}) != []

    def test_rejects_malformed_nodes(self):
        """["x", 1] 这类畸形数组会被 normalize 静默丢弃 → 章变空壳，
        必须在校验阶段拦下并计入 failed_chapters。"""
        assert sh._sublevel_validate_fn({"outline": ["x", 1]}) != []
        assert sh._sublevel_validate_fn({"outline": [{"title": "  "}]}) != []

    def test_reports_bad_count(self):
        issues = sh._sublevel_validate_fn({"outline": [{"title": "ok"}, "bad", {"no": 1}]})
        assert issues and "2" in issues[0]


# ============================================================
# _review_and_fix_outline（审核语义收紧 + 阶段回调）
# ============================================================
class TestReviewSemantics:
    @pytest.fixture(autouse=True)
    def _isolate_nine_chapter_check(self, monkeypatch):
        """⚠️ 2026-10-02（第二十六轮补救，承接第二十五轮门控放宽）：
        程序化覆盖预检现**恒定参与**（`outline_checkpoint_check` 默认开），
        本组用例测的是**AI 审核/修复链路语义**（passed 缺省/字符串、阶段回调、
        放宽校验器），用最小目录夹具时会被九章检查先行触发外科补齐调用。
        九章覆盖已由 tests/test_outline_checkpoint_20261002.py 单独钉住，
        此处显式关闭属**测试范围界定**，不是掩盖缺陷。"""
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)

    async def _run(self, monkeypatch, review_obj, fix_obj=None, phase_cb=None,
                   phase_cb_raises=False):
        calls = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            calls.append(validate_fn)
            if len(calls) == 1:
                return review_obj, json.dumps(review_obj, ensure_ascii=False)
            return fix_obj, json.dumps(fix_obj, ensure_ascii=False)

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)

        cb = phase_cb
        if phase_cb_raises:
            def cb(_phase):            # noqa: F811
                raise RuntimeError("回调炸了")

        outline = [{"title": "工程概况", "description": "", "children": []}]
        res = await sh._review_and_fix_outline(
            outline, "深基坑", True, "摘要", scheme_name="方案",
            project_facts="", requirements="", phase_cb=cb)
        return res, calls

    async def test_missing_passed_with_suggestions_triggers_fix(self, monkeypatch):
        """BUG 修复：旧实现 passed 缺失 → default=True → 一律视为通过，
        审核形同虚设。现在「缺失但给了建议」按不通过处理。"""
        fixed = {"outline": [{"title": "工程概况", "description": "d", "children": [
            {"title": "监测方案", "description": "d", "children": []}]}]}
        (_outline, review), calls = await self._run(
            monkeypatch, {"suggestions": ["补充监测方案章节"]}, fixed)
        assert len(calls) == 2, "应触发修复轮"
        assert review["passed"] is True
        # ✅ 语义变更（2026-09-16）：修复成功后**保留**原始审核建议（追加而非覆盖），
        #    前端「审核结论」区块要展示本轮按哪些建议改的。
        assert review["suggestions"][0] == "✅ 已根据审核意见自动修复"
        assert "补充监测方案章节" in review["suggestions"]

    async def test_missing_passed_without_suggestions_is_pass(self, monkeypatch):
        """passed 缺失且无任何建议 → 视为通过，避免无依据地空跑一轮修复。"""
        (_outline, review), calls = await self._run(monkeypatch, {"note": "看起来还行"})
        assert len(calls) == 1, "不应触发修复轮"
        assert review["suggestions"] == []

    async def test_fail_without_suggestions_injects_fallback(self, monkeypatch):
        """BUG 修复：判不通过却没给建议时，旧实现静默跳过修复、
        用户拿到未修正的目录且看不到原因。现注入兜底建议并执行修复。"""
        fixed = {"outline": [{"title": "工程概况", "children": []}]}
        (_outline, review), calls = await self._run(
            monkeypatch, {"passed": False}, fixed)
        assert len(calls) == 2, "应触发修复轮"
        # ✅ 语义变更（2026-09-16）：保留兜底建议 + 修复结论
        assert review["suggestions"][0] == "✅ 已根据审核意见自动修复"
        assert any("补齐必要章节" in s for s in review["suggestions"])

    async def test_string_false_still_triggers_fix(self, monkeypatch):
        """回归：弱模型把 passed 写成字符串 "false" 仍须判不通过。"""
        fixed = {"outline": [{"title": "工程概况", "children": []}]}
        (_outline, review), calls = await self._run(
            monkeypatch, {"passed": "false", "suggestions": "补验收"}, fixed)
        assert len(calls) == 2
        assert review["passed"] is True

    async def test_review_timeout_skips_and_passes(self, monkeypatch):
        """审核超时 → 跳过审核直接完成（不消耗修复轮）。"""
        async def fake_collect(messages, validate_fn=None, **kwargs):
            raise RuntimeError("timeout-ish")

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        _outline, review = await sh._review_and_fix_outline(
            [{"title": "工程概况", "children": []}], "深基坑", True, "摘要")
        assert review["passed"] is True
        assert isinstance(review["suggestions"], list)

    async def test_empty_outline_short_circuits(self):
        _outline, review = await sh._review_and_fix_outline([], "深基坑", False, "")
        assert review["passed"] is False
        assert review["suggestions"]

    async def test_phase_cb_reports_review_then_fix(self, monkeypatch):
        """阶段回调顺序：review（审核开始）→ fix（修复开始）。"""
        seen = []
        fixed = {"outline": [{"title": "工程概况", "children": []}]}
        await self._run(monkeypatch, {"passed": False, "suggestions": ["补验收"]},
                        fixed, phase_cb=seen.append)
        assert seen == ["review", "fix"]

    async def test_phase_cb_only_review_when_no_fix(self, monkeypatch):
        seen = []
        await self._run(monkeypatch, {"passed": True, "suggestions": []},
                        phase_cb=seen.append)
        assert seen == ["review"]

    async def test_phase_cb_exception_does_not_break_review(self, monkeypatch):
        """回调异常必须被隔离，绝不能把整次目录生成打成失败。"""
        fixed = {"outline": [{"title": "工程概况", "children": []}]}
        (_outline, review), calls = await self._run(
            monkeypatch, {"passed": False, "suggestions": ["补验收"]},
            fixed, phase_cb_raises=True)
        assert len(calls) == 2
        assert review["passed"] is True

    async def test_fix_uses_relaxed_validator(self, monkeypatch):
        """修复轮必须使用放宽节点上限的校验函数（长方案完整目录 > 500 节点）。"""
        big_fix = {"outline": _wide_outline(20, n_roots=30)}   # 630 节点
        (outline, review), calls = await self._run(
            monkeypatch, {"passed": False, "suggestions": ["补齐"]}, big_fix)
        assert calls[1] is sh._outline_fix_validate_fn
        assert review["passed"] is True
        assert len(outline) == 30, "修复结果应被接受并落库（旧实现会被判非法丢弃）"


# ============================================================
# 常量
# ============================================================
def test_stepwise_threshold_constant():
    assert sh.OUTLINE_STEPWISE_MIN_WORDS == 50000


def test_review_skeleton_budget_constant_used():
    assert sh.OUTLINE_REVIEW_MAX_NODES == 150
    big = _wide_outline(400)
    assert len(sh._outline_skeleton(big)) <= 150


def test_review_skeleton_balanced_sampling():
    """超预算时按一级章均衡取样：每个一级章都应进入审核视野（遗留修复）。

    旧实现前序优先截断，30 章 × 20 节点的超长目录只有前 ~7 章会被审核，
    尾部章节的重复/遗漏/错位对审核完全不可见。
    """
    chapters = [
        {"title": f"章{r}", "description": "", "children": [
            {"title": f"章{r}-节{c}", "description": "", "children": []}
            for c in range(20)
        ]}
        for r in range(30)
    ]  # 30 × 21 = 630 节点 > 150
    sk = sh._outline_skeleton(chapters)
    assert len(sk) == 30, "所有一级章标题都应保留"
    assert [c["title"] for c in sk] == [f"章{i}" for i in range(30)]
    for i, ch in enumerate(sk):
        assert ch.get("children"), f"章{i} 至少应有 1 个代表子节点被审核"
    assert sh._count_nodes(sk) <= 150, "均衡取样后总节点数仍不得超过预算"


def test_review_skeleton_balanced_reallocates_unused_budget():
    """无子树的前序章应把预算归还总池，让后面有内容的章多分节点。"""
    chapters = (
        [{"title": f"空章{i}", "description": "", "children": []} for i in range(20)]
        + [{"title": f"实章{i}", "description": "", "children": [
            {"title": f"实章{i}-节{c}", "description": "", "children": []}
            for c in range(30)
        ]} for i in range(20)]
    )  # 20 + 20 × 31 = 640 节点 > 150
    sk = sh._outline_skeleton(chapters)
    assert len(sk) == 40, "所有一级章标题都应保留"
    real = [c for c in sk if c["title"].startswith("实章")]
    assert all(c.get("children") for c in real), \
        "前序空章归还的预算应让尾部实章都有子节点被审核"
    assert sh._count_nodes(sk) <= 150


def test_review_skeleton_small_outline_untouched():
    """未超预算的目录应完整保留（走原全量路径）。"""
    chapters = [
        {"title": "章1", "description": "说明", "children": [
            {"title": "1.1", "description": "", "children": []},
            {"title": "1.2", "description": "", "children": []},
        ]},
        {"title": "章2", "description": "", "children": []},
    ]
    sk = sh._outline_skeleton(chapters)
    assert sh._count_nodes(sk) == sh._count_nodes(chapters) == 4
    assert sk[0]["description"] == "说明"
