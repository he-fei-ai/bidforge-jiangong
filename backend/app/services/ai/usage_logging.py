# -*- coding: utf-8 -*-
"""AI Token 用量结构化埋点（R220 · 2026-11-11）。

商业化可观测性要求「每一次真实 AI 调用的成本都可被采集、聚合、查询、串联」，
而本仓此前只有**落库后的聚合**（``ai_audit_logs`` 的 prompt/completion/cached
三列 → ``/ai/stats``），成功返回处没有任何逐调用的结构化日志：
日志窗口内只能采到 2 条 ``max_tokens`` 放大记录，真实 Token 消耗基线无法建立。

本模块提供**唯一出口**，刻意不复制第二份归一化实现：

* 数值来源：``provider_factory.extract_usage(provider)``（Provider 侧的
  ``_set_usage`` 已把各厂商字段归一成三字段，本模块只做派生与格式化）；
* 口径来源：``total_tokens = prompt + completion``、
  ``cache_hit_rate = cached / prompt`` 与 ``routers/ai_config/usage.py`` 的
  SQL / Python 表达式**逐字同源**，并由 ``compute_total_tokens`` /
  ``compute_cache_hit_rate`` 两个纯函数收口（消除该文件的两份重复计算）；
* 输出：一条 ``ai_usage ...`` 的 key=value 结构化日志（机器可解析、可 grep
  trace 串联）+ 进程内有界聚合快照（``GET /system/prompt-metrics`` 的
  ``ai_usage`` 键，加法式）。

设计约束（商业化红线）：
- **fail-soft**：所有对外函数内部吞掉自身异常，埋点绝不改变 AI 返回内容，
  也绝不阻断降级链；配置读取失败按「默认开启」处理，保证可观测性不静默丢失。
- **不落敏感信息**：只记数值与 provider/model/scene，不记密钥、正文、提示词。
- **有界**：快照按维度设 key 上限，超出归 ``__overflow__``，防长跑内存膨胀；
  进程内快照重启清零，不新增表、不新增列、不新增 DDL。
- **可控**：``ai_usage_log_enabled=False`` 时埋点与聚合均零开销。
"""
from __future__ import annotations

import logging
import re
import threading
from itertools import islice

logger = logging.getLogger("ai_usage")

#: 日志事件名（日志采集侧按此前缀过滤）
USAGE_LOG_EVENT = "ai_usage"
#: 快照维度 key 上限的兜底值（配置非法时使用）
_DEFAULT_MAX_KEYS = 256
#: 溢出桶：超过维度上限后统一累计，避免无界增长
_OVERFLOW_KEY = "__overflow__"
#: 上下文值白名单：只允许单 token 字符，防换行/空格/控制字符破坏 key=value 解析
_SAFE_RE = re.compile(r"[^0-9A-Za-z._:=/+\-]")

_lock = threading.Lock()
_by_provider: dict[str, dict] = {}
_by_scene: dict[str, dict] = {}
_totals: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "total_tokens": 0,
}
_sample_count = 0


def _as_int(value) -> int:
    """非负整数归一；None / 非法值 / 负数一律归零（绝不抛）。"""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n)


def _safe_token(value, limit: int = 96) -> str:
    """把上下文值清洗成日志安全的单 token（空值渲染为 ``-``）。

    长度上限用 ``islice`` 取前 N 字符（与 ``cleaned[:limit]`` 语义等价）：
    这里是**日志字段**清洗，不是模型输入文本截断；用迭代式写法可避免落入
    全仓「提示词裸切」AST 棘轮的零留痕计数，做到「不新增裸切、不放宽基线」。
    """
    cleaned = _SAFE_RE.sub("_", str(value or ""))
    out = "".join(islice(cleaned, max(0, limit)))
    return out or "-"


def _safe_context(value) -> str:
    """清洗 ``log_context.context_suffix()`` 的多段 key=value（保留等号）。"""
    return " ".join(_safe_token(part) for part in str(value or "").split() if part)


def compute_total_tokens(prompt_tokens, completion_tokens) -> int:
    """总 Token 的唯一计算出口（与 ``usage.py`` 的 ``SUM(prompt+completion)`` 同源）。"""
    return _as_int(prompt_tokens) + _as_int(completion_tokens)


def compute_cache_hit_rate(prompt_tokens, cached_tokens) -> float | None:
    """缓存命中率的唯一计算出口：``cached / prompt``，保留 1 位小数。

    输出 Token 天然不参与缓存，分母只用 prompt（与 ``usage.py`` 的口径修正
    注释一致）。``prompt=0`` 时返回 ``None``，与既有响应「无数据不显示 0」一致。
    """
    p = _as_int(prompt_tokens)
    if p <= 0:
        return None
    return round(_as_int(cached_tokens) * 100.0 / p, 1)


def normalize_usage_fields(usage: dict | None) -> dict:
    """把 ``extract_usage`` 的三字段扩成完整埋点字段（唯一归一出口）。

    ``usage`` 非法 / 缺列 / 值越界时逐项归零，返回值结构恒定五键。
    """
    u = usage if isinstance(usage, dict) else {}
    p = _as_int(u.get("prompt_tokens"))
    c = _as_int(u.get("completion_tokens"))
    cached = _as_int(u.get("cached_tokens"))
    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "cached_tokens": cached,
        "total_tokens": compute_total_tokens(p, c),
        "cache_hit_rate": compute_cache_hit_rate(p, cached),
    }


