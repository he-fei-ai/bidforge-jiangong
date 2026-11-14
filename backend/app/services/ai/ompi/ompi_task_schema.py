"""OMP task 扇出 schema 校验与字段回收适配层。

对应 omp 上游能力（README「batch 形态 ``{ context, tasks[] }``」与
``agent://<id>/findings.0.path`` 拉取子 Agent 输出字段）：
  - ``task`` 工具扇出多个子 Agent，各自返回结构化结果（如 ``findings`` 数组）；
  - 宿主可用 ``agent://<id>/field.path`` 从子 Agent 输出中拉取指定字段做校验/回收。

本层提供确定性实现：
  - ``validate_yield(payload, schema)`` —— 对扇出结果做 schema 校验，返回
    ``(ok, errors)``；schema 是嵌套类型契约，缺键/类型不符/容器形态不符均记 error；
  - ``pull_path(data, "findings.0.path")`` —— 按点路径拉取嵌套字段，支持列表下标
    （``findings.0.path``）与键路径（``meta.summary``）；缺失返回 ``MISSING`` 哨兵。

schema 契约（确定性、可注入）：
  - 叶子：Python 类型对象（``str/int/float/bool/list/dict`` 或 ``type(None)``）；
  - 容器：``dict[str, schema]`` 表示对象、``list[schema]`` 表示数组（元素递归校验）；
  - 键的必需性：默认全部必需；传入 ``optional`` 集合可豁免（缺失不记 error）；
  - ``Any``（``typing.Any``）表示不校验类型。

⚠️ 分层约束：services 层禁止 import routers；本层纯函数，零网络、零数据库访问。
"""

from __future__ import annotations

from typing import Any

#: ``pull_path`` 缺失哨兵：与 None 区分（字段存在但值为 None）。
MISSING = object()


def pull_path(data: Any, path: str) -> Any:
    """按点路径拉取嵌套字段。

    支持：
        pull_path({"findings": [{"path": "a.md"}]}, "findings.0.path")  -> "a.md"
        pull_path({"meta": {"summary": "ok"}}, "meta.summary")          -> "ok"

    路径段为纯数字时按列表下标取（字符串键优先）；任一段缺失 / 越界 / 类型不符
    返回 ``MISSING``（不抛异常——校验器宁缺勿崩）。
    """
    if not isinstance(path, str) or not path.strip():
        return MISSING
    cur = data
    for seg in path.strip().split("."):
        if seg == "":
            return MISSING
        if isinstance(cur, dict):
            if seg not in cur:
                return MISSING
            cur = cur[seg]
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(seg)
            except (TypeError, ValueError):
                return MISSING
            if idx < 0 or idx >= len(cur):
                return MISSING
            cur = cur[idx]
        else:
            return MISSING
    return cur


def _schema_leaf(schema: Any) -> type:
    """把 schema 叶子归一为可做 isinstance 判定的类型对象。"""
    if schema in (None, type(None)):
        return type(None)
    if schema is Any:
        return object
    if isinstance(schema, type):
        return schema
    # 允许 ``list[str]`` 形态（types.GenericAlias）：取其 origin。
    origin = getattr(schema, "__origin__", None)
    if isinstance(origin, type):
        return origin
    return object


def _check(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    """递归校验 value 是否符合 schema，错误追加到 errors（路径定位）。"""
    # 容器：list[schema]
    if isinstance(schema, list):
        elem_schema = schema[0] if schema else object
        if not isinstance(value, list):
            errors.append(f"{path}: 期望数组，实际 {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _check(item, elem_schema, f"{path}[{i}]", errors)
        return
    # 容器：dict[str, schema]
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: 期望对象，实际 {type(value).__name__}")
            return
        for k, sub in schema.items():
            if k not in value:
                errors.append(f"{path}.{k}: 缺失键")
                continue
            _check(value[k], sub, f"{path}.{k}" if path else str(k), errors)
        return
    # 叶子：类型对象
    if isinstance(schema, type) or getattr(schema, "__origin__", None) is not None:
        typ = _schema_leaf(schema)
        if not isinstance(value, typ):
            errors.append(
                f"{path}: 期望 {getattr(typ, '__name__', str(typ))}，实际 {type(value).__name__}"
            )
        return
    errors.append(f"{path}: 无法识别的 schema 类型 {schema!r}")


def validate_yield(
    payload: Any, schema: dict | list, *, optional: set[str] | None = None
) -> tuple[bool, list[str]]:
    """对 task 扇出结果做 schema 校验。

    参数：
        payload  —— 子 Agent 返回的结构化结果（dict / list）；
        schema   —— 嵌套类型契约（见模块 docstring）；
        optional —— 可选键集合（点路径形式，如 ``{"meta.summary"}``），缺失豁免。

    返回：
        (ok, errors)：ok=True 表示通过；errors 为逐条错误（空列表表示通过）。
    """
    errors: list[str] = []
    _check(payload, schema, "$", errors)
    if optional:
        # 从错误中剔除「缺失键」但键在 optional 里的项（仅豁免缺失，类型不符仍报）。
        kept: list[str] = []
        for e in errors:
            if e.endswith(": 缺失键"):
                key_path = e[:-len(": 缺失键")]
                if key_path in optional:
                    continue
            kept.append(e)
        errors = kept
    return (not errors), errors
