"""P3 补齐 —— 阶段产物 Schema 本体（移植自易标 Yibiao 2.30.20）。

移植来源（``C:\\Users\\Administrator\\AppData\\Local\\Temp\\yibiao_outline\\
outlineGenerationTaskV2.cjs``，行号 1:1 对应）：

- L36–80 ``createDirectoryNodeSchema(level, root)`` →
  :func:`create_directory_node_schema`（level 1–6 递归，``level < 6`` 时
  ``oneOf`` 叶/枝，``children`` ``minItems 2``；``root=True`` 追加
  ``attr`` enum 与 ``ATTRIBUTE_CONTENT_MODE_RULES`` 的 ``if/then`` 规则）；
- L82 ``ROOT_NODE_SCHEMA`` / L84–95 ``OUTLINE_JSON_SCHEMA``；
- L97–122 ``TECHNICAL_SCORE_GROUPS_SCHEMA``（``requirement_id`` pattern
  ``^R[1-9]\\d*$``）；
- L124–177 ``SCORE_DIRECTORY_PLAN_SCHEMA``；
- L179–205 ``OUTLINE_REVIEW_SCHEMA``（``status`` / ``category`` enum 逐字）；
- L207–259 ``createLeafAllocationSchema(minimumLeafCount)`` →
  :func:`create_leaf_allocation_schema`（``allocated`` / ``agent-decides``
  双形态 ``oneOf``）；
- L895–903 ``jsonValidationSchemas`` 构建 → :func:`build_json_validation_schemas`
  （``no_technical_score_mode`` 时不装评分两件套与 leaf-allocation）；
- L904–906 ``outputValidators`` 编译 → :func:`build_output_validators`。

消费契约：

- :func:`build_json_validation_schemas` 产物同时用于
  ``json_validation.build_json_validation_tool`` 的 ``validation_schemas``
  预置（模型自查）与提交校验器；
- :func:`build_output_validators` 产物满足 P2 ``schema_validators`` 注入契约
  ``validator(value) -> (ok, errors)``，``errors`` 项为 Ajv 风格
  ``instancePath`` / ``message``（复用 ``json_validation`` 的归一实现），
  与 Ajv ``outputAjv.compile(schema)`` 的消费方式等价。
  jsonschema 没有 ``allErrors`` 构造参数（Ajv 才有），错误列表一次全量返回。
"""
from __future__ import annotations

from jsonschema import Draft7Validator

from .json_validation import _normalize_errors
from .outline_identity import NODE_ID_PATTERN
from .validation import (
    ATTRIBUTE_CONTENT_MODE_RULES,
    CONTENT_MODES,
    LEAF_ALLOCATION_FILE,
    OUTLINE_ATTRIBUTES,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_FILE,
    SCORE_DIRECTORY_PLAN_FILE,
    TECHNICAL_SCORE_GROUPS_FILE,
)


def create_directory_node_schema(level: int, root: bool = False) -> dict:
    """目录节点 Schema（易标 ``createDirectoryNodeSchema``，L36–80 逐结构移植）。"""
    base_properties: dict = {
        "id": {"anyOf": [{"type": "string", "pattern": NODE_ID_PATTERN},
                         {"type": "null"}]},
        "number": {"type": "string"},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
    }
    if root:
        base_properties["attr"] = {"type": "string", "enum": OUTLINE_ATTRIBUTES}
    base_required = ["id", "title", "description"] + (["attr"] if root else [])
    leaf_schema: dict = {
        "type": "object",
        "required": [*base_required, "content_mode"],
        "additionalProperties": False,
        "properties": {
            **base_properties,
            "content_mode": {"type": "string", "enum": CONTENT_MODES},
            "content_mode_note": {"type": "string"},
        },
    }
    if root:
        # 易标 L57–61：属性 → 强制内容模式的 if/then 规则（按规则表逐条展开）。
        leaf_schema["allOf"] = [
            {
                "if": {"properties": {"attr": {"const": attr}},
                       "required": ["attr"]},
                "then": {"properties": {"content_mode": {"const": mode}}},
            }
            for attr, mode in ATTRIBUTE_CONTENT_MODE_RULES.items()
        ]
    if level < 6:
        branch_schema: dict = {
            "type": "object",
            "required": [*base_required, "children"],
            "additionalProperties": False,
            "properties": {
                **base_properties,
                "children": {
                    "type": "array",
                    "minItems": 2,
                    "items": create_directory_node_schema(level + 1),
                },
            },
        }
        return {"oneOf": [leaf_schema, branch_schema]}
    return leaf_schema


