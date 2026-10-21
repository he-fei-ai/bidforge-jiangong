"""R122-G3：成稿后跨章节字数批量调度 —— 纯函数计划层（引入自易标思路）。

对齐易标（OpenBidKit）contentGenerationTask.cjs:28-45 的
``TOTAL_WORD_ADJUSTMENT_BATCH_SIZE=10`` / ``MAX_WORD_ADJUSTMENT_ROUNDS=3``：
成稿后收集 under/over 章节，按目录顺序每 10 章一批做全局调度，
**批间串行、批内并发**，单节扩/缩轮次仍由其既有执行链封顶。

本模块**只含纯函数**（零 DB / 零 AI 调用 / 零副作用）：
- ``classify_word_direction``：单章偏方向判定（与执行侧判据逐字同源）；
- ``build_adjust_plan``：按目录序（输入序）切批。

执行侧不新造 AI 交互形态：under 复用语义等同 ``_continue_if_needed``
（R66 续写触发线 ``max(0.8×预算, min(300, 预算))``、≤CONTENT_CONTINUE_MAX_ROUNDS 轮），
over 复用 ``content_shrink.shrink_content_rounds``（≤SHRINK_MAX_ROUNDS 轮、
保护区间拒绝、失败保留原正文）。接入点 ``sse_handlers`` 正文收尾
（一致性管道之后、总字数统计之前），由配置开关
``word_adjust_batch_enabled`` 门控 —— **默认关闭时调用方整段不执行，
本模块甚至不会被 import**。

引入原则（用户铁律）：复用思路不照搬代码、保持架构兼容、新增配置默认关闭。
"""

from __future__ import annotations

from app.services.ai.prompts._limits import WORD_OVER_RATIO
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    LEAF_MIN_WORD_BUDGET,
    WORD_UNDER_RATIO,
)

# ---------- 调度常量（单点定义，护栏测试锁口径） ----------
# 易标 TOTAL_WORD_ADJUSTMENT_BATCH_SIZE=10（批大小对齐，非照搬实现）
WORD_ADJUST_BATCH_SIZE = 10
# 易标 MAX_WORD_ADJUSTMENT_ROUNDS=3：单节扩/缩轮次封顶语义。本仓执行链
# 已有各自封顶（CONTENT_CONTINUE_MAX_ROUNDS / SHRINK_MAX_ROUNDS），此常量
# 仅作计划层文档口径与护栏断言锚点，执行侧不重复计数。
WORD_ADJUST_MAX_ROUNDS_PER_SECTION = 3
# 批内并发度：与修复域 REPAIR_CONCURRENCY=3 同族信号量档位（保守并发，
# 避免成稿收尾把 provider 限流打穿）。
WORD_ADJUST_CONCURRENCY = 3

# 偏方向字面值（与 sections.word_status 同词表，前端零新枚举）
DIRECTION_UNDER = "under"
DIRECTION_OVER = "over"
DIRECTION_OK = "ok"


def continue_trigger_line(word_budget: int) -> float:
    """under 判据 = R66 续写触发线，与 ``_continue_if_needed`` 调用
    ``should_continue_round(min_floor_chars=min(LEAF_MIN_WORD_BUDGET, wb))``
    后的实际触发条件逐字同源：``max(wb × WORD_UNDER_RATIO, min(300, wb))``。

    预算非正时按 ``DEFAULT_WORD_BUDGET`` 兜底（与 ``word_status_for`` 同口径）。
    """
    try:
        wb = int(word_budget or 0)
    except (TypeError, ValueError):
        wb = 0
    if wb <= 0:
        wb = DEFAULT_WORD_BUDGET
    return max(wb * WORD_UNDER_RATIO, min(LEAF_MIN_WORD_BUDGET, wb))


def classify_word_direction(word_count: int, word_budget: int) -> str:
    """单章偏方向：``under`` / ``over`` / ``ok``（纯函数，脏值宽容不抛）。

    - under：字数低于续写触发线（比 ``word_status_for`` 的 0.8×预算更严——
      小预算章取全额预算为线，与执行侧「不达标必续写」一致）；
    - over：字数高于 ``WORD_OVER_RATIO``×预算 —— 与 ``_should_auto_shrink``
      （超字数自动压缩门槛）逐字同判据，确保「计划说 over」⇔「执行真会缩」。
    """
    try:
        wc = int(word_count or 0)
    except (TypeError, ValueError):
        wc = 0
    try:
        wb = int(word_budget or 0)
    except (TypeError, ValueError):
        wb = 0
    if wb <= 0:
        wb = DEFAULT_WORD_BUDGET
    if wc < continue_trigger_line(wb):
        return DIRECTION_UNDER
    if wc > wb * WORD_OVER_RATIO:
        return DIRECTION_OVER
    return DIRECTION_OK


def build_adjust_plan(entries: list[dict],
                      *,
                      batch_size: int = WORD_ADJUST_BATCH_SIZE) -> dict:
    """按目录顺序（= 输入顺序，调用方须传 DFS 序叶子清单）切批。

    Args:
        entries: ``[{"section_id","title","word_count","word_budget"}, ...]``
            脏值宽容：缺字段按 0/空处理，绝不抛异常；同一 section_id
            重复出现时**只保留第一条**（目录序在前者优先）。
        batch_size: 每批章数（钳制 ≥1；易标口径默认 10）。

    Returns:
        ``{"items": [带 direction 的调整项（仅 under/over，目录序）],
          "batches": [[item, ...], ...],   # 批间串行、批内由调用方并发
          "under_count", "over_count", "ok_count", "total_sections"}``

    刻意不返回「批内执行序打乱」的信息 —— 落库/展示/导出顺序与目录一致
    是本仓 R105 既有纪律，调度只决定「谁与谁同批并发」，不改变任何序。
    """
    try:
        size = max(1, int(batch_size or 1))
    except (TypeError, ValueError):
        size = WORD_ADJUST_BATCH_SIZE

    items: list[dict] = []
    seen: set = set()
    under_count = over_count = ok_count = 0
    total = 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        total += 1
        sid = e.get("section_id")
        if sid in seen:
            continue
        seen.add(sid)
        try:
            wb = int(e.get("word_budget") or 0)
        except (TypeError, ValueError):
            wb = 0
        try:
            wc = int(e.get("word_count") or 0)
        except (TypeError, ValueError):
            wc = 0
        direction = classify_word_direction(wc, wb)
        if direction == DIRECTION_OK:
            ok_count += 1
            continue
        if direction == DIRECTION_UNDER:
            under_count += 1
        else:
            over_count += 1
        items.append({
            "section_id": sid,
            "title": str(e.get("title") or ""),
            "word_count": wc,
            "word_budget": wb,
            "direction": direction,
        })
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    return {
        "items": items,
        "batches": batches,
        "under_count": under_count,
        "over_count": over_count,
        "ok_count": ok_count,
        "total_sections": total,
    }
