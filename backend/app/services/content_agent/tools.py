"""内容修复 Agent 的专用工具与工具调用审计（R135）。

- ``search-sections``：对齐易标 ``contentGenerationConsistencyTools.cjs``
  的同名工具 —— 在工作区小节文件中做**字面量**检索，返回
  ``{section_id, line, snippet}``（跳过程序标记行，便于 Agent 定位冲突文本）；
- ``with_tool_audit``：包装任意工具表，把每次调用记录为
  ``{tool, args, duration_ms, ok, summary}`` —— 「每次工具调用记录：工具名、
  参数、耗时、结果摘要；失败时返回明确错误，不静默吞掉」的落点。

检索刻意不做正则（对齐易标 rg 默认字面量用法的最常用形态）：修复 Agent
的典型查询是「工期」「96 人」「GB 50204」这类字面量；正则由模型转义负担
大且易误伤，不作为第一版能力。
"""
from __future__ import annotations

import asyncio
import json
import time

from app.services.agent.agent_loop import AgentTool, ToolCallOutcome
from app.services.agent.workspace_tools import resolve_workspace_file

#: search-sections 返回的匹配数上限（对齐易标 SEARCH_LIMIT=80）。
SEARCH_LIMIT = 80
#: 单条 snippet 的字符上限（对齐易标 SNIPPET_LENGTH=400）。
SNIPPET_LENGTH = 400


def build_search_sections_tool(workspace_dir: str,
                               section_ids: list[str]) -> AgentTool:
    """构建 ``search-sections`` 工具（仅检索指定小节，且只限工作区内）。"""
    allowed = [str(s) for s in section_ids if str(s)]
    root = str(workspace_dir)

    def _search(query: str, limit: int) -> list[dict]:
        needle = str(query or "").strip()
        matches: list[dict] = []
        if not needle:
            return matches
        for sid in allowed:
            try:
                target = resolve_workspace_file(
                    root, f"正文/{sid}.md")
            except ValueError:
                continue
            try:
                text = open(target.resolved_path, encoding="utf-8").read()
            except OSError:
                continue
            low_needle = needle.lower()
            low_text = text.lower()
            start = 0
            while True:
                idx = low_text.find(low_needle, start)
                if idx < 0:
                    break
                line_no = text.count("\n", 0, idx) + 1
                line = text.splitlines()[line_no - 1] if line_no <= text.count("\n") + 1 else ""
                if "<!-- SECTION:" not in line:  # 程序标记行不是正文，跳过
                    line_start = text.rfind("\n", 0, idx) + 1
                    line_end = text.find("\n", idx)
                    if line_end < 0:
                        line_end = len(text)
                    snippet = text[line_start:line_end].strip()
                    matches.append({
                        "section_id": sid,
                        "line": line_no,
                        "snippet": snippet[:SNIPPET_LENGTH],
                    })
                    if len(matches) >= limit:
                        return matches
                start = idx + max(len(needle), 1)
        return matches

    async def _handler(args: dict) -> ToolCallOutcome:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolCallOutcome.error("query 不能为空")
        try:
            limit = int(args.get("limit") or SEARCH_LIMIT)
        except (TypeError, ValueError):
            return ToolCallOutcome.error("limit 必须是整数")
        limit = max(1, min(limit, SEARCH_LIMIT))
        matches = await asyncio.to_thread(_search, query, limit)
        return ToolCallOutcome.json_result({
            "query": query,
            "total": len(matches),
            "truncated": len(matches) >= limit,
            "matches": matches,
        })

    return AgentTool(
        name="search-sections",
        description=(
            "在本次修复目标小节内检索关键词（字面量、不区分大小写），返回"
            "小节 ID、行号与所在行摘录（最多 80 条）。用于先定位冲突文本再"
            "用 edit 精准修改。参数：query（必填）、limit（可选，默认 80）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要检索的关键词（字面量）"},
                "limit": {"type": "integer",
                          "description": "最多返回条数（默认 80，上限 80）"},
            },
        },
        handler=_handler,
        prompt_guidelines=[
            "先用 search-sections 定位冲突文本的 section_id 与原文，再用 edit 做最小修改。",
        ],
    )


def _digest_args(args: dict) -> str:
    try:
        raw = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(args)
    return raw[:200]


def with_tool_audit(tools: list[AgentTool], audit: list[dict]) -> list[AgentTool]:
    """包装工具表，记录每次调用的审计条目（原工具语义与结果不变）。

    审计条目：``{"tool", "args", "duration_ms", "ok", "summary"}``。
    工具失败（``is_error``）照常回灌给模型并如实记录 ``ok=False`` ——
    绝不静默吞掉。
    """
    wrapped: list[AgentTool] = []
    for tool in tools:
        if tool.name == "ask-user":
            wrapped.append(tool)  # 交互工具不经审计包装（避免打断问答通道）
            continue
        inner = tool.handler

        async def _audited(args: dict, _tool=tool, _inner=inner) -> ToolCallOutcome:
            started = time.monotonic()
            try:
                outcome = await _inner(args)
            except Exception as error:  # noqa: BLE001 —— 记录后按失败回灌，不抛出
                audit.append({
                    "tool": _tool.name, "args": _digest_args(args),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "ok": False,
                    "summary": f"工具异常：{error}"[:200],
                })
                return ToolCallOutcome.error(f"工具 {_tool.name} 执行异常：{error}")
            audit.append({
                "tool": _tool.name, "args": _digest_args(args),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "ok": not outcome.is_error,
                "summary": (outcome.content or "")[:200],
            })
            return outcome

        wrapped.append(AgentTool(
            name=tool.name, description=tool.description,
            parameters=tool.parameters, handler=_audited,
            prompt_guidelines=list(tool.prompt_guidelines),
            sequential=tool.sequential,
        ))
    return wrapped
