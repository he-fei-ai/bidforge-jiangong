"""P0 — Agent 工具调用循环（易标 piRuntimeService 双层 while 移植）。

覆盖：
1. ``chat()`` 无 ``tools`` 时的既有契约（空响应守卫）不回归；
2. ``chat()`` 带 ``tools`` 时返回 assistant 消息 dict，且 content 为 null 不抛错；
3. ``agent_loop`` 的轮数上限、stop 收束、未知工具、工具异常 fail-soft。
"""
import json

import pytest
from app.services.agent.agent_loop import (
    STOP_COMPLETED,
    STOP_TOOL_STOP,
    STOP_TURN_LIMIT,
    AgentTool,
    ToolCallOutcome,
    normalize_assistant_message,
    run_agent_loop,
)


# --------------------------------------------------------------------------
# Provider 层打桩
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, resp: _FakeResp):
        self.resp = resp
        self.payloads: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(dict(json or {}))
        return self.resp


def _provider(monkeypatch, payload: dict, status: int = 200):
    from app.services.ai.providers import openai_compatible as oc
    client = _FakeClient(_FakeResp(status, payload))
    monkeypatch.setattr(oc, "get_async_client", lambda *a, **k: client)
    return oc.OpenAICompatibleProvider("sk-x", "https://api.test.com/v1", "m",
                                       max_tokens=100, temperature=0.7), client


def _tool_call(fn_name: str, args: dict | str, call_id: str = "call_1") -> dict:
    raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"id": call_id, "type": "function",
            "function": {"name": fn_name, "arguments": raw}}


# --------------------------------------------------------------------------
# 1) chat() 既有契约不回归
# --------------------------------------------------------------------------
class TestChatWithoutTools:
    async def test_plain_text_returns_str(self, monkeypatch):
        provider, _ = _provider(monkeypatch, {
            "choices": [{"message": {"role": "assistant", "content": "你好"},
                         "finish_reason": "stop"}]})
        out = await provider.chat([{"role": "user", "content": "hi"}])
        assert out == "你好"
        assert isinstance(out, str)

    async def test_empty_content_still_raises(self, monkeypatch):
        """tools=None 时的空响应守卫必须保留（否则上层把空串当成功）。"""
        provider, _ = _provider(monkeypatch, {
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "stop"}]})
        with pytest.raises(RuntimeError):
            await provider.chat([{"role": "user", "content": "hi"}])

    async def test_payload_has_no_tools_key(self, monkeypatch):
        provider, client = _provider(monkeypatch, {
            "choices": [{"message": {"content": "ok"}}]})
        await provider.chat([{"role": "user", "content": "hi"}])
        assert "tools" not in client.payloads[0]


# --------------------------------------------------------------------------
# 2) chat() 工具模式
# --------------------------------------------------------------------------
class TestChatWithTools:
    async def test_returns_message_dict_with_tool_calls(self, monkeypatch):
        calls = [_tool_call("read", {"file_path": "a.json"})]
        provider, client = _provider(monkeypatch, {
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": calls},
                         "finish_reason": "tool_calls"}]})
        out = await provider.chat([{"role": "user", "content": "hi"}],
                                  tools=[{"type": "function", "function": {
                                      "name": "read", "description": "d",
                                      "parameters": {"type": "object"}}}])
        assert isinstance(out, dict)
        assert out["content"] is None          # 只发工具调用 = 合法形态，未抛错
        assert out["tool_calls"] == calls
        assert client.payloads[0]["tools"][0]["function"]["name"] == "read"
        assert "response_format" not in client.payloads[0]

    async def test_tool_choice_forwarded(self, monkeypatch):
        provider, client = _provider(monkeypatch, {
            "choices": [{"message": {"content": "ok"}}]})
        await provider.chat([{"role": "user", "content": "hi"}],
                            tools=[{"type": "function", "function": {
                                "name": "x", "description": "d",
                                "parameters": {"type": "object"}}}],
                            tool_choice="auto")
        assert client.payloads[0]["tool_choice"] == "auto"

    async def test_missing_tool_calls_normalized_to_empty_list(self, monkeypatch):
        provider, _ = _provider(monkeypatch, {
            "choices": [{"message": {"role": "assistant", "content": "纯文本"}}]})
        out = await provider.chat([{"role": "user", "content": "hi"}],
                                  tools=[{"type": "function", "function": {
                                      "name": "x", "description": "d",
                                      "parameters": {"type": "object"}}}])
        assert out["tool_calls"] == []


