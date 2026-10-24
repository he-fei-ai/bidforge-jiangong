"""目录生成四标准结构（引入自《标书智能体（五）——弱模型稳定输出复杂 JSON》）。

外部引用软件（OpenBidKit 易标）以 Pydantic 定义 OutlineItem / OutlineResponse /
OutlineChildrenResponse / OutlineReviewResponse 四结构，走「JSON 解析 → Schema 校验 →
业务校验 → 定向修复」流水线。本模块按同名同字段落地，但采用**宽松校验**策略：

- title / description 等文本字段做宽松强制转换（弱模型输出数字/None 不整次判死）；
- children 缺失或 None 归一为 []（合法叶节点），仅非列表类型才报错；
- 额外字段一律忽略（模型自发补充字段不触发修复轮）；
- 校验错误统一转为「路径: 原因」式问题清单，与 _format_json_issues 口径一致。

冲突裁决：外部文档提出的 L1~L5 深度与 L2=3~5 / L4=4~7 数量约束与本仓
三级标准（MAX_OUTLINE_DEPTH=3、L2 2~3、L3 1~3）冲突，按用户规则以外部引用
软件的三级标准为准，**不引入** L4/L5；故 OutlineItem.children 只做结构校验，
深度与数量业务校验仍由 sse_handlers._validate_outline 单一事实源负责。
"""
from __future__ import annotations

from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


def _to_str(v: Any) -> str:
    """宽松文本强制转换：None → ""，其余 → str(v)。"""
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _to_children(v: Any, name: str = "children") -> list:
    """列表字段宽松归一：None → []，列表原样，其余类型报错（报字段名便于定位）。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    raise ValueError(f"{name} 不是列表")


class OutlineItem(BaseModel):
    """目录节点（宽松版）。id / source_* 字段模型可能省略，给默认值不报错。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    title: str = ""
    description: str = ""
    source_requirement_id: str = ""
    source_requirement_title: str = ""
    children: list["OutlineItem"] = Field(default_factory=list)
    content: str = ""

    @field_validator(
        "id", "title", "description",
        "source_requirement_id", "source_requirement_title", "content",
        mode="before",
    )
    @classmethod
    def _norm_text(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("children", mode="before")
    @classmethod
    def _norm_children(cls, v: Any) -> list:
        return _to_children(v)


class OutlineResponse(BaseModel):
    """完整目录结果（oneshot / 回灌修复 / 审核合并链路）。"""

    model_config = ConfigDict(extra="ignore")

    outline: list[OutlineItem] = Field(default_factory=list)

    @field_validator("outline", mode="before")
    @classmethod
    def _norm_outline(cls, v: Any) -> list:
        return _to_children(v, "outline")  # 同口径：None → []，非列表报错


class OutlineChildrenResponse(BaseModel):
    """分步生成的子目录结果（level1 / sublevel / sublevel_batch 的逐章产出）。

    字段别名说明（冲突裁决）：外部标准结构字段名为 ``children``；本仓提示词
    （outline_level1_system / outline_sublevel_system）出厂契约是顶层 ``outline``。
    按「外部标准为主」以 ``children`` 为规范字段名，同时用 ``AliasChoices``
    接受本仓既有的 ``outline`` 顶层键 —— 两侧都能校验，不破坏任何既有调用点。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    children: list[OutlineItem] = Field(
        default_factory=list,
        validation_alias=AliasChoices("children", "outline"),
    )

    @field_validator("children", mode="before")
    @classmethod
    def _norm_children(cls, v: Any) -> list:
        return _to_children(v, "children")


class OutlineReviewResponse(BaseModel):
    """目录审核结果：passed + suggestions（外部标准结构）。"""

    model_config = ConfigDict(extra="ignore")

    passed: bool = False
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("suggestions", mode="before")
    @classmethod
    def _norm_suggestions(cls, v: Any) -> list:
        if v is None:
            return []
        if isinstance(v, str):  # 弱模型常给单串，转单元素数组保留内容
            return [v]
        if isinstance(v, list):
            return v
        return [str(v)]


def schema_issues(schema: Any, data: Any) -> list[str]:
    """Pydantic 校验 → 「路径: 原因」式问题清单；通过返回 []。

    与外部 _format_json_issues 口径一致，例如：
    ``outline.0.children.1.title: Field required``。
    """
    try:
        schema.model_validate(data)
    except ValidationError as e:
        out: list[str] = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(根)"
            out.append(f"{loc}: {err.get('msg', '校验失败')}")
        return out
    except Exception as e:  # 结构极端异常不阻断（fail-soft）
        return [f"Schema 校验异常: {e}"]
    return []
