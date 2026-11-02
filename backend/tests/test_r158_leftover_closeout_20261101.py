"""R158 遗留收口护栏（2026-11-01）

本轮裁定（承接 R156 记录的设计要点 + R150 遗留，先行核对现状后执行）：
  A FIX-1 · prompt_guidelines 接线（R156-L9-D6「未完成的移植」→ 完成移植）：
    ``run_agent_loop`` 建循环前收集全部工具的非空守则 → 去重保序 → 拼装
    「工具调用守则」system 消息注入（幂等、不改动调用方既有消息）。
    此前 8 处构造填真实文本、0 处消费（R156 确认）。
  C FIX-3 · 盘点工具分窗对比能力：``_r150_ai_inventory.py`` 新增 ``--cutoff``
    （改造前/后同口径分窗，供后端重启并积累真实用量后一键对比）。

B 组（前端 by_error_class / P95 消费）在 ``frontend/src/tests/aiStatsR158.test.tsx``。
"""
import inspect
import json

import pytest
from app.services.agent.agent_loop import (
    AgentTool,
    build_guidelines_system_message,
    inject_guidelines,
    run_agent_loop,
)


def _tool(name: str, guidelines: list[str] | None = None) -> AgentTool:
    async def _handler(args, tool_call_id=None):
        from app.services.agent.agent_loop import ToolCallOutcome
        return ToolCallOutcome.text("ok")
    return AgentTool(name=name, description="d", parameters={"type": "object"},
                     handler=_handler, prompt_guidelines=list(guidelines or []))


async def _noop_chat(messages, *, tools=None, tool_choice=None, **kw):
    # 立即收束：返回无 tool_calls 的 assistant 消息
    return {"role": "assistant", "content": "done", "tool_calls": []}


# ============================================================
# A · FIX-1 prompt_guidelines 接线
# ============================================================
class TestPromptGuidelinesWiring:
    def test_collect_dedupe_preserves_order(self):
        tools = [
            _tool("a", ["守则一", "守则二"]),
            _tool("b", ["守则二", "  守则三  "]),   # 重复 + 带空白
            _tool("c", ["", "   "]),                # 全空白忽略
            _tool("d", None),                       # 未声明
        ]
        block = build_guidelines_system_message(tools)
        assert block is not None
        assert block.startswith("【工具调用守则】")
        lines = [ln for ln in block.splitlines()[1:]]
        assert lines == ["- 守则一", "- 守则二", "- 守则三"]

    def test_empty_guidelines_returns_none(self):
        assert build_guidelines_system_message([_tool("a"), _tool("b", [])]) is None
        assert build_guidelines_system_message([]) is None

    def test_inject_no_system_message_inserts_at_zero(self):
        msgs = [{"role": "user", "content": "任务"}]
        assert inject_guidelines(msgs, [_tool("a", ["守则甲"])]) is True
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "守则甲" in msgs[0]["content"]
        assert msgs[1] == {"role": "user", "content": "任务"}

    def test_inject_after_existing_system_message(self):
        """已有 system 消息：插在其后，且**不改动**调用方既有消息内容。"""
        sys_msg = {"role": "system", "content": "业务系统提示"}
        user_msg = {"role": "user", "content": "任务"}
        msgs = [sys_msg, user_msg]
        assert inject_guidelines(msgs, [_tool("a", ["守则乙"])]) is True
        assert [m["role"] for m in msgs] == ["system", "system", "user"]
        assert msgs[0]["content"] == "业务系统提示"     # 原内容逐字不变
        assert "守则乙" in msgs[1]["content"]

    def test_inject_idempotent_on_retry(self):
        """调用方复用同一 messages 重试：块已在场 → 不重复注入。"""
        msgs = [{"role": "user", "content": "任务"}]
        tools = [_tool("a", ["守则丙"])]
        assert inject_guidelines(msgs, tools) is True
        snapshot = json.dumps(msgs, ensure_ascii=False)
        assert inject_guidelines(msgs, tools) is False
        assert json.dumps(msgs, ensure_ascii=False) == snapshot

    def test_no_guidelines_no_injection(self):
        """全空守则：零行为变化（messages 逐字节不变）。"""
        msgs = [{"role": "user", "content": "任务"}]
        snapshot = json.dumps(msgs, ensure_ascii=False)
        assert inject_guidelines(msgs, [_tool("a")]) is False
        assert json.dumps(msgs, ensure_ascii=False) == snapshot

    async def test_run_agent_loop_injects_and_chat_sees_it(self):
        """端到端（agent 路径 · R153 打桩纪律）：chat_fn 收到的首条消息即守则。"""
        seen: list[list[dict]] = []

        async def chat(messages, *, tools=None, tool_choice=None, **kw):
            seen.append([dict(m) for m in messages])
            return {"role": "assistant", "content": "done", "tool_calls": []}

        result = await run_agent_loop(
            [{"role": "user", "content": "做任务"}],
            tools=[_tool("read", ["用 read 而不是 cat"]),
                   _tool("write", ["用 read 而不是 cat", "write 只用于新文件"])],
            chat_fn=chat)
        assert result.stop_reason == "completed"
        assert len(seen) == 1
        first = seen[0][0]
        assert first["role"] == "system"
        assert "【工具调用守则】" in first["content"]
        assert "- 用 read 而不是 cat" in first["content"]
        assert "- write 只用于新文件" in first["content"]
        # 业务消息原样保留在守则之后
        assert seen[0][1] == {"role": "user", "content": "做任务"}

    async def test_run_agent_loop_without_guidelines_unchanged(self):
        """无守则工具：chat_fn 收到的 messages 与传入逐字一致。"""
        seen: list[list[dict]] = []

        async def chat(messages, *, tools=None, tool_choice=None, **kw):
            seen.append([dict(m) for m in messages])
            return {"role": "assistant", "content": "done", "tool_calls": []}

        original = [{"role": "user", "content": "做任务"}]
        await run_agent_loop(list(original), tools=[_tool("echo")], chat_fn=chat)
        assert seen[0] == original

    def test_source_anchor_definition_call_form(self):
        """源码锚断「定义处调用形态」（R144 教训② / R149 教训③）。"""
        src = inspect.getsource(run_agent_loop)
        assert "inject_guidelines(messages, tools)" in src
        mod_src = inspect.getsource(
            __import__("app.services.agent.agent_loop", fromlist=["x"]))
        assert "def build_guidelines_system_message(" in mod_src
        assert "def inject_guidelines(" in mod_src
        assert '_GUIDELINES_MARKER = "【工具调用守则】"' in mod_src