def test_supports_tools_flags():
    from app.services.ai.providers.anthropic_compatible import AnthropicCompatibleProvider
    from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider
    a = AnthropicCompatibleProvider("k", "https://api.test.com/v1", "m")
    o = OpenAICompatibleProvider("k", "https://api.test.com/v1", "m")
    assert o.supports_tools() is True
    assert a.supports_tools() is False


async def test_anthropic_rejects_tools_loudly():
    """未实现工具协议的 Provider 必须显式拒绝，不能被 **_kwargs 静默吞掉。"""
    from app.services.ai.providers.anthropic_compatible import AnthropicCompatibleProvider
    p = AnthropicCompatibleProvider("k", "https://api.test.com/v1", "m")
    with pytest.raises(ValueError):
        await p.chat([{"role": "user", "content": "hi"}],
                     tools=[{"type": "function", "function": {
                         "name": "x", "description": "d",
                         "parameters": {"type": "object"}}}])


# --------------------------------------------------------------------------
# 3) agent_loop
# --------------------------------------------------------------------------
async def _echo_tool(args: dict) -> ToolCallOutcome:
    return ToolCallOutcome.json_result({"echo": args})


def _make_tools() -> list[AgentTool]:
    return [
        AgentTool(name="echo", description="回显参数",
                  parameters={"type": "object", "properties": {}},
                  handler=_echo_tool),
    ]


class TestAgentLoopBasics:
    async def test_executes_tool_and_completes(self):
        seen: list[list] = []

        async def chat(messages, *, tools, tool_choice=None, **kw):
            seen.append(list(messages))
            if len(seen) == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call("echo", {"a": 1})]}
            return {"role": "assistant", "content": "完成", "tool_calls": []}

        msgs = [{"role": "user", "content": "做点事"}]
        result = await run_agent_loop(msgs, tools=_make_tools(), chat_fn=chat)
        assert result.stop_reason == STOP_COMPLETED
        assert result.turns == 2
        assert result.text == "完成"
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[2]["tool_call_id"] == "call_1"
        assert json.loads(msgs[2]["content"]) == {"echo": {"a": 1}}

    async def test_string_response_from_chat_is_normalized(self):
        async def chat(messages, *, tools, tool_choice=None, **kw):
            return "直接文本回复"

        result = await run_agent_loop([{"role": "user", "content": "x"}],
                                      tools=_make_tools(), chat_fn=chat)
        assert result.stop_reason == STOP_COMPLETED
        assert result.text == "直接文本回复"

    async def test_requires_at_least_one_tool(self):
        async def chat(messages, *, tools, tool_choice=None, **kw):  # pragma: no cover
            return "x"

        with pytest.raises(ValueError):
            await run_agent_loop([{"role": "user", "content": "x"}],
                                 tools=[], chat_fn=chat)

    async def test_handler_gets_tool_call_id_when_declared(self):
        """易标 ``execute(toolCallId, params)`` 语义：ask-user / report-failure
        这类要与前端按调用关联回答的工具，必须拿到 tool_call_id；未声明
        形参的纯本地工具（如 ``_echo_tool``）仍只收 args，签名不受影响。"""
        seen: dict[str, str] = {}

        async def ask(args: dict, *, tool_call_id: str = "") -> ToolCallOutcome:
            seen["id"] = tool_call_id
            return ToolCallOutcome.json_result({"answered": True})

        tools = [AgentTool(name="ask-user", description="提问",
                           parameters={"type": "object", "properties": {}},
                           handler=ask)]
        calls = {"n": 0}

        async def chat(messages, *, tools, tool_choice=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call("ask-user", {}, "call_42")]}
            return {"role": "assistant", "content": "ok", "tool_calls": []}

        result = await run_agent_loop([{"role": "user", "content": "x"}],
                                      tools=tools, chat_fn=chat)
        assert result.stop_reason == STOP_COMPLETED
        assert seen["id"] == "call_42"


