"""正文 Agent 修复编排（R135 · 第二层修复的核心）。

流程（对齐易标「审计台账 → repair-sections → 提交校验 → 只回写变化小节」）：

1. 隔离工作区物化（小节文件 + 全局事实 + 任务说明）；
2. 基线保护**输入文件**（事实/任务说明被 Agent 改动则在提交前还原）；
3. 构建工具表（原生六件 + search-sections + report-failure）并挂审计；
4. 经**全局 FIFO 队列**串行执行 ``run_agent_loop``（工具循环由
   ``services/agent/agent_loop`` 承担，与目录 Agent 同一运行时契约）；
5. 提交前 ``baseline.restore()`` 还原被改动的输入；
6. 结构校验（:mod:`.validation`）—— 只有「标记完整 + 有变化」的小节才回写；
7. 回写：``UPDATE sections SET content, word_count, word_status, updated_at``
   （与 ``repair_agent`` 同一口径），``resync_fn`` 在同事务 commit 前全量
   重登记本章图表（R92 契约），收尾失效导出缓存；
8. 任何异常 / 校验失败 → **保留原正文**，绝不写库。

与易标的有意偏离：
- 正文为 Markdown（非受限 HTML），图片保护不适用（本仓图表是代码围栏 +
  导出期渲染，``resync_fn`` 已覆盖两侧一致性）；
- 不做「并发子 Agent 修复」（易标 fix-submission-issues 的多节并发形态）：
  本仓正文为单文件小节、冲突面小，由全局队列串行 + 单 Agent 多轮 edit
  已覆盖第一版能力；并发派发登记为后续增强。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.services.content_agent.workspace import (
    FACTS_FILE as FACTS_FILE_NAME,
)
from app.services.content_agent.workspace import (
    TASK_FILE as TASK_FILE_NAME,
)
from app.services.content_agent.workspace import (
    workspace_root_for,
)

logger = logging.getLogger("content_agent")

STOP_COMPLETED = "completed"
STOP_TOOL_STOP = "tool_stop"
STOP_TURN_LIMIT = "turn_limit"
STOP_TIMEOUT = "timeout"

#: 可回写的 stop 原因（其余一律视为未完成）。
_WRITABLE_STOP_REASONS = {STOP_COMPLETED, STOP_TOOL_STOP}


@dataclass
class SectionTarget:
    """一个待修复小节（主存储中的当前正文 + 冲突清单）。"""

    section_id: str
    title: str = ""
    content: str = ""
    conflicts: list = field(default_factory=list)
    word_budget: int = 0


@dataclass
class AgentRepairResult:
    """一次 Agent 修复批次的完整结论（含审计面）。"""

    status: str  # skipped / completed / failed
    reason: str = ""
    repaired: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    turns: int = 0
    stop_reason: str = ""
    workspace_dir: str = ""
    restored_inputs: list = field(default_factory=list)
    #: 校验通过且有变化的小节新正文（persist=False 时由调用方负责落库）。
    new_contents: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status, "reason": self.reason,
            "repaired": self.repaired, "unchanged": self.unchanged,
            "invalid": self.invalid, "problems": self.problems,
            "tool_calls": self.tool_calls, "turns": self.turns,
            "stop_reason": self.stop_reason,
            "restored_inputs": self.restored_inputs,
        }


async def _default_chat_fn(messages, *, tools, tool_choice=None, **kwargs):
    """默认 LLM 通道：复用 ``provider_factory.chat_with_tools``。"""
    from app.services.ai.provider_factory import chat_with_tools

    return await chat_with_tools(
        messages, tools=tools, tool_choice=tool_choice,
        scene="content_agent_repair", **kwargs)


def _build_tools(workspace_dir: str, target_ids: list[str],
                 audit: list) -> list:
    from app.services.agent.report_failure import build_report_failure_tool
    from app.services.agent.workspace_tools import (
        build_edit_tool,
        build_find_tool,
        build_grep_tool,
        build_ls_tool,
        build_read_tool,
        build_write_tool,
    )
    from app.services.content_agent.tools import (
        build_search_sections_tool,
        with_tool_audit,
    )

    async def _report_failure(reason: str) -> None:
        logger.warning("正文修复 Agent 报告无法继续：%s", reason)

    tools = [
        build_read_tool(workspace_dir),
        build_write_tool(workspace_dir),
        build_edit_tool(workspace_dir),
        build_grep_tool(workspace_dir),
        build_find_tool(workspace_dir),
        build_ls_tool(workspace_dir),
        build_search_sections_tool(workspace_dir, target_ids),
        build_report_failure_tool(_report_failure),
    ]
    return with_tool_audit(tools, audit)

async def run_agent_section_repair(
    db,
    *,
    scheme_id: str,
    targets: list[SectionTarget],
    facts_text: str = "",
    chat_fn=None,
    resync_fn=None,
    progress_cb=None,
    max_turns: int | None = None,
    total_timeout: float | None = None,
    persist: bool = True,
) -> AgentRepairResult:
    """执行一次 Agent 修复批次（第二层修复入口）。

    Args:
        db: aiosqlite 连接（``persist=True`` 时用于回写）；
        scheme_id: 方案 ID；
        targets: 待修复小节（含当前正文与冲突清单）；
        facts_text: 全局事实文本（唯一真相来源）；
        chat_fn: 可注入的 ``async (messages, *, tools, ...)`` 通道
            （缺省 ``provider_factory.chat_with_tools``，scene=
            ``content_agent_repair``）；
        resync_fn: R92 图表重登记回调 ``async (db, sid, content) -> str``；
        persist: 是否回写主存储（``repair_agent`` 集成路径传 False，
            由其既有落库块执行写库）。

    Returns:
        :class:`AgentRepairResult`。任何失败路径都**不写库**（保留原正文）。
    """
    from app.services.agent.agent_loop import run_agent_loop
    from app.services.agent.workspace_baseline import create_workspace_baseline
    from app.services.content_agent.prompts import SYSTEM_PROMPT, USER_TEMPLATE, build_task_instructions
    from app.services.content_agent.queue import global_agent_queue
    from app.services.content_agent.validation import validate_workspace_sections
    from app.services.content_agent.workspace import materialize_workspace

    if not bool(getattr(settings, "content_agent_repair_enabled", False)):
        return AgentRepairResult(
            status="skipped", reason="content_agent_repair_enabled 未开启")
    targets = [t for t in (targets or []) if str(t.section_id or "").strip()]
    if not targets:
        return AgentRepairResult(status="skipped", reason="无可修复小节")

    audit: list = []
    task_id = f"content_repair_{uuid.uuid4().hex[:12]}"
    workspace = workspace_root_for(task_id)
    sections_payload = [
        {"section_id": t.section_id, "title": t.title, "content": t.content or ""}
        for t in targets]
    instructions = build_task_instructions(
        [{"section_id": t.section_id, "title": t.title, "conflicts": t.conflicts}
         for t in targets])
    try:
        materialize_workspace(workspace, sections=sections_payload,
                              facts_text=facts_text, task_instructions=instructions)
    except Exception as error:  # noqa: BLE001 —— 物化失败：不写库，如实返回
        logger.warning("Agent 修复工作区物化失败（scheme=%s）：%s", scheme_id, error)
        return AgentRepairResult(status="failed",
                                 reason=f"工作区物化失败：{error}",
                                 tool_calls=audit)


    # 基线只保护**输入文件**：小节文件是 Agent 的修改对象，不在保护范围。
    baseline = create_workspace_baseline(
        workspace, workspace.parent / (task_id + "_baseline"))
    input_files = [TASK_FILE_NAME]
    if str(facts_text or "").strip():
        input_files.append(FACTS_FILE_NAME)
    try:
        baseline.protect("inputs", input_files)
    except ValueError as error:  # 路径越界等：防御，不让基线问题打断主流程
        logger.warning("Agent 修复基线登记失败（忽略保护）：%s", error)
        baseline = None

    target_ids = [t.section_id for t in targets]
    tools = _build_tools(str(workspace), target_ids, audit)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE},
    ]
    effective_turns = int(max_turns or getattr(
        settings, "content_agent_repair_max_turns", 24) or 24)
    effective_timeout = float(total_timeout or getattr(
        settings, "content_agent_repair_timeout", 900) or 900)
    queue = global_agent_queue()

    async def _job():
        if progress_cb:
            await progress_cb("Agent 修复执行中（队列已受理）")
        return await run_agent_loop(
            messages, tools=tools, chat_fn=chat_fn or _default_chat_fn,
            max_turns=effective_turns, total_timeout=effective_timeout)

    started = time.monotonic()
    try:
        loop_result = await queue.submit(
            _job, priority=10, task_id=task_id,
            description=f"正文 Agent 修复（{len(targets)} 节）",
            timeout=effective_timeout + 30)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 —— 队列/循环失败：不写库
        logger.warning("Agent 修复循环失败（scheme=%s task=%s）：%s",
                       scheme_id, task_id, error)
        return AgentRepairResult(
            status="failed", reason=f"Agent 修复失败：{error}", tool_calls=audit,
            turns=0, workspace_dir=str(workspace))
    turns = int(getattr(loop_result, "turns", 0) or 0)
    stop_reason = str(getattr(loop_result, "stop_reason", "") or "")


    # 提交前：还原被 Agent 改动的输入文件（不信任 Agent 自报）。
    restored: list = []
    if baseline is not None:
        try:
            restored = baseline.restore()
        except Exception as error:  # noqa: BLE001
            logger.warning("Agent 修复基线还原失败：%s", error)
    if restored:
        logger.info("Agent 修复还原了 %d 个被改动的输入文件：%s",
                    len(restored), restored)

    validations, global_problems = validate_workspace_sections(
        workspace, sections_payload)
    writable = {v.section_id for v in validations if v.writable}
    invalid = [v.section_id for v in validations if v.status == "invalid"]
    unchanged = [v.section_id for v in validations if v.status == "unchanged"]
    problems = list(global_problems)
    for v in validations:
        problems.extend(f"小节 {v.section_id}：{p}" for p in v.problems)

    if stop_reason not in _WRITABLE_STOP_REASONS:
        # 轮数上限/超时/未知收束 = 任务未完成，全部不落库（保留原正文）。
        problems.append(
            f"Agent 未正常收束（stop_reason={stop_reason}），本轮修复不落库")
        writable = set()

    result = AgentRepairResult(
        status="failed" if (invalid or problems) else "completed",
        reason="", repaired=[], unchanged=unchanged, invalid=invalid,
        problems=problems, tool_calls=audit, turns=turns,
        stop_reason=stop_reason, workspace_dir=str(workspace),
        restored_inputs=restored)

    new_contents: dict = {}
    from app.services.content_agent.workspace import parse_section_content, read_section_file
    for t in targets:
        if t.section_id not in writable:
            continue
        raw = read_section_file(workspace, t.section_id)
        content = parse_section_content(raw or "", t.section_id) or ""
        if content:
            new_contents[t.section_id] = content

    if not new_contents:
        if not problems and not invalid:
            result.reason = "Agent 未做任何修改"
        return result
    result.new_contents = new_contents

    if not persist:
        # repair_agent 集成路径：由其既有落库块执行写库，这里只交回内容。
        result.repaired = list(new_contents.keys())
        result.status = "completed"
        if progress_cb:
            await progress_cb(
                f"Agent 修复完成：待回写 {len(result.repaired)} 节"
                f"（耗时 {time.monotonic() - started:.0f}s）")
        return result

    try:
        from datetime import datetime

        from app.services.content_utils import DEFAULT_WORD_BUDGET, text_word_count, word_status_for
        for t in targets:
            content = new_contents.get(t.section_id)
            if content is None:
                continue
            if resync_fn is not None:
                try:
                    fixed = await resync_fn(db, t.section_id, content)
                    if isinstance(fixed, str) and fixed:
                        content = fixed
                        new_contents[t.section_id] = fixed
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        "Agent 修复：小节 %s 图表重登记失败（保留修复正文）：%s",
                        t.section_id, error)
            wc = text_word_count(content)
            wb = t.word_budget or DEFAULT_WORD_BUDGET
            await db.execute(
                "UPDATE sections SET content=?, word_count=?, word_status=?, "
                "updated_at=? WHERE id=?",
                (content, wc, word_status_for(wc, wb),
                 datetime.now().isoformat(), t.section_id))
        await db.commit()
        try:
            from app.services.facts_extractor import invalidate_export_cache
            await invalidate_export_cache(db, scheme_id)
        except Exception as error:  # noqa: BLE001 —— 失效失败不影响修复结果
            logger.warning("Agent 修复后导出缓存失效失败：%s", error)
        result.repaired = list(new_contents.keys())
        result.status = "completed"
        if progress_cb:
            await progress_cb(
                f"Agent 修复完成：回写 {len(result.repaired)} 节"
                f"（耗时 {time.monotonic() - started:.0f}s）")
    except Exception as error:  # noqa: BLE001 —— 落库失败：保留原正文
        logger.warning("Agent 修复回写失败（保留原正文）：%s", error)
        result.status = "failed"
        result.reason = f"回写失败：{error}"
        result.repaired = []
    return result

