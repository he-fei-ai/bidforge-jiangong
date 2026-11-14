"""OmpTaskAgent → 平台目录树的转换桥（阶段 2 集成 seam，router-free）。

把 ``OmpTaskAgent.generate_outline`` 产出的**扁平** ``GeneratedSection`` 列表折叠成
``_save_outline_to_db`` 所需的**嵌套**目录树 ``{title, level, description, children}``。

⚠️ 分层约束（``test_services_never_import_routers``）：本模块属 services 层，**禁止 import
routers**。真正的 DB 落库由 ``routers/sections.py`` 的旁路端点（``generate_outline_via_omp``）
调用 ``_save_outline_to_db`` 完成；本模块只产出目录树，绝不触碰 sections 表。

⚠️ 不预填 ``parent_id`` / ``sort_order`` / ``path``（点分编号）：这些由 ``_save_outline_to_db``
内部的 ``_process_nodes`` + ``normalize_outline`` 递归算出，调用方（routers 侧）直接把树喂给
落库器即可。
"""

from __future__ import annotations

import logging

from app.services.ai.omp_task_agent import GeneratedSection, OmpTaskAgent, OutlineResult

logger = logging.getLogger("ai_provider.omp_task")


def fold_generated_sections_to_tree(sections: list[GeneratedSection]) -> list[dict]:
    """把扁平的 ``GeneratedSection`` 列表按 ``level`` 折叠成嵌套目录树。

    规则：每个节点追加到「最近一个 ``level`` 更小的前驱节点」的 ``children`` 下；
    ``level`` 相等则为同级兄弟。返回根级节点列表（每项 dict：``title`` / ``level`` /
    ``description`` / ``children``）。

    边界处理：
      - ``level`` 跳级（如 1 之后直接 3）：3 挂在最近的更小前驱（1）下，符合"就近父"语义；
      - ``level`` 回退（如 3 之后来 1/2）：弹出栈直到找到更小的父，自动复位层级关系；
      - 空输入返回 ``[]``。
    """
    root: list[dict] = []
    # 栈元素：(level, children_container) —— children_container 是该 level 节点应挂的列表
    stack: list[tuple[int, list[dict]]] = [(0, root)]
    for s in sections:
        # ✅ R231（2026-11-13 · 防御性钳制）：level≤0 会让 ``(0, root)`` 哨兵被
        #    ``0 >= level`` 弹空 ⇒ ``stack[-1]`` IndexError。解析侧
        #    （parse_outline_json 容器根）已不产出 level≤0，此处钳制兜住
        #    任何未来生产者——level 非法时按一级章处理（与平台「一级章」语义
        #    一致），绝不崩。
        s_level = s.level if isinstance(s.level, int) and s.level >= 1 else 1
        node: dict = {
            "title": s.title,
            "level": s_level,
            "description": s.description,
            "children": [],
        }
        while stack and stack[-1][0] >= s_level:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((s_level, node["children"]))
    return root


async def generate_outline_tree(
    *,
    brief: str,
    project_type: str = "",
    industry: str = "",
    extra_instructions: str = "",
    system_prompt: str | None = None,
    api_key: str = "",
    model: str | None = None,
    tools: str | None = None,
    workspace_root: str | None = None,
    timeout: int | None = None,
    keep_workspace: bool | None = None,
) -> list[dict]:
    """让 omp 生成目录并折成平台目录树（**不直接落库**）。

    安全闸门在 ``OmpTaskAgent`` 内部：未启用 / 二进制缺失 / 未配模型 → ``RuntimeError``，
    调用方据此回退到主目录生成链路。构造参数透传给 ``OmpTaskAgent``，便于显式覆盖。
    """
    agent = OmpTaskAgent(
        api_key=api_key,
        model=model,
        tools=tools,
        workspace_root=workspace_root,
        timeout=timeout,
        keep_workspace=keep_workspace,
        system_prompt=system_prompt,
    )
    result: OutlineResult = await agent.generate_outline(
        brief=brief,
        project_type=project_type,
        industry=industry,
        extra_instructions=extra_instructions,
    )
    return fold_generated_sections_to_tree(result.sections)
