"""P0 — Agent 工具调用循环

对应易标 ``piRuntimeService.runTask`` 的**内层 while**（工具在一次
``session.prompt`` 内被模型自主调用）与本仓报告第五章 P0 条目：

    messages → LLM → 若有 tool_calls 则执行 → 追加 tool 结果 → 循环
    含轮数上限与超时

设计要点（与易标机制保持一致的部分以易标为准）：

1. **工具异常 fail-soft**：单个工具执行失败不抛出循环，而是回一条
   ``is_error`` 的 tool 消息让模型自行修正 —— 对应易标 pi 工具执行的
   ``isError:true`` 契约（``piJsonValidationTool.createToolResult``）。
2. **stop 由工具显式发起**：``report-failure`` 之类工具返回
   ``stop=True`` 后循环立即收束，对应易标「调用 report-failure 后任务
   立即结束，不要再调用任何工具」。
3. **轮数上限是硬闸**：超限以 ``stop_reason='turn_limit'`` 收束，不做静默
   重置 —— 否则提交修复「无次数上限」会退化成无限循环。
4. 本模块**不关心**具体 Provider：``chat_fn`` 由调用方注入，测试可直接打桩。
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("agent_loop")

#: 单次 ``run_agent_loop`` 默认最大模型轮数（易标按任务
#: ``max_turns_per_prompt`` 配置且 0 表示不限；本仓未接该配置前给安全默认，
#: 防止模型在提交修复阶段空转烧 token）。
DEFAULT_MAX_TURNS = 40

STOP_COMPLETED = "completed"
STOP_TURN_LIMIT = "turn_limit"
STOP_TOOL_STOP = "tool_stop"
STOP_TIMEOUT = "timeout"

#: 工具执行函数签名。两种形态（派发时按签名自动兼容，见 ``_dispatch``）：
#:
#: - ``handler(args, tool_call_id)`` —— 需要把调用 ID 回传给上层通道的工具
#:   （易标 ``ask-user`` / ``report-failure`` 的 ``execute(toolCallId, params)``
#:   1:1：前端问答按 tool_call_id 关联应答）；
#: - ``handler(args)`` —— 纯本地工具，不需要调用 ID。
ToolHandler = Callable[..., Awaitable["ToolCallOutcome"]]


@dataclass
class ToolCallOutcome:
    """单次工具执行结果（回灌给模型的 tool 消息体）。"""

    content: str
    is_error: bool = False
    #: True 表示工具要求立即终止本轮 Agent 循环（report-failure 语义）
    stop: bool = False
    #: 结构化结果，供宿主旁路读取（如 ask-user 的用户回答）
    payload: dict | None = None

    @classmethod
    def text(cls, content: str, *, is_error: bool = False,
             payload: dict | None = None) -> "ToolCallOutcome":
        return cls(content=content, is_error=is_error, payload=payload)

    @classmethod
    def error(cls, message: str) -> "ToolCallOutcome":
        return cls(content=message, is_error=True)

    @classmethod
    def json_result(cls, payload: dict, *, is_error: bool = False,
                    stop: bool = False) -> "ToolCallOutcome":
        """按易标工具契约返回 ``JSON.stringify(payload, null, 2)`` 文本。"""
        return cls(content=json.dumps(payload, ensure_ascii=False, indent=2),
                   is_error=is_error, stop=stop, payload=payload)


@dataclass
class AgentTool:
    """一个可供模型调用的工具。

    ``parameters`` 是 OpenAI function calling 使用的 JSON Schema
    （易标用 typebox ``Type.Object`` 构造，语义等价，本仓直接写 JSON Schema）。
    """

    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    #: 与易标 ``promptGuidelines`` 对齐：追加到提示词的调用守则
    prompt_guidelines: list[str] = field(default_factory=list)
    #: 易标 ``executionMode:'sequential'``（ask-user / report-failure 必须串行）
    sequential: bool = False

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AgentLoopResult:
    messages: list
    final_message: dict
    turns: int
    stop_reason: str
    #: 工具侧记录的结构化载荷（ask-user 回答、report-failure 原因等）
    tool_payloads: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return str(self.final_message.get("content") or "")

    @property
    def stopped_by_tool(self) -> bool:
        return self.stop_reason == STOP_TOOL_STOP


def build_tools_payload(tools: list[AgentTool]) -> list[dict]:
    return [t.to_openai() for t in tools]


def _normalize_arguments(raw: Any) -> dict:
    """厂商返回的 ``function.arguments`` 可能是 JSON 字符串或已解析 dict。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"arguments 不是合法 JSON：{e}") from e
        if not isinstance(obj, dict):
            raise ValueError("arguments 解析结果必须是 JSON 对象")
        return obj
    raise ValueError(f"arguments 类型不支持：{type(raw).__name__}")


