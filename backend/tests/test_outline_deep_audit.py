"""目录生成模块 · 深度审查回归测试（2026-09-16 第二轮）

本轮修复项与对应测试：

| 编号 | 修复 | 测试类 |
|---|---|---|
| D-1 | 一次性直出链路（短方案）`eta_ms` 恒为 None（`total=0` 使分支不可达） | TestOutlineEta |
| D-2 | 分步链路 ETA 分母含准备/一级目录阶段 → 系统性放大 | TestOutlineEta |
| D-3 | 审核/修复阶段（章节已完成）ETA 消失 | TestOutlineEta |
| D-4 | 一级目录校验过弱：非对象节点放行 → 生成循环 AttributeError | TestLevel1Validator |
| D-5 | 分步链路 progress 事件可回退（force=0.3 后第一格 ≈0.295） | TestMonotonicPush |
| D-6 | 阶段/单章预期耗时无在线校准（弱模型下填充顶格后长时间静止） | TestExpectCalibration |
| D-7 | 修复提示词塞入完整目录 JSON（>10 万字符）→ 修复轮空转 | TestCompactOutlineJson |
| D-8 | 修复结果节点数骤降仍整份替换原目录（弱模型回"示例结构"） | TestFixDegradationGuard |
| D-9 | 前序章节上下文按字符硬截断（半截标题）且保留最早内容 | TestJoinTailBudget |
| D-10 | 客户端断开（GeneratorExit）不写 checkpoint → 部分成果确定性丢失 | TestPartialCheckpoint |
"""
import json
import time

import pytest

from app.routers import sse_handlers as sh


def _now() -> float:
    return time.monotonic()


# ============================================================
# D-1/D-2/D-3：ETA 口径
# ============================================================
class TestOutlineEta:
    def test_short_path_has_stage_eta(self):
        """✅ D-1：一次性直出链路（total=0）也必须给出阶段剩余时间。

        旧实现分支 `if total > done` 在该链路恒为假（0 > 0），
        「按阶段预期耗时外推」整段不可达 → 开局永远没有「预计剩余」。
        """
        now = _now()
        st = {"phase": "draft", "started_at": now - 30, "phase_started_at": now - 30,
              "chapter_started_at": now - 30, "sub_total": 0, "sub_done": 0}
        eta = sh._snapshot_outline_stats(st)["eta_ms"]
        assert eta is not None, "短方案链路必须给出阶段剩余时间"
        # 已耗时 30s、预期 55s → 剩余约 25s
        assert eta == pytest.approx(25_000, rel=0.1)

    def test_stepwise_eta_excludes_prepare_and_level1(self):
        """✅ D-2：分步链路 ETA 只用「子目录阶段」耗时做分母。

        场景：任务总耗时 600s（准备+一级目录占 400s），子目录阶段 200s 完成 2 章，
        共 10 章。旧口径 600/2×8 = 2400s；新口径 200/2×8 = 800s。
        """
        now = _now()
        st = {"phase": "sublevels", "started_at": now - 600, "phase_started_at": now - 200,
              "chapter_started_at": now, "sub_started_at": now - 200,
              "sub_total": 10, "sub_done": 2}
        eta = sh._snapshot_outline_stats(st)["eta_ms"]
        assert eta == pytest.approx(800_000, rel=0.05)

    def test_stepwise_eta_counts_current_chapter_partial(self):
        """进行中章节的已投入时间应减小 ETA（与进度条同源）。"""
        now = _now()
        base = {"phase": "sublevels", "started_at": now - 100, "phase_started_at": now - 100,
                "sub_started_at": now - 100, "sub_total": 10, "sub_done": 2}
        idle = sh._snapshot_outline_stats({**base, "chapter_started_at": now})["eta_ms"]
        working = sh._snapshot_outline_stats(
            {**base, "chapter_started_at": now - sh.OUTLINE_CHAPTER_EXPECT})["eta_ms"]
        assert working < idle

    def test_eta_present_during_review_phase(self):
        """✅ D-3：审核/修复阶段（章节已完成）仍有 ETA，不再返回 None。"""
        now = _now()
        st = {"phase": "review", "started_at": now, "phase_started_at": now,
              "chapter_started_at": now, "sub_total": 5, "sub_done": 5}
        assert sh._snapshot_outline_stats(st)["eta_ms"] is not None

    def test_eta_none_when_progress_complete(self):
        now = _now()
        st = {"phase": "fix", "started_at": now, "phase_started_at": now,
              "chapter_started_at": now, "sub_total": 0, "sub_done": 0, "last_p": 1.0}
        assert sh._snapshot_outline_stats(st)["eta_ms"] is None

    def test_eta_never_raises_on_dirty_input(self):
        dirty = {"phase": "sublevels", "started_at": "bad", "phase_started_at": None,
                 "chapter_started_at": "x", "sub_started_at": "y",
                 "sub_total": "7", "sub_done": "3"}
        assert isinstance(sh._snapshot_outline_stats(dirty), dict)


