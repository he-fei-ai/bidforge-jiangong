"""P2 — 目录节点身份分配（移植自易标 ``technicalPlanOutline.cjs``）。

来源：``C:\\...\\yibiao_outline\\electron_services_technicalPlanOutline.cjs``
的 ``NODE_ID_PATTERN`` / ``numberOutline`` / ``collectOutlineIds`` /
``acceptAgentOutline``，全文 1:1 移植；Node ``crypto.randomUUID`` 以 Python
``uuid.uuid4()`` 等价替换（同为 UUID v4，与 ``NODE_ID_PATTERN`` 匹配）。

红线：目录身份 ``id`` 与编号 ``number`` 分离 —— 编号只反映当前树的位置，
绝不修改节点身份。
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

#: UUID v4 节点身份格式（易标原字符串常量，逐字保留）。
NODE_ID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def number_outline(items: Iterable[dict] | None, prefix: str = "") -> list[dict]:
    """编号只反映当前树的位置，绝不修改节点身份（易标 ``numberOutline``）。

    返回新列表：每项浅拷贝并写入 ``number``（根层为 ``"1"``/``"2"``…，
    子层为 ``"1.1"``/``"1.2"``…）；非空 ``children`` 递归编号，``id`` 原样保留。
    """
    numbered: list[dict] = []
    for index, item in enumerate(items or []):
        number = f"{prefix}.{index + 1}" if prefix else str(index + 1)
        node = {**item, "number": number}
        if item.get("children"):
            node["children"] = number_outline(item["children"], number)
        numbered.append(node)
    return numbered


def collect_outline_ids(
    items: Iterable[dict] | None,
    ids: set | None = None,
) -> set:
    """收集当前目录身份，用于接受下一阶段 Agent 输出（易标 ``collectOutlineIds``）。"""
    if ids is None:
        ids = set()
    for item in items or []:
        ids.add(item.get("id"))
        collect_outline_ids(item.get("children"), ids)
    return ids


def accept_agent_outline(items: Iterable[dict] | None,
                         known_ids: set | None = None) -> list[dict]:
    """Agent 只能沿用已提供的 ID；新节点必须填 ``None``（易标 ``id:null``），
    由程序一次性分配身份（易标 ``acceptAgentOutline``）。

    - 未知 ID 报错：``目录包含未知 ID：{id}；新节点请使用 id:null``；
    - 重复 ID 报错：``目录 ID 重复：{id}``；
    - 通过后整体 ``number_outline`` 编号（编号不改 ``id``）。

    与易标的取舍：节点缺失 ``id`` 键时按 ``id:null``（新节点）处理 —— 易标
    里 ``undefined !== null`` 会走未知 ID 分支，但 Schema 本就要求每节点
    显式给 ``id``，此差异仅存在于不合法产物上。
    """
    known_ids = set() if known_ids is None else known_ids
    seen: set = set()

    def visit(nodes: Iterable[dict] | None, root: bool = True) -> list[dict]:
        accepted: list[dict] = []
        for item in nodes or []:
            node_id = item.get("id")
            if node_id is not None and node_id not in known_ids:
                raise ValueError(
                    f"目录包含未知 ID：{node_id}；新节点请使用 id:null")
            node_id = str(uuid.uuid4()) if node_id is None else node_id
            if node_id in seen:
                raise ValueError(f"目录 ID 重复：{node_id}")
            seen.add(node_id)
            node: dict[str, Any] = {
                "id": node_id,
                "title": str(item.get("title") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
            if root:
                node["attr"] = item.get("attr")
            children = item.get("children")
            if children:
                node["children"] = visit(children, False)
            else:
                node["content_mode"] = item.get("content_mode")
                note = str(item.get("content_mode_note") or "").strip()
                if node.get("content_mode") == "other" and note:
                    node["content_mode_note"] = note
            accepted.append(node)
        return accepted

    return number_outline(visit(items))
