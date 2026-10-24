"""P1 — ask-user 工具（1:1 移植自 piUserQuestionTool.cjs）

移植来源：易标 Yibiao 2.30.20 的 ``electron/services/pi/piUserQuestionTool.cjs``。

- 工具名、description、参数契约（question + options[]{label, description?,
  custom}，2–5 项、首项不能 custom、custom 最多 1 个）与三条运行时校验的
  错误文案逐字照抄；``promptGuidelines`` 8 条原文照抄。
- 执行时调用上层注入的 ``request_user_question``（本仓由上层实现挂起问答），
  请求体逐字段对齐易标（``tool_call_id`` / ``question`` / ``options``），
  返回 ``{"answered": true, ...answer}`` 的 JSON 文本。
- ``tool_call_id`` 由 ``agent_loop._dispatch`` 按 handler 形参自动回传
  （对应易标 ``execute(toolCallId, params, signal)``），前端据此按调用关联回答；
  注入通道支持同步/协程两种返回（``_maybe_await`` 统一等待）。
"""
from __future__ import annotations

import inspect

from app.services.agent.agent_loop import AgentTool, ToolCallOutcome

AGENT_USER_QUESTION_TOOL_NAME = "ask-user"

#: 8 条 promptGuidelines 原文照抄（piUserQuestionTool.cjs promptGuidelines）。
_PROMPT_GUIDELINES = [
    "已有材料足以判断时自主执行，不要调用 ask-user。",
    "只有不确定事项会实质影响结果时才调用 ask-user；每次只问一个问题，提供 2 至 5 个互斥选项，并将无需用户补充输入的推荐选项放在第一项。每个选项都必须填写 custom。",
    "问题、选项名称和选项说明必须面向不懂软件开发的普通用户，只说明业务对象、推荐结果、实际影响和可选操作，语言简单、自然且容易决策。",
    "禁止向用户展示变量名、字段名、文件名、JSON 属性、英文状态、内部编号或其他软件实现术语；必须把内部信息转换为具体名称、数量、层级和调整效果等业务表述。",
    "问题、选项名称和选项说明不得复述、概括或改写当前任务 Prompt 中的要求，只呈现分析后确实需要用户确认的结论或不确定事项。",
    "存在多个实际确认事项时，使用简单 Markdown 按“简短说明、空行、分行要点、空行、最后确认问题”组织；每个要点只表达一件事，不要增加小标题或输出连续长段落。",
    "选项名称使用“按推荐方案处理”“保持当前结果”等可直接判断结果的简短操作文案，不得描述程序处理步骤或数据结构。",
    "普通选项设置 custom=false；只有需要用户选中后输入具体要求时才设置 custom=true。custom=true 的选项最多只能有一个，通常放在最后，且必须使用“补充具体要求”等明确操作名称，不得使用含义模糊的“其他”。",
]


async def _maybe_await(value):
    """注入通道可能是普通函数也可能是协程函数，统一等待其结果。"""
    if inspect.isawaitable(value):
        return await value
    return value


def build_ask_user_tool(request_user_question) -> AgentTool:
    """ask-user 工具（移植自 piUserQuestionTool.cjs）。

    Args:
        request_user_question: 上层注入的提问通道，
            ``({"tool_call_id", "question", "options"}, ...) -> dict | Awaitable[dict]``，
            负责挂起执行并向用户提问。
    """
    # 易标 execute 里的两条校验文案（逐字照抄），另有 schema 级 2–5 项约束
    # 在本仓由运行时校验兜底（agent_loop 不做 JSON Schema 校验）。
    async def _handler(args: dict, *, tool_call_id: str = "") -> ToolCallOutcome:
        if not callable(request_user_question):
            return ToolCallOutcome.error("用户提问通道未初始化")
        question = args.get("question")
        if not isinstance(question, str) or not question:
            return ToolCallOutcome.error("question 必须是非空字符串")
        options = args.get("options")
        if not isinstance(options, list):
            return ToolCallOutcome.error("options 必须是数组")
        if not 2 <= len(options) <= 5:
            return ToolCallOutcome.error(
                "options 必须提供 2 至 5 个互斥选项，请调整选项后重新提问")
        for i, option in enumerate(options):
            if (not isinstance(option, dict)
                    or not isinstance(option.get("label"), str)
                    or not option.get("label")
                    or not isinstance(option.get("custom"), bool)):
                return ToolCallOutcome.error(
                    f"选项格式错误：options[{i}] 必须包含非空字符串 label 与"
                    f" 布尔值 custom")
        custom_option_count = sum(
            1 for option in options if option.get("custom") is True)
        if custom_option_count > 1:
            return ToolCallOutcome.error(
                "自定义输入选项最多只能有一个，请调整选项后重新提问")
        if options[0].get("custom") is True:
            return ToolCallOutcome.error(
                "推荐选项不能要求用户补充输入，请调整选项后重新提问")
        try:
            answer = await _maybe_await(request_user_question({
                "tool_call_id": tool_call_id,
                "question": question,
                "options": options,
            }))
        except Exception as e:  # noqa: BLE001 —— fail-soft，回灌给模型
            return ToolCallOutcome.error(f"用户提问失败：{e}")
        if not isinstance(answer, dict):
            return ToolCallOutcome.error("用户提问通道必须返回 dict 形式的回答")
        result = {"answered": True, **answer}
        return ToolCallOutcome.json_result(result)

    return AgentTool(
        name=AGENT_USER_QUESTION_TOOL_NAME,
        description=(
            "当任务材料无法确定且不同选择会实质影响结果时，暂停执行并向普通用户"
            "提出一个简单易懂的问题。提供 2 至 5 个互斥选项，将推荐选项放在第一项；"
            "每个选项必须声明是否需要用户输入具体要求，且最多只能有一个自定义输入选项。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "description": "需要普通用户确认的具体业务问题，必须使用简单自然的中文，"
                                   "不得包含任何软件实现术语或复述任务要求；存在多个实际确认"
                                   "事项时使用简单 Markdown 分行列出要点。",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "description": "候选选项，第一项必须是 custom=false 的推荐选项；"
                                   "自定义输入选项计入总数。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "minLength": 1,
                                "description": "简短、明确且可直接判断结果的操作名称，"
                                               "不得包含内部字段或技术标识。",
                            },
                            "description": {
                                "type": "string",
                                "description": "使用普通用户能理解的语言说明该选项的业务结果或影响。",
                            },
                            "custom": {
                                "type": "boolean",
                                "description": "是否需要用户在选中后输入具体要求。普通选项为 "
                                               "false，自定义输入选项为 true；一次提问最多只能有"
                                               "一个 true。",
                            },
                        },
                        "required": ["label", "custom"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["question", "options"],
            "additionalProperties": False,
        },
        handler=_handler,
        prompt_guidelines=list(_PROMPT_GUIDELINES),
        # 易标 executionMode:'sequential'
        sequential=True,
    )


__all__ = [
    "AGENT_USER_QUESTION_TOOL_NAME",
    "build_ask_user_tool",
]
