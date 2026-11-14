"""OMP 上下文阈值路由（引擎层）：按上下文体积决定走哪一档引擎。

对应《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》§5.4「阈值路由与引擎选择」：

* 比率（附件 §5.4）：**正文生成 0.7 / 一致性修复 0.85 / 编排 0.8**；
* 判定：``上下文体积 > 上下文窗口 × 比率`` ⇒ 走 Agent 档（``omp`` 真 omp，
  否则 ``local`` 本地编排原语）；未超阈值 ⇒ 走平台主链（附件里的 ``chat``，
  本仓对应 :data:`omp_engine_router.ENGINE_ORDER` 里的 ``main``）。

⚠️ **只做引擎层判定，不改主链字数判据**：主链按 ``WORD_BUDGET_TIERS`` 判档位，
附件方案按「上下文窗口比率」判 —— 两套口径**互不干扰**：本模块只回答
「这次该不该走 Agent 档」，不参与「正文是否达标」的判定（避免同一判据两份副本）。

⚠️ 三档命名**与既有引擎路由同口径**（``omp`` / ``local`` / ``main``），
不另立第三套词汇（附件的 ``chat`` ≡ 本仓的 ``main``）。

⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

#: 任务类型 → 上下文阈值比率（附件 §5.4；``default`` 兜底）。
DEFAULT_THRESHOLD_RATIOS: dict[str, float] = {
    "content": 0.7,
    "audit": 0.85,
    "planning": 0.8,
    "outline": 0.8,
    "default": 0.8,
}

#: 引擎档位（与 ``omp_engine_router.ENGINE_ORDER`` 同口径；``main`` ≡ 附件的 chat）。
ENGINE_CHAT = "main"
ENGINE_OMP = "omp"
ENGINE_LOCAL = "local"


def _config_content_ratio() -> float | None:
    """``content`` 比率的**配置覆盖**（并行轮次 R237 的 ``agent_engine_context_threshold_ratio``）。

    ⚠️ **同源读取**，避免「配置一份、常量一份」的双份副本漂移（AGENTS §4.7 铁律）：
    配置缺失 / 非法 ⇒ 返回 ``None``（回落默认表 0.7）。
    """
    try:
        from app.config import settings

        raw = getattr(settings, "agent_engine_context_threshold_ratio", None)
    except Exception:  # noqa: BLE001 — 配置不可用时按「未配置」处理
        return None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 1 else None


def _ratio_for(task_type: str, ratios: dict | None) -> float:
    """取任务类型对应比率；非法/缺失 ⇒ 回落 ``default``（再兜底 0.8）。

    ⚠️ 调用方**显式传入** ``ratios`` 时以传入值为准；未传且任务为 ``content`` 时，
    读平台配置 ``agent_engine_context_threshold_ratio``（与并行轮次 R237 同源）。
    """
    key = str(task_type or "").strip().lower()
    if ratios is None and key == "content":
        override = _config_content_ratio()
        if override is not None:
            return override
    table = dict(ratios or DEFAULT_THRESHOLD_RATIOS)
    raw = table.get(key)
    if raw is None:
        raw = table.get("default", 0.8)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.8
    if not (0 < value <= 1):
        value = 0.8
    return value


def get_agent_threshold(
    *, context_limit: int, task_type: str = "", ratios: dict | None = None
) -> int:
    """上下文阈值（字）= 上下文窗口 × 比率；窗口未知（<=0）⇒ 0（判定为「不可按阈值路由」）。"""
    limit = int(context_limit or 0)
    if limit <= 0:
        return 0
    return int(limit * _ratio_for(task_type, ratios))


def select_engine_for_messages(
    *,
    task_type: str = "",
    context_chars: int = 0,
    context_limit: int = 0,
    omp_available: bool = True,
    local_available: bool = True,
    prefer: str = "",
    ratios: dict | None = None,
) -> dict:
    """按上下文体积选择引擎档位。

    返回：``{"engine", "ratio", "threshold", "context_chars", "reason"}``。

    规则（自上而下）：

    1. ``prefer`` 显式指定（``omp`` / ``local`` / ``main``）⇒ 直接采用；
    2. 上下文窗口未知（``context_limit <= 0``）⇒ ``main``（不臆造阈值）；
    3. 未超阈值 ⇒ ``main``（普通对话 / 平台主链）；
    4. 超阈值 ⇒ ``omp``（真 omp 可用）→ ``local``（本地编排原语）→ ``main``（兜底）。
    """
    ratio = _ratio_for(task_type, ratios)
    threshold = get_agent_threshold(
        context_limit=context_limit, task_type=task_type, ratios=ratios
    )
    chars = max(0, int(context_chars or 0))
    explicit = str(prefer or "").strip().lower()
    base = {
        "engine": ENGINE_CHAT,
        "ratio": ratio,
        "threshold": threshold,
        "context_chars": chars,
    }

    if explicit in (ENGINE_OMP, ENGINE_LOCAL, ENGINE_CHAT):
        base["engine"] = explicit
        base["reason"] = f"显式指定引擎档位 {explicit}"
        return base

    if threshold <= 0:
        base["reason"] = "上下文窗口未知（context_limit<=0）⇒ 走平台主链"
        return base

    if chars <= threshold:
        base["reason"] = f"未超阈值（{chars} <= {threshold}）⇒ 走平台主链"
        return base

    if omp_available:
        base["engine"] = ENGINE_OMP
        base["reason"] = f"超阈值（{chars} > {threshold}）且 omp 可用 ⇒ 走 omp 档"
        return base
    if local_available:
        base["engine"] = ENGINE_LOCAL
        base["reason"] = f"超阈值（{chars} > {threshold}）但 omp 不可用 ⇒ 走本地编排档"
        return base
    base["reason"] = f"超阈值（{chars} > {threshold}）但 Agent 档均不可用 ⇒ 回落平台主链"
    return base