#: 根节点 Schema（易标 ``ROOT_NODE_SCHEMA``，L82）。
ROOT_NODE_SCHEMA = create_directory_node_schema(1, True)

#: ``outline.json`` 结构 Schema（易标 L84–95）。
OUTLINE_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["outline"],
    "additionalProperties": False,
    "properties": {
        "outline": {
            "type": "array",
            "minItems": 1,
            "items": ROOT_NODE_SCHEMA,
        },
    },
}

#: ``technical-score-groups.json`` Schema（易标 L97–122）。
TECHNICAL_SCORE_GROUPS_SCHEMA: dict = {
    "type": "object",
    "required": ["groups"],
    "additionalProperties": False,
    "properties": {
        "groups": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["requirement_id", "title", "description",
                             "detail_points"],
                "additionalProperties": False,
                "properties": {
                    "requirement_id": {"type": "string",
                                       "pattern": r"^R[1-9]\d*$"},
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "detail_points": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    },
}

#: ``score-directory-plan.json`` Schema（易标 L124–177）。
SCORE_DIRECTORY_PLAN_SCHEMA: dict = {
    "type": "object",
    "required": ["allow_root_changes", "branches", "extra_titles"],
    "additionalProperties": False,
    "properties": {
        "allow_root_changes": {"type": "boolean"},
        "branches": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["root_id", "root_title", "score_item_level",
                             "mappings"],
                "additionalProperties": False,
                "properties": {
                    "root_id": {"anyOf": [{"type": "string",
                                           "pattern": NODE_ID_PATTERN},
                                          {"type": "null"}]},
                    "root_title": {"type": "string", "minLength": 1},
                    "score_item_level": {"type": "integer", "minimum": 1,
                                         "maximum": 6},
                    "mappings": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["requirement_id", "target_title"],
                            "additionalProperties": False,
                            "properties": {
                                "requirement_id": {"type": "string",
                                                   "pattern": r"^R[1-9]\d*$"},
                                "target_title": {"type": "string",
                                                 "minLength": 1},
                                "additional_titles": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string",
                                              "minLength": 1},
                                },
                                "adjustment_note": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "extra_titles": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["root_id", "title", "reason"],
                "additionalProperties": False,
                "properties": {
                    "root_id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

#: ``outline-review.json`` Schema（易标 L179–205；enum 逐字）。
OUTLINE_REVIEW_SCHEMA: dict = {
    "type": "object",
    "required": ["status", "issues", "user_feedback", "summary"],
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string",
                   "enum": ["passed", "simple_fix", "user_feedback",
                            "user_refuse"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category", "problem", "repair",
                             "confirmation_required"],
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["leaf-count", "score-coverage",
                                 "duplicate-directory",
                                 "professional-structure"],
                    },
                    "problem": {"type": "string", "minLength": 1},
                    "repair": {"type": "string", "minLength": 1},
                    "confirmation_required": {"type": "boolean"},
                },
            },
        },
        "user_feedback": {"type": "string"},
        "summary": {"type": "string", "minLength": 1},
    },
}


