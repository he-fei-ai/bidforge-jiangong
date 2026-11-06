# ✅ R179 U5（2026-11-05）：分组预热编排（先 1 个代表探路 → 放开并发）。
#
# 设计边界（方案 §2，冻结契约）：
#   - **默认关**：`ai_warmup_enabled=False` 时 run_grouped 走「现状调度」分支，
#     不加任何等待、不改调用顺序、不打 AI_WARMUP 日志 —— 与引入前逐字等价。
#   - 固定**第一个**为代表（不选最短/最典型）：预热目标是共享前缀进缓存，
#     同组任一项覆盖同一前缀；固定第一个可复现、可测。
#   - 失败语义不优化：429/超时/网络/5xx → **不换代表**、整组退化串行；
#     parse_failed → 换下一个代表**仅一次**；认证/配置类 → 整组放弃。
#   - 编排器只加法式回传 `warmup` 诊断对象，不参与既有 failed_sections/partial 判定。
#   - 并发上限始终取调用方传入值，并受 `max_concurrency` 夹取；永不自行抬高。
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

logger = logging.getLogger(__name__)

#: 代表/单项失败后「整组退化串行」的错误类（不换代表）。
_SERIAL_FALLBACK_CLASSES = {"rate_limit", "timeout", "network", "upstream_5xx"}
#: 代表失败后「整组放弃」的错误类（认证/配额/配置，重试必败）。
_ABANDON_CLASSES = {"auth"}
#: 未发起（停止/整组放弃）的占位结果。
SKIPPED = {"status": "skipped", "reason": "warmup_skipped"}


@dataclass
class WarmupPlan:
    """一个预热分组：同 (阶段, 模板, 模型, 共享前缀指纹) 的项集合。"""

    key: str
    items: list[Any]
    concurrency: int
    representative_index: int = 0
    prefix_fingerprint: str = ""
    delay_ms: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _settings(settings_obj=None):
    if settings_obj is not None:
        return settings_obj
    from app.config import settings as _s

    return _s


