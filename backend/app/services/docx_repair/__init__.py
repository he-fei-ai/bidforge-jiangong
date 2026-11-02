# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮「检查与修复」子模块（R147）。

第一轮（初稿）沿用 ``app.routers.export._build_docx_sync``；本包实现第二轮：
对初稿 DOCX 的 OpenXML 部件做只读检查（inspector）→ 程序精确修复（fixes）
→ 复杂排版错乱 Agent 辅助（agent_fix）→ 编排与回滚（pipeline）。

设计契约：
- 同栈：zipfile + 正则 + python-docx（不引入 lxml / 不做 python-docx
  「打开-保存」往返，规避 OMML 与域代码被改写的风险）；
- 检查只读、修复显式记录（RepairAction），未收敛项进 ``unresolved``；
- 所有修复按 ECMA-376 CT_* 严格序列插入（本包内自带顺序表，与
  ``export.py:_STRICT_CT_SEQUENCE`` 同源口径）。
"""
from app.services.docx_repair.model import (
    RepairAction,
    RepairFinding,
    RepairReport,
)
from app.services.docx_repair.pipeline import run_two_round_repair

__all__ = [
    "RepairAction",
    "RepairFinding",
    "RepairReport",
    "run_two_round_repair",
]
