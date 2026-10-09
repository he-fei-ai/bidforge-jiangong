"""SSE 任务成果 checkpoint 持久化 + DB 瞬态重试。

从 sse_handlers.py 拆出，负责：
- 把目录/正文/事实生成的终态成果写入 task_registry.checkpoint_json
- 断线重挂接时回读 checkpoint
- 对 SQLite "database is locked" 瞬态错误做指数退避重试
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime

from app.db import get_conn, get_read_conn, release_read_conn

logger = logging.getLogger("sse.checkpoint")

#: checkpoint 写库瞬态失败的重试次数 / 退避基数（秒）
CHECKPOINT_WRITE_RETRIES = 2
CHECKPOINT_WRITE_BASE_DELAY = 0.2

#: facts checkpoint 回传字段白名单（与在线 completed 事件消费的键保持一致）。
#: 刻意**不含** groups/facts 明细：成果本身已落 global_facts 表，前端重挂接后
#: 会走 loadFacts() 重新拉取；把明细塞进 checkpoint 只会让 task_registry 行膨胀。
_FACTS_CHECKPOINT_FIELDS = (
    "segment_stats", "cross_conflicts", "warnings", "group_count", "total_items",
)


async def _retry_db_locked(factory, max_retries: int = 3,
                           base_delay: float = 0.2):
    """对「database is locked / disk I/O error」做有限重试，其余异常原样抛出。

    背景（2026-09-23）：SQLite 在并发写 + busy_timeout 耗尽时会抛
    OperationalError("database is locked")。这类错误是**瞬态**的，退避后
    重试通常即成功；而把它与「no such table」这类确定性错误混在一起
    直接抛出，会让并发写场景出现难以复现的随机失败。

    只重试两类**瞬态**错误：
      · "database is locked"
      · "disk I/O error"
    其余 sqlite3.OperationalError（如 no such table）与非 sqlite3 异常
    一律原样抛出 —— 重试它们只是浪费时间并掩盖真实缺陷。

    退避为指数递增（base_delay × 2^尝试次数），总尝试次数 = 1 + max_retries。
    max_retries=0 即不重试。
    """
    attempts = max(0, int(max_retries))
    last_exc: Exception | None = None
    for i in range(attempts + 1):
        try:
            return await factory()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            transient = ("database is locked" in msg or "disk i/o error" in msg)
            if not transient:
                raise
            last_exc = e
            if i >= attempts:
                break
            await asyncio.sleep(base_delay * (2 ** i))
    if last_exc is not None:
        raise last_exc
    return await factory()


async def _write_task_checkpoint_db(task_id: str, kind: str,
                                   payload: dict) -> None:
    """把 checkpoint 真正写入 task_registry.checkpoint_json。

    独立成函数是为了让 `_save_task_checkpoint` 能整体重试（单测可 monkeypatch
    本函数注入"首次 locked、次次成功"来验证重试链路）。
    """
    conn = await get_conn()
    await conn.execute(
        "UPDATE task_registry SET checkpoint_json=?, updated_at=? WHERE id=?",
        (json.dumps({"kind": kind, **(payload or {})}, ensure_ascii=False),
         datetime.now().isoformat(), task_id))
    await conn.commit()


async def _save_task_checkpoint(task_id: str, kind: str, payload: dict):
    """把任务终态成果写入 task_registry.checkpoint_json（kind 区分任务类型）。

    payload 统一带 `event` 字段（completed/stopped/error），便于前端判断来源。

    ✅ 瞬态写失败自动重试（2026-09-23）：checkpoint 是断线重挂接的**唯一**
    数据源，一次 "database is locked" 就让用户刷新后拿不到任何成果。
    通过 _retry_db_locked 对 locked / disk I/O 退避重试（确定性错误不重试）。

    ✅ 通过 sse_handlers 命名空间调用 _write_task_checkpoint_db（延迟 import 避免
    循环依赖），这样测试 monkeypatch ``sse_handlers._write_task_checkpoint_db``
    时本函数会拿到 mock 版本，保持与拆分前的测试兼容。
    """
    from app.routers import sse_handlers  # 延迟 import：避免循环依赖
    await _retry_db_locked(
        lambda: sse_handlers._write_task_checkpoint_db(task_id, kind, payload),
        max_retries=CHECKPOINT_WRITE_RETRIES,
        base_delay=CHECKPOINT_WRITE_BASE_DELAY)


async def _save_outline_checkpoint(task_id: str, payload: dict):
    """把目录生成终态成果（outline/review/failed_chapters）写入 checkpoint_json。"""
    await _save_task_checkpoint(task_id, "outline_result", payload)


async def _save_content_checkpoint(task_id: str, payload: dict):
    """把正文生成成果清单（done/failed_sections/word_count）写入 checkpoint_json。

    ✅ 增强（2026-09-16）：正文内容本身已逐章落库（sections.content），
    但「本次任务生成了哪几章、哪几章失败、为什么失败」只在 SSE 事件里出现过。
    """
    await _save_task_checkpoint(task_id, "content_result", payload)


def _facts_checkpoint_payload(frontend_data: dict) -> dict:
    """从 format_for_frontend 载荷中筛出可安全落 checkpoint 的字段。"""
    if not isinstance(frontend_data, dict):
        return {}
    return {k: v for k, v in frontend_data.items() if k in _FACTS_CHECKPOINT_FIELDS}


async def _save_facts_checkpoint(task_id: str, payload: dict):
    """把全局事实提取终态成果写入 checkpoint_json。"""
    await _save_task_checkpoint(task_id, "facts_result", payload)


async def _load_task_checkpoint(task_id: str, kind: str) -> dict | None:
    """读取指定类型的任务成果 checkpoint（不匹配的 kind 返回 None）。"""
    conn = await get_read_conn()
    try:
        cur = await conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id=?", (task_id,))
        row = await cur.fetchone()
    finally:
        await release_read_conn(conn)
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("kind") == kind:
        return data
    return None


async def _checkpoint_partial_outline(task_id: str, outline, failed_chapters,
                                      *, event: str = "stopped") -> bool:
    """落库「部分成果」checkpoint（客户端断开/用户停止时的兜底）。

    返回是否成功落库（失败静默，绝不影响收尾流程）。
    """
    outline = outline or []
    if not outline and not failed_chapters:
        return False
    payload = {"event": event, "task_id": task_id, "outline": outline}
    if failed_chapters:
        payload["failed_chapters"] = list(failed_chapters)
        payload["failed_count"] = len(failed_chapters)
    try:
        await _save_outline_checkpoint(task_id, payload)
        return True
    except Exception:
        logger.warning("检查点写入失败（task=%s · 目录生成 · 部分成果）", task_id, exc_info=True)
        return False


async def _load_outline_checkpoint(task_id: str) -> dict | None:
    """读取目录生成成果 checkpoint（薄封装，语义与 kind 校验见 _load_task_checkpoint）。"""
    return await _load_task_checkpoint(task_id, "outline_result")
