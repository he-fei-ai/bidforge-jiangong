"""Oh My Pi (omp) 深度能力层（R234 增量）。

本项目在 2026-11-12/13（R230~R233）已把 omp 作为 AGENT 集成：三环节角色与
plan/execute/fix 模板（omp_agent_roles）、RPC NDJSON 客户端（omp_rpc）、
批式编排状态机（omp_workflow）、文件工作区 Agent（omp_task_agent）、
Provider 包装（providers/omp_provider）等。

本子包补强 omp 的**深度能力**——已有集成尚未映射的机制：

* ``ompi_memory`` —— OMP ``retain/recall/reflect/memory_edit`` 记忆银行（跨会话上下文交接）；
* ``ompi_advisor`` —— OMP advisor 旁观模型（第二模型监督每一轮，注入 note 让主 Agent 纠偏）；
* ``ompi_fix`` —— OMP hashline 内容哈希锚点（结构化修复，stale 锚拒绝，防过期文件损坏）；
* ``ompi_task_schema`` —— OMP task 扇出后的 schema 校验回收（``agent://<id>/field.path`` 拉取）。

⚠️ 分层约束：services 层禁止 import routers（对齐 omp_workflow 约定）。
⚠️ 本层全部为**确定性、可注入、无副作用的纯逻辑**——不拉起真实 omp 子进程，
   校验与编排由既有 ``OmpBatchOrchestrator`` 承载，深度层只做增强。
⚠️ 与 omp 上游能力的对应关系见 ``docs/oh-my-pi_深度探索与集成_R234_20261113.md``。
"""

from __future__ import annotations

from .ompi_advisor import AdvisorGuard, AdvisorNote
from .ompi_fix import apply_hashline, hashline_anchor, is_hashline_anchor, stale_anchor_error
from .ompi_memory import MemoryBank, MemoryEntry
from .ompi_task_schema import MISSING, pull_path, validate_yield

__all__ = [
    "AdvisorGuard",
    "AdvisorNote",
    "MISSING",
    "MemoryBank",
    "MemoryEntry",
    "apply_hashline",
    "hashline_anchor",
    "is_hashline_anchor",
    "pull_path",
    "stale_anchor_error",
    "validate_yield",
]