class TestAgentLoopGuards:
    async def test_unknown_tool_returns_error_message(self):
        async def chat(messages, *, tools, tool_choice=None, **kw):
            if len(messages) == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call("nope", {})]}
            return {"role": "assistant", "content": "收到错误", "tool_calls": []}

        msgs = [{"role": "user", "content": "x"}]
        await run_agent_loop(msgs, tools=_make_tools(), chat_fn=chat)
        assert msgs[2]["role"] == "tool"
        assert msgs[2].get("is_error") is True
        assert "未知工具" in msgs[2]["content"]

    async def test_handler_exception_is_fail_soft(self):
        async def boom(args):
            raise RuntimeError("磁盘炸了")

        tools = [AgentTool(name="boom", description="d",
                           parameters={"type": "object"}, handler=boom)]

        async def chat(messages, *, tools, tool_choice=None, **kw):
            if len(messages) == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call("boom", {})]}
            return {"role": "assistant", "content": "已知悉", "tool_calls": []}

        msgs = [{"role": "user", "content": "x"}]
        result = await run_agent_loop(msgs, tools=tools, chat_fn=chat)
        assert result.stop_reason == STOP_COMPLETED
        assert msgs[2].get("is_error") is True
        assert "磁盘炸了" in msgs[2]["content"]

    async def test_invalid_arguments_json_is_fail_soft(self):
        async def chat(messages, *, tools, tool_choice=None, **kw):
            if len(messages) == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call("echo", "{坏 JSON")]}
            return {"role": "assistant", "content": "好", "tool_calls": []}

        msgs = [{"role": "user", "content": "x"}]
        await run_agent_loop(msgs, tools=_make_tools(), chat_fn=chat)
        assert msgs[2].get("is_error") is True
        assert "arguments 不是合法 JSON" in msgs[2]["content"]

    async def test_turn_limit_stops_loop(self):
        async def chat(messages, *, tools, tool_choice=None, **kw):
            return {"role": "assistant", "content": None,
                    "tool_calls": [_tool_call("echo", {})]}

        result = await run_agent_loop([{"role": "user", "content": "x"}],
                                      tools=_make_tools(), chat_fn=chat,
                                      max_turns=3)
        # 第 4 次进入循环时收束，实际执行 3 轮
        assert result.stop_reason == STOP_TURN_LIMIT
        assert result.turns == 3

    async def test_stop_tool_ends_loop_and_records_payload(self):
        async def stopper(args):
            return ToolCallOutcome.json_result({"reported": True, "reason": "没资料"},
                                               stop=True)

        tools = [AgentTool(name="report-failure", description="d",
                           parameters={"type": "object"}, handler=stopper)]

        async def chat(messages, *, tools, tool_choice=None, **kw):
            # 首轮下发 stop 工具调用；stop 收束后绝不允许再进模型
            if len(messages) == 1:
                return {"role": "assistant", "content": None,
                        "tool_calls": [_tool_call(
                            "report-failure", {"reason": "没资料"})]}
            raise AssertionError("stop 之后不应再调用模型")

        msgs = [{"role": "user", "content": "x"}]
        result = await run_agent_loop(msgs, tools=tools, chat_fn=chat)
        assert result.stop_reason == STOP_TOOL_STOP
        assert result.stopped_by_tool is True
        assert result.tool_payloads[0]["tool"] == "report-failure"
        assert result.tool_payloads[0]["payload"]["reason"] == "没资料"


class TestNormalizeAssistantMessage:
    def test_str(self):
        assert normalize_assistant_message("文本")["tool_calls"] == []

    def test_none_content_without_calls_becomes_empty_str(self):
        msg = normalize_assistant_message({"role": "assistant", "content": None})
        assert msg["content"] == ""

    def test_dict_passthrough(self):
        msg = normalize_assistant_message({"role": "assistant", "content": "a",
                                           "tool_calls": []})
        assert msg["content"] == "a"