def create_leaf_allocation_schema(minimum_leaf_count: int = 2) -> dict:
    """``leaf-allocation.json`` Schema（易标 ``createLeafAllocationSchema``，L207–259）。"""
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["mode", "target_ai_leaf_count",
                             "fixed_ai_leaf_count",
                             "allocatable_ai_leaf_count", "allocations"],
                "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "enum": ["allocated"]},
                    "target_ai_leaf_count": {"type": "integer",
                                             "minimum": 1},
                    "fixed_ai_leaf_count": {"type": "integer",
                                            "minimum": 0},
                    "allocatable_ai_leaf_count": {"type": "integer",
                                                  "minimum": 1},
                    "allocations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["root_id", "leaf_count"],
                            "additionalProperties": False,
                            "properties": {
                                "root_id": {"type": "string",
                                            "minLength": 1},
                                "leaf_count": {"type": "integer",
                                               "minimum": minimum_leaf_count},
                            },
                        },
                    },
                },
            },
            {
                "type": "object",
                "required": ["mode", "target_ai_leaf_count",
                             "fixed_ai_leaf_count",
                             "allocatable_ai_leaf_count", "allocations"],
                "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string",
                             "enum": ["agent-decides"]},
                    "target_ai_leaf_count": {"type": "null"},
                    "fixed_ai_leaf_count": {"type": "integer",
                                            "minimum": 0},
                    "allocatable_ai_leaf_count": {"type": "null"},
                    "allocations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["root_id"],
                            "additionalProperties": False,
                            "properties": {
                                "root_id": {"type": "string",
                                            "minLength": 1},
                            },
                        },
                    },
                },
            },
        ],
    }


def build_json_validation_schemas(*, no_technical_score_mode: bool,
                                  standalone_technical: bool) -> dict:
    """预置 Schema 映射（易标 L895–903 ``jsonValidationSchemas`` 逐结构移植）。

    ``no_technical_score_mode=True`` 时不装评分两件套与 leaf-allocation
    （对应易标 ``...(!noTechnicalScoreMode ? {...} : {})`` 展开）。
    """
    schemas: dict = {OUTLINE_OUTPUT_FILE: OUTLINE_JSON_SCHEMA}
    if not no_technical_score_mode:
        schemas[TECHNICAL_SCORE_GROUPS_FILE] = TECHNICAL_SCORE_GROUPS_SCHEMA
        schemas[SCORE_DIRECTORY_PLAN_FILE] = SCORE_DIRECTORY_PLAN_SCHEMA
        schemas[LEAF_ALLOCATION_FILE] = create_leaf_allocation_schema(
            1 if standalone_technical else 2)
    schemas[OUTLINE_REVIEW_FILE] = OUTLINE_REVIEW_SCHEMA
    return schemas


def make_json_schema_validator(schema: dict):
    """把 Draft-07 Schema 包装成 P2 ``validator(value) -> (ok, errors)`` 契约。

    ``errors`` 由 ``json_validation._normalize_errors`` 归一为 Ajv 风格
    ``instancePath`` / ``schemaPath`` / ``keyword`` / ``message`` / ``params``，
    与易标 ``outputAjv.compile(schema)`` 的消费方式等价。
    """
    compiled = Draft7Validator(schema)

    def _validator(value):
        errors = _normalize_errors(compiled.iter_errors(value))
        return (not errors, errors or None)

    return _validator


def build_output_validators(schemas: dict | None = None, *,
                            no_technical_score_mode: bool = True,
                            standalone_technical: bool = False) -> dict:
    """编译提交校验器表（易标 L904–906 ``outputValidators``）。

    - 传 ``schemas`` 时按给定表编译；
    - 缺省时按 ``build_json_validation_schemas`` 现构建后编译。
    """
    if schemas is None:
        schemas = build_json_validation_schemas(
            no_technical_score_mode=no_technical_score_mode,
            standalone_technical=standalone_technical)
    return {file: make_json_schema_validator(schema)
            for file, schema in schemas.items()}


__all__ = [
    "OUTLINE_JSON_SCHEMA",
    "ROOT_NODE_SCHEMA",
    "SCORE_DIRECTORY_PLAN_SCHEMA",
    "TECHNICAL_SCORE_GROUPS_SCHEMA",
    "OUTLINE_REVIEW_SCHEMA",
    "build_json_validation_schemas",
    "build_output_validators",
    "create_directory_node_schema",
    "create_leaf_allocation_schema",
    "make_json_schema_validator",
]