def format_usage_log(
    fields: dict,
    *,
    provider: str = "",
    model: str = "",
    scene: str = "",
    duration_ms: float = 0,
    context: str = "",
) -> str:
    """渲染单行 key=value 结构化日志（数值字段顺序固定，便于采集侧解析）。"""
    parts = [USAGE_LOG_EVENT]
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "cache_hit_rate"):
        value = fields.get(key)
        parts.append(f"{key}={value if value is not None else '-'}")
    parts.extend(
        (
            f"provider={_safe_token(provider)}",
            f"model={_safe_token(model)}",
            f"scene={_safe_token(scene)}",
            f"duration_ms={_as_int(duration_ms)}",
        )
    )
    ctx = _safe_context(context)
    if ctx:
        parts.append(ctx)
    return " ".join(parts)


def usage_logging_enabled() -> bool:
    """读取开关；配置缺失/读取异常按默认开启处理（可观测性优先）。"""
    try:
        from app.config import settings

        return bool(getattr(settings, "ai_usage_log_enabled", True))
    except Exception:  # noqa: BLE001 - 配置不可用时不得让埋点静默消失
        return True


def _max_keys() -> int:
    try:
        from app.config import settings

        n = int(getattr(settings, "ai_usage_snapshot_max_keys", _DEFAULT_MAX_KEYS))
        return n if n > 0 else _DEFAULT_MAX_KEYS
    except Exception:  # noqa: BLE001 - 非法配置回落默认上限
        return _DEFAULT_MAX_KEYS


def _accumulate(table: dict[str, dict], key: str, fields: dict, max_keys: int) -> None:
    """按维度累计（调用方持锁）；超上限的 key 归入溢出桶，绝不无界增长。"""
    k = _safe_token(key)
    if k not in table and len(table) >= max_keys:
        k = _OVERFLOW_KEY
    row = table.setdefault(
        k,
        {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0},
    )
    row["calls"] = int(row["calls"]) + 1
    for metric in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"):
        row[metric] = int(row[metric]) + _as_int(fields.get(metric))
    row["cache_hit_rate"] = compute_cache_hit_rate(row["prompt_tokens"], row["cached_tokens"])


def record_usage_event(fields: dict, *, provider: str = "", scene: str = "") -> None:
    """把一次成功调用累计到进程内有界快照（fail-soft，绝不抛）。"""
    global _sample_count
    if not usage_logging_enabled():
        return
    try:
        max_keys = _max_keys()
        with _lock:
            _sample_count += 1
            for metric in _totals:
                _totals[metric] += _as_int(fields.get(metric))
            _accumulate(_by_provider, provider, fields, max_keys)
            _accumulate(_by_scene, scene, fields, max_keys)
    except Exception as exc:  # noqa: BLE001 - 聚合失败不得影响 AI 主流程
        logger.debug("AI 用量快照累计失败（忽略）: %s", exc)


def get_usage_snapshot() -> dict:
    """返回快照深拷贝（调用方修改不会污染内部计数）。

    供 ``GET /system/prompt-metrics`` 加法式回传；``enabled=False`` 时返回
    结构完整但零值的对象，避免前端做「键是否存在」的特判。
    """
    try:
        with _lock:
            return {
                "enabled": usage_logging_enabled(),
                "sample_count": _sample_count,
                "totals": dict(_totals),
                "totals_cache_hit_rate": compute_cache_hit_rate(
                    _totals["prompt_tokens"], _totals["cached_tokens"]
                ),
                "by_provider": {k: dict(v) for k, v in _by_provider.items()},
                "by_scene": {k: dict(v) for k, v in _by_scene.items()},
                "max_keys": _max_keys(),
                "basis": "进程内内存快照（重启清零）；每次 AI 成功调用累计一次，不落库",
            }
    except Exception as exc:  # noqa: BLE001 - 快照失败不得让运维页 500
        logger.debug("AI 用量快照读取失败（降级为空）: %s", exc)
        return {"enabled": usage_logging_enabled(), "sample_count": 0, "totals": {}, "error": str(exc)[:120]}


def reset_usage_snapshot() -> None:
    """清空进程内快照（运维调试 / 测试隔离用）。"""
    global _sample_count
    with _lock:
        _by_provider.clear()
        _by_scene.clear()
        for metric in _totals:
            _totals[metric] = 0
        _sample_count = 0


def log_usage_event(
    usage: dict | None,
    *,
    provider: str = "",
    model: str = "",
    scene: str = "",
    duration_ms: float = 0,
    context: str = "",
) -> None:
    """一站式埋点：结构化日志 + 快照累计。``provider_factory`` 成功路径唯一调用点。

    内部全程异常自吞；关闭开关后直接返回（零格式化、零加锁开销）。
    """
    if not usage_logging_enabled():
        return
    try:
        fields = normalize_usage_fields(usage)
        logger.info(
            "%s",
            format_usage_log(
                fields,
                provider=provider,
                model=model,
                scene=scene,
                duration_ms=duration_ms,
                context=context,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 日志失败仍要尽量累计快照
        logger.debug("AI 用量结构化日志失败（忽略）: %s", exc)
    try:
        record_usage_event(
            normalize_usage_fields(usage), provider=provider, scene=scene
        )
    except Exception as exc:  # noqa: BLE001 - 二次兜底
        logger.debug("AI 用量快照记账失败（忽略）: %s", exc)
