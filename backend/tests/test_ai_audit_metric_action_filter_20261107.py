# -*- coding: utf-8 -*-
"""ai_audit_logs 指标隔离护栏（2026-11-07）

``agent_*`` 状态迁移流水与 AI 调用共用 ai_audit_logs（见
services/agent/state_machine.py::_audit）。``/system/activity`` 的 ``tokens_today``
与 ``last_call_at`` 是**整表聚合**，旧实现没有 action 过滤：

- 迁移行一落库，「最近一次 AI 调用时间」就被顶成「最近一次状态迁移时间」；
- 非 chat 行若带 token，今日 token 会虚增。

本文件直接取 system.py **源码里的真实 SQL 原文**（AST 提取，不复制一份），
在内存库上执行：去掉 action 过滤，两条断言分别红。
"""

import ast
import pathlib
import re
import sqlite3
import time

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
SYSTEM_SRC = (APP_DIR / "routers" / "system.py").read_text(encoding="utf-8")
PROVIDER_SRC = (APP_DIR / "services" / "ai" / "provider_factory.py").read_text(encoding="utf-8")
COMPLIANCE_SRC = (APP_DIR / "routers" / "compliance.py").read_text(encoding="utf-8")

TODAY = time.strftime("%Y-%m-%d")
CHAT_AT = f"{TODAY} 09:00:00"
AGENT_LATE_AT = f"{TODAY} 12:00:00"
NONCALL_WITH_TOKENS_AT = f"{TODAY} 11:00:00"

_DDL = """
CREATE TABLE ai_audit_logs (
    id TEXT PRIMARY KEY,
    provider_name TEXT DEFAULT '',
    model TEXT DEFAULT '',
    action TEXT NOT NULL,
    success INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    scene TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    config_id TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    error_class TEXT DEFAULT '',
    attempt_count INTEGER DEFAULT 1,
    attempts_json TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def _query_sql() -> str:
    """从 system.py 源码里取含 last_call_at 的那条审计聚合 SQL（不复制副本）。"""
    for node in ast.walk(ast.parse(SYSTEM_SRC)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and "last_call_at" in node.args[0].value
        ):
            return node.args[0].value
    raise AssertionError("system.py 中未找到含 last_call_at 的审计聚合 SQL")


def _ai_actions() -> tuple:
    """从 system.py 源码里取过滤集合常量（不经模块导入，避免运行时副作用）。"""
    for node in ast.parse(SYSTEM_SRC).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_AI_AUDIT_ACTIONS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("system.py 缺少模块常量 _AI_AUDIT_ACTIONS")


def _log_audit_literals() -> set:
    """provider_factory 实际写入的 action 字面量（漏斗的 _log_audit 调用）。"""
    out = set()
    for node in ast.walk(ast.parse(PROVIDER_SRC)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_log_audit"):
            continue
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant) and isinstance(node.args[2].value, str):
            out.add(node.args[2].value)
        for kw in node.keywords:
            if kw.arg == "action" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                out.add(kw.value.value)
    return out


def _compliance_action() -> str:
    m = re.search(r'COMPLIANCE_BATCH_ACTION\s*=\s*"([^"]+)"', COMPLIANCE_SRC)
    assert m, "compliance.py 缺少 COMPLIANCE_BATCH_ACTION 常量"
    return m.group(1)


def _seed_row(cur, action: str, created_at: str, *, prompt: int = 0, completion: int = 0) -> None:
    cur.execute(
        "INSERT INTO ai_audit_logs (id, action, success, prompt_tokens, completion_tokens, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (f"{action}@{created_at}", action, 1, prompt, completion, created_at),
    )


def _fetch() -> dict:
    sql = _query_sql()
    actions = _ai_actions()
    assert sql.count("?") == 2 * len(actions) + 1, (
        f"占位符数量 {sql.count('?')} 与过滤集合 {actions} 不匹配，需同步本护栏"
    )
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(_DDL)
    # 真实成功调用：150 token
    _seed_row(cur, "chat", CHAT_AT, prompt=100, completion=50)
    # 状态迁移流水：晚于最后一次调用（无 token）
    _seed_row(cur, "agent_run_start", AGENT_LATE_AT)
    # 非调用行：带 token，且早于 agent 那条
    _seed_row(cur, "agent_stage_start", NONCALL_WITH_TOKENS_AT, prompt=500, completion=500)
    row = cur.execute(sql, (*actions, *actions, f"{TODAY} 00:00:00")).fetchone()
    con.close()
    return dict(row)


def test_action_filter_set_is_derived_from_actual_writers():
    """过滤集合必须等于两个真实写入方的 action 全集，不得凭印象增删。"""
    expected = _log_audit_literals() | {_compliance_action()}
    assert set(_ai_actions()) == expected, (
        f"过滤集合与写入方实际 action 不一致：常量 {sorted(_ai_actions())}，"
        f"实际写入 {sorted(expected)}"
    )


def test_last_call_at_ignores_agent_transition_rows():
    """晚于最近一次 chat 的 agent_run_start 不得顶掉 last_call_at。"""
    assert _fetch()["last_call_at"] == CHAT_AT


def test_tokens_today_ignores_non_call_rows():
    """非 chat 行的 token 不得计入 tokens_today。"""
    assert _fetch()["tokens_today"] == 150