def _json_map(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def warmup_stage_enabled(stage: str, settings_obj=None) -> bool:
    """该阶段是否启用预热（仅开关打开时读取，关时返回 False 且不读其他键）。"""
    s = _settings(settings_obj)
    if not bool(getattr(s, "ai_warmup_enabled", False)):
        return False
    stages = str(getattr(s, "ai_warmup_stages", "") or "")
    if not stages.strip():
        return True
    return stage in {x.strip() for x in stages.split(",") if x.strip()}


def stage_concurrency(stage: str, settings_obj=None) -> int:
    """分阶段并发（唯一入口）。

    关闭时返回 0 —— 调用方统一写 `conc = stage_concurrency(stage) or <现状并发>`，
    0 走 or 分支即逐字现状，无需在各调用点写 if。
    """
    s = _settings(settings_obj)
    if not bool(getattr(s, "ai_warmup_enabled", False)):
        return 0
    base = int(getattr(s, "ai_warmup_stage_concurrency", 2) or 2)
    override = _json_map(getattr(s, "ai_warmup_stage_concurrency_map", "")).get(stage)
    raw = int(override) if override is not None else base
    hard = max(1, int(getattr(s, "max_concurrency", 5) or 5))
    return max(1, min(raw, hard))


def stage_delay_ms(stage: str, settings_obj=None) -> int:
    """分阶段等待（毫秒）；空覆盖表回落到 ai_warmup_delay_ms。"""
    s = _settings(settings_obj)
    override = _json_map(getattr(s, "ai_warmup_stage_delay_ms", "")).get(stage)
    if override is not None:
        try:
            return max(0, int(override))
        except (TypeError, ValueError):
            pass
    return max(0, int(getattr(s, "ai_warmup_delay_ms", 5000) or 5000))


def config_selfcheck(settings_obj=None) -> dict[str, Any]:
    """配置自检（只读配置，不发起任何 AI 调用）。

    返回诊断对象：
      - status=disabled / enabled_but_no_gain / enabled
      - equivalent=verbatim（前两种都等于现状）
      - clamped / effective_concurrency / message
    """
    s = _settings(settings_obj)
    if not bool(getattr(s, "ai_warmup_enabled", False)):
        return {
            "status": "disabled",
            "equivalent": "verbatim",
            "clamped": False,
            "effective_concurrency": 0,
            "message": "ai_warmup_enabled=False，不读取其余 ai_warmup_*（逐字现状）",
        }
    base = int(getattr(s, "ai_warmup_stage_concurrency", 2) or 2)
    hard = max(1, int(getattr(s, "max_concurrency", 5) or 5))
    clamped = base > hard
    effective = max(1, min(base, hard))
    if effective <= 1:
        return {
            "status": "enabled_but_no_gain",
            "equivalent": "verbatim",
            "clamped": clamped,
            "effective_concurrency": effective,
            "message": (
                "ai_warmup_enabled=True 但阶段并发上限=1，等于未开启（无并发收益）；"
                "请检查 ai_warmup_stage_concurrency"
            ),
        }
    return {
        "status": "enabled",
        "equivalent": "changed",
        "clamped": clamped,
        "effective_concurrency": effective,
        "message": f"阶段并发上限={effective}" + ("（已按 max_concurrency 夹取）" if clamped else ""),
    }


def log_config_selfcheck(settings_obj=None) -> dict[str, Any]:
    """启动时打一条结构化日志 AI_WARMUP_CONFIG，并返回同一诊断对象。"""
    report = config_selfcheck(settings_obj)
    if report["status"] == "enabled_but_no_gain":
        logger.warning(
            "AI_WARMUP_CONFIG status=%s equivalent=%s clamped=%s effective=%s message=%s",
            report["status"],
            report["equivalent"],
            report["clamped"],
            report["effective_concurrency"],
            report["message"],
        )
    else:
        logger.info(
            "AI_WARMUP_CONFIG status=%s equivalent=%s clamped=%s effective=%s message=%s",
            report["status"],
            report["equivalent"],
            report["clamped"],
            report["effective_concurrency"],
            report["message"],
        )
    return report


# ---------------------------------------------------------------------------
# 结果分类
# ---------------------------------------------------------------------------
def classify_result(result: Any, error: BaseException | None = None) -> str:
    """把 runner 返回值 / 异常映射成契约类别（success/parse_failed/...）。"""
    try:
        from app.services.ai.provider_factory import _error_class, is_non_retryable_error
    except Exception:  # provider 层不可用（极端测试替身）→ 保守按失败处理
        _error_class = None
        is_non_retryable_error = None

    if error is not None:
        if is_non_retryable_error is not None and is_non_retryable_error(error):
            return "auth"
        cls = _error_class(error) if _error_class is not None else "unknown"
        if cls in ("invalid_response", "truncated"):
            return "parse_failed"
        if cls in _SERIAL_FALLBACK_CLASSES:
            return cls
        if cls == "auth":
            return "auth"
        return "failed"

    if isinstance(result, dict):
        if result.get("parse_failed"):
            return "parse_failed"
        cls = str(result.get("error_class") or "")
        if cls in ("invalid_response", "truncated"):
            return "parse_failed"
        if cls in _SERIAL_FALLBACK_CLASSES:
            return cls
        if cls in _ABANDON_CLASSES:
            return "auth"
        ok = result.get("success", result.get("ok", None))
        if ok is False or str(result.get("status") or "") == "error":
            return "failed"
    return "success"


def _is_ok(cls: str) -> bool:
    return cls == "success"


def _item_id(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("id", "item_id", "section_id", "name"):
            value = item.get(key)
            if value:
                return str(value)
    return f"#{index}"


# ---------------------------------------------------------------------------
# 调度原语（并发/串行）
# ---------------------------------------------------------------------------
async def _call(runner, item, stop_check):
    if stop_check is not None and stop_check():
        return SKIPPED, "stopped", None
    try:
        return await runner(item), None, None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # 单项失败不打断整组
        return None, e, None


async def _run_serial(indexes, items, runner, results, stop_check):
    for idx in indexes:
        if stop_check is not None and stop_check():
            results[idx] = SKIPPED
            continue
        try:
            results[idx] = await runner(items[idx])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            results[idx] = e


async def _run_concurrent(indexes, items, runner, results, concurrency, stop_check):
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(idx: int) -> None:
        async with sem:
            if stop_check is not None and stop_check():
                results[idx] = SKIPPED
                return
            try:
                results[idx] = await runner(items[idx])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                results[idx] = e

    await asyncio.gather(*[_one(i) for i in indexes])


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------
async def run_grouped(
    plans: Sequence[WarmupPlan],
    runner: Callable[[Any], Awaitable[Any]],
    *,
    enabled: bool,
    default_delay_ms: int,
    fallback_serial: bool = True,
    rep_retry_on_parse: bool = True,
    stop_check: Callable[[], bool] | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    clock_fn: Callable[[], float] | None = None,
) -> list[dict[str, Any]]:
    """按分组执行预热编排，返回每组的 `{key, results, warmup}`。

    `enabled=False`（默认）→ 完全走现状调度：不等待、不重排、不打日志。
    """
    sleep_fn = sleep_fn or asyncio.sleep
    clock_fn = clock_fn or time.monotonic
    out: list[dict[str, Any]] = []

    for plan in plans:
        items = list(plan.items or [])
        concurrency = max(1, int(plan.concurrency or 1))
        results: list[Any] = [SKIPPED for _ in items]
        warmup: dict[str, Any] = {
            "stage": plan.key,
            "group": (plan.prefix_fingerprint or "")[:8],
            "representative_id": _item_id(items[plan.representative_index], plan.representative_index)
            if items and 0 <= plan.representative_index < len(items)
            else "",
            "representative_ok": False,
            "representative_class": "",
            "wait_ms": 0,
            "switch_at_ms": 0,
            "concurrency": concurrency,
            "fallback_serial": bool(fallback_serial),
            "total": len(items),
            "success": 0,
            "failed": 0,
            "applied": False,
            "degraded_reason": "",
        }

        # 现状调度分支：关闭 / 单项 / 并发≤1 都不接管（自检②：开了但上限=1 也等于现状）
        if not enabled or len(items) <= 1 or concurrency <= 1:
            if concurrency > 1:
                await _run_concurrent(range(len(items)), items, runner, results, concurrency, stop_check)
            else:
                await _run_serial(range(len(items)), items, runner, results, stop_check)
            warmup["applied"] = False
            warmup["degraded_reason"] = "disabled" if not enabled else "no_gain"
            out.append({"key": plan.key, "results": results, "warmup": warmup})
            _count(warmup, results)
            continue

        started = clock_fn()
        rep_idx = plan.representative_index if 0 <= plan.representative_index < len(items) else 0
        if rep_idx != 0:  # 固定第一个是默认；显式指定时按指定项
            items[0], items[rep_idx] = items[rep_idx], items[0]
            rep_idx = 0
        rep = items[0]
        rep_result, rep_error, _ = await _call(runner, rep, stop_check)
        rep_cls = classify_result(rep_result, rep_error)
        if stop_check is not None and stop_check() and rep_result is SKIPPED:
            rep_cls = "stopped"
        results[0] = SKIPPED if rep_cls == "stopped" else (rep_error if rep_error else rep_result)
        warmup["applied"] = True
        warmup["representative_class"] = rep_cls

        remaining = list(range(1, len(items)))
        delay_ms = plan.delay_ms if plan.delay_ms is not None else int(default_delay_ms or 0)
        degraded = ""
        abandoned = False

        if _is_ok(rep_cls):
            warmup["representative_ok"] = True
            if remaining and delay_ms > 0:
                await sleep_fn(delay_ms / 1000)
                warmup["wait_ms"] = delay_ms
            warmup["switch_at_ms"] = int((clock_fn() - started) * 1000)
            await _run_concurrent(remaining, items, runner, results, concurrency, stop_check)
        elif rep_cls == "parse_failed" and rep_retry_on_parse and len(items) > 1:
            # 换下一个代表，仅一次
            second = items[1]
            second_result, second_error, _ = await _call(runner, second, stop_check)
            second_cls = classify_result(second_result, second_error)
            results[1] = second_error if second_error else second_result
            warmup["representative_class"] = f"parse_failed->{second_cls}"
            remaining = list(range(2, len(items)))
            if _is_ok(second_cls):
                warmup["representative_ok"] = True
                if remaining and delay_ms > 0:
                    await sleep_fn(delay_ms / 1000)
                    warmup["wait_ms"] = delay_ms
                warmup["switch_at_ms"] = int((clock_fn() - started) * 1000)
                await _run_concurrent(remaining, items, runner, results, concurrency, stop_check)
            elif second_cls in _ABANDON_CLASSES or second_cls == "stopped":
                abandoned = True
                degraded = second_cls
            else:
                degraded = second_cls
                await _run_serial(remaining, items, runner, results, stop_check)
        elif rep_cls in _ABANDON_CLASSES or rep_cls == "stopped":
            abandoned = True
            degraded = rep_cls
        elif rep_cls == "parse_failed":
            degraded = rep_cls
            await _run_serial(remaining, items, runner, results, stop_check)
        else:
            # 429 / 超时 / 网络 / 5xx / 其他失败：不换代表，整组退化串行
            degraded = rep_cls
            if fallback_serial:
                await _run_serial(remaining, items, runner, results, stop_check)
            else:
                await _run_concurrent(remaining, items, runner, results, concurrency, stop_check)

        if abandoned:
            warmup["representative_ok"] = False
        warmup["degraded_reason"] = degraded
        warmup["switch_at_ms"] = warmup["switch_at_ms"] or int((clock_fn() - started) * 1000)
        _count(warmup, results)
        _log_group(warmup)
        out.append({"key": plan.key, "results": results, "warmup": warmup})

    return out


def _count(warmup: dict[str, Any], results: list[Any]) -> None:
    ok = 0
    failed = 0
    for res in results:
        if res is SKIPPED or isinstance(res, BaseException):
            failed += 1
        else:
            cls = classify_result(res)
            if _is_ok(cls):
                ok += 1
            else:
                failed += 1
    warmup["success"] = ok
    warmup["failed"] = failed


def _log_group(warmup: dict[str, Any]) -> None:
    """单行结构化日志（开关打开才调用）。"""
    logger.info(
        "AI_WARMUP stage=%s group=%s rep=%s rep_ok=%s rep_class=%s wait_ms=%s "
        "switch_at_ms=%s total=%s ok=%s failed=%s concurrency=%s fallback_serial=%s degraded=%s",
        warmup.get("stage"),
        warmup.get("group"),
        warmup.get("representative_id"),
        warmup.get("representative_ok"),
        warmup.get("representative_class"),
        warmup.get("wait_ms"),
        warmup.get("switch_at_ms"),
        warmup.get("total"),
        warmup.get("success"),
        warmup.get("failed"),
        warmup.get("concurrency"),
        warmup.get("fallback_serial"),
        warmup.get("degraded_reason"),
    )
