"""提示词运行时指标（进程内内存计数器）。

这是**运维观测指标**，不是持久化业务数据 —— 进程重启即清零。刻意不持久化、
不做历史时间序列、不引入 Prometheus：进程内 ``collections.Counter`` 足够回答
「哪个模板被渲染最多 / 哪类 JSON 修复触发最频繁 / 哪个业务场景 AI 失败最多」。

热路径纪律（``_cache.get_prompt`` 每次正文/目录生成被调几十次）：
  * 每个埋点包 try/except —— 计数失败**绝不**阻断 prompt 渲染主流程（fail-soft）；
  * 只做一行 Counter 自增，不打日志、不加锁（CPython GIL 下 Counter 自增是
    原子读改写，极端竞争下最多丢 1~2 次计数，观测指标可接受）。

对外仅暴露两个读路径：
  * :func:`get_snapshot` —— 结构化快照（HTTP 端点消费）；
  * :func:`reset` —— 清零（运维调试 / 测试隔离）。
"""
from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

#: 每个模板 key 的渲染次数（成功返回即记一次）。
render_total: Counter = Counter()
#: 渲染异常次数（变量缺失导致抛错 / 模板解析失败等）。
render_errors: Counter = Counter()
#: token 预算截断触发次数（key = 被截断的资料段标签，如「全局事实」/「知识库」）。
#: ✅ 2026-10-07：唯一消费点是 `prompt_governance.allocate_context_budget`
#:   （原先全仓零调用方，端点永远回空 dict —— 运维无法区分「没截断」与「未埋点」）。
token_budget_truncated: Counter = Counter()
#: JSON 修复轮触发次数（key = repair_key；首轮输出未过校验进入修复循环即记一次）。
repair_triggered: Counter = Counter()
#: AI 调用失败/降级次数（key = 业务场景 scene）。
ai_failure_by_scene: Counter = Counter()


def _inc(counter: Counter, key: str) -> None:
    """一行自增；任何异常吞掉并 debug 级留痕（观测指标不得阻断主流程）。"""
    try:
        if key:
            counter[key] += 1
    except Exception:  # noqa: BLE001
        logger.debug("prompt metrics counter increment failed", exc_info=True)


def record_render(key: str) -> None:
    """记一次成功渲染（``_cache.get_prompt`` 正常返回时调用）。"""
    _inc(render_total, key)


def record_render_error(key: str) -> None:
    """记一次渲染异常（``_cache.get_prompt`` 抛错路径调用）。"""
    _inc(render_errors, key)


def record_token_budget_truncated(key: str) -> None:
    """记一次 token/长度预算截断（facts/章节正文超预算被切时调用）。"""
    _inc(token_budget_truncated, key)


def record_repair_triggered(repair_key: str) -> None:
    """记一次 JSON 修复轮被触发（首轮输出未过校验、进入修复循环时调用）。"""
    _inc(repair_triggered, repair_key)


def record_ai_failure(scene: str) -> None:
    """记一次 AI 调用失败/降级（首轮 chat 抛错路径调用）。"""
    _inc(ai_failure_by_scene, scene or "unknown")


def get_snapshot() -> dict:
    """导出结构化快照（全部为 dict 拷贝，调用方持有后修改不影响内部计数）。

    顶层固定 6 个键：5 个计数器 dict + ``registered_keys``（注册表全部 key，
    供运维对照「哪些模板从无人渲染过」）。
    """
    from app.services.ai.prompts._registry import (
        _ALL_PROMPTS,
        register_lazy_prompts,
    )
    try:
        register_lazy_prompts()
    except Exception:  # noqa: BLE001
        pass
    try:
        registered = sorted(_ALL_PROMPTS.keys())
    except Exception:  # noqa: BLE001
        registered = []
    return {
        "render_total": dict(render_total),
        "render_errors": dict(render_errors),
        "token_budget_truncated": dict(token_budget_truncated),
        "repair_triggered": dict(repair_triggered),
        "ai_failure_by_scene": dict(ai_failure_by_scene),
        "registered_keys": registered,
    }


def reset() -> None:
    """清零全部内存计数器（运维调试 / 测试隔离用）。"""
    for c in (render_total, render_errors, token_budget_truncated,
              repair_triggered, ai_failure_by_scene):
        try:
            c.clear()
        except Exception:  # noqa: BLE001
            pass
