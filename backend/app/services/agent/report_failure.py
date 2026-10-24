"""P1 — report-failure 工具（1:1 移植自 piTaskFailureTool.cjs，双形态）

移植来源：易标 Yibiao 2.30.20 的 ``electron/services/pi/piTaskFailureTool.cjs``。

- **简单形态**（无终止决策通道）：报告失败原因并立即结束任务，返回
  ``{reported: true, reason}``，置 ``stop=True`` 让 Agent 循环收束
  （对应易标「调用 report-failure 后任务立即结束」与 P0 的 stop 契约）。
- **双形态**（注入 ``request_termination_decision``）：向用户确认是否终止，
  ``TERMINATION_OPTIONS`` 三项中文标签与两段问题/放宽文案逐字移植；
  用户选择继续时返回放宽执行要求且**不**停止，并置 ``continued`` 门禁——
  此后再次请求终止必须人工确认（``auto_answer=False``），避免自动作答
  反复放行形成死循环；用户确认终止时置 ``stop=True`` 收束。
- 与易标的差异：pi 的 execute 能拿到 tool_call_id，本仓 handler 契约只传
  参数 dict（见 agent_loop ``_dispatch``），故通道请求体不含该字段；
  注入通道支持同步/协程两种返回（``_maybe_await`` 统一等待）。
"""
from __future__ import annotations

import inspect

from app.services.agent.agent_loop import AgentTool, ToolCallOutcome

AGENT_TASK_FAILURE_TOOL_NAME = "report-failure"
AGENT_REPORTED_FAILURE_CODE = "AGENT_REPORTED_FAILURE"  # 移植保留的易标常量

# TERMINATION_OPTIONS 三项（移植自 piTaskFailureTool.cjs 顶部常量，逐字照抄）
CONTINUE_OPTION = {
    "label": "降低质量继续",
    "description": "放宽质量要求，只保证流程能完成，结果需要人工核对",
    "custom": False,
}
TERMINATE_OPTION = {
    "label": "终止任务",
    "description": "结束当前任务，可补充资料或调整设置后重新执行",
    "custom": False,
}
CUSTOM_OPTION = {
    "label": "补充处理要求",
    "description": "输入您希望 Agent 如何继续处理",
    "custom": True,
}
TERMINATION_OPTIONS = [CONTINUE_OPTION, TERMINATE_OPTION, CUSTOM_OPTION]


