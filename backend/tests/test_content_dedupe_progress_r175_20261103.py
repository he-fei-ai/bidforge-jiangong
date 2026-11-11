# -*- coding: utf-8 -*-
"""R175 修复回归：续写去重窗口/误删、进度单调护栏、章节计数口径。

对应 `docs/正文生成模块审计_R175_20261103.md` 的 BUG-1 ~ BUG-4：

BUG-1 窗口三处错配（tail 3000 / 上限 4000 / 调用点 3000）→ 统一为
      `sse_utils._DEDUP_TAIL_WINDOW`（8000）。本文件锁定：
        · 纯整段重抄（3001/3500/4000/8001）仍整体丢弃（不回退 R173 修复）；
        · `prev[-3500:] + 新内容` 不再残留 500 字；
        · 正常接续不误删。
BUG-2 规则②（`b[:n] in tail` 无位置约束）→ 删除，只保留 ⓪ 整段闸 + ①
      接缝对齐；本文件锁定「中段复述的标准条款保留」「接缝重复仍删」。
BUG-3 心跳 raw progress 可能回退 → `_snapshot_stats.progress` 走
      `_monotonic_progress`，不得低于历史 `progress_max`。
BUG-4 运行期 done 含失败、终态 done 只含成功 → 统一为「done=成功，
      processed=已处理，failed=失败」；本文件锁定运行期与终态一致。
"""

import time  # noqa: I001 — 下方 import 顺序即运行时契约，禁止重排

import pytest

from app.routers.sse_handlers import (
    _DEDUP_TAIL_WINDOW,
    _dedup_continuation,
    _snapshot_stats,
    build_content_checkpoint,
)

# sse_handlers 先导入完成 → _sse_outline_build 的循环转发已初始化，
# 此处的直接导入不会触发「partially initialized module」。
from app.routers._sse_outline_build import _done_counters


# ============================================================================
# BUG-1 · 窗口单一事实源
# ============================================================================


def test_tail_window_constant_single_source():
    """窗口常量唯一，且 docstring 与实际值一致（不得再出现 3000/4000 分叉）。"""
    assert _DEDUP_TAIL_WINDOW == 8000
    assert "8000" in _dedup_continuation.__doc__
    assert (
        "4000" not in _dedup_continuation.__doc__.split("R175 修复", 1)[0]
    ), "修复前的 docstring 不得继续声称 4000 —— 文档与实值必须一致"


@pytest.mark.parametrize("n", [3001, 3500, 4000, 8001])
def test_pure_recopy_still_dropped(n):
    """纯整段重抄（≥min_overlap）整体丢弃 → 空串（保留 R173 历史修复）。

    n=8001 覆盖「超出 tail 窗口 1 字」的边界：由 ⓪ 整段闸（12000 窗口）兜住，
    不得因窗口收敛而复活重复。
    """
    prev = "乙" * 300 + "甲" * n
    out = _dedup_continuation(prev, "甲" * n)
    assert out == ""


@pytest.mark.parametrize("n", [3001, 3500, 4000])
def test_recopy_plus_new_content_no_residue(n):
    """`prev[-n:] + 新内容`：重复部分必须删净（旧实现 3500 字残留 500 字）。"""
    prev = "乙" * 300 + "甲" * n
    cont = prev[-n:] + "新增内容如下："
    out = _dedup_continuation(prev, cont)
    assert out == "新增内容如下："


def test_cross_paragraph_recopy_at_seam_stripped():
    """跨段重抄（重复区恰好是前文结尾的连续多段）→ 整体剥离。"""
    p1 = "第一段：基坑开挖应分层分段进行，每层开挖深度不宜大于 2m，严禁超挖。"
    p2 = "第二段：开挖至基底后应及时验槽，验槽合格后方可进行垫层施工。"
    prev = "甲" * 200 + "\n" + p1 + "\n" + p2
    cont = p1 + "\n" + p2 + "\n" + "第三段：雨季施工应设置排水沟与集水井。"
    out = _dedup_continuation(prev, cont)
    assert p1 not in out and p2 not in out
    assert out.startswith("第三段")


def test_normal_continuation_not_touched():
    """正常接续（无重叠）原样返回，不得误删。"""
    prev = "混凝土浇筑完成后应及时覆盖保湿养护，养护时间不少于 7 天。"
    cont = "养护期间应监测表面温度，防止早期裂缝。"
    assert _dedup_continuation(prev, cont) == cont


# ============================================================================
# BUG-2 · 规则②删除：仅接缝重复才剥
# ============================================================================


def test_mid_tail_clause_repeat_kept():
    """审计实测场景：48 字标准条款在 tail **中部**复述 → 必须保留。

    旧规则②（任意位置命中）会整段吞掉，续写开头凭空少一句话。
    """
    clause = "安全技术交底应由项目技术负责人组织，参加人员须签字确认，" "交底记录随施工资料一并归档保存。"
    assert len(clause) >= 40
    prev = clause + "\n" + "乙" * 200  # clause 在前文开头，不在接缝
    cont = clause + "\n新增：交底内容变更时应重新交底。"
    out = _dedup_continuation(prev, cont)
    assert clause in out
    assert out.startswith(clause)


def test_seam_repeat_still_stripped():
    """真重复（位于接缝）仍按最长重叠剥离 —— 规则②删除不影响主路径。"""
    shared = "模板支撑体系搭设完成后应组织验收，验收内容包括立杆垂直度、" "水平杆步距，验收记录应签字归档"
    assert len(shared) >= 40
    prev = "甲" * 100 + shared
    cont = shared + "随后进行混凝土浇筑。"
    assert _dedup_continuation(prev, cont) == "随后进行混凝土浇筑。"