# ============================================================
# D-4：一级目录校验
# ============================================================
class TestLevel1Validator:
    def test_accepts_dict_nodes_without_title(self):
        """缺 title 是合法输入（生成循环会兜底为「第 N 章」）。"""
        assert sh._level1_validate_fn({"outline": [{"title": "工程概况"}, {"children": []}]}) == []

    def test_rejects_empty_and_non_list(self):
        assert sh._level1_validate_fn({"outline": []}) == ["outline 必须为非空数组"]
        assert sh._level1_validate_fn({"outline": "工程概况"}) == ["outline 必须为非空数组"]
        assert sh._level1_validate_fn({}) == ["outline 必须为非空数组"]

    def test_rejects_non_object_nodes(self):
        """✅ D-4 回归：["工程概况", 1] 旧实现会放行，随后 ch.get() 抛 AttributeError。"""
        issues = sh._level1_validate_fn({"outline": ["工程概况", 1, None]})
        assert issues and "非法节点" in issues[0]

    def test_can_drive_repair_round(self):
        """校验器必须能驱动 collect_json_response 的「修复轮」判定（返回 issues 列表）。"""
        assert sh._level1_validate_fn({"outline": [{"title": "A"}]}) == []
        assert sh._level1_validate_fn({"outline": [{"title": "A"}, "B"]}) != []


# ============================================================
# D-5：进度不可回退
# ============================================================
class TestMonotonicPush:
    def test_does_not_regress_after_forced_3(self):
        """✅ D-5：sublevels 的 force=0.3 之后，第 1 章完成时模型折算值 ≈0.295，
        旧实现把该值直接写进 progress 事件 → 前端进度条从 30% 倒退到 29%。"""
        now = _now()
        st = {"phase": "sublevels", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 10, "sub_done": 1, "last_p": 0.3}
        assert sh._outline_push_value(st) == 0.3
        assert st["last_p"] == 0.3

    def test_takes_larger_value_and_updates_last_p(self):
        now = _now()
        st = {"phase": "sublevels", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 10, "sub_done": 9, "last_p": 0.3}
        p = sh._outline_push_value(st)
        assert p > 0.3
        assert st["last_p"] == p

    def test_never_exceeds_one(self):
        now = _now()
        st = {"phase": "fix", "phase_started_at": now, "chapter_started_at": now,
              "started_at": now, "sub_total": 1, "sub_done": 1, "last_p": 0.99}
        assert sh._outline_push_value(st) <= 1.0


