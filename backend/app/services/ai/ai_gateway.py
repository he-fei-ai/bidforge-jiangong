"""统一 AI 网关门面（R150）—— 业务调用层与厂商适配层之间的唯一入口

架构分层（本轮显式化，代码事实此前已存在但入口散落）
------------------------------------------------------
::

    业务层（outline / content / facts / review / ...）
        │  只 import 本模块，不感知厂商差异
        ▼
    ai_gateway（本模块 · 业务调用层门面）
        │  chat / stream_chat / request_json / chat_tools / refresh
        ▼
    provider_factory（编排层：场景路由 → 候选链 → 门禁 → 退避/对冲/降级）
        │
        ▼
    providers/*（厂商适配层：openai_compatible / anthropic_compatible，
                 统一 chat/stream 语义、SSE→文本增量、usage 三字段归一化）

为什么需要门面（而不是让业务层继续直接 import provider_factory）：

  1. **入口收敛**：此前业务层散点 import ``chat_with_fallback`` /
     ``collect_json_response`` / ``chat_with_tools``，三种调用形态各自
     组装参数；门面把 ``chat`` / ``stream_chat`` / ``request_json`` /
     ``chat_tools`` 四种语义统一命名，新增业务调用点不再需要理解编排层
     的参数细节（scene 透传、json_mode 降级等）；
  2. **可替换性**：编排层内部实现（候选链 / 对冲 / 退避）演进时，
     门面签名稳定，业务层零改动；
  3. **运维入口**：``refresh()`` 聚合配置热更新失效动作，
     ``gateway_snapshot()`` 聚合实时状态，排障不再需要知道内部变量名。

向后兼容：既有业务代码继续直接调用 provider_factory 也完全正常
（门面是薄透传，不做任何参数改写）；新代码建议走门面。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from app.services.ai import provider_factory as _pf
from app.services.ai.error_taxonomy import (  # noqa: F401  (re-export)
    ERROR_CLASSES,
    SKIP_CLASSES,
    TRANSIENT_CLASSES,
    classify_error,
    error_class_label,
)
from app.services.ai.json_response import collect_json_response

__all__ = [
    "chat", "stream_chat", "request_json", "chat_tools", "refresh",
    "gateway_snapshot", "classify_error", "error_class_label",
    "ERROR_CLASSES", "TRANSIENT_CLASSES", "SKIP_CLASSES",
]


async def chat(messages: list, *, scene: str = "",
               ai_config: dict | None = None,
               temperature: float | None = None,
               json_mode: bool = False,
               timeout: int | None = None,
               max_tokens: int | None = None,
               reasoning_effort: str = "") -> str:
    """统一对话入口：场景路由 + 降级链 + 对冲竞速 + 瞬态退避（透传编排层）。"""
    return await _pf.chat_with_fallback(
        messages, ai_config=ai_config, temperature=temperature,
        json_mode=json_mode, timeout=timeout, max_tokens=max_tokens,
        scene=scene, reasoning_effort=reasoning_effort)


def stream_chat(messages: list, *, scene: str = "",
                ai_config: dict | None = None,
                temperature: float | None = None,
                json_mode: bool = False,
                timeout: int | None = None,
                max_tokens: int | None = None,
                reasoning_effort: str = "") -> AsyncIterator[dict]:
    """统一流式入口：产出归一化事件 ``{"type": "delta"/"done", ...}``。

    语义与事件格式见
    :func:`provider_factory.chat_stream_with_fallback`（厂商间 SSE 差异
    已由适配层抹平；部分增量产出后断流 = 整体失败，消费方丢弃重来）。
    """
    return _pf.chat_stream_with_fallback(
        messages, ai_config=ai_config, temperature=temperature,
        json_mode=json_mode, timeout=timeout, max_tokens=max_tokens,
        scene=scene, reasoning_effort=reasoning_effort)


async def request_json(messages: list, validate_fn=None,
                       max_retries: int | None = None, *,
                       scene: str = "",
                       temperature: float | None = None,
                       json_mode: bool = False,
                       timeout: int | None = None,
                       max_tokens: int | None = None,
                       schema=None,
                       target_desc: str = "") -> Any:
    """统一 JSON 入口：生成 → 解析 → 校验 → 定向修复 → 重试。

    ``max_retries`` 缺省时沿用 :func:`json_response.collect_json_response`
    的既有默认值（MAX_REPAIR_RETRIES），行为逐字不变；JSON 模式不被厂商
    支持时由编排层自动回退普通模式 + 正则/围栏提取。

    ✅ R152（2026-11-01 · 遗留收口）：移除上一版暴露的 ``repair_key`` 转发
    形参 —— 该门面全仓零调用方、零测试锚，而「调用方值布尔默认兜底」的转发
    写法被 repair_key 形态扫描器判为漂移信号（转发面正是该护栏要猎杀的形态，
    R149 FIX-H 对 ``_collect_ai`` 的白名单是有双分支锁的特例，此处没有）。
    修复键恒为 GENERIC_REPAIR_KEY；需要业务归因的调用方直接用
    ``json_response.collect_json_response``（既有惯例）。
    """
    from app.services.ai.json_response import GENERIC_REPAIR_KEY, MAX_REPAIR_RETRIES
    return await collect_json_response(
        messages, validate_fn,
        max_retries if max_retries is not None else MAX_REPAIR_RETRIES,
        temperature=temperature, json_mode=json_mode, timeout=timeout,
        repair_key=GENERIC_REPAIR_KEY, scene=scene,
        max_tokens=max_tokens, schema=schema, target_desc=target_desc)


async def chat_tools(messages: list, *, tools: list, tool_choice=None,
                     scene: str = "",
                     ai_config: dict | None = None,
                     temperature: float | None = None,
                     max_tokens: int | None = None,
                     timeout: int | None = None,
                     reasoning_effort: str = "") -> dict:
    """统一工具调用入口（Agent 工具循环专用，顺序尝试支持 tools 的候选）。"""
    return await _pf.chat_with_tools(
        messages, tools=tools, tool_choice=tool_choice, ai_config=ai_config,
        scene=scene, temperature=temperature, max_tokens=max_tokens,
        timeout=timeout, reasoning_effort=reasoning_effort)


async def refresh() -> dict:
    """配置热更新聚合入口：立即失效全部网关缓存（配置/降级链/场景路由/环境/开关）。

    与「文本模型配置模块」的联动：``save_ai_config`` / 运行时开关 /
    场景路由保存路径在写库后已各自调用 ``invalidate_config_cache``；
    本入口供外部编排（部署脚本 / 测试 / 运维手工操作）在**库被旁路修改**时
    强制立即生效，不必等 TTL（默认 300s）自然过期。
    """
    _pf.invalidate_config_cache()
    return {"ok": True, "invalidated": [
        "ai_config(主配置)", "fallback_chain(降级链)", "scene_routes(场景路由)",
        "active_env(环境)", "disabled_providers(运行时开关)"]}


def gateway_snapshot() -> dict:
    """网关实时状态快照（只读）：在飞/成败计数、并发档位、熔断与冷却状态。"""
    return {
        "live": _pf.get_ai_live_stats(),
        "concurrency": {
            "current": _pf.concurrency_controller.current,
            "target": _pf.concurrency_controller.target,
            "min": _pf.concurrency_controller.min_c,
            "max": _pf.concurrency_controller.max_c,
            "failure_rate": round(_pf.concurrency_controller.failure_rate(), 4),
            "avg_response_s": round(_pf.concurrency_controller.avg_response(), 2),
        },
        "circuit_breaker": _pf.circuit_breaker.snapshot_state(),
        "scene_tiers": dict(_pf.SCENE_TIERS),
        "tier_anchors": dict(_pf.TIER_ANCHOR_SCENES),
    }