def test_short_sentence_below_min_overlap_kept():
    """短于 min_overlap 的句子不进任何剥离分支（防误删正常衔接语）。"""
    short = "施工准备阶段"
    prev = "甲" * 100 + short
    assert _dedup_continuation(prev, short) == short


def test_min_overlap_boundary_seam_stripped():
    """重叠恰好等于 min_overlap(40) 的接缝重复仍剥离（边界下界）。"""
    shared = "甲" * 40
    prev = "乙" * 100 + shared
    assert _dedup_continuation(prev, shared + "新增") == "新增"


# ============================================================================
# BUG-4 · 章节计数单一口径
# ============================================================================


def test_done_counters_excludes_failed():
    assert _done_counters(10, 2) == {"done": 8, "processed": 10, "failed": 2}


def test_done_counters_clamps_and_sanitizes():
    """失败数不得超过已处理数；脏输入一律归零，不得抛异常。"""
    assert _done_counters(3, 5) == {"done": 0, "processed": 3, "failed": 3}
    assert _done_counters("bad", None) == {"done": 0, "processed": 0, "failed": 0}


def _terminal_payload(done: int, failed: int, total: int) -> dict:
    return build_content_checkpoint(
        "completed",
        "生成完成",
        ckpt={"done": done, "total": total},
        task_id="t-r175",
        total=total,
        done_ids={f"d{i}" for i in range(done)},
        failed_ids={f"f{i}" for i in range(failed)},
        failed_reasons={},
        prog={"words": 0},
        run_stats={},
        word_tol=None,
        fact_autofix_summary={},
        dispatched_order=[],
        expected_order=[],
        fact_audit_by_leaf={},
        order_warned={"flag": False},
    )


def test_runtime_and_terminal_counts_consistent():
    """含失败章节：运行期 stats 与终态 checkpoint 的 done/processed/failed 一致。

    ✅ R215 重锚（意图保留、强度不减）：R194 已确立终态计数 = **done_ids 口径**
    （failed_count = total - done，未处理章计入「未成功」；在线 stopped/error
    通道此前已用该口径），R215 把 checkpoint 通道拉齐（原 len(failed_ids) 是
    分叉时代的旧形态）。两个口径的语义差 = 未处理章数：
      · terminal["failed_count"] = total - done → 面向用户「几章没成功」；
      · runtime["failed"]（_count_failed 累加）= 实际失败数 → 面向明细。
    本场景 total=6、done=3、实际失败=2、未处理=1 ⇒ 终态 3、实际 2。
    另补「无未处理章时两口径重合」的正例，保住原「一致」意图的适用域。
    """
    processed, failed, total = 5, 2, 6
    runtime = _done_counters(processed, failed)
    terminal = _terminal_payload(done=runtime["done"], failed=failed, total=total)
    assert terminal["done"] == runtime["done"] == 3
    assert terminal["processed"] == runtime["processed"] == 5
    # ✅ R215：done_ids 口径 —— total(6) - done(3) = 3（含 1 个未处理章）
    assert terminal["failed_count"] == 3
    # 实际失败数（明细口径）：failed_sections 明细按 failed_reasons 下发（本 helper 传空）
    assert runtime["failed"] == 2
    assert terminal["failed_sections"] == []
    assert terminal["total"] == total

    # 正例：全部章节都已处理（无未处理章）时，两口径重合（原意图适用域）
    terminal2 = _terminal_payload(done=4, failed=2, total=6)
    assert terminal2["failed_count"] == 2


def test_terminal_payload_processed_is_additive():
    """向后兼容：done/total/failed_count 原义不变，processed 为新增字段。"""
    payload = _terminal_payload(done=3, failed=2, total=5)
    assert payload["done"] == 3
    assert payload["total"] == 5
    assert payload["failed_count"] == 2
    assert payload["processed"] == 5


# ============================================================================
# BUG-3 · 心跳快照进度单调
# ============================================================================


def _prog(**over):
    base = {
        "started_at": time.monotonic() - 100.0,
        "total": 10,
        "done": 5,
        "failed": 2,
        "running": {},
        "phase": "sections",
        "phase_label": "生成正文",
        "progress_max": 0.0,
    }
    base.update(over)
    return base


def test_snapshot_progress_never_below_history_max():
    """心跳快照的 progress 取 max(历史最大值, 加权值) —— 不得回退。"""
    s = _snapshot_stats(_prog(progress_max=0.9))
    assert s["progress"] == 0.9
    assert s["done"] == 3 and s["processed"] == 5 and s["failed"] == 2


def test_snapshot_progress_follows_weighted_without_history():
    """无历史最大值时按加权进度取值（不得被护栏压成常量）。"""
    s = _snapshot_stats(_prog(done=5, failed=0, progress_max=0.0))
    assert s["progress"] == pytest.approx(0.95 * 5 / 10, abs=1e-3)


def test_snapshot_progress_monotonic_across_ping_sequence():
    """模拟连续心跳：已上报 0.8 后，即便加权值算回 0.5 也不得回退。"""
    prog = _prog(done=2, failed=0, progress_max=0.8)
    s = _snapshot_stats(prog)  # 加权 = 0.95 * 2/10 = 0.19
    assert s["progress"] == 0.8