def normalize_assistant_message(raw: Any) -> dict:
    """把 Provider 返回值归一为标准 assistant 消息。

    ``chat()`` 在未传 ``tools`` 时返回 ``str``（本仓既有契约），传了 ``tools``
    时返回 dict；测试桩两种形态都可能出现，这里统一兜底，避免循环里到处判型。
    """
    if isinstance(raw, str):
        return {"role": "assistant", "content": raw, "tool_calls": []}
    if not isinstance(raw, dict):
        return {"role": "assistant", "content": str(raw), "tool_calls": []}
    msg = dict(raw)
    msg.setdefault("role", "assistant")
    content = msg.get("content")
    if content is not None and not isinstance(content, str):
        msg["content"] = str(content)
    calls = msg.get("tool_calls")
    if not isinstance(calls, list):
        msg["tool_calls"] = []
        if msg.get("content") is None:
            msg["content"] = ""
    return msg


def _call_name(call: dict) -> str:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(fn.get("name") or "").strip()


async def _call_handler(handler: Callable[..., Awaitable["ToolCallOutcome"]],
                        args: dict, call_id: str) -> "ToolCallOutcome":
    """按 handler 声明的形参决定是否回传 ``tool_call_id``。"""
    try:
        params = inspect.signature(handler).parameters
        wants_call_id = "tool_call_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):  # 内置/不可自省对象：按不需要处理
        wants_call_id = False
    if wants_call_id:
        return await handler(args, tool_call_id=call_id)
    return await handler(args)


async def _dispatch(tool_index: dict[str, AgentTool], call: dict) -> ToolCallOutcome:
    """执行一次 tool_call；任何异常都转成 is_error 消息（fail-soft）。"""
    call_id = str(call.get("id") or "")
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = _call_name(call)
    tool = tool_index.get(name)
    if tool is None:
        known = "、".join(sorted(tool_index)) or "（无）"
        return ToolCallOutcome.error(f"未知工具「{name}」。当前可用工具：{known}")
    try:
        args = _normalize_arguments(fn.get("arguments"))
    except ValueError as e:
        return ToolCallOutcome.error(f"工具 {name} 的参数格式错误：{e}")
    logger.debug("agent tool call: %s(%s) id=%s", name,
                 json.dumps(args, ensure_ascii=False)[:200], call_id)
    try:
        # 易标 ``execute(toolCallId, params, signal)`` 的 1:1 对应：声明了
        # ``tool_call_id`` 的工具（ask-user / report-failure）拿得到调用 ID，
        # 用于前端问答按调用关联应答；未声明的纯本地工具不传，签名保持最简。
        outcome = await _call_handler(tool.handler, args, call_id)
    except Exception as e:  # noqa: BLE001 —— 工具内部异常一律回灌给模型
        logger.warning("工具 %s 执行异常（已转为回灌消息）：%s", name, e)
        return ToolCallOutcome.error(f"工具 {name} 执行失败：{e}")
    if not isinstance(outcome, ToolCallOutcome):
        return ToolCallOutcome.text(str(outcome))
    return outcome


