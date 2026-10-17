"""Checkpoint schema — single source of truth for task checkpoint contracts.

Both backend whitelist (_CHECKPOINT_KINDS) and frontend TypeScript interface
(frontend/src/types/checkpointSchema.ts) are derived from this file.

Usage:
    python tools/generate_checkpoint_schema.py

This eliminates the manual sync drift that caused G13-5 (whitelist missing
event/partial) and G12-6 (frontend never consumed content_result).
"""
from __future__ import annotations

from typing import Any

#: task_type → (checkpoint kind, allowed fields)
#:
#: kind is the JSON key stored in checkpoint_json and returned by
#: GET /sse/task/{id} under _attach_checkpoint_result.
#: fields is the whitelist of checkpoint_json keys allowed to pass through
#: to the frontend response.
#: ✅ R89（2026-10-17）：``facts_generation`` 此前**缺席**于本单一事实源，
#: 而实际白名单写在 ``routers/sse_checkpoint.py::_FACTS_CHECKPOINT_FIELDS``
#: （手写第二份）。后果有两处，且都正是本文件 docstring 声称要消除的漂移：
#:   ① ``frontend/src/types/checkpointSchema.ts``（由本文件生成）**没有**
#:      ``facts_result`` 接口 ⇒ 前端 ``SchemeWorkbenchPage`` 只能在页面里
#:      **内联再抄一份**（其注释已自认「本文件另有本地副本」）；
#:   ② 内容/目录两种任务类型都有 ``sh._CHECKPOINT_KINDS[t] == cs.CHECKPOINT_KINDS[t]``
#:      的 parity 断言（R77 / R66 护栏），唯独 facts 这一族没有 ——
#:      两份清单分叉时无任何测试会红。
#: 现把 facts 归入本文件，并把内容字段提到模块级常量供写入侧派生。
#: ``event`` / ``message`` 由调用方在筛选**之外**补入（sse_handlers 统一行为），
#: 但它们同样属于该 kind 的响应契约，故一并登记以生成前端类型。
FACTS_CHECKPOINT_FIELDS: tuple[str, ...] = (
    "event",
    "message",
    "segment_stats",
    "cross_conflicts",
    "warnings",
    "group_count",
    "total_items",
)

CHECKPOINT_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "outline_generation": (
        "outline_result",
        (
            "event",
            "partial",
            "outline",
            "review",
            "failed_chapters",
            "failed_count",
        ),
    ),
    "content_generation": (
        "content_result",
        (
            "event",
            "message",
            "done",
            "total",
            "failed_count",
            "failed_sections",
            "words",
            "word_count",
            "run_words",
            "over_count",
            # ✅ R66（2026-10-11）字数可追溯：逐章目标/实际/偏差汇总
            #    （word_audit_summary 结构；审核预检与断线重挂共用）。
            "word_audit",
            # ✅ R77b（2026-10-15）：事实冲突定点修复终态汇总（sections /
            #    fixed_sections / conflicts / applied / rejected；无冲突时全 0）。
            "fact_autofix_summary",
        ),
    ),
    # ✅ R89（2026-10-17）：facts 归入单一事实源（此前只存在于 routers 侧手写副本，
    #    导致生成的 TS 契约缺 facts_result + 无 parity 断言可拦分叉）。
    "facts_generation": ("facts_result", FACTS_CHECKPOINT_FIELDS),
}

#: Python type hints for each field (for documentation / future Pydantic model)
CHECKPOINT_FIELD_TYPES: dict[str, dict[str, str]] = {
    "outline_result": {
        "event": "str",
        "partial": "bool",
        "outline": "list[dict]",
        "review": "dict",
        # ✅ R79：实收值是**章标题字符串**（sse_handlers._merge_unit_results 的
        #    `failed_chapters.append(str(ch.get("title") ...))`），旧声明写成
        #    list[dict] 属文档失真 —— 前端 `types/checkpointSchema.ts` 由本文件
        #    生成，声明错误会传导到生成的 TS 类型与所有消费方读法。
        "failed_chapters": "list[str]",
        "failed_count": "int",
    },
    "content_result": {
        "event": "str",
        "message": "str",
        "done": "int",
        "total": "int",
        "failed_count": "int",
        "failed_sections": "list[dict]",
        "words": "int",
        "word_count": "int",
        "run_words": "int",
        "over_count": "int",
        "word_audit": "dict",
        "fact_autofix_summary": "dict",
    },
    # ✅ R89：与页面内联副本逐项对齐（前端 applyFactsCompletedEvent 只按数组消费
    #    cross_conflicts，具体形态由 facts_extractor 决定，故用 list[dict] → unknown[]）。
    "facts_result": {
        "event": "str",
        "message": "str",
        "segment_stats": "dict",
        "cross_conflicts": "list[dict]",
        "warnings": "list[str]",
        "group_count": "int",
        "total_items": "int",
    },
}


def get_checkpoint_schema() -> dict[str, Any]:
    """Return the full schema as a JSON-serializable dict."""
    return {
        "kinds": CHECKPOINT_KINDS,
        "field_types": CHECKPOINT_FIELD_TYPES,
    }


def generate_typescript_schema() -> str:
    """Generate a TypeScript interface file from the schema."""
    lines = [
        "// AUTO-GENERATED — DO NOT EDIT",
        "// Source: backend/app/services/checkpoint_schema.py",
        "// Regenerate: python tools/generate_checkpoint_schema.py",
        "",
    ]
    for kind, fields in CHECKPOINT_KINDS.values():
        field_lines = []
        types = CHECKPOINT_FIELD_TYPES.get(kind, {})
        for field in fields:
            py_type = types.get(field, "unknown")
            ts_type = _py_to_ts(py_type)
            field_lines.append(f"  {field}?: {ts_type};")
        lines.append(f"export interface {kind} {{")
        lines.extend(field_lines)
        lines.append("}")
        lines.append("")
    lines.append("export interface TaskTerminalInfo {")
    lines.append("  status: string;")
    lines.append("  message?: string;")
    lines.append("  progress?: number;")
    for kind, _fields in CHECKPOINT_KINDS.values():
        lines.append(f"  {kind}?: {kind};")
    lines.append("  [k: string]: unknown;")
    lines.append("}")
    return "\n".join(lines)


def _py_to_ts(py_type: str) -> str:
    """Convert a Python type annotation to a TypeScript type."""
    mapping = {
        "str": "string",
        "int": "number",
        "bool": "boolean",
        "dict": "Record<string, unknown>",
        "list[dict]": "unknown[]",
        "list[str]": "string[]",
        "list[int]": "number[]",
    }
    return mapping.get(py_type, "unknown")