# ============================================================
# C · FIX-3 盘点工具分窗对比能力
# ============================================================
class TestInventoryCutoff:
    def test_inventory_script_has_cutoff_mode(self):
        src = open("_r150_ai_inventory.py", encoding="utf-8").read()
        assert "--cutoff" in src
        # 分窗对比必须复用同一 build_view（同口径），且默认行为不变
        assert "def build_view(" in src
        assert "def load_rows(" in src

    def test_inventory_cutoff_split_is_consistent(self, tmp_path):
        """--cutoff 分窗：before ∪ after = 全量（同口径切分）。"""
        import sqlite3
        import sys
        sys.path.insert(0, ".")
        import _r150_ai_inventory as inv
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ai_audit_logs (provider_name TEXT, model TEXT,"
            " action TEXT, prompt_tokens INT, completion_tokens INT,"
            " cached_tokens INT, duration REAL, success INT, created_at TEXT,"
            " error TEXT, scene TEXT, config_id TEXT, base_url TEXT)")
        rows = [
            ("p1", "m1", "chat", 10, 5, 0, 1.0, 1, "2026-10-30 10:00:00", "", "s"),
            ("p1", "m1", "chat", 10, 5, 0, 2.0, 0, "2026-11-01 09:00:00",
             "HTTP 429: rl", "s"),
        ]
        conn.executemany("INSERT INTO ai_audit_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(*r, "", "") for r in rows])
        conn.commit()
        before = inv.build_view(inv.load_rows_from(conn, "2026-10-31 00:00:00", before=True))
        after = inv.build_view(inv.load_rows_from(conn, "2026-10-31 00:00:00", before=False))
        assert before["chat_calls"] == 1 and after["chat_calls"] == 1
        assert after["errors_by_class"].get("rate_limit") == 1
        conn.close()
