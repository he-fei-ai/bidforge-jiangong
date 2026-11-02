"""R135（2026-10-26）正文 Agent 分层修复护栏。

引入依据：《正文生成工具调用与修正机制引入_实施报告》（易标
Yibiao-2.30.20 `contentGenerationAgent/ConsistencyTools/EditTools` +
`agentToolEnvironment` + `piWorkspaceBaseline` 的本仓落地）。

覆盖组：
- A 工作区标记与解析回读（materialize/parse 往返 + 各类坏形态拒绝）；
- B 结构校验（变化识别 / 标记不合法 / 新增与删除文件 / 修改范围）；
- C 全局 FIFO 队列（串行 / FIFO / 优先级 / 暂停恢复 / 取消 / 超时 / 异常回传 / 队列满）;
- D 工具（search-sections 定位 + 工具调用审计含失败不吞）；
- E 端到端修复（fake chat：读任务说明 → 写回修复小节 → DB 只回写变化小节
  + 字数口径 + resync + 导出缓存失效）；
- F 失败路径（标记破坏 / Agent 异常 / 输入文件被改动被基线还原 → 全部不落库）；
- G 开关关闭 → skipped 且零副作用；
- H repair_agent 分层回落钩子（persist=False 交回内容 / 未产出返回 None）；
- I 新文件字节体检（BOM / U+FFFD / 孤立 CR / 顶层重名）。

全部用例零真实 AI：chat_fn / resync_fn / DB 均可注入或打桩。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.content_agent import (  # noqa: E402
    FACTS_FILE,
    SECTION_DIR,
    TASK_FILE,
    AgentQueueFull,
    materialize_workspace,
    parse_section_content,
    reset_global_agent_queue,
    section_markers,
    validate_workspace_sections,
)
from app.services.content_agent.queue import GlobalAgentQueue  # noqa: E402
from app.services.content_agent.repair import (  # noqa: E402
    AgentRepairResult,
    SectionTarget,
    run_agent_section_repair,
)
from app.services.content_agent.tools import (  # noqa: E402
    build_search_sections_tool,
    with_tool_audit,
)
from app.services.content_agent.workspace import (  # noqa: E402
    render_section_file,
)

SEC_A = "aaaaaaaa-1111-2222-3333-444444444444"
SEC_B = "bbbbbbbb-1111-2222-3333-444444444444"
ORIG_A = "基坑开挖深度为 5.5m，采用放坡开挖。"
ORIG_B = "工期 120 日历天。"
FIXED_A = "基坑开挖深度为 5.6m，采用放坡开挖。"

# asyncio_mode=auto：async 用例自动跑事件循环，同步用例不受影响，
# 不设模块级 pytestmark（否则同步用例会收到 PytestWarning）。


def _sections():
    return [
        {"section_id": SEC_A, "title": "土方开挖", "content": ORIG_A},
        {"section_id": SEC_B, "title": "工期安排", "content": ORIG_B},
    ]


# ---------------------------------------------------------------------------
# A 工作区标记与解析回读
# ---------------------------------------------------------------------------
class TestWorkspaceMarkers:
    def test_roundtrip_preserves_content_bytes(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections(),
                              facts_text="- 基坑深度 5.6m\n",
                              task_instructions="# 任务\n修冲突")
        raw = (tmp_path / SECTION_DIR / f"{SEC_A}.md").read_text(encoding="utf-8")
        assert parse_section_content(raw, SEC_A) == ORIG_A
        # 标记独占两行，标题在标记之外
        lines = raw.splitlines()
        begin, end = section_markers(SEC_A)
        assert lines[2] == begin and lines[-1] == end
        assert lines[0].startswith("# 土方开挖")
        assert lines[3] == ORIG_A
        assert (tmp_path / FACTS_FILE).exists()
        assert (tmp_path / TASK_FILE).exists()

    def test_roundtrip_with_code_fence_and_crlf(self, tmp_path):
        content = "```mermaid\nflowchart TD\nA --> B\n```\n正文含 ``` 围栏与\r\n回车。"
        materialize_workspace(tmp_path, sections=[
            {"section_id": SEC_A, "title": "t", "content": content}])
        raw = (tmp_path / SECTION_DIR / f"{SEC_A}.md").read_text(encoding="utf-8")
        # parse 回读按行归一 \r\n（与物化一致），语义等价比较
        assert parse_section_content(raw, SEC_A) == content.replace("\r\n", "\n")

    def test_rejects_missing_end_marker(self):
        begin, end = section_markers(SEC_A)
        text = f"{begin}\n正文没有结束标记\n"
        assert parse_section_content(text, SEC_A) is None

    def test_rejects_duplicate_begin(self):
        begin, end = section_markers(SEC_A)
        text = f"{begin}\n{begin}\n正文\n{end}\n"
        assert parse_section_content(text, SEC_A) is None

    def test_rejects_reversed_order(self):
        begin, end = section_markers(SEC_A)
        text = f"{end}\n正文\n{begin}\n"
        assert parse_section_content(text, SEC_A) is None

    def test_rejects_marker_glued_into_paragraph(self):
        begin, end = section_markers(SEC_A)
        text = f"开头{begin}\n正文\n{end}\n"
        assert parse_section_content(text, SEC_A) is None

    def test_invalid_section_id_rejected(self):
        with pytest.raises(ValueError):
            __import__("app.services.content_agent.workspace", fromlist=["x"]) \
                .section_relpath("../escape")

# ---------------------------------------------------------------------------
# B 结构校验
# ---------------------------------------------------------------------------
class TestValidation:
    def test_changed_section_is_writable(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        (tmp_path / SECTION_DIR / f"{SEC_A}.md").write_text(
            render_section_file("土方开挖", FIXED_A, SEC_A), encoding="utf-8")
        results, problems = validate_workspace_sections(tmp_path, _sections())
        assert problems == []
        by_id = {v.section_id: v for v in results}
        assert by_id[SEC_A].status == "ok" and by_id[SEC_A].writable
        assert by_id[SEC_B].status == "unchanged" and not by_id[SEC_B].writable

    def test_broken_marker_is_invalid(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        (tmp_path / SECTION_DIR / f"{SEC_A}.md").write_text(
            "标记被我删了\n", encoding="utf-8")
        results, _ = validate_workspace_sections(tmp_path, _sections())
        by_id = {v.section_id: v for v in results}
        assert by_id[SEC_A].status == "invalid"
        assert any("标记" in p for p in by_id[SEC_A].problems)

    def test_empty_content_is_invalid(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        (tmp_path / SECTION_DIR / f"{SEC_A}.md").write_text(
            render_section_file("t", "", SEC_A), encoding="utf-8")
        results, _ = validate_workspace_sections(tmp_path, _sections())
        assert results[0].status == "invalid"

    def test_new_and_missing_files_are_global_problems(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        (tmp_path / SECTION_DIR / "extra.md").write_text("新增章节", encoding="utf-8")
        (tmp_path / SECTION_DIR / f"{SEC_B}.md").unlink()
        results, problems = validate_workspace_sections(tmp_path, _sections())
        assert any("新增" in p for p in problems)
        assert any("缺失" in p for p in problems)
        assert all(v.status == "invalid" for v in results)


# ---------------------------------------------------------------------------
# C 全局 FIFO 队列
# ---------------------------------------------------------------------------
class TestGlobalQueue:
    async def test_fifo_order_and_results(self):
        q = GlobalAgentQueue()
        order = []
        results = await asyncio.gather(*[
            q.submit(lambda v=i: _record(order, v)) for i in range(5)])
        assert order == [0, 1, 2, 3, 4]
        assert results == [0, 1, 2, 3, 4]

    async def test_serial_execution_no_overlap(self):
        q = GlobalAgentQueue()
        spans = []

        async def job(i):
            start = time.monotonic()
            await asyncio.sleep(0.03)
            spans.append((start, time.monotonic(), i))

        await asyncio.gather(*[q.submit(lambda i=i: job(i)) for i in range(4)])
        spans.sort()
        for (s1, e1, _), (s2, _, __) in zip(spans, spans[1:]):
            assert s2 >= e1 - 1e-6, "队列必须串行：下一任务不得在上一任务结束前开始"

    async def test_priority_beats_fifo(self):
        q = GlobalAgentQueue()
        q.pause()
        order = []
        low = asyncio.create_task(q.submit(lambda: _record(order, "low")))
        await asyncio.sleep(0)
        high = asyncio.create_task(q.submit(lambda: _record(order, "high"),
                                            priority=10))
        await asyncio.sleep(0)
        q.resume()
        await asyncio.gather(low, high)
        assert order == ["high", "low"]

    async def test_pause_blocks_and_resume_releases(self):
        q = GlobalAgentQueue()
        q.pause()
        order = []
        task = asyncio.create_task(q.submit(lambda: _record(order, 1)))
        await asyncio.sleep(0.05)
        assert order == [], "暂停期间不得出队"
        q.resume()
        await asyncio.wait_for(task, 2)
        assert order == [1]

    async def test_cancel_pending(self):
        q = GlobalAgentQueue()
        q.pause()
        ran = []
        task = asyncio.create_task(q.submit(lambda: _record(ran, 1),
                                            task_id="t-cancel"))
        await asyncio.sleep(0)
        assert q.cancel_pending("t-cancel") == 1
        q.resume()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran == []

    async def test_timeout_does_not_kill_worker(self):
        q = GlobalAgentQueue()

        async def slow():
            await asyncio.sleep(0.5)
            return "slow-done"

        with pytest.raises(asyncio.TimeoutError):
            await q.submit(slow, timeout=0.05)
        # worker 仍可用
        assert await q.submit(lambda: _ret("ok")) == "ok"
        await q.wait_idle()

    async def test_exception_propagates_not_swallowed(self):
        q = GlobalAgentQueue()

        async def boom():
            raise RuntimeError("任务内部故障")

        with pytest.raises(RuntimeError, match="任务内部故障"):
            await q.submit(boom)
        assert await q.submit(lambda: _ret("alive")) == "alive"

    async def test_queue_full_rejects(self):
        q = GlobalAgentQueue(max_pending=1)
        release = asyncio.Event()

        async def blocker():
            await release.wait()

        first = asyncio.create_task(q.submit(blocker))
        await asyncio.sleep(0.02)
        with pytest.raises(AgentQueueFull):
            await q.submit(lambda: _ret("x"))
        release.set()
        await first
        assert await q.submit(lambda: _ret("after")) == "after"


async def _record(order: list, value):
    order.append(value)
    await asyncio.sleep(0)
    return value


async def _ret(value):
    await asyncio.sleep(0)
    return value



# ---------------------------------------------------------------------------
# D 工具：search-sections + 调用审计
# ---------------------------------------------------------------------------
class TestContentTools:
    async def test_search_sections_returns_section_and_line(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        tool = build_search_sections_tool(str(tmp_path), [SEC_A, SEC_B])
        outcome = await tool.handler({"query": "开挖深度"})
        assert not outcome.is_error
        payload = json.loads(outcome.content)
        assert payload["total"] == 1
        assert payload["matches"][0]["section_id"] == SEC_A
        assert "5.5m" in payload["matches"][0]["snippet"]
        # 检索结果不包含程序标记行
        empty = await tool.handler({"query": "SECTION:"})
        assert json.loads(empty.content)["total"] == 0

    async def test_search_sections_empty_query_is_error(self, tmp_path):
        tool = build_search_sections_tool(str(tmp_path), [SEC_A])
        outcome = await tool.handler({"query": "  "})
        assert outcome.is_error

    async def test_tool_audit_records_ok_and_error(self, tmp_path):
        materialize_workspace(tmp_path, sections=_sections())
        audit: list = []
        tool = build_search_sections_tool(str(tmp_path), [SEC_A, SEC_B])
        (tool,) = with_tool_audit([tool], audit)
        await tool.handler({"query": "工期"})
        await tool.handler({"query": ""})
        assert len(audit) == 2
        assert audit[0]["tool"] == "search-sections" and audit[0]["ok"] is True
        assert audit[0]["duration_ms"] >= 0 and audit[0]["summary"]
        assert audit[1]["ok"] is False and audit[1]["summary"]


# ---------------------------------------------------------------------------
# E/F/G 端到端：run_agent_section_repair（fake chat，零真实 AI）
# ---------------------------------------------------------------------------
def _assistant(calls: list) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": name,
                              "arguments": json.dumps(args, ensure_ascii=False)}}
                for i, (name, args) in enumerate(calls, 1)]}


class _FakeChat:
    """按脚本回放 assistant 消息；记录全部请求供断言。"""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list = []

    async def __call__(self, messages, *, tools=None, tool_choice=None, **kw):
        self.requests.append({"n_tools": len(tools or []),
                              "messages": list(messages)})
        if not self.responses:
            return {"role": "assistant", "content": "完成"}
        return self.responses.pop(0)


@pytest.fixture()
def queue_guard():
    """每条用例独立的队列，防跨用例串扰（R110 测试隔离教训）。"""
    reset_global_agent_queue()
    yield
    reset_global_agent_queue()


@pytest.fixture()
def agent_on(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "content_agent_repair_enabled", True)


def _targets() -> list:
    return [
        SectionTarget(section_id=SEC_A, title="土方开挖", content=ORIG_A,
                      word_budget=1200),
        SectionTarget(section_id=SEC_B, title="工期安排", content=ORIG_B,
                      word_budget=1200),
    ]


def _patch_workspace(monkeypatch, tmp_path):
    from app.services.content_agent import repair as repair_mod
    monkeypatch.setattr(repair_mod, "workspace_root_for",
                        lambda task_id: tmp_path / task_id)
    return repair_mod


async def _seed_sections(db_conn):
    for sid, content in ((SEC_A, ORIG_A), (SEC_B, ORIG_B)):
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, title, content, level,"
            " word_budget) VALUES (?,?,?,?,1,1200)",
            (sid, "scheme-x", "t", content))
    await db_conn.commit()


class TestEndToEndRepair:
    async def test_repair_writes_only_changed_sections(self, db_conn, tmp_path,
                                                       monkeypatch, agent_on,
                                                       queue_guard):
        _patch_workspace(monkeypatch, tmp_path)
        new_file = render_section_file("土方开挖", FIXED_A, SEC_A)
        chat = _FakeChat([
            _assistant([("read", {"path": TASK_FILE})]),
            _assistant([("write", {"path": f"{SECTION_DIR}/{SEC_A}.md",
                                   "content": new_file})]),
            {"role": "assistant",
             "content": "已把 5.5m 改为 5.6m；其余保持原样。"},
        ])
        resync_calls: list = []

        async def resync_fn(db, sid, content):
            resync_calls.append(sid)
            return content

        cache_calls: list = []

        async def fake_invalidate(db, scheme_id, facts_touched=False):
            cache_calls.append(scheme_id)
            return 0

        import app.services.facts_extractor as fe_mod
        monkeypatch.setattr(fe_mod, "invalidate_export_cache", fake_invalidate)

        await _seed_sections(db_conn)
        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(),
            facts_text="- 基坑深度 5.6m", chat_fn=chat, resync_fn=resync_fn)

        assert result.status == "completed", result.problems
        assert result.repaired == [SEC_A]
        assert result.invalid == [] and result.unchanged == [SEC_B]
        cur = await db_conn.execute(
            "SELECT content, word_count, word_status FROM sections WHERE id=?",
            (SEC_A,))
        row = await cur.fetchone()
        assert row[0] == FIXED_A and row[1] > 0 and row[2] in ("normal", "under", "over")
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id=?", (SEC_B,))
        assert (await cur.fetchone())[0] == ORIG_B
        assert resync_calls == [SEC_A]
        assert cache_calls == ["scheme-x"]
        # Agent 走的是工具循环：带 tools 下发，工具审计含 read 与 write
        assert chat.requests and chat.requests[0]["n_tools"] >= 8
        names = [c["tool"] for c in result.tool_calls]
        assert "read" in names and "write" in names
        assert result.turns >= 2

    async def test_broken_markers_keep_original_content(self, db_conn, tmp_path,
                                                        monkeypatch, agent_on,
                                                        queue_guard):
        _patch_workspace(monkeypatch, tmp_path)
        chat = _FakeChat([
            _assistant([("write", {"path": f"{SECTION_DIR}/{SEC_A}.md",
                                   "content": "标记被破坏的正文\n"})]),
            {"role": "assistant", "content": "完成"},
        ])
        await _seed_sections(db_conn)
        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(), chat_fn=chat)
        assert result.status == "failed"
        assert SEC_A in result.invalid
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())[0] == ORIG_A, "校验失败必须保留原正文"

    async def test_agent_exception_keeps_original(self, db_conn, tmp_path,
                                                  monkeypatch, agent_on,
                                                  queue_guard):
        _patch_workspace(monkeypatch, tmp_path)

        async def boom(*a, **kw):
            raise RuntimeError("AI 通道不可用")

        for sid in (SEC_A, SEC_B):
            await db_conn.execute(
                "INSERT INTO sections (id, scheme_id, title, content, level,"
                " word_budget) VALUES (?,?,?,?,1,1200)",
                (sid, "scheme-x", "t", "x"))
        await db_conn.commit()
        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(), chat_fn=boom)
        assert result.status == "failed" and "AI 通道不可用" in result.reason
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id=?", (SEC_A,))
        assert (await cur.fetchone())[0] == "x"

    async def test_modified_input_files_restored_by_baseline(
            self, db_conn, tmp_path, monkeypatch, agent_on, queue_guard):
        _patch_workspace(monkeypatch, tmp_path)
        # ✅ R154：run_agent_section_repair 终态后回收工作区（L-4）。本用例的
        #    断言面是「基线还原了被改写的输入文件内容」，需在返回后读工作区
        #    文件——显式豁免清理；清理行为本身由 test_r154 C3 组行为锁覆盖。
        import app.services.content_agent.repair as _repair_mod
        monkeypatch.setattr(_repair_mod, "cleanup_workspace_and_baseline",
                            lambda ws, bl: None)
        chat = _FakeChat([
            _assistant([
                ("write", {"path": TASK_FILE, "content": "被我改写的任务说明"}),
                ("write", {"path": FACTS_FILE, "content": "被我改写的事实"}),
            ]),
            {"role": "assistant", "content": "完成"},
        ])
        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(),
            facts_text="- 基坑深度 5.6m", chat_fn=chat)
        assert result.status == "completed"
        assert set(result.restored_inputs) == {TASK_FILE, FACTS_FILE}
        assert (Path(result.workspace_dir) / TASK_FILE).read_text(
            encoding="utf-8") != "被我改写的任务说明"

    async def test_turn_limit_keeps_original(self, db_conn, tmp_path,
                                             monkeypatch, agent_on, queue_guard):
        _patch_workspace(monkeypatch, tmp_path)

        async def endless(messages, **kw):
            return _assistant([("ls", {"path": "."})])

        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(),
            chat_fn=endless, max_turns=3)
        assert result.status == "failed"
        assert any("stop_reason=turn_limit" in p for p in result.problems)

    async def test_switch_off_is_skipped(self, db_conn, tmp_path, monkeypatch):
        _patch_workspace(monkeypatch, tmp_path)
        result = await run_agent_section_repair(
            db_conn, scheme_id="scheme-x", targets=_targets(),
            chat_fn=_FakeChat([]))
        assert result.status == "skipped"
        assert list(tmp_path.glob("content_repair_*")) == []


# ---------------------------------------------------------------------------
# H repair_agent 分层回落钩子
# ---------------------------------------------------------------------------
class TestRepairAgentFallbackHook:
    async def test_fallback_returns_content_when_core_reports_completed(
            self, db_conn, monkeypatch):
        import app.services.content_agent.repair as core_mod
        from app.services import repair_agent

        async def fake_core(db, **kw):
            assert kw.get("persist") is False, "集成路径必须 persist=False（由既有落库块写库）"
            return AgentRepairResult(
                status="completed", repaired=[SEC_A],
                new_contents={SEC_A: FIXED_A})

        monkeypatch.setattr(core_mod, "run_agent_section_repair", fake_core)
        cur = await db_conn.execute("SELECT 1")
        await cur.close()
        out = await repair_agent._agent_repair_fallback(
            db=db_conn, scheme_id="s", sid=SEC_A, title="t",
            before=ORIG_A, conflicts=[], facts="")
        assert out == FIXED_A

    async def test_fallback_returns_none_when_core_reports_failed(
            self, db_conn, monkeypatch):
        import app.services.content_agent.repair as core_mod
        from app.services import repair_agent

        async def fake_core(db, **kw):
            return AgentRepairResult(status="failed", reason="x")

        monkeypatch.setattr(core_mod, "run_agent_section_repair", fake_core)
        out = await repair_agent._agent_repair_fallback(
            db=db_conn, scheme_id="s", sid=SEC_A, title="t",
            before=ORIG_A, conflicts=[], facts="")
        assert out is None


# ---------------------------------------------------------------------------
# I 新文件字节体检（BOM / U+FFFD / 孤立 CR / py_compile）
# ---------------------------------------------------------------------------
class TestByteHealth:
    NEW_FILES = [
        "app/services/content_agent/__init__.py",
        "app/services/content_agent/workspace.py",
        "app/services/content_agent/validation.py",
        "app/services/content_agent/queue.py",
        "app/services/content_agent/tools.py",
        "app/services/content_agent/prompts.py",
        "app/services/content_agent/repair.py",
        "app/routers/content_agent.py",
    ]

    def test_no_bom_no_fffd_no_lone_cr(self):
        for rel in self.NEW_FILES:
            raw = (BACKEND_DIR / rel).read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), rel
            assert b"\xef\xbf\xbd" not in raw, rel
            crlf = raw.count(b"\r\n")
            lone_cr = raw.count(b"\r") - crlf
            assert lone_cr == 0, f"{rel} 孤立 CR={lone_cr}"

    def test_repair_agent_hook_in_place(self):
        src = (BACKEND_DIR / "app/services/repair_agent.py").read_text(
            encoding="utf-8")
        assert "content_agent_repair_enabled" in src
        assert "_agent_repair_fallback(" in src
        assert "run_agent_section_repair(" in src

    def test_router_registered_in_main(self):
        src = (BACKEND_DIR / "app/main.py").read_text(encoding="utf-8")
        assert "content_agent" in src
