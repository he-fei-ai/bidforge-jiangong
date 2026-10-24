"""P1 — json-validation 工具（1:1 移植自 piJsonValidationTool.cjs）

移植来源：易标 Yibiao 2.30.20 的 ``electron_services_pi_piJsonValidationTool.cjs``。

对应关系：
- ``normalize_workspace_file_path`` / ``resolve_workspace_file``：复用
  workspace_tools.py 中 1:1 移植的路径防护（源文件顶部同名函数）。
- 预置 Schema Map（含 Windows 大小写归一的 fileKey）、parse / schema /
  read / validation / success 五种 stage、
  ``{valid, stage, file_path, errors, message}`` 返回体与两段 message 文案、
  校验失败置 isError —— 均照搬源文件 ``createToolResult`` /
  ``createPiJsonValidator`` / ``createPiJsonValidationTool``。
- Ajv（Draft-07）替换为 jsonschema 4.26 的 ``Draft7Validator``：
  注意 jsonschema 没有 ``all_errors=True`` 构造参数（Ajv 才有），
  其 ``iter_errors()`` 默认即收集全部错误，语义等价于 Ajv allErrors。
  错误结构按 ``normalizeAjvErrors`` 归一为
  {instancePath, schemaPath, keyword, message, params}。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from jsonschema import Draft7Validator

from app.services.agent.agent_loop import AgentTool, ToolCallOutcome
from app.services.agent.workspace_tools import (
    normalize_workspace_file_path,
    resolve_workspace_file,
)

JSON_VALIDATION_TOOL_NAME = "json-validation"

#: 校验通过/未通过的 message 文案（与易标逐字一致）。
_MESSAGE_OK = "JSON.parse 和 Ajv 校验均已通过。"
_MESSAGE_FAIL = "校验未通过，请根据 errors 修复文件后重新校验。"

_REQUIRED_MSG_RE = re.compile(r"'([^']+)'")

#: 常见 keyword 的 Ajv params 近似映射（jsonschema 不提供 Ajv 的 params 结构，
#: 取而代之用 validator_value 还原同名字段；未覆盖的 keyword 返回空 dict）。
_LIMIT_KEYWORDS = frozenset({
    "minLength", "maxLength", "minimum", "maximum",
    "minItems", "maxItems", "minProperties", "maxProperties",
})


def _file_key(workspace_root: str, file_path: str) -> str:
    """预置 Schema 的匹配键（移植自 piJsonValidationTool.cjs ``fileKey``：
    resolve 到工作区，Windows 下统一小写）。"""
    resolved = os.path.abspath(os.path.join(workspace_root, str(file_path or "")))
    return resolved.lower() if os.name == "nt" else resolved


def _ajv_params(error) -> dict:
    """按 Ajv params 语义还原常见 keyword 的参数（见模块 docstring 说明）。"""
    keyword = error.validator
    if keyword == "required":
        match = _REQUIRED_MSG_RE.search(error.message or "")
        return {"missingProperty": match.group(1)} if match else {}
    if keyword == "type":
        return {"type": error.validator_value}
    if keyword == "enum":
        return {"allowedValues": list(error.validator_value)}
    if keyword == "pattern":
        return {"pattern": error.validator_value}
    if keyword in _LIMIT_KEYWORDS:
        return {"limit": error.validator_value}
    return {}


def _normalize_errors(errors) -> list[dict]:
    """把 jsonschema 错误归一为易标 ``normalizeAjvErrors`` 的结构。"""
    normalized = []
    for error in errors:
        path_parts = [str(p) for p in error.absolute_path]
        schema_parts = [str(p) for p in error.absolute_schema_path]
        normalized.append({
            "instancePath": "/" + "/".join(path_parts) if path_parts else "/",
            "schemaPath": ("#" + "/" + "/".join(schema_parts)
                           if schema_parts else "#"),
            "keyword": error.validator or "",
            "message": error.message or "字段不符合 JSON Schema",
            "params": _ajv_params(error),
        })
    return normalized


def _create_tool_result(file_path: str, valid: bool, stage: str,
                        errors: list[dict] | None = None) -> ToolCallOutcome:
    """生成统一的工具返回结果（移植自 ``createToolResult``）。

    对应易标 ``JSON.stringify(payload, null, 2)`` 文本，校验未通过置
    ``is_error``（易标 isError:true）。
    """
    payload = {
        "valid": valid,
        "stage": stage,
        "file_path": file_path,
        "errors": errors or [],
        "message": _MESSAGE_OK if valid else _MESSAGE_FAIL,
    }
    return ToolCallOutcome.json_result(payload, is_error=not valid)


def build_json_validation_tool(workspace_dir: str,
                               validation_schemas: dict | None = None) -> AgentTool:
    """json-validation 工具（1:1 移植自 piJsonValidationTool.cjs）。

    预置 Schema 的文件只传 file_path，其余文件由调用方在参数里提供 schema；
    结果由任务提交时统一把关。

    Args:
        workspace_dir: 任务工作区根目录（所有 file_path 只允许其内）。
        validation_schemas: 预置 Schema 映射，键为工作区相对路径，
            值为 JSON Schema（Draft-07）。
    """
    root = os.path.abspath(workspace_dir)
    # 预置 Schema Map（移植自 createPiJsonValidator 里的 presetSchemas 构建）。
    preset_schemas: dict[str, object] = {}
    for preset_path, schema in dict(validation_schemas or {}).items():
        preset_schemas[_file_key(root, normalize_workspace_file_path(preset_path))] = schema

    def _validate_content(relative_path: str, source: str, supplied_schema):
        try:
            data = json.loads(source)
        except ValueError as e:  # JSONDecodeError 是 ValueError 子类
            return _create_tool_result(relative_path, False, "parse",
                                       [{"message": str(e)}])
        try:
            if _file_key(root, relative_path) in preset_schemas:
                schema = preset_schemas[_file_key(root, relative_path)]
            else:
                schema = supplied_schema
            if schema is None:
                raise ValueError(
                    f"任务未为 {relative_path} 预置 Schema，调用时必须提供 schema")
            validator = Draft7Validator(schema)
            errors = _normalize_errors(validator.iter_errors(data))
        except Exception as e:  # noqa: BLE001 —— 对应易标对 ajv.compile 的兜底
            return _create_tool_result(relative_path, False, "schema",
                                       [{"message": str(e)}])
        if errors:
            return _create_tool_result(relative_path, False, "validation", errors)
        return _create_tool_result(relative_path, True, "success")

    async def _handler(args: dict) -> ToolCallOutcome:
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return ToolCallOutcome.error("file_path 必须是当前工作区内的非空相对路径")
        # read 阶段：路径防护 + 读文件（移植自 validateFile 的 try 块）。
        try:
            resolved = resolve_workspace_file(root, file_path)
            relative_path = resolved.relative_path
            source = Path(resolved.resolved_path).read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError) as e:
            return _create_tool_result(file_path, False, "read",
                                       [{"message": str(e)}])
        return _validate_content(relative_path, source, args.get("schema"))

    return AgentTool(
        name=JSON_VALIDATION_TOOL_NAME,
        description=(
            "使用 JSON.parse 和 Ajv 校验当前工作区内的 JSON 文件。任务已预置 "
            "Schema 时只传 file_path；没有预置时根据输出要求提供完整 schema。"
            "校验失败后修复文件并再次调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "待校验 JSON 文件相对于当前工作区的路径。",
                },
                "schema": {
                    "anyOf": [{"type": "object"}, {"type": "boolean"}],
                    "description": "仅在任务没有为目标文件预置 Schema 时提供，"
                                   "根据当前任务输出要求构造 JSON Schema Draft-07。",
                },
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
        handler=_handler,
        # pi 该工具无 promptGuidelines（仅 promptSnippet），故留空。
        prompt_guidelines=[],
    )


__all__ = [
    "JSON_VALIDATION_TOOL_NAME",
    "build_json_validation_tool",
]