async def run_agent_loop(
    messages: list,
    *,
    tools: list[AgentTool],
    chat_fn: Callable[..., Awaitable[Any]],
    max_turns: int = DEFAULT_MAX_TURNS,
    tool_choice: Any = None,
    total_timeout: float | None = None,
    stop_reason_when_no_tool_calls: str = STOP_COMPLETED,
    **chat_kwargs,
) -> AgentLoopResult:
    """执行一次完整的工具调用循环。

    Args:
        messages: 会话历史（会被原地追加，调用方若需保留副本请自行 copy）。
        tools: 可用工具；**至少要有一个**，否则没有下发 tools 的必要。
        chat_fn: ``async (messages, *, tools, tool_choice, **kw) -> str|dict``。
        max_turns: 单次循环的最大模型轮数（超限收束，不抛错）。
        total_timeout: 整轮循环的墙钟超时（秒）；``None`` 表示不限。
        stop_reason_when_no_tool_calls: 模型不再发起工具调用时的收束原因。
    Returns:
        :class:`AgentLoopResult`；调用方根据 ``stop_reason`` 决定后续动作
        （``turn_limit`` / ``timeout`` 应当中止任务，不能静默当成功）。
    """
    if not tools:
        raise ValueError("run_agent_loop 需要至少一个工具；无工具时应直接走普通 chat")
    tool_index = {t.name: t for t in tools}
    payload = build_tools_payload(tools)
    started = time.monotonic()
    turn = 0
    tool_payloads: list[dict] = []

    while True:
        turn += 1
        if max_turns and turn > max_turns:
            logger.warning("Agent 循环达到轮数上限 %d，强制收束", max_turns)
            return AgentLoopResult(messages=messages,
                                   final_message=messages[-1] if messages else {},
                                   turns=turn - 1, stop_reason=STOP_TURN_LIMIT,
                                   tool_payloads=tool_payloads)
        if total_timeout is not None and (time.monotonic() - started) > total_timeout:
            logger.warning("Agent 循环超过 %.0fs 墙钟超时，强制收束", total_timeout)
            return AgentLoopResult(messages=messages,
                                   final_message=messages[-1] if messages else {},
                                   turns=turn - 1, stop_reason=STOP_TIMEOUT,
                                   tool_payloads=tool_payloads)

        raw = await chat_fn(messages, tools=payload, tool_choice=tool_choice,
                            **chat_kwargs)
        assistant = normalize_assistant_message(raw)
        messages.append(assistant)
        calls = assistant.get("tool_calls") or []
        if not calls:
            return AgentLoopResult(messages=messages, final_message=assistant,
                                   turns=turn,
                                   stop_reason=stop_reason_when_no_tool_calls,
                                   tool_payloads=tool_payloads)

        should_stop = False
        for i, call in enumerate(calls):
            if not isinstance(call, dict):
                call = {}
            call_id = str(call.get("id") or f"call_{turn}_{i}")
            call_name = _call_name(call)
            outcome = await _dispatch(tool_index, call)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": outcome.content,
                # 非标准字段：部分 OpenAI 兼容厂商会原样回传，不影响协议
                **({"is_error": True} if outcome.is_error else {}),
            })
            if outcome.payload is not None:
                tool_payloads.append({"tool": call_name, "payload": outcome.payload})
            if outcome.stop:
                should_stop = True
                break
        if should_stop:
            return AgentLoopResult(messages=messages, final_message=assistant,
                                   turns=turn, stop_reason=STOP_TOOL_STOP,
                                   tool_payloads=tool_payloads)


__all__ = [
    "AgentLoopResult",
    "AgentTool",
    "DEFAULT_MAX_TURNS",
    "STOP_COMPLETED",
    "STOP_TIMEOUT",
    "STOP_TOOL_STOP",
    "STOP_TURN_LIMIT",
    "ToolCallOutcome",
    "build_tools_payload",
    "normalize_assistant_message",
    "run_agent_loop",
]
