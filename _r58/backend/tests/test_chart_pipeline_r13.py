"""R13 回归测试：配图上限复核路径下 ``db.execute`` 返回 None 时不崩。

背景（详见《目录生成与正文生成 AI 调用次数优化方案 2026-09-22》§6 与
``logs/backend.log`` 的 R13 现象）：
- ``_chart_pipeline._load_scheme_type_counts`` 与 ``apply_inline_chart_plan`` 中，
  在全局单连接上做 SELECT 后直接 ``cur.fetchall()``。当连接处于异常/事务冲突状态时，
  ``execute()`` 可能返回 ``None``，直接取 ``.fetchall()`` 触发
  ``AttributeError: 'NoneType' object has no attribute 'fetchall'``。
- 该错误在 8 小时日志中出现 24 次（连续相同签名），说明同一类缺陷被反复触发。
- 修复：显式判空，走 fallback（跳过限额，行为与旧 except 分支一致）。

本测试用 monkeypatch 打桩让 ``db.execute`` 返回 ``None``，断言：
1. 不再抛 AttributeError；
2. 返回空 dict（fallback 语义）；
3. apply_inline_chart_plan 在同样条件下仍能写入登记行（不阻断业务）。
"""
from __future__ import annotations

import uuid

import pytest


class _BrokenExecuteDB:
    """伪连接：execute 恒返回 None，用于复现 R13。"""

    async def execute(self, sql, params=()):
        return None


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


@pytest.mark.asyncio
async def test_load_scheme_type_counts_survives_none_execute(monkeypatch):
    from unittest.mock import AsyncMock

    from app.routers import _chart_pipeline

    db = _BrokenExecuteDB()
    # 覆盖：函数体第一次 execute 返回 None
    db.execute = AsyncMock(return_value=None)
    result = await _chart_pipeline._load_scheme_type_counts(db, "scheme", "sec")
    assert result == {}
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_apply_inline_chart_plan_survives_none_execute_in_recheck(monkeypatch):
    """事务内复核走 SELECT 时如果 execute 返回 None，仍应正常写入登记。"""
    from app.routers._chart_pipeline import apply_inline_chart_plan

    plan_db_calls: list[tuple[str, tuple]] = []

    async def fake_execute(sql, params=()):
        # DELETE / INSERT 直接吃掉；SELECT（复核查询）返回 None 模拟 R13
        plan_db_calls.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            return None  # R13 场景
        return _FakeCursor([])

    class _DB:
        def __init__(self):
            self.rows_after_write: list[tuple] = []

        async def execute(self, sql, params=()):
            sql_low = sql.lstrip().upper()
            plan_db_calls.append((sql_low[:20], params))
            if sql_low.startswith("INSERT"):
                self.rows_after_write.append(params)
            return _FakeCursor([])

    db = _DB()
    db.execute = fake_execute  # type: ignore[assignment]

    section_id = uuid.uuid4().hex
    scheme_id = "scheme-a"
    # 构造一条 rows：(id, section_id, scheme_id, chart_type, source, budget, status, payload_json)
    row = (
        uuid.uuid4().hex,
        section_id,
        scheme_id,
        "timeline",
        "正文同步生成",
        5,
        "generated",
        "{}",
    )
    result = await apply_inline_chart_plan(db, section_id, [row], content="正文A")
    # 关键断言：
    # 1) 不抛异常（原代码会因 cur is None 抛 AttributeError）
    # 2) 登记行已写入（不受复核失败阻断）
    assert result == "正文A"
    inserts = [c for c in plan_db_calls if c[0].startswith("INSERT")]
    assert len(inserts) == 1


@pytest.mark.asyncio
async def test_apply_inline_chart_plan_still_enforces_limit_on_normal_path():
    """复核路径正常时，超出全方案同类型上限的登记仍要被跳过（回归保护）。"""
    from app.routers._chart_pipeline import (
        _CHART_SCHEME_TYPE_LIMITS,
        apply_inline_chart_plan,
    )

    class _DB:
        def __init__(self):
            self.rows_after_write: list[tuple] = []

        async def execute(self, sql, params=()):
            sql_low = sql.lstrip().upper()
            if sql_low.startswith("SELECT"):
                # 已存在同类型 10 条 → 远超默认限额
                return _FakeCursor([
                    {"chart_type": "timeline", "n": 10},
                ])
            if sql_low.startswith("INSERT"):
                self.rows_after_write.append(params)
            return _FakeCursor([])

    db = _DB()
    section_id = uuid.uuid4().hex
    scheme_id = "scheme-a"
    limit = _CHART_SCHEME_TYPE_LIMITS.get("timeline", 3)
    assert limit <= 3  # 默认 timeline 上限在个位数
    row = (
        uuid.uuid4().hex,
        section_id,
        scheme_id,
        "timeline",
        "正文同步生成",
        5,
        "generated",
        "{}",
    )
    await apply_inline_chart_plan(db, section_id, [row], content="正文B")
    # 已达上限，本次不写入
    assert db.rows_after_write == []
