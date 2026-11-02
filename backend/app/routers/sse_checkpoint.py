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

from app.db import get_conn, get_read_conn, release_read_conn, safe_rowcount
from app.services.checkpoint_schema import (
    FACTS_CHECKPOINT_FIELDS as SCHEMA_FACTS_CHECKPOINT_FIELDS,
)

logger = logging.getLogger("sse.checkpoint")

#: checkpoint 写库瞬态失败的重试次数 / 退避基数（秒）
CHECKPOINT_WRITE_RETRIES = 2
CHECKPOINT_WRITE_BASE_DELAY = 0.2

#: facts checkpoint 回传字段白名单（与在线 completed 事件消费的键保持一致）。
#: 刻意**不含** groups/facts 明细：成果本身已落 global_facts 表，前端重挂接后
#: 会走 loadFacts() 重新拉取；把明细塞进 checkpoint 只会让 task_registry 行膨胀。
#:
#: ✅ R89（2026-10-17）：改为**由单一事实源派生**，不再手写第二份清单。
#:   `checkpoint_schema.FACTS_CHECKPOINT_FIELDS` 是完整 kind 契约（含 event /
#:   message —— 那两项由调用方在筛选**之外**补入，故不属于本过滤器的范围）；
#:   本常量 = 该契约去掉 event/message 后的内容部分。这样：
#:     · 新增终态字段只需改 schema 一处，写入侧筛选与前端生成的 TS 类型同步生效；
#:     · `sh._CHECKPOINT_KINDS["facts_generation"][1]` 与
#:       `cs.CHECKPOINT_KINDS["facts_generation"][1]` 恒等（由 sse_handlers 拼装）。
#:   旧实现在两处各写一份且**无任何 parity 断言**，分叉时测试不会红。
_FACTS_CHECKPOINT_FIELDS = tuple(
    f for f in SCHEMA_FACTS_CHECKPOINT_FIELDS if f not in ("event", "message")
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
    # ✅ 2026-10-12（技术债 P2-13 · 删除不可达的死代码）：
    #    此处原有一行 ``return await factory()``。它**永远不可达** ——
    #    ``attempts = max(0, int(max_retries))`` ⇒ 循环至少执行 1 次，每次迭代
    #    要么 return（成功）、要么 raise（非瞬态错误）、要么 break（重试耗尽且
    #    last_exc 已置位），退出循环后 last_exc 必不为 None，上面已经 raise。
    #    危害不在运行期而在**可读性**：读到这行的人会以为「重试耗尽后还会再试
    #    一次」，从而把真实总尝试次数误算成 1+max_retries+1，与 docstring 的
    #    「1 + max_retries」相矛盾 —— 属于本仓反复记录的「注释宣称一套、
    #    实现看起来是另一套」。删掉后 docstring 与实际实现一致。
    raise AssertionError(  # pragma: no cover - 结构性不可达，仅防未来改坏循环
        "_retry_db_locked 退出循环时 last_exc 必不为 None")


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
    # ✅ BUG 修复（2026-10-28 · 断线路径 partial 缺键）：
    #    _checkpoint_partial_outline 是**客户端断开**（GeneratorExit → finally 兜底）
    #    的唯一收尾出口，旧实现从不写 partial=True —— 而显式停止路径
    #    _finish_stopped_with_partial（sse_handlers）恒写 partial=True。
    #    断线重挂接时 `_attach_checkpoint_result` 按白名单回传 partial，缺失时
    #    前端 buildPartialOutlineHint 拿到 undefined ⇒ 把「用户断开时只生成了
    #    一部分」当成「完整目录」展示，用户可能把半份目录当完整保存（R89 S3
    #    注释「同口径带 partial」的承诺就此落空）。
    #    本函数语义即「部分成果」，到达此处 outline 或 failed_chapters 必非空，
    #    恒置 partial=True（与 _finish_stopped_with_partial 同口径，不再分叉）。
    payload["partial"] = True
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


#: 终态任务状态词表（单一事实源：sse_handlers._TERMINAL_STATUSES 改为本常量的别名，
#: 见 R154）。运行中/暂停任务的 partial checkpoint 是断线重挂的**活兜底**，绝不清。
TERMINAL_TASK_STATUSES = frozenset({"completed", "degraded", "failed", "stopped"})


async def _clear_outline_checkpoints_for_scheme(db, scheme_id: str) -> int:
    """目录整表重建成功后，清理该方案**终态任务**的目录成果 checkpoint。

    ✅ R154（R149 遗留 L-3 · 终态 checkpoint 永不失效）：目录成果经
    ``/sse/task/{id}`` 的 ``_attach_checkpoint_result`` 断线重挂回传（kind=
    ``outline_result``），前端据此走「保存确认」流程。但用户**已保存新目录**
    （或套用目录库整表重建）之后，旧任务的 checkpoint 仍持久有效——刷新页面
    重挂接时旧树作为「待确认成果」复活，用户确认即用陈旧树覆盖自己更新过的
    目录。修法：整表重建（_save_outline_to_db 是 save-outline 与 apply-and-save
    的共同落库出口）成功提交后，按 scheme 清掉终态任务的 outline_result。
    只清终态：运行中任务的 partial checkpoint 是生成中途断线的唯一恢复源。

    失败**绝不阻断**目录保存（advisory 语义，与导出缓存失效同口径）。
    返回清理的任务数（供加法式回传 `cleared_checkpoints`）。
    """
    try:
        cur = await db.execute(
            "SELECT id, status, checkpoint_json FROM task_registry WHERE scheme_id=?",
            (scheme_id,))
        rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("读取任务 checkpoint 失败（task 清理跳过，scheme=%s）",
                       scheme_id, exc_info=True)
        return 0
    stale_ids: list[str] = []
    for row in rows:
        if row["status"] not in TERMINAL_TASK_STATUSES or not row["checkpoint_json"]:
            continue
        try:
            data = json.loads(row["checkpoint_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == "outline_result":
            stale_ids.append(row["id"])
    if not stale_ids:
        return 0
    try:
        placeholders = ",".join("?" for _ in stale_ids)
        cur = await db.execute(
            f"UPDATE task_registry SET checkpoint_json=NULL WHERE id IN ({placeholders})",
            stale_ids)
        await db.commit()
        # ✅ R13 纪律：行数一律走 safe_rowcount 集中化出口（裸 .rowcount 被全量
        #    扫描器拦截；0 行 = 并发下任务行已消失，按预期清理数回退）。
        n = safe_rowcount(cur)
        return n if n > 0 else len(stale_ids)
    except Exception:  # noqa: BLE001
        logger.warning("清理目录成果 checkpoint 失败（不影响保存，scheme=%s）",
                       scheme_id, exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