# ============================================================
# D-6：阶段/单章耗时在线校准
# ============================================================
class TestExpectCalibration:
    def test_expect_falls_back_to_factory_value(self):
        assert sh._outline_expect({}, "draft") == sh._OUTLINE_PHASE_MODEL["draft"]["expect"]
        assert sh._outline_chapter_expect({}) == sh.OUTLINE_CHAPTER_EXPECT

    def test_expect_ignores_dirty_values(self):
        st = {"stage_expect": {"draft": "abc"}, "chapter_expect": -3}
        assert sh._outline_expect(st, "draft") == sh._OUTLINE_PHASE_MODEL["draft"]["expect"]
        assert sh._outline_chapter_expect(st) == sh.OUTLINE_CHAPTER_EXPECT

    def test_ema_moves_toward_measured_and_clamps(self):
        st = {}
        sh._calibrate_outline_expect(st, "draft", 300.0)
        v = st["stage_expect"]["draft"]
        assert 55.0 < v < 300.0                      # 朝实测值移动
        assert v == pytest.approx(0.7 * 55.0 + 0.3 * 300.0, rel=1e-6)
        sh._calibrate_outline_expect(st, "draft", 10 ** 6)   # 极端样本被钳制
        assert st["stage_expect"]["draft"] <= sh._OUTLINE_EXPECT_MAX

    def test_tiny_sample_is_ignored(self):
        """空转阶段（<下限）不得污染 EMA —— 否则 expect 被拽到下限。"""
        st = {}
        sh._calibrate_outline_expect(st, "draft", sh._OUTLINE_CALIBRATE_MIN_SAMPLE - 0.5)
        assert not st.get("stage_expect")
        sh._calibrate_outline_chapter_expect(st, 0.2)
        assert not st.get("chapter_expect")

    def test_chapter_expect_calibrated_from_measured(self):
        st = {}
        sh._calibrate_outline_chapter_expect(st, 90.0)
        assert st["chapter_expect"] == pytest.approx(0.7 * 45.0 + 0.3 * 90.0, rel=1e-6)

    def test_advance_phase_calibrates_previous_and_keeps_same_phase_clock(self):
        now = _now()
        st = {"phase": "level1", "phase_started_at": now - 120, "chapter_started_at": now - 120}
        sh._advance_outline_phase(st, "sublevels")
        assert st["phase"] == "sublevels"
        assert st["stage_expect"]["level1"] == pytest.approx(0.7 * 40.0 + 0.3 * 120.0, rel=1e-3)
        # 同阶段重复刷新：保留 phase_started_at（否则耗时样本被截断成 0.x 秒被丢弃）
        started = st["phase_started_at"]
        sh._advance_outline_phase(st, "sublevels")
        assert st["phase_started_at"] == started

    def test_progress_still_monotonic_across_calibrated_transition(self):
        """校准改变了 expect，但阶段内取值恒定 ⇒ 跨阶段进度仍单调不减。"""
        now = _now()
        st = {"phase": "level1", "phase_started_at": now - 20, "chapter_started_at": now,
              "started_at": now - 20, "sub_total": 0, "sub_done": 0, "last_p": 0.0}
        vals = [sh._outline_push_value(st)]
        for elapsed in (1.0, 5.0, 30.0):          # level1 阶段内推进
            st["phase_started_at"] = now - elapsed
            vals.append(sh._outline_push_value(st))
        sh._advance_outline_phase(st, "sublevels")   # 生产写法：force=0.3 的切换
        st["sub_total"] = 4
        for done in (1, 2, 3, 4):
            st["sub_done"] = done
            st["chapter_started_at"] = now
            vals.append(sh._outline_push_value(st))
        assert vals == sorted(vals), vals


# ============================================================
# D-7：修复提示词紧凑化
# ============================================================
class TestCompactOutlineJson:
    @staticmethod
    def _outline(n=10):
        return [{
            "id": str(i), "level": 1, "title": f"第{i}章", "description": "说明" * 60,
            "children": [{"id": f"{i}.1", "level": 2, "title": "小节",
                          "description": "d", "children": []}],
        } for i in range(1, n + 1)]

    def test_strips_noise_and_stays_valid_json(self):
        node = json.loads(sh._compact_outline_json(self._outline()))["outline"][0]
        assert "id" not in node and "level" not in node
        assert node["title"] == "第1章"
        assert "children" in node
        assert "children" not in node["children"][0]   # 空 children 不输出
        assert len(node["description"]) == 80          # 描述被截断

    def test_much_smaller_than_raw_dumps(self):
        """✅ D-7：压缩只删「对模型无用的字段」，实测约为原 JSON 的 0.63~0.66。

        旧实现把完整目录 JSON 塞进修复提示词，长方案 1200 节点可达十万字符级。
        """
        raw = self._outline(30)          # 长描述场景（截断收益最大）
        assert len(sh._compact_outline_json(raw)) < len(json.dumps(raw, ensure_ascii=False)) * 0.7
        short = [{"id": str(i), "level": 1, "title": f"第{i}章", "description": "说明" * 15,
                  "children": [{"id": f"{i}.1", "level": 2, "title": "小节标题",
                                "description": "说明若干字", "children": []}]}
                 for i in range(1, 31)]
        ratio = (len(sh._compact_outline_json(short))
                 / len(json.dumps(short, ensure_ascii=False)))
        assert ratio < 0.75, f"压缩比 {ratio:.3f} 高于预期"

    def test_dirty_nodes_do_not_raise(self):
        out = sh._compact_outline_json([{"title": "A"}, "x", None, 3])
        assert json.loads(out)["outline"] == [{"title": "A"}]
        assert sh._compact_outline_json("not-a-list") == '{"outline": []}'


