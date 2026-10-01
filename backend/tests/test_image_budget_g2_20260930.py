"""G2（2026-09-30）AI 配图全局预算·分段择优 — 纯函数回归护栏。

对应缺口：上游（OpenBidKit 易标《标书智能体（六）》）要求 AI 可提名很多生图候选，
但最终只按 ``maxAiImages`` 择优执行，且把候选小节「分段」、每段选优先级最高的，
避免前面章节把图片额度全部用完。

本仓落点：
  - 配置 ``app.config.Settings.max_ai_images``（默认 0 = 关闭，零迁移）
  - 纯函数 ``app.services.ai.image_engine.apply_image_budget``
  - 包装   ``app.services.ai.image_engine.select_ai_image_codes``
  - 接线   ``app.routers.export._auto_generate_ai_image_blocks``

测试策略：本文件只测纯函数（不触碰 DB / 不发起 AI 调用），保证可独立、快速、稳定回归。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.ai.image_engine import (  # noqa: E402
    apply_image_budget,
    select_ai_image_codes,
)


def _cands(n, orders=None, prios=None):
    """构造 n 个候选：默认 order=0..n-1、priority=0。"""
    orders = orders if orders is not None else list(range(n))
    prios = prios if prios is not None else [0] * n
    return [{"key": f"k{i}", "order": orders[i], "priority": prios[i]} for i in range(n)]


# ---------------------------------------------------------------------------
# 1. 向后兼容 / 边界（关闭预算时行为必须逐字不变）
# ---------------------------------------------------------------------------

def test_budget_disabled_returns_all():
    """max_images<=0 视为关闭：原样返回（等于旧行为，零迁移）。"""
    c = _cands(20)
    assert apply_image_budget(c, 0) == c
    assert apply_image_budget(c, -1) == c
    assert apply_image_budget(c, None) == c


def test_budget_empty_candidates():
    assert apply_image_budget([], 5) == []
    assert apply_image_budget([], 0) == []


def test_budget_candidates_not_exceed_limit():
    """候选数 <= 上限时不裁剪（含正好相等）。"""
    c = _cands(3)
    assert len(apply_image_budget(c, 6)) == 3
    assert len(apply_image_budget(c, 3)) == 3
    assert len(apply_image_budget(_cands(6), 6)) == 6


def test_budget_single_candidate():
    c = _cands(1)
    assert len(apply_image_budget(c, 5)) == 1
    assert len(apply_image_budget(c, 1)) == 1


# ---------------------------------------------------------------------------
# 2. 硬不变式：返回值数量严格 <= 上限
# ---------------------------------------------------------------------------

def test_budget_count_never_exceeds_limit():
    for n in (2, 7, 13, 20, 50, 80):
        for m in (1, 2, 4, 6, 9, 12):
            out = apply_image_budget(_cands(n), m)
            assert len(out) <= m, f"n={n} m={m} 返回 {len(out)}"


def test_budget_returns_subset_of_input():
    c = _cands(20)
    keys_in = {x["key"] for x in c}
    out = apply_image_budget(c, 6)
    assert {x["key"] for x in out} <= keys_in
    assert len({x["key"] for x in out}) == len(out)  # 不重复


# ---------------------------------------------------------------------------
# 3. 核心语义：分段均匀，避免前段独占（上游设计要点）
# ---------------------------------------------------------------------------

def test_budget_not_front_loaded():
    """20 候选限 6：不能 6 张全落在最前面几章。"""
    out = apply_image_budget(_cands(20), 6)
    chosen = sorted(x["order"] for x in out)
    assert len(chosen) == 6
    assert max(chosen) >= 10, f"选中位置集中在前段: {chosen}"
    assert not all(o < 6 for o in chosen)


def test_budget_segment_distribution_spans_document():
    """6 张应跨越全文多个区段（前/中/后段都有代表）。"""
    out = apply_image_budget(_cands(20), 6)
    chosen = sorted(x["order"] for x in out)
    assert any(o < 7 for o in chosen), "前段无图"
    assert any(7 <= o < 14 for o in chosen), "中段无图"
    assert any(o >= 14 for o in chosen), "后段无图"


def test_budget_even_when_limit_is_one_per_many_chapters():
    """限 1 张时仍保留 1 张（不误清空）。"""
    out = apply_image_budget(_cands(20), 1)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 4. 段内优先级 / 文档顺序
# ---------------------------------------------------------------------------

def test_budget_priority_within_same_order():
    """同位置多候选：段内按 priority 降序取。"""
    c = [
        {"key": "low", "order": 0, "priority": 1},
        {"key": "high", "order": 0, "priority": 9},
        {"key": "mid", "order": 0, "priority": 5},
    ]
    assert apply_image_budget(c, 1)[0]["key"] == "high"


def test_budget_no_prune_preserves_input_order():
    """候选数 <= 上限时不裁剪：保持输入原序（不做无谓重排，零迁移）。"""
    c = [
        {"key": "b", "order": 1, "priority": 0},
        {"key": "a", "order": 0, "priority": 0},
        {"key": "c", "order": 2, "priority": 0},
    ]
    assert [x["key"] for x in apply_image_budget(c, 3)] == ["b", "a", "c"]


def test_budget_pruning_respects_document_order():
    """需要裁剪时：乱序输入仍按 order 升序参与分段。

    输入 order: b=1, a=0, c=2；限 2 张 → 分段后取的是文档顺序上的 a(0) 与 c(2)。
    """
    c = [
        {"key": "b", "order": 1, "priority": 0},
        {"key": "a", "order": 0, "priority": 0},
        {"key": "c", "order": 2, "priority": 0},
    ]
    out = apply_image_budget(c, 2)
    assert [x["key"] for x in out] == ["a", "c"]


def test_budget_missing_priority_defaults_zero():
    """缺 priority 字段不应崩溃（默认 0）。"""
    c = [{"key": "x", "order": 0}, {"key": "y", "order": 1}]
    assert len(apply_image_budget(c, 2)) == 2


# ---------------------------------------------------------------------------
# 5. 确定性（同输入同输出，便于缓存/回归）
# ---------------------------------------------------------------------------

def test_budget_deterministic():
    c = _cands(20)
    a = [x["key"] for x in apply_image_budget(c, 6)]
    b = [x["key"] for x in apply_image_budget(c, 6)]
    assert a == b


# ---------------------------------------------------------------------------
# 6. select_ai_image_codes 包装（export 接线实际调用的是它）
# ---------------------------------------------------------------------------

def _groups(n):
    return {f"code{i}": [{"type": "ai_image", "code": f"code{i}"}] for i in range(n)}


def test_select_disabled_keeps_all():
    groups = _groups(10)
    order = {f"code{i}": i for i in range(10)}
    assert select_ai_image_codes(groups, order, 0) == set(groups.keys())


def test_select_empty_groups():
    assert select_ai_image_codes({}, {}, 6) == set()
    assert select_ai_image_codes({}, {}, 0) == set()


def test_select_prunes_to_budget():
    groups = _groups(20)
    order = {f"code{i}": i for i in range(20)}
    keep = select_ai_image_codes(groups, order, 6)
    assert len(keep) == 6
    assert keep <= set(groups.keys())


def test_select_not_front_loaded():
    groups = _groups(20)
    order = {f"code{i}": i for i in range(20)}
    keep = select_ai_image_codes(groups, order, 6)
    chosen = sorted(order[c] for c in keep)
    assert max(chosen) >= 10
    assert any(o < 7 for o in chosen)
    assert any(o >= 14 for o in chosen)


def test_select_accepts_callable_order():
    groups = _groups(20)
    out = select_ai_image_codes(groups, lambda code: int(code[4:]), 6)
    assert len(out) <= 6
    assert out <= set(groups.keys())


def test_select_missing_order_defaults_zero():
    """order 映射缺失该码 → 默认 0，不应抛异常。"""
    groups = _groups(5)
    keep = select_ai_image_codes(groups, {}, 2)
    assert len(keep) <= 2