async def _maybe_await(value):
    """注入通道可能是普通函数也可能是协程函数，统一等待其结果。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _build_termination_question(reason: str) -> str:
    """终止确认问题（移植自 buildTerminationQuestion，逐字照抄）。"""
    return (
        f"Agent 认为当前任务无法按原要求继续：\n\n{reason}\n\n"
        "继续执行需要降低质量要求：数量、篇幅、专业性和资料依据都会放宽，"
        "内容可能不完整、与项目实际不符，需要您后续人工核对。是否终止当前任务？"
    )


def _build_continue_instruction(answer: dict) -> str:
    """用户选择继续后的放宽执行要求（移植自 buildContinueInstruction，逐字照抄）。"""
    lines = [
        "用户选择不终止任务，要求继续执行。",
        "- 质量要求已放宽：任务要求中的数量、篇幅、层级、专业性、资料依据和真实性等质量要求均可降低或放弃，不足部分可用通用内容、推测内容或占位内容补齐。",
        "- 唯一底线：按任务要求写出必需文件，结构和格式通过程序校验，使流程能够稳定进入下一步。",
        "- 不得以同一原因再次请求终止，立即调整做法继续执行。",
    ]
    if answer.get("is_custom"):
        lines.append(f"用户补充要求：{answer.get('answer')}")
        lines.append("该要求优先执行，与上述放宽规则冲突时以用户要求为准。")
    return "\n".join(lines)


_SIMPLE_DESCRIPTION = (
    "可自行修复的问题应先修复；需要用户决策时按本任务的提问规则处理。"
    "无法在当前任务允许的处理规则内完成要求时，报告面向普通用户的具体原因并立即结束任务。"
)

_SIMPLE_GUIDELINES = [
    "可自行修复的问题继续修复；需要用户选择有效方案时按本任务的提问规则处理，有 ask-user 工具时调用该工具。当无法在当前任务允许的处理规则内完成要求时，调用 report-failure 并说明原因。任务明确允许的补充设定、占位或概括性表达按对应规范执行；不得虚构工具执行结果、文件存在状态或任务完成情况。",
    "reason 必须直接面向普通用户，说明无法继续的具体业务原因和需要补充或调整的内容，不得使用文件名、字段名、JSON 属性或内部错误码。",
    "调用 report-failure 后任务会立即结束，不要再调用任何工具，也不得通过删除、清空或重命名输入、过程或输出文件表达失败。",
]

_DUAL_DESCRIPTION = (
    "只有判断当前任务无法按要求继续时调用。程序会向用户说明原因并确认是否终止："
    "用户确认后任务结束；用户选择继续时，工具会返回放宽后的执行要求，"
    "你必须按该要求继续完成任务。"
)

_DUAL_GUIDELINES = [
    "可自行修复的问题继续修复；需要用户在有效方案之间选择时按本任务的提问规则处理。只有判断当前任务无法按要求继续时才调用 report-failure，由用户决定是否终止；不得以其他方式自行结束或放弃任务，不得虚构工具执行结果、文件存在状态或任务完成情况。",
    "reason 必须直接面向普通用户，说明无法继续的具体业务原因，不得使用文件名、字段名、JSON 属性或内部错误码。",
    "用户确认终止后任务会立即结束，不要再调用任何工具；用户选择继续时，严格按工具返回的要求降低质量继续执行，不得以同一原因再次请求终止。",
    "不得通过删除、清空或重命名输入、过程或输出文件表达失败。",
]


def _build_simple_tool(report_task_failure) -> AgentTool:
    """简单形态（移植自 piTaskFailureTool.cjs 中 requestTerminationDecision 缺省分支）。"""

    async def _handler(args: dict) -> ToolCallOutcome:
        if not callable(report_task_failure):
            return ToolCallOutcome.error("任务失败报告通道未初始化")
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason:
            return ToolCallOutcome.error("reason 必须是非空字符串")
        try:
            await _maybe_await(report_task_failure(reason))
        except Exception as e:  # noqa: BLE001 —— fail-soft，回灌给模型
            return ToolCallOutcome.error(f"任务失败报告失败：{e}")
        # 报告失败即任务终点：置 stop 让 Agent 循环立即收束（P0 契约）。
        return ToolCallOutcome.json_result(
            {"reported": True, "reason": reason}, stop=True)

    return AgentTool(
        name=AGENT_TASK_FAILURE_TOOL_NAME,
        description=_SIMPLE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "面向普通用户的任务失败原因，并说明需要补充或调整的业务内容。",
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        handler=_handler,
        prompt_guidelines=list(_SIMPLE_GUIDELINES),
        sequential=True,
    )


def _build_dual_tool(report_task_failure, request_termination_decision) -> AgentTool:
    """双形态（移植自 piTaskFailureTool.cjs 的 requestTerminationDecision 分支）。

    ``continued`` 门禁：用户第一次选择继续后，再次请求终止时
    ``auto_answer=False``，必须由用户人工确认，避免自动作答反复放行死循环。
    """
    continued = False  # 闭包门禁状态，见模块 docstring

    async def _handler(args: dict, *, tool_call_id: str = "") -> ToolCallOutcome:
        nonlocal continued
        if not callable(report_task_failure):
            return ToolCallOutcome.error("任务失败报告通道未初始化")
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return ToolCallOutcome.error("reason 必须是非空字符串")
        try:
            answer = await _maybe_await(request_termination_decision({
                "tool_call_id": tool_call_id,
                "question": _build_termination_question(reason),
                "options": TERMINATION_OPTIONS,
                "auto_answer": not continued,
            }))
        except Exception as e:  # noqa: BLE001 —— fail-soft（含门禁拒绝）
            return ToolCallOutcome.error(f"终止决策请求失败：{e}")
        if not isinstance(answer, dict) or not answer.get("selected_option"):
            return ToolCallOutcome.error("终止决策通道必须返回带 selected_option 的 dict")
        if answer["selected_option"] == TERMINATE_OPTION["label"]:
            try:
                await _maybe_await(
                    report_task_failure(f"{reason}（已按您的选择终止任务）"))
            except Exception as e:  # noqa: BLE001
                return ToolCallOutcome.error(f"任务失败报告失败：{e}")
            return ToolCallOutcome.json_result(
                {"terminated": True, "reason": reason}, stop=True)
        # 用户选择继续：放宽要求继续执行，并落 continued 门禁。
        continued = True
        payload = {"terminated": False, "selected_option": answer["selected_option"]}
        if answer.get("is_custom"):
            payload["user_reply"] = answer.get("answer")
        return ToolCallOutcome.text(_build_continue_instruction(answer), payload=payload)

    return AgentTool(
        name=AGENT_TASK_FAILURE_TOOL_NAME,
        description=_DUAL_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "面向普通用户的无法继续原因，会展示给用户用于决定是否终止任务。",
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        handler=_handler,
        prompt_guidelines=list(_DUAL_GUIDELINES),
        sequential=True,
    )


def build_report_failure_tool(report_task_failure,
                              request_termination_decision=None) -> AgentTool:
    """report-failure 工具（移植自 piTaskFailureTool.cjs，双形态）。

    Args:
        report_task_failure: 上层注入的失败上报通道
            ``(reason: str) -> None | Awaitable[None]``。
        request_termination_decision: 可选的终止决策通道
            ``({"tool_call_id", "question", "options", "auto_answer"}, ...) -> dict | Awaitable``；
            缺省时构造简单形态，提供时构造带 ``continued`` 门禁的双形态。
    """
    if not callable(request_termination_decision):
        return _build_simple_tool(report_task_failure)
    return _build_dual_tool(report_task_failure, request_termination_decision)


__all__ = [
    "AGENT_REPORTED_FAILURE_CODE",
    "AGENT_TASK_FAILURE_TOOL_NAME",
    "CONTINUE_OPTION",
    "CUSTOM_OPTION",
    "TERMINATE_OPTION",
    "TERMINATION_OPTIONS",
    "build_report_failure_tool",
]
