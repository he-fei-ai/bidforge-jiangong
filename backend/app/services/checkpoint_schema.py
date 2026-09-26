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
        ),
    ),
}

#: Python type hints for each field (for documentation / future Pydantic model)
CHECKPOINT_FIELD_TYPES: dict[str, dict[str, str]] = {
    "outline_result": {
        "event": "str",
        "partial": "bool",
        "outline": "list[dict]",
        "review": "dict",
        "failed_chapters": "list[dict]",
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