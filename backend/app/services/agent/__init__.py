"""智能体编排层（R179 Phase G，2026-11-07）。

当前仅包含 :mod:`state_machine`：以数据库为唯一事实源的 stage 状态机，
提供「run/stage 迁移校验 + 产物 hash 幂等 + 断点续跑 + 迁移审计留痕」。
"""

from __future__ import annotations