# ============================================================
# D-8：修复结果退化护栏
# ============================================================
class TestFixDegradationGuard:
    @staticmethod
    def _flat(n):
        return [{"title": f"第{i}章", "children": []} for i in range(n)]

    def test_shrunk_result_is_degraded(self):
        assert sh._outline_fix_looks_degraded(self._flat(10), self._flat(3)) is True

    def test_near_full_result_is_accepted(self):
        assert sh._outline_fix_looks_degraded(self._flat(10), self._flat(9)) is False
        assert sh._outline_fix_looks_degraded(self._flat(10), self._flat(11)) is False

    def test_boundary_tolerated(self):
        # 恰好 80% 视为可接受（允许合并少量节点）
        assert sh._outline_fix_looks_degraded(self._flat(10), self._flat(8)) is False
        assert sh._outline_fix_looks_degraded(self._flat(10), self._flat(7)) is True

    def test_empty_original_is_never_degraded(self):
        assert sh._outline_fix_looks_degraded([], self._flat(3)) is False
        assert sh._outline_fix_looks_degraded(None, self._flat(3)) is False


# ============================================================
# D-9：前序章节上下文预算
# ============================================================
class TestJoinTailBudget:
    def test_empty(self):
        assert sh._join_tail_budget([], 100) == ""
        assert sh._join_tail_budget(["", "  "], 100) == ""

    def test_keeps_recent_items_within_budget(self):
        items = [f"第{i}章 / 小节标题{i}" for i in range(1, 21)]
        out = sh._join_tail_budget(items, 60)
        assert len(out) <= 60
        assert "第20章" in out                 # 最近的章节被保留
        assert "第1章 / 小节标题1" not in out

    def test_never_cuts_a_title_in_half(self):
        items = ["甲" * 10, "乙" * 10, "丙" * 10]
        out = sh._join_tail_budget(items, 25)
        # 25 预算最多容纳两条完整标题（10+2+10=22）
        assert out == "乙" * 10 + "; " + "丙" * 10

    def test_no_budget_keeps_all(self):
        assert sh._join_tail_budget(["a", "b"], 0) == "a; b"


# ============================================================
# D-10：断线部分成果 checkpoint
# ============================================================
class TestPartialCheckpoint:
    """✅ D-10：客户端断开（刷新页面/关标签/网络抖动）时，finally 兜底必须把
    已生成的部分目录写进 checkpoint —— 否则前端 pollTaskUntilTerminal 拿到
    stopped 却取不到 outline_result，跑了数分钟的成果确定性丢失。"""

    @pytest.fixture
    async def ctx(self, tmp_path):
        import app.db as _appdb
        _appdb.DB_PATH = tmp_path / "audit.sqlite"
        await _appdb.init_db()
        yield _appdb
        await _appdb.close_db()

    async def test_partial_outline_is_recoverable(self, ctx):
        from app.services.ai.task_registry import register_task, finish_task
        tid = await register_task("outline_generation", "p", "s")
        outline = [{"title": "工程概况",
                    "children": [{"title": "现场条件", "children": []}]}]
        assert await sh._checkpoint_partial_outline(tid, outline, ["第2章"]) is True
        ckpt = await sh._load_outline_checkpoint(tid)
        assert ckpt is not None and ckpt["kind"] == "outline_result"
        assert ckpt["outline"] == outline
        assert ckpt["failed_chapters"] == ["第2章"]
        assert ckpt["failed_count"] == 1
        await finish_task(tid, "stopped", "客户端断开")

    async def test_empty_results_write_nothing(self, ctx):
        from app.services.ai.task_registry import register_task, finish_task
        tid = await register_task("outline_generation", "p", "s")
        assert await sh._checkpoint_partial_outline(tid, [], []) is False
        assert await sh._load_outline_checkpoint(tid) is None
        await finish_task(tid, "stopped", "客户端断开")

    async def test_failed_chapters_alone_are_still_saved(self, ctx):
        """只有失败清单、没有目录时也要落库（前端可提示"哪几章失败"）。"""
        from app.services.ai.task_registry import register_task, finish_task
        tid = await register_task("outline_generation", "p", "s")
        assert await sh._checkpoint_partial_outline(tid, [], ["第1章"]) is True
        ckpt = await sh._load_outline_checkpoint(tid)
        assert ckpt["failed_count"] == 1
        await finish_task(tid, "stopped", "客户端断开")
